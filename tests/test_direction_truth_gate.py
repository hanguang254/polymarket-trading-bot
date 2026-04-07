"""
Tests for ai_trader.direction_truth_gate.

Covers all 9 rows of the spec's decision table (Section 5.2 of
docs/superpowers/specs/2026-04-07-direction-truth-gate-design.md):

| n_alive | UP | DOWN | NEU | strict      | balanced       |
|---------|----|------|-----|-------------|----------------|
| 3       | 3  | 0    | 0   | UP          | UP             |
| 3       | 2  | 0    | 1   | BLOCK       | UP (2/3)       |
| 3       | 2  | 1    | 0   | BLOCK       | UP (2/3)       |
| 3       | 1  | 1    | 1   | BLOCK       | BLOCK          |
| 3       | 0  | 0    | 3   | BLOCK       | BLOCK          |
| 2       | 2  | 0    | 0   | UP (degr)   | UP (degraded)  |
| 2       | 1  | 1    | 0   | BLOCK       | BLOCK          |
| 2       | 1  | 0    | 1   | BLOCK       | BLOCK          |
| ≤1      | *  | *    | *   | BLOCK       | BLOCK          |

Tests inject SourceVote objects directly via the public _aggregate_votes()
helper, then a separate suite tests check_direction_truth() end-to-end with
fake stream snapshots.
"""
import unittest
from unittest.mock import patch

from ai_trader.direction_truth_gate import (
    DirectionTruth,
    SourceVote,
    _aggregate_votes,
    check_direction_truth,
)


def _vote(source, vote, p_up=0.6, stale=False, age=0.5):
    return SourceVote(
        source=source,
        price=100000.0,
        p_up=p_up,
        vote=vote,
        stale=stale,
        age_sec=age,
    )


def _stale_vote(source):
    return SourceVote(
        source=source,
        price=100000.0,
        p_up=None,
        vote=None,
        stale=True,
        age_sec=999.0,
    )


