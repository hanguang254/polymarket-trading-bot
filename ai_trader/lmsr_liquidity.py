"""
流动性评估模块 v3.0（LMSR理论定价 + 订单簿分析）

新增 LMSR 数学框架:
  - Cost Function: C(q) = b·ln(Σe^(qi/b))
  - Price Function (Softmax): pi = e^(qi/b) / Σe^(qj/b)
  - Trade Cost: C(q+δ) - C(q)
  - Inefficiency Detection: LMSR fair price vs CLOB actual price

CLOB 不是 LMSR AMM，但 LMSR 理论价格可作为"公允价格"参考线，
检测 CLOB 报价是否偏离理论值 → 无效率信号。

用途: ai_analyze_v2.py 的折价/无效率判断 + 流动性评估
"""
import math
import requests
from ai_trader.polymarket_api import extract_orderbook_levels
from ai_trader import clob_client


# ═══════════════════════════════════════════════════════════
#  LMSR 理论定价框架（文档公式 1-4）
# ═══════════════════════════════════════════════════════════

def lmsr_cost(q, b):
    """
    LMSR 成本函数（文档公式1）

    C(q) = b · ln(Σ e^(qi/b))

    Args:
        q: 各结果的未平仓量向量，如 [q_up, q_down]
        b: 流动性参数（b越大 → 流动性越深，做市商最大损失越大）

    Maximum market maker loss: L_max = b·ln(n)
    For binary (n=2) with b=100000: L_max ≈ $69,315
    """
    if b <= 0:
        return 0.0
    # 数值稳定: 减去最大值避免 exp 溢出
    max_q = max(q)
    shifted = [qi - max_q for qi in q]
    return b * (max_q / b + math.log(sum(math.exp(si / b) for si in shifted)))


def lmsr_price(q, b, i=0):
    """
    LMSR 即时价格函数 / Softmax（文档公式3）

    pi(q) = ∂C/∂qi = e^(qi/b) / Σe^(qj/b)

    性质: Σpi = 1, pi ∈ (0,1) ∀i
    等价于神经网络 softmax — 市场即信念定价网络。

    Args:
        q: 未平仓量向量
        b: 流动性参数
        i: 要计算价格的结果索引（0=YES/UP, 1=NO/DOWN）
    """
    if b <= 0:
        return 0.5
    # 数值稳定 softmax
    max_q = max(q)
    shifted = [qi - max_q for qi in q]
    exp_vals = [math.exp(si / b) for si in shifted]
    total = sum(exp_vals)
    return exp_vals[i] / total if total > 0 else 1.0 / len(q)


def lmsr_trade_cost(q, b, i, delta):
    """
    交易成本（文档公式4）

    Cost = C(q1,...,qi+δ,...,qn) - C(q1,...,qi,...,qn)

    买入 delta 份结果 i 的成本。

    Args:
        q: 当前未平仓量向量
        b: 流动性参数
        i: 买入的结果索引
        delta: 买入份数
    """
    q_after = list(q)
    q_after[i] += delta
    return lmsr_cost(q_after, b) - lmsr_cost(q, b)


def infer_quantities_from_prices(p_up, b):
    """
    从观测价格反推未平仓量（二元市场）

    由 softmax: p_up = e^(q_up/b) / (e^(q_up/b) + e^(q_down/b))
    设 q_down = 0，则: q_up = b · ln(p_up / (1 - p_up))

    Args:
        p_up: UP 结果的观测价格 (0,1)
        b: 流动性参数
    Returns:
        [q_up, q_down]
    """
    p_up = max(0.01, min(0.99, p_up))
    q_up = b * math.log(p_up / (1 - p_up))
    q_down = 0.0
    return [q_up, q_down]


def estimate_b_from_orderbook(spread, total_depth):
    """
    从订单簿特征估算 LMSR b 参数

    在 p≈0.5 附近: spread ≈ 1/(2b) → b ≈ 1/(2·spread)
    同时考虑深度: 深度越大说明流动性越好，b 应更大。

    Args:
        spread: bid-ask 价差
        total_depth: 总订单深度（bid+ask前10档）
    Returns:
        估算的 b 参数
    """
    if spread <= 0:
        spread = 0.01

    # 从价差估算
    b_spread = 1.0 / (2.0 * spread)

    # 从深度估算: 经验公式，depth=200对应b≈100
    b_depth = total_depth / 2.0

    # 加权融合（价差权重更高）
    b = b_spread * 0.6 + b_depth * 0.4
    return max(1.0, b)


