"""
GBM closed-form implied probability helper.

P(price_T > strike) under Geometric Brownian Motion approximation, calibrated
against the project's existing ATR convention:

    sigma_per_min = atr / EV_ATR_SIGMA_RATIO   # default 1.5
    sigma_total   = sigma_per_min * sqrt(remaining_seconds / 60)
    z             = |gap| / sigma_total
    base_p        = 0.5 * (1 + erf(z / sqrt(2)))
    p_up          = base_p if gap >= 0 else 1 - base_p

This is the SINGLE SOURCE OF TRUTH for the formula. Both
`bayesian_engine._state_signal` and `direction_truth_gate` call this helper —
keep them in sync by calling here, never by re-deriving.
"""
import math
import os


def _sigma_ratio() -> float:
    """ATR → sigma_per_min ratio. Defaults to 1.5, overridable via env."""
    try:
        return float(os.environ.get("EV_ATR_SIGMA_RATIO", "1.5"))
    except (TypeError, ValueError):
        return 1.5


def gbm_p_up(price: float, strike: float, atr: float, remaining_sec: float) -> float:
    """
    Return P(end_price > strike) given current price, ATR, and time to settlement.

    Edge cases:
      - atr <= 0 or remaining_sec <= 0 → terminal collapse:
          price > strike → 1.0
          price < strike → 0.0
          price == strike → 0.5
      - Any input NaN/inf → 0.5 (no signal, P2-7 fix)
    """
    # P2-7: defensive NaN/Inf guard — corrupt stream data must not poison the gate.
    if not (math.isfinite(price) and math.isfinite(strike)
            and math.isfinite(atr) and math.isfinite(remaining_sec)):
        return 0.5

    gap = price - strike

    if atr <= 0 or remaining_sec <= 0:
        if gap > 0:
            return 1.0
        if gap < 0:
            return 0.0
        return 0.5

    sigma_per_min = atr / _sigma_ratio()
    sigma_total = sigma_per_min * math.sqrt(remaining_sec / 60.0)
    if sigma_total <= 0:
        if gap > 0:
            return 1.0
        if gap < 0:
            return 0.0
        return 0.5

    z = abs(gap) / sigma_total
    base_p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return base_p if gap >= 0 else (1.0 - base_p)
