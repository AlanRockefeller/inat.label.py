import os
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("LABELS_DISABLE_FILE_LOGGING", "1")

try:
    import app as labels_app
    from date_windows import build_date_windows
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Application dependencies are unavailable: {exc}") from exc


class TestBuildDateWindows(unittest.TestCase):
    def test_zero_observations(self):
        self.assertEqual(build_date_windows({}, "2026-01-01", "2026-01-31"), [])

    def test_one_observation(self):
        self.assertEqual(
            build_date_windows({"2026-01-12": 1}, "2026-01-01", "2026-01-31"),
            [
                {
                    "start": "2026-01-12",
                    "end": "2026-01-12",
                    "count": 1,
                    "over_cap_single_day": False,
                }
            ],
        )

    def test_exactly_cap(self):
        windows = build_date_windows(
            {"2026-01-01": 250, "2026-01-02": 250},
            "2026-01-01",
            "2026-01-02",
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["count"], 500)

    def test_501_across_multiple_dates_keeps_days_intact(self):
        windows = build_date_windows(
            {"2026-04-01": 250, "2026-04-02": 251},
            "2026-04-01",
            "2026-04-02",
        )
        self.assertEqual(
            windows,
            [
                {
                    "start": "2026-04-02",
                    "end": "2026-04-02",
                    "count": 251,
                    "over_cap_single_day": False,
                },
                {
                    "start": "2026-04-01",
                    "end": "2026-04-01",
                    "count": 250,
                    "over_cap_single_day": False,
                },
            ],
        )

    def test_1240_observations_create_three_windows(self):
        windows = build_date_windows(
            {
                "2026-01-01": 400,
                "2026-01-02": 400,
                "2026-01-03": 400,
                "2026-01-04": 40,
            },
            "2026-01-01",
            "2026-01-04",
        )
        self.assertEqual([window["count"] for window in windows], [440, 400, 400])
        self.assertEqual(sum(window["count"] for window in windows), 1240)

    def test_repeated_boundary_date_is_aggregated_not_split(self):
        windows = build_date_windows(
            [
                ("2026-03-01", 450),
                ("2026-03-02", 30),
                ("2026-03-02", 30),
            ],
            "2026-03-01",
            "2026-03-02",
        )
        self.assertEqual([window["count"] for window in windows], [60, 450])
        self.assertEqual(windows[0]["start"], "2026-03-02")

    def test_dense_boundary_forces_preceding_window_under_cap(self):
        windows = build_date_windows(
            {"2026-04-01": 490, "2026-04-02": 20},
            "2026-04-01",
            "2026-04-02",
        )
        self.assertEqual([window["count"] for window in windows], [20, 490])

    def test_single_day_over_cap_is_marked_unsplittable(self):
        self.assertEqual(
            build_date_windows({"2026-06-12": 731}, "2026-06-12", "2026-06-12"),
            [
                {
                    "start": "2026-06-12",
                    "end": "2026-06-12",
                    "count": 731,
                    "over_cap_single_day": True,
                }
            ],
        )

    def test_ascending_and_descending_input_are_deterministic(self):
        ascending = [
            ("2026-01-01", 250),
            ("2026-01-02", 251),
            ("2026-01-03", 10),
        ]
        descending = list(reversed(ascending))
        self.assertEqual(
            build_date_windows(ascending, "2026-01-01", "2026-01-03"),
            build_date_windows(descending, "2026-01-01", "2026-01-03"),
        )

    def test_invalid_missing_and_out_of_range_dates_are_ignored(self):
        windows = build_date_windows(
            [
                (None, 20),
                ("not-a-date", 20),
                ("2025-12-31", 20),
                ("2026-01-01", 1),
                ("2026-01-31T18:12:00Z", 2),
                ("2026-02-01", 20),
            ],
            "2026-01-01",
            "2026-01-31",
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["start"], "2026-01-01")
        self.assertEqual(windows[0]["end"], "2026-01-31")
        self.assertEqual(windows[0]["count"], 3)

    def test_windows_have_no_overlap_or_gap_between_boundaries(self):
        windows = build_date_windows(
            {
                "2026-01-01": 450,
                "2026-01-05": 100,
                "2026-01-09": 450,
            },
            "2026-01-01",
            "2026-01-09",
            newest_first=False,
        )
        for earlier, later in zip(windows, windows[1:]):
            earlier_end = date.fromisoformat(earlier["end"])
            later_start = date.fromisoformat(later["start"])
            self.assertEqual(later_start, earlier_end + timedelta(days=1))
        self.assertTrue(all(window["count"] <= 500 for window in windows))


