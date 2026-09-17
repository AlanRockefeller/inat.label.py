import csv
import io
import unittest
from unittest.mock import patch

try:
    import flask  # noqa: F401
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc


def inat_observation(obs_id, **overrides):
    """A minimal iNaturalist observation record shaped like the API's."""
    observation = {
        "id": int(obs_id),
        "taxon": {"name": "Amanita muscaria", "preferred_common_name": "Fly Agaric"},
        "user": {"login": "someone", "name": "Some One"},
        "observed_on": "2026-07-31",
        "observed_on_string": "2026-07-31 09:15:00",
        "time_observed_at": "2026-07-31T09:15:00-07:00",
        "observed_time_zone": "America/Los_Angeles",
        "place_guess": "Marin County, CA, US",
        "geojson": {"type": "Point", "coordinates": [-122.5, 37.9]},
        "positional_accuracy": 10,
        "ofvs": [],
    }
    observation.update(overrides)
    return observation


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class CsvExportTestCase(unittest.TestCase):
    def setUp(self):
        labels_app.app.config["TESTING"] = True
        self.client = labels_app.app.test_client()

    def export(self, observations, **extra):
        data = {"observations[]": observations}
        data.update(extra)
        response = self.client.post("/labels/submit", data=data)
        return response

    def rows(self, response):
        self.assertEqual(response.status_code, 200)
        reader = csv.reader(io.StringIO(response.get_data(as_text=True)))
        return list(reader)

    def patched_inat(self, records):
        """Serve the CSV endpoint's batch fetch from *records* instead of iNat."""
        by_id = {str(record["id"]): record for record in records}

        def fake_get(url, params=None, **kwargs):
            requested = (params or {}).get("id", "").split(",")
            return FakeResponse(
                {"results": [by_id[i] for i in requested if i in by_id]}
            )

        return patch("app.inat_api_get", side_effect=fake_get)


