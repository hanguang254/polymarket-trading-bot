# tests/test_oracle_sniper.py
"""Unit tests for Oracle Sniper (v14.4) — tail-segment high-confidence channel."""
import time
import time as _time_module  # alias for use inside tests that need a wall clock
import pytest

from ai_trader.oracle_sniper import OracleVerdict


# M10 fix: BinancePriceStream is a singleton. Reset between tests so
# module-level state doesn't leak across test ordering.
@pytest.fixture(autouse=True)
def _reset_binance_singleton():
    try:
        from ai_trader.binance_api import BinancePriceStream
        BinancePriceStream._instance = None
    except Exception:
        pass
    yield
    try:
        from ai_trader.binance_api import BinancePriceStream
        BinancePriceStream._instance = None
    except Exception:
        pass


@pytest.fixture
def oracle_env(monkeypatch):
    """Explicit env fixture. Each test opts in and sets its own LIVE flag."""
    monkeypatch.setenv("ORACLE_SNIPER_ENABLED", "1")
    monkeypatch.setenv("ORACLE_SNIPER_LIVE", "1")  # override in tests that need shadow
    return monkeypatch


def test_oracle_verdict_dataclass_minimal():
    v = OracleVerdict(
        action="REJECT",
        direction=None,
        p_up=None,
        phase=None,
        reason="OUT_OF_WINDOW",
        details={"remaining": 45.0},
        ts=1712345678.0,
    )
    assert v.action == "REJECT"
    assert v.direction is None
    assert v.reason == "OUT_OF_WINDOW"
    assert v.details["remaining"] == 45.0


def test_oracle_verdict_buy_maker_phase():
    v = OracleVerdict(
        action="BUY",
        direction="UP",
        p_up=0.9563,
        phase="MAKER",
        reason="OK",
        details={"cl_price": 70432.5},
        ts=1712345678.0,
    )
    assert v.action == "BUY"
    assert v.direction == "UP"
    assert v.phase == "MAKER"


# ═══ Tests for get_price_delta() helper ═══
from collections import deque
from ai_trader.binance_api import BinancePriceStream, get_price_delta


def _make_stream_with_trades(coin: str, trades):
    """Create a BinancePriceStream with pre-seeded _trade_tape for testing.

    trades: list of (ts_offset_sec_from_now, price, qty, is_buyer_maker)
    """
    stream = BinancePriceStream.__new__(BinancePriceStream)
    stream._trade_tape = {}
    stream.prices = {}
    stream.last_update = {}
    stream.event_timestamps = {}
    stream.trade_timestamps = {}
    stream.update_count = {}
    now = time.time()
    tape = deque(maxlen=500)
    for offset, price, qty, maker in trades:
        tape.append((now + offset, price, qty, maker))
    stream._trade_tape[coin] = tape
    # Mark price stream as fresh (not stale) so get_snapshot returns non-stale.
    stream.prices[coin] = trades[-1][1] if trades else None
    stream.last_update[coin] = now
    stream.update_count[coin] = len(trades)
    return stream


def test_get_price_delta_up_trend():
    stream = _make_stream_with_trades("BTC", [
        (-14.0, 70000.0, 0.1, False),
        (-7.0, 70050.0, 0.1, False),
        (-1.0, 70100.0, 0.1, False),
    ])
    result = stream.get_price_delta("BTC", window_sec=15)
    assert result["start_price"] == 70000.0
    assert result["end_price"] == 70100.0
    assert result["n_trades"] == 3
    assert result["stale"] is False
    assert result["direction"] == "UP"
    # (70100 / 70000 - 1) * 10000 ≈ 14.28 bps
    assert 14.0 < result["delta_bps"] < 14.5


def test_get_price_delta_down_trend():
    stream = _make_stream_with_trades("BTC", [
        (-14.0, 70000.0, 0.1, True),
        (-7.0, 69950.0, 0.1, True),
        (-1.0, 69900.0, 0.1, True),
    ])
    result = stream.get_price_delta("BTC", window_sec=15)
    assert result["direction"] == "DOWN"
    assert result["delta_bps"] < 0
    assert result["n_trades"] == 3


def test_get_price_delta_flat():
    stream = _make_stream_with_trades("BTC", [
        (-14.0, 70000.0, 0.1, False),
        (-1.0, 70000.0, 0.1, False),
    ])
    result = stream.get_price_delta("BTC", window_sec=15)
    assert result["direction"] == "FLAT"
    assert abs(result["delta_bps"]) < 0.01


def test_get_price_delta_empty_window():
    stream = _make_stream_with_trades("BTC", [
        (-60.0, 70000.0, 0.1, False),  # outside 15s window
    ])
    result = stream.get_price_delta("BTC", window_sec=15)
    assert result["n_trades"] == 0
    assert result["stale"] is True
    assert result["delta_bps"] == 0.0
    assert result["direction"] == "FLAT"


def test_get_price_delta_no_coin_tape():
    stream = BinancePriceStream.__new__(BinancePriceStream)
    stream._trade_tape = {}
    result = stream.get_price_delta("ETH", window_sec=15)
    assert result["stale"] is True
    assert result["n_trades"] == 0


