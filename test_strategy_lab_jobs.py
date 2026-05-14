import time
import unittest
from unittest.mock import patch

from web.app import app


class StrategyLabJobsTest(unittest.TestCase):
    def test_job_lifecycle_returns_result(self):
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

    def test_unknown_job_kind_is_rejected(self):
        with app.test_client() as client:
            response = client.post(
                "/api/strategy-lab/jobs",
                json={"kind": "unknown", "payload": {}},
            )
        self.assertEqual(response.status_code, 400)

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


if __name__ == "__main__":
    unittest.main()
