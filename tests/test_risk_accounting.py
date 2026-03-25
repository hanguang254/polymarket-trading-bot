import json
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ai_analyze_v2 = _load_module("risk_test_ai_analyze_v2", "ai_analyze_v2.py")
trading_state = _load_module("risk_test_trading_state", "trading_state.py")


class TestTradingStateRollover(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.tmpdir.name, "trading_state.json")
        self.lock_file = self.state_file + ".lock"

        self.state_patch = patch.object(trading_state, "STATE_FILE", self.state_file)
        self.lock_patch = patch.object(trading_state, "_LOCK_FILE", self.lock_file)
        self.state_patch.start()
        self.lock_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.lock_patch.stop()
        self.tmpdir.cleanup()

    def _write_state(self, payload):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(payload, f)

    def test_overnight_settlement_does_not_refund_yesterday_reserved_cost(self):
        self._write_state({
            "consecutive_losses": 0,
            "cooldown_remaining": 0,
            "last_bet_time": None,
            "last_bet_result": None,
            "total_bets": 0,
            "total_wins": 0,
            "total_losses": 0,
            "daily_pnl": -3.0,
            "daily_pnl_date": "2026-03-21",
            "pending_costs": {
                "btc-1": {"cost": 3.0, "session_date": "2026-03-21"}
            },
        })

        with patch.object(trading_state, "_today_str", return_value="2026-03-22"):
            new_pnl = trading_state.settle_bet_cost("btc-1", 1.25)

        self.assertEqual(new_pnl, 1.25)
        state = trading_state.load_state()
        self.assertEqual(state["daily_pnl"], 1.25)
        self.assertEqual(state["pending_costs"], {})

    def test_same_day_settlement_refunds_reserved_cost_before_realized_pnl(self):
        self._write_state({
            "consecutive_losses": 0,
            "cooldown_remaining": 0,
            "last_bet_time": None,
            "last_bet_result": None,
            "total_bets": 0,
            "total_wins": 0,
            "total_losses": 0,
            "daily_pnl": -3.0,
            "daily_pnl_date": "2026-03-22",
            "pending_costs": {
                "btc-1": {"cost": 3.0, "session_date": "2026-03-22"}
            },
        })

        with patch.object(trading_state, "_today_str", return_value="2026-03-22"):
            new_pnl = trading_state.settle_bet_cost("btc-1", 1.25)

        self.assertEqual(new_pnl, 1.25)


class TestKellySizing(unittest.TestCase):
    def test_size_below_minimum_forces_minimum_net_size(self):
        with patch.dict(os.environ, {
            "MIN_BET_SIZE": "5",
            "MAX_BET_SIZE": "10",
            "P_WIN_CAP": "0.92",
            "MAX_ENTRY_BALANCE_PCT": "0.20",
        }):
            size_plan = ai_analyze_v2.calculate_kelly_size(
                confidence=0.75,
                ev=0.04,
                balance=20,
                target_price=0.8,
                p_win=0.86,
                kelly_reduction=1.0,
                exit_bid_depth=100,
                return_details=True,
            )

        self.assertTrue(size_plan["forced_to_min"])
        self.assertEqual(size_plan["target_net_size"], 5.0)
        self.assertGreaterEqual(size_plan["expected_net_size"], 5.0)
        self.assertGreaterEqual(size_plan["gross_order_size"], 5.0)

    def test_hard_cap_below_minimum_net_size_still_skips(self):
        with patch.dict(os.environ, {
            "MIN_BET_SIZE": "5",
            "MAX_BET_SIZE": "10",
            "P_WIN_CAP": "0.92",
        }):
            size_plan = ai_analyze_v2.calculate_kelly_size(
                confidence=0.75,
                ev=0.04,
                balance=19,
                target_price=0.8,
                p_win=0.86,
                kelly_reduction=1.0,
                exit_bid_depth=100,
                return_details=True,
            )

        self.assertEqual(size_plan["gross_order_size"], 0.0)
        self.assertEqual(size_plan["skip_reason"], "HARD_CAP_BELOW_MIN")

    def test_max_entry_balance_pct_can_raise_balance_cap(self):
        with patch.dict(os.environ, {
            "MIN_BET_SIZE": "4",
            "MAX_BET_SIZE": "20",
            "P_WIN_CAP": "0.90",
            "MAX_ENTRY_BALANCE_PCT": "0.40",
        }):
            size_plan = ai_analyze_v2.calculate_kelly_size(
                confidence=0.63,
                ev=0.06,
                balance=8.9,
                target_price=0.78,
                p_win=0.90,
                kelly_reduction=1.0,
                exit_bid_depth=5000,
                return_details=True,
            )

        self.assertIsNone(size_plan["skip_reason"])
        self.assertGreaterEqual(size_plan["expected_net_size"], 4.0)


if __name__ == "__main__":
    unittest.main()