# ═══ Tests for cooldown gate ═══


def test_cooldown_first_call_passes():
    from ai_trader.oracle_sniper import _cooldown_check, _reset_cooldown_state
    _reset_cooldown_state()
    now = 1000.0
    ok, reason = _cooldown_check("BTC", now, cooldown_sec=5.0)
    assert ok is True
    assert reason is None


def test_cooldown_blocks_within_window():
    from ai_trader.oracle_sniper import _cooldown_check, _record_cooldown, _reset_cooldown_state
    _reset_cooldown_state()
    _record_cooldown("BTC", 1000.0)
    ok, reason = _cooldown_check("BTC", 1003.0, cooldown_sec=5.0)
    assert ok is False
    assert reason == "COOLDOWN"


def test_cooldown_lifts_after_window():
    from ai_trader.oracle_sniper import _cooldown_check, _record_cooldown, _reset_cooldown_state
    _reset_cooldown_state()
    _record_cooldown("BTC", 1000.0)
    ok, reason = _cooldown_check("BTC", 1006.0, cooldown_sec=5.0)
    assert ok is True
    assert reason is None


def test_cooldown_per_coin_isolation():
    from ai_trader.oracle_sniper import _cooldown_check, _record_cooldown, _reset_cooldown_state
    _reset_cooldown_state()
    _record_cooldown("BTC", 1000.0)
    ok, _ = _cooldown_check("ETH", 1001.0, cooldown_sec=5.0)
    assert ok is True  # ETH is a different coin, not affected by BTC cooldown


# ─────────── Chainlink freshness gate ───────────

def test_chainlink_freshness_ok():
    from ai_trader.oracle_sniper import _chainlink_freshness_check
    snapshot = {"price": 70000.0, "age_ms": 500, "stale": False}
    ok, reason, details = _chainlink_freshness_check(snapshot, max_age_sec=1.0)
    assert ok is True
    assert reason is None
    assert details["cl_price"] == 70000.0


def test_chainlink_freshness_missing():
    from ai_trader.oracle_sniper import _chainlink_freshness_check
    ok, reason, details = _chainlink_freshness_check(None, max_age_sec=1.0)
    assert ok is False
    assert reason == "CL_MISSING"


def test_chainlink_freshness_stale_by_age():
    from ai_trader.oracle_sniper import _chainlink_freshness_check
    snapshot = {"price": 70000.0, "age_ms": 2500, "stale": False}
    ok, reason, details = _chainlink_freshness_check(snapshot, max_age_sec=1.0)
    assert ok is False
    assert reason == "CL_STALE"
    assert details["age_ms"] == 2500


def test_chainlink_freshness_stale_flag():
    from ai_trader.oracle_sniper import _chainlink_freshness_check
    snapshot = {"price": 70000.0, "age_ms": 500, "stale": True}
    ok, reason, details = _chainlink_freshness_check(snapshot, max_age_sec=1.0)
    assert ok is False
    assert reason == "CL_STALE"


# ─────────── Confidence gate (gbm_p_up threshold) ───────────

def test_confidence_up_high():
    from ai_trader.oracle_sniper import _compute_confidence
    verdict = _compute_confidence(
        price=71000.0, strike=70000.0, atr=50.0, remaining_sec=25.0, threshold=0.93
    )
    assert verdict["direction"] == "UP"
    assert verdict["p_up"] > 0.93
    assert verdict["reason"] is None


def test_confidence_down_high():
    from ai_trader.oracle_sniper import _compute_confidence
    verdict = _compute_confidence(
        price=69000.0, strike=70000.0, atr=50.0, remaining_sec=25.0, threshold=0.93
    )
    assert verdict["direction"] == "DOWN"
    assert verdict["p_up"] < 0.07
    assert verdict["reason"] is None


def test_confidence_middle_rejected():
    from ai_trader.oracle_sniper import _compute_confidence
    verdict = _compute_confidence(
        price=70000.1, strike=70000.0, atr=500.0, remaining_sec=25.0, threshold=0.93
    )
    # price ≈ strike with large ATR → p_up ≈ 0.5 → LOW_CONFIDENCE
    assert verdict["direction"] is None
    assert verdict["reason"] == "LOW_CONFIDENCE"
    assert 0.07 < verdict["p_up"] < 0.93


# ─────────── Binance reversal gate ───────────

def test_binance_reversal_allow_same_direction():
    from ai_trader.oracle_sniper import _binance_reversal_check
    bn = {"delta_bps": 5.0, "n_trades": 40, "stale": False, "direction": "UP"}
    ok, reason, warn = _binance_reversal_check(bn, direction="UP", reverse_bps_threshold=2.0)
    assert ok is True
    assert reason is None
    assert warn is False


