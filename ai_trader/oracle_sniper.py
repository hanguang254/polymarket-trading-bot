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

import json as _json
import os
import threading
import time as _time_module
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
      1. Cooldown check      → REJECT(COOLDOWN)
      2. Window check        → REJECT(OUT_OF_WINDOW)
      2b. TOO_LATE guard     → REJECT(TOO_LATE)
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


# ─────────── Log writer ───────────

# ─────────── Log dedup state ───────────
# Per-coin "last REJECT reason" map. On state change (new REJECT reason OR
# any BUY/SHADOW action), one log line is written and the dedup clears.
# BUY / SHADOW actions ALWAYS write (never deduped).
_log_lock = threading.Lock()
_log_last_reason_by_coin: Dict[str, str] = {}

# All REJECT reasons dedup — not just COOLDOWN/OUT_OF_WINDOW (I1 fix).
# The reason for dedup is log-volume containment during the 30s tail window
# which can be polled at 50ms cadence.
_DEDUP_ACTIONS = {"REJECT"}


def _reset_log_dedup_state() -> None:
    """Test helper."""
    with _log_lock:
        _log_last_reason_by_coin.clear()


def _log_path() -> str:
    return _env_str("ORACLE_SNIPER_LOG_PATH", "logs/oracle_sniper.jsonl") or "logs/oracle_sniper.jsonl"


def log_oracle_verdict(v: OracleVerdict) -> None:
    """Write a verdict as a JSONL record.

    Dedup semantics (I1 + M8 fix):
      - REJECT verdicts are deduped per-coin: only write if the reason
        differs from the previous REJECT for that coin.
      - BUY / SHADOW verdicts ALWAYS write and clear the per-coin dedup
        state so the next REJECT (if any) writes once.
    """
    coin = v.details.get("coin", "?")

    with _log_lock:
        last_reason = _log_last_reason_by_coin.get(coin)
        if v.action in _DEDUP_ACTIONS:
            if last_reason == v.reason:
                return  # same REJECT reason, silent
            _log_last_reason_by_coin[coin] = v.reason
        else:
            # BUY / SHADOW → always write, clear per-coin dedup state
            _log_last_reason_by_coin.pop(coin, None)

    record = {
        "ts": v.ts,
        "action": v.action,
        "direction": v.direction,
        "p_up": v.p_up,
        "phase": v.phase,
        "reason": v.reason,
        "details": v.details,
    }
    path = _log_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(_json.dumps(record, ensure_ascii=False) + "\n")


# ─────────── Per-coin state machine state ───────────
# Holds active order info keyed by coin. Guarded by _orders_lock because
# the main loop and sniper thread both touch it (I2 fix).
# Persisted to disk on every change so cleanup_orphan_orders can recover
# across process restarts (C2 fix).
_orders_lock = threading.Lock()
_active_orders: Dict[str, Dict[str, Any]] = {}


def _orders_state_path() -> str:
    return _env_str("ORACLE_SNIPER_STATE_PATH", "logs/oracle_active_orders.json") \
        or "logs/oracle_active_orders.json"


def _persist_active_orders() -> None:
    """Write _active_orders to disk. Caller must already hold _orders_lock."""
    path = _orders_state_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(_active_orders, f)
        os.replace(tmp, path)
    except Exception:
        pass  # persistence is best-effort; in-memory state is source of truth


def _load_persisted_active_orders() -> Dict[str, Dict[str, Any]]:
    """Read persisted active orders from disk. Returns {} on any error."""
    path = _orders_state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _reset_oracle_state() -> None:
    """Test helper: clear all per-coin state (cooldown, active orders, dedup)."""
    _reset_cooldown_state()
    with _orders_lock:
        _active_orders.clear()
        _persist_active_orders()
    _reset_log_dedup_state()


def _compute_kelly_size(balance: float, p_win: float, ask_price: float,
                        min_bet: float, max_bet: float) -> float:
    """Reuse the same Kelly 1/4 formula as endgame/ambush.

    Returns gross size in shares (already divided by 0.975 for taker fee grossing).

    NOTE: callers must pass the actual best ask (not a maker-discounted price)
    so Kelly sees the real fill price — otherwise f_star is biased upward (C5 fix).
    """
    kr = 1.0
    f_star = (p_win - ask_price) / max(1.0 - ask_price, 1e-6)
    if f_star <= 0:
        return 0.0
    f_quarter = f_star / 4.0 * kr
    dollar = balance * f_quarter
    net_size = round(dollar / ask_price, 1) if ask_price > 0 else 0
    net_size = max(min_bet, min(max_bet, net_size))
    return round(net_size / 0.975, 2)


