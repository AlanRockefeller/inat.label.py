"""Telling users when an observation ID and a taxon ID have been swapped.

On 2026-10-17 a user typed observation 403642387 under Taxon; iNaturalist
answered 422 and the app reported a bare 502 four times.  On 2026-10-10 and
2026-10-21 jobs were started for "1", "14" and "15", which are taxon IDs
(Animalia, Cariama cristata, Chunga) with no observation behind them.
"""
import io
import os
from collections import deque
from unittest.mock import MagicMock, patch

import pytest
import requests

os.environ.setdefault("LABELS_DISABLE_FILE_LOGGING", "1")

import app as labels_app

JOB_ID = '12345678-1234-4123-8123-123456789abc'
OBSERVATION = {
    "id": 403642387,
    "taxon": {"id": 48419, "name": "Amanita", "rank": "genus"},
    "user": {"login": "lenrely"},
}
CHUNGA = {"id": 15, "name": "Chunga", "rank": "genus"}


def _response(results):
    response = MagicMock()
    response.json.return_value = {"results": results}
    return response


def _fake_inat(taxa=(), observation=None, search_status=422, observations=()):
    """Dispatch inat_api_get by URL the way iNaturalist answers it."""
    calls = []

    def fake(url, **kwargs):
        params = kwargs.get("params") or {}
        calls.append((url, params))
        if url.endswith("/v1/observations") and "user_login" in params:
            raise requests.exceptions.HTTPError(
                f"{search_status} Client Error",
                response=MagicMock(status_code=search_status),
            )
        if url.endswith("/v1/observations"):
            return _response(list(observations))
        if "/v1/taxa/" in url:
            return _response(list(taxa))
        if "/v1/observations/" in url:
            return _response([observation] if observation else [])
        raise AssertionError(f"unexpected iNaturalist call {url}")

    fake.calls = calls
    return fake


def _find(taxon):
    labels_app.app.config["TESTING"] = True
    return labels_app.app.test_client().post("/labels/find_observations", data={
        "d1": "2026-09-01", "d2": "2026-10-26", "username": "lenrely",
        "taxon": taxon, "source": "inat", "date_mode": "observed",
    })


# --- an observation ID typed under Taxon -------------------------------------

def test_observation_id_under_taxon_names_the_observation_and_its_taxon():
    with patch.object(labels_app, "inat_api_get", _fake_inat(observation=OBSERVATION)), \
            patch.object(labels_app, "log_user_problem") as logged:
        response = _find("403642387")
    assert response.status_code == 400
    error = response.get_json()["error"]
    assert "403642387 is an iNaturalist observation ID, not a taxon ID" in error
    assert "observation of Amanita by lenrely" in error
    assert "taxon is Amanita (genus), ID 48419" in error
    assert "add 403642387 to the observation list" in error
    assert logged.call_args_list[0].args[0] == "taxon_id_is_observation_id"


def test_unidentified_observation_under_taxon_still_names_its_observer():
    unidentified = {"id": 403642387, "taxon": None, "user": {"login": "lenrely"}}
    with patch.object(labels_app, "inat_api_get", _fake_inat(observation=unidentified)):
        error = _find("403642387").get_json()["error"]
    assert "not a taxon ID (it is an observation by lenrely). Enter" in error
    assert "that observation's taxon" not in error


def test_number_that_is_neither_taxon_nor_observation_says_so():
    with patch.object(labels_app, "inat_api_get", _fake_inat()):
        response = _find("999999999")
    assert response.status_code == 400
    assert response.get_json()["error"].startswith("No iNaturalist taxon has ID 999999999")


def test_real_taxon_rejected_for_another_reason_keeps_the_upstream_error():
    with patch.object(labels_app, "inat_api_get", _fake_inat(taxa=[CHUNGA])):
        response = _find("15")
    assert response.status_code == 502
    assert "iNaturalist returned HTTP 422" in response.get_json()["error"]


def test_failed_check_keeps_the_upstream_error():
    fake = _fake_inat()

    def flaky(url, **kwargs):
        if "/v1/taxa/" in url:
            raise requests.exceptions.Timeout("slow")
        return fake(url, **kwargs)

    with patch.object(labels_app, "inat_api_get", flaky):
        response = _find("403642387")
    assert response.status_code == 502


