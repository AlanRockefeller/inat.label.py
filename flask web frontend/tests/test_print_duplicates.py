import unittest
from unittest.mock import MagicMock, patch

try:
    import flask  # noqa: F401
    import app as labels_app
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(f"Flask app dependencies are unavailable: {exc}") from exc


class TestPrintDuplicateLabels(unittest.TestCase):
    def setUp(self):
        labels_app.app.config["TESTING"] = True
        self.client = labels_app.app.test_client()
        labels_app._jobs.clear()

    def tearDown(self):
        labels_app._jobs.clear()

    @patch("app.uuid4", return_value="duplicate-test-job")
    @patch("app.subprocess.Popen")
    def test_print_start_duplicates_each_label_when_requested(self, popen, _uuid4):
        proc = MagicMock()
        proc.poll.return_value = None
        popen.return_value = proc

        response = self.client.post(
            "/labels/print_start",
            data={
                "format": "rtf",
                "observations[]": ["123", "456"],
                "print_duplicate_labels": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        command = popen.call_args.args[0]
        # Observation IDs are passed as positionals after the "--" end-of-options
        # separator, so nothing user-supplied can be read back as an option.
        separator_index = command.index("--")
        self.assertEqual(
            command[separator_index + 1 :],
            ["123", "123", "456", "456"],
        )


if __name__ == "__main__":
    unittest.main()
