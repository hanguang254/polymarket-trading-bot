import os
import unittest
from unittest.mock import patch

import ai_analyze_v2


class TestDirectionalEnvConfig(unittest.TestCase):
    @patch.object(ai_analyze_v2, "analyze_market")
    @patch.object(ai_analyze_v2, "get_base_rate", return_value=0.60)
    def test_direction_specific_max_buy_price_overrides_global(self, _mock_rate, mock_analyze):
        mock_analyze.return_value = (
            "DOWN",
            0.70,
            {
                "target_odds": 0.82,
                "discount": 0.10,
                "estimated_value": 0.90,
                "price_diff": -100.0,
                "price_diff_pct": -0.20,
                "diff_in_atr": 2.0,
                "atr": 1.0,
                "current_price": 100.0,
                "total_score": 60,
                "trend_15m_alignment": "neutral",
            },
        )

        with patch.dict(os.environ, {
            "MAX_BUY_PRICE": "0.95",
            "MAX_BUY_PRICE_DOWN": "0.79",
            "MIN_EV": "0.04",
            "MIN_CONFIDENCE": "0.50",
        }, clear=False):
            should_bet, direction, _, details = ai_analyze_v2.analyze_and_decide(
                "BTC", 200.0, 0.50, 0.50, "btc-test"
            )

        self.assertEqual(direction, "DOWN")
        self.assertFalse(should_bet)
        self.assertEqual(details["max_buy_price_threshold"], 0.79)

    @patch.object(ai_analyze_v2, "analyze_market")
    @patch.object(ai_analyze_v2, "get_base_rate", return_value=0.60)
    def test_direction_specific_up_can_stay_looser_than_down(self, _mock_rate, mock_analyze):
        mock_analyze.return_value = (
            "UP",
            0.70,
            {
                "target_odds": 0.82,
                "discount": 0.10,
                "estimated_value": 0.90,
                "price_diff": 100.0,
                "price_diff_pct": 0.20,
                "diff_in_atr": 2.0,
                "atr": 1.0,
                "current_price": 300.0,
                "total_score": 60,
                "trend_15m_alignment": "neutral",
            },
        )

        with patch.dict(os.environ, {
            "MAX_BUY_PRICE": "0.95",
            "MAX_BUY_PRICE_UP": "0.85",
            "MAX_BUY_PRICE_DOWN": "0.79",
            "MIN_EV": "0.04",
            "MIN_CONFIDENCE": "0.50",
        }, clear=False):
            should_bet, direction, _, details = ai_analyze_v2.analyze_and_decide(
                "BTC", 200.0, 0.50, 0.50, "btc-test"
            )

        self.assertEqual(direction, "UP")
        self.assertTrue(should_bet)
        self.assertEqual(details["max_buy_price_threshold"], 0.85)


if __name__ == "__main__":
    unittest.main()
