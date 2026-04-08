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


import os
import time as _time_module


# ─────────── Source accessors (seam for mocking) ───────────

def _get_chainlink_snapshot(coin: str) -> Optional[Dict[str, Any]]:
    """Thin wrapper over chainlink_stream.get_snapshot so tests can mock at module level."""
    try:
        from ai_trader.polymarket_rtds import chainlink_stream
        return chainlink_stream.get_snapshot(coin)
    except Exception:
        return None


def _get_binance_delta(coin: str, window_sec: int) -> Dict[str, Any]:
    """Thin wrapper over binance_api.get_price_delta so tests can mock at module level."""
    try:
        from ai_trader.binance_api import get_price_delta
        return get_price_delta(coin, window_sec)
    except Exception:
        return {"delta_bps": 0.0, "n_trades": 0, "stale": True, "direction": "FLAT"}


# ─────────── Config loader with fallbacks ───────────

def _env_str(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read an env var, treating whitespace-only as unset (I7 fix)."""
    v = os.environ.get(key, default)
    if v is None:
        return default
    v = str(v).strip()
    return v if v else default


def _env_float(key: str, default: str) -> float:
    return float(_env_str(key, default) or default)


def _env_int(key: str, default: str) -> int:
    return int(float(_env_str(key, default) or default))


def _env_bool(key: str, default: str) -> bool:
    return (_env_str(key, default) or default) == "1"


def _get_oracle_config() -> Dict[str, Any]:
    """Read all ORACLE_SNIPER_* env vars with defaults. Called per check (cheap).

    Falls back to global MIN_BET_SIZE / MAX_BET_SIZE when ORACLE_SNIPER_MIN/MAX_BET_SIZE
    are unset or whitespace (I7).
    """
    return {
        "enabled": _env_bool("ORACLE_SNIPER_ENABLED", "1"),
        "live": _env_bool("ORACLE_SNIPER_LIVE", "0"),
        "tail_sec": _env_float("ORACLE_SNIPER_TAIL_SEC", "30"),
        "taker_switch_sec": _env_float("ORACLE_SNIPER_TAKER_SWITCH_SEC", "15"),
        "p_up_threshold": _env_float("ORACLE_SNIPER_P_UP_THRESHOLD", "0.93"),
        "cl_stale_sec": _env_float("ORACLE_SNIPER_CL_STALE_SEC", "1.0"),
        "bn_window_sec": _env_int("ORACLE_SNIPER_BN_WINDOW_SEC", "15"),
        "bn_reverse_bps": _env_float("ORACLE_SNIPER_BN_REVERSE_BPS", "2"),
        "cooldown_sec": _env_float("ORACLE_SNIPER_COOLDOWN_SEC", "5"),
        "maker_edge": _env_float("ORACLE_SNIPER_MAKER_EDGE", "0.012"),
        "too_late_sec": _env_float("ORACLE_SNIPER_TOO_LATE_SEC", "1.0"),
        "min_bet_size": float(
            _env_str("ORACLE_SNIPER_MIN_BET_SIZE") or _env_str("MIN_BET_SIZE", "2") or "2"
        ),
        "max_bet_size": float(
            _env_str("ORACLE_SNIPER_MAX_BET_SIZE") or _env_str("MAX_BET_SIZE", "3") or "3"
        ),
    }


# ─────────── Main entry ───────────

def check_oracle_sniper(
    coin: str,
    strike: float,
    atr: float,
    deadline_ts: float,
    now_ts: Optional[float] = None,
) -> OracleVerdict:
    """Run the Oracle Sniper validation pipeline and return an OracleVerdict.

    Pipeline (fail-fast):
      1. Cooldown check     → REJECT(COOLDOWN)
      2. Window check       → REJECT(OUT_OF_WINDOW)
      3. Chainlink freshness → REJECT(CL_MISSING / CL_STALE)
      4. Confidence compute  → REJECT(LOW_CONFIDENCE / COMPUTE_ERROR)
      5. Binance reversal    → REJECT(BN_CONTRADICT)
      6. Phase routing       → BUY (MAKER or TAKER)

    Does NOT place any orders — returns a verdict for the caller to act on.
    Does NOT record cooldown — that fires only on confirmed OPEN/FILLED in phase handlers.
    """
    cfg = _get_oracle_config()
    if now_ts is None:
        now_ts = _time_module.time()

    remaining = deadline_ts - now_ts
    base_details: Dict[str, Any] = {
        "coin": coin,
        "strike": strike,
        "atr": atr,
        "remaining": round(remaining, 3),
        "deadline_ts": deadline_ts,
    }

    # ── 1. Cooldown ─────────────────────────────────────────────
    ok, reason = _cooldown_check(coin, now_ts, cfg["cooldown_sec"])
    if not ok:
        return OracleVerdict(
            action="REJECT", direction=None, p_up=None, phase=None,
            reason=reason or "COOLDOWN", details=dict(base_details), ts=now_ts,
        )

    # ── 2. Window (0, tail_sec] ─────────────────────────────────
    if not (0 < remaining <= cfg["tail_sec"]):
        return OracleVerdict(
            action="REJECT", direction=None, p_up=None, phase=None,
            reason="OUT_OF_WINDOW", details=dict(base_details), ts=now_ts,
        )

    # ── 2b. TOO_LATE guard ──────────────────────────────────────
    if remaining < cfg["too_late_sec"]:
        return OracleVerdict(
            action="REJECT", direction=None, p_up=None, phase=None,
            reason="TOO_LATE", details=dict(base_details), ts=now_ts,
        )

    # ── 3. Chainlink freshness ──────────────────────────────────
    cl_snapshot = _get_chainlink_snapshot(coin)
    ok, reason, cl_details = _chainlink_freshness_check(cl_snapshot, cfg["cl_stale_sec"])
    base_details.update(cl_details)
    if not ok:
        return OracleVerdict(
            action="REJECT", direction=None, p_up=None, phase=None,
            reason=reason or "CL_MISSING", details=dict(base_details), ts=now_ts,
        )

    # ── 4. Confidence ───────────────────────────────────────────
    conf = _compute_confidence(
        price=cl_details["cl_price"],
        strike=strike,
        atr=atr,
        remaining_sec=remaining,
        threshold=cfg["p_up_threshold"],
    )
    if conf["reason"] is not None:
        return OracleVerdict(
            action="REJECT", direction=None, p_up=conf["p_up"], phase=None,
            reason=conf["reason"], details=dict(base_details), ts=now_ts,
        )
    direction = conf["direction"]
    p_up = conf["p_up"]
    base_details["p_up"] = round(p_up, 6)

    # ── 5. Binance reversal ─────────────────────────────────────
    bn_delta = _get_binance_delta(coin, cfg["bn_window_sec"])
    base_details["bn_delta_bps"] = bn_delta.get("delta_bps", 0.0)
    base_details["bn_n_trades"] = bn_delta.get("n_trades", 0)
    ok, reason, warn = _binance_reversal_check(bn_delta, direction, cfg["bn_reverse_bps"])
    if warn:
        base_details["bn_sparse_warn"] = True
    if not ok:
        return OracleVerdict(
            action="REJECT", direction=direction, p_up=p_up, phase=None,
            reason=reason or "BN_CONTRADICT", details=dict(base_details), ts=now_ts,
        )

    # ── 6. Phase routing ────────────────────────────────────────
    phase: Literal["MAKER", "TAKER"] = (
        "MAKER" if remaining > cfg["taker_switch_sec"] else "TAKER"
    )
    # NOTE: cooldown is NOT recorded here. It fires only on confirmed
    # order placement success inside _phase_maker (OPEN) or _phase_taker
    # (FILLED). A passed verdict that later fails at CLOB must not
    # burn the 5s cooldown budget (C6 fix).
    action: Literal["BUY", "SHADOW"] = "BUY" if cfg["live"] else "SHADOW"
    return OracleVerdict(
        action=action, direction=direction, p_up=p_up, phase=phase,
        reason="OK", details=dict(base_details), ts=now_ts,
    )
