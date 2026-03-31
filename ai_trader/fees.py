"""
Polymarket taker fee helpers.

This repo trades crypto markets, so the default category assumption is
"crypto". The exponent can still be overridden via env when Polymarket
changes fee schedules again.

Fee schedule verified 2026-03-31 against docs.polymarket.com/trading/fees:
  Crypto: fee_rate=0.072 (720bps from SDK), exponent=1, peak 1.80% at p=0.50
  Maker: 0 fee + 20% daily rebate (GTD/GTC limit orders)
  Taker: bell-curve fee = C × p × fee_rate × (p×(1-p))^exponent
  Fee at p=0.80: 1.15%, at p=0.90: 0.65%, at p=0.95: 0.34%
"""
import os


_CATEGORY_EXPONENTS = {
    "crypto": 1.0,
    "sports": 1.0,
    "finance": 1.0,
    "politics": 1.0,
    "culture": 1.0,
    "tech": 1.0,
    "economics": 0.5,
    "weather": 0.5,
    "other": 2.0,
    "general": 2.0,
    "mentions": 2.0,
}


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _market_category(market_category=None):
    category = market_category or os.environ.get("POLYMARKET_FEE_CATEGORY") or os.environ.get("POLYMARKET_FEE_MARKET_TYPE") or "crypto"
    return str(category).strip().lower()


def infer_fee_exponent(fee_rate_bps, market_category=None):
    """Infer the exponent in the fee curve.

    For this bot the practical default is crypto:
    - current live crypto fee (`2500` bps base fee) uses exponent 2
    - March 30, 2026+ crypto fee (`720` bps base fee) uses exponent 1
    """
    override = os.environ.get("POLYMARKET_FEE_EXPONENT")
    if override not in (None, ""):
        return _safe_float(override, 1.0)

    fee_rate_bps = _safe_int(fee_rate_bps, 0)
    if fee_rate_bps <= 0:
        return 0.0

    category = _market_category(market_category)
    if category == "crypto":
        return 2.0 if fee_rate_bps >= 2000 else 1.0

    return _CATEGORY_EXPONENTS.get(category, 1.0)


def effective_fee_rate(price, fee_rate_bps, market_category=None):
    """Return the realized taker fee fraction at a given price."""
    price = _safe_float(price)
    fee_rate_bps = _safe_int(fee_rate_bps, 0)
    if price <= 0 or price >= 1 or fee_rate_bps <= 0:
        return 0.0

    exponent = infer_fee_exponent(fee_rate_bps, market_category=market_category)
    base_fee_rate = fee_rate_bps / 10000.0
    return base_fee_rate * ((price * (1.0 - price)) ** exponent)


def round_fee_usdc(fee_usdc):
    rounded = round(_safe_float(fee_usdc), 4)
    return rounded if rounded >= 0.0001 else 0.0


def estimate_buy_fill(price, gross_size, fee_rate_bps, market_category=None, gross_cost=None, net_size=None):
    """Estimate or normalize a buy fill after shares-based taker fees."""
    price = _safe_float(price)
    gross_size = max(_safe_float(gross_size), 0.0)
    gross_cost = _safe_float(gross_cost, price * gross_size)
    if gross_size <= 0 or price <= 0:
        return {
            "gross_size": gross_size,
            "gross_cost": gross_cost,
            "net_size": 0.0,
            "effective_entry_price": price,
            "fee_shares": 0.0,
            "fee_usdc": 0.0,
            "effective_rate": 0.0,
        }

    effective_rate = effective_fee_rate(price, fee_rate_bps, market_category=market_category)
    estimated_fee_usdc = round_fee_usdc(gross_cost * effective_rate)
    estimated_fee_shares = round(estimated_fee_usdc / price, 6) if price > 0 else 0.0

    actual_net_size = _safe_float(net_size)
    if actual_net_size > 0:
        net_size_final = min(gross_size, actual_net_size)
        fee_shares = round(max(gross_size - net_size_final, 0.0), 6)
        fee_usdc = round_fee_usdc(fee_shares * price)
    else:
        fee_shares = estimated_fee_shares
        fee_usdc = estimated_fee_usdc
        net_size_final = round(max(gross_size - fee_shares, 0.0), 6)

    effective_entry_price = round(gross_cost / net_size_final, 6) if net_size_final > 0 else round(price, 6)
    return {
        "gross_size": round(gross_size, 6),
        "gross_cost": round(gross_cost, 6),
        "net_size": round(net_size_final, 6),
        "effective_entry_price": effective_entry_price,
        "fee_shares": fee_shares,
        "fee_usdc": fee_usdc,
        "effective_rate": effective_rate,
    }


def estimate_sell_fill(price, size, fee_rate_bps, market_category=None, gross_proceeds=None):
    """Estimate a sell fill after USDC-based taker fees."""
    price = _safe_float(price)
    size = max(_safe_float(size), 0.0)
    gross_proceeds = _safe_float(gross_proceeds, price * size)
    if size <= 0 or price <= 0:
        return {
            "gross_price": price,
            "gross_proceeds": gross_proceeds,
            "net_price": price,
            "net_proceeds": gross_proceeds,
            "fee_usdc": 0.0,
            "effective_rate": 0.0,
        }

    effective_rate = effective_fee_rate(price, fee_rate_bps, market_category=market_category)
    fee_usdc = round_fee_usdc(gross_proceeds * effective_rate)
    net_proceeds = max(gross_proceeds - fee_usdc, 0.0)
    net_price = round(net_proceeds / size, 6) if size > 0 else round(price, 6)
    return {
        "gross_price": round(price, 6),
        "gross_proceeds": round(gross_proceeds, 6),
        "net_price": net_price,
        "net_proceeds": round(net_proceeds, 6),
        "fee_usdc": fee_usdc,
        "effective_rate": effective_rate,
    }
