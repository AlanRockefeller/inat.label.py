"""CSV Upload in Add Observations: ID extraction and the 1,000-observation run."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import flask  # noqa: F401
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc

from test_security_limits import LimitsTestCase

APP_DIR = Path(__file__).resolve().parents[1]


def extract(csv_text, limit=None):
    """Run extractCsvObservationIds in Node on *csv_text*."""
    args = json.dumps(csv_text) + ("" if limit is None else f", {int(limit)}")
    script = (
        "const h = require('./static/addobs_helpers.js');"
        f"process.stdout.write(JSON.stringify(h.extractCsvObservationIds({args})));"
    )
    completed = subprocess.run(
        ["node", "-e", script], cwd=APP_DIR, check=True, capture_output=True, text=True
    )
    return json.loads(completed.stdout)


class CsvObservationIdExtractionTestCase(unittest.TestCase):
    def test_single_column_without_header_crlf(self):
        result = extract("255645737\r\n255644217\r\n255643649\r\n")
        self.assertTrue(result["ok"])
        self.assertEqual(result["ids"], ["255645737", "255644217", "255643649"])

    def test_inaturalist_export_uses_id_and_ignores_other_number_columns(self):
        result = extract(
            "id,uuid,user_id,taxon_id,latitude,url\n"
            "12,a-b,5,47170,37.9,https://www.inaturalist.org/observations/12\n"
            "13,c-d,5,47170,37.8,https://www.inaturalist.org/observations/13\n"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["ids"], ["12", "13"])
        self.assertEqual(result["column"], "id")

    def test_header_synonyms_with_other_delimiters(self):
        for text in (
            "Observation ID;Notes\n555;\"a; b\"\n556;c\n",
            "Notes\tiNat #\nx\t555\ny\t556\n",
        ):
            with self.subTest(text=text):
                result = extract(text)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["ids"], ["555", "556"])

    def test_url_column_found_without_a_recognized_header(self):
        result = extract(
            "name,link\nfoo,https://www.inaturalist.org/observations/99\n"
            "bar,https://inaturalist.ca/observations/100?x=1\n"
        )
        self.assertEqual(result["ids"], ["99", "100"])

    def test_duplicates_bom_quotes_and_non_numbers_are_reported(self):
        result = extract('﻿id,notes\n"12","a, ""quoted"" note"\n12,again\nabc,bad\n0013,lead\n')
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ids"], ["12", "13"])
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["skipped"], 1)

    def test_ambiguous_number_columns_explain_how_to_fix(self):
        result = extract("a,b\n1,2\n3,4\n")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ambiguous")
        self.assertIn("“id”", result["error"])

    def test_unparseable_and_non_csv_files_get_helpful_errors(self):
        unclosed = extract('id\n"12\n13\n')
        self.assertEqual(unclosed["code"], "unparseable")
        self.assertIn("quotation mark", unclosed["error"])

        spreadsheet = extract("PK\u0003\u0004binary")
        self.assertEqual(spreadsheet["code"], "not_csv")
        self.assertIn("export it as CSV", spreadsheet["error"])

        self.assertEqual(extract("")["code"], "empty")
        self.assertEqual(extract("hello\nworld\n")["code"], "no_ids")

    def test_limit_is_1000_with_a_friendly_message(self):
        at_limit = extract("\n".join(str(n) for n in range(1, 1001)))
        self.assertTrue(at_limit["ok"])
        self.assertEqual(len(at_limit["ids"]), 1000)

        over = extract("\n".join(str(n) for n in range(1, 1002)))
        self.assertFalse(over["ok"])
        self.assertEqual(over["code"], "too_many")
        self.assertEqual(over["count"], 1001)
        self.assertIn("at most 1,000", over["error"])
        self.assertIn("Split it into smaller files", over["error"])


class CsvUploadTemplateTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (APP_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    def test_csv_upload_segment_follows_mushroom_observer(self):
        mo = self.template.index('name="addObsSource" value="mo"')
        csv = self.template.index('name="addObsSource" value="csv"')
        self.assertLess(mo, csv)
        self.assertIn("CSV Upload", self.template[csv:csv + 200])

    def test_run_and_search_caps_are_separate(self):
        self.assertIn("const maxObservations = 1000;", self.template)
        self.assertIn("const maxSearchResults = 500;", self.template)


class RunCapTestCase(LimitsTestCase):
    @patch("app.subprocess.Popen")
    def test_print_accepts_1000_observations_as_multipart(self, popen):
        proc = MagicMock()
        proc.poll.return_value = None
        popen.return_value = proc
        ids = [str(n) for n in range(1, 1001)]
        response = self.post(
            "/labels/print_start",
            data={"format": "pdf", "observations[]": ids, "sort": "", "custom_fields": ""},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(popen.call_args.args[0][-1000:], ids)

    @patch("app.subprocess.Popen")
    def test_print_refuses_1001_observations(self, popen):
        ids = [str(n) for n in range(1, 1002)]
        response = self.post(
            "/labels/print_start",
            data={"format": "pdf", "observations[]": ids},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("max 1000", response.get_json()["error"])
        popen.assert_not_called()

    def test_search_cap_stays_at_500(self):
        self.assertEqual(labels_app.MAX_OBS_PER_REQUEST, 1000)
        self.assertEqual(labels_app.MAX_SEARCH_RESULTS, 500)


if __name__ == "__main__":
    unittest.main()
