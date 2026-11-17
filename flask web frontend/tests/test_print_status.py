import io
import json
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as labels_app

JOB_ID = '12345678-1234-4123-8123-123456789abc'


def make_job(tmp_path, code=0, output=None):
    path = tmp_path / 'labels.pdf'
    if output is not None:
        path.write_bytes(output)
    proc = MagicMock()
    proc.stdout = io.StringIO('Generating 2 labels\nNo valid observations found.\n')
    proc.wait.return_value = code
    proc.poll.return_value = code
    return {'proc': proc, 'output_path': str(path), 'filename': 'labels.pdf',
            'started_time': 0, 'observation_count': 2,
            'logs': deque(maxlen=1000), 'log_cursor': 0}


def test_running_status_does_not_probe_or_offer_partial_file(tmp_path):
    job = make_job(tmp_path, output=b'partial')
    with patch.dict(labels_app._jobs, {JOB_ID: job}, clear=True):
        response = labels_app.app.test_client().get('/labels/print_status', query_string={'job_id': JOB_ID})
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.get_json()['status'] == 'running'
    assert 'download_url' not in response.get_json()


def test_collector_publishes_a_finished_nonempty_file_without_a_stream(tmp_path):
    job = make_job(tmp_path, output=b'%PDF finished')
    with patch.dict(labels_app._jobs, {JOB_ID: job}, clear=True):
        labels_app._collect_print_output(JOB_ID, job)
        response = labels_app.app.test_client().get('/labels/print_status', query_string={'job_id': JOB_ID})
    assert response.get_json()['status'] == 'completed'
    assert response.get_json()['download_url'] == f'/labels/static/jobs/{JOB_ID}/labels.pdf'
    assert len(job['logs']) == 2


def test_exit_zero_without_output_explains_unusable_observations(tmp_path):
    job = make_job(tmp_path)
    with patch.object(labels_app, 'log_user_problem') as logged:
        labels_app._collect_print_output(JOB_ID, job)
    assert job['result']['status'] == 'failed'
    assert 'observation IDs' in job['result']['error']
    assert 'Process exited' not in job['result']['error']
    assert logged.call_args.args[0] == 'label_generator_output_missing'


def test_empty_output_is_a_failure(tmp_path):
    job = make_job(tmp_path, output=b'')
    with patch.object(labels_app, 'log_user_problem'):
        labels_app._collect_print_output(JOB_ID, job)
    assert not job['result']['success']


def test_reconnecting_stream_replays_only_new_lines_and_retains_job(tmp_path):
    job = make_job(tmp_path, output=b'%PDF')
    labels_app._collect_print_output(JOB_ID, job)
    with patch.dict(labels_app._jobs, {JOB_ID: job}, clear=True):
        response = labels_app.app.test_client().get('/labels/print_stream', query_string={'job_id': JOB_ID}, headers={'Last-Event-ID': '1'})
        body = response.get_data(as_text=True)
    assert 'Generating 2 labels' not in body
    assert 'No valid observations' in body
    assert 'event: done' in body
    job['proc'].terminate.assert_not_called()


def test_disconnect_does_not_kill_generation(tmp_path):
    job = make_job(tmp_path)
    job['logs'].append((1, 'working'))
    with patch.dict(labels_app._jobs, {JOB_ID: job}, clear=True):
        response = labels_app.app.test_client().get('/labels/print_stream', query_string={'job_id': JOB_ID}, buffered=False)
        next(iter(response.response))
        response.close()
    job['proc'].terminate.assert_not_called()
    assert labels_app._open_streams[0] == 0


def test_unknown_status_reports_a_terminal_error():
    with patch.dict(labels_app._jobs, {}, clear=True):
        response = labels_app.app.test_client().get('/labels/print_status', query_string={'job_id': JOB_ID})
    assert response.status_code == 410
    assert response.get_json()['status'] == 'failed'


def test_real_unicode_field_names_survive_command_construction(tmp_path):
    fields = ['+Odor — Macrofungi', '+Substrate — Fungi', '+Hábitat – hongos']
    with patch.object(labels_app.subprocess, 'Popen', return_value=MagicMock()) as popen, patch.dict(labels_app._jobs, {}, clear=True):
        response = labels_app.app.test_client().post('/labels/print_start', data={
            'format': 'pdf', 'observations[]': ['123'], 'use_custom': 'on', 'custom_args[]': fields,
        })
    assert response.status_code == 200
    cmd = popen.call_args.args[0]
    assert '--custom=' + ', '.join(fields) in cmd


def test_finished_status_and_stream_recover_after_job_is_reaped(tmp_path, monkeypatch):
    directory = tmp_path / 'static' / 'jobs' / JOB_ID
    directory.mkdir(parents=True)
    job = make_job(directory, output=b'%PDF finished')
    monkeypatch.setattr(labels_app, 'PRINT_JOBS_DIR', str(tmp_path / 'static' / 'jobs'))
    labels_app._collect_print_output(JOB_ID, job)
    with patch.dict(labels_app._jobs, {}, clear=True), patch.object(labels_app.subprocess, 'Popen') as spawn:
        client = labels_app.app.test_client()
        response = client.get('/labels/print_status', query_string={'job_id': JOB_ID})
        stream = client.get('/labels/print_stream', query_string={'job_id': JOB_ID})
    assert response.json['status'] == 'completed'
    assert response.json['download_url'].endswith('labels.pdf')
    assert 'event: done' in stream.get_data(as_text=True)
    spawn.assert_not_called()


def test_partial_file_without_completion_marker_is_never_recovered(tmp_path, monkeypatch):
    directory = tmp_path / 'static' / 'jobs' / JOB_ID
    directory.mkdir(parents=True)
    (directory / 'labels.pdf').write_bytes(b'%PDF partial')
    monkeypatch.setattr(labels_app, 'PRINT_JOBS_DIR', str(tmp_path / 'static' / 'jobs'))
    with patch.dict(labels_app._jobs, {}, clear=True):
        response = labels_app.app.test_client().get('/labels/print_status', query_string={'job_id': JOB_ID})
    assert response.status_code == 410