class TestCsvSortOrder(CsvExportTestCase):
    """The Sort dropdown drives the CSV, not just the printed labels."""

    def setUp(self):
        super().setUp()
        # Entered newest first, so entry order, observation number, and date all
        # disagree; each sort mode has to produce a visibly different order.
        self.records = [
            inat_observation(
                300,
                observed_on="2026-07-31",
                observed_on_string="2026-07-31 18:00:00",
                time_observed_at="2026-07-31T18:00:00-07:00",
            ),
            inat_observation(
                100,
                observed_on="2026-07-31",
                observed_on_string="2026-07-31 06:00:00",
                time_observed_at="2026-07-31T06:00:00-07:00",
            ),
            inat_observation(
                200,
                observed_on="2026-07-30",
                observed_on_string="2026-07-30 12:00:00",
                time_observed_at="2026-07-30T12:00:00-07:00",
            ),
        ]
        self.entered = ["300", "100", "200"]

    def exported_numbers(self, **extra):
        with self.patched_inat(self.records):
            rows = self.rows(self.export(self.entered, **extra))
        return [row[1] for row in rows[1:]]

    def test_oldest_first_orders_by_observation_time(self):
        self.assertEqual(self.exported_numbers(sort="date"), ["200", "100", "300"])

    def test_newest_first_orders_by_observation_time(self):
        self.assertEqual(self.exported_numbers(sort="date-desc"), ["300", "100", "200"])

    def test_default_orders_by_observation_number(self):
        self.assertEqual(self.exported_numbers(), ["100", "200", "300"])

    def test_as_entered_keeps_input_order(self):
        self.assertEqual(self.exported_numbers(sort="none"), self.entered)

    def test_id_column_numbers_the_exported_order(self):
        with self.patched_inat(self.records):
            rows = self.rows(self.export(self.entered, sort="date"))
        self.assertEqual([row[0] for row in rows[1:]], ["1", "2", "3"])

    def test_voucher_sort_uses_the_voucher_field(self):
        records = [
            inat_observation(100, ofvs=[{"name": "Voucher Number(s)", "value": "AR22"}]),
            inat_observation(200, ofvs=[]),
            inat_observation(300, ofvs=[{"name": "Voucher Number", "value": "AR3"}]),
        ]
        with self.patched_inat(records):
            rows = self.rows(self.export(["100", "200", "300"], sort="voucher"))
        # Same prefix, so the trailing number decides; the unvouchered row sorts
        # last, as it does on the labels.
        self.assertEqual([row[1] for row in rows[1:]], ["300", "100", "200"])

    def test_mo_herbarium_id_does_not_participate_in_voucher_sort(self):
        mo_result = {
            "consensus": {"name": "Amanita muscaria"},
            "owner": {"login_name": "observer"},
            "date": "2026-07-31",
            "herbarium_id": "A1",
        }
        records = [
            inat_observation(100, ofvs=[{"name": "Voucher Number", "value": "Z9"}])
        ]
        with self.patched_inat(records), patch(
            "app.requests.get",
            return_value=FakeResponse({"results": [mo_result]}),
        ):
            rows = self.rows(self.export(["MO123", "100"], sort="voucher"))
        self.assertEqual([row[1] for row in rows[1:]], ["100", "MO123"])

    def test_custom_field_sort_uses_the_named_field(self):
        records = [
            inat_observation(100, ofvs=[{"name": "Habitat", "value": "B"}]),
            inat_observation(200, ofvs=[{"name": "Habitat", "value": "A"}]),
        ]
        with self.patched_inat(records):
            rows = self.rows(
                self.export(["100", "200"], sort="custom", sort_field="Habitat")
            )
        self.assertEqual([row[1] for row in rows[1:]], ["200", "100"])

    def test_custom_sort_ignores_a_raw_field_not_rendered_on_labels(self):
        records = [
            inat_observation(100, ofvs=[{"name": "Shelf Code", "value": "B"}]),
            inat_observation(200, ofvs=[{"name": "Shelf Code", "value": "A"}]),
        ]
        with self.patched_inat(records):
            rows = self.rows(
                self.export(["100", "200"], sort="custom", sort_field="Shelf Code")
            )
        self.assertEqual([row[1] for row in rows[1:]], ["100", "200"])

    def test_custom_sort_uses_an_explicitly_added_field(self):
        records = [
            inat_observation(100, ofvs=[{"name": "Shelf Code", "value": "B"}]),
            inat_observation(200, ofvs=[{"name": "Shelf Code", "value": "A"}]),
        ]
        with self.patched_inat(records):
            rows = self.rows(
                self.export(
                    ["100", "200"],
                    sort="custom",
                    sort_field="Shelf Code",
                    use_custom="on",
                    **{"custom_args[]": "+Shelf Code"},
                )
            )
        self.assertEqual([row[1] for row in rows[1:]], ["200", "100"])

    def test_custom_sort_ignores_a_suppressed_default_field(self):
        records = [
            inat_observation(100, ofvs=[{"name": "Habitat", "value": "B"}]),
            inat_observation(200, ofvs=[{"name": "Habitat", "value": "A"}]),
        ]
        with self.patched_inat(records):
            rows = self.rows(
                self.export(
                    ["100", "200"],
                    sort="custom",
                    sort_field="Habitat",
                    use_custom="on",
                    **{"custom_args[]": "-Habitat"},
                )
            )
        self.assertEqual([row[1] for row in rows[1:]], ["100", "200"])

    def test_invalid_sort_requests_are_rejected(self):
        for extra in (
            {"sort": "sideways"},
            {"sort": "custom"},
            {"sort": "custom", "sort_field": "bad;field`"},
        ):
            with self.subTest(**extra):
                response = self.export(["100"], **extra)
                self.assertEqual(response.status_code, 400)


