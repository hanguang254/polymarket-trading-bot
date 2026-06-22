import tempfile
import unittest
from pathlib import Path


class TestStrategyGrid(unittest.TestCase):
    def test_normalizes_exec_price_and_filters_grid(self):
        from ai_trader.strategy_grid import evaluate_grid, normalize_decision

        records = [
            {"direction": "UP", "ev": 0.03, "confidence": 0.5, "diff_in_atr": 1.2, "exec_price": 0.62},
            {"direction": "DOWN", "ev": 0.01, "confidence": 0.5, "diff_in_atr": 1.2, "exec_price": 0.62},
            {"direction": "UP", "ev": 0.04, "confidence": 0.2, "diff_in_atr": 1.2, "exec_price": 0.62},
        ]
        samples = [normalize_decision(record) for record in records]

        results = evaluate_grid(
            [sample for sample in samples if sample is not None],
            max_prices=[0.65],
            min_evs=[0.02],
            min_atrs=[1.0],
            min_confidences=[0.35],
            target_rate=0.5,
        )

        self.assertEqual(results[0]["accepted"], 1)
        self.assertAlmostEqual(results[0]["rate"], 1 / 3)
        self.assertAlmostEqual(results[0]["avg_ev"], 0.03)

    def test_falls_back_to_directional_odds_when_exec_price_missing(self):
        from ai_trader.strategy_grid import normalize_decision

        sample = normalize_decision({
            "direction": "DOWN",
            "ev": 0.04,
            "confidence": 0.6,
            "diff_in_atr": 1.1,
            "up_odds": 0.2,
            "down_odds": 0.8,
        })

        self.assertIsNotNone(sample)
        self.assertEqual(sample.price, 0.8)
        self.assertEqual(sample.price_source, "direction_odds")

    def test_load_decision_samples_ignores_bad_json_lines(self):
        from ai_trader.strategy_grid import load_decision_samples

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(
                '{"direction":"UP","ev":0.04,"confidence":0.6,"diff_in_atr":1.1,"exec_price":0.6}\n'
                'not json\n'
                '{"direction":"UP","ev":"bad","confidence":0.6,"diff_in_atr":1.1,"exec_price":0.6}\n',
                encoding="utf-8",
            )

            samples, stats = load_decision_samples(path)

        self.assertEqual(len(samples), 1)
        self.assertEqual(stats["bad_json"], 1)
        self.assertEqual(stats["unusable"], 1)


if __name__ == "__main__":
    unittest.main()
