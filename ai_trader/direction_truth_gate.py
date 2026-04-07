"""
Direction Truth Gate — pre-trade direction cross-validation.

Cross-validates 3 independent sources (Chainlink, Binance, Pyth) using GBM
implied P(win) and 2/3 majority voting. Produces a DirectionTruth verdict
that all "open position" strategies must consult before committing.

See docs/superpowers/specs/2026-04-07-direction-truth-gate-design.md for the
full design rationale and decision table.

Usage:
    truth = check_direction_truth(
        coin="BTC",
        strike=100000.0,
        deadline_ts=resolve_unix,
        atr_val=current_atr,
        strict=False,  # True for Sniper/Endgame
    )
    if truth.direction is None and not SHADOW_MODE:
        return  # blocked
    # else proceed with truth.direction / truth.confidence
"""
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from ai_trader.gbm_p_up import gbm_p_up


logger = logging.getLogger(__name__)

# Per-source default stale thresholds (seconds). Overridable via env.
_DEFAULT_STALE = {"CL": 30.0, "BN": 5.0, "Pyth": 30.0}

# P1-3 fix: serialize concurrent JSONL writes — gate is called from multiple threads.
_log_lock = threading.Lock()

# v15.1 fix: state-change-only logging — 闸门每秒被调用 50+ 次，无脑写 JSONL
# 一小时产 114MB / 一天 2.7GB / 一月 80GB，93.8% 是连续相同状态。
# 用 (coin, deadline_int, call_site) 做 key 缓存上一次写入的 state；
# state 不变且距上次写入未超过心跳间隔时跳过写入。预期日志体积压缩 94%。
_LOG_HEARTBEAT_SEC = 30.0  # 即使无状态变化，每个 (coin, deadline, call_site) 至少 30s 一条心跳
_last_logged_state: dict = {}    # key -> state_tuple
_last_heartbeat_ts: dict = {}    # key -> last write ts


# ──────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class SourceVote:
    source: str                       # "CL" | "BN" | "Pyth"
    price: Optional[float]
    p_up: Optional[float]             # P(end_T > strike) per gbm_p_up
    vote: Optional[str]               # "UP" | "DOWN" | "NEUTRAL" | None (stale)
    stale: bool
    age_sec: float
    error: Optional[str] = None

    def __post_init__(self):
        """P2-4 invariant: stale and live votes must be well-formed.

        - If stale=True, vote/p_up may be None
        - If stale=False, vote MUST be in {"UP","DOWN","NEUTRAL"} and p_up MUST be a float
        Catches future bugs where a non-stale slot has vote=None (stealth NEUTRAL).
        """
        if not self.stale:
            if self.vote not in ("UP", "DOWN", "NEUTRAL"):
                raise ValueError(
                    f"SourceVote {self.source}: stale=False requires vote in "
                    f"(UP,DOWN,NEUTRAL), got {self.vote!r}"
                )
            if self.p_up is None:
                raise ValueError(
                    f"SourceVote {self.source}: stale=False requires p_up to be a float, got None"
                )


@dataclass
class DirectionTruth:
    direction: Optional[str]          # "UP" | "DOWN" | None (None = blocked OR disabled)
    confidence: float                 # [0, 1]
    block_reason: Optional[str]
    votes: dict                       # {"CL": SourceVote, "BN": SourceVote, "Pyth": SourceVote}
    mode: str                         # "strict" | "balanced" | "degraded" | "blocked" | "disabled"
    n_alive_sources: int
    n_agreeing: int

    @property
    def is_blocked(self) -> bool:
        """
        True only when the gate has actually blocked (no consensus / not enough live sources).

        IMPORTANT: returns False when mode == "disabled" — disabled gate must fall through
        to legacy logic at call sites, not be treated as a block. (P0-2 fix.)
        """
        if self.mode == "disabled":
            return False
        return self.direction is None


# ──────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _gate_enabled() -> bool:
    return os.environ.get("DIR_TRUTH_GATE_ENABLED", "1") == "1"


def _shadow_mode() -> bool:
    return os.environ.get("DIR_TRUTH_SHADOW", "1") == "1"


def _stale_threshold(source: str) -> float:
    key = f"DIR_TRUTH_STALE_{source}_SEC"
    return _env_float(key, _DEFAULT_STALE.get(source, 30.0))


def _vote_thresholds() -> tuple[float, float]:
    up = _env_float("DIR_TRUTH_VOTE_UP_P", 0.55)
    dn = _env_float("DIR_TRUTH_VOTE_DOWN_P", 0.45)
    return up, dn


def _shrinkage() -> float:
    return _env_float("DIR_TRUTH_SHRINKAGE", 0.85)


# ──────────────────────────────────────────────────────────────────────────
# Source data fetching
# ──────────────────────────────────────────────────────────────────────────


