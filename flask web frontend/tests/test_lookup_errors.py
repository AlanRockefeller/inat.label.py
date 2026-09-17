import os
import unittest
from unittest.mock import MagicMock, patch

import requests

os.environ.setdefault("LABELS_DISABLE_FILE_LOGGING", "1")

try:
    import flask  # noqa: F401
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc


class TestLookupErrorClassification(unittest.TestCase):
    @patch("app.inat_api_get")
    def test_missing_inaturalist_observation_is_not_found(self, inat_api_get):
        response = MagicMock()
        response.json.return_value = {"results": []}
        inat_api_get.return_value = response

        item = labels_app.lookup_batch_internal(["44444"])["items"][0]

        self.assertEqual(item["status"], 404)
        self.assertEqual(
            item["error"], "iNaturalist Observation #44444 does not exist"
        )

    @patch("app.inat_api_get")
    def test_inaturalist_api_failure_is_retryable_server_error(self, inat_api_get):
        inat_api_get.side_effect = requests.exceptions.Timeout("upstream timeout")

        item = labels_app.lookup_batch_internal(["44444"])["items"][0]

        self.assertEqual(item["status"], 500)
        self.assertIn("Could not fetch this observation from iNaturalist", item["error"])

    @patch("app.requests.get")
    def test_missing_mushroom_observer_observation_matches_not_found_copy(
        self, requests_get
    ):
        response = MagicMock()
        response.json.return_value = {"results": []}
        requests_get.return_value = response

        item = labels_app.lookup_batch_internal(["MO4444444444444"])["items"][0]

        self.assertEqual(item["status"], 404)
        self.assertEqual(
            item["error"],
            "Mushroom Observer observation #4444444444444 does not exist",
        )


if __name__ == "__main__":
    unittest.main()
