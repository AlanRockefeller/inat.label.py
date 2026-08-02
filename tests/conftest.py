import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope='session')
def inat_module():
    module_path = Path(__file__).resolve().parents[1] / 'inat.label.py'
    spec = importlib.util.spec_from_file_location('inat_label', module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
