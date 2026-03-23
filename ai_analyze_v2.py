#!/usr/bin/env python3
"""
AI 分析和下注决策 v2
- 使用 ai_model_v2 的新策略
- 用期望值（EV）决定是否下注，而不是固定阈值
"""
import sys
import os
import json
import math
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


from ai_trader.ai_model_v2 import analyze_market
from ai_trader.base_rate import get_base_rate
from ai_trader import clob_client
from py_clob_client.order_builder.constants import BUY

_bet_executor = ThreadPoolExecutor(max_workers=3)


def _random_walk_p_win(gap, atr_val, remaining_seconds):
    """
    Random walk model: P(price stays on current side of PTB at expiry).

    Uses Φ(|gap| / σ_total) where σ_total = (ATR/1.5) × √(remaining_min).
    More accurate than static ATR-band lookup because it accounts for
    actual gap magnitude AND time remaining.
    """
    if atr_val <= 0 or remaining_seconds <= 0:
        return 0.50

    sigma_per_min = atr_val / 1.5          # ATR ≈ 1.5σ (normal approx)
    sigma_total = sigma_per_min * math.sqrt(remaining_seconds / 60)

    if sigma_total <= 0:
        return 0.50

    z = abs(gap) / sigma_total
    p_win = 0.5 * (1 + math.erf(z / math.sqrt(2)))   # Φ(z)

    return p_win                             # cap 由 P_WIN_CAP env 统一控制


def log_decision(slug, coin, ptb, direction, confidence, up_odds, down_odds, details, action="SKIP"):
    """记录决策到统计文件"""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "coin": coin,
        "ptb": ptb,
        "direction": direction,
        "confidence": confidence,
        "up_odds": up_odds,
        "down_odds": down_odds,
        "ev": details.get("expected_value", 0),
        "discount": details.get("discount", 0),
        "discount_pct": details.get("discount_pct", 0),
        "estimated_value": details.get("estimated_value", 0),
        "price_diff_pct": details.get("price_diff_pct", 0),
        "diff_in_atr": details.get("diff_in_atr", 0),
        "current_price": details.get("current_price", 0),
        "total_score": details.get("total_score", 0),
        "action": action,
    }

    with open("logs/decisions_v2.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


PENDING_ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "pending_orders.jsonl")