class TestAggregateVotes(unittest.TestCase):
    """Decision-table coverage."""

    # ── n_alive = 3 ──

    def test_3of3_up_passes_strict(self):
        votes = {
            "CL": _vote("CL", "UP"),
            "BN": _vote("BN", "UP"),
            "Pyth": _vote("Pyth", "UP"),
        }
        r = _aggregate_votes(votes, strict=True)
        self.assertEqual(r.direction, "UP")
        self.assertEqual(r.mode, "strict")
        self.assertEqual(r.n_alive_sources, 3)
        self.assertEqual(r.n_agreeing, 3)

    def test_3of3_up_passes_balanced(self):
        votes = {
            "CL": _vote("CL", "UP"),
            "BN": _vote("BN", "UP"),
            "Pyth": _vote("Pyth", "UP"),
        }
        r = _aggregate_votes(votes, strict=False)
        self.assertEqual(r.direction, "UP")
        self.assertEqual(r.mode, "balanced")

    def test_2of3_up_with_neutral_blocks_strict_passes_balanced(self):
        votes = {
            "CL": _vote("CL", "UP"),
            "BN": _vote("BN", "UP"),
            "Pyth": _vote("Pyth", "NEUTRAL"),
        }
        self.assertIsNone(_aggregate_votes(votes, strict=True).direction)
        balanced = _aggregate_votes(votes, strict=False)
        self.assertEqual(balanced.direction, "UP")
        self.assertEqual(balanced.n_agreeing, 2)

    def test_2of3_up_with_one_down_blocks_strict_passes_balanced(self):
        votes = {
            "CL": _vote("CL", "UP"),
            "BN": _vote("BN", "UP"),
            "Pyth": _vote("Pyth", "DOWN"),
        }
        self.assertIsNone(_aggregate_votes(votes, strict=True).direction)
        balanced = _aggregate_votes(votes, strict=False)
        self.assertEqual(balanced.direction, "UP")

    def test_split_1_1_1_blocks_both_modes(self):
        votes = {
            "CL": _vote("CL", "UP"),
            "BN": _vote("BN", "DOWN"),
            "Pyth": _vote("Pyth", "NEUTRAL"),
        }
        self.assertIsNone(_aggregate_votes(votes, strict=True).direction)
        self.assertIsNone(_aggregate_votes(votes, strict=False).direction)

    def test_all_neutral_blocks_both_modes(self):
        votes = {
            "CL": _vote("CL", "NEUTRAL"),
            "BN": _vote("BN", "NEUTRAL"),
            "Pyth": _vote("Pyth", "NEUTRAL"),
        }
        self.assertIsNone(_aggregate_votes(votes, strict=True).direction)
        self.assertIsNone(_aggregate_votes(votes, strict=False).direction)

    # ── n_alive = 2 (1 stale source) ──

    def test_degraded_2_alive_both_up_passes_both_modes(self):
        votes = {
            "CL": _vote("CL", "UP"),
            "BN": _vote("BN", "UP"),
            "Pyth": _stale_vote("Pyth"),
        }
        strict_r = _aggregate_votes(votes, strict=True)
        self.assertEqual(strict_r.direction, "UP")
        # P2-1 fix: when n_alive < 3, mode is "degraded" even in strict (matches spec §5.2 row 6)
        self.assertEqual(strict_r.mode, "degraded")

        bal_r = _aggregate_votes(votes, strict=False)
        self.assertEqual(bal_r.direction, "UP")
        self.assertEqual(bal_r.mode, "degraded")
        self.assertEqual(bal_r.n_alive_sources, 2)

    def test_degraded_split_blocks_both(self):
        votes = {
            "CL": _vote("CL", "UP"),
            "BN": _vote("BN", "DOWN"),
            "Pyth": _stale_vote("Pyth"),
        }
        self.assertIsNone(_aggregate_votes(votes, strict=True).direction)
        self.assertIsNone(_aggregate_votes(votes, strict=False).direction)

    def test_degraded_one_neutral_blocks_both(self):
        votes = {
            "CL": _vote("CL", "UP"),
            "BN": _vote("BN", "NEUTRAL"),
            "Pyth": _stale_vote("Pyth"),
        }
        self.assertIsNone(_aggregate_votes(votes, strict=True).direction)
        self.assertIsNone(_aggregate_votes(votes, strict=False).direction)

    # ── n_alive ≤ 1 ──

    def test_only_one_alive_blocks(self):
        votes = {
            "CL": _vote("CL", "UP"),
            "BN": _stale_vote("BN"),
            "Pyth": _stale_vote("Pyth"),
        }
        for strict in (True, False):
            r = _aggregate_votes(votes, strict=strict)
            self.assertIsNone(r.direction)
            self.assertEqual(r.n_alive_sources, 1)
            self.assertIn("live", r.block_reason)

    def test_zero_alive_blocks(self):
        votes = {
            "CL": _stale_vote("CL"),
            "BN": _stale_vote("BN"),
            "Pyth": _stale_vote("Pyth"),
        }
        r = _aggregate_votes(votes, strict=False)
        self.assertIsNone(r.direction)
        self.assertEqual(r.n_alive_sources, 0)

    # ── DOWN symmetry ──

    def test_3of3_down_passes(self):
        votes = {
            "CL": _vote("CL", "DOWN", p_up=0.3),
            "BN": _vote("BN", "DOWN", p_up=0.3),
            "Pyth": _vote("Pyth", "DOWN", p_up=0.3),
        }
        r = _aggregate_votes(votes, strict=True)
        self.assertEqual(r.direction, "DOWN")
        self.assertEqual(r.n_agreeing, 3)

    def test_2of3_down_passes_balanced(self):
        votes = {
            "CL": _vote("CL", "DOWN", p_up=0.3),
            "BN": _vote("BN", "DOWN", p_up=0.3),
            "Pyth": _vote("Pyth", "UP", p_up=0.6),
        }
        r = _aggregate_votes(votes, strict=False)
        self.assertEqual(r.direction, "DOWN")

    # ── Confidence ──

    def test_confidence_uses_only_agreeing_sources(self):
        """Confidence comes from mean p_up of agreeing votes, not all alive."""
        votes = {
            "CL": _vote("CL", "UP", p_up=0.70),
            "BN": _vote("BN", "UP", p_up=0.70),
            "Pyth": _vote("Pyth", "DOWN", p_up=0.30),  # outvoted
        }
        r = _aggregate_votes(votes, strict=False)
        self.assertEqual(r.direction, "UP")
        # Confidence = |0.70 - 0.5| * 2 * shrinkage(0.85) = 0.40 * 0.85 = 0.34
        self.assertAlmostEqual(r.confidence, 0.34, places=3)


