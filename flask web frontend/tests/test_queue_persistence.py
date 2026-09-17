import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("LABELS_DISABLE_FILE_LOGGING", "1")

try:
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc


DAY_MS = 24 * 60 * 60 * 1000


class TestQueueStateHelpers(unittest.TestCase):
    """The autosaved queue is serialized by pure helpers in addobs_helpers.js."""

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

    def test_serialize_keeps_raw_text_and_drops_blank_rows(self):
        result = self.run_helpers(
            "h.serializeQueueState(['12345', '  ', 'MO678', ' bg99 ', ''], 1700000000000)"
        )
        # Raw typed text is preserved verbatim (trimmed): normalizing here would
        # lose half-typed rows and dedupe entries the user meant to keep.
        self.assertEqual(
            result,
            {"v": 1, "savedAt": 1700000000000, "observations": ["12345", "MO678", "bg99"]},
        )

    def test_serialize_returns_null_when_nothing_is_queued(self):
        self.assertIsNone(self.run_helpers("h.serializeQueueState([])"))
        self.assertIsNone(self.run_helpers("h.serializeQueueState(['', '   '])"))
        self.assertIsNone(self.run_helpers("h.serializeQueueState([null, undefined])"))

    def test_round_trip_preserves_order(self):
        result = self.run_helpers(
            "h.deserializeQueueState("
            "JSON.stringify(h.serializeQueueState(['9', '7', '8'], 1700000000000)),"
            "1700000000000)"
        )
        self.assertEqual(result, ["9", "7", "8"])

    def test_deserialize_rejects_malformed_payloads(self):
        cases = {
            "garbage": "h.deserializeQueueState('not json', 1700000000000)",
            "null": "h.deserializeQueueState('null', 1700000000000)",
            "array": "h.deserializeQueueState('[1,2,3]', 1700000000000)",
            "wrong_version": (
                "h.deserializeQueueState("
                "JSON.stringify({v: 2, savedAt: 1700000000000, observations: ['1']}),"
                "1700000000000)"
            ),
            "observations_not_array": (
                "h.deserializeQueueState("
                "JSON.stringify({v: 1, savedAt: 1700000000000, observations: '123'}),"
                "1700000000000)"
            ),
            "missing_saved_at": (
                "h.deserializeQueueState("
                "JSON.stringify({v: 1, observations: ['1']}),"
                "1700000000000)"
            ),
        }
        result = self.run_helpers(
            "({" + ",".join(f"{name}:{expr}" for name, expr in cases.items()) + "})"
        )
        for name, value in result.items():
            with self.subTest(case=name):
                self.assertEqual(value, [])

    def test_deserialize_drops_non_string_entries(self):
        result = self.run_helpers(
            "h.deserializeQueueState("
            "JSON.stringify({v: 1, savedAt: 1700000000000,"
            " observations: ['1', 2, null, {}, ' 3 ', '']}),"
            "1700000000000)"
        )
        self.assertEqual(result, ["1", "3"])

    def test_saved_queue_expires_after_a_week(self):
        fresh = self.run_helpers(
            "h.deserializeQueueState("
            f"JSON.stringify({{v: 1, savedAt: 1700000000000 - {6 * DAY_MS},"
            " observations: ['1']}),"
            "1700000000000)"
        )
        self.assertEqual(fresh, ["1"])

        stale = self.run_helpers(
            "h.deserializeQueueState("
            f"JSON.stringify({{v: 1, savedAt: 1700000000000 - {8 * DAY_MS},"
            " observations: ['1']}),"
            "1700000000000)"
        )
        self.assertEqual(stale, [])

    def test_both_directions_cap_at_the_request_limit(self):
        self.assertEqual(labels_app.MAX_OBS_PER_REQUEST, 500)

        serialized = self.run_helpers(
            "h.serializeQueueState("
            "Array.from({length: 600}, (_, i) => String(i + 1)), 1700000000000"
            ").observations.length"
        )
        self.assertEqual(serialized, 500)

        deserialized = self.run_helpers(
            "h.deserializeQueueState("
            "JSON.stringify({v: 1, savedAt: 1700000000000,"
            " observations: Array.from({length: 600}, (_, i) => String(i + 1))}),"
            "1700000000000).length"
        )
        self.assertEqual(deserialized, 500)