def test_binance_reversal_block_opposite():
    from ai_trader.oracle_sniper import _binance_reversal_check
    bn = {"delta_bps": -3.0, "n_trades": 40, "stale": False, "direction": "DOWN"}
    ok, reason, warn = _binance_reversal_check(bn, direction="UP", reverse_bps_threshold=2.0)
    assert ok is False
    assert reason == "BN_CONTRADICT"


def test_binance_reversal_below_threshold_allowed():
    from ai_trader.oracle_sniper import _binance_reversal_check
    # -1 bps reverse, threshold is 2 bps → allowed
    bn = {"delta_bps": -1.0, "n_trades": 40, "stale": False, "direction": "DOWN"}
    ok, reason, warn = _binance_reversal_check(bn, direction="UP", reverse_bps_threshold=2.0)
    assert ok is True
    assert reason is None


def test_binance_reversal_sparse_window_allow_with_warn():
    from ai_trader.oracle_sniper import _binance_reversal_check
    bn = {"delta_bps": 0.0, "n_trades": 0, "stale": True, "direction": "FLAT"}
    ok, reason, warn = _binance_reversal_check(bn, direction="UP", reverse_bps_threshold=2.0)
    assert ok is True
    assert reason is None
    assert warn is True  # freshness-bottom fallback


# ─────────── check_oracle_sniper() main entry ───────────

from unittest.mock import patch
from ai_trader.oracle_sniper import check_oracle_sniper


class _StubSnapshot:
    """Helper to build a chainlink snapshot dict."""
    @staticmethod
    def ok(price=71000.0, age_ms=500):
        return {"price": price, "age_ms": age_ms, "stale": False}

    @staticmethod
    def missing():
        return None

    @staticmethod
    def stale():
        return {"price": 71000.0, "age_ms": 2500, "stale": False}


def _stub_bn_allow(direction="UP"):
    return {"delta_bps": 5.0, "n_trades": 40, "stale": False, "direction": direction}


def _stub_bn_reverse(direction="DOWN"):
    return {"delta_bps": -5.0, "n_trades": 40, "stale": False, "direction": direction}


def _stub_bn_sparse():
    return {"delta_bps": 0.0, "n_trades": 0, "stale": True, "direction": "FLAT"}


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_out_of_window(bn_mock, cl_mock, oracle_env):
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok()
    bn_mock.return_value = _stub_bn_allow()
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 45.0, now_ts=1000.0,
    )
    assert v.action == "REJECT"
    assert v.reason == "OUT_OF_WINDOW"


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_too_late(bn_mock, cl_mock, oracle_env):
    """remaining < TOO_LATE_SEC (default 1.0s) → REJECT(TOO_LATE)."""
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok()
    bn_mock.return_value = _stub_bn_allow()
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 0.5, now_ts=1000.0,
    )
    assert v.action == "REJECT"
    assert v.reason == "TOO_LATE"


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_cl_missing(bn_mock, cl_mock, oracle_env):
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = None
    bn_mock.return_value = _stub_bn_allow()
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 25.0, now_ts=1000.0,
    )
    assert v.action == "REJECT"
    assert v.reason == "CL_MISSING"


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_cl_stale(bn_mock, cl_mock, oracle_env):
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.stale()
    bn_mock.return_value = _stub_bn_allow()
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 25.0, now_ts=1000.0,
    )
    assert v.reason == "CL_STALE"


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_accept_up_maker_phase(bn_mock, cl_mock, oracle_env):
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok(price=71000.0)
    bn_mock.return_value = _stub_bn_allow("UP")
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 25.0, now_ts=1000.0,
    )
    assert v.action == "BUY"
    assert v.direction == "UP"
    assert v.phase == "MAKER"
    assert v.p_up > 0.93
    assert v.reason == "OK"


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_accept_up_taker_phase(bn_mock, cl_mock, oracle_env):
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok(price=71000.0)
    bn_mock.return_value = _stub_bn_allow("UP")
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 10.0, now_ts=1000.0,
    )
    assert v.action == "BUY"
    assert v.direction == "UP"
    assert v.phase == "TAKER"


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_phase_boundary_exactly_15s(bn_mock, cl_mock, oracle_env):
    """I6: At exactly remaining=15.0, phase should be TAKER (> is strict)."""
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok(price=71000.0)
    bn_mock.return_value = _stub_bn_allow("UP")
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 15.0, now_ts=1000.0,
    )
    assert v.action == "BUY"
    assert v.phase == "TAKER"


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_bn_contradict(bn_mock, cl_mock, oracle_env):
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok(price=71000.0)
    bn_mock.return_value = _stub_bn_reverse("DOWN")
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 25.0, now_ts=1000.0,
    )
    assert v.action == "REJECT"
    assert v.reason == "BN_CONTRADICT"


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_bn_sparse_allow_with_warn(bn_mock, cl_mock, oracle_env):
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok(price=71000.0)
    bn_mock.return_value = _stub_bn_sparse()
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 25.0, now_ts=1000.0,
    )
    assert v.action == "BUY"
    assert v.details.get("bn_sparse_warn") is True


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_low_confidence(bn_mock, cl_mock, oracle_env):
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok(price=70000.1)
    bn_mock.return_value = _stub_bn_allow()
    v = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=500.0,
        deadline_ts=1000.0 + 25.0, now_ts=1000.0,
    )
    assert v.action == "REJECT"
    assert v.reason == "LOW_CONFIDENCE"


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_cooldown_not_recorded_on_verdict_alone(bn_mock, cl_mock, oracle_env):
    """C6 fix: check_oracle_sniper no longer burns cooldown on a BUY verdict."""
    from ai_trader.oracle_sniper import _reset_cooldown_state
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok(price=71000.0)
    bn_mock.return_value = _stub_bn_allow("UP")
    v1 = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 25.0, now_ts=1000.0,
    )
    assert v1.action == "BUY"
    v2 = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 22.0, now_ts=1003.0,
    )
    assert v2.action == "BUY"  # cooldown was NOT recorded


