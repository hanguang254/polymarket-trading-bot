"""
AI 评分模型 v2.1 - 折价套利策略

核心发现（基于889条历史数据验证）：
  - 5分钟市场结束时，价格回归PTB附近（偏离缩小91.7%）
  - 74.5%的市场结束时偏离<0.01%，方向预测几乎无效
  - 真正的盈利来源：T+60s买入折价token → T+180s提前平仓

新策略：
  1. 不再预测"结束时谁赢"，而是判断"当前token是否被低估"
  2. 核心指标：token实际价值 vs 市场赔率的差值（折价空间）
  3. 只在折价空间足够大时下注，通过提前平仓锁定利润
  4. 动量信号用于判断"偏离是否还在扩大"（扩大=更好的平仓机会）

盈利逻辑：
  T+60s  价格偏离PTB → token实际价值>赔率 → 买入
  T+120s token价格反映偏离 → 卖出（不等结算）
  利润 = 卖出价 - 买入价（与结算结果无关）
"""
from ai_trader.binance_api import get_klines
from ai_trader.indicators import ema, rsi, atr


# v12: estimated_value 连续插值表（替代离散 if-elif）
# 节点: (ATR倍数, token估值) — 节点之间线性插值
_EV_KNOTS = [
    (0.5, 0.51),
    (0.7, 0.55),
    (1.0, 0.58),
    (1.5, 0.63),
    (2.0, 0.69),
    (3.0, 0.75),
    (4.0, 0.83),
    (6.0, 0.88),
]


def _interpolate_estimated_value(diff_in_atr):
    """ATR偏离 → token估值，线性插值消除离散跳变"""
    if diff_in_atr <= _EV_KNOTS[0][0]:
        return 0.50
    if diff_in_atr >= _EV_KNOTS[-1][0]:
        return _EV_KNOTS[-1][1]
    for i in range(1, len(_EV_KNOTS)):
        if diff_in_atr <= _EV_KNOTS[i][0]:
            x0, y0 = _EV_KNOTS[i - 1]
            x1, y1 = _EV_KNOTS[i]
            return y0 + (y1 - y0) * (diff_in_atr - x0) / (x1 - x0)
    return _EV_KNOTS[-1][1]


def analyze_5m_trend(coin, price_to_beat):
    """
    5分钟K线趋势分析 — 与5分钟市场同频的趋势过滤

    返回: dict
      trend_dir: "UP" / "DOWN" / None (无明确趋势)
      trend_strength: 0.0-1.0 (趋势强度)
      momentum_5m: 最近2根5m K线涨跌幅之和
      ptb_position: PTB在近期高低点中的相对位置 (0=最低, 1=最高)
    """
    symbol = f"{coin}USDT"
    klines = get_klines(symbol, "5m", 6)

    result = {"trend_dir": None, "trend_strength": 0.0, "momentum_5m": 0.0, "ptb_position": 0.5}

    if not klines or len(klines) < 4:
        return result

    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]

    # ── 信号1: 最近3根5m K线的方向 ──
    recent_3 = closes[-3:]
    up_count = sum(1 for i in range(1, len(recent_3)) if recent_3[i] > recent_3[i-1])
    down_count = sum(1 for i in range(1, len(recent_3)) if recent_3[i] < recent_3[i-1])

    if up_count >= 2:
        result["trend_dir"] = "UP"
        result["trend_strength"] = 0.7 if up_count == 2 else 1.0
    elif down_count >= 2:
        result["trend_dir"] = "DOWN"
        result["trend_strength"] = 0.7 if down_count == 2 else 1.0

    # ── 信号2: 最近2根5m K线的动量 ──
    if len(closes) >= 3 and closes[-3] > 0:
        pct1 = (closes[-2] - closes[-3]) / closes[-3] * 100
        pct2 = (closes[-1] - closes[-2]) / closes[-2] * 100
        result["momentum_5m"] = round(pct1 + pct2, 4)

    # ── 信号3: PTB在近4根5m高低点中的位置 ──
    recent_highs = highs[-4:]
    recent_lows = lows[-4:]
    range_high = max(recent_highs)
    range_low = min(recent_lows)
    if range_high > range_low and price_to_beat:
        position = (price_to_beat - range_low) / (range_high - range_low)
        result["ptb_position"] = round(max(0, min(1, position)), 3)

    return result


