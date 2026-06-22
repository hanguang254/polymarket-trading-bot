import pytest
from collections import deque

from ai_trader.binance_spread import (
    calculate_binance_offset,
    calculate_spread_snapshot,
)


def test_binance_offset_filters_bucket_outlier():
    now = 1_000.0
    timestamps = [940.0, 950.0, 960.0, 970.0, 980.0, 990.0]
    binance = [{"ts": ts, "price": 100_000.0 + i} for i, ts in enumerate(timestamps)]
    chainlink = [
        {"ts": 940.0, "price": 100_020.0},
        {"ts": 950.0, "price": 100_021.0},
        {"ts": 960.0, "price": 100_022.0},
        {"ts": 970.0, "price": 100_300.0},  # stale/pushed outlier
        {"ts": 980.0, "price": 100_024.0},
        {"ts": 990.0, "price": 100_025.0},
    ]

    offset = calculate_binance_offset(binance, chainlink, now=now)

    assert offset == pytest.approx(20.0)


def test_binance_offset_can_fallback_to_latest_when_span_too_short():
    now = 1_000.0
    binance = [{"ts": 999.0, "price": 100_000.0}]
    chainlink = [{"ts": 999.2, "price": 100_012.5}]

    offset = calculate_binance_offset(
        binance,
        chainlink,
        now=now,
        allow_latest_fallback=True,
    )

    assert offset == pytest.approx(12.5)


def test_spread_snapshot_uses_offset_adjusted_price_to_beat():
    snapshot = calculate_spread_snapshot(
        coin="BTC",
        ptb=100_000.0,
        atr_val=100.0,
        binance_price=100_020.0,
        offset=20.0,
        min_diff_atr=0.3,
    )

    assert snapshot["adjusted_ptb"] == pytest.approx(99_980.0)
    assert snapshot["diff"] == pytest.approx(40.0)
    assert snapshot["diff_atr"] == pytest.approx(0.4)
    assert snapshot["diff_atr_signed"] == pytest.approx(0.4)
    assert snapshot["direction"] == "UP"
    assert snapshot["supports_up"] is True
    assert snapshot["supports_down"] is False


def test_binance_stream_price_history_includes_recent_trades_and_latest(monkeypatch):
    import time
    from ai_trader.binance_api import BinancePriceStream

    BinancePriceStream._instance = None
    stream = BinancePriceStream()
    now = time.time()
    stream._trade_tape["BTC"] = deque(
        [
            (now - 70.0, 99_900.0, 0.1, False),
            (now - 10.0, 100_000.0, 0.1, False),
            (now - 1.0, 100_010.0, 0.1, False),
        ],
        maxlen=500,
    )
    stream.prices["BTC"] = 100_012.0
    stream.last_update["BTC"] = now - 0.5

    history = stream.get_price_history("BTC", window_sec=60)

    assert [price for _ts, price in history] == [100_000.0, 100_010.0, 100_012.0]
    assert [ts for ts, _price in history] == pytest.approx([now - 10.0, now - 1.0, now - 0.5])


def test_chainlink_stream_price_history_includes_recent_points_and_latest():
    import time
    from ai_trader.polymarket_rtds import ChainlinkPriceStream

    ChainlinkPriceStream._instance = None
    stream = ChainlinkPriceStream()
    now = time.time()
    stream.price_history["BTC"] = deque(
        [
            (now - 70.0, 99_900.0),
            (now - 10.0, 100_020.0),
        ],
        maxlen=1000,
    )
    stream.prices["BTC"] = 100_025.0
    stream.last_update["BTC"] = now - 0.5

    history = stream.get_price_history("BTC", window_sec=60)

    assert [price for _ts, price in history] == [100_020.0, 100_025.0]
    assert [ts for ts, _price in history] == pytest.approx([now - 10.0, now - 0.5])


def test_live_spread_snapshot_rejects_stale_chainlink(monkeypatch):
    import time
    from ai_trader.binance_spread import get_spread_snapshot

    now = time.time()

    class FakeBinanceStream:
        def get_snapshot(self, coin):
            return {"price": 100_020.0, "stale": False, "age_ms": 100.0}

        def get_price(self, coin):
            return 100_020.0

        def get_price_history(self, coin, window_sec=60):
            return [(now - 20.0, 100_000.0), (now - 10.0, 100_010.0), (now - 1.0, 100_020.0)]

    class FakeChainlinkStream:
        def get_snapshot(self, coin):
            return {"price": 100_040.0, "stale": True, "age_ms": 20_000.0}

        def get_price(self, coin):
            return None

        def get_price_history(self, coin, window_sec=60):
            return [(now - 20.0, 100_020.0), (now - 10.0, 100_030.0), (now - 1.0, 100_040.0)]

    monkeypatch.setattr("ai_trader.binance_api.price_stream", FakeBinanceStream())
    monkeypatch.setattr("ai_trader.polymarket_rtds.chainlink_stream", FakeChainlinkStream())

    assert get_spread_snapshot("BTC", ptb=100_000.0, atr_val=100.0) is None
