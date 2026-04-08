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
