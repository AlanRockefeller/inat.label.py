import datetime
import html
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest


def _install_import_stubs():
    if 'qrcode' not in sys.modules:
        sys.modules['qrcode'] = types.ModuleType('qrcode')

    if 'bs4' not in sys.modules:
        bs4 = types.ModuleType('bs4')
        class _SoupTag:
            def __init__(self, parent, outer_html, inner_html, attrs):
                self._parent = parent
                self._outer_html = outer_html
                self._inner_html = inner_html
                self._attrs = attrs

            @property
            def text(self):
                return self.get_text()

            def get(self, key, default=None):
                return self._attrs.get(key, default)

            def get_text(self):
                text = re.sub(r'<[^>]+>', '', self._inner_html)
                return html.unescape(text)

            def replace_with(self, replacement):
                self._parent._replace_once(self._outer_html, str(replacement))

            def unwrap(self):
                self._parent._replace_once(self._outer_html, self._inner_html)

        class _BeautifulSoup:
            def __init__(self, markup, *args, **kwargs):
                del args, kwargs
                self._text = str(markup)

            def _replace_once(self, old, new):
                self._text = self._text.replace(old, new, 1)

            def find_all(self, names):
                if isinstance(names, str):
                    names = [names]
                tags = []
                for name in names:
                    pattern = rf'<{name}\b([^>]*)>(.*?)</{name}>'
                    for match in re.finditer(pattern, self._text, flags=re.IGNORECASE | re.DOTALL):
                        attrs_text = match.group(1)
                        attrs = {}
                        for attr_match in re.finditer(
                            r'([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*["\']([^"\']*)["\']',
                            attrs_text,
                        ):
                            attrs[attr_match.group(1)] = html.unescape(attr_match.group(2))
                        tags.append(_SoupTag(self, match.group(0), match.group(2), attrs))
                return sorted(tags, key=lambda tag: self._text.find(tag._outer_html))

            def __str__(self):
                return self._text

        bs4.BeautifulSoup = _BeautifulSoup
        sys.modules['bs4'] = bs4

    if 'reportlab' not in sys.modules:
        reportlab = types.ModuleType('reportlab')
        lib = types.ModuleType('reportlab.lib')
        pagesizes = types.ModuleType('reportlab.lib.pagesizes')
        pagesizes.letter = (612, 792)
        styles = types.ModuleType('reportlab.lib.styles')
        styles.getSampleStyleSheet = lambda *args, **kwargs: {}
        class _ParagraphStyle:
            def __init__(self, *args, **kwargs):
                pass
        styles.ParagraphStyle = _ParagraphStyle
        units = types.ModuleType('reportlab.lib.units')
        units.inch = 72
        pdfbase = types.ModuleType('reportlab.pdfbase')
        pdfmetrics = types.ModuleType('reportlab.pdfbase.pdfmetrics')
        ttfonts = types.ModuleType('reportlab.pdfbase.ttfonts')
        class _TTFont:
            def __init__(self, *args, **kwargs):
                pass
        class _TTFError(Exception):
            pass
        ttfonts.TTFont = _TTFont
        ttfonts.TTFError = _TTFError
        class _Dummy:
            def __init__(self, *args, **kwargs):
                pass
        platypus = types.ModuleType('reportlab.platypus')
        for name in [
            'BaseDocTemplate', 'Frame', 'Image', 'KeepInFrame', 'KeepTogether',
            'PageTemplate', 'Paragraph', 'Spacer', 'Table', 'TableStyle'
        ]:
            setattr(platypus, name, _Dummy)
        sys.modules['reportlab'] = reportlab
        sys.modules['reportlab.lib'] = lib
        sys.modules['reportlab.lib.pagesizes'] = pagesizes
        sys.modules['reportlab.lib.styles'] = styles
        sys.modules['reportlab.lib.units'] = units
        sys.modules['reportlab.pdfbase'] = pdfbase
        sys.modules['reportlab.pdfbase.pdfmetrics'] = pdfmetrics
        sys.modules['reportlab.pdfbase.ttfonts'] = ttfonts
        sys.modules['reportlab.platypus'] = platypus

    if 'dateutil' not in sys.modules:
        dateutil = types.ModuleType('dateutil')
        parser = types.ModuleType('dateutil.parser')

        def _parse(value, fuzzy=False):
            del fuzzy
            text = str(value)
            for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%B %d, %Y', '%b %d, %Y'):
                try:
                    return datetime.datetime.strptime(text, fmt)
                except ValueError:
                    continue
            raise ValueError(f'Unknown date format: {value}')

        parser.parse = _parse
        dateutil.parser = parser
        sys.modules['dateutil'] = dateutil
        sys.modules['dateutil.parser'] = parser


@pytest.fixture(scope='session')
def inat_module():
    _install_import_stubs()
    module_path = Path(__file__).resolve().parents[1] / 'inat.label.py'
    spec = importlib.util.spec_from_file_location('inat_label', module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
