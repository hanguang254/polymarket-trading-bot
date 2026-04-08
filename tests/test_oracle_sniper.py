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