class TestCheckDirectionTruth(unittest.TestCase):
    """End-to-end tests with mocked price streams."""

    def _make_snapshot(self, price, age_ms=500):
        return {
            "coin": "BTC",
            "price": price,
            "age_ms": age_ms,
            "stale": False,
            "last_update_ts": 0,
        }

    def test_three_aligned_sources_pass_balanced(self):
        """All 3 sources above strike → balanced mode → UP."""
        snapshots = {
            "CL": self._make_snapshot(100050.0),
            "BN": self._make_snapshot(100055.0),
            "Pyth": self._make_snapshot(100048.0),
        }
        with patch("ai_trader.direction_truth_gate._fetch_snapshots", return_value=snapshots):
            r = check_direction_truth(
                coin="BTC",
                strike=100000.0,
                deadline_ts=1000.0,
                atr_val=80.0,
                strict=False,
                now_ts=940.0,  # remaining = 60s
            )
        self.assertEqual(r.direction, "UP")
        self.assertEqual(r.n_alive_sources, 3)

    def test_one_stale_source_degrades_to_2of2(self):
        """Pyth stale → balanced runs in degraded mode, requires 2/2."""
        snapshots = {
            "CL": self._make_snapshot(100050.0),
            "BN": self._make_snapshot(100055.0),
            "Pyth": self._make_snapshot(100048.0, age_ms=120_000),  # 120s = stale
        }
        with patch("ai_trader.direction_truth_gate._fetch_snapshots", return_value=snapshots):
            r = check_direction_truth(
                coin="BTC",
                strike=100000.0,
                deadline_ts=1000.0,
                atr_val=80.0,
                strict=False,
                now_ts=940.0,
            )
        self.assertEqual(r.direction, "UP")
        self.assertEqual(r.mode, "degraded")
        self.assertTrue(r.votes["Pyth"].stale)

    def test_two_stale_sources_blocks(self):
        snapshots = {
            "CL": self._make_snapshot(100050.0),
            "BN": self._make_snapshot(100055.0, age_ms=120_000),
            "Pyth": self._make_snapshot(100048.0, age_ms=120_000),
        }
        with patch("ai_trader.direction_truth_gate._fetch_snapshots", return_value=snapshots):
            r = check_direction_truth(
                coin="BTC", strike=100000.0, deadline_ts=1000.0,
                atr_val=80.0, strict=False, now_ts=940.0,
            )
        self.assertIsNone(r.direction)
        self.assertIn("live", r.block_reason)

    def test_strict_mode_blocks_on_2of3_consensus(self):
        """Sniper/Endgame mode: 2/3 not enough, needs unanimous live."""
        snapshots = {
            "CL": self._make_snapshot(100050.0),
            "BN": self._make_snapshot(100055.0),
            "Pyth": self._make_snapshot(99950.0),  # disagrees
        }
        with patch("ai_trader.direction_truth_gate._fetch_snapshots", return_value=snapshots):
            r_strict = check_direction_truth(
                coin="BTC", strike=100000.0, deadline_ts=1000.0,
                atr_val=80.0, strict=True, now_ts=940.0,
            )
            r_bal = check_direction_truth(
                coin="BTC", strike=100000.0, deadline_ts=1000.0,
                atr_val=80.0, strict=False, now_ts=940.0,
            )
        self.assertIsNone(r_strict.direction)
        self.assertEqual(r_bal.direction, "UP")  # 2/3 majority OK in balanced

    def test_neutral_zone_when_too_close_to_strike(self):
        """Tiny gaps → all sources NEUTRAL → block."""
        # gap = 1, atr = 80, remaining = 60s → p_up very close to 0.5 → NEUTRAL
        snapshots = {
            "CL": self._make_snapshot(100001.0),
            "BN": self._make_snapshot(100001.5),
            "Pyth": self._make_snapshot(100000.5),
        }
        with patch("ai_trader.direction_truth_gate._fetch_snapshots", return_value=snapshots):
            r = check_direction_truth(
                coin="BTC", strike=100000.0, deadline_ts=1000.0,
                atr_val=80.0, strict=False, now_ts=940.0,
            )
        # p_up should be very near 0.5 → NEUTRAL → no consensus → block
        self.assertIsNone(r.direction)

    def test_disabled_gate_returns_passthrough(self):
        """When DIR_TRUTH_GATE_ENABLED=0, gate returns passthrough (direction=None mode='disabled')."""
        with patch.dict("os.environ", {"DIR_TRUTH_GATE_ENABLED": "0"}):
            r = check_direction_truth(
                coin="BTC", strike=100000.0, deadline_ts=1000.0,
                atr_val=80.0, strict=False, now_ts=940.0,
            )
        self.assertEqual(r.mode, "disabled")
        # P0-2 fix: disabled gate must NOT count as "blocked" — callers should fall through
        self.assertFalse(
            r.is_blocked,
            "Disabled gate must report is_blocked=False so callers fall through to legacy logic",
        )


