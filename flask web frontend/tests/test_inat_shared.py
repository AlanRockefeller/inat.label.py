import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest
import requests

import app as labels
import inat_shared


def raw(oid):
    return {'id': int(oid), 'taxon': {'name': 'Amanita example', 'iconic_taxon_name': 'Fungi'},
            'user': {'login': 'test'}, 'ofvs': []}


def test_cooldown_imposed_while_waiting_is_rechecked():
    inat_shared.wait_turn(interval=.1)
    admitted = threading.Event()
    def waiter():
        inat_shared.wait_turn(max_wait=1, interval=0)
        admitted.set()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(waiter)
        time.sleep(.03)
        inat_shared.cooldown(.25)
        assert not admitted.wait(.15)
        future.result(timeout=1)
    assert admitted.is_set()


def test_pacing_and_cooldown_cross_process(monkeypatch):
    monkeypatch.setenv('INAT_MIN_REQUEST_INTERVAL', '.08')
    script = '''import inat_shared, time
inat_shared.wait_turn(max_wait=3)
print(time.time(), flush=True)
'''
    children = [subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, text=True) for _ in range(3)]
    times = sorted(float(child.communicate(timeout=5)[0]) for child in children)
    assert all(child.returncode == 0 for child in children)
    assert all(b-a >= .07 for a,b in zip(times, times[1:]))
    inat_shared.cooldown(60)
    result = subprocess.run([sys.executable, '-c', 'import inat_shared; inat_shared.wait_turn(max_wait=0)'], capture_output=True, text=True)
    assert result.returncode != 0 and 'Throttled' in result.stderr


def test_overlapping_batches_share_raw_observations_across_web_and_print(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def fetch(ids):
        entered.set()
        assert release.wait(2)
        return {'results': [raw(i) for i in ids]}
    api = Mock(side_effect=lambda *a, **kw: Mock(json=lambda: fetch(kw['params']['id'].split(','))))
    monkeypatch.setattr(labels, 'inat_api_get', api)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        web = pool.submit(labels.lookup_batch_internal, ['123', '124', '123'])
        assert entered.wait(1)
        printer = pool.submit(inat_shared.observations, ['124'], lambda ids: pytest.fail('duplicate fetch'))
        release.set()
        assert len(web.result(timeout=2)['items']) == 3
        found, failed = printer.result(timeout=2)
        assert found['124']['id'] == 124 and not failed
    assert api.call_count == 1
    # An independent generator process must reuse the same successful data.
    script = '''import importlib.util
s=importlib.util.spec_from_file_location('printer', 'inat.label.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.fetch_api_data=lambda *a, **k: (_ for _ in ()).throw(AssertionError('duplicate upstream'))
o, taxon=m.get_observation_data('123')
assert o['id']==123 and taxon=='Fungi'
'''
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)


def test_failed_batch_does_not_fan_out_and_successes_survive(monkeypatch):
    inat_shared.observations(['1'], lambda ids: {'results': [raw(1)]})
    def fail(ids):
        raise inat_shared.Throttled(60)
    fetch = Mock(side_effect=fail)
    found, failed = inat_shared.observations(['1', '2', '3'], fetch)
    assert found['1']['id'] == 1
    assert set(failed) == {'2', '3'} and failed['2']['status'] == 429
    inat_shared.observations(['2'], fetch)
    assert fetch.call_count == 1
    monkeypatch.setattr(labels, 'inat_api_get', Mock(side_effect=fail))
    items = labels.lookup_batch_internal(['1', '2', '3'])['items']
    assert items[0]['inat_id'] == '1'
    assert items[1]['status'] == 429 and items[1]['retryable'] and items[1]['retry_after'] == 60


def test_batch_is_deduplicated_and_chunked():
    fetch = Mock(side_effect=lambda ids: {'results': [raw(i) for i in ids]})
    found, errors = inat_shared.observations(list(range(1, 65)) * 2, fetch)
    assert len(found) == 64 and not errors
    assert [len(call.args[0]) for call in fetch.call_args_list] == [30, 30, 4]