@patch("ai_trader.oracle_sniper._get_chainlink_snapshot")
@patch("ai_trader.oracle_sniper._get_binance_delta")
def test_check_cooldown_blocks_after_explicit_record(bn_mock, cl_mock, oracle_env):
    """After _record_cooldown is called (simulating a successful phase_maker),
    the next check within 5s is blocked."""
    from ai_trader.oracle_sniper import _reset_cooldown_state, _record_cooldown
    _reset_cooldown_state()
    cl_mock.return_value = _StubSnapshot.ok(price=71000.0)
    bn_mock.return_value = _stub_bn_allow("UP")
    v1 = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 25.0, now_ts=1000.0,
    )
    assert v1.action == "BUY"
    _record_cooldown("BTC", 1000.0)
    v2 = check_oracle_sniper(
        coin="BTC", strike=70000.0, atr=50.0,
        deadline_ts=1000.0 + 22.0, now_ts=1003.0,
    )
    assert v2.action == "REJECT"
    assert v2.reason == "COOLDOWN"


# ═══ Tests for log writer ═══

import json
import os
from pathlib import Path


def test_log_oracle_verdict_writes_jsonl(tmp_path, monkeypatch):
    from ai_trader.oracle_sniper import log_oracle_verdict, OracleVerdict
    log_file = tmp_path / "oracle_sniper.jsonl"
    monkeypatch.setenv("ORACLE_SNIPER_LOG_PATH", str(log_file))

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.9563, phase="MAKER",
        reason="OK",
        details={"coin": "BTC", "cl_price": 70432.5, "remaining": 19.88},
        ts=1712345678.123,
    )
    log_oracle_verdict(v)

    assert log_file.exists()
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["action"] == "BUY"
    assert record["direction"] == "UP"
    assert record["p_up"] == 0.9563
    assert record["phase"] == "MAKER"
    assert record["reason"] == "OK"
    assert record["details"]["coin"] == "BTC"
    assert record["ts"] == 1712345678.123


def test_log_oracle_verdict_dedups_repeated_reject(tmp_path, monkeypatch):
    from ai_trader.oracle_sniper import log_oracle_verdict, OracleVerdict, _reset_log_dedup_state
    log_file = tmp_path / "oracle_sniper.jsonl"
    monkeypatch.setenv("ORACLE_SNIPER_LOG_PATH", str(log_file))
    _reset_log_dedup_state()

    for i in range(5):
        v = OracleVerdict(
            action="REJECT", direction=None, p_up=None, phase=None,
            reason="COOLDOWN",
            details={"coin": "BTC", "remaining": 20.0 - i},
            ts=1712345678.0 + i,
        )
        log_oracle_verdict(v)

    lines = log_file.read_text().strip().splitlines()
    # Only the first COOLDOWN reject should be written; subsequent ones deduped.
    assert len(lines) == 1


def test_log_oracle_verdict_dedups_cl_stale_flood(tmp_path, monkeypatch):
    """I1 fix: CL_STALE must dedup too (was flooding at 50ms cadence)."""
    from ai_trader.oracle_sniper import log_oracle_verdict, OracleVerdict, _reset_log_dedup_state
    log_file = tmp_path / "oracle_sniper.jsonl"
    monkeypatch.setenv("ORACLE_SNIPER_LOG_PATH", str(log_file))
    _reset_log_dedup_state()

    for i in range(20):
        v = OracleVerdict(
            action="REJECT", direction=None, p_up=None, phase=None,
            reason="CL_STALE",
            details={"coin": "BTC", "age_ms": 2500 + i * 50},
            ts=1712345678.0 + i * 0.05,
        )
        log_oracle_verdict(v)

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1  # 20 calls → 1 logged line


