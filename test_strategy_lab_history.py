import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drawdown.strategy_lab_history import (
    delete_run_snapshot,
    list_experiment_presets,
    list_run_snapshots,
    load_experiment_preset,
    load_run_snapshot,
    save_experiment_preset,
    save_run_snapshot,
)


class StrategyLabHistoryTest(unittest.TestCase):
    def test_save_list_load_and_delete_run_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("drawdown.strategy_lab_history.strategy_lab_data_dir", return_value=Path(tmpdir)):
                saved = save_run_snapshot(
                    "run",
                    {
                        "start": "2026-01-01",
                        "end": "2026-05-14",
                        "buy_strategies": ["pyramid_3"],
                        "sell_strategies": ["repair_step"],
                        "targets": [{"symbol": "TSM.US", "name": "TSM", "weight": 100}],
                    },
                    {
                        "range": {"start": "2026-01-01", "end": "2026-05-14"},
                        "strategies": [
                            {
                                "label": "三档金字塔 / 阶梯修复卖出",
                                "metrics": {
                                    "return_pct": 12.5,
                                    "max_drawdown_pct": 18.0,
                                },
                            }
                        ],
                        "warnings": [],
                    },
                    job_id="job-1",
                )

                self.assertNotIn("config_payload", saved)
                runs = list_run_snapshots()
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["id"], saved["id"])
                self.assertEqual(runs[0]["result_summary"]["strategy_count"], 1)

                loaded = load_run_snapshot(saved["id"])
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["config_payload"]["targets"][0]["symbol"], "TSM.US")
                self.assertEqual(loaded["config_summary"]["target_count"], 1)

                self.assertTrue(delete_run_snapshot(saved["id"]))
                self.assertEqual(list_run_snapshots(), [])

    def test_list_skips_corrupt_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("drawdown.strategy_lab_history.strategy_lab_data_dir", return_value=Path(tmpdir)):
                runs_path = Path(tmpdir) / "runs"
                runs_path.mkdir(parents=True)
                (runs_path / "20260514010101_deadbeef.json").write_text("{bad", encoding="utf-8")
                save_run_snapshot("score", {}, {"summary": [], "questions": []})

                runs = list_run_snapshots()
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["kind"], "score")

    def test_save_and_load_experiment_preset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("drawdown.strategy_lab_history.strategy_lab_data_dir", return_value=Path(tmpdir)):
                preset = save_experiment_preset(
                    "核心组合",
                    {
                        "start": "2026-01-01",
                        "end": "2026-05-14",
                        "targets": [{"symbol": "TSLA.US", "weight": 50}],
                    },
                )

                self.assertNotIn("config_payload", preset)
                self.assertEqual(list_experiment_presets()[0]["name"], "核心组合")
                loaded = load_experiment_preset(preset["id"])
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["config_payload"]["targets"][0]["symbol"], "TSLA.US")

                with self.assertRaises(ValueError):
                    save_experiment_preset("", {})

    def test_save_and_load_leaps_preset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("drawdown.strategy_lab_history.strategy_lab_data_dir", return_value=Path(tmpdir)):
                leaps_payload = {
                    "type": "leaps",
                    "leaps_note": "test note",
                    "drawdown_threshold_pct": 20,
                    "entry_mode": "both",
                    "stage1_days": 15, "stage1_profit": 80, "stage1_sell": 50,
                    "stage2_days": 60, "stage2_profit": 60, "stage2_sell": 50,
                    "position_pct": 20, "cooldown_days": 5,
                }
                preset = save_experiment_preset("LEAPS test", leaps_payload)
                self.assertNotIn("config_payload", preset)
                loaded = load_experiment_preset(preset["id"])
                self.assertIsNotNone(loaded)
                cfg = loaded["config_payload"]
                self.assertEqual(cfg["type"], "leaps")
                self.assertEqual(cfg["leaps_note"], "test note")
                self.assertEqual(cfg["drawdown_threshold_pct"], 20)
                self.assertEqual(cfg["stage1_days"], 15)


if __name__ == "__main__":
    unittest.main()
