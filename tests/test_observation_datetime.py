"""Regression tests for observation_sort_datetime() timezone handling.

These cover the trailing-timezone-abbreviation stripping in
``observed_on_string``: the meridiem guard (a trailing ``AM``/``PM`` is part of
the clock time, not a zone), abbreviation resolution, and what happens when no
zone can be resolved at all.  Every assertion is an absolute UTC instant, so
the suite must pass under any host ``TZ``.
"""

import datetime

UTC = datetime.timezone.utc  # noqa: UP017


def _at(year, month, day, hour=0, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=UTC)


def _item(index, fields, taxon='Fungi', observation_datetime=None):
    return (index, (fields, taxon), observation_datetime)


def _value(tagged_label, field_name):
    label_fields, _ = tagged_label
    return next((v for f, v in label_fields if f == field_name), None)


def test_trailing_pm_is_not_stripped_as_a_timezone(inat_module):
    # The bug: "PM" matched the trailing-abbreviation regex, got stripped, and
    # 01:20 PM was parsed as 01:20 -- moving an afternoon collection into the
    # morning and ahead of collections that really were earlier.
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 01:20 PM'}
    )

    assert result == _at(2019, 5, 23, 13, 20)


def test_trailing_lowercase_pm_is_not_stripped_as_a_timezone(inat_module):
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 01:20 pm'}
    )

    assert result == _at(2019, 5, 23, 13, 20)


def test_trailing_am_is_not_stripped_as_a_timezone(inat_module):
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 09:05 AM'}
    )

    assert result == _at(2019, 5, 23, 9, 5)


def test_known_abbreviation_after_a_meridiem_still_resolves(inat_module):
    # The meridiem guard must not disable abbreviation handling: PDT is the
    # trailing token here and still sets the offset (-7).
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 01:20 PM PDT'}
    )

    assert result == _at(2019, 5, 23, 20, 20)


def test_known_abbreviation_takes_precedence_over_named_observation_zone(inat_module):
    result = inat_module.observation_sort_datetime(
        {
            'observed_on_string': '2019-05-23 01:20 PM UTC',
            'observed_time_zone': 'America/Los_Angeles',
        }
    )

    assert result == _at(2019, 5, 23, 13, 20)


def test_afternoon_sorts_after_morning_on_the_same_day(inat_module):
    afternoon = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 01:20 PM'}
    )
    morning = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 09:15 AM'}
    )
    items = [
        _item(0, [('ID', 'afternoon')], observation_datetime=afternoon),
        _item(1, [('ID', 'morning')], observation_datetime=morning),
    ]

    result = inat_module.sort_labels(items, 'date')

    assert [_value(label, 'ID') for label in result] == ['morning', 'afternoon']


def test_twenty_four_hour_clock_is_unaffected(inat_module):
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 13:20:00'}
    )

    assert result == _at(2019, 5, 23, 13, 20)


def test_twenty_four_hour_clock_uses_a_named_zone_when_present(inat_module):
    result = inat_module.observation_sort_datetime(
        {
            'observed_on_string': '2019-05-23 13:20:00',
            'observed_time_zone': 'America/Los_Angeles',
        }
    )

    assert result == _at(2019, 5, 23, 20, 20)


def test_zoneless_meridiem_clock_time_is_kept_as_utc(inat_module):
    # No zone can be resolved, so the clock time is read as UTC.  Flattening it
    # to midnight would tie every observation from the same collecting day and
    # collapse a date-sorted sheet back to input order.
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 01:20 PM XYZ'}
    )

    assert result == _at(2019, 5, 23, 13, 20)


def test_zoneless_twenty_four_hour_clock_time_is_kept_as_utc(inat_module):
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 13:20:00 XYZ'}
    )

    assert result == _at(2019, 5, 23, 13, 20)


def test_date_only_string_uses_midnight_utc(inat_module):
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23'}
    )

    assert result == _at(2019, 5, 23)


def test_date_only_string_with_abbreviation_uses_midnight_utc(inat_module):
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2019-05-23 PDT'}
    )

    assert result == _at(2019, 5, 23)


def test_time_observed_at_wins_over_observed_on_string(inat_module):
    result = inat_module.observation_sort_datetime(
        {
            'time_observed_at': '2019-05-23T18:00:00Z',
            'observed_on_string': '2019-05-23 01:20 PM',
        }
    )

    assert result == _at(2019, 5, 23, 18, 0)