def _fetch_snapshots(coin: str) -> dict:
    """
    Pull (price, age_ms) snapshots from CL / BN / Pyth streams.

    Returns: {"CL": snapshot or None, "BN": snapshot or None, "Pyth": snapshot or None}

    Each snapshot is the dict returned by the stream's get_snapshot(coin).
    Runtime errors are caught and the slot is set to None — the gate then
    treats that source as stale/unreachable.

    NOTE: ImportError is intentionally NOT caught — a wrong import path is a
    deployment bug that must surface loudly, not be swallowed silently.
    (P0-1 lesson.)

    Tests patch this function via unittest.mock.patch.
    """
    snapshots = {"CL": None, "BN": None, "Pyth": None}

    # P0-1 fix: imports happen at module load via the helpers below; ImportError
    # would surface at module import time rather than being silenced per-call.
    from ai_trader.polymarket_rtds import chainlink_stream
    from ai_trader.binance_api import price_stream as binance_stream
    from ai_trader.pyth_api import pyth_stream

    try:
        snapshots["CL"] = chainlink_stream.get_snapshot(coin)
    except Exception as e:
        logger.debug(f"dir_truth_gate: CL snapshot fetch failed: {e}")

    try:
        snapshots["BN"] = binance_stream.get_snapshot(coin)
    except Exception as e:
        logger.debug(f"dir_truth_gate: BN snapshot fetch failed: {e}")

    try:
        snapshots["Pyth"] = pyth_stream.get_snapshot(coin)
    except Exception as e:
        logger.debug(f"dir_truth_gate: Pyth snapshot fetch failed: {e}")

    return snapshots


# ──────────────────────────────────────────────────────────────────────────
# Single-source vote logic
# ──────────────────────────────────────────────────────────────────────────


def _snapshot_to_vote(
    source: str,
    snapshot: Optional[dict],
    strike: float,
    atr_val: float,
    remaining_sec: float,
) -> SourceVote:
    """Convert a stream snapshot dict to a SourceVote."""
    if snapshot is None:
        return SourceVote(
            source=source, price=None, p_up=None, vote=None,
            stale=True, age_sec=999.0, error="snapshot_unavailable",
        )

    price = snapshot.get("price")
    age_sec = (snapshot.get("age_ms") or 0.0) / 1000.0

    if price is None:
        return SourceVote(
            source=source, price=None, p_up=None, vote=None,
            stale=True, age_sec=age_sec, error="price_missing",
        )

    if age_sec > _stale_threshold(source):
        return SourceVote(
            source=source, price=price, p_up=None, vote=None,
            stale=True, age_sec=age_sec,
        )

    p_up = gbm_p_up(price=price, strike=strike, atr=atr_val, remaining_sec=remaining_sec)
    up_th, dn_th = _vote_thresholds()
    if p_up >= up_th:
        vote = "UP"
    elif p_up <= dn_th:
        vote = "DOWN"
    else:
        vote = "NEUTRAL"

    return SourceVote(
        source=source, price=price, p_up=p_up, vote=vote,
        stale=False, age_sec=age_sec,
    )


# ──────────────────────────────────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────────────────────────────────


def _aggregate_votes(votes: dict, strict: bool) -> DirectionTruth:
    """
    Apply the spec decision table to a dict of SourceVote.

    Tests rely on this being a pure function of `votes` and `strict`.
    """
    alive = [v for v in votes.values() if not v.stale]
    n_alive = len(alive)

    if n_alive < 2:
        return DirectionTruth(
            direction=None,
            confidence=0.0,
            block_reason=f"only {n_alive} live sources (need >=2)",
            votes=votes,
            mode="blocked",
            n_alive_sources=n_alive,
            n_agreeing=0,
        )

    if strict:
        required = n_alive  # all live sources must agree
        # P2-1 fix: when n_alive < 3 in strict mode, label as "degraded" to match spec §5.2
        mode = "strict" if n_alive == 3 else "degraded"
    else:
        required = 2
        mode = "balanced" if n_alive == 3 else "degraded"

    up_votes = [v for v in alive if v.vote == "UP"]
    down_votes = [v for v in alive if v.vote == "DOWN"]

    if len(up_votes) >= required:
        direction = "UP"
        agreeing = up_votes
    elif len(down_votes) >= required:
        direction = "DOWN"
        agreeing = down_votes
    else:
        return DirectionTruth(
            direction=None,
            confidence=0.0,
            block_reason=(
                f"no {required}/{n_alive} consensus "
                f"(UP={len(up_votes)} DOWN={len(down_votes)} "
                f"NEUTRAL={n_alive - len(up_votes) - len(down_votes)})"
            ),
            votes=votes,
            mode=mode,
            n_alive_sources=n_alive,
            n_agreeing=max(len(up_votes), len(down_votes)),
        )

    # Confidence: mean p_up of agreeing sources, mapped to [0,1], shrunk
    mean_p = sum(v.p_up for v in agreeing) / len(agreeing)
    confidence = abs(mean_p - 0.5) * 2.0
    confidence *= _shrinkage()
    # P2-11: clamp to [0,1] in case shrinkage env is misconfigured > 1.0
    confidence = max(0.0, min(1.0, confidence))

    return DirectionTruth(
        direction=direction,
        confidence=confidence,
        block_reason=None,
        votes=votes,
        mode=mode,
        n_alive_sources=n_alive,
        n_agreeing=len(agreeing),
    )