def _phase_maker(
    verdict: OracleVerdict,
    token: str,
    ask_price: float,
    balance: float,
    deadline_ts: float,
    clob_client: Any,
) -> Dict[str, Any]:
    """Place a GTD maker order at (ask_price - maker_edge).

    Real CLOB signature (C1 fix):
        clob_client.place_order(
            token_id, side, price, size,
            order_type=OrderType.GTD,       # enum from py_clob_client.clob_types
            expiration=unix_ts,              # Polymarket requires now + 60 + desired_lifetime
        )

    No `client_order_id` kwarg exists in the real API (C1 fix). Oracle orders
    are tracked by the server-returned order_id in _active_orders[coin] and
    persisted to disk for orphan recovery (C2 fix).

    Returns:
        {"status": "OPEN" | "ABORTED", "order_id": str, "buy_price": float,
         "size": float, "reason": str}
    """
    from py_clob_client.clob_types import OrderType as _OT
    from py_clob_client.order_builder.constants import BUY as _BUY

    cfg = _get_oracle_config()
    coin = verdict.details.get("coin", "?")

    # Per-coin lock: reject if there's already an active order for this coin.
    # This is the *real* per-coin mutual exclusion, protected by _orders_lock (I2).
    with _orders_lock:
        if coin in _active_orders:
            return {
                "status": "ABORTED",
                "order_id": None,
                "buy_price": 0.0,
                "size": 0.0,
                "reason": "COIN_LOCKED: active order exists",
            }
        # Reserve the slot immediately so a concurrent caller can't race in.
        _active_orders[coin] = {
            "order_id": None,
            "phase": "RESERVED",
            "opened_ts": verdict.ts,
        }
        _persist_active_orders()

    try:
        # Upper-bound tick guard (M3 fix): Polymarket rejects <=0.01 or >=0.99
        if ask_price <= 0.01 or ask_price >= 0.99:
            with _orders_lock:
                _active_orders.pop(coin, None)
                _persist_active_orders()
            return {
                "status": "ABORTED", "order_id": None,
                "buy_price": 0.0, "size": 0.0,
                "reason": f"ASK_OUT_OF_RANGE: {ask_price}",
            }

        # Price below ask by maker_edge (ambush-style), clamped to valid tick range
        buy_price = max(0.02, min(0.98, round(ask_price - cfg["maker_edge"], 2)))

        # C5 fix: Kelly sizing uses the REAL ask (not the maker-discounted price).
        size = _compute_kelly_size(
            balance=balance,
            p_win=verdict.p_up or 0.0,
            ask_price=ask_price,
            min_bet=cfg["min_bet_size"],
            max_bet=cfg["max_bet_size"],
        )
        if size <= 0:
            with _orders_lock:
                _active_orders.pop(coin, None)
                _persist_active_orders()
            return {
                "status": "ABORTED", "order_id": None,
                "buy_price": buy_price, "size": 0.0,
                "reason": "KELLY_ZERO: f_star non-positive",
            }

        # CRIT-2 fix: GTD expiry = max(deadline_ts - 5, now + 60)
        now_ts = _time_module.time()
        target_expiry = int(deadline_ts) - 5
        gtd_expiry = max(target_expiry, int(now_ts) + 60)

        try:
            result = clob_client.place_order(
                token, _BUY, buy_price, size,
                order_type=_OT.GTD,
                expiration=gtd_expiry,
            )
        except Exception as exc:
            with _orders_lock:
                _active_orders.pop(coin, None)
                _persist_active_orders()
            return {
                "status": "ABORTED", "order_id": None,
                "buy_price": buy_price, "size": size,
                "reason": f"CLOB_EXCEPTION: {exc}",
            }

        if not result or not result.get("success"):
            err = (result or {}).get("status") or (result or {}).get("error", "unknown")
            with _orders_lock:
                _active_orders.pop(coin, None)
                _persist_active_orders()
            return {
                "status": "ABORTED", "order_id": None,
                "buy_price": buy_price, "size": size,
                "reason": f"CLOB_REJECT: {err}",
            }

        order_id = result.get("order_id") or "?"
        with _orders_lock:
            _active_orders[coin] = {
                "order_id": order_id,
                "phase": "MAKER",
                "buy_price": buy_price,
                "size": size,
                "token": token,
                "opened_ts": verdict.ts,
                "deadline_ts": deadline_ts,
                "p_up": verdict.p_up,  # IMP-2: store for handle_maker_timeout
            }
            _persist_active_orders()

        # C6 fix: cooldown is recorded only on confirmed OPEN
        _record_cooldown(coin, verdict.ts)

        return {
            "status": "OPEN",
            "order_id": order_id,
            "buy_price": buy_price,
            "size": size,
            "reason": "OK",
        }
    except Exception as outer_exc:
        # Belt-and-suspenders: any unexpected error must unlock the coin slot.
        with _orders_lock:
            _active_orders.pop(coin, None)
            _persist_active_orders()
        return {
            "status": "ABORTED", "order_id": None,
            "buy_price": 0.0, "size": 0.0,
            "reason": f"UNEXPECTED: {outer_exc}",
        }


