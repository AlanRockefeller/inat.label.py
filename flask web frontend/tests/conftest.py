"""Print tests must not launch daemon collectors against mocked subprocesses."""
import pytest
import os
import tempfile

# Isolate imports and every inherited subprocess from production state/logs.
_test_state = tempfile.TemporaryDirectory(prefix="labelmaker-tests-")
os.environ["LABELS_INAT_STATE"] = _test_state.name + "/inat.sqlite3"
os.environ["LABELS_DISABLE_FILE_LOGGING"] = "1"
os.environ["LABELS_JOB_DIR"] = _test_state.name + "/jobs"


@pytest.fixture(autouse=True)
def isolate_print_collectors(monkeypatch):
    import app
    monkeypatch.setattr(app, '_start_print_collector', lambda *_args: None)


@pytest.fixture(autouse=True)
def isolate_inat_state(tmp_path, monkeypatch):
    monkeypatch.setenv('LABELS_INAT_STATE', str(tmp_path / 'inat.sqlite3'))
    import app
    monkeypatch.setattr(app, '_search_versions', app.OrderedDict())
    monkeypatch.setattr(app, 'PRINT_JOBS_DIR', str(tmp_path / 'jobs'))
