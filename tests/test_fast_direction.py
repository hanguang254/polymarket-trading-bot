import pytest


def test_fast_direction_cross_exchange_uses_offset_adjusted_ptb(monkeypatch):
    from ai_trader.fast_direction import get_fast_direction

    def fake_spread_snapshot(coin, ptb, atr_val, allow_latest_fallback=True):
        return {
            "coin": coin,
            "binance_price": 100_020.0,
            "ptb": ptb,
            "offset": 20.0,
            "adjusted_ptb": 99_980.0,
            "diff": 40.0,
            "diff_atr": 0.4,
            "diff_atr_signed": 0.4,
            "direction": "UP",
            "supports_up": True,
            "supports_down": False,
        }

    monkeypatch.setattr(
        "ai_trader.binance_spread.get_spread_snapshot",
        fake_spread_snapshot,
    )

    result = get_fast_direction("BTC", ptb=100_000.0, atr_val=100.0)

    assert result["direction"] == "UP"
    signal = result["signals"]["cross_exchange_lead"]
    assert signal["source"] == "binance_spread_offset"
    assert signal["ptb"] == pytest.approx(100_000.0)
    assert signal["adjusted_ptb"] == pytest.approx(99_980.0)
    assert signal["offset"] == pytest.approx(20.0)
    assert signal["diff"] == pytest.approx(40.0)
    assert signal["lead_atr"] == pytest.approx(0.4)