class TestDateWindowEndpoint(unittest.TestCase):
    def setUp(self):
        # TESTING disables the per-IP request limits, which would otherwise
        # reject the repeated searches these tests make from one address.
        labels_app.app.config["TESTING"] = True
        self.client = labels_app.app.test_client()

    @staticmethod
    def response(payload):
        response = MagicMock()
        response.json.return_value = payload
        return response

    @patch.object(labels_app, "MAX_OBS_PER_REQUEST", 3)
    @patch("app.inat_api_get")
    def test_inaturalist_over_cap_adds_windows_with_one_histogram_request(
        self, inat_api_get
    ):
        observations = [
            {
                "id": index,
                "observed_on": f"2026-01-0{1 if index < 3 else 2}",
                "taxon": {"name": "Testus", "iconic_taxon_name": "Fungi"},
                "user": {"login": "observer"},
            }
            for index in range(1, 5)
        ]
        inat_api_get.side_effect = [
            self.response({"results": [{"id": 47170}]}),
            self.response({"total_results": 4, "results": observations}),
            self.response({"results": {"day": {"2026-01-01": 2, "2026-01-02": 2}}}),
        ]

        response = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-01-01",
                "d2": "2026-01-02",
                "username": "observer",
                "taxon": "Life",
                "source": "inat",
                "date_mode": "observed",
            },
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["total_count"], 4)
        self.assertEqual(sum(window["count"] for window in payload["windows"]), 4)
        self.assertEqual(inat_api_get.call_count, 3)
        histogram_call = inat_api_get.call_args_list[-1]
        self.assertTrue(histogram_call.args[0].endswith("/observations/histogram"))
        self.assertEqual(histogram_call.kwargs["params"]["interval"], "day")
        self.assertEqual(histogram_call.kwargs["params"]["date_field"], "observed")

    @patch.object(labels_app, "MAX_OBS_PER_REQUEST", 3)
    @patch("app.inat_api_get")
    def test_endpoint_marks_single_day_over_cap(self, inat_api_get):
        observations = [
            {
                "id": index,
                "observed_on": "2026-06-12",
                "taxon": {"name": "Testus", "iconic_taxon_name": "Fungi"},
                "user": {"login": "observer"},
            }
            for index in range(1, 5)
        ]
        inat_api_get.side_effect = [
            self.response({"results": [{"id": 47170}]}),
            self.response({"total_results": 4, "results": observations}),
            self.response({"results": {"day": {"2026-06-12": 4}}}),
        ]

        payload = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-06-12",
                "d2": "2026-06-12",
                "username": "observer",
                "taxon": "Life",
                "source": "inat",
                "date_mode": "observed",
            },
        ).get_json()

        self.assertEqual(
            payload["windows"],
            [
                {
                    "start": "2026-06-12",
                    "end": "2026-06-12",
                    "count": 4,
                    "over_cap_single_day": True,
                }
            ],
        )

    @patch.object(labels_app, "MAX_OBS_PER_REQUEST", 3)
    @patch("app.inat_api_get")
    def test_created_mode_uses_created_histogram_semantics(self, inat_api_get):
        observations = [
            {
                "id": index,
                "observed_on": "2025-01-01",
                "created_at": f"2026-02-0{1 if index < 3 else 2}T12:00:00Z",
                "taxon": {"name": "Testus", "iconic_taxon_name": "Fungi"},
                "user": {"login": "observer"},
            }
            for index in range(1, 5)
        ]
        inat_api_get.side_effect = [
            self.response({"results": [{"id": 47170}]}),
            self.response({"total_results": 4, "results": observations}),
            self.response({"results": {"day": {"2026-02-01": 2, "2026-02-02": 2}}}),
        ]

        payload = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-02-01",
                "d2": "2026-02-02",
                "username": "observer",
                "taxon": "Life",
                "source": "inat",
                "date_mode": "created",
            },
        ).get_json()

        histogram_params = inat_api_get.call_args_list[-1].kwargs["params"]
        self.assertEqual(histogram_params["date_field"], "created")
        self.assertEqual(histogram_params["created_d1"], "2026-02-01")
        self.assertEqual(histogram_params["created_d2"], "2026-02-02")
        self.assertEqual(payload["windows"][0]["start"], "2026-02-02")

    @patch.object(labels_app, "MAX_OBS_PER_REQUEST", 3)
    @patch("app.requests.get")
    def test_mo_created_mode_reuses_pages_and_created_dates(
        self, requests_get
    ):
        page_one = self.response(
            {
                "number_of_results": 5,
                "number_of_pages": 2,
                "results": [
                    {
                        "id": index,
                        "date": "2026-03-01",
                        "created_at": "2026-04-01T12:00:00Z",
                        "consensus_name": "Testus",
                    }
                    for index in range(1, 5)
                ],
            }
        )
        page_two = self.response(
            {
                "number_of_results": 5,
                "number_of_pages": 2,
                "results": [
                    {
                        "id": 5,
                        "date": "2026-03-02",
                        "created_at": "2026-04-02T12:00:00Z",
                        "consensus_name": "Testus",
                    }
                ],
            }
        )
        requests_get.side_effect = [page_one, page_two]

        payload = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-04-01",
                "d2": "2026-04-02",
                "username": "observer",
                "taxon": "Fungi",
                "source": "mo",
                "date_mode": "created",
            },
        ).get_json()

        self.assertEqual(requests_get.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["params"]["detail"] == "low"
                for call in requests_get.call_args_list
            )
        )
        self.assertEqual(payload["total_count"], 5)
        self.assertEqual(sum(window["count"] for window in payload["windows"]), 5)
        self.assertEqual(
            requests_get.call_args_list[0].kwargs["params"]["created_at"],
            "2026-04-01-2026-04-02",
        )
        self.assertEqual(payload["windows"][0]["start"], "2026-04-02")

    @patch.object(labels_app, "MAX_OBS_PER_REQUEST", 3)
    @patch("app.inat_api_get")
    def test_at_or_below_cap_omits_windows_and_histogram(self, inat_api_get):
        observations = [
            {
                "id": index,
                "observed_on": "2026-01-01",
                "taxon": {"name": "Testus", "iconic_taxon_name": "Fungi"},
                "user": {"login": "observer"},
            }
            for index in range(1, 4)
        ]
        inat_api_get.side_effect = [
            self.response({"results": [{"id": 47170}]}),
            self.response({"total_results": 3, "results": observations}),
            self.response({"total_results": 3, "results": []}),
        ]

        payload = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-01-01",
                "d2": "2026-01-01",
                "username": "observer",
                "taxon": "Life",
                "source": "inat",
                "date_mode": "observed",
            },
        ).get_json()

        self.assertNotIn("windows", payload)
        self.assertEqual(inat_api_get.call_count, 3)

    @patch.object(labels_app, "MAX_OBS_PER_REQUEST", 3)
    @patch("app.inat_api_get")
    def test_histogram_failure_keeps_capped_results_without_windows(
        self, inat_api_get
    ):
        observations = [
            {
                "id": index,
                "observed_on": "2026-01-01",
                "taxon": {"name": "Testus", "iconic_taxon_name": "Fungi"},
                "user": {"login": "observer"},
            }
            for index in range(1, 5)
        ]
        inat_api_get.side_effect = [
            self.response({"results": [{"id": 47170}]}),
            self.response({"total_results": 4, "results": observations}),
            labels_app.requests.Timeout("histogram timed out"),
        ]

        with patch.object(labels_app.api_error_logger, "warning") as warning:
            response = self.client.post(
                "/labels/find_observations",
                data={
                    "d1": "2026-01-01",
                    "d2": "2026-01-02",
                    "username": "observer",
                    "taxon": "Life",
                    "source": "inat",
                    "date_mode": "observed",
                },
            )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["total_count"], 4)
        self.assertEqual(len(payload["items"]), 4)
        self.assertNotIn("windows", payload)
        warning.assert_called_once()

    @patch.object(labels_app, "MAX_OBS_PER_REQUEST", 3)
    def test_incomplete_daily_counts_do_not_return_incorrect_windows(self):
        with patch.object(labels_app.app.logger, "warning"):
            windows = labels_app._windows_for_complete_daily_counts(
                {"2026-01-01": 3}, 4, "2026-01-01", "2026-01-02"
            )
        self.assertEqual(windows, [])


class TestDateWindowTemplateIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(labels_app.app.root_path) / "templates" / "index.html"
        ).read_text(encoding="utf-8")

    def test_panel_has_accessible_buttons_and_explicit_unsplittable_warning(self):
        self.assertIn('id="addObsWindowsPanel"', self.template)
        self.assertIn(
            'type="button" class="btn btn-outline addobs-window-chip"', self.template
        )
        self.assertIn("over_cap_single_day", self.template)
        self.assertIn("narrow by taxon", self.template)

    def test_unavailable_windows_use_one_consolidated_warning(self):
        render_helper = self.template.split(
            "function renderAddObsWindows(windows, totalCount, shownCount)", 1
        )[1].split("function setAddObsSearchState", 1)[0]
        self.assertIn("LabelsAddObsHelpers.overCapSummary", render_helper)
        self.assertIn(
            "This search cannot be divided safely into date-only runs.",
            render_helper,
        )
        self.assertIn("sorting below applies only", render_helper)
        self.assertNotIn("Exact date windows are unavailable", self.template)
        self.assertIn("hasUsableWindow", render_helper)
        self.assertIn(".html(chips)", render_helper)

    def test_panel_is_pinned_before_toolbar_and_outside_scrolling_list(self):
        results_region = self.template.split(
            'id="addObsResults" class="addobs-results-region"', 1
        )[1].split('id="addObsList" class="addobs-results-list"', 1)[0]
        panel_position = results_region.index('id="addObsWindowsPanel"')
        toolbar_position = results_region.index('class="addobs-results-toolbar"')
        self.assertLess(panel_position, toolbar_position)

    def test_chip_applies_dates_and_schedules_one_immediate_search(self):
        handler = self.template.split(
            '$("#addObsWindowsPanel").on("click", ".addobs-window-chip"', 1
        )[1].split("});", 1)[0]
        self.assertIn(".val(this.dataset.start)", handler)
        self.assertIn(".val(this.dataset.end)", handler)
        self.assertEqual(handler.count("scheduleAddObsSearch"), 1)
        self.assertIn("immediate: true", handler)


if __name__ == "__main__":
    unittest.main()
