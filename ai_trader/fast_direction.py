"""
v14.1 Fast Direction Module — 3-5秒快速方向判断

融合三路信号，在贝叶斯引擎收敛前给出方向预判：
  1. Binance交易流动量 (OFI) — 领先Chainlink 10-30秒
  2. 跨交易所价差领先 — (Binance - Chainlink) / ATR
  3. Polymarket订单簿失衡 (OBI) — 做市商提前调仓信号

输出: direction(UP/DOWN/None), confidence(0-1), 各信号明细
"""
import time


def get_fast_direction(coin, ptb, atr_val, up_token=None, down_token=None):
    """计算快速方向复合信号

    Args:
        coin: 币种 (BTC/ETH/...)
        ptb: Price-to-Beat (结算参考价)
        atr_val: 当前ATR值 (用于归一化)
        up_token: UP token asset_id (用于OBI查询)
        down_token: DOWN token asset_id (用于OBI查询)

    Returns:
        dict: {
            "direction": "UP"|"DOWN"|None,
            "confidence": float 0-1,
            "prior_bias": float 0.35-0.65 (用于设置贝叶斯先验),
            "signals": {各信号明细},
            "reason": str
        }
    """
    if not ptb or not atr_val or atr_val <= 0:
        return _neutral("ptb/atr无效")

    signals = {}
    votes_up = 0.0
    votes_down = 0.0
    total_weight = 0.0

    # ── 信号1: Binance交易流动量 (权重0.40) ──
    W_BINANCE = 0.40
    try:
        from ai_trader.binance_api import BinancePriceStream
        bn = BinancePriceStream()
        momentum = bn.get_tick_momentum(coin, window_sec=10)
        signals["binance_momentum"] = momentum
        if momentum["n_trades"] >= 5:  # 至少5笔交易才有统计意义
            ofi = momentum["ofi"]
            if ofi > 0.10:
                votes_up += W_BINANCE * min(abs(ofi), 1.0)
            elif ofi < -0.10:
                votes_down += W_BINANCE * min(abs(ofi), 1.0)
            total_weight += W_BINANCE
    except Exception:
        pass

    # ── 信号2: 跨交易所价差领先 (权重0.35) ──
    W_CROSS = 0.35
    try:
        from ai_trader.binance_spread import get_spread_snapshot
        spread = get_spread_snapshot(coin, ptb, atr_val, allow_latest_fallback=True)
        if spread:
            # Binance领先Chainlink: 差值 > 0 表示Binance已经涨了但Chainlink还没跟上
            lead = spread["diff_atr_signed"]
            signals["cross_exchange_lead"] = {
                "source": "binance_spread_offset",
                "binance_price": round(spread["binance_price"], 2),
                "ptb": round(ptb, 2),
                "adjusted_ptb": round(spread["adjusted_ptb"], 2),
                "offset": round(spread["offset"], 4),
                "diff": round(spread["diff"], 4),
                "lead_atr": round(lead, 4),
                "offset_method": spread.get("offset_method"),
                "offset_reliable": spread.get("offset_reliable"),
            }
            if abs(lead) > 0.3:  # 至少0.3个ATR的领先才有意义
                if lead > 0:
                    votes_up += W_CROSS * min(abs(lead) / 2.0, 1.0)
                else:
                    votes_down += W_CROSS * min(abs(lead) / 2.0, 1.0)
                total_weight += W_CROSS
    except Exception:
        try:
            from ai_trader.binance_api import BinancePriceStream
            bn = BinancePriceStream()
            binance_price = bn.get_price(coin)
            if binance_price and binance_price > 0:
                lead = (binance_price - ptb) / atr_val
                signals["cross_exchange_lead"] = {
                    "source": "raw_binance_ptb_fallback",
                    "binance_price": round(binance_price, 2),
                    "ptb": round(ptb, 2),
                    "lead_atr": round(lead, 4),
                }
                if abs(lead) > 0.3:
                    if lead > 0:
                        votes_up += W_CROSS * min(abs(lead) / 2.0, 1.0)
                    else:
                        votes_down += W_CROSS * min(abs(lead) / 2.0, 1.0)
                    total_weight += W_CROSS
        except Exception:
            pass

    # ── 信号3: Polymarket订单簿失衡 (权重0.25) ──
    W_OBI = 0.25
    try:
        from ai_trader.polymarket_ws import poly_ws
        # UP token的OBI高 = 买方多 = 看涨
        if up_token:
            obi_up = poly_ws.get_orderbook_imbalance(up_token, depth=5)
            signals["obi_up"] = obi_up
            if obi_up is not None:
                # OBI > 0.6 看涨(UP token被买), < 0.4 看跌(UP token被卖)
                obi_signal = (obi_up - 0.5) * 2  # 归一化到 [-1, 1]
                if abs(obi_signal) > 0.2:
                    if obi_signal > 0:
                        votes_up += W_OBI * min(abs(obi_signal), 1.0)
                    else:
                        votes_down += W_OBI * min(abs(obi_signal), 1.0)
                    total_weight += W_OBI
    except Exception:
        pass

    # ── 融合 ──
    if total_weight < 0.20:
        return _neutral("信号不足")

    net = (votes_up - votes_down) / total_weight if total_weight > 0 else 0
    confidence = min(abs(net) * 1.5, 1.0)  # 放大到[0,1]

    if confidence < 0.15:
        return _neutral_with_signals("信号弱", signals)

    direction = "UP" if net > 0 else "DOWN"
    # 先验偏置: 从0.5偏移, 但不超过 [0.35, 0.65]
    prior_bias = 0.5 + net * 0.15  # 最多偏移0.15
    prior_bias = max(0.35, min(0.65, prior_bias))

    return {
        "direction": direction,
        "confidence": round(confidence, 4),
        "prior_bias": round(prior_bias, 4),
        "signals": signals,
        "reason": f"{direction} net={net:+.3f} weight={total_weight:.2f}",
    }


def _neutral(reason):
    return {
        "direction": None, "confidence": 0, "prior_bias": 0.5,
        "signals": {}, "reason": reason,
    }


def _neutral_with_signals(reason, signals):
    return {
        "direction": None, "confidence": 0, "prior_bias": 0.5,
        "signals": signals, "reason": reason,
    }
