"""Suppressing a URL field hides its text line but keeps the QR code."""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent

# tests/test_sorting.py replaces reportlab, qrcode and others with MagicMocks
# in sys.modules for the rest of the run.  These tests render real PDF/RTF, and
# reportlab imports some of its submodules lazily while rendering, so the real
# modules stay in place until tearDownModule puts the stubs back.
_stubs = {n: m for n, m in sys.modules.items() if isinstance(m, MagicMock)}
for _name in _stubs:
    del sys.modules[_name]
try:
    spec = importlib.util.spec_from_file_location(
        "inat_label_suppressed_url", REPO_ROOT / "inat.label.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load inat.label.py")
    inat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inat)
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    raise unittest.SkipTest(
        f"inat.label.py dependencies are unavailable: {exc}"
    ) from exc
finally:
    _real = {n: sys.modules[n] for n in _stubs if n in sys.modules}
    sys.modules.update(_stubs)


def setUpModule():
    sys.modules.update(_real)


def tearDownModule():
    sys.modules.update(_stubs)

URL = "https://www.inaturalist.org/observations/123456"


def inat_observation():
    return {
        "id": 123456,
        "taxon": {"name": "Amanita muscaria", "rank": "species"},
        "user": {"login": "someone", "name": "Some One"},
        "observed_on": "2026-07-31",
        "observed_on_string": "2026-07-31 09:15:00",
        "place_guess": "Marin County, CA, US",
        "geojson": {"type": "Point", "coordinates": [-122.5, 37.9]},
        "ofvs": [],
    }


def build_label(custom_remove=None):
    with contextlib.redirect_stderr(io.StringIO()):
        return inat.create_inaturalist_label(
            inat_observation(), "Fungi", custom_remove=custom_remove
        )


class SuppressedUrlKeepsQrTestCase(unittest.TestCase):
    def test_suppressed_url_is_not_a_visible_field(self):
        label, _ = build_label(["iNaturalist URL"])
        fields = [field for field, _ in label]
        self.assertNotIn("iNaturalist URL", fields)
        self.assertIn((inat.QR_URL_FIELD, URL), label)

    def test_unsuppressed_label_is_unchanged(self):
        label, _ = build_label(["Observer"])
        self.assertIn(("iNaturalist URL", URL), label)
        self.assertNotIn(inat.QR_URL_FIELD, [field for field, _ in label])

    def test_rtf_drops_the_url_line_but_keeps_the_qr_code(self):
        labels = [build_label(["iNaturalist URL"])]
        with patch.object(
            inat, "generate_qr_code", wraps=inat.generate_qr_code
        ) as qr:
            rtf = inat.create_rtf_content(labels)
        qr.assert_called_once()
        self.assertEqual(qr.call_args.args[0], URL)
        self.assertNotIn("inaturalist.org/observations", rtf)
        self.assertNotIn(inat.QR_URL_FIELD, rtf)

    def test_no_qr_still_removes_the_qr_code(self):
        labels = [build_label(["iNaturalist URL"])]
        with patch.object(inat, "generate_qr_code") as qr:
            inat.create_rtf_content(labels, no_qr=True)
        qr.assert_not_called()

    def test_pdf_draws_the_qr_code_from_the_suppressed_url(self):
        labels = [build_label(["iNaturalist URL"])]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                inat, "generate_qr_code", wraps=inat.generate_qr_code
            ) as qr, patch.object(inat, "rl_safe", wraps=inat.rl_safe) as text:
                inat.create_pdf_content(labels, str(Path(tmp) / "out.pdf"))
        self.assertEqual(qr.call_args.args[0], URL)
        texts = " ".join(str(c.args[0]) for c in text.call_args_list)
        self.assertIn("Amanita", texts)
        self.assertNotIn("inaturalist.org/observations", texts)

    def test_minilabel_qr_url_uses_the_suppressed_url(self):
        label, _ = build_label(["iNaturalist URL"])
        self.assertEqual(inat._minilabel_qr_url(label), URL)
        self.assertEqual(inat._minilabel_qr_url(label, allow_fallback=False), URL)

    def test_plaintext_omits_the_suppressed_url(self):
        labels = [build_label(["iNaturalist URL"])]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inat.render_plaintext_labels(labels)
        self.assertNotIn("inaturalist.org/observations", out.getvalue())
        self.assertIn("Scientific Name", out.getvalue())


if __name__ == "__main__":
    unittest.main()
