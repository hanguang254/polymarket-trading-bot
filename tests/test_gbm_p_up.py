"""
Tests for ai_trader.gbm_p_up — the shared GBM closed-form implied probability helper.

Used by both bayesian_engine._state_signal (refactored) and direction_truth_gate.
"""
import math
import unittest

from ai_trader.gbm_p_up import gbm_p_up


class TestGbmPUp(unittest.TestCase):
    def test_zero_gap_returns_half(self):
        """price == strike → P(end > strike) = 0.5 by symmetry."""
        p = gbm_p_up(price=100000.0, strike=100000.0, atr=80.0, remaining_sec=60.0)
        self.assertAlmostEqual(p, 0.5, places=6)

    def test_positive_gap_above_half(self):
        """price > strike → P(UP) > 0.5."""
        p = gbm_p_up(price=100050.0, strike=100000.0, atr=80.0, remaining_sec=60.0)
        self.assertGreater(p, 0.5)
        self.assertLess(p, 1.0)

    def test_negative_gap_below_half(self):
        """price < strike → P(UP) < 0.5."""
        p = gbm_p_up(price=99950.0, strike=100000.0, atr=80.0, remaining_sec=60.0)
        self.assertLess(p, 0.5)
        self.assertGreater(p, 0.0)

    def test_symmetry_around_strike(self):
        """gbm_p_up(strike+gap) + gbm_p_up(strike-gap) == 1.0 (Gaussian symmetry)."""
        gap = 30.0
        p_up = gbm_p_up(price=100000.0 + gap, strike=100000.0, atr=80.0, remaining_sec=60.0)
        p_dn = gbm_p_up(price=100000.0 - gap, strike=100000.0, atr=80.0, remaining_sec=60.0)
        self.assertAlmostEqual(p_up + p_dn, 1.0, places=6)

    def test_large_positive_gap_approaches_one(self):
        """Far above strike → P approaches 1."""
        p = gbm_p_up(price=110000.0, strike=100000.0, atr=80.0, remaining_sec=60.0)
        self.assertGreater(p, 0.99)

    def test_large_negative_gap_approaches_zero(self):
        """Far below strike → P approaches 0."""
        p = gbm_p_up(price=90000.0, strike=100000.0, atr=80.0, remaining_sec=60.0)
        self.assertLess(p, 0.01)

    def test_more_time_compresses_certainty(self):
        """Same gap, more time remaining → less certain (closer to 0.5)."""
        p_short = gbm_p_up(price=100050.0, strike=100000.0, atr=80.0, remaining_sec=10.0)
        p_long = gbm_p_up(price=100050.0, strike=100000.0, atr=80.0, remaining_sec=300.0)
        self.assertGreater(p_short, p_long)
        self.assertGreater(p_long, 0.5)

    def test_higher_atr_compresses_certainty(self):
        """Same gap, higher volatility → less certain."""
        p_low_vol = gbm_p_up(price=100050.0, strike=100000.0, atr=20.0, remaining_sec=60.0)
        p_high_vol = gbm_p_up(price=100050.0, strike=100000.0, atr=200.0, remaining_sec=60.0)
        self.assertGreater(p_low_vol, p_high_vol)

    def test_zero_remaining_returns_terminal(self):
        """remaining_sec <= 0 → P collapses to deterministic terminal (1.0 or 0.0)."""
        p_above = gbm_p_up(price=100001.0, strike=100000.0, atr=80.0, remaining_sec=0.0)
        p_below = gbm_p_up(price=99999.0, strike=100000.0, atr=80.0, remaining_sec=0.0)
        self.assertEqual(p_above, 1.0)
        self.assertEqual(p_below, 0.0)

    def test_zero_atr_returns_terminal(self):
        """atr <= 0 → no uncertainty → deterministic terminal."""
        p_above = gbm_p_up(price=100001.0, strike=100000.0, atr=0.0, remaining_sec=60.0)
        p_below = gbm_p_up(price=99999.0, strike=100000.0, atr=0.0, remaining_sec=60.0)
        self.assertEqual(p_above, 1.0)
        self.assertEqual(p_below, 0.0)

    def test_matches_bayesian_engine_state_signal_formula(self):
        """
        Bit-equivalence with bayesian_engine._state_signal pre-refactor formula:
            sigma_per_min = atr / 1.5
            sigma_total = sigma_per_min * sqrt(remaining_seconds / 60)
            z = |gap| / sigma_total
            base_p = 0.5 * (1 + erf(z / sqrt(2)))
            p_up = base_p if gap >= 0 else 1 - base_p
        """
        price, strike, atr, remaining = 100023.0, 100000.0, 80.0, 87.3
        gap = price - strike

        # Reference computation (copied verbatim from bayesian_engine._state_signal):
        sigma_per_min = atr / 1.5
        sigma_total = sigma_per_min * math.sqrt(remaining / 60.0)
        z = abs(gap) / sigma_total
        base_p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        expected = base_p if gap >= 0 else (1.0 - base_p)

        p = gbm_p_up(price=price, strike=strike, atr=atr, remaining_sec=remaining)
        self.assertAlmostEqual(p, expected, places=12)


if __name__ == "__main__":
    unittest.main()
