import importlib.util
import json
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


ai_analyze_v2 = _load_module("test_ai_analyze_v2_module", "ai_analyze_v2.py")


class _ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        class _DoneFuture:
            def __init__(self, value):
                self._value = value

            def result(self, timeout=None):
                return self._value

        return _DoneFuture(fn(*args, **kwargs))


class TestAdaptiveEntryHelpers(unittest.TestCase):
    def test_plan_fok_entry_reduces_size_when_cap_depth_is_too_shallow(self):
        asks = [
            {"price": "0.85", "size": "2"},
            {"price": "0.86", "size": "2"},
            {"price": "0.87", "size": "1.9"},
            {"price": "0.93", "size": "20"},
        ]

        plan = ai_analyze_v2._plan_fok_entry(
            asks=asks,
            desired_size=7,
            best_ask=0.85,
            price_cap=0.92,
            ev=0.04,
            min_size=5,
        )

        self.assertIsNotNone(plan)
        self.assertEqual(plan["mode"], "reduced")
        self.assertEqual(plan["size"], 5.9)
        self.assertEqual(plan["limit_price"], 0.88)


class TestExecuteBetAdaptiveRetry(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.prev_cwd = os.getcwd()
        os.chdir(self.tmpdir.name)
        os.makedirs("logs", exist_ok=True)

    def tearDown(self):
        os.chdir(self.prev_cwd)
        self.tmpdir.cleanup()

    def test_execute_bet_requotes_after_explicit_fok_kill_without_ghost_check(self):
        initial_quote = {
            "best_bid": 0.84,
            "best_ask": 0.85,
            "bids": [{"price": "0.84", "size": "100"}],
            "asks": [{"price": "0.85", "size": "7"}],
            "bid_depth": 100.0,
            "source": "rest_book",
            "age_ms": 0.0,
        }
        retry_quote = {
            "best_bid": 0.90,
            "best_ask": 0.91,
            "bids": [{"price": "0.90", "size": "100"}],
            "asks": [{"price": "0.91", "size": "9"}],
            "bid_depth": 100.0,
            "source": "rest_book",
            "age_ms": 0.0,
        }
        explicit_fok_error = {
            "matched": False,
            "status": "ERROR",
            "elapsed_ms": 610,
            "error": "PolyApiException[status_code=400, error_message={'error': \"order couldn't be fully filled. FOK orders are fully filled or killed.\"}]",
            "raw": "PolyApiException[status_code=400, error_message={'error': \"order couldn't be fully filled. FOK orders are fully filled or killed.\"}]",
            "making": 0,
            "taking": 0,
            "order_id": None,
        }
        matched_retry = {
            "matched": True,
            "status": "matched",
            "elapsed_ms": 615,
            "error": "",
            "raw": "{'status': 'matched'}",
            "making": 6.44,
            "taking": 7.0,
            "order_id": "oid-1",
        }

        with patch.dict(os.environ, {
            "MIN_BALANCE": "5",
            "MIN_BET_SIZE": "5",
            "MAX_BUY_PRICE": "0.99",
            "MAX_BUY_PRICE_UP": "0.99",
            "MAX_BUY_PRICE_DOWN": "0.99",
            "ENTRY_WS_MAX_AGE_MS": "400",
        }, clear=False):
            with patch.object(ai_analyze_v2, "_bet_executor", _ImmediateExecutor()):
                with patch.object(ai_analyze_v2, "_get_execution_quote", side_effect=[initial_quote, retry_quote]):
                    with patch.object(ai_analyze_v2, "calculate_kelly_size", return_value={
                        "gross_order_size": 7.0,
                        "min_gross_size": 5.0,
                        "raw_net_size": 7.0,
                        "target_net_size": 7.0,
                        "expected_net_size": 7.0,
                        "forced_to_min": False,
                        "entry_fee_rate": 0.0,
                        "skip_reason": None,
                    }):
                        with patch.object(ai_analyze_v2, "_detect_ghost_fill", return_value=None) as ghost_mock:
                            with patch.object(ai_analyze_v2.clob_client, "get_orderbook", return_value=None):
                                with patch.object(ai_analyze_v2.clob_client, "place_fok_order", side_effect=[explicit_fok_error, matched_retry]) as place_mock:
                                    with patch.object(ai_analyze_v2.clob_client, "get_token_balance", return_value=7.0):
                                        with patch.object(ai_analyze_v2.clob_client, "update_token_allowance", return_value=True):
                                            success, actual_price, actual_size, _ = ai_analyze_v2.execute_bet(
                                                slug="eth-updown-5m-test",
                                                direction="DOWN",
                                                token_id="token-1",
                                                confidence=0.66,
                                                ev=0.08,
                                                p_hat=0.75,
                                                entry_details={"p_win_final": 0.95},
                                                pre_balance=100.0,
                                            )

        self.assertTrue(success)
        self.assertEqual(actual_price, 0.92)
        self.assertEqual(actual_size, 7.0)
        ghost_mock.assert_not_called()
        self.assertEqual(place_mock.call_count, 2)
        self.assertEqual(place_mock.call_args_list[0].args[2:], (0.86, 7))
        self.assertEqual(place_mock.call_args_list[1].args[2:], (0.92, 7))

        with open("logs/bets.jsonl") as f:
            record = json.loads(f.readline())

        self.assertEqual(record["quoted_price"], 0.85)
        self.assertEqual(record["limit_price"], 0.92)
        self.assertEqual(record["requested_size"], 7)
        self.assertEqual(record["requested_net_size"], 7)
        self.assertTrue(record["success"])

    def test_execute_bet_uses_net_balance_for_fee_aware_entry_price(self):
        quote = {
            "best_bid": 0.70,
            "best_ask": 0.71,
            "bids": [{"price": "0.70", "size": "100"}],
            "asks": [{"price": "0.71", "size": "20"}],
            "bid_depth": 100.0,
            "source": "rest_book",
            "age_ms": 0.0,
        }
        matched = {
            "matched": True,
            "status": "matched",
            "elapsed_ms": 120,
            "error": "",
            "raw": "{'status': 'matched'}",
            "making": 5.039998,
            "taking": 7.098589,
            "order_id": "oid-2",
        }

        with patch.dict(os.environ, {
            "MIN_BALANCE": "5",
            "MIN_BET_SIZE": "5",
            "MAX_BUY_PRICE": "0.99",
            "MAX_BUY_PRICE_UP": "0.99",
            "MAX_BUY_PRICE_DOWN": "0.99",
            "ENTRY_WS_MAX_AGE_MS": "400",
        }, clear=False):
            with patch.object(ai_analyze_v2, "_bet_executor", _ImmediateExecutor()):
                with patch.object(ai_analyze_v2, "_get_execution_quote", return_value=quote):
                    with patch.object(ai_analyze_v2, "calculate_kelly_size", return_value={
                        "gross_order_size": 7.0,
                        "min_gross_size": 5.1,
                        "raw_net_size": 6.8,
                        "target_net_size": 7.0,
                        "expected_net_size": 6.935,
                        "forced_to_min": False,
                        "entry_fee_rate": 0.0092,
                        "skip_reason": None,
                    }):
                        with patch.object(ai_analyze_v2.clob_client, "place_fok_order", return_value=matched):
                            with patch.object(ai_analyze_v2.clob_client, "get_token_balance", return_value=7.033124):
                                with patch.object(ai_analyze_v2.clob_client, "get_fee_rate_bps", return_value=2500):
                                    with patch.object(ai_analyze_v2.clob_client, "update_token_allowance", return_value=True):
                                        success, actual_price, actual_size, _ = ai_analyze_v2.execute_bet(
                                            slug="btc-updown-5m-fee-aware",
                                            direction="UP",
                                            token_id="token-2",
                                            confidence=0.71,
                                            ev=0.10,
                                            p_hat=0.80,
                                            entry_details={"p_win_final": 0.92},
                                            pre_balance=100.0,
                                        )

        self.assertTrue(success)
        self.assertAlmostEqual(actual_size, 7.033124, places=6)
        self.assertAlmostEqual(actual_price, 5.039998 / 7.033124, places=6)

        with open("logs/bets.jsonl") as f:
            record = json.loads(f.readline())

        self.assertAlmostEqual(record["size"], 7.033124, places=6)
        self.assertAlmostEqual(record["gross_size"], 7.0986, places=4)
        self.assertEqual(record["requested_net_size"], 7.0)
        self.assertGreater(record["buy_fee_shares"], 0.06)


if __name__ == "__main__":
    unittest.main()