def analyze_market(coin, price_to_beat, up_odds, down_odds):
    """
    v2.1 折价套利分析

    返回: (direction, confidence, details)
    """
    # ── 数据采集（30根K线，更稳定的ATR估算） ──
    symbol = f"{coin}USDT"
    klines_1m = get_klines(symbol, "1m", 30)
    # 决策时价格：优先 Chainlink（与PTB/结算同源），fallback Pyth
    from ai_trader.price_oracle import get_onchain_price
    current_price = get_onchain_price(coin)

    if not klines_1m or not current_price or not price_to_beat:
        return None, 0, {"error": "数据不足"}

    closes = [k["close"] for k in klines_1m]
    highs = [k["high"] for k in klines_1m]
    lows = [k["low"] for k in klines_1m]
    volumes = [k["volume"] for k in klines_1m]

    details = {
        "current_price": current_price,
        "price_to_beat": price_to_beat,
    }

    # ── ATR计算 ──
    atr_val = atr(highs, lows, closes, min(14, len(highs) - 1))
    if not atr_val or atr_val <= 0:
        atr_val = abs(current_price * 0.001)

    # ═══════════════════════════════════════
    # 核心指标1：价格偏离度（决定方向和token价值）
    # ═══════════════════════════════════════
    price_diff = current_price - price_to_beat
    price_diff_pct = (price_diff / price_to_beat) * 100
    diff_in_atr = abs(price_diff) / atr_val

    details["price_diff"] = round(price_diff, 2)
    details["price_diff_pct"] = round(price_diff_pct, 4)
    details["diff_in_atr"] = round(diff_in_atr, 2)
    details["atr"] = round(atr_val, 2)

    # 方向：价格>=PTB → UP领先，价格<PTB → DOWN领先（零值不偏向DOWN）
    if price_diff >= 0:
        direction = "UP"
        leading_odds = up_odds      # 领先方的赔率（我们要买的）
        trailing_odds = down_odds
    else:
        direction = "DOWN"
        leading_odds = down_odds
        trailing_odds = up_odds

    details["direction"] = direction
    details["leading_odds"] = leading_odds

    # ═══════════════════════════════════════
    # 核心指标2：折价空间（最重要的指标）
    # ═══════════════════════════════════════
    # token实际价值估算：基于当前偏离度（v12: 连续插值替代离散表）
    # 偏离越大 → 领先方token价值越接近1.0
    # 但5分钟市场会均值回归，所以打折
    estimated_value = _interpolate_estimated_value(diff_in_atr)

    # 折价空间 = 估算价值 - 买入赔率
    # 例：estimated_value=0.80, leading_odds=0.65 → 折价=0.15（15%利润空间）
    discount = estimated_value - leading_odds
    discount_pct = discount / leading_odds * 100 if leading_odds > 0 else 0

    details["estimated_value"] = round(estimated_value, 3)
    details["discount"] = round(discount, 3)
    details["discount_pct"] = round(discount_pct, 1)

    # Base Rate 校准的概率估计（保守先验，比 estimated_value 更谨慎）
    from ai_trader.base_rate import get_base_rate
    details["p_win"] = round(get_base_rate(diff_in_atr), 4)

    # ═══════════════════════════════════════
    # 辅助指标：动量（判断偏离是否在扩大）
    # ═══════════════════════════════════════
    momentum_bonus = 0
    if len(closes) >= 4:
        recent_change = closes[-1] - closes[-4]
        micro_change = closes[-1] - closes[-2]

        # 动量方向与偏离方向一致 = 偏离在扩大 = 更好
        momentum_dir = 1 if recent_change > 0 else -1
        position_dir = 1 if price_diff >= 0 else -1

        if momentum_dir == position_dir:
            # 动量确认偏离方向，加分
            momentum_bonus = 10
            details["momentum"] = "confirming"
        else:
            # 动量反向，偏离可能在缩小，减分
            momentum_bonus = -5
            details["momentum"] = "reversing"

        # 微观动量（最近1分钟）额外确认
        micro_dir = 1 if micro_change > 0 else -1
        if micro_dir == position_dir:
            momentum_bonus += 5
            details["micro_momentum"] = "confirming"
        else:
            momentum_bonus -= 3
            details["micro_momentum"] = "reversing"

        details["recent_3m_pct"] = round((recent_change / closes[-4]) * 100, 4)
        details["micro_1m_pct"] = round((micro_change / closes[-2]) * 100, 4)

    details["momentum_bonus"] = momentum_bonus

    # ═══════════════════════════════════════
    # 辅助指标：5分钟趋势过滤（与市场周期同频）
    # ═══════════════════════════════════════
    trend_5m = analyze_5m_trend(coin, price_to_beat)
    details["trend_5m"] = trend_5m["trend_dir"]
    details["trend_5m_strength"] = trend_5m["trend_strength"]
    details["momentum_5m"] = trend_5m["momentum_5m"]
    details["ptb_position"] = trend_5m["ptb_position"]

    # 5m趋势与1m方向的一致性调整
    trend_5m_bonus = 0
    if trend_5m["trend_dir"]:
        if trend_5m["trend_dir"] == direction:
            # 共振：5m趋势和1m方向一致 → 加分
            trend_5m_bonus = 10
            details["trend_15m_alignment"] = "confirming"
        else:
            # 矛盾：5m趋势反向 → 减分
            trend_5m_bonus = -15
            details["trend_15m_alignment"] = "conflicting"
    else:
        details["trend_15m_alignment"] = "neutral"

    momentum_bonus += trend_5m_bonus
    details["trend_5m_bonus"] = trend_5m_bonus

    # ═══════════════════════════════════════
    # 辅助指标：成交量（放量=趋势延续）
    # ═══════════════════════════════════════
    vol_bonus = 0
    if len(volumes) >= 5:
        avg_vol = sum(volumes[-5:]) / 5
        curr_vol = volumes[-1]
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1
        details["vol_ratio"] = round(vol_ratio, 2)
        if vol_ratio > 2.0:
            vol_bonus = 5
        elif vol_ratio > 1.5:
            vol_bonus = 3

    # ═══════════════════════════════════════
    # 综合评分
    # ═══════════════════════════════════════
    # 核心：折价空间（0-60分）
    if discount >= 0.20:
        discount_score = 60  # 20%+折价，极好
    elif discount >= 0.15:
        discount_score = 50
    elif discount >= 0.10:
        discount_score = 40
    elif discount >= 0.05:
        discount_score = 25
    elif discount >= 0.02:
        discount_score = 15
    else:
        discount_score = 0   # 没有折价，不值得

    total_score = discount_score + momentum_bonus + vol_bonus
    total_score = max(total_score, 0)

    # 置信度 = 总分/80
    confidence = min(max(total_score / 80, 0), 1.0)

    details["discount_score"] = discount_score
    details["vol_bonus"] = vol_bonus
    details["total_score"] = round(total_score, 1)
    details["confidence"] = round(confidence, 3)

    # ═══════════════════════════════════════
    # 期望值（基于折价而非方向预测）
    # ═══════════════════════════════════════
    # EV = 折价空间 / 买入价（预期收益率）
    if leading_odds > 0:
        ev = discount / leading_odds
        details["discount_ratio"] = round(ev, 3)
        details["discount_ratio_positive"] = ev > 0
        details["target_odds"] = leading_odds
    else:
        details["expected_value"] = 0
        details["ev_positive"] = False

    return direction, confidence, details