def test_log_oracle_verdict_dedup_clears_on_buy(tmp_path, monkeypatch):
    """After a BUY, the per-coin dedup state clears so next REJECT writes once."""
    from ai_trader.oracle_sniper import log_oracle_verdict, OracleVerdict, _reset_log_dedup_state
    log_file = tmp_path / "oracle_sniper.jsonl"
    monkeypatch.setenv("ORACLE_SNIPER_LOG_PATH", str(log_file))
    _reset_log_dedup_state()

    reject = OracleVerdict(
        action="REJECT", direction=None, p_up=None, phase=None,
        reason="COOLDOWN", details={"coin": "BTC"}, ts=1000.0,
    )
    buy = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="MAKER",
        reason="OK", details={"coin": "BTC"}, ts=1001.0,
    )
    reject2 = OracleVerdict(
        action="REJECT", direction=None, p_up=None, phase=None,
        reason="COOLDOWN", details={"coin": "BTC"}, ts=1002.0,
    )

    log_oracle_verdict(reject)
    log_oracle_verdict(buy)
    log_oracle_verdict(reject2)

    lines = log_file.read_text().strip().splitlines()
    # REJECT → 1 line, BUY → 1 line (clears dedup), REJECT → 1 line again
    assert len(lines) == 3


def test_log_oracle_verdict_per_coin_isolation(tmp_path, monkeypatch):
    """Dedup is per-coin: BTC COOLDOWN doesn't suppress ETH COOLDOWN."""
    from ai_trader.oracle_sniper import log_oracle_verdict, OracleVerdict, _reset_log_dedup_state
    log_file = tmp_path / "oracle_sniper.jsonl"
    monkeypatch.setenv("ORACLE_SNIPER_LOG_PATH", str(log_file))
    _reset_log_dedup_state()

    for coin in ("BTC", "ETH", "BTC", "ETH"):
        v = OracleVerdict(
            action="REJECT", direction=None, p_up=None, phase=None,
            reason="COOLDOWN", details={"coin": coin}, ts=1000.0,
        )
        log_oracle_verdict(v)

    lines = log_file.read_text().strip().splitlines()
    # BTC writes once (first), ETH writes once (first), BTC/ETH deduped after
    assert len(lines) == 2


def test_log_oracle_verdict_dedup_does_not_suppress_buy(tmp_path, monkeypatch):
    from ai_trader.oracle_sniper import log_oracle_verdict, OracleVerdict, _reset_log_dedup_state
    log_file = tmp_path / "oracle_sniper.jsonl"
    monkeypatch.setenv("ORACLE_SNIPER_LOG_PATH", str(log_file))
    _reset_log_dedup_state()

    for i in range(3):
        v = OracleVerdict(
            action="BUY", direction="UP", p_up=0.95, phase="MAKER",
            reason="OK", details={"coin": "BTC"}, ts=1712345678.0 + i,
        )
        log_oracle_verdict(v)

    lines = log_file.read_text().strip().splitlines()
    # BUY verdicts must never be deduped.
    assert len(lines) == 3


# ═══ Tests for _phase_maker ═══

from unittest.mock import MagicMock

# Real _parse_response shape (C1/C3 fix): {success, matched, status, order_id, making, taking, raw}
def _clob_ok(order_id="OID_abc123", taking=0.0):
    return {
        "success": True, "matched": taking > 0,
        "status": "LIVE" if taking == 0 else "MATCHED",
        "order_id": order_id, "making": 0.0, "taking": taking, "raw": "",
    }


def _clob_err(status="REJECTED", error="insufficient liquidity"):
    return {
        "success": False, "matched": False, "status": status,
        "order_id": None, "making": 0, "taking": 0, "error": error, "raw": error,
    }


def test_phase_maker_places_gtd_order_success(oracle_env):
    from ai_trader.oracle_sniper import _phase_maker, OracleVerdict, _reset_oracle_state
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_order.return_value = _clob_ok("OID_abc123")

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="MAKER", reason="OK",
        details={"coin": "BTC", "cl_price": 71000.0, "remaining": 25.0},
        ts=_time_module.time(),
    )

    result = _phase_maker(
        verdict=v, token="0xtoken", ask_price=0.68,
        balance=20.0, deadline_ts=v.ts + 25.0,
        clob_client=fake_clob,
    )

    assert result["status"] == "OPEN"
    assert result["order_id"] == "OID_abc123"
    assert result["buy_price"] < 0.68  # maker price is below ask by edge

    # Verify call args: positional + order_type enum + expiration int (no client_order_id)
    fake_clob.place_order.assert_called_once()
    args, kwargs = fake_clob.place_order.call_args
    assert len(args) == 4  # token, side, price, size
    assert kwargs.get("order_type") is not None
    assert "expiration" in kwargs
    assert "client_order_id" not in kwargs  # real API doesn't accept it


def test_phase_maker_kelly_uses_real_ask_not_buy_price(oracle_env):
    """C5 fix: Kelly sizing passes ask_price, not the maker-discounted buy_price."""
    from ai_trader.oracle_sniper import _phase_maker, OracleVerdict, _reset_oracle_state, _compute_kelly_size
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_order.return_value = _clob_ok("OID1")

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="MAKER", reason="OK",
        details={"coin": "BTC"}, ts=_time_module.time(),
    )
    ask = 0.68

    result = _phase_maker(
        verdict=v, token="0xtoken", ask_price=ask, balance=20.0,
        deadline_ts=v.ts + 25.0, clob_client=fake_clob,
    )
    # Expected: size computed from ask=0.68, not from buy_price=0.668
    expected_size = _compute_kelly_size(
        balance=20.0, p_win=0.95, ask_price=ask, min_bet=2.0, max_bet=3.0,
    )
    assert result["size"] == expected_size


