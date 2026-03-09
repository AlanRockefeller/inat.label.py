import importlib.util 
import sys 
import types 
from pathlib import Path 
 
import pytest 
 
 
def _install_import_stubs(): 
    if 'qrcode' not in sys.modules: 
        sys.modules['qrcode'] = types.ModuleType('qrcode') 
 
    if 'bs4' not in sys.modules: 
        bs4 = types.ModuleType('bs4') 
        class _BeautifulSoup: 
            def __init__(self, *args, **kwargs): 
                pass 
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
 
 
@pytest.fixture(scope='session') 
def inat_module(): 
    _install_import_stubs() 
    module_path = Path(__file__).resolve().parents[1] / 'inat.label.py' 
    spec = importlib.util.spec_from_file_location('inat_label', module_path) 
    assert spec and spec.loader 
    module = importlib.util.module_from_spec(spec) 
    spec.loader.exec_module(module) 
    return module
