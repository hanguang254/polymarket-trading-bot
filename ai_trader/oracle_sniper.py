# ai_trader/oracle_sniper.py
"""
Oracle Sniper (v14.4) — Tail-segment high-confidence trading channel.

Activates in the final 30 seconds before contract deadline. Uses Chainlink
as the primary directional signal (gbm_p_up > 0.93 or < 0.07) with Binance
15-second trend as a reversal cross-validation filter.

Order placement runs a two-phase state machine:
  - remaining ∈ (15, 30]: MAKER phase (GTD ambush order, reuses SNIPER_AMBUSH_EDGE pricing)
  - remaining ∈ (0, 15]: TAKER phase (IOC fill, relies on low fee at extreme prices)

This module is INDEPENDENT of direction_truth_gate; it runs its own Chainlink +
Binance two-source pipeline. See docs/superpowers/specs/2026-04-08-oracle-sniper-design.md
for the full design document.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

from ai_trader.gbm_p_up import gbm_p_up


@dataclass
class OracleVerdict:
    """Result of a single check_oracle_sniper() invocation.

    action:
        "BUY"    → downstream should place an order for `direction` in `phase`
        "REJECT" → gate blocked entry; `reason` explains why
        "SHADOW" → shadow mode active; check passed but no order placed
    direction:
        "UP" / "DOWN" / None (only set when action != REJECT)
    p_up:
        Computed gbm_p_up value at check time (for logging/audit)
    phase:
        "MAKER" / "TAKER" / None (routing hint for order placement)
    reason:
        One of: OK, OUT_OF_WINDOW, CL_STALE, CL_MISSING, LOW_CONFIDENCE,
        BN_CONTRADICT, BN_MISSING, COOLDOWN, COMPUTE_ERROR
    details:
        Free-form audit dict (cl_price, bn_delta_bps, remaining, etc.)
    ts:
        Unix timestamp of the check
    """

    action: Literal["BUY", "REJECT", "SHADOW"]
    direction: Optional[Literal["UP", "DOWN"]]
    p_up: Optional[float]
    phase: Optional[Literal["MAKER", "TAKER"]]
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0


# ─────────── Per-coin cooldown state ───────────
# Module-level dict, process-local. Keyed by coin symbol.
# Guarded by _cooldown_lock because _cooldown_check is called from both
# the main loop and the sniper thread.
_cooldown_lock = threading.Lock()
_last_trigger_ts: Dict[str, float] = {}


def _reset_cooldown_state() -> None:
    """Test helper: clear all cooldown state."""
    with _cooldown_lock:
        _last_trigger_ts.clear()


def _record_cooldown(coin: str, now_ts: float) -> None:
    """Record the timestamp of a successful (non-rejected) trigger.

    Called ONLY from _phase_maker (on OPEN) and _phase_taker (on FILLED).
    NOT called from check_oracle_sniper — a passed verdict that later fails
    at the CLOB step must not burn the 5s cooldown budget.
    """
    with _cooldown_lock:
        _last_trigger_ts[coin] = now_ts


def _cooldown_check(coin: str, now_ts: float, cooldown_sec: float) -> tuple[bool, Optional[str]]:
    """Return (ok, reason). ok=True means proceed; reason='COOLDOWN' means blocked."""
    with _cooldown_lock:
        last = _last_trigger_ts.get(coin)
    if last is None:
        return True, None
    if now_ts - last >= cooldown_sec:
        return True, None
    return False, "COOLDOWN"


def _chainlink_freshness_check(
    snapshot: Optional[Dict[str, Any]],
    max_age_sec: float,
) -> tuple[bool, Optional[str], Dict[str, Any]]:
    """Return (ok, reason, details).

    Accepts None (CL_MISSING), stale snapshots (CL_STALE), or fresh (ok=True).
    Snapshot shape mirrors `chainlink_stream.get_snapshot`:
        {"price": float, "age_ms": float, "stale": bool, ...}
    """
    if snapshot is None:
        return False, "CL_MISSING", {}
    price = snapshot.get("price")
    age_ms = snapshot.get("age_ms", 0) or 0
    if price is None or price <= 0:
        return False, "CL_MISSING", {}
    if snapshot.get("stale"):
        return False, "CL_STALE", {"cl_price": price, "age_ms": age_ms}
    if age_ms > max_age_sec * 1000.0:
        return False, "CL_STALE", {"cl_price": price, "age_ms": age_ms}
    return True, None, {"cl_price": price, "age_ms": age_ms}


def _compute_confidence(
    price: float,
    strike: float,
    atr: float,
    remaining_sec: float,
    threshold: float,
) -> Dict[str, Any]:
    """Compute direction based on gbm_p_up threshold.

    Returns a dict with:
        p_up     (float)
        direction ("UP" | "DOWN" | None)
        reason   ("LOW_CONFIDENCE" | None)
    """
    try:
        p_up = gbm_p_up(price=price, strike=strike, atr=atr, remaining_sec=remaining_sec)
    except Exception:
        return {"p_up": None, "direction": None, "reason": "COMPUTE_ERROR"}

    lower = 1.0 - threshold
    if p_up > threshold:
        return {"p_up": p_up, "direction": "UP", "reason": None}
    if p_up < lower:
        return {"p_up": p_up, "direction": "DOWN", "reason": None}
    return {"p_up": p_up, "direction": None, "reason": "LOW_CONFIDENCE"}


def _binance_reversal_check(
    bn_delta: Dict[str, Any],
    direction: Literal["UP", "DOWN"],
    reverse_bps_threshold: float,
) -> tuple[bool, Optional[str], bool]:
    """Return (ok, reason, warn).

    `warn=True` means the sparse-window fallback fired (allowed, but log a warning).

    Logic:
        - If BN window is sparse (n_trades=0 or stale) → allow with warn=True
        - If BN moved > reverse_bps_threshold in OPPOSITE direction → block (BN_CONTRADICT)
        - Otherwise → allow
    """
    n_trades = bn_delta.get("n_trades", 0)
    stale = bn_delta.get("stale", False)

    if n_trades == 0 or stale:
        return True, None, True

    delta_bps = bn_delta.get("delta_bps", 0.0)

    if direction == "UP" and delta_bps < -reverse_bps_threshold:
        return False, "BN_CONTRADICT", False
    if direction == "DOWN" and delta_bps > reverse_bps_threshold:
        return False, "BN_CONTRADICT", False
    return True, None, False
