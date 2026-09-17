import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("LABELS_DISABLE_FILE_LOGGING", "1")

try:
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc


class TestFindObservationsRichFields(unittest.TestCase):
    def setUp(self):
        # TESTING disables the per-IP request limits, which would otherwise
        # reject the repeated searches these tests make from one address.
        labels_app.app.config["TESTING"] = True
        self.client = labels_app.app.test_client()

    def test_missing_or_full_size_only_photo_returns_null(self):
        self.assertIsNone(labels_app._thumbnail_url({}))
        self.assertIsNone(
            labels_app._thumbnail_url(
                {"primary_image": {"url": "https://example.test/full.jpg"}}
            )
        )

    def test_mushroom_observer_primary_image_id_uses_160px_thumbnail(self):
        self.assertEqual(
            labels_app._mushroom_observer_thumbnail_url({"primary_image_id": 123}),
            "https://mushroomobserver.org/images/thumb/123.jpg",
        )
        self.assertIsNone(
            labels_app._mushroom_observer_thumbnail_url({"primary_image_id": None})
        )
        self.assertIsNone(
            labels_app._mushroom_observer_thumbnail_url({"primary_image_id": "bad"})
        )

    def test_locality_prefers_existing_human_readable_field(self):
        observation = {
            "place_guess": "Santa Clara, EC-PA, EC",
            "locality": "Santa Clara, Pastaza, Ecuador",
        }
        self.assertEqual(
            labels_app._concise_locality(observation),
            "Santa Clara, Pastaza, Ecuador",
        )

    def test_locality_cleanup_is_conservative(self):
        self.assertEqual(
            labels_app._clean_locality_text("Berkeley, California, California"),
            "Berkeley, California",
        )
        self.assertEqual(
            labels_app._clean_locality_text("Santa Clara, EC-PA, EC"),
            "Santa Clara, EC-PA, EC",
        )
        self.assertEqual(
            labels_app._clean_locality_text("New York, New York"),
            "New York, New York",
        )

    @patch("app.inat_api_get")
    def test_inaturalist_items_include_rich_additive_fields(self, inat_api_get):
        taxon_response = MagicMock()
        taxon_response.json.return_value = {"results": [{"id": 47170}]}
        observations_response = MagicMock()
        observations_response.json.return_value = {
            "total_results": 327,
            "results": [
                {
                    "id": 327631026,
                    "observed_on": "2026-07-03",
                    "place_guess": "Berkeley, California",
                    "photos": [
                        {"url": "https://static.inaturalist.org/photos/1/square.jpg"}
                    ],
                    "ofvs": [{"id": 55, "name": "Voucher", "value": "V-1"}],
                    "taxon": {
                        "name": "Araneus diadematus",
                        "iconic_taxon_name": "Arachnida",
                    },
                    "user": {"login": "observer"},
                }
            ],
        }
        empty_response = MagicMock()
        empty_response.json.return_value = {"total_results": 327, "results": []}
        inat_api_get.side_effect = [
            taxon_response,
            observations_response,
            empty_response,
        ]

        response = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-07-01",
                "d2": "2026-07-31",
                "username": "observer",
                "taxon": "Life",
                "source": "inat",
                "date_mode": "observed",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total_count"], 327)
        item = payload["items"][0]
        self.assertEqual(item["id"], 327631026)
        self.assertEqual(item["inat_id"], "327631026")
        self.assertEqual(item["scientific_name"], "Araneus diadematus")
        self.assertEqual(item["observed_on"], "2026-07-03")
        self.assertEqual(item["place_guess"], "Berkeley, California")
        self.assertEqual(
            item["photo_url"],
            "https://static.inaturalist.org/photos/1/square.jpg",
        )
        self.assertEqual(item["iconic_taxon_name"], "Arachnida")
        self.assertEqual(item["taxon_color_group"], "orange-animal")
        self.assertEqual(
            item["ofvs"],
            [{"id": 55, "name": "Voucher", "value": "V-1"}],
        )

    @patch("app.requests.get")
    def test_mushroom_observer_items_use_thumbnail_or_null(self, requests_get):
        response = MagicMock()
        response.json.return_value = {
            "number_of_results": 1,
            "number_of_pages": 1,
            "results": [
                {
                    "id": 585855,
                    "date": "2026-07-04",
                    "consensus_name": "Amanita muscaria",
                    "location": {"name": "Tilden Regional Park"},
                    "primary_image_id": 123456,
                }
            ],
        }
        requests_get.return_value = response

        result = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-07-01",
                "d2": "2026-07-31",
                "username": "observer",
                "taxon": "Fungi",
                "source": "mo",
                "date_mode": "observed",
            },
        ).get_json()["items"][0]

        self.assertEqual(result["inat_id"], "MO585855")
        self.assertEqual(result["place_guess"], "Tilden Regional Park")
        self.assertEqual(
            result["photo_url"],
            "https://mushroomobserver.org/images/thumb/123456.jpg",
        )
        self.assertEqual(result["iconic_taxon_name"], "Fungi")

    @patch("app.requests.get")
    def test_mushroom_observer_skips_bare_ids_without_losing_dict_items(
        self, requests_get
    ):
        response = MagicMock()
        response.json.return_value = {
            "number_of_results": 2,
            "number_of_pages": 1,
            "results": [
                585854,
                {
                    "id": 585855,
                    "date": "2026-07-04",
                    "consensus_name": "Amanita muscaria",
                },
            ],
        }
        requests_get.return_value = response

        payload = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-07-01",
                "d2": "2026-07-31",
                "username": "observer",
                "taxon": "Fungi",
                "source": "mo",
                "date_mode": "observed",
            },
        ).get_json()

        self.assertEqual(payload["total_count"], 2)
        self.assertEqual([item["id"] for item in payload["items"]], [585855])