# ──────────────────────────────────────────────────────────────────────────
# Public entry
# ──────────────────────────────────────────────────────────────────────────


def check_direction_truth(
    coin: str,
    strike: float,
    deadline_ts: float,
    atr_val: float,
    *,
    strict: bool = False,
    now_ts: Optional[float] = None,
    call_site: str = "unknown",
) -> DirectionTruth:
    """
    Pre-trade direction truth gate.

    Args:
        coin: e.g. "BTC"
        strike: contract resolution price
        deadline_ts: contract resolution unix epoch (seconds)
        atr_val: current ATR estimate (same units as price)
        strict: True for Sniper/Endgame (3/3 unanimous), False for market making (2/3)
        now_ts: injectable for testing; defaults to time.time()
        call_site: tag for log filtering

    Returns:
        DirectionTruth. If `direction is None`, the gate has blocked.
        Caller is responsible for honouring/ignoring the verdict based on
        DIR_TRUTH_SHADOW.
    """
    if not _gate_enabled():
        return DirectionTruth(
            direction=None,
            confidence=0.0,
            block_reason="gate disabled",
            votes={},
            mode="disabled",
            n_alive_sources=0,
            n_agreeing=0,
        )

    if now_ts is None:
        now_ts = time.time()

    remaining_sec = max(0.0, deadline_ts - now_ts)

    snapshots = _fetch_snapshots(coin)
    votes = {
        source: _snapshot_to_vote(source, snapshots.get(source), strike, atr_val, remaining_sec)
        for source in ("CL", "BN", "Pyth")
    }

    truth = _aggregate_votes(votes, strict=strict)

    _log_gate_decision(
        coin=coin,
        strike=strike,
        deadline_ts=deadline_ts,
        now_ts=now_ts,
        atr_val=atr_val,
        strict=strict,
        call_site=call_site,
        truth=truth,
    )

    return truth


# ──────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────


def _log_gate_decision(
    coin, strike, deadline_ts, now_ts, atr_val,
    strict, call_site, truth: DirectionTruth,
):
    """Append one JSON line to logs/dir_truth_gate.jsonl.

    v15.1 dedup: 用 (coin, deadline_int, call_site) 做 key，state 没变且距上次
    写入未到 _LOG_HEARTBEAT_SEC 秒就跳过。被跳过的事件仍然在内存里参与决策，
    只是不再每 2ms 砸一行到磁盘。所有方向反转 / mode 切换 / 拦截事件都会被
    立即写入（state_tuple 改变 → bypass dedup）。
    """
    try:
        # ── state-change-only dedup with heartbeat ──
        key = (coin, int(deadline_ts), call_site)
        state_tuple = (
            truth.direction,
            truth.mode,
            truth.n_alive_sources,
            tuple((v.vote, v.stale) for v in truth.votes.values()),
        )
        with _log_lock:
            last_state = _last_logged_state.get(key)
            last_hb = _last_heartbeat_ts.get(key, 0.0)
            if last_state == state_tuple and (now_ts - last_hb) < _LOG_HEARTBEAT_SEC:
                return  # 状态没变 + 心跳未到 → 跳过写入
            _last_logged_state[key] = state_tuple
            _last_heartbeat_ts[key] = now_ts

        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "dir_truth_gate.jsonl")

        record = {
            "ts": round(now_ts, 3),
            "coin": coin,
            "strike": strike,
            "deadline_in": round(deadline_ts - now_ts, 2),
            "atr": round(atr_val, 4),
            "call_site": call_site,
            "strict": strict,
            "votes": {
                k: {
                    "price": v.price,
                    "p_up": round(v.p_up, 4) if v.p_up is not None else None,
                    "vote": v.vote,
                    "stale": v.stale,
                    "age_sec": round(v.age_sec, 3),
                    "error": v.error,
                }
                for k, v in truth.votes.items()
            },
            "decision": truth.direction,
            "confidence": round(truth.confidence, 4),
            "mode": truth.mode,
            "n_alive": truth.n_alive_sources,
            "n_agreeing": truth.n_agreeing,
            "block_reason": truth.block_reason,
            "shadow": _shadow_mode(),
            "would_have_blocked": truth.is_blocked,
        }
        # P1-3 fix: serialize concurrent writes from sniper/sampling/ambush/endgame threads
        with _log_lock:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"dir_truth_gate: log write failed: {e}")


def is_shadow_mode() -> bool:
    """Public helper for callers to honour shadow mode."""
    return _shadow_mode()
