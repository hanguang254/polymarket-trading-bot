import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "ai_trader" / "base_rate.py"
SPEC = importlib.util.spec_from_file_location("test_base_rate_module", MODULE_PATH)
br = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(br)


class TestBaseRateCalibrate(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmpdir.name)
        self.orig_outcomes = br.OUTCOMES_FILE
        self.orig_rates = br.BASE_RATES_FILE
        self.orig_cache = {"rates": br._cache.get("rates"), "loaded_at": br._cache.get("loaded_at", 0)}
        br.OUTCOMES_FILE = str(self.log_dir / "outcomes.jsonl")
        br.BASE_RATES_FILE = str(self.log_dir / "base_rates.json")
        br._cache["rates"] = None
        br._cache["loaded_at"] = 0

    def tearDown(self):
        br.OUTCOMES_FILE = self.orig_outcomes
        br.BASE_RATES_FILE = self.orig_rates
        br._cache["rates"] = self.orig_cache["rates"]
        br._cache["loaded_at"] = self.orig_cache["loaded_at"]
        self.tmpdir.cleanup()

    def _write_rows(self, rows):
        with open(br.OUTCOMES_FILE, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def test_calibrate_skips_early_exit_rows_and_prefers_directional_won(self):
        self._write_rows([
            {
                "atr_band": "1.0-1.5",
                "won": False,
                "directional_won": None,
                "calibration_eligible": False,
            },
            {
                "atr_band": "1.0-1.5",
                "won": False,
                "directional_won": True,
                "calibration_eligible": True,
            },
            {
                "atr_band": "1.0-1.5",
                "won": True,
                "directional_won": False,
                "calibration_eligible": True,
            },
            {
                "atr_band": "2.0-3.0",
                "won": True,
            },
        ])

        result = br.calibrate()

        self.assertEqual(result["1.0-1.5"]["n"], 2)
        self.assertEqual(result["1.0-1.5"]["wins"], 1)
        self.assertAlmostEqual(result["1.0-1.5"]["rate"], 0.5, places=4)
        self.assertEqual(result["2.0-3.0"]["n"], 1)
        self.assertEqual(result["2.0-3.0"]["wins"], 1)


if __name__ == "__main__":
    unittest.main()
