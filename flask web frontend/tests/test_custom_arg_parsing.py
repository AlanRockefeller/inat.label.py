"""The --custom value app.py builds must survive inat.label.py's real parser.

On 2026-10-16 two RTF jobs suppressing only "-Observer" exited 2 with
"argument --custom: expected one argument": argparse reads a separate
dash-prefixed word as an option.  "-Herbarium Catalog Number" only worked
because argparse treats a value containing a space as positional.
"""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app as labels_app

REPO_ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "inat_label_custom_args", REPO_ROOT / "inat.label.py"
)
inat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inat)


def _command_for(fields, fmt):
    with patch.object(labels_app.subprocess, 'Popen', return_value=MagicMock()) as popen, \
            patch.dict(labels_app._jobs, {}, clear=True):
        response = labels_app.app.test_client().post('/labels/print_start', data={
            'format': fmt, 'observations[]': ['123'], 'use_custom': 'on', 'custom_args[]': fields,
        })
    assert response.status_code == 200
    return popen.call_args.args[0]


@pytest.mark.parametrize('fmt', ['pdf', 'rtf'])
@pytest.mark.parametrize('fields', [
    ['-Observer'],
    ['-Herbarium Catalog Number'],
    ['+Habitat'],
    ['-Observer', '+Habitat'],
    ['-iNaturalist URL'],
])
def test_generator_parser_accepts_the_built_command(fields, fmt):
    cmd = _command_for(fields, fmt)
    script = next(i for i, part in enumerate(cmd) if part.endswith('inat.label.py'))
    args = inat.build_arg_parser().parse_args(cmd[script + 1:])
    assert args.custom == ', '.join(fields)
    assert args.observation_ids == ['123']
