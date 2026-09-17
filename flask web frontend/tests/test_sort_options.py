import unittest
from unittest.mock import MagicMock, patch

try:
    import flask  # noqa: F401
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc


class TestPrintSortOptions(unittest.TestCase):
    def setUp(self):
        labels_app.app.config["TESTING"] = True
        self.client = labels_app.app.test_client()
        labels_app._jobs.clear()

    def tearDown(self):
        labels_app._jobs.clear()

    def _post(self, popen, **extra):
        proc = MagicMock()
        proc.poll.return_value = None
        popen.return_value = proc

        data = {"format": "rtf", "observations[]": ["123"]}
        data.update(extra)
        return self.client.post("/labels/print_start", data=data)

    @patch("app.subprocess.Popen")
    def test_default_sort_passes_no_sort_flag(self, popen):
        response = self._post(popen)

        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0]
        self.assertNotIn("--sort", command)
        self.assertNotIn("--sort-field", command)

    @patch("app.subprocess.Popen")
    def test_named_sort_mode_is_forwarded(self, popen):
        for mode in ("none", "date", "date-desc", "voucher"):
            with self.subTest(mode=mode):
                popen.reset_mock()
                # Each iteration leaves a never-finishing mock job behind, which
                # would otherwise trip the concurrent-job limit.
                labels_app._jobs.clear()
                response = self._post(popen, sort=mode)

                self.assertEqual(response.status_code, 200)
                command = popen.call_args.args[0]
                self.assertEqual(command[command.index("--sort") + 1], mode)
                self.assertNotIn("--sort-field", command)

    @patch("app.subprocess.Popen")
    def test_custom_sort_forwards_field_name(self, popen):
        response = self._post(popen, sort="custom", sort_field="Voucher Number(s)")

        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--sort") + 1], "custom")
        self.assertEqual(
            command[command.index("--sort-field") + 1], "Voucher Number(s)"
        )

    @patch("app.subprocess.Popen")
    def test_custom_sort_without_field_is_rejected(self, popen):
        response = self._post(popen, sort="custom", sort_field="   ")

        self.assertEqual(response.status_code, 400)
        popen.assert_not_called()

    @patch("app.subprocess.Popen")
    def test_sort_field_starting_with_hyphen_is_rejected(self, popen):
        for mode in ("custom", "date"):
            with self.subTest(mode=mode):
                response = self._post(popen, sort=mode, sort_field="--help")

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json(), {"error": "Invalid sort field"})
                popen.assert_not_called()

    @patch("app.subprocess.Popen")
    def test_unknown_sort_mode_is_rejected(self, popen):
        response = self._post(popen, sort="alphabetical")

        self.assertEqual(response.status_code, 400)
        popen.assert_not_called()

    @patch("app.subprocess.Popen")
    def test_sort_field_ignored_for_non_custom_modes(self, popen):
        response = self._post(popen, sort="date", sort_field="Voucher Number(s)")

        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0]
        self.assertNotIn("--sort-field", command)


if __name__ == "__main__":
    unittest.main()