def lmsr_fair_price(token_id, timeout=3):
    """
    计算 LMSR 理论公允价格 & 与 CLOB 实际价格的无效率缺口

    流程:
      1. 从 CLOB 订单簿获取 bid/ask/depth
      2. 估算 b 参数
      3. 用 CLOB midpoint 作为观测价格，反推 q 向量
      4. 计算 LMSR 理论价格（应与观测价格一致，差异来自滑点/不对称）
      5. 计算买入 5/10 份的理论成本 vs 实际成本

    Returns:
        {
            "lmsr_b": float,              # 估算的 b 参数
            "lmsr_price_up": float,       # LMSR UP 理论价格
            "lmsr_price_down": float,     # LMSR DOWN 理论价格
            "clob_midpoint": float,       # CLOB 中间价
            "clob_best_ask": float,       # CLOB 最佳卖价
            "inefficiency": float,        # 无效率 = LMSR理论价 - CLOB best_ask
            "theoretical_cost_5": float,  # 买5份的LMSR理论成本
            "theoretical_cost_10": float, # 买10份的LMSR理论成本
            "max_market_maker_loss": float, # 最大做市商损失
        } or None
    """
    try:
        book = clob_client.get_orderbook(token_id)
        bids, asks = extract_orderbook_levels(book)
        if not bids or not asks:
            return None

        if not bids or not asks:
            return None

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        spread = best_ask - best_bid
        midpoint = (best_bid + best_ask) / 2.0

        # 订单簿深度
        bid_depth = sum(float(b["size"]) for b in bids[:10])
        ask_depth = sum(float(a["size"]) for a in asks[:10])
        total_depth = bid_depth + ask_depth

        # Step 1: 估算 b 参数
        b = estimate_b_from_orderbook(spread, total_depth)

        # Step 2: 从 midpoint 反推未平仓量
        q = infer_quantities_from_prices(midpoint, b)

        # Step 3: 计算 LMSR 理论价格
        price_up = lmsr_price(q, b, 0)
        price_down = lmsr_price(q, b, 1)

        # Step 4: 无效率 = LMSR 理论价 - CLOB best_ask
        # 正值 = 市场低估（便宜），负值 = 市场高估（贵）
        inefficiency = price_up - best_ask

        # Step 5: 理论交易成本
        cost_5 = lmsr_trade_cost(q, b, 0, 5)
        cost_10 = lmsr_trade_cost(q, b, 0, 10)

        # 最大做市商损失: L_max = b·ln(n), n=2 for binary
        max_mm_loss = b * math.log(2)

        print(f"  📊 LMSR定价: b={b:.1f} | fair_up={price_up:.4f} fair_down={price_down:.4f} | ask={best_ask:.4f} | 无效率={inefficiency:+.4f} | 理论成本5份=${cost_5:.4f}")

        return {
            "lmsr_b": round(b, 2),
            "lmsr_price_up": round(price_up, 4),
            "lmsr_price_down": round(price_down, 4),
            "clob_midpoint": round(midpoint, 4),
            "clob_best_ask": round(best_ask, 4),
            "inefficiency": round(inefficiency, 4),
            "theoretical_cost_5": round(cost_5, 4),
            "theoretical_cost_10": round(cost_10, 4),
            "max_market_maker_loss": round(max_mm_loss, 2),
        }

    except Exception:
        return None


def estimate_lmsr_b(token_id, timeout=3):
    """
    从 CLOB 订单簿评估流动性（不再反推 LMSR b 参数）

    Returns:
        {
            "b": float,              # 兼容旧接口，实际=depth_score*100
            "liquidity_score": float, # 0-1 流动性评分
            "best_bid": float,
            "best_ask": float,
            "spread": float,          # 买卖价差
            "bid_depth": float,       # 买单深度（总量）
            "ask_depth": float,       # 卖单深度（总量）
            "slippage_5": float,      # 买5份的预估滑点
            "slippage_10": float,     # 买10份的预估滑点
        }
    """
    try:
        book = clob_client.get_orderbook(token_id)
        if not book:
            print(f"  📡 SDK订单簿请求失败: token={str(token_id)[:16]}...")
            return _default_result()

        bids, asks = extract_orderbook_levels(book)

        # 调试日志: 打印规范化后的订单簿数据
        bid_summary = [(float(b["price"]), float(b["size"])) for b in bids[:5]]
        ask_summary = [(float(a["price"]), float(a["size"])) for a in asks[:5]]
        print(f"  📡 CLOB订单簿(规范化) token={token_id[:16]}...")
        print(f"     Bids(前5,降序): {bid_summary}")
        print(f"     Asks(前5,升序): {ask_summary}")
        print(f"     总档位: bids={len(bids)} asks={len(asks)}")

        if not bids or not asks:
            return _default_result()

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        spread = best_ask - best_bid

        # 计算订单簿深度（前10档）
        bid_depth = sum(float(b["size"]) for b in bids[:10])
        ask_depth = sum(float(a["size"]) for a in asks[:10])

        # 计算不同仓位的滑点
        slippage_5 = _estimate_slippage(asks, 5)
        slippage_10 = _estimate_slippage(asks, 10)

        # 流动性评分 (0-1)
        score = _calc_liquidity_score(spread, bid_depth, ask_depth, slippage_5)

        # b 字段保留兼容性，= score * 100
        b_compat = round(score * 100, 2)

        return {
            "b": b_compat,
            "liquidity_score": round(score, 3),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread, 4),
            "bid_depth": round(bid_depth, 1),
            "ask_depth": round(ask_depth, 1),
            "slippage_5": round(slippage_5, 4),
            "slippage_10": round(slippage_10, 4),
        }

    except Exception:
        return _default_result()