class TestP0Regression(unittest.TestCase):
    """Tests guarding against the two P0 defects found in code review v15.0."""

    def test_p0_1_all_three_stream_singletons_importable_by_name(self):
        """P0-1 guard: gate must reach all 3 stream singletons by their actual export names.

        Pre-fix bug: gate imported `binance_stream` but the actual singleton is `price_stream`.
        The exception was swallowed silently, leaving Binance permanently dead.
        """
        from ai_trader.polymarket_rtds import chainlink_stream
        from ai_trader.binance_api import price_stream
        from ai_trader.pyth_api import pyth_stream
        self.assertIsNotNone(chainlink_stream)
        self.assertIsNotNone(price_stream)
        self.assertIsNotNone(pyth_stream)

    def test_p0_1_fetch_snapshots_actually_reaches_each_stream(self):
        """P0-1 guard: _fetch_snapshots must actually CALL each stream's get_snapshot.

        Pre-fix bug: gate imported `binance_stream` (wrong name), ImportError was
        swallowed, BN slot was always None even when stream had data.

        Strategy: monkeypatch each singleton's get_snapshot to return a marker,
        then verify the marker propagates through _fetch_snapshots. If the gate
        imports the wrong name, the patch never applies and the assertion fails.
        """
        from ai_trader import direction_truth_gate as gate
        from ai_trader.polymarket_rtds import chainlink_stream
        from ai_trader.binance_api import price_stream
        from ai_trader.pyth_api import pyth_stream

        cl_marker = {"price": 11111.0, "age_ms": 100, "_marker": "CL"}
        bn_marker = {"price": 22222.0, "age_ms": 100, "_marker": "BN"}
        py_marker = {"price": 33333.0, "age_ms": 100, "_marker": "Pyth"}

        with patch.object(chainlink_stream, "get_snapshot", return_value=cl_marker), \
             patch.object(price_stream, "get_snapshot", return_value=bn_marker), \
             patch.object(pyth_stream, "get_snapshot", return_value=py_marker):
            snapshots = gate._fetch_snapshots("BTC")

        self.assertEqual(snapshots["CL"]["_marker"], "CL")
        self.assertEqual(snapshots["BN"]["_marker"], "BN", "Binance source must reach price_stream singleton")
        self.assertEqual(snapshots["Pyth"]["_marker"], "Pyth")

    def test_p0_2_disabled_gate_does_not_block_at_call_site_predicate(self):
        """P0-2 guard: when DIR_TRUTH_GATE_ENABLED=0, callers must not see is_blocked=True."""
        with patch.dict("os.environ", {"DIR_TRUTH_GATE_ENABLED": "0", "DIR_TRUTH_SHADOW": "0"}):
            r = check_direction_truth(
                coin="BTC", strike=100000.0, deadline_ts=1000.0,
                atr_val=80.0, strict=False, now_ts=940.0,
            )
        self.assertFalse(r.is_blocked)

    def test_p0_2_actual_blocks_still_report_blocked(self):
        """Post-fix sanity: real no-consensus blocks must still report is_blocked=True."""
        snapshots = {
            "CL": {"coin": "BTC", "price": 100000.0, "age_ms": 500, "stale": False, "last_update_ts": 0},
            "BN": {"coin": "BTC", "price": 100000.0, "age_ms": 500, "stale": False, "last_update_ts": 0},
            "Pyth": {"coin": "BTC", "price": 100000.0, "age_ms": 500, "stale": False, "last_update_ts": 0},
        }
        with patch("ai_trader.direction_truth_gate._fetch_snapshots", return_value=snapshots):
            r = check_direction_truth(
                coin="BTC", strike=100000.0, deadline_ts=1000.0,
                atr_val=80.0, strict=False, now_ts=940.0,
            )
        self.assertIsNone(r.direction)
        self.assertTrue(r.is_blocked)
        self.assertNotEqual(r.mode, "disabled")


if __name__ == "__main__":
    unittest.main()