class TestQueuePersistenceWiring(unittest.TestCase):
    """Guard the index.html wiring that makes autosave actually fire."""

    @classmethod
    def setUpClass(cls):
        template_path = (
            Path(labels_app.app.root_path) / "templates" / "index.html"
        )
        cls.template = template_path.read_text(encoding="utf-8")

    def test_queue_is_saved_on_every_mutation_point(self):
        self.assertIn('const QUEUE_STORAGE_KEY = "labels-queue-v1"', self.template)
        self.assertIn(
            'const PREVIOUS_LABELS_STORAGE_KEY = "labels-previous-v1"',
            self.template,
        )

        typing = self.template.split(
            '$("#observationInputs").on("input", ".obs-id-input"', 1
        )[1].split('$("#observationInputs").on("paste"', 1)[0]
        self.assertIn("scheduleQueueSave()", typing)

        removing = self.template.split(
            '$("#observationInputs").on("click", ".removeButton"', 1
        )[1].split("function resetObservationQueue()", 1)[0]
        self.assertIn("scheduleQueueSave()", removing)

        adding = self.template.split("function addObservationsToForm", 1)[1].split(
            "function validatePrintRequest", 1
        )[0]
        self.assertIn("scheduleQueueSave()", adding)

    def test_pending_save_is_flushed_before_leaving_the_page(self):
        # This is the exact failure that prompted the feature: navigating away
        # inside the debounce window must not lose the queue.
        self.assertIn(
            'window.addEventListener("beforeunload", saveQueueStateNow)', self.template
        )
        self.assertIn('document.visibilityState === "hidden"', self.template)
        self.assertNotIn("preventDefault", self.template.split(
            'window.addEventListener("beforeunload"', 1
        )[1].split("function", 1)[0])

    def test_restore_runs_only_when_url_params_are_absent(self):
        self.assertIn(
            "if (!preloadFromUrl() && !restoreSavedQueue()) ensureSingleTrailingEmptyRow();",
            self.template,
        )

    def test_restore_reuses_the_batched_lookup_path_and_offers_undo(self):
        restore_rows = self.template.split(
            "function restoreQueueObservations", 1
        )[1].split("function restoreSavedQueue()", 1)[0]
        restore_saved = self.template.split("function restoreSavedQueue()", 1)[1].split(
            "function restorePreviousLabels()", 1
        )[0]
        self.assertIn("LabelsAddObsHelpers.deserializeQueueState", restore_saved)
        # One input fanned out by expandObservationInput => a single lookup_batch POST.
        self.assertIn('$first.trigger("input")', restore_rows)
        self.assertIn("lookupObservation($first)", restore_rows)
        self.assertIn('label: "Undo"', restore_saved)
        self.assertIn("onClick: resetObservationQueue", restore_saved)
        self.assertIn("restoringQueue = true", restore_rows)

    def test_saving_is_suppressed_while_restoring(self):
        save = self.template.split("function saveQueueStateNow()", 1)[1].split(
            "function scheduleQueueSave()", 1
        )[0]
        schedule = self.template.split("function scheduleQueueSave()", 1)[1].split(
            "function forgetSavedQueue()", 1
        )[0]
        self.assertIn("if (restoringQueue || currentQueueWasGenerated) return;", save)
        self.assertIn("if (restoringQueue) return;", schedule)

    def test_successful_print_is_archived_and_removed_from_automatic_restore(self):
        remember = self.template.split(
            "function rememberSuccessfullyGeneratedQueue", 1
        )[1].split('window.addEventListener("beforeunload"', 1)[0]
        self.assertIn("PREVIOUS_LABELS_STORAGE_KEY", remember)
        self.assertIn("queueStillMatches", remember)
        self.assertIn("currentQueueWasGenerated = true", remember)
        self.assertIn("forgetSavedQueue()", remember)

        print_job = self.template.split("async function startPrintJob", 1)[1].split(
            '$("#printRtfButton")', 1
        )[0]
        self.assertGreaterEqual(print_job.count("recordSuccessfulPrint();"), 3)

    def test_changed_queue_is_not_discarded_when_an_older_print_finishes(self):
        remember = self.template.split(
            "function rememberSuccessfullyGeneratedQueue", 1
        )[1].split('window.addEventListener("beforeunload"', 1)[0]
        self.assertIn("currentObservationIds", remember)
        self.assertIn("submittedObservationIds", remember)
        self.assertIn("saveQueueStateNow()", remember)

    def test_previous_labels_link_restores_printed_queue_as_active_work(self):
        self.assertRegex(
            self.template,
            re.compile(
                r'id="clearQueueButton".*?id="restorePreviousLabelsButton"', re.S
            ),
        )
        restore = self.template.split("function restorePreviousLabels()", 1)[1].split(
            "function preloadObservationRowsFromUrl()", 1
        )[0]
        self.assertIn("PREVIOUS_LABELS_STORAGE_KEY", restore)
        self.assertIn("resetObservationQueue()", restore)
        self.assertIn("restoreQueueObservations(observations, true)", restore)

    def test_only_observation_text_is_persisted(self):
        # Label options are deliberately not saved; restoring them would silently
        # change print output.
        save = self.template.split("function saveQueueStateNow()", 1)[1].split(
            "function scheduleQueueSave()", 1
        )[0]
        for option in (
            "omitQrCodes",
            "printDuplicateLabels",
            "minilabelSize",
            "sortMode",
            "allObservationData",
            "addFields",
        ):
            with self.subTest(option=option):
                self.assertNotIn(option, save)

    def test_toast_action_button_exists_and_is_hidden_by_default(self):
        self.assertIn('id="toastAction"', self.template)
        self.assertIn("#toast.show.toast-lasting", self.template)


class TestHelperScriptIsCacheBusted(unittest.TestCase):
    """index.html calls into addobs_helpers.js, so the two must ship together."""

    def test_helper_script_url_carries_an_mtime_version(self):
        with labels_app.app.test_client() as client:
            html = client.get("/").get_data(as_text=True)

        match = re.search(
            r'<script src="(/labels/static/addobs_helpers\.js\?v=(\d+))"></script>', html
        )
        self.assertIsNotNone(match, "helper script must be served with a version param")

        expected = int(
            os.path.getmtime(
                Path(labels_app.app.static_folder) / "addobs_helpers.js"
            )
        )
        self.assertEqual(int(match.group(2)), expected)

    def test_missing_asset_falls_back_to_a_plain_url(self):
        with labels_app.app.test_request_context():
            self.assertEqual(
                labels_app.static_asset_url("does-not-exist.js"),
                "/labels/static/does-not-exist.js",
            )


if __name__ == "__main__":
    unittest.main()
