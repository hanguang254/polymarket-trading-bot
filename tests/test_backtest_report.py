import json
import tempfile
import unittest
from pathlib import Path

import backtest


class TestBacktestReport(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_jsonl(self, name, rows):
        path = self.log_dir / name
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def test_build_report_uses_realized_pnl_and_funnel_metrics(self):
        self._write_jsonl("decisions_v2.jsonl", [
            {
                "timestamp": "2026-03-22T00:00:01+00:00",
                "slug": "btc-updown-5m-1",
                "coin": "BTC",
                "direction": "UP",
                "confidence": 0.82,
                "ev": 0.12,
                "action": "BET",
            },
            {
                "timestamp": "2026-03-22T00:05:01+00:00",
                "slug": "eth-updown-5m-2",
                "coin": "ETH",
                "direction": "DOWN",
                "confidence": 0.74,
                "ev": 0.08,
                "action": "BET",
            },
            {
                "timestamp": "2026-03-22T00:10:01+00:00",
                "slug": "btc-updown-5m-3",
                "coin": "BTC",
                "direction": "DOWN",
                "confidence": 0.40,
                "ev": -0.01,
                "action": "SKIP",
            },
        ])

        self._write_jsonl("bets.jsonl", [
            {
                "timestamp": "2026-03-22T00:00:05+00:00",
                "slug": "btc-updown-5m-1",
                "direction": "UP",
                "success": True,
                "pending": False,
            },
            {
                "timestamp": "2026-03-22T00:05:05+00:00",
                "slug": "eth-updown-5m-2",
                "direction": "DOWN",
                "success": False,
                "pending": True,
            },
        ])

        self._write_jsonl("outcomes.jsonl", [
            {
                "timestamp": "2026-03-22T00:03:00Z",
                "slug": "btc-updown-5m-1",
                "direction": "UP",
                "entry_price": 0.50,
                "exit_price": 0.90,
                "size": 4,
                "realized_pnl": 1.6,
                "atr_band": "1.0-1.5",
            },
            {
                "timestamp": "2026-03-22T00:08:00Z",
                "slug": "eth-updown-5m-2",
                "direction": "DOWN",
                "entry_price": 0.90,
                "exit_price": 0.70,
                "size": 5,
                "atr_band": "1.5-2.0",
            },
        ])

        report = backtest.build_report(str(self.log_dir))
        summary = report["summary"]

        self.assertEqual(summary["total_decisions"], 3)
        self.assertEqual(summary["bet_signals"], 2)
        self.assertEqual(summary["order_attempts"], 2)
        self.assertEqual(summary["immediate_fills"], 1)
        self.assertEqual(summary["pending_orders"], 1)
        self.assertEqual(summary["realized_trades"], 2)
        self.assertAlmostEqual(summary["gross_pnl"], 0.6, places=4)
        self.assertAlmostEqual(summary["capital_deployed"], 6.5, places=4)
        self.assertEqual(summary["estimated_pnl_trades"], 1)

        entry_modes = {row["entry_mode"]: row for row in report["breakdown"]["by_entry_mode"]}
        self.assertIn("immediate_fill", entry_modes)
        self.assertIn("pending_fill", entry_modes)
        self.assertAlmostEqual(entry_modes["pending_fill"]["gross_pnl"], -1.0, places=4)


if __name__ == "__main__":
    unittest.main()
