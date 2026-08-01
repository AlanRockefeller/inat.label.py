import datetime
import sys
import warnings
from types import SimpleNamespace


def _item(index, fields, taxon='Fungi', observation_datetime=None):
    return (index, (fields, taxon), observation_datetime)


def _value(tagged_label, field_name):
    label_fields, _ = tagged_label
    return next((v for f, v in label_fields if f == field_name), None)


def test_sort_labels_none_preserves_index_order(inat_module):
    items = [
        _item(2, [('iNaturalist Observation Number', '20')]),
        _item(0, [('iNaturalist Observation Number', '10')]),
        _item(1, [('iNaturalist Observation Number', '30')]),
    ]

    result = inat_module.sort_labels(items, 'none')
    assert [_value(x, 'iNaturalist Observation Number') for x in result] == ['10', '30', '20']


def test_sort_labels_default_uses_numeric_observation_number(inat_module):
    items = [
        _item(0, [('iNaturalist Observation Number', '10')]),
        _item(1, [('iNaturalist Observation Number', '2')]),
        _item(2, [('Mushroom Observer Number', '5')]),
    ]

    result = inat_module.sort_labels(items, None)
    assert [
        _value(x, 'iNaturalist Observation Number') or _value(x, 'Mushroom Observer Number')
        for x in result
    ] == [
        '2',
        '5',
        '10',
    ]


def test_sort_labels_voucher_sorts_alpha_then_trailing_number(inat_module):
    items = [
        _item(0, [('Voucher Number', 'Plot 10')]),
        _item(1, [('Voucher Number', 'Plot 2')]),
        _item(2, [('Voucher Number', 'Area 1')]),
        _item(3, [('Scientific Name', 'No Voucher')]),
    ]

    result = inat_module.sort_labels(items, 'voucher')
    assert [_value(x, 'Voucher Number') for x in result] == ['Area 1', 'Plot 2', 'Plot 10', None]


def test_sort_labels_custom_uses_requested_field(inat_module):
    items = [
        _item(0, [('Collection Number', 'B 9')]),
        _item(1, [('Collection Number', 'A 12')]),
        _item(2, [('Collection Number', 'A 2')]),
    ]

    result = inat_module.sort_labels(items, 'custom', sort_field_name='Collection Number')
    assert [_value(x, 'Collection Number') for x in result] == ['A 2', 'A 12', 'B 9']


def test_date_sort_uses_time_for_same_day_observations(inat_module):
    later = inat_module.observation_sort_datetime({'time_observed_at': '2026-07-31T18:30:00-07:00'})
    earlier = inat_module.observation_sort_datetime(
        {'time_observed_at': '2026-07-31T08:15:00-07:00'}
    )
    items = [
        _item(0, [('ID', 'later'), ('Date Observed', '2026-07-31')], observation_datetime=later),
        _item(
            1, [('ID', 'earlier'), ('Date Observed', '2026-07-31')], observation_datetime=earlier
        ),
    ]

    result = inat_module.sort_labels(items, 'date')

    assert [_value(label, 'ID') for label in result] == ['earlier', 'later']
    assert [_value(label, 'Date Observed') for label in result] == [
        '2026-07-31',
        '2026-07-31',
    ]


def test_date_sort_orders_different_dates(inat_module):
    items = [
        _item(
            0,
            [('ID', 'newer')],
            observation_datetime=inat_module.observation_sort_datetime(
                {'observed_on': '2026-08-01'}
            ),
        ),
        _item(
            1,
            [('ID', 'older')],
            observation_datetime=inat_module.observation_sort_datetime(
                {'observed_on': '2026-07-31'}
            ),
        ),
    ]

    result = inat_module.sort_labels(items, 'date')

    assert [_value(label, 'ID') for label in result] == ['older', 'newer']


def test_sort_labels_default_falls_back_to_bugguide_number(inat_module):
    items = [
        _item(0, [('iNaturalist Observation Number', '100')]),
        _item(1, [('BugGuide Number', '2520730')]),
        _item(2, [('iNaturalist Observation Number', '200')]),
    ]

    result = inat_module.sort_labels(items, None)
    assert [
        _value(x, 'iNaturalist Observation Number') or _value(x, 'BugGuide Number')
        for x in result
    ] == ['100', '200', '2520730']


