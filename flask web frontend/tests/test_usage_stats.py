import datetime
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import usage_stats


class UsageStatsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="labels-usage-stats-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.jobs_dir = os.path.join(self.tmpdir, "jobs")
        self.ledger = os.path.join(self.tmpdir, "stats", "usage_daily.csv")
        os.makedirs(self.jobs_dir)
        self.now = 1_700_000_000.0

    def _make_job(self, job_id, age_days, label_count=10, extension="rtf"):
        """Create a job directory sized to hold ``label_count`` labels."""
        job_path = os.path.join(self.jobs_dir, job_id)
        os.makedirs(job_path)
        bytes_per_label = (
            usage_stats.RTF_BYTES_PER_LABEL
            if extension == "rtf"
            else usage_stats.PDF_BYTES_PER_LABEL
        )
        output = os.path.join(job_path, f"labels.{extension}")
        with open(output, "wb") as handle:
            handle.write(b"x" * (bytes_per_label * label_count))

        mtime = self.now - age_days * 86400
        os.utime(output, (mtime, mtime))
        os.utime(job_path, (mtime, mtime))
        return job_path, datetime.date.fromtimestamp(mtime)


class TestPruneJobDirs(UsageStatsTestCase):
    def test_old_directories_are_deleted_and_counted_in_the_ledger(self):
        old_path, old_date = self._make_job("old", age_days=45, label_count=7)

        result = usage_stats.prune_job_dirs(
            retention_days=30,
            jobs_dir=self.jobs_dir,
            ledger_path=self.ledger,
            now=self.now,
        )

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["labels"], 7)
        self.assertFalse(os.path.exists(old_path))
        self.assertEqual(
            usage_stats.load_ledger(self.ledger), {old_date: {"jobs": 1, "labels": 7}}
        )

    def test_recent_directories_are_left_alone(self):
        recent_path, _ = self._make_job("recent", age_days=5)

        result = usage_stats.prune_job_dirs(
            retention_days=30,
            jobs_dir=self.jobs_dir,
            ledger_path=self.ledger,
            now=self.now,
        )

        self.assertEqual(result["removed"], 0)
        self.assertTrue(os.path.exists(recent_path))
        self.assertEqual(usage_stats.load_ledger(self.ledger), {})

    def test_same_day_jobs_accumulate_into_one_ledger_row(self):
        _, day = self._make_job("a", age_days=40, label_count=3)
        self._make_job("b", age_days=40, label_count=4)

        usage_stats.prune_job_dirs(
            retention_days=30,
            jobs_dir=self.jobs_dir,
            ledger_path=self.ledger,
            now=self.now,
        )

        self.assertEqual(
            usage_stats.load_ledger(self.ledger), {day: {"jobs": 2, "labels": 7}}
        )

    def test_repeated_prunes_do_not_double_count(self):
        _, day = self._make_job("old", age_days=40, label_count=5)

        for _ in range(3):
            usage_stats.prune_job_dirs(
                retention_days=30,
                jobs_dir=self.jobs_dir,
                ledger_path=self.ledger,
                now=self.now,
            )

        self.assertEqual(
            usage_stats.load_ledger(self.ledger), {day: {"jobs": 1, "labels": 5}}
        )

    def test_pdf_jobs_use_the_pdf_size_estimate(self):
        _, day = self._make_job("pdf", age_days=40, label_count=6, extension="pdf")

        usage_stats.prune_job_dirs(
            retention_days=30,
            jobs_dir=self.jobs_dir,
            ledger_path=self.ledger,
            now=self.now,
        )

        self.assertEqual(usage_stats.load_ledger(self.ledger)[day]["labels"], 6)


class TestDailyUsage(UsageStatsTestCase):
    def test_archived_and_live_days_are_merged_without_double_counting(self):
        _, old_day = self._make_job("old", age_days=40, label_count=8)
        _, recent_day = self._make_job("recent", age_days=2, label_count=5)

        usage_stats.prune_job_dirs(
            retention_days=30,
            jobs_dir=self.jobs_dir,
            ledger_path=self.ledger,
            now=self.now,
        )
        rows = usage_stats.daily_usage(
            jobs_dir=self.jobs_dir, ledger_path=self.ledger
        )

        by_date = {row["date"]: row for row in rows}
        self.assertEqual(by_date[old_day]["jobs"], 1)
        self.assertEqual(by_date[old_day]["labels"], 8)
        self.assertEqual(by_date[recent_day]["jobs"], 1)
        self.assertEqual(by_date[recent_day]["labels"], 5)
        self.assertEqual(sum(row["jobs"] for row in rows), 2)

    def test_history_survives_the_directories_it_came_from(self):
        """The point of the ledger: totals stay put after the files are gone."""
        self._make_job("old", age_days=40, label_count=9)

        before = usage_stats.daily_usage(
            jobs_dir=self.jobs_dir, ledger_path=self.ledger
        )
        usage_stats.prune_job_dirs(
            retention_days=30,
            jobs_dir=self.jobs_dir,
            ledger_path=self.ledger,
            now=self.now,
        )
        after = usage_stats.daily_usage(
            jobs_dir=self.jobs_dir, ledger_path=self.ledger
        )

        self.assertEqual(before, after)
        self.assertEqual(os.listdir(self.jobs_dir), [])

    def test_ledger_is_readable_by_users_other_than_the_writer(self):
        """make_graph.py is run by people; the service user writes the ledger."""
        self._make_job("old", age_days=40)

        usage_stats.prune_job_dirs(
            retention_days=30,
            jobs_dir=self.jobs_dir,
            ledger_path=self.ledger,
            now=self.now,
        )

        self.assertEqual(
            os.stat(self.ledger).st_mode & 0o777, usage_stats.LEDGER_FILE_MODE
        )

    def test_ensure_ledger_permissions_repairs_an_existing_file(self):
        self._make_job("old", age_days=40)
        usage_stats.prune_job_dirs(
            retention_days=30,
            jobs_dir=self.jobs_dir,
            ledger_path=self.ledger,
            now=self.now,
        )
        os.chmod(self.ledger, 0o600)

        usage_stats.ensure_ledger_permissions(self.ledger)

        self.assertEqual(
            os.stat(self.ledger).st_mode & 0o777, usage_stats.LEDGER_FILE_MODE
        )

    def test_missing_ledger_and_missing_jobs_dir_are_not_errors(self):
        self.assertEqual(
            usage_stats.daily_usage(
                jobs_dir=os.path.join(self.tmpdir, "nope"),
                ledger_path=os.path.join(self.tmpdir, "nope.csv"),
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
