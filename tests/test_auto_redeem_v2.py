import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with patch.dict(os.environ, {
        "EOA_WALLET": "0x1111111111111111111111111111111111111111",
        "PROXY_WALLET": "0x2222222222222222222222222222222222222222",
        "PRIVATE_KEY": "0xabc123",
    }, clear=False):
        spec.loader.exec_module(module)
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    return module


redeem = _load_module("test_auto_redeem_v2_module", "auto_redeem_v2.py")


class TestAutoRedeemProfitEstimate(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.positions_file = os.path.join(self.tmpdir.name, "positions.jsonl")
        self.positions_patch = patch.object(redeem, "POSITIONS_FILE", self.positions_file)
        self.positions_patch.start()

    def tearDown(self):
        self.positions_patch.stop()
        self.tmpdir.cleanup()

    def _write_positions(self, positions):
        with open(self.positions_file, "w", encoding="utf-8") as f:
            for position in positions:
                f.write(json.dumps(position) + "\n")

    def test_estimate_redeem_profit_uses_actual_redeemed_balance(self):
        self._write_positions([
            {
                "token_id": "token-1",
                "slug": "btc-updown-5m-1774232400",
                "entry_price": 0.96,
                "size": 7.0737,
                "closed": True,
                "exit_price": 1.0,
                "exit_time": "2026-03-23T02:25:46+00:00",
            }
        ])

        profit, matched = redeem.estimate_redeem_profit([
            {
                "token_id": "token-1",
                "market_slug": "btc-updown-5m-1774232400",
                "balance": 7069700,
            }
        ], settled_amount=7.0697)

        self.assertEqual(matched, 1)
        self.assertAlmostEqual(profit, 0.2828, places=4)

    def test_estimate_redeem_profit_falls_back_to_market_slug(self):
        self._write_positions([
            {
                "token_id": "token-2",
                "slug": "btc-updown-5m-1774232700",
                "entry_price": 0.96,
                "size": 12.2553,
                "closed": True,
                "exit_price": 1.0,
                "exit_time": "2026-03-23T02:30:47+00:00",
            }
        ])

        profit, matched = redeem.estimate_redeem_profit([
            {
                "token_id": "",
                "market_slug": "btc-updown-5m-1774232700",
                "balance": 12255300,
            }
        ], settled_amount=12.2553)

        self.assertEqual(matched, 1)
        self.assertAlmostEqual(profit, 0.4902, places=4)


if __name__ == "__main__":
    unittest.main()
