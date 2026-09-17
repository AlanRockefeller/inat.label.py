"""Tests for the abuse and injection guards on the public endpoints."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

try:
    import flask  # noqa: F401
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc


class LimitsTestCase(unittest.TestCase):
    """Base case that runs with the request limits switched on.

    Most suites set TESTING, which disables them; these tests are about the
    limits themselves, so they turn TESTING back off and restore it afterwards.
    """

    def setUp(self):
        self._previous_testing = labels_app.app.config.get("TESTING")
        labels_app.app.config["TESTING"] = False
        labels_app._rate_limiter.reset()
        labels_app._jobs.clear()
        self.client = labels_app.app.test_client()
        self.addCleanup(labels_app._jobs.clear)
        self.addCleanup(labels_app._rate_limiter.reset)
        self.addCleanup(
            labels_app.app.config.__setitem__, "TESTING", self._previous_testing
        )

    def post(self, path, data=None, ip="203.0.113.5", **kwargs):
        headers = kwargs.pop("headers", {})
        headers.setdefault("X-Real-IP", ip)
        return self.client.post(path, data=data or {}, headers=headers, **kwargs)


class TestTodoSubmissionLimits(LimitsTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp(prefix="labels-todo-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.todo_file = os.path.join(self.tmpdir, "todos.txt")
        patcher = patch.object(
            labels_app, "_todo_file_path", return_value=self.todo_file
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _suggest(self, ip="198.51.100.7", suggestion="Please add a print preview"):
        return self.post(
            "/labels/todo",
            data={"name": "Tester", "suggestion": suggestion},
            ip=ip,
        )

    def test_daily_cap_is_enforced_per_ip(self):
        for attempt in range(10):
            response = self._suggest()
            self.assertEqual(response.status_code, 302, f"attempt {attempt + 1}")

        blocked = self._suggest()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Rate limit reached", blocked.get_data(as_text=True))
        self.assertIn("Retry-After", blocked.headers)

        with open(self.todo_file) as handle:
            self.assertEqual(len(handle.readlines()), 10)

    def test_a_different_ip_has_its_own_allowance(self):
        for _ in range(10):
            self._suggest(ip="198.51.100.7")

        self.assertEqual(self._suggest(ip="198.51.100.8").status_code, 302)

    def test_reading_the_page_is_not_capped(self):
        for _ in range(10):
            self._suggest()

        response = self.client.get(
            "/labels/todo", headers={"X-Real-IP": "198.51.100.7"}
        )
        self.assertEqual(response.status_code, 200)

    def test_long_submissions_are_truncated(self):
        self._suggest(suggestion="A" * 5000)

        with open(self.todo_file) as handle:
            line = handle.read()
        self.assertLessEqual(
            len(line.strip()), labels_app.TODO_MAX_SUGGESTION_LENGTH + 40
        )

    def test_submissions_stop_when_the_file_is_full(self):
        with open(self.todo_file, "w") as handle:
            handle.write("x" * (labels_app.TODO_MAX_FILE_BYTES + 1))

        response = self._suggest()

        self.assertEqual(response.status_code, 507)
        self.assertEqual(
            os.path.getsize(self.todo_file), labels_app.TODO_MAX_FILE_BYTES + 1
        )


class TestCrossSiteWriteBlocking(LimitsTestCase):
    def test_post_from_another_site_is_rejected(self):
        response = self.post(
            "/labels/lookup_batch",
            data={"obs_ids[]": ["12345"]},
            headers={"Origin": "https://evil.example"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("another website", response.get_json()["error"])

    def test_same_origin_post_is_allowed(self):
        with patch.object(labels_app, "lookup_batch_internal", return_value=[]):
            response = self.post(
                "/labels/lookup_batch",
                data={"obs_ids[]": ["12345"]},
                headers={"Origin": "http://localhost"},
            )

        self.assertEqual(response.status_code, 200)

    def test_post_without_an_origin_is_allowed(self):
        """Scripts and curl send no Origin; only browsers need the check."""
        with patch.object(labels_app, "lookup_batch_internal", return_value=[]):
            response = self.post(
                "/labels/lookup_batch", data={"obs_ids[]": ["12345"]}
            )

        self.assertEqual(response.status_code, 200)

    def test_reads_are_never_blocked(self):
        response = self.client.get("/labels", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 200)


class TestResponseHeaders(LimitsTestCase):
    def test_nosniff_and_referrer_policy_are_set(self):
        response = self.client.get("/labels")

        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            response.headers["Referrer-Policy"], "strict-origin-when-cross-origin"
        )

    def test_framing_is_left_open(self):
        """Embedding the app in another site's iframe is supported."""
        response = self.client.get("/labels")

        self.assertNotIn("X-Frame-Options", response.headers)
        self.assertNotIn("Content-Security-Policy", response.headers)