class TestObservationFieldFiltering(unittest.TestCase):
    def setUp(self):
        labels_app.app.config["TESTING"] = True
        self.client = labels_app.app.test_client()
        labels_app._observation_field_cache.clear()
        labels_app._histogram_cache.clear()
        self.addCleanup(labels_app._observation_field_cache.clear)
        self.addCleanup(labels_app._histogram_cache.clear)

    @staticmethod
    def response(payload):
        response = MagicMock()
        response.json.return_value = payload
        return response

    def search_data(self, **overrides):
        data = {
            "d1": "2026-07-01",
            "d2": "2026-07-31",
            "username": "observer",
            "taxon": "47170",
            "source": "inat",
            "date_mode": "observed",
            "obs_field_name": "Personal voucher number",
            "obs_field_id": "123",
        }
        data.update(overrides)
        return data

    @patch("app.inat_api_get")
    def test_autocomplete_skips_short_queries(self, inat_api_get):
        response = self.client.get("/labels/observation_fields?q=x")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"results": []})
        inat_api_get.assert_not_called()

    @patch("app.inat_api_get")
    def test_autocomplete_rejects_overlong_queries(self, inat_api_get):
        response = self.client.get(
            "/labels/observation_fields",
            query_string={"q": "x" * (labels_app.INAT_OBSERVATION_FIELD_QUERY_MAX_LENGTH + 1)},
        )
        self.assertEqual(response.status_code, 400)
        inat_api_get.assert_not_called()

    @patch("app.inat_api_get")
    def test_autocomplete_normalizes_caps_sorts_and_caches(self, inat_api_get):
        fields = [
            {
                "id": index,
                "name": f"Field {index}",
                "datatype": "text",
                "values_count": index,
            }
            for index in range(1, 16)
        ]
        fields.append({"id": "bad", "name": "Ignored", "values_count": 9999})
        inat_api_get.return_value = self.response({"results": fields})

        first = self.client.get("/labels/observation_fields?q=voucher")
        second = self.client.get("/labels/observation_fields?q=VOUCHER")

        self.assertEqual(first.status_code, 200)
        results = first.get_json()["results"]
        self.assertEqual(len(results), labels_app.INAT_OBSERVATION_FIELD_RESULT_LIMIT)
        self.assertEqual(
            results[0],
            {
                "id": 15,
                "name": "Field 15",
                "datatype": "text",
                "values_count": 15,
            },
        )
        self.assertEqual(second.get_json(), first.get_json())
        self.assertEqual(inat_api_get.call_count, 1)
        self.assertEqual(
            inat_api_get.call_args.args[0],
            "https://www.inaturalist.org/observation_fields.json",
        )
        self.assertEqual(inat_api_get.call_args.kwargs["params"], {"q": "voucher"})

    @patch("app.inat_api_get")
    def test_autocomplete_accepts_a_direct_field_list(self, inat_api_get):
        inat_api_get.return_value = self.response(
            [{"id": 9273, "name": "Voucher", "datatype": "text", "values_count": 4}]
        )

        response = self.client.get("/labels/observation_fields?q=direct-list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"][0]["id"], 9273)

    @patch("app.inat_api_get")
    def test_autocomplete_cache_is_bounded(self, inat_api_get):
        inat_api_get.return_value = self.response({"results": []})
        for index in range(labels_app.INAT_OBSERVATION_FIELD_CACHE_MAX_ENTRIES + 5):
            response = self.client.get(
                "/labels/observation_fields", query_string={"q": f"field-{index}"}
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(
            len(labels_app._observation_field_cache),
            labels_app.INAT_OBSERVATION_FIELD_CACHE_MAX_ENTRIES,
        )

    def test_local_field_check_prefers_id_and_falls_back_to_name(self):
        observation = {
            "ofvs": [
                {
                    "id": 7451462,
                    "field_id": 9273,
                    "observation_field": {"id": 9273, "name": "Personal voucher number"},
                    "value": "ABC-1",
                }
            ]
        }
        self.assertTrue(
            labels_app._observation_has_required_field(observation, 9273, "Wrong name")
        )
        self.assertFalse(
            labels_app._observation_has_required_field(
                observation, 7451462, "Wrong name"
            )
        )
        self.assertTrue(
            labels_app._observation_has_required_field(
                observation, 999, " personal VOUCHER NUMBER "
            )
        )
        observation["ofvs"][0]["value"] = "   "
        self.assertFalse(
            labels_app._observation_has_required_field(
                observation, 9273, "Personal voucher number"
            )
        )

    @patch("app.inat_api_get")
    def test_field_filter_is_shared_by_observation_and_histogram_queries(self, inat_api_get):
        observations = [
            {
                "id": index,
                "observed_on": "2026-07-03",
                "taxon": {"name": "Testus", "iconic_taxon_name": "Fungi"},
                "user": {"login": "observer"},
                "ofvs": [
                    {
                        "observation_field": {
                            "id": 123,
                            "name": "Personal voucher number",
                        },
                        "value": str(index),
                    }
                ],
            }
            for index in (1, 2)
        ]
        inat_api_get.side_effect = [
            self.response({"total_results": 138, "results": []}),
            self.response({"total_results": 42, "results": observations}),
            self.response({"results": {"day": {"2026-07-03": 42}}}),
        ]

        with patch.object(labels_app, "MAX_OBS_PER_REQUEST", 1):
            response = self.client.post(
                "/labels/find_observations", data=self.search_data()
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total_count"], 42)
        self.assertEqual(payload["total_in_scope"], 138)
        self.assertEqual(payload["required_observation_field"], "Personal voucher number")
        self.assertEqual(payload["items"][0]["ofvs"], observations[1]["ofvs"])
        observation_params = inat_api_get.call_args_list[1].kwargs["params"]
        histogram_params = inat_api_get.call_args_list[2].kwargs["params"]
        field_key = "field:Personal voucher number"
        self.assertEqual(inat_api_get.call_args_list[0].kwargs["params"]["per_page"], 1)
        self.assertEqual(observation_params[field_key], "")
        self.assertEqual(histogram_params[field_key], observation_params[field_key])
        self.assertNotIn("q", observation_params)
        self.assertNotIn("q", histogram_params)
        self.assertEqual(histogram_params["date_field"], "observed")

    @patch("app.inat_api_get")
    def test_created_date_mode_keeps_field_filter_and_rejects_blank_values(self, inat_api_get):
        results = [
            {
                "id": 1,
                "observed_on": "2026-06-30",
                "taxon": {"name": "Blankus", "iconic_taxon_name": "Fungi"},
                "ofvs": [{"observation_field": {"id": 123, "name": "Personal voucher number"}, "value": " "}],
            },
            {
                "id": 2,
                "observed_on": "2026-06-30",
                "taxon": {"name": "Validus", "iconic_taxon_name": "Fungi"},
                "ofvs": [{"name": "personal VOUCHER number", "value": "V-2"}],
            },
        ]
        inat_api_get.side_effect = [
            self.response({"total_results": 2}),
            self.response({"total_results": 2, "results": results}),
            self.response({"total_results": 0, "results": []}),
        ]

        response = self.client.post(
            "/labels/find_observations",
            data=self.search_data(date_mode="created", obs_field_id="999"),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual([item["id"] for item in payload["items"]], [2])
        filtered_params = inat_api_get.call_args_list[1].kwargs["params"]
        self.assertEqual(filtered_params["created_d1"], "2026-07-01")
        self.assertEqual(filtered_params["created_d2"], "2026-07-31")
        self.assertNotIn("d1", filtered_params)

    @patch("app.inat_api_get")
    def test_created_date_histogram_uses_the_same_field_filter(self, inat_api_get):
        observations = [
            {
                "id": index,
                "observed_on": "2026-06-30",
                "taxon": {"name": "Testus", "iconic_taxon_name": "Fungi"},
                "ofvs": [{"observation_field": {"id": 123}, "value": str(index)}],
            }
            for index in (1, 2)
        ]
        inat_api_get.side_effect = [
            self.response({"total_results": 4}),
            self.response({"total_results": 2, "results": observations}),
            self.response({"results": {"day": {"2026-07-02": 2}}}),
        ]

        with patch.object(labels_app, "MAX_OBS_PER_REQUEST", 1):
            response = self.client.post(
                "/labels/find_observations",
                data=self.search_data(date_mode="created"),
            )

        self.assertEqual(response.status_code, 200)
        observation_params = inat_api_get.call_args_list[1].kwargs["params"]
        histogram_params = inat_api_get.call_args_list[2].kwargs["params"]
        field_key = "field:Personal voucher number"
        self.assertEqual(histogram_params[field_key], observation_params[field_key])
        self.assertEqual(histogram_params["date_field"], "created")
        self.assertIn("created_d1", histogram_params)
        self.assertNotIn("d1", histogram_params)

    @patch.object(labels_app, "INAT_MAX_SEARCH_PAGES", 2)
    @patch("app.inat_api_get")
    def test_field_filtered_search_stops_at_page_budget(self, inat_api_get):
        inat_api_get.side_effect = [
            self.response({"total_results": 100}),
            self.response({"total_results": 100, "results": [{"id": 1}]}),
            self.response({"total_results": 99, "results": [{"id": 2}]}),
        ]

        response = self.client.post(
            "/labels/find_observations", data=self.search_data()
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(inat_api_get.call_count, 3)
        self.assertEqual(
            inat_api_get.call_args_list[-1].kwargs["params"]["id_above"], 1
        )
        self.assertEqual(response.get_json()["items"], [])

    @patch("app.requests.get")
    def test_mushroom_observer_rejects_field_filter(self, requests_get):
        response = self.client.post(
            "/labels/find_observations",
            data=self.search_data(source="mo", taxon="Fungi"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("only supported for iNaturalist", response.get_json()["error"])
        requests_get.assert_not_called()


class TestAddObservationBrowserHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("Node.js is unavailable")
        cls.repo_root = Path(labels_app.app.root_path)

    def run_helpers(self, expression):
        script = (
            "const h = require('./static/addobs_helpers.js');"
            f"const result = ({expression});"
            "process.stdout.write(JSON.stringify(result));"
        )
        completed = subprocess.run(
            ["node", "-e", script],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_canonical_keys_are_source_aware(self):
        result = self.run_helpers(
            "["
            "h.canonicalObservationKey('123', 'inat'),"
            "h.canonicalObservationKey('MO123'),"
            "h.canonicalObservationKey('BG123'),"
            "h.canonicalObservationKey('https://www.inaturalist.org/observations/123'),"
            "h.canonicalObservationKey('https://mushroomobserver.org/obs/123'),"
            "h.canonicalObservationKey('https://mushroomobserver.org/observations?ids=123'),"
            "h.canonicalObservationKey('https://bugguide.net/node/view/123'),"
            "h.canonicalObservationKey('motoinat123')"
            "]"
        )
        self.assertEqual(
            result,
            [
                "inat:123",
                "mo:123",
                "bg:123",
                "inat:123",
                "mo:123",
                "mo:123",
                "bg:123",
                "motoinat:123",
            ],
        )
        self.assertNotEqual(result[0], result[1])

    def test_pasted_observation_ids_are_normalized_in_order(self):
        result = self.run_helpers(
            "({"
            "spaces:h.parseObservationInput('123 456 789'),"
            "commas:h.parseObservationInput('123,456, 789'),"
            "bugguide:h.parseObservationInput('BG 12345'),"
            "conversions:h.parseObservationInput('motoinat1 moinat2 inatmo3'),"
            "urls:h.parseObservationInput('https://mushroomobserver.org/observations?ids=11 https://bugguide.net/node/view/22'),"
            "mixed:h.parseObservationInput('123 MO456 motoinat789 https://www.inaturalist.org/observations/321 https://mushroomobserver.org/obs/654 BG 987 bugguide 246')"
            "})"
        )
        self.assertEqual(result["spaces"], ["123", "456", "789"])
        self.assertEqual(result["commas"], ["123", "456", "789"])
        self.assertEqual(result["bugguide"], ["BG12345"])
        self.assertEqual(result["conversions"], ["motoinat1", "moinat2", "inatmo3"])
        self.assertEqual(result["urls"], ["MO11", "BG22"])
        self.assertEqual(
            result["mixed"],
            ["123", "MO456", "motoinat789", "321", "MO654", "BG987", "BG246"],
        )

    def test_queue_counts_sheet_breaks_and_print_preflight_are_distinct(self):
        result = self.run_helpers(
            "(() => {"
            "const ids=h.queuedObservationIds(['123','456','789','']);"
            "return {"
            "ids,queued:ids.length,normal:h.printedLabelCount(ids.length,false),"
            "duplicates:h.printedLabelCount(ids.length,true),"
            "eightSheets:h.standardSheetCount(8),nineSheets:h.standardSheetCount(9),"
            "eightBreaks:h.sheetBreakAfterRows(8,false,false),"
            "nineBreaks:h.sheetBreakAfterRows(9,false,false),"
            "duplicateBreaks:h.sheetBreakAfterRows(5,true,false),"
            "miniBreaks:h.sheetBreakAfterRows(20,true,true),"
            "ordinary:h.shouldShowPrintPreflight({printedLabelCount:3}),"
            "duplicate:h.shouldShowPrintPreflight({printedLabelCount:6,printDuplicates:true}),"
            "mini:h.shouldShowPrintPreflight({printedLabelCount:3,miniLabelSize:'1'}),"
            "fields:h.shouldShowPrintPreflight({printedLabelCount:3,addedFields:['Voucher']}),"
            "suppressed:h.shouldShowPrintPreflight({printedLabelCount:3,suppressedFields:['Notes']}),"
            "customSort:h.shouldShowPrintPreflight({printedLabelCount:3,sortMode:'custom'}),"
            "large:h.shouldShowPrintPreflight({printedLabelCount:100})"
            "};})()"
        )
        self.assertEqual(result["ids"], ["123", "456", "789"])
        self.assertEqual((result["queued"], result["normal"], result["duplicates"]), (3, 3, 6))
        self.assertEqual((result["eightSheets"], result["nineSheets"]), (1, 2))
        self.assertEqual(result["eightBreaks"], [])
        self.assertEqual(result["nineBreaks"], [8])
        self.assertEqual(result["duplicateBreaks"], [4])
        self.assertEqual(result["miniBreaks"], [])
        self.assertFalse(result["ordinary"])
        for key in ("duplicate", "mini", "fields", "suppressed", "customSort", "large"):
            self.assertTrue(result[key], key)

    def test_observation_links_are_source_aware(self):
        result = self.run_helpers(
            "["
            "h.observationUrl('327631026','inat'),"
            "h.observationUrl('MO585855'),"
            "h.observationUrl('BG2520730')"
            "]"
        )
        self.assertEqual(
            result,
            [
                "https://www.inaturalist.org/observations/327631026",
                "https://mushroomobserver.org/observations/585855",
                "https://bugguide.net/node/view/2520730",
            ],
        )

    def test_remaining_capacity_accounts_for_existing_observations(self):
        self.assertEqual(
            self.run_helpers(
                "[h.remainingRunCapacity(0),h.remainingRunCapacity(2),h.remainingRunCapacity(500),h.remainingRunCapacity(502)]"
            ),
            [500, 498, 0, 0],
        )

    def test_sorting_is_stable_and_missing_dates_are_last(self):
        result = self.run_helpers(
            "(() => {"
            "const rows = ["
            "{scientific_name:'Zulu', observed_on:'', _originalIndex:0, selected:true},"
            "{scientific_name:'alpha', observed_on:'2026-01-01', _originalIndex:1, selected:false},"
            "{scientific_name:'Beta', observed_on:'2026-02-01', _originalIndex:2, selected:true}"
            "];"
            "return {"
            "newest:h.sortResultItems(rows,'newest').map(x=>x.scientific_name),"
            "oldest:h.sortResultItems(rows,'oldest').map(x=>x.scientific_name),"
            "name:h.sortResultItems(rows,'name').map(x=>x.scientific_name),"
            "selected:h.sortResultItems(rows,'name').filter(x=>x.selected).map(x=>x.scientific_name)"
            "};})()"
        )
        self.assertEqual(result["newest"], ["Beta", "alpha", "Zulu"])
        self.assertEqual(result["oldest"], ["alpha", "Beta", "Zulu"])
        self.assertEqual(result["name"], ["alpha", "Beta", "Zulu"])
        self.assertEqual(result["selected"], ["Beta", "Zulu"])

    def test_selection_state_excludes_disabled_and_uses_visible_rows(self):
        result = self.run_helpers(
            "(() => {"
            "const rows=["
            "{checked:false,disabled:true,hidden:false},"
            "{checked:true,disabled:false,hidden:false},"
            "{checked:false,disabled:false,hidden:false},"
            "{checked:true,disabled:false,hidden:true}"
            "];"
            "const partial=h.selectionState(rows);"
            "rows.filter(x=>!x.hidden&&!x.disabled).forEach(x=>x.checked=true);"
            "return {partial,allVisible:h.selectionState(rows)};"
            "})()"
        )
        self.assertEqual(result["partial"]["selected"], 2)
        self.assertEqual(result["partial"]["shown"], 4)
        self.assertEqual(result["partial"]["alreadyOnSheet"], 1)
        self.assertEqual(result["partial"]["visibleEligible"], 2)
        self.assertTrue(result["partial"]["selectIndeterminate"])
        self.assertEqual(result["allVisible"]["selected"], 3)
        self.assertEqual(result["allVisible"]["shown"], 4)
        self.assertTrue(result["allVisible"]["selectChecked"])

    def test_search_and_selection_summaries_keep_count_scopes_distinct(self):
        result = self.run_helpers(
            "(() => {"
            "const rows=Array.from({length:500},(_,index)=>({"
            "checked:index>=3,disabled:index<3,hidden:false"
            "}));"
            "const initial=h.selectionState(rows);"
            "rows[3].checked=false;"
            "const unchecked=h.selectionState(rows);"
            "rows.slice(100,200).forEach(row=>row.hidden=true);"
            "const filtered=h.selectionState(rows);"
            "return {"
            "under:h.searchSummary(327),"
            "over:h.searchSummary(648),"
            "warning:h.overCapSummary(648,500),"
            "underSelection:h.selectionSummary({shown:327,selected:327,alreadyOnSheet:0}),"
            "overSelection:h.selectionSummary({shown:500,selected:500,alreadyOnSheet:0}),"
            "initial:h.selectionSummary(initial),"
            "unchecked:h.selectionSummary(unchecked),"
            "filtered:h.selectionSummary(filtered),"
            "sorted:h.selectionSummary(h.selectionState(rows.reverse()))"
            "};"
            "})()"
        )
        self.assertEqual(result["under"], "327 observations match")
        self.assertEqual(result["over"], "648 observations match")
        self.assertEqual(
            result["underSelection"],
            "327 shown · 327 selected · 0 on sheet",
        )
        self.assertEqual(
            result["overSelection"],
            "500 shown · 500 selected · 0 on sheet",
        )
        self.assertEqual(
            result["warning"],
            "648 observations match. 500 shown; 148 observations are not shown.",
        )
        self.assertEqual(result["initial"], "500 shown · 497 selected · 3 on sheet")
        self.assertEqual(result["unchecked"], "500 shown · 496 selected · 3 on sheet")
        self.assertEqual(result["filtered"], result["unchecked"])
        self.assertEqual(result["sorted"], result["unchecked"])
        self.assertEqual(
            self.run_helpers("h.searchSummary(1)"),
            "1 observation matches",
        )

    def test_result_filter_matches_name_id_and_locality_case_insensitively(self):
        result = self.run_helpers(
            "(() => {"
            "const item={scientific_name:'Amanita muscaria',display_id:'MO585855',place_guess:'Tilden Park'};"
            "return ['amanita','585855','TILDEN','missing'].map(q=>h.resultMatchesQuery(item,q));"
            "})()"
        )
        self.assertEqual(result, [True, True, True, False])

    def test_recent_observers_are_case_insensitive_source_ready_mru(self):
        result = self.run_helpers(
            "h.updateRecentObservers(['Alice','Bob','Carol','Dan','Eve'],'alice')"
        )
        self.assertEqual(result, ["alice", "Bob", "Carol", "Dan", "Eve"])


class TestAddObservationTemplateIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(labels_app.app.root_path) / "templates" / "index.html"
        ).read_text(encoding="utf-8")

    def test_queue_has_one_canonical_id_source_and_formdata_uses_it(self):
        self.assertEqual(self.template.count("function getQueuedObservationIds()"), 1)
        collect = self.template.split("function collectObservationsFormData()", 1)[1].split(
            "function escapeHtml", 1
        )[0]
        self.assertIn("getQueuedObservationIds().forEach", collect)
        self.assertNotIn(".obs-id-input", collect)
        self.assertNotIn("parseObservationInput", collect)
        capacity = self.template.split("function addObsRemainingRunCapacity()", 1)[1].split(
            "function selectedAddObsCount", 1
        )[0]
        self.assertIn("countQueuedObservations()", capacity)

    def test_pasted_group_expands_then_uses_one_batch_request(self):
        normalize = self.template.split("function normalizeAndLookupObservationInput", 1)[1].split(
            "function lookupObservation", 1
        )[0]
        batch = self.template.split("function lookupObservationRows", 1)[1].split(
            "function normalizeAndLookupObservationInput", 1
        )[0]
        self.assertIn("expandObservationInput($input, obsIds)", normalize)
        self.assertIn('["loading", "complete"].includes', normalize)
        self.assertLess(
            normalize.index('["loading", "complete"].includes'),
            normalize.index("lookupObservationRows($inputs, obsIds)"),
        )
        self.assertEqual(batch.count('url: "/labels/lookup_batch"'), 1)
        self.assertIn("const item = items[index]", batch)
        self.assertIn("allObservationData[state.inputId] = [item]", batch)
        self.assertIn('data("lookupStatus", "loading")', batch)

    def test_lookup_errors_show_server_message_and_retry_only_transient_failures(self):
        renderer = self.template.split("function renderObservationResult", 1)[1].split(
            "function updateDuplicateFlags", 1
        )[0]
        self.assertIn('.text(data.error || "Could not fetch observation")', renderer)
        self.assertNotIn("escapeHtml(original)", renderer)
        self.assertIn("status === 429 || status >= 500", renderer)
        self.assertIn('class: "lookup-retry"', renderer)

        retry_handler = self.template.split(
            '$("#observationInputs").on("click", ".lookup-retry"', 1
        )[1].split(
            '$("#observationInputs").on("input", ".obs-id-input"', 1
        )[0]
        self.assertIn('$input.removeData("lookupStatus")', retry_handler)
        self.assertIn("normalizeAndLookupObservationInput($input)", retry_handler)

    def test_clear_queue_aborts_lookups_and_preserves_options(self):
        reset = self.template.split("function resetObservationQueue()", 1)[1].split(
            "function clearObservationQueue()", 1
        )[0]
        self.assertIn("pendingRowLookups.forEach((request) => request.abort())", reset)
        self.assertIn("allObservationData = {}", reset)
        self.assertIn('$("#observationInputs").empty()', reset)
        self.assertIn("addObservationField()", reset)
        self.assertNotIn("printDuplicateLabels", reset)
        # Discarding the queue must also drop the autosaved copy.
        self.assertIn("forgetSavedQueue()", reset)

        clear = self.template.split("function clearObservationQueue()", 1)[1].split(
            '$("#clearQueueButton")', 1
        )[0]
        self.assertIn("queuedCount", clear)
        self.assertIn("Clear all ${queuedCount} observation", clear)
        self.assertLess(clear.index("confirm("), clear.index("resetObservationQueue()"))

    def test_observation_field_combobox_and_filtered_summary_are_wired(self):
        self.assertIn('role="combobox" aria-autocomplete="list"', self.template)
        self.assertIn('role="listbox"', self.template)
        for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            self.assertIn(f'event.key === "{key}"', self.template)
        self.assertIn('formData.append("obs_field_name"', self.template)
        self.assertIn('formData.append("obs_field_id"', self.template)
        self.assertIn("total_in_scope", self.template)
        source_controls = self.template.split("function updateAddObsSourceControls", 1)[1].split(
            "function closeAddObsModal", 1
        )[0]
        self.assertIn('$("#addObsObservationFieldGroup").prop("hidden", true)', source_controls)
        self.assertIn('$("#addObsObservationField").prop("disabled", true)', source_controls)
        self.assertIn('$("#addObsObservationFieldId").val("")', source_controls)
        self.assertIn('toggleClass("is-mo-source", source === "mo")', source_controls)
        self.assertIn(
            "#addObsModal.is-mo-source .addobs-presets-group",
            self.template,
        )
        importer = self.template.split("function addObservationsToForm(observations)", 1)[1].split(
            "function validatePrintRequest", 1
        )[0]
        self.assertIn("allObservationData[$empty.attr(\"id\")] = [obsData]", importer)
        self.assertNotIn("lookupObservation(", importer)
        self.assertNotIn("lookup_batch", importer)

    def test_print_preflight_revalidates_before_starting_stream(self):
        confirm = self.template.split('$("#printPreflightConfirm").on("click"', 1)[1].split(
            "// Add Observations automatic-search workflow", 1
        )[0]
        self.assertIn("validatePrintRequest(request)", confirm)
        self.assertIn("startPrintJob(validatedState)", confirm)
        self.assertNotIn("skipChecks", self.template)
        validate = self.template.split("function validatePrintRequest(request)", 1)[1].split(
            "function shouldShowPrintPreflight", 1
        )[0]
        self.assertIn("queuedObservationCount > maxObservations", validate)
        self.assertIn('sortMode === "custom" && !sortField', validate)

    def test_rows_disable_on_sheet_results_and_select_all_ignores_them(self):
        self.assertIn(
            'const disabled = item._onSheet ? " disabled" : "";', self.template
        )
        self.assertIn(".addObsRowCk:not(:disabled)", self.template)
        self.assertIn("buildOnSheetObservationKeys()", self.template)

    def test_result_ids_are_external_links_without_changing_row_selection(self):
        self.assertIn("LabelsAddObsHelpers.observationUrl(", self.template)
        self.assertIn(
            'class="addobs-result-id" href="${escapeHtml(observationHref)}" target="_blank" rel="noopener noreferrer"',
            self.template,
        )
        row_click_handler = self.template.split(
            '$("#addObsResults").on("click", ".addobs-result-row"', 1
        )[1].split("});", 1)[0]
        self.assertIn('closest("a, input, label, button")', row_click_handler)

    def test_frontend_feedback_errors_and_modals_are_hardened(self):
        self.assertIn(
            'id="toast" role="alert" aria-live="assertive" aria-atomic="true"',
            self.template,
        )
        self.assertIn('id="warningMessage" class="warning"', self.template)
        self.assertIn('aria-live="assertive" aria-atomic="true"', self.template)
        self.assertIn('href="${escapeHtml(url)}"', self.template)
        self.assertIn("function trapFocusInOpenModal(event)", self.template)
        self.assertIn("trapFocusInOpenModal(e);", self.template)
        modal_ids = self.template.split("const modalOverlayIds = [", 1)[1].split(
            "];", 1
        )[0]
        for modal_id in ("editFieldsModal", "customFieldsModal"):
            self.assertIn(f'"{modal_id}"', modal_ids)

        start_job = self.template.split("async function startPrintJob", 1)[1].split(
            '$("#printRtfButton")', 1
        )[0]
        self.assertEqual(start_job.count("startResp.json()"), 1)
        self.assertLess(
            start_job.index("const data = await startResp.json()"),
            start_job.index("if (!startResp.ok)"),
        )
        self.assertIn("const message = escapeHtml(err.message", start_job)

        render = self.template.split("function renderAddObsResults(items)", 1)[1].split(
            "function applyAddObsResultFilter", 1
        )[0]
        self.assertEqual(render.count("getAddObsFilters().source"), 1)
        self.assertIn("canonicalObservationKey(displayId, filterSource)", render)
        self.assertIn("const resultSource = filterSource", render)

    def test_selection_is_capped_by_remaining_run_capacity(self):
        render_helper = self.template.split(
            "function renderAddObsResults(items)", 1
        )[1].split("function applyAddObsResultFilter", 1)[0]
        self.assertIn(
            "let remainingSelectionSlots = addObsRemainingRunCapacity();",
            render_helper,
        )
        self.assertIn(
            "const shouldSelect = !item._onSheet && remainingSelectionSlots > 0;",
            render_helper,
        )
        self.assertIn(
            "selectedAddObsCount() > addObsRemainingRunCapacity()",
            self.template,
        )
        self.assertIn(
            "selectedIds.length > addObsRemainingRunCapacity()",
            self.template,
        )

    def test_observer_storage_is_separate_by_source(self):
        for key in (
            "labels-last-observer-inat",
            "labels-last-observer-mo",
            "labels-recent-observers-inat",
            "labels-recent-observers-mo",
        ):
            self.assertIn(key, self.template)

    def test_inaturalist_taxon_label_identifies_numeric_ids(self):
        self.assertIn(
            'id="addObsTaxonLabel">Taxon (name or ID #)</label>',
            self.template,
        )
        self.assertIn(
            '$("#addObsTaxonLabel").text("Taxon (name or ID #)")',
            self.template,
        )

    def test_footer_summary_adds_existing_and_selected_observations(self):
        summary_helper = self.template.split(
            "function updateAddObsRunSummary(selectedCount = 0)", 1
        )[1].split("function renderAddObsStateMessage", 1)[0]
        self.assertIn(
            "const observationsAfterAdding = existingObservations + selectedCount;",
            summary_helper,
        )
        self.assertIn("countQueuedObservations()", summary_helper)
        self.assertIn("addObsCountText(selectedCount)", summary_helper)
        self.assertIn(
            "addObsCountText(observationsAfterAdding)", summary_helper
        )

    def test_on_sheet_rows_are_dimmed_and_flag_is_plain_status_text(self):
        self.assertIn(
            ".addobs-result-row.is-on-sheet > .addobs-thumb-wrap,",
            self.template,
        )
        self.assertIn(
            ".addobs-result-row.is-on-sheet > .addobs-result-main,",
            self.template,
        )
        flag_rules = self.template.split(
            ".addobs-result-flag {\n            min-width: 4.7rem;", 1
        )[1].split("}", 1)[0]
        self.assertIn("border: 0;", flag_rules)
        self.assertIn("text-transform: uppercase;", flag_rules)
        self.assertNotIn(".is-on-sheet .addobs-result-flag", self.template)

    def test_result_filter_clear_uses_real_disabled_state(self):
        self.assertIn(
            'aria-label="Clear result filter" disabled>Clear</button>',
            self.template,
        )
        clear_helper = self.template.split(
            "function updateAddObsFilterClearControl()", 1
        )[1].split("function renderAddObsStateMessage", 1)[0]
        self.assertNotIn('.prop("hidden"', clear_helper)
        self.assertIn(
            '.prop("disabled", !hasFilter || addObsSearchState !== "results")',
            clear_helper,
        )
        self.assertIn("color: var(--brass);", self.template)

        clear_handler = self.template.split(
            '$("#addObsResultFilterClear").on("click"', 1
        )[1].split("});", 1)[0]
        self.assertIn('.val("").focus()', clear_handler)
        self.assertIn("applyAddObsResultFilter()", clear_handler)

    def test_filter_bar_and_toolbar_have_distinct_compact_groups(self):
        self.assertIn("width: min(1180px, 96vw);", self.template)
        self.assertIn("grid-template-columns:\n                minmax(250px, 1.35fr)", self.template)
        self.assertIn('class="addobs-preset-chip" id="quickToday"', self.template)
        self.assertNotIn(
            'class="btn btn-outline btn-sm" id="quickToday"', self.template
        )
        self.assertIn("border-left: 1px solid var(--rule-hi);", self.template)
        filter_control_rules = self.template.split(
            ".addobs-filter-control {\n            flex: 1 1 15rem;", 1
        )[1].split("}", 1)[0]
        self.assertIn("border: 1px solid var(--rule-hi);", filter_control_rules)

    def test_observer_status_is_below_wide_input_and_dates_can_shrink_cleanly(self):
        self.assertIn('class="addobs-observer-field"', self.template)
        self.assertEqual(self.template.count('class="field-group addobs-date-field"'), 2)
        observer_rules = self.template.split(
            ".addobs-observer-field #addObsUserStatus {", 1
        )[1].split("}", 1)[0]
        self.assertIn("display: block;", observer_rules)
        self.assertIn("margin-top: 6px;", observer_rules)
        self.assertIn(
            ".addobs-observer-field .field-input,\n        .addobs-date-field .field-input",
            self.template,
        )

    def test_zero_result_status_is_factual_and_empty_state_remains_friendly(self):
        search_state = self.template.split(
            "function setAddObsSearchState(state, options = {})", 1
        )[1].split("function invalidateAddObsSearch", 1)[0]
        self.assertIn(
            '"zero-results": LabelsAddObsHelpers.searchSummary(0)',
            search_state,
        )
        self.assertIn(
            ': "No observations found for these filters."',
            search_state,
        )

    def test_desktop_footer_keeps_summary_and_actions_on_one_row(self):
        footer_rules = self.template.split(".addobs-modal-footer {", 1)[1].split(
            "}", 1
        )[0]
        self.assertIn("justify-content: space-between;", footer_rules)
        self.assertIn("flex-wrap: nowrap;", footer_rules)
        action_rules = self.template.split(".addobs-footer-actions {", 1)[1].split(
            "}", 1
        )[0]
        self.assertIn("flex: 0 0 auto;", action_rules)
        self.assertIn("flex-wrap: nowrap;", action_rules)

    def test_search_status_uses_matched_and_shown_summary(self):
        search_state = self.template.split(
            "function setAddObsSearchState(state, options = {})", 1
        )[1].split("function invalidateAddObsSearch", 1)[0]
        self.assertIn("LabelsAddObsHelpers.searchSummary(", search_state)
        self.assertNotIn("Search exceeds the run limit", self.template)

        perform_search = self.template.split(
            "async function performAddObsSearch(searchGeneration)", 1
        )[1].split("function updateAddObsSourceControls", 1)[0]
        self.assertIn(
            ': addObsTotalCount > maxObservations ? "" : undefined',
            perform_search,
        )
        self.assertIn('.prop("hidden", !statusText)', search_state)

    def test_results_list_has_modest_footer_clearance(self):
        list_rules = self.template.split(".addobs-results-list {", 1)[1].split(
            "}", 1
        )[0]
        self.assertIn("padding: 0.5rem 1rem 1.25rem;", list_rules)
        self.assertIn("scroll-padding-bottom: 1.25rem;", list_rules)


class TestLocalityIsNotCoordinates(unittest.TestCase):
    def test_bare_coordinate_pair_is_not_reported_as_a_locality(self):
        self.assertEqual(
            labels_app._concise_locality(
                {"location": "37.8712,-122.2456", "place_guess": ""}
            ),
            "",
        )

    def test_place_guess_still_wins_over_coordinates(self):
        self.assertEqual(
            labels_app._concise_locality(
                {"location": "37.8712,-122.2456", "place_guess": "Berkeley, California"}
            ),
            "Berkeley, California",
        )

    def test_place_names_containing_numbers_are_kept(self):
        self.assertEqual(
            labels_app._concise_locality({"location": "Plot 12, Tilden Park"}),
            "Plot 12, Tilden Park",
        )


class TestHistogramCaching(unittest.TestCase):
    """The daily counts are identical for identical filters, and every
    iNaturalist call is serialized behind one rate-limited lock."""

    def setUp(self):
        labels_app._histogram_cache.clear()
        self.addCleanup(labels_app._histogram_cache.clear)

    @patch("app.inat_api_get")
    def test_identical_filters_reuse_the_cached_counts(self, inat_api_get):
        response = MagicMock()
        response.json.return_value = {"results": {"day": {"2026-07-03": 4}}}
        inat_api_get.return_value = response
        params = {"user_login": "observer", "taxon_id": 47170, "interval": "day"}

        first = labels_app._inat_daily_counts(params)
        second = labels_app._inat_daily_counts(dict(reversed(list(params.items()))))

        self.assertEqual(first, {"2026-07-03": 4})
        self.assertEqual(second, first)
        self.assertEqual(inat_api_get.call_count, 1)

    @patch("app.inat_api_get")
    def test_different_filters_are_fetched_separately(self, inat_api_get):
        response = MagicMock()
        response.json.return_value = {"results": {"day": {"2026-07-03": 4}}}
        inat_api_get.return_value = response

        labels_app._inat_daily_counts({"user_login": "observer"})
        labels_app._inat_daily_counts({"user_login": "someone_else"})

        self.assertEqual(inat_api_get.call_count, 2)

    @patch("app.inat_api_get")
    def test_expired_entries_are_refetched(self, inat_api_get):
        response = MagicMock()
        response.json.return_value = {"results": {"day": {"2026-07-03": 4}}}
        inat_api_get.return_value = response
        params = {"user_login": "observer"}

        labels_app._inat_daily_counts(params)
        key, (_, counts) = next(iter(labels_app._histogram_cache.items()))
        labels_app._histogram_cache[key] = (0.0, counts)
        labels_app._inat_daily_counts(params)

        self.assertEqual(inat_api_get.call_count, 2)

    @patch("app.inat_api_get")
    def test_cache_is_bounded(self, inat_api_get):
        response = MagicMock()
        response.json.return_value = {"results": {"day": {"2026-07-03": 1}}}
        inat_api_get.return_value = response

        for index in range(labels_app.INAT_HISTOGRAM_CACHE_MAX_ENTRIES + 10):
            labels_app._inat_daily_counts({"user_login": f"observer{index}"})

        self.assertEqual(
            len(labels_app._histogram_cache),
            labels_app.INAT_HISTOGRAM_CACHE_MAX_ENTRIES,
        )


class TestFindObservationsTotals(unittest.TestCase):
    """The reported total drives both the "not shown" summary and the
    date-window chips, so it has to be the real upstream match count."""

    def setUp(self):
        labels_app.app.config["TESTING"] = True
        self.client = labels_app.app.test_client()

    def _inat_search(self):
        return self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-07-01",
                "d2": "2026-07-31",
                "username": "observer",
                "taxon": "47170",
                "source": "inat",
                "date_mode": "observed",
            },
        ).get_json()

    @staticmethod
    def _inat_page(total_results, ids):
        page = MagicMock()
        page.json.return_value = {
            "total_results": total_results,
            "results": [
                {
                    "id": obs_id,
                    "observed_on": "2026-07-03",
                    "taxon": {"name": "Amanita muscaria", "iconic_taxon_name": "Fungi"},
                    "user": {"login": "observer"},
                }
                for obs_id in ids
            ],
        }
        return page

    @patch("app.inat_api_get")
    def test_inat_total_ignores_shrinking_id_above_page_totals(self, inat_api_get):
        # id_above is a filter, so each later page reports only the matches
        # still ahead of the cursor.  Only the first page knows the total.
        cap = labels_app.MAX_OBS_PER_REQUEST + 1
        first_ids = list(range(1, 401))
        second_ids = list(range(401, 401 + cap))
        histogram = MagicMock()
        histogram.json.return_value = {"results": {"day": {}}}
        inat_api_get.side_effect = [
            self._inat_page(5000, first_ids),
            self._inat_page(4600, second_ids),
            histogram,
        ]

        payload = self._inat_search()

        self.assertEqual(payload["total_count"], 5000)

    @patch("app.inat_api_get")
    def test_inat_total_never_falls_below_the_rows_returned(self, inat_api_get):
        inat_api_get.side_effect = [
            self._inat_page(0, [1, 2, 3]),
            self._inat_page(0, []),
        ]

        payload = self._inat_search()

        self.assertEqual(payload["total_count"], 3)

    @patch("app.inat_api_get")
    def test_inat_page_without_usable_ids_does_not_repeat(self, inat_api_get):
        page = MagicMock()
        page.json.return_value = {
            "total_results": 1,
            "results": [{"taxon": {"name": "Missing id"}}, "not-a-record"],
        }
        inat_api_get.return_value = page

        with patch.object(labels_app.api_error_logger, "warning") as warning:
            payload = self._inat_search()

        self.assertEqual(inat_api_get.call_count, 1)
        self.assertEqual(payload["items"], [])
        warning.assert_called_once()

    @patch("app.requests.get")
    def test_mo_total_reads_api2_number_of_records(self, requests_get):
        response = MagicMock()
        response.json.return_value = {
            "number_of_records": 1754,
            "number_of_pages": 1,
            "results": [
                {"id": 1, "date": "2026-07-04", "consensus_name": "Amanita muscaria"}
            ],
        }
        requests_get.return_value = response

        payload = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-07-01",
                "d2": "2026-07-31",
                "username": "observer",
                "taxon": "Fungi",
                "source": "mo",
                "date_mode": "observed",
            },
        ).get_json()

        self.assertEqual(payload["total_count"], 1754)

    @patch("app.requests.get")
    def test_mo_page_walk_is_bounded_and_drops_windows(self, requests_get):
        # MO has no histogram endpoint, so per-day counts come from walking
        # pages.  Past the budget the walk stops and the chips are skipped
        # rather than holding the single worker for tens of seconds.
        cap = labels_app.MAX_OBS_PER_REQUEST + 1
        pages_fetched = []

        def fake_get(url, params=None, timeout=None):
            page = (params or {}).get("page", 1)
            pages_fetched.append(page)
            start = (page - 1) * cap
            response = MagicMock()
            response.json.return_value = {
                "number_of_records": cap * 50,
                "number_of_pages": 50,
                "results": [
                    {
                        "id": start + offset,
                        "date": "2026-07-04",
                        "consensus_name": "Amanita muscaria",
                    }
                    for offset in range(cap)
                ],
            }
            return response

        requests_get.side_effect = fake_get

        payload = self.client.post(
            "/labels/find_observations",
            data={
                "d1": "2026-07-01",
                "d2": "2026-07-31",
                "username": "observer",
                "taxon": "Fungi",
                "source": "mo",
                "date_mode": "observed",
            },
        ).get_json()

        self.assertEqual(pages_fetched, list(range(1, labels_app.MO_MAX_WINDOW_PAGES + 1)))
        self.assertEqual(payload["total_count"], cap * 50)
        self.assertNotIn("windows", payload)


class TestAddObservationSearchResilience(unittest.TestCase):
    """A failed observer lookup or an unreadable error body must not leave the
    modal stuck or show a JSON parse error as the user-facing message."""

    @classmethod
    def setUpClass(cls):
        cls.template = Path("templates/index.html").read_text(encoding="utf-8")

    def test_failed_observer_lookup_is_unverified_not_invalid(self):
        validate = self.template.split("async function validateAddObsObserver", 1)[
            1
        ].split("\n        function ", 1)[0]
        self.assertIn("ObserverLookupError", validate)
        self.assertIn('addObsObserverValidationState = "unverified"', validate)
        self.assertIn("scheduleAddObsSearch({ immediate: immediateSearch });", validate)

    def test_unverified_observers_can_still_search(self):
        gate = self.template.split("function validateAddObsSearchFilters", 1)[1].split(
            "\n        function ", 1
        )[0]
        self.assertIn('addObsObserverValidationState !== "unverified"', gate)

    def test_non_json_error_body_does_not_surface_a_parse_error(self):
        perform_search = self.template.split("async function performAddObsSearch", 1)[
            1
        ].split("\n        function ", 1)[0]
        parse_guard = perform_search.split("catch (parseError)", 1)[1].split("}", 1)[0]
        self.assertIn("AbortError", parse_guard)
        self.assertIn("Search failed (HTTP ${response.status})", perform_search)
        self.assertNotIn(
            "const data = await response.json();\n                if (searchGeneration",
            perform_search,
        )


if __name__ == "__main__":
    unittest.main()
