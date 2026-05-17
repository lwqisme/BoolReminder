import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drawdown.position_strategy import StrategyLabComputationCancelled
from web.app import app, strategy_lab_jobs, strategy_lab_jobs_lock


class StrategyLabJobsTest(unittest.TestCase):
    def setUp(self):
        with strategy_lab_jobs_lock:
            strategy_lab_jobs.clear()

    def test_job_lifecycle_returns_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("drawdown.strategy_lab_history.strategy_lab_data_dir", return_value=Path(tmpdir)):
                with patch("web.app._run_strategy_score_payload", return_value={"summary": [], "questions": []}):
                    with app.test_client() as client:
                        created = client.post(
                            "/api/strategy-lab/jobs",
                            json={"kind": "score", "payload": {"end": "2026-05-14"}},
                        )
                        self.assertEqual(created.status_code, 202)
                        job = created.get_json()["job"]
                        self.assertEqual(job["kind"], "score")

                        for _ in range(20):
                            status = client.get(f"/api/strategy-lab/jobs/{job['id']}")
                            self.assertEqual(status.status_code, 200)
                            job = status.get_json()["job"]
                            if job["status"] == "succeeded":
                                break
                            time.sleep(0.02)

                        self.assertEqual(job["status"], "succeeded")
                        self.assertEqual(job["data"], {"summary": [], "questions": []})
                        self.assertIn("run_snapshot", job)

                        runs = client.get("/api/strategy-lab/runs")
                        self.assertEqual(runs.status_code, 200)
                        saved_runs = runs.get_json()["runs"]
                        self.assertEqual(len(saved_runs), 1)
                        self.assertEqual(saved_runs[0]["kind"], "score")

                        loaded = client.get(f"/api/strategy-lab/runs/{saved_runs[0]['id']}")
                        self.assertEqual(loaded.status_code, 200)
                        self.assertEqual(loaded.get_json()["run"]["config_payload"]["end"], "2026-05-14")

    def test_unknown_job_kind_is_rejected(self):
        with app.test_client() as client:
            response = client.post(
                "/api/strategy-lab/jobs",
                json={"kind": "unknown", "payload": {}},
            )
        self.assertEqual(response.status_code, 400)

    def test_robust_job_kind_is_supported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("drawdown.strategy_lab_history.strategy_lab_data_dir", return_value=Path(tmpdir)):
                with patch("web.app._run_strategy_robust_payload", return_value={"leaderboard": [], "tasks": []}):
                    with app.test_client() as client:
                        created = client.post(
                            "/api/strategy-lab/jobs",
                            json={"kind": "robust", "payload": {"end": "2026-05-14"}},
                        )
                        self.assertEqual(created.status_code, 202)
                        job_id = created.get_json()["job"]["id"]

                        for _ in range(20):
                            status = client.get(f"/api/strategy-lab/jobs/{job_id}")
                            job = status.get_json()["job"]
                            if job["status"] == "succeeded":
                                break
                            time.sleep(0.02)

                        self.assertEqual(job["status"], "succeeded")
                        self.assertEqual(job["kind"], "robust")
                        self.assertEqual(job["data"], {"leaderboard": [], "tasks": []})

    def test_job_failure_returns_error(self):
        with patch("web.app._run_strategy_scan_payload", side_effect=RuntimeError("network down")):
            with app.test_client() as client:
                created = client.post(
                    "/api/strategy-lab/jobs",
                    json={"kind": "scan", "payload": {"start": "2026-01-01", "end": "2026-05-14"}},
                )
                self.assertEqual(created.status_code, 202)
                job_id = created.get_json()["job"]["id"]

                for _ in range(20):
                    status = client.get(f"/api/strategy-lab/jobs/{job_id}")
                    job = status.get_json()["job"]
                    if job["status"] == "failed":
                        break
                    time.sleep(0.02)

                self.assertEqual(job["status"], "failed")
                self.assertIn("network down", job["error"])

    def test_job_pause_resume_and_cancel_controls_running_task(self):
        def slow_runner(payload):
            job_id = payload.get("_job_id")
            deadline = time.time() + 3
            while time.time() < deadline:
                with strategy_lab_jobs_lock:
                    job = strategy_lab_jobs.get(job_id)
                    if job and job.get("cancel_requested"):
                        raise StrategyLabComputationCancelled("任务已取消。")
                time.sleep(0.02)
            return {"leaderboard": [], "tasks": []}

        with patch("web.app._run_strategy_robust_payload", side_effect=slow_runner):
            with app.test_client() as client:
                created = client.post(
                    "/api/strategy-lab/jobs",
                    json={"kind": "robust", "payload": {"end": "2026-05-14"}},
                )
                self.assertEqual(created.status_code, 202)
                job_id = created.get_json()["job"]["id"]

                running_job = None
                for _ in range(30):
                    status = client.get(f"/api/strategy-lab/jobs/{job_id}")
                    running_job = status.get_json()["job"]
                    if running_job["status"] == "running":
                        break
                    time.sleep(0.02)
                self.assertEqual(running_job["status"], "running")

                paused = client.post(f"/api/strategy-lab/jobs/{job_id}/pause")
                self.assertEqual(paused.status_code, 200)
                paused_job = paused.get_json()["job"]
                self.assertTrue(paused_job["pause_requested"])

                resumed = client.post(f"/api/strategy-lab/jobs/{job_id}/resume")
                self.assertEqual(resumed.status_code, 200)
                resumed_job = resumed.get_json()["job"]
                self.assertFalse(resumed_job["pause_requested"])

                cancelled = client.post(f"/api/strategy-lab/jobs/{job_id}/cancel")
                self.assertEqual(cancelled.status_code, 200)
                self.assertTrue(cancelled.get_json()["job"]["cancel_requested"])

                final_job = None
                for _ in range(30):
                    status = client.get(f"/api/strategy-lab/jobs/{job_id}")
                    final_job = status.get_json()["job"]
                    if final_job["status"] == "cancelled":
                        break
                    time.sleep(0.02)
                self.assertEqual(final_job["status"], "cancelled")
                self.assertIn("取消", final_job["error"])

    def test_cancel_unknown_job_returns_404(self):
        with app.test_client() as client:
            response = client.post("/api/strategy-lab/jobs/not-found/cancel")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