def _estimate_slippage(asks, size):
    """
    估算买入 size 份的实际滑点

    遍历卖单簿，计算加权平均价格 vs best_ask 的差值
    """
    if not asks:
        return 0.05  # 无卖单，默认5%滑点

    best_ask = float(asks[0]["price"])
    remaining = size
    total_cost = 0.0

    for ask in asks:
        ask_price = float(ask["price"])
        ask_size = float(ask["size"])

        fill = min(remaining, ask_size)
        total_cost += fill * ask_price
        remaining -= fill

        if remaining <= 0:
            break

    if remaining > 0:
        # 订单簿深度不足，剩余部分假设 +10% 滑点
        last_price = float(asks[-1]["price"]) if asks else best_ask
        total_cost += remaining * last_price * 1.10

    avg_price = total_cost / size if size > 0 else best_ask
    slippage = (avg_price - best_ask) / best_ask if best_ask > 0 else 0

    return max(0, slippage)


def _calc_liquidity_score(spread, bid_depth, ask_depth, slippage_5):
    """
    综合流动性评分 (0-1)

    权重:
      - spread (35%): 越小越好
      - depth (35%): 越大越好
      - slippage (30%): 越小越好
    """
    # spread 评分: 0.01 以下满分，0.10 以上零分
    if spread <= 0.01:
        spread_score = 1.0
    elif spread >= 0.10:
        spread_score = 0.0
    else:
        spread_score = 1.0 - (spread - 0.01) / 0.09

    # depth 评分: 200+ 满分，20 以下零分
    total_depth = bid_depth + ask_depth
    if total_depth >= 200:
        depth_score = 1.0
    elif total_depth <= 20:
        depth_score = 0.0
    else:
        depth_score = (total_depth - 20) / 180.0

    # slippage 评分: 0 满分，5%+ 零分
    if slippage_5 <= 0:
        slip_score = 1.0
    elif slippage_5 >= 0.05:
        slip_score = 0.0
    else:
        slip_score = 1.0 - slippage_5 / 0.05

    return spread_score * 0.35 + depth_score * 0.35 + slip_score * 0.30


def get_dynamic_discount_threshold(liquidity_info):
    """
    根据流动性评估动态调整折价阈值

    Args:
        liquidity_info: estimate_lmsr_b() 的返回值

    Returns:
        discount_threshold: 动态折价阈值
    """
    score = liquidity_info.get("liquidity_score", 0.5)
    slippage = liquidity_info.get("slippage_5", 0.02)

    # 基础阈值: 流动性越好，阈值越低
    if score >= 0.8:
        base_threshold = 0.08
    elif score >= 0.6:
        base_threshold = 0.10
    elif score >= 0.4:
        base_threshold = 0.13
    elif score >= 0.2:
        base_threshold = 0.16
    else:
        base_threshold = 0.20

    # 滑点补偿: 折价必须覆盖买入和卖出的双向滑点
    slippage_compensation = slippage * 2.5

    return round(base_threshold + slippage_compensation, 3)


def _default_result():
    """订单簿获取失败时的默认值"""
    return {
        "b": 10.0,
        "liquidity_score": 0.3,
        "best_bid": None,
        "best_ask": None,
        "spread": 0.05,
        "bid_depth": 0,
        "ask_depth": 0,
        "slippage_5": 0.03,
        "slippage_10": 0.06,
    }
