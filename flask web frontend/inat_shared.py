"""Cross-process iNaturalist pacing and observation cache (SQLite, stdlib only).

State is private to this app, never under static/. Transactions contain no
network calls or sleeps. Waiting callers recheck the cooldown before admission.
Tests MUST set LABELS_INAT_STATE to a temporary database, including subprocesses.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import os
import sqlite3
import time
from uuid import uuid4

import requests

logger = logging.getLogger("labelmaker.performance")


class Throttled(requests.HTTPError):
    def __init__(self, seconds, response=None):
        self.retry_after = max(1, math.ceil(seconds))
        super().__init__(f'iNaturalist is busy; retry in {self.retry_after} seconds', response=response)


def retry_after(response, default=2):
    value = response.headers.get('Retry-After', '')
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else default
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return default


@contextmanager
def database():
    path = os.environ.get('LABELS_INAT_STATE', os.path.join(os.path.dirname(__file__), '.inat-state.sqlite3'))
    db = sqlite3.connect(path, timeout=2, isolation_level=None)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS pace (id INTEGER PRIMARY KEY, next REAL, cooldown REAL)')
        db.execute('INSERT OR IGNORE INTO pace VALUES (1, 0, 0)')
        db.execute('CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, payload TEXT, expires REAL, owner TEXT, lease REAL)')
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def cooldown(seconds):
    # Never shorten Retry-After to fit a particular request's wait budget.
    with database() as db:
        db.execute('UPDATE pace SET cooldown=max(cooldown, ?) WHERE id=1', (time.time() + seconds,))


def wait_turn(max_wait=2, check=lambda: None, interval=None):
    interval = max(0, float(os.environ.get('INAT_MIN_REQUEST_INTERVAL', '1'))) if interval is None else interval
    deadline = time.monotonic() + max_wait
    started = time.monotonic()
    while True:
        check()
        with database() as db:
            now = time.time()
            next_at, cool = db.execute('SELECT next, cooldown FROM pace WHERE id=1').fetchone()
            delay = max(next_at, cool) - now
            if delay <= 0:
                db.execute('UPDATE pace SET next=? WHERE id=1', (now + interval,))
                return time.monotonic() - started
        if time.monotonic() + max(0, delay) > deadline:
            raise Throttled(delay)
        time.sleep(min(max(delay, .001), .1))


def observations(ids, fetch, max_wait=2, check=lambda: None):
    """Return (raw observations, per-ID failures); coalesce overlapping batches.

    Successful raw observations can be reused by both web and print clients.
    Short failure caching prevents a failed batch fanning out into N retries.
    A lease expires if its fetching process dies. No lock spans upstream I/O.
    """
    pending = list(dict.fromkeys(str(i) for i in ids))
    found, failed = {}, {}
    deadline = time.monotonic() + max_wait
    owner = uuid4().hex
    while pending:
        check()
        claimed = []
        with database() as db:
            now = time.time()
            db.execute('DELETE FROM observations WHERE expires < ? AND lease < ?', (now - 900, now))
            for oid in pending[:]:
                row = db.execute('SELECT payload, expires, owner, lease FROM observations WHERE id=?', (oid,)).fetchone()
                if row and row[0] is not None and row[1] > now:
                    value = json.loads(row[0])
                    if '_failure' in value:
                        failed[oid] = value['_failure']
                    else:
                        found[oid] = value
                    pending.remove(oid)
                elif not row or row[3] <= now:
                    if len(claimed) < 30:
                        db.execute('INSERT OR REPLACE INTO observations VALUES (?, NULL, 0, ?, ?)', (oid, owner, now + 90))
                        claimed.append(oid)
        if not claimed:
            if pending and time.monotonic() >= deadline:
                failed.update({oid: {'status': 503, 'retryable': True, 'retry_after': 2,
                                    'error': 'Observation lookup is already running; retry shortly.'} for oid in pending})
                break
            if pending:
                time.sleep(.05)
            continue
        try:
            data = fetch(claimed)
            if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                raise ValueError('Invalid iNaturalist observation response')
            batch = {str(r['id']): r for r in data['results'] if isinstance(r, dict) and 'id' in r}
            values = {oid: batch.get(oid, {'_failure': {'status': 404, 'retryable': False,
                            'error': f'iNaturalist Observation #{oid} does not exist'}}) for oid in claimed}
        except (requests.RequestException, ValueError) as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            throttled = isinstance(exc, Throttled) or status == 429
            failure = {'status': 429 if throttled else 500, 'retryable': True,
                       'error': ('iNaturalist is limiting requests. Please wait and retry.' if throttled else 'Could not fetch this observation from iNaturalist.'),
                       'retry_after': getattr(exc, 'retry_after', 2)}
            logger.warning('inat_batch_failed ids=%s status=%s error=%s', claimed, failure['status'], type(exc).__name__)
            values = {oid: {'_failure': failure} for oid in claimed}
        except BaseException:
            with database() as db:
                db.execute('DELETE FROM observations WHERE owner=?', (owner,))
            raise
        with database() as db:
            for oid, value in values.items():
                ttl = 2 if '_failure' in value else 900
                db.execute('UPDATE observations SET payload=?, expires=?, lease=0 WHERE id=? AND owner=?',
                           (json.dumps(value), time.time() + ttl, oid, owner))
                if '_failure' in value:
                    failed[oid] = value['_failure']
                else:
                    found[oid] = value
                pending.remove(oid)
            # A fixed cap bounds storage even when many unique IDs are queried.
            db.execute('DELETE FROM observations WHERE id IN (SELECT id FROM observations WHERE lease=0 ORDER BY expires DESC LIMIT -1 OFFSET 5000)')
    return found, failed
