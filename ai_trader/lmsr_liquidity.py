"""
流动性评估模块 v2.0（纯订单簿）

v1.0 的 b ≈ 1/(4*spread) 反推公式对 CLOB 无效（CLOB 不是 LMSR），
改为直接使用订单簿经验数据：spread、depth、实际滑点。

用途: 替代 ai_analyze_v2.py 中粗糙的 odds_spread 流动性判断
"""
import requests


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
        resp = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=timeout
        )
        if resp.status_code != 200:
            return _default_result()

        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

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

    avg_price = total_cost / size
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
