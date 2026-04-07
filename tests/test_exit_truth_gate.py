"""
Tests for ai_trader.exit_truth_gate.

The Exit Truth Gate suppresses EV stop-losses on positions where any of three
layers indicate the stop is likely a false positive:

  Layer 1 — P_win floor:    if bayesian P_win > EXIT_GATE_PWIN_FLOOR (default 0.75)
  Layer 2 — Near-expiry:    if remaining_sec < EXIT_GATE_NEAR_EXPIRY_SEC (default 30)
  Layer 3 — DTG consensus:  if direction_truth_gate still confirms position direction

Each layer is OR-combined: ANY one layer suppressing is enough to skip the stop.

Built on the data analysis showing 75% of EV stop-losses on April 6-7 were
false positives (sold winning positions). See conversation analysis for the
3 specific cases.
"""
import unittest
from unittest.mock import patch

from ai_trader.exit_truth_gate import (
    ExitVerdict,
    should_suppress_ev_exit,
)


class TestExitVerdict(unittest.TestCase):
    """Layer 1: P_win floor."""

    def test_p_win_above_floor_suppresses(self):
        """P_win=0.85 > 0.75 → suppress (the ETH 9:45 case)."""
        v = should_suppress_ev_exit(
            coin="ETH", strike=2080.0, atr_val=10.0,
            p_win=0.85, direction="DOWN", remaining_sec=120.0,
        )
        self.assertTrue(v.suppress)
        self.assertEqual(v.layer, "p_win_floor")

    def test_p_win_at_floor_does_not_suppress_via_layer1(self):
        """P_win exactly at floor=0.75 should not trigger layer 1 (strict greater-than)."""
        # Force layers 2 and 3 to NOT suppress so we test layer 1 in isolation
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.75, direction="DOWN", remaining_sec=120.0,
            )
        self.assertFalse(v.suppress)

    def test_p_win_below_floor_falls_through(self):
        """P_win=0.40 → layer 1 fails, falls through to other layers."""
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="BTC", strike=68000.0, atr_val=80.0,
                p_win=0.40, direction="UP", remaining_sec=120.0,
            )
        self.assertFalse(v.suppress)


class TestNearExpiryLayer(unittest.TestCase):
    """Layer 2: near-expiry lockout."""

    def test_remaining_below_threshold_suppresses(self):
        """remaining_sec=20 < 30 → suppress (let dice land)."""
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40,  # below P_win floor, so layer 1 fails
                direction="DOWN", remaining_sec=20.0,
            )
        self.assertTrue(v.suppress)
        self.assertEqual(v.layer, "near_expiry")

    def test_remaining_at_threshold_does_not_suppress_via_layer2(self):
        """remaining=30 → not yet inside lockout window."""
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=30.0,
            )
        self.assertFalse(v.suppress)

    def test_remaining_well_above_threshold_falls_through(self):
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=120.0,
            )
        self.assertFalse(v.suppress)


class TestDtgConsensusLayer(unittest.TestCase):
    """Layer 3: DTG cross-source consensus."""

    def test_dtg_confirms_direction_suppresses(self):
        """DTG truth.direction == position direction → suppress (sources still agree)."""
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=True):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=120.0,
            )
        self.assertTrue(v.suppress)
        self.assertEqual(v.layer, "dtg_consensus")

    def test_dtg_disagrees_does_not_suppress(self):
        """DTG says direction is wrong → let EV stop fire."""
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=120.0,
            )
        self.assertFalse(v.suppress)


class TestLayerPriority(unittest.TestCase):
    """Layers OR together; first-match wins for the layer label."""

    def test_layer1_wins_when_all_three_would_suppress(self):
        """All 3 layers would suppress → layer 1 wins (cheapest evaluated first)."""
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=True):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.90, direction="DOWN", remaining_sec=15.0,
            )
        self.assertTrue(v.suppress)
        self.assertEqual(v.layer, "p_win_floor")

    def test_layer2_when_layer1_fails(self):
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=True):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=15.0,
            )
        self.assertTrue(v.suppress)
        self.assertEqual(v.layer, "near_expiry")

    def test_layer3_when_layers_1_and_2_fail(self):
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=True):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=120.0,
            )
        self.assertTrue(v.suppress)
        self.assertEqual(v.layer, "dtg_consensus")

    def test_no_layer_suppresses_returns_pass_through(self):
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=120.0,
            )
        self.assertFalse(v.suppress)
        self.assertIsNone(v.layer)


