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


if __name__ == "__main__":
    unittest.main()
