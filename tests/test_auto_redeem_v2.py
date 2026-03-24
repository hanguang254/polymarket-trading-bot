import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


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


class TestAutoRedeemBalancesAndDiscovery(unittest.TestCase):
    def test_get_usdc_balances_includes_proxy_and_total(self):
        wallet = type("Wallet", (), {"address": redeem.EOA_WALLET})()
        eoa_cs = redeem.Web3.to_checksum_address(redeem.EOA_WALLET)
        proxy_cs = redeem.Web3.to_checksum_address(redeem.PROXY_WALLET)
        raw_balances = {
            eoa_cs: 0,
            proxy_cs: 9_000_000,
        }

        def balance_of(addr):
            return MagicMock(call=MagicMock(return_value=raw_balances[addr]))

        usdc_contract = MagicMock()
        usdc_contract.functions.balanceOf.side_effect = balance_of

        balances = redeem.get_usdc_balances(wallet, usdc_contract)

        self.assertEqual(balances["eoa"], 0.0)
        self.assertEqual(balances["proxy"], 9.0)
        self.assertEqual(balances["total"], 9.0)

    def test_fetch_positions_queries_active_and_closed_for_both_wallets(self):
        active_side_effect = [
            [{"source": "proxy-active"}],
            [{"source": "eoa-active"}],
        ]
        closed_side_effect = [
            [{"source": "proxy-closed"}],
            [{"source": "eoa-closed"}],
        ]

        with patch.object(redeem, "_fetch_positions_api", side_effect=active_side_effect) as mock_active:
            with patch.object(redeem, "_fetch_closed_positions_api", side_effect=closed_side_effect) as mock_closed:
                positions = redeem.fetch_positions()

        self.assertEqual(
            positions,
            [
                {"source": "proxy-active"},
                {"source": "proxy-closed"},
                {"source": "eoa-active"},
                {"source": "eoa-closed"},
            ],
        )
        self.assertEqual(
            mock_active.call_args_list,
            [
                unittest.mock.call(redeem.PROXY_WALLET, "proxy"),
                unittest.mock.call(redeem.EOA_WALLET, "eoa"),
            ],
        )
        self.assertEqual(
            mock_closed.call_args_list,
            [
                unittest.mock.call(redeem.PROXY_WALLET, "proxy"),
                unittest.mock.call(redeem.EOA_WALLET, "eoa"),
            ],
        )

    def test_find_redeemable_checks_all_tokens_within_same_condition(self):
        wallet = type("Wallet", (), {"address": redeem.EOA_WALLET})()
        eoa_cs = redeem.Web3.to_checksum_address(redeem.EOA_WALLET)

        positions = [
            {
                "conditionId": "0x" + "11" * 32,
                "asset": "101",
                "title": "First token",
                "slug": "first-token",
                "resolved": True,
                "size": 0,
                "currentValue": 0,
            },
            {
                "conditionId": "0x" + "11" * 32,
                "asset": "202",
                "title": "Winning token",
                "slug": "winning-token",
                "resolved": True,
                "size": 7,
                "currentValue": 7,
            },
        ]

        def balance_of(addr, token_id):
            raw = 7_000_000 if (addr, token_id) == (eoa_cs, 202) else 0
            return MagicMock(call=MagicMock(return_value=raw))

        ctf_contract = MagicMock()
        ctf_contract.functions.balanceOf.side_effect = balance_of

        with patch.object(redeem, "fetch_positions", return_value=positions):
            with patch.object(redeem, "check_resolved_onchain", return_value=True):
                redeemable = redeem.find_redeemable(MagicMock(), wallet, ctf_contract)

        self.assertEqual(len(redeemable), 1)
        self.assertEqual(redeemable[0]["token_id"], "202")
        self.assertEqual(redeemable[0]["balance"], 7_000_000)
        self.assertEqual(redeemable[0]["slug"], "Winning token")


if __name__ == "__main__":
    unittest.main()