def test_phase_maker_clob_failure_aborts_and_unlocks(oracle_env):
    from ai_trader.oracle_sniper import _phase_maker, OracleVerdict, _reset_oracle_state, _active_orders
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_order.return_value = _clob_err("FAILED", "insufficient liquidity")

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="MAKER", reason="OK",
        details={"coin": "BTC"}, ts=_time_module.time(),
    )

    result = _phase_maker(
        verdict=v, token="0xtoken", ask_price=0.68,
        balance=20.0, deadline_ts=v.ts + 25.0,
        clob_client=fake_clob,
    )

    assert result["status"] == "ABORTED"
    assert "insufficient" in result["reason"] or "FAILED" in result["reason"]
    # Crucial: coin slot must be unlocked so the next tick can try again
    assert "BTC" not in _active_orders


def test_phase_maker_coin_locked_rejected(oracle_env):
    """Per-coin lock (I2): second call while first is RESERVED returns COIN_LOCKED."""
    from ai_trader.oracle_sniper import _phase_maker, OracleVerdict, _reset_oracle_state, _active_orders, _orders_lock
    _reset_oracle_state()

    with _orders_lock:
        _active_orders["BTC"] = {"order_id": "OID_prev", "phase": "MAKER", "opened_ts": 1000.0}

    fake_clob = MagicMock()
    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="MAKER", reason="OK",
        details={"coin": "BTC"}, ts=1001.0,
    )
    result = _phase_maker(
        verdict=v, token="0xtoken", ask_price=0.68, balance=20.0,
        deadline_ts=1025.0, clob_client=fake_clob,
    )
    assert result["status"] == "ABORTED"
    assert "COIN_LOCKED" in result["reason"]
    fake_clob.place_order.assert_not_called()


def test_phase_maker_ask_out_of_range(oracle_env):
    """M3: ask <= 0.01 or >= 0.99 → ASK_OUT_OF_RANGE, no order placed."""
    from ai_trader.oracle_sniper import _phase_maker, OracleVerdict, _reset_oracle_state
    _reset_oracle_state()

    fake_clob = MagicMock()
    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="MAKER", reason="OK",
        details={"coin": "BTC"}, ts=_time_module.time(),
    )
    result = _phase_maker(
        verdict=v, token="0xtoken", ask_price=0.995, balance=20.0,
        deadline_ts=v.ts + 25.0, clob_client=fake_clob,
    )
    assert result["status"] == "ABORTED"
    assert "ASK_OUT_OF_RANGE" in result["reason"]
    fake_clob.place_order.assert_not_called()


def test_phase_maker_gtd_expiry_bounded(oracle_env):
    """CRIT-2 fix: GTD expiry = max(deadline_ts - 5, now + 60)."""
    from ai_trader.oracle_sniper import _phase_maker, OracleVerdict, _reset_oracle_state
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_order.return_value = _clob_ok("OID1")

    now = _time_module.time()
    deadline = now + 25.0
    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="MAKER", reason="OK",
        details={"coin": "BTC"}, ts=now,
    )
    _phase_maker(
        verdict=v, token="0xtoken", ask_price=0.68, balance=20.0,
        deadline_ts=deadline, clob_client=fake_clob,
    )

    _, kwargs = fake_clob.place_order.call_args
    expiration = kwargs["expiration"]
    # Security threshold satisfied
    assert expiration >= int(now) + 60
    # Tight upper bound: expiry <= max(deadline-5, now+60) + tolerance
    expected_upper = max(int(deadline) - 5, int(now) + 60)
    assert expiration <= expected_upper + 2  # 2s tolerance for time.time() drift


def test_phase_maker_records_cooldown_on_success(oracle_env):
    """C6 fix: cooldown is recorded only on OPEN, not earlier."""
    from ai_trader.oracle_sniper import _phase_maker, OracleVerdict, _reset_oracle_state, _cooldown_check
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_order.return_value = _clob_ok("OID1")

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="MAKER", reason="OK",
        details={"coin": "BTC"}, ts=1000.0,
    )
    _phase_maker(
        verdict=v, token="0xtoken", ask_price=0.68, balance=20.0,
        deadline_ts=1025.0, clob_client=fake_clob,
    )
    # Now cooldown should block within 5s
    ok, reason = _cooldown_check("BTC", 1003.0, cooldown_sec=5.0)
    assert ok is False
    assert reason == "COOLDOWN"