def test_observation_datetime_instants_are_comparable_across_timezones(inat_module):
    pacific = inat_module.observation_sort_datetime(
        {'time_observed_at': '2026-07-31T08:00:00-07:00'}
    )
    utc = inat_module.observation_sort_datetime({'time_observed_at': '2026-07-31T15:00:00Z'})

    assert (
        pacific
        == utc
        == datetime.datetime(
            2026,
            7,
            31,
            15,
            tzinfo=datetime.timezone.utc,  # noqa: UP017
        )
    )


def test_date_only_observation_uses_start_of_day(inat_module):
    result = inat_module.observation_sort_datetime({'observed_on': '2026-07-31'})

    assert result == datetime.datetime(
        2026,
        7,
        31,
        0,
        0,
        tzinfo=datetime.timezone.utc,  # noqa: UP017
    )


def test_observed_on_string_is_mushroom_observer_fallback(inat_module):
    result = inat_module.observation_sort_datetime(
        {'id': 'MO12345', 'observed_on_string': '2026-07-31'}
    )

    assert result == datetime.datetime(
        2026,
        7,
        31,
        0,
        0,
        tzinfo=datetime.timezone.utc,  # noqa: UP017
    )


def test_observed_on_string_timezone_abbreviation_is_host_independent(inat_module):
    with warnings.catch_warnings(record=True) as caught:
        pst_datetime = inat_module.observation_sort_datetime(
            {'observed_on_string': '2025-11-14 03:25 PM PST'}
        )

    assert pst_datetime == datetime.datetime(
        2025,
        11,
        14,
        23,
        25,
        tzinfo=datetime.timezone.utc,  # noqa: UP017
    )
    assert caught == []

    utc_datetime = inat_module.observation_sort_datetime(
        {'time_observed_at': '2025-11-14T20:00:00Z'}
    )
    items = [
        _item(0, [('ID', 'pst')], observation_datetime=pst_datetime),
        _item(1, [('ID', 'utc')], observation_datetime=utc_datetime),
    ]

    assert [_value(label, 'ID') for label in inat_module.sort_labels(items, 'date')] == [
        'utc',
        'pst',
    ]


def test_naive_observed_on_string_uses_named_observation_timezone(inat_module):
    result = inat_module.observation_sort_datetime(
        {
            'observed_on_string': '2025-11-14 15:25:00',
            'observed_time_zone': 'America/Los_Angeles',
        }
    )

    assert result == datetime.datetime(
        2025,
        11,
        14,
        23,
        25,
        tzinfo=datetime.timezone.utc,  # noqa: UP017
    )


def test_unresolved_observed_on_string_timezone_uses_calendar_date(inat_module):
    result = inat_module.observation_sort_datetime(
        {'observed_on_string': '2025-11-14 03:25 PM XYZ'}
    )

    assert result == datetime.datetime(
        2025,
        11,
        14,
        tzinfo=datetime.timezone.utc,  # noqa: UP017
    )


def test_meridiem_is_not_stripped_as_a_timezone_abbreviation(inat_module):
    result = inat_module.observation_sort_datetime(
        {
            'observed_on_string': '2025-11-14 03:25 PM',
            'observed_time_zone': 'America/Los_Angeles',
        }
    )

    assert result == datetime.datetime(
        2025,
        11,
        14,
        23,
        25,
        tzinfo=datetime.timezone.utc,  # noqa: UP017
    )


def test_date_only_string_ignores_trailing_timezone_abbreviation(inat_module):
    bare = inat_module.observation_sort_datetime({'observed_on_string': '2026-07-31'})
    suffixed = inat_module.observation_sort_datetime(
        {'observed_on_string': '2026-07-31 PST'}
    )

    assert bare == suffixed


def test_date_sort_keeps_displayed_dates_in_order_across_timezones(inat_module):
    # Local evening in Pacific time is already the next day in UTC; the label
    # prints the local date, so the earlier printed date must sort first.
    pacific_evening = inat_module.observation_sort_datetime(
        {'time_observed_at': '2026-07-31T20:00:00-07:00'}
    )
    utc_after_midnight = inat_module.observation_sort_datetime(
        {'time_observed_at': '2026-08-01T01:00:00+00:00'}
    )
    items = [
        _item(0, [('Date Observed', 'July 31, 2026')], observation_datetime=pacific_evening),
        _item(
            1,
            [('Date Observed', 'August 1, 2026')],
            observation_datetime=utc_after_midnight,
        ),
    ]

    result = inat_module.sort_labels(items, 'date')

    assert [_value(label, 'Date Observed') for label in result] == [
        'July 31, 2026',
        'August 1, 2026',
    ]