def test_other_upstream_failures_skip_the_check():
    fake = _fake_inat(observation=OBSERVATION, search_status=503)
    with patch.object(labels_app, "inat_api_get", fake):
        response = _find("403642387")
    assert response.status_code == 502
    assert not any("/v1/taxa/" in url for url, _ in fake.calls)


# --- a taxon ID typed into the observation list ------------------------------

def test_lookup_of_missing_observation_that_is_a_taxon_explains_it():
    with patch.object(labels_app, "inat_api_get", _fake_inat(taxa=[CHUNGA])), \
            patch.object(labels_app, "log_user_problem") as logged:
        item = labels_app.lookup_batch_internal(["15"])["items"][0]
    assert item["status"] == 404
    assert item["error"].startswith("iNaturalist Observation #15 does not exist. ")
    assert "15 is the iNaturalist taxon ID for Chunga (genus)" in item["error"]
    assert logged.call_args.args[0] == "observation_id_is_taxon_id"


def test_lookup_of_missing_observation_url_is_not_second_guessed():
    fake = _fake_inat(taxa=[CHUNGA])
    with patch.object(labels_app, "inat_api_get", fake):
        item = labels_app.lookup_batch_internal(
            ["https://www.inaturalist.org/observations/15"]
        )["items"][0]
    assert item["error"] == "iNaturalist Observation #15 does not exist"
    assert not any("/v1/taxa/" in url for url, _ in fake.calls)


def test_lookup_of_existing_observation_that_is_also_a_taxon_has_no_hint():
    fake = _fake_inat(taxa=[{"id": 48419, "name": "Amanita", "rank": "genus"}],
                      observations=[{"id": 48419, "taxon": {"name": "Spinus psaltria"}}])
    with patch.object(labels_app, "inat_api_get", fake):
        item = labels_app.lookup_batch_internal(["48419"])["items"][0]
    assert "error" not in item
    assert item["scientific_name"] == "Spinus psaltria"
    assert not any("/v1/taxa/" in url for url, _ in fake.calls)


def _job(tmp_path, bare_ids):
    proc = MagicMock()
    proc.stdout = io.StringIO('No valid observations found.\n')
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    return {'proc': proc, 'output_path': str(tmp_path / 'labels.pdf'),
            'filename': 'labels.pdf', 'started_time': 0,
            'observation_count': len(bare_ids), 'bare_inat_ids': bare_ids,
            'logs': deque(maxlen=1000), 'log_cursor': 0}


def test_print_with_only_taxon_ids_says_they_are_taxa(tmp_path):
    job = _job(tmp_path, ["15"])
    with patch.object(labels_app, "inat_api_get", _fake_inat(taxa=[CHUNGA])), \
            patch.object(labels_app, "log_user_problem") as logged:
        labels_app._collect_print_output(JOB_ID, job)
    assert job['result']['status'] == 'failed'
    assert "15 is the iNaturalist taxon ID for Chunga (genus)" in job['result']['error']
    assert [c.args[0] for c in logged.call_args_list] == [
        "observation_id_is_taxon_id", "label_generator_output_missing"]


@pytest.mark.parametrize("taxa, observations", [
    ([], []),                                   # not a taxon either
    ([CHUNGA], [{"id": 15}]),                   # the observation exists after all
])
def test_print_failure_without_a_taxon_explanation_keeps_the_plain_error(
        tmp_path, taxa, observations):
    job = _job(tmp_path, ["15"])
    with patch.object(labels_app, "inat_api_get", _fake_inat(taxa=taxa, observations=observations)), \
            patch.object(labels_app, "log_user_problem"):
        labels_app._collect_print_output(JOB_ID, job)
    assert "taxon ID" not in job['result']['error']
    assert "observation IDs exist" in job['result']['error']


def test_print_hint_lookup_failure_keeps_the_plain_error(tmp_path):
    job = _job(tmp_path, ["15"])
    with patch.object(labels_app, "inat_api_get",
                      side_effect=requests.exceptions.ConnectionError("down")), \
            patch.object(labels_app, "log_user_problem"):
        labels_app._collect_print_output(JOB_ID, job)
    assert job['result']['status'] == 'failed'
    assert "taxon ID" not in job['result']['error']
