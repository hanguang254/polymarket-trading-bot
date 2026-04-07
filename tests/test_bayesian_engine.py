import math
import os
import unittest
from unittest.mock import patch

from ai_trader.bayesian_engine import BayesianUpdater


class TestBayesianUpdaterSignals(unittest.TestCase):
    def test_state_signal_matches_inline_gbm_formula_pre_refactor(self):
        """
        Equivalence guard for the gbm_p_up refactor (P1-2 hardened).

        Pre-refactor _state_signal hardcoded `sigma_per_min = atr / 1.5`.
        Post-refactor calls gbm_p_up which reads EV_ATR_SIGMA_RATIO from env
        (default 1.5). This test PINS the env var to 1.5 so equivalence holds
        regardless of runtime config. P1-2 disclosure: setting
        EV_ATR_SIGMA_RATIO != 1.5 will silently change bayesian_engine output.
        """
        ptb = 70787.02
        atr = 55.11714285714489
        price = 70831.95          # gap > 0
        remaining_seconds = 264.0

        with patch.dict(os.environ, {"EV_ATR_SIGMA_RATIO": "1.5"}):
            updater = BayesianUpdater(prior_up=0.5, atr_val=atr)
            updater.current_price = price
            updater.current_ptb = ptb
            state = updater._state_signal(remaining_seconds=remaining_seconds)

        # Reference: inline pre-refactor formula
        sigma_per_min = atr / 1.5
        sigma_total = sigma_per_min * math.sqrt(remaining_seconds / 60.0)
        gap = price - ptb
        z = abs(gap) / sigma_total
        base_p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        expected_p_up = base_p if gap >= 0 else (1.0 - base_p)
        # _signal_from_p_up clips to [0.07, 0.93]
        expected_p_up_clipped = max(0.07, min(0.93, expected_p_up))

        self.assertIsNotNone(state)
        self.assertAlmostEqual(state["p_up"], expected_p_up_clipped, places=12)


    def test_state_signal_overrides_stale_incremental_direction(self):
        ptb = 70787.02
        atr = 55.11714285714489
        prices = [
            70767.67, 70763.93, 70771.02, 70764.88, 70762.50, 70759.90,
            70759.89, 70760.79, 70772.50, 70774.86, 70816.85, 70831.95,
        ]
        updater = BayesianUpdater(prior_up=0.5, atr_val=atr)

        for price in prices:
            updater.update(price, ptb)

        inc_dir, _, inc_conf = updater.get_direction_and_confidence()
        summary = updater.get_summary(remaining_seconds=264)

        self.assertEqual(inc_dir, "DOWN")
        self.assertLess(inc_conf, 0.10)
        self.assertEqual(summary["direction"], "UP")
        self.assertEqual(summary["source"], "state_override")
        self.assertGreater(summary["confidence"], summary["incremental_confidence"])
        self.assertGreater(summary["state_confidence"], summary["incremental_confidence"])

    def test_persistent_gap_flip_triggers_soft_reset_and_realigns_incremental_posterior(self):
        ptb = 70787.02
        atr = 55.11714285714489
        prices = [
            70767.67, 70763.93, 70771.02, 70764.88, 70762.50, 70759.90,
            70759.89, 70760.79, 70772.50, 70774.86, 70816.85, 70831.95,
            70837.26, 70839.69, 70848.24,
        ]
        updater = BayesianUpdater(prior_up=0.5, atr_val=atr)

        for price in prices:
            updater.update(price, ptb)

        inc_dir, inc_p_hat, inc_conf = updater.get_direction_and_confidence()

        self.assertEqual(updater.reset_count, 1)
        self.assertEqual(inc_dir, "UP")
        self.assertGreater(inc_p_hat, 0.75)
        self.assertGreater(inc_conf, 0.50)


if __name__ == "__main__":
    unittest.main()
