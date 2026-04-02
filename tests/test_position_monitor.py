import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import position_monitor as pm

# Restore stdout/stderr after module replaces them.
sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__


class DummyResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestPositionMonitor(unittest.TestCase):
    def test_compute_p0_profit_threshold(self):
        threshold = pm.compute_p0_profit_threshold(60, 0.15, 0.15)
        self.assertAlmostEqual(threshold, 0.1725, places=6)

    def test_resolve_market_end_time_from_slug(self):
        end_ts = pm.resolve_market_end_time("btc-updown-5m-1700000000", None, None)
        self.assertEqual(end_ts, 1700000300)

    def test_resolve_market_end_time_from_entry_time(self):
        entry_time = "2024-01-01T00:00:00+00:00"
        expected = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()) + 300
        end_ts = pm.resolve_market_end_time("bad-slug", entry_time, None)
        self.assertEqual(end_ts, expected)

    def test_should_attempt_stop_loss(self):
        self.assertTrue(pm.should_attempt_stop_loss(False))
        self.assertFalse(pm.should_attempt_stop_loss(True))
        self.assertFalse(pm.should_attempt_stop_loss(None))

    def test_evaluate_proximity_guard_freezes_inside_deadzone(self):
        info = pm.evaluate_proximity_guard(
            direction_correct=False,
            true_direction_correct=False,
            diff_atr=0.20,
            remaining=150,
            profit_rate=-0.20,
            wrong_streak=1,
            direction_history=[True, False, True, True],
        )

        self.assertTrue(info["in_proximity"])
        self.assertTrue(info["freeze"])
        self.assertFalse(info["released"])

    def test_evaluate_proximity_guard_releases_on_extreme_loss(self):
        info = pm.evaluate_proximity_guard(
            direction_correct=False,
            true_direction_correct=False,
            diff_atr=0.20,
            remaining=150,
            profit_rate=-(pm.PTB_PROXIMITY_EXTREME_STOP + 0.01),
            wrong_streak=1,
            direction_history=[True, False, True, True],
        )

        self.assertTrue(info["in_proximity"])
        self.assertTrue(info["released"])
        self.assertTrue(info["release_by_extreme_stop"])
        self.assertFalse(info["freeze"])

    @patch("position_monitor.subprocess.run")
    def test_check_balance_changed_zero_balance(self, mock_run):
        mock_run.return_value = DummyResult(stdout="token_id=abc balance: 0.00")
        self.assertTrue(pm.check_balance_changed("abc", 5))

    @patch("position_monitor.subprocess.run")
    def test_check_balance_changed_non_zero(self, mock_run):
        mock_run.return_value = DummyResult(stdout="token_id=abc balance: 2.50")
        self.assertFalse(pm.check_balance_changed("abc", 5))

    @patch("position_monitor.subprocess.run")
    def test_check_balance_changed_token_absent_listing(self, mock_run):
        mock_run.return_value = DummyResult(stdout="Token balances:\n- token_id=xyz balance: 1.00")
        self.assertTrue(pm.check_balance_changed("abc", 5))

    @patch("position_monitor.get_current_crypto_price")
    def test_get_settlement_reference_price_prefers_frozen_close_price(self, mock_price):
        mock_price.return_value = 99999.0
        price, source = pm.get_settlement_reference_price({"close_crypto_price": 68671.2}, "BTC")
        self.assertEqual(price, 68671.2)
        self.assertEqual(source, "冻结价")

    @patch("position_monitor.subprocess.run")
    @patch("position_monitor.get_price_to_beat_api", return_value=70351.52373576991)
    def test_get_ptb_from_slug_prefers_crypto_price_api(self, mock_api, mock_run):
        price = pm.get_ptb_from_slug("btc-updown-5m-1774328400")

        self.assertEqual(price, 70351.52373576991)
        mock_api.assert_called_once_with("btc-updown-5m-1774328400", timeout=3)
        mock_run.assert_not_called()

    @patch("position_monitor.get_price_to_beat_api", return_value=None)
    @patch("position_monitor.subprocess.run")
    def test_get_ptb_from_slug_falls_back_to_playwright_subprocess(self, mock_run, mock_api):
        mock_run.return_value = DummyResult(stdout="测试 slug: btc-updown-5m-1774328400\nPTB=70351.52, 耗时=1.0s\n")

        price = pm.get_ptb_from_slug("btc-updown-5m-1774328400")

        self.assertEqual(price, 70351.52)
        mock_api.assert_called_once_with("btc-updown-5m-1774328400", timeout=3)
        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