class TestEnvOverrides(unittest.TestCase):
    """Env var configuration."""

    def test_disabled_gate_never_suppresses(self):
        """EXIT_GATE_ENABLED=0 → fall through to legacy logic, never suppress."""
        with patch.dict("os.environ", {"EXIT_GATE_ENABLED": "0"}):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.95, direction="DOWN", remaining_sec=10.0,
            )
        self.assertFalse(v.suppress)

    def test_custom_p_win_floor(self):
        """EXIT_GATE_PWIN_FLOOR override raises the bar for layer 1."""
        with patch.dict("os.environ", {"EXIT_GATE_PWIN_FLOOR": "0.90"}), \
             patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            # P_win=0.85 was suppressing with default 0.75; now floor=0.90 → no longer
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.85, direction="DOWN", remaining_sec=120.0,
            )
        self.assertFalse(v.suppress)

    def test_custom_near_expiry_window(self):
        """EXIT_GATE_NEAR_EXPIRY_SEC=60 widens the lockout."""
        with patch.dict("os.environ", {"EXIT_GATE_NEAR_EXPIRY_SEC": "60"}), \
             patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=45.0,
            )
        self.assertTrue(v.suppress)
        self.assertEqual(v.layer, "near_expiry")


class TestDtgBridgeRobustness(unittest.TestCase):
    """P1-1 / P2-5: DTG bridge must fail-open on every error path."""

    def test_dtg_bridge_returns_false_when_dtg_disabled(self):
        """When DIR_TRUTH_GATE_ENABLED=0, DTG returns mode='disabled' direction=None.
        Bridge must NOT suppress in this case (fail-open contract)."""
        from ai_trader.exit_truth_gate import _dtg_says_direction_correct
        with patch.dict("os.environ", {"DIR_TRUTH_GATE_ENABLED": "0"}):
            result = _dtg_says_direction_correct(
                coin="ETH", strike=2080.0, atr_val=10.0,
                direction="DOWN", remaining_sec=120.0, now_ts=1000.0,
            )
        self.assertFalse(result, "DTG disabled → bridge must not claim direction is correct")

    def test_dtg_bridge_returns_false_when_underlying_raises(self):
        """If check_direction_truth raises, bridge must catch and return False."""
        from ai_trader.exit_truth_gate import _dtg_says_direction_correct
        with patch("ai_trader.direction_truth_gate.check_direction_truth",
                   side_effect=RuntimeError("simulated DTG failure")):
            result = _dtg_says_direction_correct(
                coin="ETH", strike=2080.0, atr_val=10.0,
                direction="DOWN", remaining_sec=120.0, now_ts=1000.0,
            )
        self.assertFalse(result, "DTG raise → bridge must fail-open")

    def test_full_gate_with_dtg_disabled_uses_layers_1_2_only(self):
        """End-to-end: gate still works with DTG disabled, just no Layer 3."""
        with patch.dict("os.environ", {"DIR_TRUTH_GATE_ENABLED": "0"}):
            # Layer 1 still works
            v1 = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.85, direction="DOWN", remaining_sec=120.0,
            )
            self.assertTrue(v1.suppress)
            self.assertEqual(v1.layer, "p_win_floor")

            # Layer 2 still works
            v2 = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=20.0,
            )
            self.assertTrue(v2.suppress)
            self.assertEqual(v2.layer, "near_expiry")

            # Layer 3 fails (DTG disabled) → no suppression
            v3 = should_suppress_ev_exit(
                coin="ETH", strike=2080.0, atr_val=10.0,
                p_win=0.40, direction="DOWN", remaining_sec=120.0,
            )
            self.assertFalse(v3.suppress)


class TestRealWorldCases(unittest.TestCase):
    """Regression tests for the 3 actual stop-loss cases found in the data."""

    def test_eth_9_45_p_win_85_should_be_suppressed(self):
        """The ETH 9:45 case from monitor_2026-04-07.log line 9318.

        Bot logged: EV止损(P_win=85%, 0.4ATR) | edge=-0.038 | 2/2轮确认
        Reality: ETH Down won → this stop was a 错杀.
        Expected: P_win=0.85 > floor 0.75 → layer 1 suppresses.
        """
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="ETH", strike=2077.0, atr_val=10.0,
                p_win=0.85, direction="DOWN", remaining_sec=24.0,
            )
        self.assertTrue(v.suppress, "ETH 9:45 P_win=85% must be suppressed")

    def test_btc_p_win_40_real_loss_not_suppressed(self):
        """A truly losing position should NOT be suppressed.

        bot=Up, won=Down, P_win=40% — the bayesian model itself agrees the
        position is losing. No layer should fire.
        """
        with patch("ai_trader.exit_truth_gate._dtg_says_direction_correct", return_value=False):
            v = should_suppress_ev_exit(
                coin="BTC", strike=68000.0, atr_val=80.0,
                p_win=0.40, direction="UP", remaining_sec=120.0,
            )
        self.assertFalse(v.suppress, "P_win=40% real loss must let stop fire")


if __name__ == "__main__":
    unittest.main()