def test_phase_taker_fak_full_fill(oracle_env):
    from ai_trader.oracle_sniper import _phase_taker, OracleVerdict, _reset_oracle_state
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_fak_order.return_value = _clob_ok("OID_taker", taking=4.3)

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="TAKER", reason="OK",
        details={"coin": "BTC", "cl_price": 71000.0, "remaining": 10.0},
        ts=1000.0,
    )

    result = _phase_taker(
        verdict=v, token="0xtoken", ask_price=0.68,
        balance=20.0, clob_client=fake_clob,
    )

    assert result["status"] == "FILLED"
    assert result["filled_size"] == 4.3
    fake_clob.place_fak_order.assert_called_once()
    args, _ = fake_clob.place_fak_order.call_args
    assert len(args) == 4


def test_phase_taker_fak_partial_fill(oracle_env):
    from ai_trader.oracle_sniper import _phase_taker, OracleVerdict, _reset_oracle_state
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_fak_order.return_value = _clob_ok("OID_taker", taking=2.0)

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="TAKER", reason="OK",
        details={"coin": "BTC", "cl_price": 71000.0, "remaining": 10.0},
        ts=1000.0,
    )

    result = _phase_taker(
        verdict=v, token="0xtoken", ask_price=0.68,
        balance=20.0, clob_client=fake_clob,
    )

    assert result["status"] == "FILLED"
    assert result["filled_size"] == 2.0
    assert result["partial"] is True


def test_phase_taker_clob_failure_aborts(oracle_env):
    from ai_trader.oracle_sniper import _phase_taker, OracleVerdict, _reset_oracle_state
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_fak_order.return_value = _clob_err("REJECTED", "no liquidity")

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="TAKER", reason="OK",
        details={"coin": "BTC"}, ts=1000.0,
    )

    result = _phase_taker(
        verdict=v, token="0xtoken", ask_price=0.68,
        balance=20.0, clob_client=fake_clob,
    )

    assert result["status"] == "ABORTED"
    assert "no liquidity" in result["reason"] or "REJECTED" in result["reason"]


def test_phase_taker_records_cooldown_on_fill(oracle_env):
    """C6 fix: cooldown is recorded only on FILLED."""
    from ai_trader.oracle_sniper import _phase_taker, OracleVerdict, _reset_oracle_state, _cooldown_check
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_fak_order.return_value = _clob_ok("OID1", taking=4.3)

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="TAKER", reason="OK",
        details={"coin": "BTC"}, ts=1000.0,
    )
    _phase_taker(
        verdict=v, token="0xtoken", ask_price=0.68, balance=20.0, clob_client=fake_clob,
    )
    ok, reason = _cooldown_check("BTC", 1003.0, cooldown_sec=5.0)
    assert ok is False
    assert reason == "COOLDOWN"


def test_phase_taker_does_not_record_cooldown_on_abort(oracle_env):
    """C6 fix: a failed taker must NOT burn the 5s cooldown."""
    from ai_trader.oracle_sniper import _phase_taker, OracleVerdict, _reset_oracle_state, _cooldown_check
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.place_fak_order.return_value = _clob_err()

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="TAKER", reason="OK",
        details={"coin": "BTC"}, ts=1000.0,
    )
    _phase_taker(
        verdict=v, token="0xtoken", ask_price=0.68, balance=20.0, clob_client=fake_clob,
    )
    ok, reason = _cooldown_check("BTC", 1003.0, cooldown_sec=5.0)
    assert ok is True
    assert reason is None


def test_phase_taker_coin_locked_rejected(oracle_env):
    """CRIT-1 fix: _phase_taker reserves coin slot, rejects if MAKER is in-flight."""
    from ai_trader.oracle_sniper import _phase_taker, OracleVerdict, _reset_oracle_state, _active_orders, _orders_lock
    _reset_oracle_state()

    with _orders_lock:
        _active_orders["BTC"] = {"order_id": "OID_prev", "phase": "MAKER", "opened_ts": 1000.0}

    fake_clob = MagicMock()
    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="TAKER", reason="OK",
        details={"coin": "BTC"}, ts=1001.0,
    )
    result = _phase_taker(
        verdict=v, token="0xtoken", ask_price=0.68, balance=20.0, clob_client=fake_clob,
    )
    assert result["status"] == "ABORTED"
    assert "COIN_LOCKED" in result["reason"]
    fake_clob.place_fak_order.assert_not_called()


def test_phase_taker_accepts_pre_set_taker_phase(oracle_env):
    """CRIT-1 carve-out: handle_maker_timeout transitions phase to TAKER first."""
    from ai_trader.oracle_sniper import _phase_taker, OracleVerdict, _reset_oracle_state, _active_orders, _orders_lock
    _reset_oracle_state()

    with _orders_lock:
        _active_orders["BTC"] = {"order_id": None, "phase": "TAKER", "opened_ts": 1000.0}

    fake_clob = MagicMock()
    fake_clob.place_fak_order.return_value = _clob_ok("OID_taker", taking=4.3)

    v = OracleVerdict(
        action="BUY", direction="UP", p_up=0.95, phase="TAKER", reason="OK",
        details={"coin": "BTC"}, ts=1001.0,
    )
    result = _phase_taker(
        verdict=v, token="0xtoken", ask_price=0.68, balance=20.0, clob_client=fake_clob,
    )
    assert result["status"] == "FILLED"
    fake_clob.place_fak_order.assert_called_once()


