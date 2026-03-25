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
import time
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


from ai_trader.ai_model_v2 import analyze_market
from ai_trader.base_rate import get_base_rate
from ai_trader import clob_client
from ai_trader.fees import effective_fee_rate, estimate_buy_fill
from py_clob_client.order_builder.constants import BUY

_bet_executor = ThreadPoolExecutor(max_workers=3)


def _directional_env_float(base_key, direction, default):
    """按方向读取环境变量，优先级: BASE_KEY_{DIR} -> BASE_KEY -> default."""
    suffix = (direction or "").upper()
    if suffix in ("UP", "DOWN"):
        value = os.environ.get(f"{base_key}_{suffix}")
        if value not in (None, ""):
            return float(value)
    return float(os.environ.get(base_key, str(default)))


def _size_step():
    try:
        step = float(os.environ.get("SIZE_STEP", "0.1"))
    except (TypeError, ValueError):
        step = 0.1
    return step if step > 0 else 0.1


def _round_size(value, mode="nearest", step=None):
    value = max(float(value or 0), 0.0)
    step = step or _size_step()
    if step <= 0:
        return round(value, 6)

    units = value / step
    if mode == "ceil":
        rounded_units = math.ceil(units - 1e-9)
    elif mode == "floor":
        rounded_units = math.floor(units + 1e-9)
    else:
        rounded_units = round(units)
    return round(rounded_units * step, 6)


def _buy_fill_ratio(price, fee_rate_bps):
    fee_rate = effective_fee_rate(price, fee_rate_bps)
    return max(1.0 - fee_rate, 1e-9), fee_rate


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


def _make_pending_order_id(slug, token_id):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{slug}-{str(token_id)[:12]}-{stamp}"