def test_api_timestamp_uses_named_zone_for_the_local_date(inat_module):
    # iNaturalist may report the instant in UTC while the label shows the
    # observer's local date.
    result = inat_module.observation_sort_datetime(
        {
            'time_observed_at': '2026-08-01T03:00:00Z',
            'observed_time_zone': 'America/Los_Angeles',
        }
    )

    assert result.date() == datetime.date(2026, 7, 31)
    assert result == datetime.datetime(
        2026,
        8,
        1,
        3,
        tzinfo=datetime.timezone.utc,  # noqa: UP017
    )


def test_unparseable_date_warns_when_sorting_by_date(inat_module, capsys):
    items = [
        _item(0, [('Date Observed', 'sometime last fall')]),
        _item(1, [('ID', 'no date field')]),
    ]

    inat_module.sort_labels(items, 'date')

    stderr = capsys.readouterr().err
    assert "Could not parse date 'sometime last fall'" in stderr
    assert stderr.count('Could not parse date') == 1


def test_missing_and_invalid_dates_sort_last(inat_module):
    valid = inat_module.observation_sort_datetime({'observed_on': '2026-07-31'})
    items = [
        _item(0, [('ID', 'missing')]),
        _item(
            1,
            [('ID', 'invalid')],
            observation_datetime=inat_module.observation_sort_datetime(
                {'time_observed_at': 'not-a-date', 'observed_on': ''}
            ),
        ),
        _item(2, [('ID', 'valid')], observation_datetime=valid),
    ]

    result = inat_module.sort_labels(items, 'date')

    assert [_value(label, 'ID') for label in result] == ['valid', 'missing', 'invalid']


def test_equal_timestamps_use_original_index(inat_module):
    timestamp = inat_module.observation_sort_datetime({'time_observed_at': '2026-07-31T12:00:00Z'})
    items = [
        _item(2, [('ID', 'third')], observation_datetime=timestamp),
        _item(0, [('ID', 'first')], observation_datetime=timestamp),
        _item(1, [('ID', 'second')], observation_datetime=timestamp),
    ]

    result = inat_module.sort_labels(items, 'date')

    assert [_value(label, 'ID') for label in result] == ['first', 'second', 'third']


def test_main_date_sort_carries_api_timestamp_to_plaintext_output(inat_module, monkeypatch):
    observations = {
        '200': {
            'id': 200,
            'time_observed_at': '2026-07-31T18:00:00Z',
            'observed_on_string': '2026-07-31',
        },
        '100': {
            'id': 100,
            'time_observed_at': '2026-07-31T08:00:00Z',
            'observed_on_string': '2026-07-31',
        },
    }

    monkeypatch.setattr(
        inat_module,
        'get_observation_data',
        lambda observation_id: (observations[str(observation_id)], 'Fungi'),
    )
    monkeypatch.setattr(
        inat_module,
        'create_inaturalist_label',
        lambda observation_data, *_args, **_kwargs: (
            [
                ('iNaturalist Observation Number', str(observation_data['id'])),
                ('Date Observed', observation_data['observed_on_string']),
            ],
            'Fungi',
        ),
    )
    emitted = []
    monkeypatch.setattr(inat_module, 'render_plaintext_labels', emitted.extend)
    monkeypatch.setattr(
        sys,
        'argv',
        ['inat.label.py', '200', '100', '--sort', 'date', '--workers', '1'],
    )

    inat_module.main()

    assert [_value(label, 'iNaturalist Observation Number') for label in emitted] == ['100', '200']


def test_read_observation_id_file_returns_combined_copy(inat_module, tmp_path):
    id_file = tmp_path / 'observations.txt'
    id_file.write_text('200, 300\n', encoding='utf-8')
    positional_ids = ['100']

    result = inat_module._read_observation_id_file(
        SimpleNamespace(file=str(id_file)), positional_ids
    )

    assert result == ['100', '200', '300']
    assert positional_ids == ['100']