def _phase_taker(
    verdict: OracleVerdict,
    token: str,
    ask_price: float,
    balance: float,
    clob_client: Any,
) -> Dict[str, Any]:
    """Place a FAK taker order at ask_price.

    Uses clob_client.place_fak_order: partial fills are accepted as-is and
    the remainder is cancelled by the exchange.

    Clears the per-coin lock in finally so a failed taker path never leaves
    the coin stuck.
    """
    from py_clob_client.order_builder.constants import BUY as _BUY

    cfg = _get_oracle_config()
    coin = verdict.details.get("coin", "?")

    if ask_price <= 0.01 or ask_price >= 0.99:
        return {
            "status": "ABORTED",
            "filled_size": 0.0,
            "partial": False,
            "reason": f"ASK_OUT_OF_RANGE: {ask_price}",
        }

    with _orders_lock:
        existing = _active_orders.get(coin)
        if existing is not None and existing.get("phase") != "TAKER":
            return {
                "status": "ABORTED",
                "filled_size": 0.0,
                "partial": False,
                "reason": "COIN_LOCKED: active order exists",
            }
        _active_orders[coin] = {
            "order_id": None,
            "phase": "TAKER",
            "opened_ts": verdict.ts,
        }
        _persist_active_orders()

    try:
        size = _compute_kelly_size(
            balance=balance,
            p_win=verdict.p_up or 0.0,
            ask_price=ask_price,
            min_bet=cfg["min_bet_size"],
            max_bet=cfg["max_bet_size"],
        )
        if size <= 0:
            return {
                "status": "ABORTED",
                "filled_size": 0.0,
                "partial": False,
                "reason": "KELLY_ZERO: f_star non-positive",
            }

        try:
            result = clob_client.place_fak_order(token, _BUY, ask_price, size)
        except Exception as exc:
            return {
                "status": "ABORTED",
                "filled_size": 0.0,
                "partial": False,
                "reason": f"CLOB_EXCEPTION: {exc}",
            }

        if not result or not result.get("success"):
            err = (result or {}).get("error") or (result or {}).get("status") or "unknown"
            return {
                "status": "ABORTED",
                "filled_size": 0.0,
                "partial": False,
                "reason": f"CLOB_REJECT: {err}",
            }

        filled = float(result.get("taking", size) or 0.0)
        partial = filled < size
        _record_cooldown(coin, verdict.ts)
        return {
            "status": "FILLED",
            "filled_size": filled,
            "partial": partial,
            "reason": "OK",
        }
    finally:
        with _orders_lock:
            _active_orders.pop(coin, None)
            _persist_active_orders()


def handle_maker_timeout(
    coin: str,
    token: str,
    ask_price: float,
    balance: float,
    now_ts: float,
    p_up: float,
    clob_client: Any,
) -> Dict[str, Any]:
    """Switch an active maker order to taker once the cutoff window is reached."""
    cfg = _get_oracle_config()

    with _orders_lock:
        order = _active_orders.get(coin)
        if not order or order.get("phase") != "MAKER":
            return {"action": "HOLD", "reason": "NO_ACTIVE_MAKER", "taker_result": None}

        deadline_ts = order.get("deadline_ts", now_ts)
        remaining = deadline_ts - now_ts
        if remaining > cfg["taker_switch_sec"]:
            return {"action": "HOLD", "reason": f"REMAINING_{remaining:.1f}s", "taker_result": None}

        order_id = order.get("order_id")
        order["phase"] = "CANCELLING"
        _persist_active_orders()

    try:
        cancelled = clob_client.cancel_order(order_id)
    except Exception as exc:
        with _orders_lock:
            _active_orders.pop(coin, None)
            _persist_active_orders()
        return {"action": "ABORTED", "reason": f"CANCEL_EXCEPTION: {exc}", "taker_result": None}

    if not cancelled:
        with _orders_lock:
            _active_orders.pop(coin, None)
            _persist_active_orders()
        return {"action": "ABORTED", "reason": "CANCEL_FAILED", "taker_result": None}

    with _orders_lock:
        if coin in _active_orders:
            _active_orders[coin]["phase"] = "TAKER"
            _persist_active_orders()

    synthetic_verdict = OracleVerdict(
        action="BUY",
        direction=None,
        p_up=p_up,
        phase="TAKER",
        reason="OK",
        details={"coin": coin},
        ts=now_ts,
    )
    try:
        taker_result = _phase_taker(
            verdict=synthetic_verdict,
            token=token,
            ask_price=ask_price,
            balance=balance,
            clob_client=clob_client,
        )
    except Exception as exc:
        with _orders_lock:
            _active_orders.pop(coin, None)
            _persist_active_orders()
        return {"action": "ABORTED", "reason": f"TAKER_EXCEPTION: {exc}", "taker_result": None}

    return {
        "action": "SWITCHED_TO_TAKER",
        "reason": "OK",
        "taker_result": taker_result,
    }
