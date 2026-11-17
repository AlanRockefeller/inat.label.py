"""Edge identity survives background collection without exposing job clients."""
import io
import json
import logging
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

import app as labels_app
from job_diagnostics import FailureExcerpt

JOB_ID = "12345678-1234-4123-8123-123456789abc"
EDGE_ID = "0123456789abcdef0123456789abcdef"


def private_events(caplog):
    events = []
    for record in caplog.records:
        if record.name == "labelmaker.performance" and record.getMessage().startswith('{"'):
            events.append(json.loads(record.getMessage()))
    return events


@pytest.mark.parametrize("peer,header,trusted", [
    ("127.0.0.1", EDGE_ID, True), ("::1", EDGE_ID, True),
    ("198.51.100.5", EDGE_ID, False), ("127.0.0.1", "forged-id", False),
    ("127.0.0.1", "f" * 4096, False),
])
def test_only_valid_edge_ids_from_loopback_are_used(peer, header, trusted):
    with patch.object(labels_app.user_problem_logger, "warning") as warning:
        response = labels_app.app.test_client().post(
            "/labels/lookup_batch", data={}, headers={"X-Request-ID": header},
            environ_overrides={"REMOTE_ADDR": peer})
    chosen = response.headers["X-Request-ID"]
    assert len(chosen) == 32
    assert (chosen == EDGE_ID) is trusted
    assert json.loads(warning.call_args.args[0])["request_id"] == chosen


def test_launch_and_background_failure_share_durable_private_identity(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="labelmaker.performance")
    monkeypatch.setattr(labels_app, "PRINT_JOBS_DIR", str(tmp_path))
    proc = MagicMock()
    proc.stdout = io.StringIO(
        "Observer: private person\nLatitude: 12.34\ntoken=SECRET\n"
        "Traceback (most recent call last):\nRuntimeError: password=SECRET\n"
        "HTTP error 429: https://example.test/?api_key=SECRET\n")
    proc.wait.return_value = 2
    with patch.dict(labels_app._jobs, {}, clear=True), \
            patch.object(labels_app.subprocess, "Popen", return_value=proc), \
            patch.object(labels_app.user_problem_logger, "warning") as problem:
        response = labels_app.app.test_client().post("/labels/print_start", data={
            "format": "pdf", "observations[]": ["123"],
        }, headers={"X-Request-ID": EDGE_ID, "X-Real-IP": "198.51.100.7"})
        assert response.status_code == 200
        job_id = response.json["job_id"]
        job = labels_app._jobs[job_id]
        assert job["client_ip"] == "198.51.100.7"
        labels_app._collect_print_output(job_id, job)
        assert job["result"]["status"] == "failed"
        marker = json.loads((tmp_path / job_id / "result.json").read_text())
        # This file is publicly accessible and must contain no actor metadata.
        assert "client_ip" not in marker and "request_id" not in marker
        status = labels_app.app.test_client().get("/labels/print_status", query_string={"job_id": job_id})
        assert "client_ip" not in status.json and "request_id" not in status.json
        failure = json.loads(problem.call_args.args[0])
        assert failure["request_id"] == EDGE_ID
        assert failure["client_ip"] == "198.51.100.7"
    events = private_events(caplog)
    assert [e["event"] for e in events] == ["label_job_started", "label_job_finished"]
    for event in events:
        assert event["request_id"] == EDGE_ID
        assert event["client_ip"] == "198.51.100.7"
        assert event["job_id"] == job_id
    terminal = events[-1]
    assert terminal["exit_code"] == 2 and terminal["output_bytes"] == 0
    assert terminal["reason"] == "nonzero_exit"
    assert "upstream HTTP 429" in terminal["failure_excerpt"]
    assert terminal["failure_excerpt_redacted"]
    assert terminal["suppressed_output_lines"] == 3
    serialized = json.dumps(events)
    for secret in ("SECRET", "private person", "12.34", "example.test", "password"):
        assert secret not in serialized


def test_success_and_nonzero_with_output_record_actual_bytes(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="labelmaker.performance")
    output = tmp_path / "labels.pdf"
    output.write_bytes(b"%PDF test")
    for code in (0, 2):
        proc = MagicMock(stdout=io.StringIO("secret label contents\n"))
        proc.wait.return_value = code
        job = {"proc": proc, "output_path": str(output), "filename": "labels.pdf",
               "request_id": EDGE_ID, "client_ip": "198.51.100.7",
               "logs": deque(maxlen=1000), "log_cursor": 0}
        labels_app._collect_print_output(JOB_ID, job)
        terminal = private_events(caplog)[-1]
        assert terminal["status"] == "completed"
        assert terminal["output_bytes"] == len(b"%PDF test")
        assert terminal["exit_code"] == code
        assert ("failure_excerpt" in terminal) is bool(code)
        assert "secret label contents" not in json.dumps(terminal)


def test_launch_failure_has_terminal_event_and_selected_request_id(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="labelmaker.performance")
    monkeypatch.setattr(labels_app, "PRINT_JOBS_DIR", str(tmp_path))
    monkeypatch.setitem(labels_app.app.config, "TESTING", False)
    with patch.object(labels_app.subprocess, "Popen", side_effect=OSError("SECRET")):
        response = labels_app.app.test_client().post("/labels/print_start", data={
            "format": "pdf", "observations[]": ["123"]}, headers={"X-Request-ID": EDGE_ID})
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == EDGE_ID
    assert [e["event"] for e in private_events(caplog)] == ["label_job_started", "label_job_finished"]
    assert private_events(caplog)[-1]["reason"] == "launch_failed"
    assert "SECRET" not in json.dumps(private_events(caplog))


def test_excerpt_is_fixed_size_and_never_copies_arbitrary_output():
    excerpt = FailureExcerpt()
    for i in range(1000):
        excerpt.add("HTTP error 500: token=" + str(i) + "X" * 10000)
        excerpt.add("Traceback (most recent call last):")
        excerpt.add("user secret=" + str(i))
    fields = excerpt.fields()
    assert len(fields["failure_excerpt"]) <= 512
    assert fields["suppressed_output_lines"] == 1000
    assert "secret" not in json.dumps(fields) and "token" not in json.dumps(fields)


def test_terminal_diagnostic_survives_lost_job_state_and_marker_failure(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="labelmaker.performance")
    private_log = tmp_path / "private-performance.log"
    handler = logging.FileHandler(private_log)
    labels_app.performance_logger.addHandler(handler)
    try:
        proc = MagicMock()
        proc.stdout.readline.side_effect = OSError("SECRET stream failure")
        proc.poll.return_value = None
        proc.wait.return_value = -15
        job = {"proc": proc, "output_path": str(tmp_path / "missing-directory" / "labels.pdf"),
               "filename": "labels.pdf", "request_id": EDGE_ID, "client_ip": "198.51.100.7",
               "logs": deque(maxlen=1000), "log_cursor": 0}
        labels_app._collect_print_output(JOB_ID, job)
        proc.terminate.assert_called_once()
        assert job["result"]["status"] == "failed"
        job.clear()  # Collector state can vanish; log evidence must remain.
    finally:
        labels_app.performance_logger.removeHandler(handler)
        handler.close()
    events = [json.loads(line) for line in private_log.read_text().splitlines() if line.startswith('{"')]
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "label_job_finished" and event["reason"] == "collector_error"
    assert event["exit_code"] == -15
    assert event["result_persisted"] is False
    assert event["client_ip"] == "198.51.100.7" and event["request_id"] == EDGE_ID
    assert "SECRET" not in json.dumps(event)
