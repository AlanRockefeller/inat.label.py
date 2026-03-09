import datetime


def test_parse_date_accepts_known_formats(inat_module):
    assert inat_module.parse_date('2025-11-14 03:25 PM PST') == datetime.date(2025, 11, 14)
    assert inat_module.parse_date('2025/11/14 03:25 PM PST') == datetime.date(2025, 11, 14)
    assert inat_module.parse_date('November 14, 2025 3:25 PM PST') == datetime.date(2025, 11, 14)


def test_parse_date_handles_empty_and_invalid(inat_module):
    assert inat_module.parse_date(None) is None
    assert inat_module.parse_date('') is None
    assert inat_module.parse_date('not-a-date') is None
