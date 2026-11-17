"""Actual single-worker/four-thread Gunicorn test with mocked upstream I/O."""
import concurrent.futures
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import requests


def test_status_stays_responsive_with_two_streams_and_blocked_lookup(tmp_path):
    module = tmp_path / 'concurrency_wsgi.py'
    module.write_text('''
import threading
from collections import deque
from types import SimpleNamespace
import app as m
m.app.config['TESTING'] = True
release = threading.Event()
entered = []
def lookup(url, **kw):
    entered.append(1)
    release.wait(8)
    return SimpleNamespace(json=lambda: {'results': [{'id': int(kw['params']['id']), 'taxon': {}, 'user': {}}]})
m.inat_api_get = lookup
for key in ['12345678-1234-4123-8123-123456789abc', '22345678-1234-4123-8123-123456789abc']:
    m._jobs[key] = {'proc': SimpleNamespace(poll=lambda: None), 'logs': deque(),
                    'log_cursor': 0, 'started_time': 0}
@m.app.get('/_test/entered')
def is_entered():
    return {'entered': len(entered)}
@m.app.post('/_test/release')
def finish():
    release.set()
    with m._jobs_changed:
        for job in m._jobs.values():
            job['result'] = {'status': 'completed', 'success': True}
        m._jobs_changed.notify_all()
    return '', 204
app = m.app
''')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root) + os.pathsep + str(tmp_path),
           'LABELS_INAT_STATE': str(tmp_path / 'isolated.sqlite3'), 'LABELS_DISABLE_FILE_LOGGING': '1'}
    process = subprocess.Popen([sys.executable, '-m', 'gunicorn', '--workers', '1', '--threads', '4',
                                '--bind', f'127.0.0.1:{port}', 'concurrency_wsgi:app'],
                               cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base = f'http://127.0.0.1:{port}'
    session = requests.Session()
    session.trust_env = False
    job1, job2 = '12345678-1234-4123-8123-123456789abc', '22345678-1234-4123-8123-123456789abc'
    streams = []
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                response = session.get(base + '/_test/entered', timeout=.5)
                if response.ok:
                    break
            except requests.ConnectionError:
                pass
            assert process.poll() is None, process.stderr.read()
            assert time.monotonic() < deadline
            time.sleep(.05)
        # First stream occupies one thread; second returns a short snapshot.
        stream = session.get(base + '/labels/print_stream', params={'job_id': job1}, stream=True, timeout=2)
        streams.append(stream)
        assert next(stream.iter_lines(chunk_size=1)) == b'retry: 5000'
        snapshot = session.get(base + '/labels/print_stream', params={'job_id': job2}, timeout=2)
        assert snapshot.ok and 'event: progress' in snapshot.text
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            blocked = pool.submit(session.post, base + '/labels/lookup_batch', data={'obs_ids[]': '123'}, timeout=10)
            for _ in range(100):
                if session.get(base + '/_test/entered', timeout=1).json()['entered']:
                    break
                time.sleep(.01)
            assert session.get(base + '/_test/entered', timeout=1).json()['entered']
            second = pool.submit(session.post, base + '/labels/lookup_batch', data={'obs_ids[]': '124'}, timeout=10)
            for _ in range(100):
                if session.get(base + '/_test/entered', timeout=1).json()['entered'] == 2:
                    break
                time.sleep(.01)
            assert session.get(base + '/_test/entered', timeout=1).json()['entered'] == 2
            started = time.monotonic()
            busy = pool.submit(session.post, base + '/labels/lookup_batch', data={'obs_ids[]': '125'}, timeout=2)
            statuses = [pool.submit(session.get, base + '/labels/print_status', params={'job_id': j}, timeout=2) for j in [job1, job2]]
            assert busy.result().status_code == 503
            assert all(task.result().json()['status'] == 'running' for task in statuses)
            assert time.monotonic() - started < 1
            session.post(base + '/_test/release', timeout=2)
            assert blocked.result().json()['items'][0]['inat_id'] == '123'
            assert second.result().json()['items'][0]['inat_id'] == '124'
        assert session.get(base + '/labels/print_status', params={'job_id': job2}, timeout=2).json()['status'] == 'completed'
    finally:
        for stream in streams:
            stream.close()
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