class TestCsvColumns(CsvExportTestCase):
    def test_header_lists_every_column(self):
        with self.patched_inat([inat_observation(100)]):
            rows = self.rows(self.export(["100"]))
        self.assertEqual(rows[0], labels_app.CSV_COLUMNS)

    def test_row_carries_date_time_place_and_coordinates(self):
        with self.patched_inat(
            [inat_observation(100, ofvs=[{"name": "Voucher Number", "value": "AR1"}])]
        ):
            rows = self.rows(self.export(["100"]))
        row = dict(zip(labels_app.CSV_COLUMNS, rows[1]))
        self.assertEqual(row["Observation Number"], "100")
        self.assertEqual(row["Scientific Name"], "Amanita muscaria")
        self.assertEqual(row["Common Name"], "Fly Agaric")
        self.assertEqual(row["Observer"], "someone")
        self.assertEqual(row["Observer Name"], "Some One")
        self.assertEqual(row["Date Observed"], "2026-07-31")
        self.assertEqual(row["Time Observed"], "09:15:00")
        self.assertEqual(row["Location"], "Marin County, CA, US")
        self.assertEqual(row["Latitude"], "37.90000")
        self.assertEqual(row["Longitude"], "-122.50000")
        self.assertEqual(row["Coordinate Accuracy"], "10m")
        self.assertEqual(row["Voucher Number"], "AR1")
        self.assertEqual(
            row["URL"], "https://www.inaturalist.org/observations/100"
        )

    def test_date_only_observation_leaves_the_time_blank(self):
        """A midnight placeholder would read as a real 00:00 collection time."""
        record = inat_observation(
            100,
            time_observed_at=None,
            observed_on_string="2026-07-31",
        )
        with self.patched_inat([record]):
            rows = self.rows(self.export(["100"]))
        row = dict(zip(labels_app.CSV_COLUMNS, rows[1]))
        self.assertEqual(row["Date Observed"], "2026-07-31")
        self.assertEqual(row["Time Observed"], "")

    def test_private_coordinates_are_not_exported(self):
        record = inat_observation(100, geoprivacy="private", geojson=None)
        with self.patched_inat([record]):
            rows = self.rows(self.export(["100"]))
        row = dict(zip(labels_app.CSV_COLUMNS, rows[1]))
        self.assertEqual(row["Latitude"], "")
        self.assertEqual(row["Longitude"], "")

    def test_negative_longitude_stays_a_number(self):
        """Formula-escaping a negative coordinate would export it as text."""
        self.assertEqual(labels_app.safe_csv_field("-122.5"), "-122.5")
        self.assertEqual(labels_app.safe_csv_field("=1+1"), "'=1+1")
        self.assertEqual(labels_app.safe_csv_field("-cmd|' /c calc"), "'-cmd|' /c calc")

    def test_bugguide_entries_get_a_row_instead_of_being_dropped(self):
        with self.patched_inat([inat_observation(100)]):
            response = self.export(["100", "BG12345"])
            rows = self.rows(response)
        self.assertIsNone(response.headers.get("X-Skipped-Observations"))
        bugguide = dict(zip(labels_app.CSV_COLUMNS, rows[2]))
        self.assertEqual(bugguide["Observation Number"], "BG12345")
        self.assertEqual(bugguide["URL"], "https://bugguide.net/node/view/12345")

    def test_mo_herbarium_id_has_its_own_column_and_is_not_a_voucher(self):
        mo_result = {
            "consensus": {"name": "Amanita muscaria"},
            "owner": {"login_name": "observer", "legal_name": "Observer Name"},
            "date": "2026-07-31",
            "herbarium_id": "UC 12345",
        }
        with patch(
            "app.requests.get",
            return_value=FakeResponse({"results": [mo_result]}),
        ):
            rows = self.rows(self.export(["MO123"]))
        row = dict(zip(labels_app.CSV_COLUMNS, rows[1]))
        self.assertEqual(row["Herbarium Catalog Number"], "UC 12345")
        self.assertEqual(row["Voucher Number"], "")


if __name__ == "__main__":
    unittest.main()