class TestRequestSizeLimit(LimitsTestCase):
    def test_oversized_body_is_refused(self):
        oversized = "9" * (labels_app.MAX_REQUEST_BYTES + 1024)

        response = self.post("/labels/print_start", data={"observations[]": oversized})

        self.assertEqual(response.status_code, 413)
        self.assertIn("too large", response.get_json()["error"])


class TestPrintStartArgumentHandling(LimitsTestCase):
    def _print(self, popen, **extra):
        proc = MagicMock()
        proc.poll.return_value = None
        popen.return_value = proc
        data = {"format": "rtf", "observations[]": ["123"]}
        data.update(extra)
        return self.post("/labels/print_start", data=data)

    @patch("app.subprocess.Popen")
    def test_ids_follow_an_end_of_options_separator(self, popen):
        response = self._print(popen)

        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], ["--", "123"])

    @patch("app.subprocess.Popen")
    def test_ordinary_sort_field_is_accepted(self, popen):
        response = self._print(popen, sort="custom", sort_field="Voucher Number(s)")

        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0]
        self.assertEqual(
            command[command.index("--sort-field") + 1], "Voucher Number(s)"
        )

    @patch("app.subprocess.Popen")
    def test_option_shaped_and_shell_shaped_sort_fields_are_rejected(self, popen):
        for field in ("--file", "-x", "$(id)", "a; id", "`id`", "a|b", "\x00x"):
            with self.subTest(field=field):
                labels_app._rate_limiter.reset()
                response = self._print(popen, sort="custom", sort_field=field)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "Invalid sort field")
                popen.assert_not_called()

    @patch("app.subprocess.Popen")
    def test_custom_fields_are_validated(self, popen):
        for field in ("--file", "--rtf", "$(id)", "x; rm -rf /"):
            with self.subTest(field=field):
                labels_app._rate_limiter.reset()
                response = self._print(
                    popen, use_custom="on", **{"custom_args[]": field}
                )

                self.assertEqual(response.status_code, 400)
                self.assertIn("Invalid custom label field", response.get_json()["error"])
                popen.assert_not_called()

    @patch("app.subprocess.Popen")
    def test_ordinary_custom_fields_are_forwarded(self, popen):
        response = self._print(
            popen, use_custom="on", **{"custom_args[]": "+Habitat"}
        )

        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--custom") + 1], "+Habitat")

    @patch("app.subprocess.Popen")
    def test_too_many_custom_fields_are_refused(self, popen):
        fields = [f"Field {n}" for n in range(labels_app.MAX_CUSTOM_FIELDS + 1)]

        response = self._print(popen, use_custom="on", **{"custom_args[]": fields})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many custom label fields", response.get_json()["error"])
        popen.assert_not_called()


