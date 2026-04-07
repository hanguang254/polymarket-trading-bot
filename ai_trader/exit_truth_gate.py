"""
Exit Truth Gate — pre-stop-loss false-positive suppressor.

Suppresses EV stop-losses on positions where any of three layers indicate the
stop is likely a false positive. Built on data analysis of April 6-7 trades
showing 75% of executed full stop-losses were on positions that would have
ended up winning.

Three OR-combined layers (cheapest evaluated first):

  1. P_win floor      — bayesian model itself says we're winning
                        suppress when p_win > EXIT_GATE_PWIN_FLOOR (default 0.75)

  2. Near-expiry      — last N seconds, let the dice land instead of locking loss
                        suppress when remaining_sec < EXIT_GATE_NEAR_EXPIRY_SEC (default 30)

  3. DTG consensus    — three-source cross-validation still confirms direction
                        reuses ai_trader.direction_truth_gate.check_direction_truth()
                        suppress when DTG truth.direction == position direction

The gate sits between the existing CL-BN skew check (position_monitor.py:4567)
and the actual EV exit execution (position_monitor.py:4604). It is a STRICTLY
ADDITIVE layer: it never causes new exits, only suppresses ones the EV gate
would otherwise have triggered.

Disabled-gate behavior follows the DTG pattern (P0-2 lesson):
EXIT_GATE_ENABLED=0 → never suppress, fall through to legacy logic.
"""
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass
class ExitVerdict:
    suppress: bool                  # True = skip this EV exit
    layer: Optional[str]            # which layer fired ("p_win_floor"|"near_expiry"|"dtg_consensus"|None)
    reason: str                     # human-readable
    details: dict                   # debug info for logging


# ──────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _gate_enabled() -> bool:
    return os.environ.get("EXIT_GATE_ENABLED", "1") == "1"


def is_shadow_mode() -> bool:
    """Public helper for callers to honour shadow mode (analogous to DTG)."""
    return os.environ.get("EXIT_GATE_SHADOW", "1") == "1"


def _pwin_floor() -> float:
    return _env_float("EXIT_GATE_PWIN_FLOOR", 0.75)


def _near_expiry_sec() -> float:
    return _env_float("EXIT_GATE_NEAR_EXPIRY_SEC", 30.0)


# ──────────────────────────────────────────────────────────────────────────
# DTG bridge
# ──────────────────────────────────────────────────────────────────────────


def _dtg_says_direction_correct(
    coin: str,
    strike: float,
    atr_val: float,
    direction: str,
    remaining_sec: float,
    now_ts: float,
) -> bool:
    """
    Layer 3 helper: returns True if the Direction Truth Gate's three-source
    consensus still agrees with the position's direction.

    On any error (DTG unavailable, all sources stale, etc.) returns False —
    fail-open, let the EV stop fire as it would without this layer.

    Patched in tests via unittest.mock.
    """
    try:
        from ai_trader.direction_truth_gate import check_direction_truth
        deadline_ts = now_ts + remaining_sec
        truth = check_direction_truth(
            coin=coin, strike=strike, deadline_ts=deadline_ts,
            atr_val=atr_val, strict=False, now_ts=now_ts,
            call_site="exit_gate",
        )
        # Suppress only if DTG produced a definitive direction that MATCHES the position.
        # If DTG is blocked / disabled / direction is None / opposite → don't suppress.
        # P1-1 hardening: defensive check on `direction is None` makes the contract
        # robust to future changes in DTG's disabled-state return shape.
        if truth.is_blocked or truth.direction is None:
            return False
        return truth.direction == direction
    except Exception as e:
        # P2-5: warning level so misconfigured DTG bridge surfaces in production logs
        logger.warning(f"exit_truth_gate: DTG bridge failed (fail-open): {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────
# Public entry
# ──────────────────────────────────────────────────────────────────────────


def should_suppress_ev_exit(
    coin: str,
    strike: float,
    atr_val: float,
    p_win: float,
    direction: str,
    remaining_sec: float,
    *,
    now_ts: Optional[float] = None,
) -> ExitVerdict:
    """
    Decide whether to suppress an EV stop-loss the position monitor is about
    to execute.

    Args:
        coin:           e.g. "BTC", "ETH"
        strike:         contract resolution price (PTB)
        atr_val:        current ATR estimate
        p_win:          bayesian P_win for the position direction (0..1)
        direction:      "UP" or "DOWN" — the position's bet
        remaining_sec:  seconds until contract resolution
        now_ts:         injectable for testing; defaults to time.time()

    Returns:
        ExitVerdict. If suppress=True, the position monitor should skip this
        EV exit cycle. Caller is responsible for honouring shadow mode.
    """
    if not _gate_enabled():
        return ExitVerdict(
            suppress=False, layer=None,
            reason="exit gate disabled",
            details={"gate_enabled": False},
        )

    if now_ts is None:
        now_ts = time.time()

    # Layer 1 — P_win floor (cheapest, always evaluable)
    pwin_floor = _pwin_floor()
    if p_win > pwin_floor:
        return ExitVerdict(
            suppress=True, layer="p_win_floor",
            reason=f"P_win={p_win:.0%} > floor {pwin_floor:.0%}",
            details={"p_win": p_win, "floor": pwin_floor},
        )

    # Layer 2 — Near-expiry lockout
    lockout = _near_expiry_sec()
    if remaining_sec < lockout:
        return ExitVerdict(
            suppress=True, layer="near_expiry",
            reason=f"remaining {remaining_sec:.0f}s < lockout {lockout:.0f}s",
            details={"remaining_sec": remaining_sec, "lockout_sec": lockout},
        )

    # Layer 3 — DTG cross-source consensus still agrees with our direction
    dtg_ok = _dtg_says_direction_correct(
        coin=coin, strike=strike, atr_val=atr_val,
        direction=direction, remaining_sec=remaining_sec, now_ts=now_ts,
    )
    if dtg_ok:
        return ExitVerdict(
            suppress=True, layer="dtg_consensus",
            reason=f"DTG 3-source still confirms {direction}",
            details={"direction": direction},
        )

    # No layer fired — let the EV stop go through
    return ExitVerdict(
        suppress=False, layer=None,
        reason="no suppress layer fired",
        details={"p_win": p_win, "remaining_sec": remaining_sec, "direction": direction},
    )
