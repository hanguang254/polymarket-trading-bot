import unittest

from ai_trader.bayesian_engine import BayesianUpdater


class TestBayesianUpdaterSignals(unittest.TestCase):
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