def test_maker_timeout_switches_to_taker(oracle_env):
    from ai_trader.oracle_sniper import handle_maker_timeout, _reset_oracle_state, _active_orders, _orders_lock
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.cancel_order.return_value = True
    fake_clob.place_fak_order.return_value = _clob_ok("OID_taker", taking=4.3)

    with _orders_lock:
        _active_orders["BTC"] = {
            "order_id": "OID_maker_abc",
            "phase": "MAKER",
            "buy_price": 0.67,
            "size": 4.4,
            "token": "0xtoken",
            "opened_ts": 1000.0,
            "deadline_ts": 1025.0,
        }

    result = handle_maker_timeout(
        coin="BTC",
        token="0xtoken",
        ask_price=0.68,
        balance=20.0,
        now_ts=1012.0,
        p_up=0.95,
        clob_client=fake_clob,
    )

    assert result["action"] == "SWITCHED_TO_TAKER"
    assert result["taker_result"]["status"] == "FILLED"
    fake_clob.cancel_order.assert_called_once_with("OID_maker_abc")
    fake_clob.place_fak_order.assert_called_once()


def test_maker_timeout_cancel_failure_aborts_without_taker(oracle_env):
    from ai_trader.oracle_sniper import handle_maker_timeout, _reset_oracle_state, _active_orders, _orders_lock
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.cancel_order.return_value = False

    with _orders_lock:
        _active_orders["BTC"] = {
            "order_id": "OID_maker_abc",
            "phase": "MAKER",
            "buy_price": 0.67,
            "size": 4.4,
            "token": "0xtoken",
            "opened_ts": 1000.0,
            "deadline_ts": 1025.0,
        }

    result = handle_maker_timeout(
        coin="BTC", token="0xtoken", ask_price=0.68, balance=20.0,
        now_ts=1012.0, p_up=0.95, clob_client=fake_clob,
    )

    assert result["action"] == "ABORTED"
    assert "CANCEL_FAILED" in result["reason"]
    fake_clob.place_fak_order.assert_not_called()
    assert "BTC" not in _active_orders


def test_maker_timeout_above_switch_is_noop(oracle_env):
    from ai_trader.oracle_sniper import handle_maker_timeout, _reset_oracle_state, _active_orders, _orders_lock
    _reset_oracle_state()

    fake_clob = MagicMock()

    with _orders_lock:
        _active_orders["BTC"] = {
            "order_id": "OID_maker_abc",
            "phase": "MAKER",
            "buy_price": 0.67,
            "size": 4.4,
            "token": "0xtoken",
            "opened_ts": 1000.0,
            "deadline_ts": 1025.0,
        }

    result = handle_maker_timeout(
        coin="BTC", token="0xtoken", ask_price=0.68, balance=20.0,
        now_ts=1005.0, p_up=0.95, clob_client=fake_clob,
    )

    assert result["action"] == "HOLD"
    fake_clob.cancel_order.assert_not_called()


def test_maker_timeout_exactly_at_15s_switches(oracle_env):
    """I6 boundary: remaining == taker_switch_sec → switch to taker."""
    from ai_trader.oracle_sniper import handle_maker_timeout, _reset_oracle_state, _active_orders, _orders_lock
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.cancel_order.return_value = True
    fake_clob.place_fak_order.return_value = _clob_ok("OID_taker", taking=4.3)

    with _orders_lock:
        _active_orders["BTC"] = {
            "order_id": "OID_maker_abc",
            "phase": "MAKER",
            "buy_price": 0.67,
            "size": 4.4,
            "token": "0xtoken",
            "opened_ts": 1000.0,
            "deadline_ts": 1025.0,
        }

    result = handle_maker_timeout(
        coin="BTC", token="0xtoken", ask_price=0.68, balance=20.0,
        now_ts=1010.0, p_up=0.95, clob_client=fake_clob,
    )

    assert result["action"] == "SWITCHED_TO_TAKER"


def test_maker_timeout_taker_exception_still_unlocks_coin(oracle_env):
    """I3 fix: if _phase_taker raises, coin must still be unlocked."""
    from ai_trader.oracle_sniper import handle_maker_timeout, _reset_oracle_state, _active_orders, _orders_lock
    _reset_oracle_state()

    fake_clob = MagicMock()
    fake_clob.cancel_order.return_value = True
    fake_clob.place_fak_order.side_effect = RuntimeError("simulated crash")

    with _orders_lock:
        _active_orders["BTC"] = {
            "order_id": "OID_maker_abc",
            "phase": "MAKER",
            "buy_price": 0.67,
            "size": 4.4,
            "token": "0xtoken",
            "opened_ts": 1000.0,
            "deadline_ts": 1025.0,
        }

    result = handle_maker_timeout(
        coin="BTC", token="0xtoken", ask_price=0.68, balance=20.0,
        now_ts=1012.0, p_up=0.95, clob_client=fake_clob,
    )
    assert "BTC" not in _active_orders
