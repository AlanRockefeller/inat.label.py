"""Regression tests for review findings on PR #69."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import flask  # noqa: F401
    import app as labels_app
    import inat_shared
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc

APP_DIR = Path(labels_app.app.root_path)


class PrintSubprocessEnvTestCase(unittest.TestCase):
    def test_app_directory_leads_pythonpath(self):
        with labels_app.app.test_request_context("/"):
            labels_app.g.request_id = "r" * 32
            with patch.dict(os.environ, {"PYTHONPATH": "/elsewhere"}):
                env = labels_app._print_subprocess_env("job")
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep),
                         [labels_app.app.root_path, "/elsewhere"])
        self.assertEqual(env["LABELS_PRINT_JOB_ID"], "job")

    def test_shared_module_importable_outside_app_directory(self):
        """The resolved generator directory may not be the app directory."""
        with labels_app.app.test_request_context("/"):
            labels_app.g.request_id = "r" * 32
            env = labels_app._print_subprocess_env("job")
        out = subprocess.run(
            [sys.executable, "-c",
             "import inat_shared, os; print(os.path.dirname(os.path.abspath(inat_shared.__file__)))"],
            env=env, capture_output=True, text=True, check=True, cwd="/")
        self.assertEqual(out.stdout.strip(), str(APP_DIR))


class LoggableJobIdTestCase(unittest.TestCase):
    def test_only_canonical_uuids_are_logged(self):
        job_id = "8abcf066-4497-4637-9d18-c0f1d0f53716"
        self.assertEqual(labels_app._loggable_job_id(job_id), job_id)
        self.assertEqual(labels_app._loggable_job_id(""), "")
        self.assertEqual(labels_app._loggable_job_id(job_id + "\nforged=1"), "invalid")
        self.assertEqual(labels_app._loggable_job_id(job_id.upper()), "invalid")


class CsvThrottleTestCase(unittest.TestCase):
    def test_throttled_batch_returns_retryable_429_not_a_short_csv(self):
        labels_app.app.config["TESTING"] = True
        client = labels_app.app.test_client()
        with patch.object(labels_app, "inat_api_get",
                          side_effect=inat_shared.Throttled(7)):
            response = client.post("/labels/submit", data={"observations[]": ["123"]})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "7")
        self.assertIn("retry", response.get_data(as_text=True))


class MotoinatTestCase(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(APP_DIR))
        import motoinat
        self.motoinat = motoinat
        self.addCleanup(sys.path.remove, str(APP_DIR))

    def response(self, status, payload=None):
        class R:
            status_code = status

            def json(self):
                return payload

        return R()

    def test_failed_query_is_not_reported_as_no_match(self):
        empty = self.response(200, {"total_results": 0, "results": []})
        responses = [self.response(503)] + [empty] * 5
        with patch.object(self.motoinat, "_inat_get", side_effect=responses):
            self.assertFalse(self.motoinat.find_inaturalist_observation("1", number_only=True))

    def test_no_match_after_all_queries_succeed(self):
        empty = self.response(200, {"total_results": 0, "results": []})
        with patch.object(self.motoinat, "_inat_get", return_value=empty):
            self.assertTrue(self.motoinat.find_inaturalist_observation("1", number_only=True))

    def test_requests_have_a_timeout(self):
        with patch.object(self.motoinat, "inat_shared", None), \
                patch.object(self.motoinat.requests, "get",
                             return_value=self.response(200)) as get:
            self.motoinat._inat_get("https://api.inaturalist.org/v1/observations", {})
        self.assertIsNotNone(get.call_args.kwargs.get("timeout"))


if __name__ == "__main__":
    unittest.main()
