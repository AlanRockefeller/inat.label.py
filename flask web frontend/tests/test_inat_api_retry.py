"""Web traffic publishes cooldown and returns promptly; printers share it."""
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from unittest.mock import Mock

import pytest
import requests

import app as labels_app
import inat_shared


def response(status, headers=None):
    value = Mock(status_code=status, headers=headers or {})
    if status >= 400:
        value.raise_for_status.side_effect = requests.HTTPError(str(status), response=value)
    return value


def test_429_does_not_sleep_or_retry_in_web_thread(monkeypatch):
    get = Mock(return_value=response(429, {'Retry-After': '3'}))
    monkeypatch.setattr(labels_app.requests, 'get', get)
    with pytest.raises(inat_shared.Throttled) as caught:
        labels_app.inat_api_get('https://api.inaturalist.org/v1/observations')
    assert caught.value.retry_after == 3
    with pytest.raises(inat_shared.Throttled):
        inat_shared.wait_turn(max_wait=0)
    assert get.call_count == 1


def test_full_retry_after_survives_short_web_budget(monkeypatch):
    get = Mock(return_value=response(429, {'Retry-After': '3600'}))
    monkeypatch.setattr(labels_app.requests, 'get', get)
    with pytest.raises(inat_shared.Throttled):
        labels_app.inat_api_get('https://api.inaturalist.org/v1/observations')
    with inat_shared.database() as db:
        import time
        assert db.execute('SELECT cooldown FROM pace').fetchone()[0] - time.time() > 3599
    with pytest.raises(inat_shared.Throttled):
        labels_app.inat_api_get('https://api.inaturalist.org/v1/observations')
    assert get.call_count == 1


def test_http_date_and_invalid_retry_after():
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
    assert 58 < inat_shared.retry_after(response(429, {'Retry-After': date})) <= 60
    for invalid in ['nonsense', 'NaN', 'inf', None]:
        assert inat_shared.retry_after(response(429, {'Retry-After': invalid})) == 2


def test_other_errors_are_not_retried(monkeypatch):
    get = Mock(return_value=response(500))
    monkeypatch.setattr(labels_app.requests, 'get', get)
    with pytest.raises(requests.HTTPError):
        labels_app.inat_api_get('https://api.inaturalist.org/v1/observations')
    assert get.call_count == 1
