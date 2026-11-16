"""Custom label fields whose names end in a colon.

On 2026-10-30 a user picked the iNaturalist observation field "Found by:" in
the Add Observation Fields dialog, which lists the observations' own field
names (observations 295816106, 346639752, 346644972 and 346644981 all carry
it, colon included).  app.py refused it twice, at submit and print_start, with
"Invalid custom label field name: '+Found by:'", because CUSTOM_FIELD_RE
allowed no colon.  Stripping the colon in app.py would not have fixed it:
inat.label.py matched field names exactly, so "Found by" would have matched
nothing and silently left the field off the label.

Now app.py accepts a trailing colon, inat.label.py ignores a trailing colon on
either side when adding or suppressing a field, and the label prints the name
once ("Found by: ..." rather than "Found by:: ...").
"""
import contextlib
import importlib.util
import io
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import app as labels_app

REPO_ROOT = Path(__file__).resolve().parent.parent

# tests/test_sorting.py replaces reportlab, qrcode and others with MagicMocks in
# sys.modules when it is collected, which may be before or after this module.
# The PDF check renders for real and reportlab imports parts of itself lazily,
# so the generator is loaded only once those stubs are out of the way, and they
# are put back when this module is done.
inat = None
_stubs = {}


def setup_module():
    global inat
    _stubs.update({n: m for n, m in sys.modules.items() if isinstance(m, MagicMock)})
    for name in _stubs:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        "inat_label_custom_field_colon", REPO_ROOT / "inat.label.py"
    )
    inat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inat)


def teardown_module():
    sys.modules.update(_stubs)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    labels_app._rate_limiter.reset()


def _print_start(fields):
    with patch.object(labels_app.subprocess, "Popen", return_value=MagicMock()) as popen, \
            patch.dict(labels_app._jobs, {}, clear=True):
        response = labels_app.app.test_client().post("/labels/print_start", data={
            "format": "pdf", "observations[]": ["123"], "use_custom": "on",
            "custom_args[]": fields,
        })
    return response, popen


def observation(*ofvs):
    return {
        "id": 295816106,
        "taxon": {"name": "Amanita muscaria", "rank": "species"},
        "user": {"login": "someone", "name": "Some One"},
        "observed_on": "2026-07-31",
        "observed_on_string": "2026-07-31 09:15:00",
        "place_guess": "Marin County, CA, US",
        "geojson": {"type": "Point", "coordinates": [-122.5, 37.9]},
        "ofvs": [{"name": name, "value": value} for name, value in ofvs],
    }


def label_for(obs, add=None, remove=None):
    with contextlib.redirect_stderr(io.StringIO()):
        label, _ = inat.create_inaturalist_label(
            obs, "Fungi", custom_add=add, custom_remove=remove
        )
    return label


FOUND_BY = observation(("Found by:", "A. Collector"))


# --- app.py: validation and the command it builds -------------------------

@pytest.mark.parametrize("fields", [
    ["+Found by:"],
    ["-Observer:"],
    ["+Found by:", "-DNA Barcode ITS", "-DNA Barcode LSU"],
])
def test_print_start_forwards_names_ending_in_a_colon(fields):
    response, popen = _print_start(fields)
    assert response.status_code == 200, response.get_json()
    cmd = popen.call_args.args[0]
    assert "--custom=" + ", ".join(fields) in cmd
    # ...and the generator's real parser reads it back unchanged.
    script = next(i for i, part in enumerate(cmd) if part.endswith("inat.label.py"))
    args = inat.build_arg_parser().parse_args(cmd[script + 1:])
    assert args.custom == ", ".join(fields)


@pytest.mark.parametrize("field", [
    "+Found by: A. Collector",   # a name and its value: the colon is not trailing
    "+Found by::",
    "+:",
    ":Found by",
    "--rtf:",
])
def test_other_colons_are_still_refused(field):
    response, popen = _print_start([field])
    assert response.status_code == 400
    assert "Invalid custom label field name" in response.get_json()["error"]
    popen.assert_not_called()


def test_a_copied_colon_is_not_argument_injection():
    # The classifier defers to CUSTOM_FIELD_RE, so "-Observer:" must not land
    # in targeted_attacks.log as labelmaker_exploit_payload.
    assert not labels_app._looks_like_argument_injection("-Observer:")
    assert not labels_app._looks_like_argument_injection("-Found by:")
    assert labels_app._looks_like_argument_injection("--rtf:")


# --- inat.label.py: matching and printing ---------------------------------

@pytest.mark.parametrize("requested", ["Found by:", "Found by", "found by :"])
def test_added_field_matches_with_or_without_the_colon(requested):
    label = label_for(FOUND_BY, add=[requested])
    values = {field.lower(): value for field, value in label}
    assert values.get("found by") == "A. Collector"
    assert not [field for field, _ in label if field.rstrip().endswith(":")]


def test_a_requested_colon_matches_a_field_without_one():
    label = label_for(observation(("Found by", "B. Finder")), add=["Found by:"])
    assert ("Found by", "B. Finder") in label


def test_an_exact_name_match_wins():
    obs = observation(("Found by", "plain"), ("Found by:", "with colon"))
    assert ("Found by", "with colon") in label_for(obs, add=["Found by:"])
    assert ("Found by", "plain") in label_for(obs, add=["Found by"])


def test_suppressing_with_a_copied_colon():
    assert "Observer" in [field for field, _ in label_for(FOUND_BY)]
    for suppressed in (["Observer:"], ["Observer"], ["observer :"]):
        fields = [field for field, _ in label_for(FOUND_BY, remove=suppressed)]
        assert "Observer" not in fields, suppressed


def test_added_then_suppressed_either_way():
    label = label_for(FOUND_BY, add=["Found by:"], remove=["Found by"])
    assert "Found by" not in [field for field, _ in label]


def test_rtf_and_plaintext_print_the_colon_once():
    labels = [(label_for(FOUND_BY, add=["Found by:"]), "Fungi")]
    rtf = inat.create_rtf_content(labels, no_qr=True)
    assert "Found by:" in rtf
    assert "Found by::" not in rtf

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        inat.render_plaintext_labels(labels)
    assert "Found by: A. Collector" in out.getvalue()
    assert "Found by::" not in out.getvalue()


def test_pdf_prints_the_colon_once():
    labels = [(label_for(FOUND_BY, add=["Found by:"]), "Fungi")]
    with tempfile.TemporaryDirectory() as tmp, \
            patch.object(inat, "Paragraph", wraps=inat.Paragraph) as paragraph:
        inat.create_pdf_content(labels, str(Path(tmp) / "out.pdf"), no_qr=True)
    markup = " ".join(str(c.args[0]) for c in paragraph.call_args_list)
    assert "<b>Found by:</b> A. Collector" in markup
    assert "Found by::" not in markup
