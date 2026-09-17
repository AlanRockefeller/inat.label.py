import datetime
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    inat = SourceFileLoader(
        "inat_label_datetime", str(REPO_ROOT / "inat.label.py")
    ).load_module()
except Exception as exc:  # pragma: no cover - dependency guard
    raise unittest.SkipTest(f"inat.label.py dependencies are unavailable: {exc}")


def utc(year, month, day, hour=0, minute=0):
    return datetime.datetime(
        year, month, day, hour, minute, tzinfo=datetime.timezone.utc
    )


class TestObservationSortDateTime(unittest.TestCase):
    """observed_on_string carries a 12-hour clock on Mushroom Observer and
    older iNaturalist records, so the trailing-token strip that removes a
    timezone abbreviation must leave AM/PM alone."""

    def test_pm_is_not_stripped_as_a_timezone_abbreviation(self):
        self.assertEqual(
            inat.observation_sort_datetime(
                {
                    "observed_on_string": "2019/05/23 1:20 PM",
                    "observed_time_zone": "America/Los_Angeles",
                }
            ),
            utc(2019, 5, 23, 20, 20),
        )

    def test_lowercase_meridiem_in_long_form_date(self):
        self.assertEqual(
            inat.observation_sort_datetime(
                {
                    "observed_on_string": "May 23, 2019 1:20 pm",
                    "observed_time_zone": "America/Los_Angeles",
                }
            ),
            utc(2019, 5, 23, 20, 20),
        )

    def test_am_keeps_the_morning_hour(self):
        self.assertEqual(
            inat.observation_sort_datetime(
                {
                    "observed_on_string": "2019/05/23 1:20 AM",
                    "observed_time_zone": "America/Los_Angeles",
                }
            ),
            utc(2019, 5, 23, 8, 20),
        )

    def test_meridiem_followed_by_a_real_abbreviation_still_resolves(self):
        self.assertEqual(
            inat.observation_sort_datetime(
                {"observed_on_string": "2019/05/23 1:20 PM PDT"},
            ),
            utc(2019, 5, 23, 20, 20),
        )

    def test_afternoon_sorts_after_morning_on_the_same_day(self):
        morning = inat.observation_sort_datetime(
            {
                "observed_on_string": "2019/05/23 9:00 AM",
                "observed_time_zone": "America/Los_Angeles",
            }
        )
        afternoon = inat.observation_sort_datetime(
            {
                "observed_on_string": "2019/05/23 1:20 PM",
                "observed_time_zone": "America/Los_Angeles",
            }
        )
        self.assertLess(morning, afternoon)

    def test_twenty_four_hour_clock_is_unaffected(self):
        self.assertEqual(
            inat.observation_sort_datetime(
                {
                    "observed_on_string": "2019/05/23 13:20",
                    "observed_time_zone": "America/Los_Angeles",
                }
            ),
            utc(2019, 5, 23, 20, 20),
        )

    def test_zoneless_clock_time_keeps_its_time_as_utc(self):
        # Flattening these to midnight would tie every same-day observation and
        # drop the printed sheet back to input order.
        morning = inat.observation_sort_datetime(
            {"observed_on_string": "2023-05-14 06:15"}
        )
        evening = inat.observation_sort_datetime(
            {"observed_on_string": "2023-05-14 19:40"}
        )
        self.assertEqual(morning, utc(2023, 5, 14, 6, 15))
        self.assertEqual(evening, utc(2023, 5, 14, 19, 40))
        self.assertLess(morning, evening)

    def test_zoneless_meridiem_time_keeps_its_time_as_utc(self):
        self.assertEqual(
            inat.observation_sort_datetime(
                {"observed_on_string": "2023-05-14 1:20 PM"}
            ),
            utc(2023, 5, 14, 13, 20),
        )

    def test_date_only_string_uses_midnight_utc(self):
        self.assertEqual(
            inat.observation_sort_datetime({"observed_on_string": "2019/05/23"}),
            utc(2019, 5, 23),
        )

    def test_time_observed_at_still_wins(self):
        self.assertEqual(
            inat.observation_sort_datetime(
                {
                    "time_observed_at": "2019-05-23T13:20:00-07:00",
                    "observed_on_string": "2019/05/23 1:20 PM",
                }
            ),
            utc(2019, 5, 23, 20, 20),
        )


if __name__ == "__main__":
    unittest.main()
