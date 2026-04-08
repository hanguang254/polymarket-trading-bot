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