def _append_pending_order(entry):
    try:
        os.makedirs(os.path.dirname(PENDING_ORDERS_FILE), exist_ok=True)
        with open(PENDING_ORDERS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def analyze_and_decide(coin, price_to_beat, up_odds, down_odds, slug, extra_info=None):
    """
    执行 AI 分析并返回决策

    返回: (should_bet, direction, confidence, details)
    """
    direction, confidence, details = analyze_market(
        coin, price_to_beat, up_odds, down_odds
    )

    if not direction:
        return False, None, 0, details

    # Bug 7 fix: 方向确定后选择正确的 token_id 做 LMSR 评估
    if extra_info and direction:
        if direction == "UP" and extra_info.get("up_token"):
            extra_info["token_id"] = extra_info["up_token"]
        elif direction == "DOWN" and extra_info.get("down_token"):
            extra_info["token_id"] = extra_info["down_token"]

    # ── 下注条件（v2.1 折价套利策略） ──
    # 核心：折价空间足够大，提前平仓能锁利
    # 1. 折价 >= 12%（保守阈值，回测验证27.3%下注率）
    # 2. EV > 0.05（正期望）
    # 3. 赔率 < 0.85（不买太贵的token）
    # 4. 动量确认（置信度≥50%表示动量方向一致）
    target_odds = details.get("target_odds", up_odds if direction == "UP" else down_odds)
    discount = details.get("discount", 0)

    # ── Base Rate 校准 (P0) ──
    diff_in_atr = details.get("diff_in_atr", 0)
    base_rate = get_base_rate(diff_in_atr)
    details["base_rate"] = round(base_rate, 4)

    # Kelly 缩减：base_rate < 0.55 表示无统计优势，仓位减半
    kelly_reduction = 0.5 if base_rate < 0.55 else 1.0
    details["kelly_reduction"] = kelly_reduction

    # ── LMSR 流动性评估 ──
    liquidity_info = None
    token_id = extra_info.get("token_id") if extra_info else None
    if token_id:
        try:
            from ai_trader.lmsr_liquidity import estimate_lmsr_b, get_dynamic_discount_threshold
            liquidity_info = estimate_lmsr_b(token_id)
            discount_threshold = get_dynamic_discount_threshold(liquidity_info)
            details["lmsr_b"] = liquidity_info["b"]
            details["liquidity_score"] = liquidity_info["liquidity_score"]
            details["spread"] = liquidity_info["spread"]
            details["slippage_5"] = liquidity_info["slippage_5"]
        except Exception:
            liquidity_info = None

    if not liquidity_info:
        odds_spread = abs(up_odds - down_odds)
        is_liquid = odds_spread < 0.15
        discount_threshold = 0.10 if is_liquid else 0.15

    # 预热gap趋势覆盖（只能提高阈值，不能降低）
    if extra_info and "min_discount" in extra_info:
        discount_threshold = max(discount_threshold, extra_info["min_discount"])

    # ── 严格概率 EV 公式 (P2) ──
    # p_win = random walk model（用 gap + ATR + 剩余时间计算），比 base_rate 更准确
    remaining_sec = extra_info.get("remaining_seconds", 200) if extra_info else 200
    price_diff = details.get("price_diff", 0)
    atr_val = details.get("atr", 0)
    rw_p_win = _random_walk_p_win(price_diff, atr_val, remaining_sec)
    p_win = max(rw_p_win, base_rate)       # 取 random walk 和 base_rate 中较高者
    details["rw_p_win"] = round(rw_p_win, 4)

    # 贝叶斯后验融合
    bayesian_info = extra_info.get("bayesian") if extra_info else None
    if bayesian_info:
        b_p_hat = bayesian_info.get("p_hat", 0.5)
        bayesian_conf = bayesian_info.get("confidence", 0)
        bayesian_dir = bayesian_info.get("direction", direction)

        # 早期窗口融合门槛与入场门槛对齐(0.15)，避免死区
        is_early_window = extra_info.get("early_window", False) if extra_info else False
        fusion_threshold = 0.15 if is_early_window else 0.3
        if bayesian_dir == direction and bayesian_conf > fusion_threshold:
            # 贝叶斯融合：只能提升 p_win，不能降低（random walk 已是合理下界）
            fused_p = p_win * 0.4 + b_p_hat * 0.6
            p_win = max(p_win, fused_p)
            confidence = confidence * 0.4 + bayesian_conf * 0.6
            details["confidence_source"] = "bayesian_fused"
        elif bayesian_dir != direction:
            confidence = confidence * 0.5
            details["confidence_source"] = "bayesian_conflict"
        else:
            details["confidence_source"] = "discount_only"

        details["bayesian_p_hat"] = round(b_p_hat, 4)
        details["bayesian_confidence"] = round(bayesian_conf, 4)
        details["bayesian_direction"] = bayesian_dir

    P_WIN_CAP = float(os.environ.get("P_WIN_CAP", "0.92"))
    p_win = min(p_win, P_WIN_CAP)  # P1: 防止贝叶斯累积推高 p_win (env可配置)

    # 交叉验证：estimated_value 比 p_win 高 0.15+ → 高估警告
    estimated_value = details.get("estimated_value", 0.5)
    if estimated_value > p_win + 0.15:
        details["overestimated"] = True
        confidence *= 0.85

    # #7: 无效率信号 — LMSR理论价 vs CLOB实际价 偏离
    lmsr_info = None
    if token_id:
        try:
            from ai_trader.lmsr_liquidity import lmsr_fair_price
            lmsr_info = lmsr_fair_price(token_id)
        except Exception:
            pass

    if lmsr_info:
        lmsr_ineff = lmsr_info["inefficiency"]
        details["lmsr_fair_price"] = lmsr_info["lmsr_price_up"]
        details["lmsr_inefficiency"] = lmsr_ineff
        details["lmsr_b_estimated"] = lmsr_info["lmsr_b"]
        details["lmsr_theoretical_cost_5"] = lmsr_info["theoretical_cost_5"]

        # LMSR无效率: 理论价 > CLOB价 → 市场低估，是买入机会
        if lmsr_ineff > 0.05:
            old_threshold = discount_threshold
            discount_threshold = max(discount_threshold - 0.02, 0.06)
            details["lmsr_inefficiency_boost"] = True
            print(f"  📊 LMSR无效率: fair={lmsr_info['lmsr_price_up']:.3f} vs ask={lmsr_info['clob_best_ask']:.3f} gap={lmsr_ineff:.3f} → 阈值{old_threshold:.3f}→{discount_threshold:.3f}")

    # 原有 p_win vs best_ask 无效率检测（保留兼容）
    if liquidity_info and liquidity_info.get("best_ask"):
        realtime_ask = liquidity_info["best_ask"]
        inefficiency = p_win - realtime_ask
        details["inefficiency"] = round(inefficiency, 4)
        details["realtime_ask"] = realtime_ask
        # 强无效率: 市场明显错价，降低入场门槛
        if inefficiency > 0.10:
            old_threshold = discount_threshold
            discount_threshold = max(discount_threshold - 0.02, 0.06)
            details["inefficiency_boost"] = True
            print(f"  📊 #7无效率信号: p_win={p_win:.3f} vs ask={realtime_ask:.3f} 偏离={inefficiency:.3f} → 阈值{old_threshold:.3f}→{discount_threshold:.3f}")

    # ── C1 执行价校准: 用 CLOB best_ask 代替 Gamma 赔率做决策 ──
    # Gamma 赔率 ≈ LMSR 概率（仅用于方向判断）
    # CLOB best_ask = 实际买入价（必须用于 EV/折价/价格检查）
    # 不校准 → 决策用0.50算EV=+0.30，实际买入价0.85，真实EV=-0.03，每单亏钱
    if liquidity_info and liquidity_info.get("best_ask"):
        exec_price = liquidity_info["best_ask"]
        # 空簿检测：best_ask >= 0.95 说明CLOB无真实卖单，用 last-trade-price 替代
        if exec_price >= 0.95 and token_id:
            details["clob_empty_book"] = True
            details["clob_raw_ask"] = round(exec_price, 4)
            last_price = clob_client.get_last_trade_price(token_id)
            if last_price and 0.01 < last_price < 0.99:
                exec_price = last_price
                liquidity_info["best_ask"] = last_price
                print(f"  📡 C1校准：CLOB空簿，使用last-trade-price=${last_price:.3f}替代best_ask")
        if 0.01 < exec_price < 0.99:
            details["gamma_odds"] = round(target_odds, 4)
            details["exec_price"] = round(exec_price, 4)
            target_odds = exec_price
            discount = estimated_value - exec_price
            details["exec_discount"] = round(discount, 4)

    # ── EV 计算：按提前平仓概率折算 spread 成本 ──
    # 持有到期结算(0或1)不付spread，只有提前平仓才付bid-ask差价
    EARLY_EXIT_RATIO = float(os.environ.get("EARLY_EXIT_RATIO", "0.3"))  # 提前平仓概率
    raw_spread = liquidity_info["spread"] if liquidity_info and liquidity_info.get("spread") else 0.02
    spread_cost = raw_spread * EARLY_EXIT_RATIO
    ev_gross = p_win - target_odds
    ev = ev_gross - spread_cost  # 按提前退出概率折算后的净EV
    details["expected_value"] = round(ev, 4)
    details["ev_gross"] = round(ev_gross, 4)
    details["spread_cost"] = round(spread_cost, 4)
    details["p_win_final"] = round(p_win, 4)
    details["ev_positive"] = ev > 0

    # ── ATR 最低偏离门槛：过滤噪音信号 ──
    MIN_ATR_DEVIATION = float(os.environ.get("MIN_ATR_DEVIATION", "1.5"))

    MAX_PRICE = float(os.environ.get("MAX_BUY_PRICE", "0.92"))
    MIN_EV = float(os.environ.get("MIN_EV", "0.05"))
    MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.60"))

    # 早期窗口放宽门槛：优势是CLOB价格好，不需要那么高的置信度/EV
    is_early = extra_info.get("early_window", False) if extra_info else False
    if is_early:
        MIN_EV = float(os.environ.get("EARLY_MIN_EV", "0.03"))
        MIN_CONFIDENCE = float(os.environ.get("EARLY_MIN_CONFIDENCE", "0.40"))

    # ── 5分钟趋势过滤（与市场周期同频） ──
    # 弱信号(1.5-2.0ATR) + 5m反向趋势 → 直接跳过
    # 强信号(>2.5ATR) + 5m反向趋势 → 置信度降低但仍可下注
    trend_5m_align = details.get("trend_15m_alignment", "neutral")
    if trend_5m_align == "conflicting":
        if diff_in_atr < 2.0:
            # 弱信号 + 5m逆势 → 大概率是1m假突破
            details["trend_15m_filter"] = "blocked"
            print(f"  🚫 5m趋势过滤: 1m信号弱({diff_in_atr:.1f}ATR<2.0) + 5m反向 → 跳过")
        elif diff_in_atr < 2.5:
            confidence *= 0.7  # 中等信号 → 降30%置信度
            details["trend_15m_filter"] = "reduced_30"
        else:
            confidence *= 0.85  # 强信号 → 降15%置信度
            details["trend_15m_filter"] = "reduced_15"
    elif trend_5m_align == "confirming":
        details["trend_15m_filter"] = "boosted"

    # ── 等比EV门槛：昂贵token的EV天花板天然更低，按利润空间等比缩放 ──
    # price=0.50 时门槛 = MIN_EV（不变）; price=0.85 时门槛 = MIN_EV × 0.30
    potential_profit = max(1 - target_odds, 0.05)   # 每份合约最大利润，下限5%
    ev_threshold = MIN_EV * potential_profit / 0.50  # 以 price=0.50 为基准等比缩放
    ev_threshold = max(ev_threshold, 0.01)           # 绝对下限：防止极端情况
    details["ev_threshold"] = round(ev_threshold, 4)

    should_bet = (
        diff_in_atr >= MIN_ATR_DEVIATION  # ATR偏离：过滤无信号区间
        and ev > ev_threshold              # 等比EV：按token价格自适应门槛
        and target_odds < MAX_PRICE        # 不买太贵（env可配置）
        and confidence >= MIN_CONFIDENCE   # 置信度（env可配置）
        and details.get("trend_15m_filter") != "blocked"  # 15m趋势过滤
    )

    details["should_bet"] = should_bet
    liq_label = f"LMSR:{liquidity_info['liquidity_score']:.2f}" if liquidity_info else "fallback"
    details["liquidity"] = liq_label
    details["discount_threshold"] = discount_threshold
    early_label = " [早期窗口]" if is_early else ""
    trend_label = f" 5m:{trend_5m_align}" if trend_5m_align != "neutral" else ""
    filter_label = f" [{details.get('trend_15m_filter', '')}]" if details.get("trend_15m_filter") else ""
    details["bet_reason"] = (
        f"atr={diff_in_atr:.2f}({'✅' if diff_in_atr>=MIN_ATR_DEVIATION else '❌'}≥{MIN_ATR_DEVIATION}) "
        f"ev={ev:+.4f}({'✅' if ev>ev_threshold else '❌'}>{ev_threshold:.3f}[adapt],扣spread{spread_cost:.3f}) "
        f"p_win={p_win:.3f} rw={rw_p_win:.3f} base={base_rate:.3f} "
        f"odds={target_odds:.3f}({'✅' if target_odds<MAX_PRICE else '❌'}<{MAX_PRICE}) "
        f"conf={confidence:.0%}({'✅' if confidence>=MIN_CONFIDENCE else '❌'}≥{MIN_CONFIDENCE:.0%}) "
        f"流动性:{liq_label}{early_label}{trend_label}{filter_label}"
    )

    # 空簿二次机会：C1校准拉负了折价，但Gamma指标本身OK → 放行，用校准价下单
    if not should_bet and details.get("clob_empty_book"):
        gamma_odds = details.get("gamma_odds", target_odds)
        gamma_discount = estimated_value - gamma_odds
        gamma_ev = p_win - gamma_odds
        if gamma_discount >= discount_threshold and gamma_ev > 0.05 and confidence >= 0.75:
            should_bet = True
            details["should_bet"] = True
            details["empty_book_override"] = True
            details["bet_reason"] = (
                f"空簿放行: Gamma折价={gamma_discount:.3f}(✅≥{discount_threshold:.3f}) "
                f"Gamma_EV={gamma_ev:+.4f}(✅>0.05) conf={confidence:.0%}(✅≥75%) "
                f"执行价=${details.get('exec_price', 0):.3f}(校准) 流动性:{liq_label}"
            )
            print(f"  📡 空簿二次机会放行: Gamma折价={gamma_discount:.3f} Gamma_EV={gamma_ev:+.4f}")

    action = "BET" if should_bet else "SKIP"
    log_decision(slug, coin, price_to_beat, direction, confidence, up_odds, down_odds, details, action)

    return should_bet, direction, confidence, details


def check_bid_depth(token_id):
    """P2: 检查token的买方深度（SDK直连）"""
    try:
        book = clob_client.get_orderbook(token_id)
        if book and book.bids:
            return sum(float(b.size) for b in book.bids)
    except:
        pass
    return None


def calculate_kelly_size(confidence, ev, balance, target_price=None, p_hat=None,
                          p_win=None, kelly_reduction=1.0, exit_bid_depth=None):
    """
    修正的 1/4 Kelly 仓位计算（5分钟市场专用）

    正确公式（二元市场）:
        f* = (p - price) / (1 - price)

    论文注释: "NEVER full Kelly on 5min markets!" → 1/4 Kelly

    仓位跟余额挂钩：Kelly 比例 × 余额 / 价格 → 份数，
    通过 MIN_BET_SIZE / MAX_BET_SIZE 环境变量控制上下限。

    Args:
        confidence: 置信度（用于 fallback）
        ev: 期望值
        balance: 当前余额
        target_price: 买入价格（市场赔率）
        p_hat: 贝叶斯后验概率
        p_win: 严格概率估计（来自 base_rate + 贝叶斯融合，优先级最高）
        kelly_reduction: 缩减因子（base_rate < 0.55 → 0.5, 相关性 → 0.5）
    """
    MIN_BET = int(os.environ.get("MIN_BET_SIZE", "5"))
    MAX_BET = int(os.environ.get("MAX_BET_SIZE", "10"))
    P_CAP = float(os.environ.get("P_WIN_CAP", "0.92"))

    if ev <= 0:
        logger.info(f"  📊 Kelly仓位: EV={ev:.3f}≤0 → 跳过")
        return 0

    # 胜率估计优先级: p_win > p_hat > confidence映射
    if p_win and p_win > 0.5:
        p = min(p_win, P_CAP)
    elif p_hat and p_hat > 0.5:
        p = min(p_hat, P_CAP)
    else:
        p = 0.5 + (confidence * 0.3)
        p = max(0.5, min(P_CAP, p))

    # 买入价格
    price = target_price if target_price and 0.01 < target_price < 0.99 else 0.50

    # 二元市场 Kelly: f* = (p - price) / (1 - price)
    kelly_full = (p - price) / (1 - price) if price < 1.0 else 0

    if kelly_full <= 0:
        logger.info(f"  📊 Kelly仓位: p={p:.3f} price={price:.3f} f*={kelly_full:.3f}≤0 → 跳过")
        return 0

    # 1/4 Kelly + 缩减因子
    kelly_quarter = kelly_full * 0.25 * kelly_reduction
    kelly_quarter = max(0, min(0.25, kelly_quarter))

    # Kelly 比例换算为份数：dollar_amount / price
    dollar_amount = balance * kelly_quarter
    size = int(dollar_amount / price) if price > 0 else 0

    # 余额约束：单笔不超过余额 20%（安全网）
    max_by_balance = int(balance * 0.20 / price) if price > 0 else 0
    size = min(size, max_by_balance)

    # P3: 流动性上限 — 不超过退出流动性的50%
    if exit_bid_depth and exit_bid_depth > 0:
        max_by_liquidity = int(exit_bid_depth * 0.5)
        if size > max_by_liquidity:
            logger.info(f"  📊 P3流动性上限: bid_depth={exit_bid_depth:.1f} → max={max_by_liquidity}份")
            size = min(size, max_by_liquidity)

    if size < MIN_BET:
        logger.info(
            f"  📊 Kelly仓位: 计算仓位{size}份 < 最小可执行仓位{MIN_BET}份，跳过"
        )
        return 0

    # ENV 上限
    size = min(MAX_BET, size)

    red_label = f" red={kelly_reduction}" if kelly_reduction < 1.0 else ""
    liq_label = f" liq_cap={exit_bid_depth:.0f}" if exit_bid_depth else ""
    logger.info(f"  📊 Kelly仓位: p={p:.3f} price={price:.3f} f*={kelly_full:.3f} f/4={kelly_quarter:.3f} ${dollar_amount:.1f}{red_label}{liq_label} → {size}份")

    return max(0, size)


def execute_bet(slug, direction, token_id, confidence=0.65, ev=0, amount=None,
                 p_hat=None, entry_details=None, kelly_reduction=1.0, pre_balance=None):
    """执行下注（通过 Polymarket CLI）

    Args:
        slug: 市场 slug
        direction: UP 或 DOWN
        token_id: 代币 ID
        confidence: AI置信度
        ev: 期望值
        amount: 下注金额（美元），None则自动计算
        p_hat: 贝叶斯后验概率
        entry_details: 完整分析详情（用于持仓记录丰富字段）
        kelly_reduction: Kelly 缩减因子（base_rate/相关性）
        pre_balance: 预取的余额（调用方提前获取，省~250ms）
    """
    # ── 余额 + 订单簿 ──
    # 余额：优先用调用方预取的结果，省一次HTTP
    if pre_balance is not None:
        balance = pre_balance
        # 只需获取 orderbook（复用1s内缓存，分析阶段刚拉过）
        book = clob_client.get_orderbook(token_id, max_age=1)
    else:
        # fallback：并行获取余额+订单簿
        fut_balance = _bet_executor.submit(clob_client.get_balance)
        fut_book = _bet_executor.submit(clob_client.get_orderbook, token_id, 1)  # 1s内复用缓存
        balance = fut_balance.result(timeout=5)
        book = fut_book.result(timeout=5)

    # 余额不足时直接跳过，不记录为失败
    MIN_BALANCE = float(os.environ.get("MIN_BALANCE", "5.0"))
    if balance < MIN_BALANCE:
        print(f"  ⚠️ 余额不足: ${balance:.2f} < ${MIN_BALANCE:.2f}，跳过下注")
        return False, 0, 0, "SKIP_NO_BALANCE"

    # 从已获取的orderbook提取best_ask和bid_depth（0次额外HTTP）
    # P0优化: 优先用 Polymarket WebSocket 实时 best_ask（0ms延迟）
    SLIPPAGE = float(os.environ.get("SLIPPAGE", "0.01"))
    price = None
    price_source = "unknown"
    bid_depth = None

    sorted_asks_full = []  # 保留完整 asks 用于 FOK 深度感知滑点

    # 优先尝试 WebSocket 实时 best_ask
    try:
        from ai_trader.polymarket_ws import poly_ws
        ws_bid, ws_ask = poly_ws.get_best_bid_ask(token_id)
        if ws_ask is not None and 0.01 < ws_ask < 0.99:
            # 倒挂校验
            if ws_bid is not None and ws_bid >= ws_ask:
                print(f"  ⚠️ WS订单簿倒挂: bid=${ws_bid:.2f} >= ask=${ws_ask:.2f}，走REST回退")
            else:
                price = ws_ask
                price_source = "ws_best_ask"
                print(f"  📡 WS best_ask: ${ws_ask:.2f} (实时推送)")
        # WS orderbook 用于 FOK 深度感知
        ws_bids, ws_asks = poly_ws.get_book(token_id)
        if ws_asks:
            sorted_asks_full = ws_asks  # 已排序
        if ws_bids:
            bid_depth = sum(float(b.get("size", 0)) for b in ws_bids)
    except Exception:
        pass

    # REST fallback: WS 未命中时用 SDK orderbook
    if book and (price is None or not sorted_asks_full):
        # best_ask + 倒挂校验
        if book.asks:
            from ai_trader.polymarket_api import normalize_orderbook
            raw_bids_chk = [{"price": b.price, "size": b.size} for b in (book.bids or [])]
            raw_asks_chk = [{"price": a.price, "size": a.size} for a in book.asks]
            sorted_bids_chk, sorted_asks_rest = normalize_orderbook(raw_bids_chk, raw_asks_chk)
            # 倒挂校验：best_bid >= best_ask 说明数据异常
            if sorted_bids_chk and sorted_asks_rest:
                _chk_bid = float(sorted_bids_chk[0]["price"])
                _chk_ask = float(sorted_asks_rest[0]["price"])
                if _chk_bid >= _chk_ask:
                    print(f"  ⚠️ 订单簿倒挂: bid=${_chk_bid:.2f} >= ask=${_chk_ask:.2f}，跳过best_ask")
                    sorted_asks_rest = []  # 清空，走 fallback
            if not sorted_asks_full:
                sorted_asks_full = sorted_asks_rest
            if price is None and sorted_asks_rest:
                best_ask = float(sorted_asks_rest[0]["price"])
                if 0.01 < best_ask < 0.99:
                    price = best_ask
                    price_source = "best_ask"
                    print(f"  📡 REST best_ask: ${best_ask:.2f} → 限价${price:.2f}")
        # bid_depth（从同一个book提取，不再额外HTTP）
        if bid_depth is None and book.bids:
            bid_depth = sum(float(b.size) for b in book.bids)

    # 2. last-trade-price + 滑点 回退
    if price is None:
        last_price = clob_client.get_last_trade_price(token_id)
        if last_price and 0.01 < last_price < 0.99:
            price = min(round(last_price + SLIPPAGE, 2), 0.99)
            price_source = "last_trade"
            print(f"  📡 last-trade回退: ${last_price:.2f} + 滑点${SLIPPAGE} → 限价${price:.2f}")

    # 3. midpoint + 滑点 回退
    if price is None:
        mid = clob_client.get_midpoint(token_id)
        if mid and 0.01 < mid < 0.99:
            price = min(round(mid + SLIPPAGE, 2), 0.99)
            price_source = "midpoint"
            print(f"  📡 midpoint回退: ${mid:.2f} + 滑点${SLIPPAGE} → 限价${price:.2f}")

    # 安全检查：全部失败
    if price is None:
        print(f"  ⚠️ 无法获取真实价格（last-trade+midpoint均失败），跳过下注")
        return False, 0, 0, "SKIP_NO_PRICE"

    # 价格四舍五入到2位小数（Polymarket要求）
    price = round(price, 2)

    # 安全检查：实际买入价不能超过上限（env可配置，默认0.92）
    MAX_PRICE = float(os.environ.get("MAX_BUY_PRICE", "0.92"))
    if price >= MAX_PRICE:
        print(f"  ⚠️ 实际买入价${price:.2f}≥${MAX_PRICE}，上行空间不足，跳过下注")
        return False, 0, 0, "SKIP_PRICE_TOO_HIGH"

    # 限价上限保护：买入价不应超过 p_win（理论公允价），防止出价过高
    p_win_cap = entry_details.get("p_win_final") if entry_details else None
    if p_win_cap and p_win_cap > 0.10 and price > p_win_cap:
        capped_price = round(p_win_cap, 2)
        print(f"  🛡️ 限价保护: ${price:.2f} > p_win=${p_win_cap:.3f}，降至${capped_price:.2f}")
        price = capped_price

    # P2: 退出流动性检查（已从上面的book提取，无额外HTTP）
    if bid_depth is not None:
        print(f"  📊 P2退出流动性: bid_depth={bid_depth:.1f}份")
        if bid_depth < 5:
            print(f"  ⚠️ 退出流动性极低({bid_depth:.1f}<5)，跳过下注")
            return False, 0, 0, "SKIP_NO_EXIT_LIQUIDITY"

    # Kelly动态仓位（修正公式 + base_rate/相关性缩减 + P3流动性上限）
    p_win_final = entry_details.get("p_win_final") if entry_details else None
    size = calculate_kelly_size(
        confidence, ev, balance, target_price=price, p_hat=p_hat,
        p_win=p_win_final, kelly_reduction=kelly_reduction,
        exit_bid_depth=bid_depth
    )

    if size < 1:
        print("  ⚠️ 风险预算不足，计算仓位小于最小可执行值，跳过下注")
        return False, 0, 0, "SKIP_SIZE_TOO_SMALL"

    # FOK 深度感知：遍历 asks 计算能覆盖 size 的限价
    fok_price = price
    original_size = size
    if sorted_asks_full and price_source == "best_ask" and size > 0:
        cumulative = 0.0
        cover_price = price
        MAX_SLIPPAGE = 0.05  # 最多允许 5 tick 滑点
        for level in sorted_asks_full:
            lv_price = float(level["price"])
            lv_size = float(level["size"])
            if lv_price > price + MAX_SLIPPAGE:
                break
            cumulative += lv_size
            cover_price = lv_price
            if cumulative >= size:
                break
        if cumulative >= size:
            # 深度够：限价始终 +1 tick（防止 best_ask 被抢导致 FOK 失败）
            fok_price = min(round(cover_price + 0.01, 2), MAX_PRICE - 0.01)
            if fok_price > price:
                print(f"  📡 FOK深度滑点: 覆盖{size}份需到${cover_price:.2f}，限价${price:.2f}→${fok_price:.2f}")
            else:
                # 一档就够，仍然 +1 tick 确保成交
                fok_price = min(round(price + 0.01, 2), MAX_PRICE - 0.01)
                print(f"  📡 FOK限价+1tick安全余量: ${price:.2f}→${fok_price:.2f}")
        elif cumulative >= 5:
            # 深度不够但≥5份：缩减 size + 限价到最远档
            size = int(cumulative)
            fok_price = min(round(cover_price + 0.01, 2), MAX_PRICE - 0.01)
            print(f"  ⚠️ ask深度不足: 缩减为{size}份，限价${price:.2f}→${fok_price:.2f}")
        else:
            print(f"  ⚠️ ask深度极低({cumulative:.1f}<5)，跳过下注")
            return False, 0, 0, "SKIP_NO_ASK_DEPTH"

    print(f"  💸 SDK下单: FOK BUY {size}@{fok_price} token={token_id[:16]}...")
    info = clob_client.place_fok_order(token_id, BUY, fok_price, size)
    output = info.get("raw", "")

    # FOK: matched=True 即全部成交，否则全部未成交（不会产生挂单）
    success = info.get("matched", False)
    pending = False

    # FOK 失败重试：提价 +2tick 重试，再失败则缩量 80% 重试
    if not success:
        print(f"  ❌ FOK未成交: Status={info.get('status')} | {info.get('elapsed_ms', 0):.0f}ms")
        # 回查链上余额：网络超时不代表订单未执行（幽灵成交）
        ghost_filled = False
        try:
            from position_monitor import get_token_balance
            ghost_balance = get_token_balance(token_id)
            if ghost_balance is not None and ghost_balance >= 1.0:
                print(f"  ⚡ 幽灵成交检测: 链上余额={ghost_balance:.2f}份，订单实际已成交")
                success = True
                size = round(ghost_balance, 4)
                ghost_filled = True
        except Exception as e_ghost:
            print(f"  ⚠️ 幽灵成交检测失败: {e_ghost}")

        if not ghost_filled:
            # 重试1：提价 +2 tick
            retry_price = min(round(fok_price + 0.02, 2), MAX_PRICE - 0.01)
            print(f"  🔄 重试1: 提价${fok_price:.2f}→${retry_price:.2f} | {size}份")
            info2 = clob_client.place_fok_order(token_id, BUY, retry_price, size)
            if info2.get("matched", False):
                info = info2
                success = True
                fok_price = retry_price
                print(f"  ✅ 重试1成交 | {info2.get('elapsed_ms', 0):.0f}ms")
            else:
                # 重试2：缩量 80%
                retry_size = max(int(original_size * 0.8), 5)
                if retry_size < size:
                    print(f"  🔄 重试2: 缩量{size}→{retry_size}份 @ ${retry_price:.2f}")
                    info3 = clob_client.place_fok_order(token_id, BUY, retry_price, retry_size)
                    if info3.get("matched", False):
                        info = info3
                        success = True
                        size = retry_size
                        fok_price = retry_price
                        print(f"  ✅ 重试2成交 | {info3.get('elapsed_ms', 0):.0f}ms")
                    else:
                        print(f"  ❌ 重试2仍未成交 | {info3.get('elapsed_ms', 0):.0f}ms")

    # 计算实际成交价和实际份数
    actual_size = size
    if success and size > 0 and info.get("making", 0) > 0:
        actual_price = round(info["making"] / size, 4)
        if info.get("taking", 0) > 0:
            actual_size = round(info["taking"], 4)
        print(f"  📊 FOK成交: Making=${info['making']:.4f} | Taking={actual_size} | 实际价=${actual_price:.4f} (限价=${price}) | {info.get('elapsed_ms', 0):.0f}ms")
    else:
        actual_price = price

    # 记录下注结果（使用实际成交价）
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "direction": direction,
        "token_id": token_id,
        "price": actual_price,
        "limit_price": price,
        "size": actual_size,
        "requested_size": size,
        "amount": actual_price * actual_size,
        "success": success,
        "pending": False,
        "order_id": info.get("order_id"),
        "status": info.get("status"),
        "output": output[:200],  # 截断输出
    }

    with open("logs/bets.jsonl", "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    # 如果下注成功，记录持仓（丰富字段供 position_monitor 使用）
    if success:
        position = {
            "token_id": token_id,
            "slug": slug,
            "direction": direction,
            "entry_price": actual_price,
            "size": actual_size,
            "confidence": confidence,
            "ev": ev,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "closed": False,
        }
        # 从 entry_details 提取关键字段（供 monitor 的 EV 计算使用）
        if entry_details:
            for key in ("price_to_beat", "atr", "estimated_value", "diff_in_atr",
                         "base_rate", "p_win_final"):
                if key in entry_details:
                    position[key] = entry_details[key]
            # 兼容字段名
            if "atr" in entry_details:
                position["atr_val"] = entry_details["atr"]
            if "price_to_beat" in entry_details:
                position["ptb"] = entry_details["price_to_beat"]
            # P1: 反向token_id（供对冲使用）
            if "opposite_token_id" in entry_details:
                position["opposite_token_id"] = entry_details["opposite_token_id"]
        # 买入后查链上真实余额（同步，写入position供monitor用）
        try:
            real_balance = clob_client.get_token_balance(token_id)
            if real_balance and real_balance > 0:
                position["token_balance"] = real_balance
        except Exception:
            pass
        # allowance刷新异步执行（不阻塞主流程，省~270ms）
        _bet_executor.submit(clob_client.update_token_allowance, token_id)

        with open("logs/positions.jsonl", "a") as f:
            f.write(json.dumps(position) + "\n")

    if pending:
        pending_msg = f"PENDING_LIVE:{info.get('order_id')}" if info.get("order_id") else "PENDING_LIVE"
        return False, actual_price, actual_size, pending_msg

    return success, actual_price, actual_size, output


if __name__ == "__main__":
    if len(sys.argv) > 1:
        coin = sys.argv[1]
        ptb = float(sys.argv[2])
        up_odds = float(sys.argv[3])
        down_odds = float(sys.argv[4])

        should_bet, direction, confidence, details = analyze_and_decide(
            coin, ptb, up_odds, down_odds, "test"
        )

        print(f"Direction: {direction}")
        print(f"Confidence: {confidence*100:.1f}%")
        print(f"Should bet: {should_bet}")
        print(f"EV: {details.get('expected_value', 0):+.3f}")
        print(f"Details: {json.dumps(details, indent=2)}")