class TestConversionBudget(LimitsTestCase):
    """MO -> iNat conversions each cost a subprocess and an upstream request."""

    @staticmethod
    def _motoinat_success(*_args, **_kwargs):
        result = MagicMock()
        result.stdout = "424242"
        return result

    @patch("app.subprocess.Popen")
    @patch("app.subprocess.run")
    def test_print_start_refuses_more_conversions_than_the_budget(self, run, popen):
        run.side_effect = self._motoinat_success
        proc = MagicMock()
        proc.poll.return_value = None
        popen.return_value = proc
        requested = labels_app.MAX_MO_CONVERSIONS_PER_REQUEST + 5

        response = self.post(
            "/labels/print_start",
            data={
                "format": "rtf",
                "observations[]": [f"motoinat{n}" for n in range(1, requested + 1)],
            },
        )

        self.assertEqual(response.status_code, 429)
        self.assertIn("Mushroom Observer conversions", response.get_json()["error"])
        self.assertEqual(run.call_count, labels_app.MAX_MO_CONVERSIONS_PER_REQUEST)
        popen.assert_not_called()

    @patch("app.subprocess.run")
    def test_lookup_batch_reports_the_overflow_per_row(self, run):
        run.side_effect = self._motoinat_success
        requested = labels_app.MAX_MO_CONVERSIONS_PER_REQUEST + 3

        with patch.object(labels_app, "inat_api_get") as api_get:
            api_get.return_value = MagicMock(json=lambda: {"results": []})
            response = self.post(
                "/labels/lookup_batch",
                data={
                    "obs_ids[]": [f"motoinat{n}" for n in range(1, requested + 1)]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.call_count, labels_app.MAX_MO_CONVERSIONS_PER_REQUEST)
        over_budget = [
            row
            for row in response.get_json()["items"]
            if "Mushroom Observer conversions" in row.get("error", "")
        ]
        self.assertEqual(len(over_budget), 3)

    @patch("app.subprocess.run")
    def test_conversion_timeout_is_reported_without_hanging(self, run):
        run.side_effect = labels_app.subprocess.TimeoutExpired(cmd="motoinat", timeout=45)

        with self.assertRaises(ValueError) as raised:
            labels_app.get_inat_id("motoinat12345")

        self.assertIn("Timed out", str(raised.exception))
        self.assertEqual(
            run.call_args.kwargs["timeout"], labels_app.MOTOINAT_TIMEOUT_SECONDS
        )

    @patch("app.subprocess.run")
    def test_conversion_failure_does_not_leak_subprocess_stderr(self, run):
        run.side_effect = labels_app.subprocess.CalledProcessError(
            returncode=1,
            cmd=["motoinat"],
            stderr="Traceback: /home/labels/labels/motoinat.py failed for token=SECRET",
        )

        with self.assertRaises(ValueError) as raised:
            labels_app.get_inat_id("motoinat12345")

        message = str(raised.exception)
        self.assertNotIn("SECRET", message)
        self.assertNotIn("/home/labels", message)
        self.assertIn("MO #12345", message)


class TestErrorDisclosure(LimitsTestCase):
    def test_upstream_failure_returns_a_reference_not_internals(self):
        failure = labels_app.requests.RequestException(
            "HTTPSConnectionPool(host='api.inaturalist.org') url=/v1/taxa?q=Amanita"
            "&api_token=SECRET (Caused by /home/labels/labels/app.py)"
        )

        with patch.object(labels_app, "inat_api_get", side_effect=failure):
            response = self.post(
                "/labels/find_observations",
                data={
                    "d1": "2026-01-01",
                    "d2": "2026-01-31",
                    "username": "someone",
                    "taxon": "Amanita",
                },
            )

        self.assertEqual(response.status_code, 502)
        payload = response.get_json()
        self.assertNotIn("SECRET", payload["error"])
        self.assertNotIn("/home/labels", payload["error"])
        self.assertNotIn("HTTPSConnectionPool", payload["error"])
        # Still specific: names the operation, the input, and a log reference.
        self.assertIn("Amanita", payload["error"])
        self.assertIn("taxon", payload["error"])
        self.assertEqual(len(payload["reference"]), 8)
        self.assertIn(payload["reference"], payload["error"])


class TestRateLimitBuckets(LimitsTestCase):
    @patch("app.subprocess.Popen")
    def test_print_start_is_capped_per_minute(self, popen):
        proc = MagicMock()
        proc.poll.return_value = None
        popen.return_value = proc
        limit, _window = labels_app.RATE_LIMIT_RULES["print"]

        statuses = []
        for _ in range(limit + 1):
            labels_app._jobs.clear()
            statuses.append(
                self.post(
                    "/labels/print_start",
                    data={"format": "rtf", "observations[]": ["123"]},
                ).status_code
            )

        self.assertEqual(statuses[:limit], [200] * limit)
        self.assertEqual(statuses[-1], 429)

    def test_forwarded_addresses_are_only_trusted_from_the_local_proxy(self):
        with labels_app.app.test_request_context(
            "/labels",
            headers={"X-Real-IP": "198.51.100.9"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        ):
            self.assertEqual(labels_app.client_ip(), "198.51.100.9")

        with labels_app.app.test_request_context(
            "/labels",
            headers={"X-Real-IP": "198.51.100.9"},
            environ_overrides={"REMOTE_ADDR": "203.0.113.1"},
        ):
            self.assertEqual(labels_app.client_ip(), "203.0.113.1")


if __name__ == "__main__":
    unittest.main()