def _queue_pending_order(
    slug,
    direction,
    token_id,
    info,
    quoted_price,
    limit_price,
    price_source,
    snapshot_age_ms,
    requested_size,
    requested_net_size,
    planned_net_size,
    confidence,
    ev,
    entry_details,
):
    pending_id = info.get("order_id") or _make_pending_order_id(slug, token_id)
    entry = {
        "pending_id": pending_id,
        "order_id": info.get("order_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "direction": direction,
        "token_id": token_id,
        "status": "PENDING",
        "quoted_price": quoted_price,
        "limit_price": limit_price,
        "price_source": price_source,
        "snapshot_age_ms": snapshot_age_ms,
        "requested_size": requested_size,
        "requested_net_size": requested_net_size,
        "planned_net_size": planned_net_size,
        "confidence": confidence,
        "ev": ev,
        "entry_details": entry_details or {},
        "raw": (info.get("raw") or info.get("error") or "")[:200],
        "reason": "transport_uncertain_fill",
        "cost_recorded": False,
    }
    _append_pending_order(entry)
    return pending_id


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
    fee_rate_bps = clob_client.get_fee_rate_bps(token_id) if token_id else 0
    details["fee_rate_bps"] = fee_rate_bps
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
        gate_p_hat = bayesian_info.get("p_hat", 0.5)
        gate_conf = bayesian_info.get("confidence", 0)
        gate_dir = bayesian_info.get("direction", direction)
        incremental_p_hat = bayesian_info.get("incremental_p_hat", gate_p_hat)
        incremental_conf = bayesian_info.get("incremental_confidence", gate_conf)
        incremental_dir = bayesian_info.get("incremental_direction", gate_dir)
        state_conf = bayesian_info.get("state_confidence", 0)

        # 早期窗口融合门槛与入场门槛对齐(0.15)，避免死区
        is_early_window = extra_info.get("early_window", False) if extra_info else False
        fusion_threshold = 0.15 if is_early_window else 0.3

        if gate_dir == direction and incremental_dir == direction and incremental_conf > fusion_threshold:
            # 仅用”增量后验”参与 p_win 融合，避免把 state probability 双重计入 EV。
            # v12 fix: 双向融合，允许 Bayesian 拉低过高的 RW p_win
            fused_p = p_win * 0.4 + incremental_p_hat * 0.6
            p_win = fused_p
            confidence = confidence * 0.4 + gate_conf * 0.6
            details["confidence_source"] = "bayesian_fused"
        elif gate_dir == direction and gate_conf > fusion_threshold:
            # gate 方向与主模型一致，但增量证据偏弱时，只温和提升整体置信度。
            confidence = confidence * 0.5 + gate_conf * 0.5
            details["confidence_source"] = "bayesian_gate_support"
        elif incremental_dir != direction and incremental_conf > fusion_threshold:
            confidence = confidence * 0.5
            details["confidence_source"] = "bayesian_incremental_conflict"
        elif gate_dir != direction and gate_conf > fusion_threshold:
            confidence *= 0.75
            details["confidence_source"] = "bayesian_gate_conflict"
        else:
            details["confidence_source"] = "discount_only"

        details["bayesian_p_hat"] = round(gate_p_hat, 4)
        details["bayesian_confidence"] = round(gate_conf, 4)
        details["bayesian_direction"] = gate_dir
        details["bayesian_incremental_p_hat"] = round(incremental_p_hat, 4)
        details["bayesian_incremental_confidence"] = round(incremental_conf, 4)
        details["bayesian_incremental_direction"] = incremental_dir
        details["bayesian_state_confidence"] = round(state_conf, 4) if state_conf is not None else None

    P_WIN_CAP = float(os.environ.get("P_WIN_CAP", "0.92"))
    p_win = min(p_win, P_WIN_CAP)  # P1: 防止贝叶斯累积推高 p_win (env可配置)

    # v12: Shrinkage toward 0.5 — 纠正 RW + Bayesian 系统性过度自信
    # 模型说 0.85 → 校准后 0.745; 说 0.70 → 校准后 0.64
    # 用实盘数据逐步调整: 胜率持续高于预期就放大，低于预期就缩小
    P_WIN_SHRINKAGE = float(os.environ.get("P_WIN_SHRINKAGE", "0.70"))
    p_win_raw = p_win
    p_win = 0.5 + (p_win - 0.5) * P_WIN_SHRINKAGE
    details["p_win_raw"] = round(p_win_raw, 4)
    details["p_win_shrinkage"] = P_WIN_SHRINKAGE

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
        # 空簿检测：best_ask >= 0.95 说明CLOB无真实卖单
        if exec_price >= 0.95 and token_id:
            details["clob_empty_book"] = True
            details["clob_raw_ask"] = round(exec_price, 4)
            last_price = clob_client.get_last_trade_price(token_id)
            if last_price and 0.01 < last_price < 0.99:
                # v12 fix: last-trade-price 也 >= 0.90 说明市场真的定价到极端，不是空簿
                # 这时候不应该用更低的价格覆盖，而是承认"没有入场机会"
                if last_price >= 0.90:
                    print(f"  📡 C1校准：市场已极端定价 ask=${exec_price:.3f} last=${last_price:.3f}，非空簿")
                else:
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
    entry_fee_rate = effective_fee_rate(target_odds, fee_rate_bps)
    net_entry_denominator = max(1.0 - entry_fee_rate, 1e-9)
    effective_entry_price = target_odds / net_entry_denominator if fee_rate_bps > 0 else target_odds
    entry_fee_cost = max(0.0, effective_entry_price - target_odds)
    exit_fee_basis = None
    if liquidity_info and liquidity_info.get("best_bid"):
        exit_fee_basis = liquidity_info["best_bid"]
    elif 0.01 < target_odds < 0.99:
        exit_fee_basis = max(round(target_odds - raw_spread, 4), 0.01)
    exit_fee_rate = effective_fee_rate(exit_fee_basis, fee_rate_bps) if exit_fee_basis else 0.0
    exit_fee_cost = (exit_fee_basis * exit_fee_rate * EARLY_EXIT_RATIO) if exit_fee_basis else 0.0
    ev_gross = p_win - target_odds
    ev = ev_gross - spread_cost - entry_fee_cost - exit_fee_cost
    details["expected_value"] = round(ev, 4)
    details["ev_gross"] = round(ev_gross, 4)
    details["spread_cost"] = round(spread_cost, 4)
    details["entry_fee_cost"] = round(entry_fee_cost, 4)
    details["entry_fee_rate"] = round(entry_fee_rate, 6)
    details["effective_entry_price"] = round(effective_entry_price, 4)
    details["expected_exit_fee_cost"] = round(exit_fee_cost, 4)
    details["expected_exit_fee_rate"] = round(exit_fee_rate, 6)
    if exit_fee_basis:
        details["expected_exit_fee_basis"] = round(exit_fee_basis, 4)
    details["p_win_final"] = round(p_win, 4)
    details["ev_positive"] = ev > 0

    # ── ATR 最低偏离门槛：过滤噪音信号 ──
    MIN_ATR_DEVIATION = float(os.environ.get("MIN_ATR_DEVIATION", "1.5"))

    MAX_PRICE = _directional_env_float("MAX_BUY_PRICE", direction, 0.92)
    MIN_EV = _directional_env_float("MIN_EV", direction, 0.05)
    MIN_CONFIDENCE = _directional_env_float("MIN_CONFIDENCE", direction, 0.60)

    # 早期窗口放宽门槛：优势是CLOB价格好，不需要那么高的置信度/EV
    is_early = extra_info.get("early_window", False) if extra_info else False
    if is_early:
        MIN_EV = _directional_env_float("EARLY_MIN_EV", direction, 0.03)
        MIN_CONFIDENCE = _directional_env_float("EARLY_MIN_CONFIDENCE", direction, 0.40)

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
    details["max_buy_price_threshold"] = round(MAX_PRICE, 4)
    details["min_ev_threshold"] = round(MIN_EV, 4)
    details["min_confidence_threshold"] = round(MIN_CONFIDENCE, 4)
    early_label = " [早期窗口]" if is_early else ""
    trend_label = f" 5m:{trend_5m_align}" if trend_5m_align != "neutral" else ""
    filter_label = f" [{details.get('trend_15m_filter', '')}]" if details.get("trend_15m_filter") else ""
    details["bet_reason"] = (
        f"atr={diff_in_atr:.2f}({'✅' if diff_in_atr>=MIN_ATR_DEVIATION else '❌'}≥{MIN_ATR_DEVIATION}) "
        f"ev={ev:+.4f}({'✅' if ev>ev_threshold else '❌'}>{ev_threshold:.3f}[adapt],"
        f"gross={ev_gross:+.3f},spread={spread_cost:.3f},fee_in={entry_fee_cost:.3f},fee_out={exit_fee_cost:.3f}) "
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
                          p_win=None, kelly_reduction=1.0, exit_bid_depth=None,
                          fee_rate_bps=0, return_details=False):
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
    MIN_BET = float(os.environ.get("MIN_BET_SIZE", "5"))
    MAX_BET = float(os.environ.get("MAX_BET_SIZE", "10"))
    P_CAP = float(os.environ.get("P_WIN_CAP", "0.92"))
    size_step = _size_step()

    def _result(skip_reason=None, **kwargs):
        result = {
            "gross_order_size": 0.0,
            "min_gross_size": 0.0,
            "raw_net_size": 0.0,
            "target_net_size": 0.0,
            "expected_net_size": 0.0,
            "max_net_by_balance": 0.0,
            "max_net_by_liquidity": None,
            "effective_entry_price": 0.0,
            "entry_fee_rate": 0.0,
            "forced_to_min": False,
            "skip_reason": skip_reason,
        }
        result.update(kwargs)
        return result if return_details else result["gross_order_size"]

    if ev <= 0:
        logger.info(f"  📊 Kelly仓位: EV={ev:.3f}≤0 → 跳过")
        return _result(skip_reason="EV_NON_POSITIVE")

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
        return _result(skip_reason="KELLY_NON_POSITIVE")

    # 1/4 Kelly + 缩减因子
    kelly_quarter = kelly_full * 0.25 * kelly_reduction
    kelly_quarter = max(0, min(0.25, kelly_quarter))

    # Kelly 比例先换算为净份额：资金预算 / 含fee的净入场成本
    fill_ratio, entry_fee_rate = _buy_fill_ratio(price, fee_rate_bps)
    effective_entry_price = price / fill_ratio if price > 0 else 0.0
    dollar_amount = balance * kelly_quarter
    raw_net_size = (dollar_amount / effective_entry_price) if effective_entry_price > 0 else 0.0
    if raw_net_size <= 0:
        logger.info(
            f"  📊 Kelly仓位: 预算${dollar_amount:.2f} 经fee换算后净仓位{raw_net_size:.4f}份 ≤ 0 → 跳过"
        )
        return _result(
            skip_reason="NET_SIZE_NON_POSITIVE",
            raw_net_size=round(raw_net_size, 6),
            effective_entry_price=round(effective_entry_price, 6),
            entry_fee_rate=round(entry_fee_rate, 6),
        )

    # 余额约束：单笔不超过余额的一定比例（安全网）
    max_entry_balance_pct = float(os.environ.get("MAX_ENTRY_BALANCE_PCT", "0.20"))
    max_entry_balance_pct = min(max(max_entry_balance_pct, 0.0), 1.0)
    max_gross_by_balance = _round_size(balance * max_entry_balance_pct / price, mode="floor", step=size_step) if price > 0 else 0.0
    max_net_by_balance = max_gross_by_balance * fill_ratio

    # P3: 流动性上限 — 不超过退出流动性的50%
    max_net_by_liquidity = None
    if exit_bid_depth and exit_bid_depth > 0:
        max_net_by_liquidity = exit_bid_depth * 0.5

    hard_net_cap = min(MAX_BET, max_net_by_balance)
    if max_net_by_liquidity is not None:
        hard_net_cap = min(hard_net_cap, max_net_by_liquidity)

    if hard_net_cap < MIN_BET:
        logger.info(
            f"  📊 Kelly仓位: 可执行净仓位上限{hard_net_cap:.4f}份 < 最小净仓位{MIN_BET:.1f}份，跳过"
        )
        return _result(
            skip_reason="HARD_CAP_BELOW_MIN",
            raw_net_size=round(raw_net_size, 6),
            max_net_by_balance=round(max_net_by_balance, 6),
            max_net_by_liquidity=round(max_net_by_liquidity, 6) if max_net_by_liquidity is not None else None,
            effective_entry_price=round(effective_entry_price, 6),
            entry_fee_rate=round(entry_fee_rate, 6),
        )

    forced_to_min = 0 < raw_net_size < MIN_BET
    target_net_size = MIN_BET if forced_to_min else raw_net_size
    target_net_size = min(target_net_size, hard_net_cap)

    gross_order_size = _round_size(target_net_size / fill_ratio, mode="ceil", step=size_step)
    min_gross_size = _round_size(MIN_BET / fill_ratio, mode="ceil", step=size_step)
    if gross_order_size > max_gross_by_balance + 1e-9:
        gross_order_size = max_gross_by_balance

    expected_net_size = gross_order_size * fill_ratio
    if expected_net_size < MIN_BET:
        logger.info(
            f"  📊 Kelly仓位: 预计净仓位{expected_net_size:.4f}份 < 最小净仓位{MIN_BET:.1f}份，跳过"
        )
        return _result(
            skip_reason="EXPECTED_NET_BELOW_MIN",
            gross_order_size=round(gross_order_size, 6),
            min_gross_size=round(min_gross_size, 6),
            raw_net_size=round(raw_net_size, 6),
            target_net_size=round(target_net_size, 6),
            expected_net_size=round(expected_net_size, 6),
            max_net_by_balance=round(max_net_by_balance, 6),
            max_net_by_liquidity=round(max_net_by_liquidity, 6) if max_net_by_liquidity is not None else None,
            effective_entry_price=round(effective_entry_price, 6),
            entry_fee_rate=round(entry_fee_rate, 6),
            forced_to_min=forced_to_min,
        )

    red_label = f" red={kelly_reduction}" if kelly_reduction < 1.0 else ""
    liq_label = f" liq_cap={exit_bid_depth:.0f}" if exit_bid_depth else ""
    min_label = f" raw_net={raw_net_size:.2f}→min{MIN_BET:.1f}" if forced_to_min else ""
    logger.info(
        f"  📊 Kelly仓位: p={p:.3f} price={price:.3f} eff={effective_entry_price:.4f} "
        f"f*={kelly_full:.3f} f/4={kelly_quarter:.3f} ${dollar_amount:.1f}{red_label}{liq_label}{min_label} "
        f"→ net={expected_net_size:.2f}份 gross={gross_order_size:.2f}份"
    )

    return _result(
        gross_order_size=round(gross_order_size, 6),
        min_gross_size=round(min_gross_size, 6),
        raw_net_size=round(raw_net_size, 6),
        target_net_size=round(target_net_size, 6),
        expected_net_size=round(expected_net_size, 6),
        max_net_by_balance=round(max_net_by_balance, 6),
        max_net_by_liquidity=round(max_net_by_liquidity, 6) if max_net_by_liquidity is not None else None,
        effective_entry_price=round(effective_entry_price, 6),
        entry_fee_rate=round(entry_fee_rate, 6),
        forced_to_min=forced_to_min,
    )


def _is_valid_clob_price(price):
    return price is not None and 0.01 < float(price) < 0.99


def _extract_orderbook_levels(book):
    if not book:
        return [], []
    from ai_trader.polymarket_api import normalize_orderbook
    raw_bids = [{"price": b.price, "size": b.size} for b in (book.bids or [])]
    raw_asks = [{"price": a.price, "size": a.size} for a in (book.asks or [])]
    return normalize_orderbook(raw_bids, raw_asks)


def _build_quote_from_levels(bids, asks, source, age_ms=None):
    quote = {
        "best_bid": None,
        "best_ask": None,
        "bids": bids or [],
        "asks": asks or [],
        "bid_depth": None,
        "source": source,
        "age_ms": age_ms,
    }
    if bids:
        quote["best_bid"] = float(bids[0]["price"])
        quote["bid_depth"] = sum(float(b["size"]) for b in bids)
    if asks:
        quote["best_ask"] = float(asks[0]["price"])
    if quote["best_bid"] is not None and quote["best_ask"] is not None:
        if quote["best_bid"] >= quote["best_ask"]:
            quote["best_bid"] = None
            quote["best_ask"] = None
            quote["asks"] = []
    return quote


def _get_execution_quote(token_id, fallback_book=None, force_fresh=False):
    # v12: WS 超时放宽到 2000ms — WS 实时推送每 ~100ms 更新一次，2s 内的数据完全可用
    # 这避免了 ~300ms 的 REST 回退，是执行加速的关键
    ws_max_age_ms = float(os.environ.get("ENTRY_WS_MAX_AGE_MS", "2000"))
    ws_quote = None
    try:
        from ai_trader.polymarket_ws import poly_ws
        ws_book = poly_ws.get_book_snapshot(token_id)
        if ws_book and ws_book.get("age_ms", ws_max_age_ms + 1) <= ws_max_age_ms:
            bids = ws_book.get("bids", [])
            asks = ws_book.get("asks", [])
            ws_quote = _build_quote_from_levels(bids, asks, "ws_book", ws_book.get("age_ms"))
            if _is_valid_clob_price(ws_quote.get("best_ask")) and ws_quote.get("asks"):
                return ws_quote

        ws_bba = poly_ws.get_best_bid_ask_snapshot(token_id)
        if ws_bba and ws_bba.get("age_ms", ws_max_age_ms + 1) <= ws_max_age_ms:
            best_bid = ws_bba.get("best_bid")
            best_ask = ws_bba.get("best_ask")
            if _is_valid_clob_price(best_ask) and (best_bid is None or best_bid < best_ask):
                ws_quote = {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "bids": [],
                    "asks": [],
                    "bid_depth": None,
                    "source": "ws_best_ask",
                    "age_ms": ws_bba.get("age_ms"),
                }
    except Exception:
        ws_quote = None

    # WS 无数据时回退 REST（~300ms）
    rest_book = None
    try:
        rest_book = clob_client.get_orderbook(token_id, max_age=0 if force_fresh else 1)
    except Exception:
        rest_book = None

    if rest_book:
        bids, asks = _extract_orderbook_levels(rest_book)
        quote = _build_quote_from_levels(bids, asks, "rest_book", 0.0)
        if _is_valid_clob_price(quote.get("best_ask")):
            return quote

    if fallback_book:
        bids, asks = _extract_orderbook_levels(fallback_book)
        quote = _build_quote_from_levels(bids, asks, "prefetched_book", None)
        if _is_valid_clob_price(quote.get("best_ask")):
            return quote

    # v12 fix: 没有有效 ask 但 bid ≥ 0.90 → 市场已极端定价，返回 ask=0.99
    # 这让 MAX_PRICE 检查自然拦截，不会走到分析价回退产生幻影信号
    for candidate in [ws_quote, None]:
        # 检查所有已获取的订单簿来源
        pass
    best_bid_seen = None
    for source_book in [rest_book, fallback_book]:
        if source_book:
            try:
                _b, _ = _extract_orderbook_levels(source_book)
                if _b:
                    best_bid_seen = float(_b[0]["price"])
                    break
            except Exception:
                pass
    if best_bid_seen is None and ws_quote and ws_quote.get("best_bid"):
        best_bid_seen = ws_quote["best_bid"]

    if best_bid_seen is not None and best_bid_seen >= 0.90:
        return {
            "best_bid": best_bid_seen,
            "best_ask": 0.99,
            "bids": [],
            "asks": [],
            "bid_depth": None,
            "source": "extreme_priced",
            "age_ms": None,
        }

    return ws_quote or {
        "best_bid": None,
        "best_ask": None,
        "bids": [],
        "asks": [],
        "bid_depth": None,
        "source": "none",
        "age_ms": None,
    }


def _derive_entry_price_cap(max_price, entry_details=None):
    caps = [round(max_price, 2)]
    p_win_cap = entry_details.get("p_win_final") if entry_details else None
    if p_win_cap and p_win_cap > 0.10:
        caps.append(round(p_win_cap, 2))
    return max(0.01, min(caps))


def _compute_chase_price_cap(price, price_cap, ev):
    price = round(price, 2)
    price_cap = round(price_cap, 2)
    if price >= price_cap:
        return price_cap

    available_ticks = max(0, int(round((price_cap - price) * 100)))
    if available_ticks == 0:
        return price_cap

    chase_ticks = 2
    if ev >= 0.03:
        chase_ticks += 1
    if ev >= 0.06:
        chase_ticks += 1
    if ev >= 0.10:
        chase_ticks += 1
    if (price_cap - price) >= 0.05:
        chase_ticks = max(chase_ticks, 5)

    chase_ticks = min(chase_ticks, available_ticks)
    return round(min(price + chase_ticks * 0.01, price_cap), 2)


def _measure_ask_depth(asks, target_size=None, max_price=None):
    top3_size = sum(float(level.get("size", 0)) for level in (asks or [])[:3])
    cumulative = 0.0
    cover_price = None
    levels = 0

    for level in asks or []:
        ask_price = float(level["price"])
        ask_size = float(level["size"])
        if max_price is not None and ask_price > max_price + 1e-9:
            break
        cumulative += ask_size
        cover_price = ask_price
        levels += 1
        if target_size is not None and cumulative >= target_size:
            break

    return {
        "top3_size": round(top3_size, 4),
        "cover_size": round(cumulative, 4),
        "cover_price": cover_price,
        "levels": levels,
        "covered": bool(target_size is not None and cumulative >= target_size),
    }


def _plan_fok_entry(asks, desired_size, best_ask, price_cap, ev, min_size):
    if desired_size <= 0:
        return None

    size_step = _size_step()
    best_ask = round(best_ask, 2)
    price_cap = round(price_cap, 2)
    if best_ask > price_cap:
        return None

    chase_cap = _compute_chase_price_cap(best_ask, price_cap, ev)
    coverage = _measure_ask_depth(asks, desired_size, max_price=chase_cap)
    cover_price = coverage.get("cover_price") or best_ask
    limit_price = min(round(cover_price + 0.01, 2), price_cap)

    if coverage["covered"]:
        return {
            "size": _round_size(desired_size, mode="ceil", step=size_step),
            "limit_price": limit_price,
            "coverage": coverage,
            "mode": "full",
            "chase_cap": chase_cap,
        }

    available_size = _round_size(coverage["cover_size"], mode="floor", step=size_step)
    if available_size >= min_size:
        return {
            "size": min(available_size, _round_size(desired_size, mode="floor", step=size_step)),
            "limit_price": limit_price,
            "coverage": coverage,
            "mode": "reduced",
            "chase_cap": chase_cap,
        }

    return {
        "size": 0,
        "limit_price": limit_price,
        "coverage": coverage,
        "mode": "skip",
        "chase_cap": chase_cap,
    }


def _is_explicit_fok_kill(info):
    err = f"{info.get('error', '')} {info.get('raw', '')}".lower()
    return "fully filled" in err or "killed" in err


def _is_transport_uncertain_fill(info):
    err = f"{info.get('error', '')} {info.get('raw', '')}".lower()
    if "425" in err or "too early" in err or "not ready" in err:
        return False
    markers = (
        "request exception",
        "status_code=none",
        "timeout",
        "timed out",
        "read error",
        "connection reset",
        "connection aborted",
        "server disconnected",
    )
    return any(marker in err for marker in markers)


def _detect_ghost_fill(token_id, expected_size=None):
    try:
        from position_monitor import get_token_balance
        attempts = max(1, int(os.environ.get("GHOST_FILL_RECHECKS", "4")))
        interval = max(0.0, float(os.environ.get("GHOST_FILL_RECHECK_INTERVAL", "0.25")))
        min_fill = max(0.1, float(os.environ.get("GHOST_FILL_MIN_SIZE", os.environ.get("PENDING_MIN_FILL", "0.5"))))

        for attempt in range(1, attempts + 1):
            ghost_balance = get_token_balance(token_id)
            if ghost_balance is not None and ghost_balance >= min_fill:
                size_hint = f"/~{float(expected_size):.2f}" if expected_size else ""
                print(
                    f"  ⚡ 幽灵成交检测: 链上余额={ghost_balance:.2f}{size_hint}份 "
                    f"(第{attempt}/{attempts}次)，订单实际已成交"
                )
                return round(ghost_balance, 4)
            if attempt < attempts:
                time.sleep(interval)
    except Exception as e_ghost:
        print(f"  ⚠️ 幽灵成交检测失败: {e_ghost}")
    return None


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
    # ── 余额 ──
    # 余额：优先用调用方预取的结果，省一次HTTP
    # v12: 订单簿不再此处获取，由 _get_execution_quote 统一处理（WS优先，避免双重REST）
    if pre_balance is not None:
        balance = pre_balance
    else:
        balance = clob_client.get_balance()
    book = None  # 由 _get_execution_quote 处理

    # 余额不足时直接跳过，不记录为失败
    MIN_BALANCE = float(os.environ.get("MIN_BALANCE", "5.0"))
    if balance < MIN_BALANCE:
        print(f"  ⚠️ 余额不足: ${balance:.2f} < ${MIN_BALANCE:.2f}，跳过下注")
        return False, 0, 0, "SKIP_NO_BALANCE"

    SLIPPAGE = float(os.environ.get("SLIPPAGE", "0.01"))
    MAX_PRICE = _directional_env_float("MAX_BUY_PRICE", direction, 0.92)
    MIN_BET = float(os.environ.get("MIN_BET_SIZE", "5"))
    p_win_final = entry_details.get("p_win_final") if entry_details else None
    price_cap = _derive_entry_price_cap(MAX_PRICE, entry_details)

    quote = _get_execution_quote(token_id, fallback_book=book, force_fresh=True)
    price = quote.get("best_ask")
    price_source = quote.get("source", "unknown")
    bid_depth = quote.get("bid_depth")
    sorted_asks_full = quote.get("asks", [])
    snapshot_age_ms = quote.get("age_ms")

    if _is_valid_clob_price(price):
        age_label = f" | age={snapshot_age_ms:.0f}ms" if snapshot_age_ms is not None else ""
        print(f"  📡 执行前刷新: {price_source} ask=${float(price):.2f}{age_label}")

    # 2. last-trade-price + 滑点 回退
    if not _is_valid_clob_price(price):
        last_price = clob_client.get_last_trade_price(token_id)
        if last_price and 0.01 < last_price < 0.99:
            price = min(round(last_price + SLIPPAGE, 2), 0.99)
            price_source = "last_trade"
            print(f"  📡 last-trade回退: ${last_price:.2f} + 滑点${SLIPPAGE} → 限价${price:.2f}")

    # 3. midpoint + 滑点 回退
    if not _is_valid_clob_price(price):
        mid = clob_client.get_midpoint(token_id)
        if mid and 0.01 < mid < 0.99:
            price = min(round(mid + SLIPPAGE, 2), 0.99)
            price_source = "midpoint"
            print(f"  📡 midpoint回退: ${mid:.2f} + 滑点${SLIPPAGE} → 限价${price:.2f}")

    # 4. 分析阶段的 CLOB 执行价兜底（C1校准后的价格）
    # v12 fix: 仅当 CLOB 没有极端定价时才回退分析价
    # 如果 CLOB raw_ask >= 0.90（市场已极端定价），分析价可能是过时的 Gamma 赔率，不能用
    if not _is_valid_clob_price(price) and entry_details:
        clob_raw = entry_details.get("clob_raw_ask", 0)
        if clob_raw and float(clob_raw) >= 0.90:
            print(f"  📡 分析价回退跳过: CLOB原始ask=${float(clob_raw):.2f}≥0.90，市场已极端定价")
        else:
            analysis_price = entry_details.get("exec_price") or entry_details.get("target_odds")
            if analysis_price and 0.01 < float(analysis_price) < 0.99:
                price = min(round(float(analysis_price) + SLIPPAGE, 2), 0.99)
                price_source = "analysis_exec_price"
                print(f"  📡 分析价回退: ${float(analysis_price):.2f} + 滑点${SLIPPAGE} → 限价${price:.2f}")

    # 安全检查：全部失败
    if not _is_valid_clob_price(price):
        print(f"  ⚠️ 无法获取真实价格（WS+REST+last-trade+midpoint+分析价均失败），跳过下注")
        return False, 0, 0, "SKIP_NO_PRICE"

    # 价格四舍五入到2位小数（Polymarket要求）
    price = round(price, 2)

    # 安全检查：实际买入价不能超过上限（env可配置，默认0.92）
    if price >= MAX_PRICE:
        print(f"  ⚠️ 实际买入价${price:.2f}≥${MAX_PRICE}，上行空间不足，跳过下注")
        return False, 0, 0, "SKIP_PRICE_TOO_HIGH"

    if price > price_cap:
        print(f"  ⚠️ 执行前刷新后 ask=${price:.2f} > 限价上限${price_cap:.2f}，跳过下注")
        return False, 0, 0, "SKIP_REQUOTE_PRICE_TOO_HIGH"

    # P2: 退出流动性检查（已从上面的book提取，无额外HTTP）
    if bid_depth is not None:
        print(f"  📊 P2退出流动性: bid_depth={bid_depth:.1f}份")
        if bid_depth < 5:
            print(f"  ⚠️ 退出流动性极低({bid_depth:.1f}<5)，跳过下注")
            return False, 0, 0, "SKIP_NO_EXIT_LIQUIDITY"

    fee_rate_bps = clob_client.get_fee_rate_bps(token_id)

    # Kelly动态仓位：先算目标净仓位，再反推 gross 下单量
    size_plan = calculate_kelly_size(
        confidence, ev, balance, target_price=price, p_hat=p_hat,
        p_win=p_win_final, kelly_reduction=kelly_reduction,
        exit_bid_depth=bid_depth, fee_rate_bps=fee_rate_bps, return_details=True,
    )
    if not isinstance(size_plan, dict):
        size_plan = {
            "gross_order_size": float(size_plan or 0),
            "min_gross_size": float(MIN_BET),
            "raw_net_size": float(size_plan or 0),
            "target_net_size": float(size_plan or 0),
            "expected_net_size": float(size_plan or 0),
            "forced_to_min": False,
            "entry_fee_rate": 0.0,
            "skip_reason": None,
        }

    size = float(size_plan.get("gross_order_size") or 0)
    min_gross_size = float(size_plan.get("min_gross_size") or MIN_BET)
    requested_net_size = float(size_plan.get("target_net_size") or 0)
    expected_net_size = float(size_plan.get("expected_net_size") or 0)

    if size <= 0:
        reason = size_plan.get("skip_reason") or "SIZE_TOO_SMALL"
        print(f"  ⚠️ 风险预算不足，净仓位计划不可执行({reason})，跳过下注")
        return False, 0, 0, "SKIP_SIZE_TOO_SMALL"
    if size_plan.get("forced_to_min"):
        print(
            f"  📏 最小净仓位补齐: raw_net={size_plan['raw_net_size']:.2f}份 "
            f"→ target_net={requested_net_size:.2f}份 | gross={size:.2f}份"
        )

    quoted_price = price
    requested_size = size

    # FOK 深度感知：基于执行前刷新后的最新 asks 规划限价和份数
    fok_price = price
    if sorted_asks_full and size > 0:
        entry_depth = _measure_ask_depth(sorted_asks_full, size, max_price=price_cap)
        print(
            f"  📊 入场流动性: ask_top3={entry_depth['top3_size']:.1f}份 "
            f"| cap内可买={entry_depth['cover_size']:.1f}份 | 限价上限=${price_cap:.2f}"
        )
        plan = _plan_fok_entry(sorted_asks_full, size, price, price_cap, ev, min_gross_size)
        if not plan or plan["mode"] == "skip":
            print(
                f"  ⚠️ cap内 ask 深度不足({entry_depth['cover_size']:.1f}<{min_gross_size:.1f} gross)，跳过下注"
            )
            return False, 0, 0, "SKIP_NO_ASK_DEPTH"
        size = plan["size"]
        fok_price = plan["limit_price"]
        cover_price = plan["coverage"].get("cover_price") or price
        if plan["mode"] == "reduced":
            print(
                f"  ⚠️ ask深度不足: cap内仅{plan['coverage']['cover_size']:.1f}份，"
                f"缩减为gross {size:.2f}份 (预计net {size * (1 - size_plan.get('entry_fee_rate', 0.0)):.2f}份) @ ${fok_price:.2f}"
            )
        elif fok_price > price:
            print(
                f"  📡 FOK执行计划: 覆盖gross {size:.2f}份需到${cover_price:.2f}，"
                f"限价${price:.2f}→${fok_price:.2f} (追价上限=${plan['chase_cap']:.2f})"
            )
        else:
            print(f"  📡 FOK限价+1tick安全余量: ${price:.2f}→${fok_price:.2f}")
    else:
        fok_price = min(round(price + 0.01, 2), price_cap)
        if fok_price > price:
            print(f"  📡 缺少完整 asks，按best_ask+1tick执行: ${price:.2f}→${fok_price:.2f}")

    print(f"  💸 SDK下单: FOK BUY {size}@{fok_price} token={token_id[:16]}...")
    info = clob_client.place_fok_order(token_id, BUY, fok_price, size)
    output = info.get("raw", "")

    # FOK: matched=True 即全部成交，否则全部未成交（不会产生挂单）
    success = info.get("matched", False)
    pending = False
    pending_id = None
    saw_uncertain_fill = False

    executed_limit_price = fok_price

    # FOK 失败重试：刷新盘口，按价格漂移 / 深度不足分流处理
    if not success:
        print(f"  ❌ FOK未成交: Status={info.get('status')} | {info.get('elapsed_ms', 0):.0f}ms")
        explicit_fok_kill = _is_explicit_fok_kill(info)
        if explicit_fok_kill:
            print("  ↻ 明确 FOK kill，跳过链上余额回查")
        else:
            saw_uncertain_fill = saw_uncertain_fill or _is_transport_uncertain_fill(info)
            ghost_balance = _detect_ghost_fill(token_id, expected_size=requested_net_size or size)
            if ghost_balance:
                success = True
                size = ghost_balance

        retry_size = size
        retry_limit = fok_price
        if not success:
            for retry_idx in range(1, 3):
                retry_quote = _get_execution_quote(token_id, force_fresh=True)
                retry_price = retry_quote.get("best_ask")
                retry_asks = retry_quote.get("asks", [])
                retry_age_ms = retry_quote.get("age_ms")
                retry_source = retry_quote.get("source", price_source)

                if not _is_valid_clob_price(retry_price):
                    print(f"  ⚠️ 重试{retry_idx}: 无法刷新到可执行 ask，停止重试")
                    break

                retry_price = round(retry_price, 2)
                if retry_price > price_cap:
                    print(f"  ⚠️ 重试{retry_idx}: ask=${retry_price:.2f} 已高于上限${price_cap:.2f}，停止追价")
                    break

                if retry_asks:
                    retry_plan = _plan_fok_entry(retry_asks, retry_size, retry_price, price_cap, ev, min_gross_size)
                    if not retry_plan or retry_plan["mode"] == "skip":
                        available = _measure_ask_depth(retry_asks, retry_size, max_price=price_cap)
                        print(
                            f"  ⚠️ 重试{retry_idx}: cap内仅剩{available['cover_size']:.1f}份，"
                            f"不足最小{min_gross_size:.1f} gross 份，停止重试"
                        )
                        break
                    next_size = retry_plan["size"]
                    next_limit = retry_plan["limit_price"]
                    reason_bits = []
                    if retry_price > retry_limit:
                        reason_bits.append(f"价格漂移${retry_limit:.2f}→${retry_price:.2f}")
                    if next_size < retry_size:
                        reason_bits.append(f"深度不足{retry_size}→{next_size}份")
                    if not reason_bits:
                        reason_bits.append("刷新盘口重试")
                    age_label = f" | age={retry_age_ms:.0f}ms" if retry_age_ms is not None else ""
                    print(
                        f"  🔄 重试{retry_idx}: {' | '.join(reason_bits)}{age_label} "
                        f"@ ${next_limit:.2f}"
                    )
                else:
                    next_size = retry_size
                    next_limit = min(round(retry_price + 0.01, 2), price_cap)
                    print(
                        f"  🔄 重试{retry_idx}: 仅有 best_ask 快照 ask=${retry_price:.2f} "
                        f"| {next_size}份 @ ${next_limit:.2f}"
                    )

                if next_limit == retry_limit and next_size == retry_size and retry_price <= retry_limit:
                    print(f"  ⚠️ 重试{retry_idx}: 刷新盘口未改善，停止重试")
                    break

                info_retry = clob_client.place_fok_order(token_id, BUY, next_limit, next_size)
                output = info_retry.get("raw", output)
                price_source = retry_source
                snapshot_age_ms = retry_age_ms
                if info_retry.get("matched", False):
                    info = info_retry
                    success = True
                    size = next_size
                    executed_limit_price = next_limit
                    print(f"  ✅ 重试{retry_idx}成交 | {info_retry.get('elapsed_ms', 0):.0f}ms")
                    break

                info = info_retry
                retry_limit = next_limit
                retry_size = next_size
                executed_limit_price = next_limit
                print(f"  ❌ 重试{retry_idx}未成交 | {info_retry.get('elapsed_ms', 0):.0f}ms")

                if _is_explicit_fok_kill(info_retry):
                    print("  ↻ 明确 FOK kill，继续刷新盘口")
                    continue

                saw_uncertain_fill = saw_uncertain_fill or _is_transport_uncertain_fill(info_retry)
                ghost_balance = _detect_ghost_fill(token_id, expected_size=retry_size)
                if ghost_balance:
                    success = True
                    size = ghost_balance
                    break

        if not success and saw_uncertain_fill:
            pending = True
            pending_id = _queue_pending_order(
                slug=slug,
                direction=direction,
                token_id=token_id,
                info=info,
                quoted_price=quoted_price,
                limit_price=executed_limit_price,
                price_source=price_source,
                snapshot_age_ms=snapshot_age_ms,
                requested_size=requested_size,
                requested_net_size=requested_net_size,
                planned_net_size=expected_net_size,
                confidence=confidence,
                ev=ev,
                entry_details=entry_details,
            )
            output = f"PENDING_GHOST:{pending_id}"
            info = dict(info)
            info["status"] = "PENDING"
            print(f"  ⏳ 请求异常后成交状态未确认，已写入 pending_orders 待对账: {pending_id}")

    # 计算实际成交价和实际份数
    actual_size = size
    if success and info.get("making", 0) > 0:
        actual_taking = info.get("taking", 0) or actual_size
        if actual_taking and actual_taking > 0:
            actual_size = round(actual_taking, 4)
            actual_price = round(info["making"] / actual_taking, 4)
        else:
            actual_price = executed_limit_price
        print(
            f"  📊 FOK成交: Making=${info['making']:.4f} | Taking={actual_size} "
            f"| 实际价=${actual_price:.4f} (限价=${executed_limit_price:.2f}) | {info.get('elapsed_ms', 0):.0f}ms"
        )
    else:
        actual_price = executed_limit_price

    gross_entry_price = actual_price
    gross_size = actual_size
    entry_cost = round(float(info.get("making", 0) or (gross_entry_price * gross_size)), 6)
    effective_entry_price = gross_entry_price
    effective_size = gross_size
    buy_fee_shares = 0.0
    buy_fee_usdc = 0.0
    token_balance = None
    if success and gross_size > 0:
        try:
            token_balance = clob_client.get_token_balance(token_id)
        except Exception:
            token_balance = None

        fill_summary = estimate_buy_fill(
            price=gross_entry_price,
            gross_size=gross_size,
            fee_rate_bps=fee_rate_bps,
            gross_cost=entry_cost,
            net_size=token_balance,
        )
        effective_entry_price = fill_summary["effective_entry_price"]
        effective_size = fill_summary["net_size"] or gross_size
        buy_fee_shares = fill_summary["fee_shares"]
        buy_fee_usdc = fill_summary["fee_usdc"]
        if effective_size > 0 and effective_size != gross_size:
            print(
                f"  💳 买入手续费: gross={gross_size:.4f}份 → net={effective_size:.4f}份 "
                f"| fee={buy_fee_shares:.4f}份 (${buy_fee_usdc:.4f})"
            )

    # 记录下注结果（使用含 fee 的净入场价 / 净份数）
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "direction": direction,
        "token_id": token_id,
        "price": effective_entry_price,
        "quoted_price": quoted_price,
        "limit_price": executed_limit_price,
        "price_source": price_source,
        "snapshot_age_ms": snapshot_age_ms,
        "size": effective_size,
        "gross_price": gross_entry_price,
        "gross_size": gross_size,
        "requested_size": requested_size,
        "requested_net_size": requested_net_size,
        "planned_net_size": expected_net_size,
        "amount": round(effective_entry_price * effective_size, 6),
        "gross_amount": entry_cost,
        "fee_rate_bps": fee_rate_bps,
        "buy_fee_shares": buy_fee_shares,
        "buy_fee_usdc": buy_fee_usdc,
        "success": success,
        "pending": pending,
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
            "entry_price": effective_entry_price,
            "size": effective_size,
            "gross_entry_price": gross_entry_price,
            "gross_size": gross_size,
            "requested_net_size": requested_net_size,
            "planned_net_size": expected_net_size,
            "entry_cost": entry_cost,
            "fee_rate_bps": fee_rate_bps,
            "buy_fee_shares": buy_fee_shares,
            "buy_fee_usdc": buy_fee_usdc,
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
        if token_balance and token_balance > 0:
            position["token_balance"] = token_balance
        # allowance刷新异步执行（不阻塞主流程，省~270ms）
        _bet_executor.submit(clob_client.update_token_allowance, token_id)

        with open("logs/positions.jsonl", "a") as f:
            f.write(json.dumps(position) + "\n")

    if pending:
        pending_msg = f"PENDING_GHOST:{pending_id}" if pending_id else f"PENDING_LIVE:{info.get('order_id')}" if info.get("order_id") else "PENDING_LIVE"
        return False, effective_entry_price, effective_size, pending_msg

    return success, effective_entry_price, effective_size, output


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