def test_lease_recovers_after_crashed_fetcher():
    with inat_shared.database() as db:
        db.execute('INSERT INTO observations VALUES (?, NULL, 0, ?, ?)', ('123', 'dead', time.time() - 1))
    found, errors = inat_shared.observations(['123'], lambda ids: {'results': [raw(123)]})
    assert found and not errors


def test_superseded_search_stops_after_inflight_page(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        entered.set()
        assert release.wait(2)
        return Mock(status_code=200, json=lambda: {'results': [raw(1)], 'total_results': 1000})
    monkeypatch.setattr(labels.requests, 'get', get)
    headers = {'X-Search-Channel': 'testtab_observations', 'X-Search-Sequence': '1'}
    def search():
        return labels.app.test_client().post('/labels/find_observations', headers=headers, data={
            'd1': '2026-01-01', 'd2': '2026-10-26', 'username': 'test', 'taxon': '1'})
    with concurrent.futures.ThreadPoolExecutor() as pool:
        task = pool.submit(search)
        assert entered.wait(1)
        response = labels.app.test_client().post('/labels/cancel_search', headers={**headers, 'X-Search-Sequence': '2'})
        assert response.status_code == 204
        release.set()
        response = task.result(timeout=2)
    assert response.status_code == 409 and response.json['superseded']
    assert len(calls) == 1


def test_superseded_waiter_never_sends_upstream(monkeypatch):
    with labels.app.test_request_context(headers={'X-Search-Channel': 'testtab_fields', 'X-Search-Sequence': '1'}):
        labels._register_search()
        labels._search_versions[labels.g.search_key] = (2, time.monotonic())
        get = Mock()
        monkeypatch.setattr(labels.requests, 'get', get)
        with pytest.raises(labels.SearchSuperseded):
            labels.inat_api_get('https://api.inaturalist.org/v1/observations')
        get.assert_not_called()


def test_print_adapter_cannot_retry_429_outside_shared_pacer():
    script = """import importlib.util
s=importlib.util.spec_from_file_location('printer', 'inat.label.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
retry=m.get_session().adapters['https://'].max_retries
assert retry.total == 0 and not retry.respect_retry_after_header
"""
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)


def test_generator_batches_before_rendering_and_preserves_duplicates(monkeypatch):
    module = labels.inat_label_module()
    monkeypatch.setattr(module, '_prefetched_observations', {})
    monkeypatch.setattr(module, '_prefetched_failures', {})
    fetched = Mock(side_effect=lambda ids: {'results': [raw(i) for i in ids]})
    monkeypatch.setattr(module, '_fetch_observation_batch', fetched)
    monkeypatch.setattr(module, '_process_one', lambda index, *args: ('err', (index, 'test rendering')))
    failed = []
    module._process_observation_ids(['123', '123', '124'], Mock(workers=2), [], [], [], failed)
    assert fetched.call_count == 1 and fetched.call_args.args[0] == ['123', '124']
    assert len(failed) == 3
    monkeypatch.setattr(module, '_prefetched_observations', {})
    monkeypatch.setattr(module, '_prefetched_failures', {})


def test_print_429_waits_for_shared_retry_after_then_succeeds():
    script = '''import importlib.util
from types import SimpleNamespace
import inat_shared
s=importlib.util.spec_from_file_location('printer', 'inat.label.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
class Clock:
    now=10000.
    def time(self): return self.now
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds
clock=Clock()
inat_shared.time=clock
attempts=[]
def get(*a, **k):
    attempts.append(clock.now)
    if len(attempts)==1:
        return SimpleNamespace(status_code=429, headers={'Retry-After':'3'})
    return SimpleNamespace(status_code=200, text='json', json=lambda:{'results':[]})
m.get_session=lambda:SimpleNamespace(get=get)
data, error=m.fetch_api_data('https://api.inaturalist.org/v1/observations/123')
assert not error and data=={'results':[]}
assert len(attempts)==2 and attempts[1]-attempts[0]>=3
'''
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)
