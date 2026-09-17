#!/usr/bin/env python3
"""Job-directory retention and the daily usage ledger.

Generated label files used to accumulate in ``static/jobs/`` forever, which is
both a disk-exhaustion risk (any anonymous request creates a directory) and a
privacy problem (every label ever produced stayed downloadable).  Job
directories are now pruned after ``JOB_RETENTION_DAYS``.

``make_graph.py`` derives its usage chart from those directories, so pruning
would otherwise erase the history behind the graph.  Before a directory is
deleted, a pending per-job entry is written durably to the ledger.  The entry is
marked committed only after deletion succeeds.  A later sweep can therefore
finish an interrupted deletion without losing or double-counting its usage.

The app prunes on a timer in its reaper thread, so no cron entry is needed.
``--status`` is read-only and safe to run as anyone; run ``--prune`` as the
``labels`` service user, since it writes the ledger that the service must keep
writing afterwards::

    venv/bin/python usage_stats.py --status
    venv/bin/python usage_stats.py --prune
"""

from __future__ import annotations

import argparse
import csv
import datetime
import errno
import fcntl
import os
import shutil
import tempfile
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "static", "jobs")
LEDGER_PATH = os.path.join(BASE_DIR, "stats", "usage_daily.csv")

# Bytes per label, measured from sample output.  The ledger records label counts
# estimated the same way ``make_graph.py`` always has, so archived days and live
# days stay comparable instead of stepping at the retention boundary.
RTF_BYTES_PER_LABEL = 1055  # 11607 bytes / 11 labels
PDF_BYTES_PER_LABEL = 2175  # 23926 bytes / 11 labels

JOB_RETENTION_DAYS = int(os.environ.get("LABELS_JOB_RETENTION_DAYS", "30"))

LEDGER_FIELDS = ("date", "jobs", "labels", "job_id", "state")
ARCHIVE_PENDING = "pending"
ARCHIVE_COMMITTED = "committed"

# The app writes the ledger as the `labels` service user, but make_graph.py is
# often run by a person.  Atomic replace would otherwise leave the temp file's
# 0600, locking everyone else out of the graph data.
LEDGER_FILE_MODE = 0o644

_ledger_lock = threading.Lock()


def ensure_ledger_permissions(ledger_path: str = LEDGER_PATH) -> None:
    """Keep an existing ledger readable; quietly do nothing if we cannot."""
    try:
        if os.stat(ledger_path).st_mode & 0o777 != LEDGER_FILE_MODE:
            os.chmod(ledger_path, LEDGER_FILE_MODE)
    except OSError:
        pass


def estimate_label_count(file_path: str) -> int:
    """Estimate how many labels an output file holds from its size."""
    try:
        size_bytes = os.path.getsize(file_path)
    except OSError:
        return 0

    if file_path.endswith(".rtf"):
        return round(size_bytes / RTF_BYTES_PER_LABEL)
    if file_path.endswith(".pdf"):
        return round(size_bytes / PDF_BYTES_PER_LABEL)
    return 0


def job_dir_stats(job_path: str):
    """Return ``(date, label_count)`` for one job directory, or ``None``."""
    try:
        job_date = datetime.date.fromtimestamp(os.path.getmtime(job_path))
    except OSError:
        return None

    for filename in ("labels.rtf", "labels.pdf"):
        candidate = os.path.join(job_path, filename)
        if os.path.exists(candidate):
            return job_date, estimate_label_count(candidate)
    return job_date, 0


def iter_job_dirs(jobs_dir: str = JOBS_DIR):
    """Yield ``(job_id, job_path)`` for each job directory that still exists."""
    try:
        entries = sorted(os.listdir(jobs_dir))
    except FileNotFoundError:
        return

    for job_id in entries:
        job_path = os.path.join(jobs_dir, job_id)
        if os.path.isdir(job_path):
            yield job_id, job_path


def _load_ledger_state(ledger_path: str):
    """Return legacy daily totals and the latest per-job archive records."""
    daily_totals: dict = {}
    job_records: dict = {}
    try:
        with open(ledger_path, "r", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    day = datetime.date.fromisoformat((row.get("date") or "").strip())
                    jobs = int(row.get("jobs") or 0)
                    labels = int(row.get("labels") or 0)
                except (TypeError, ValueError):
                    continue
                job_id = (row.get("job_id") or "").strip()
                if job_id:
                    state = (row.get("state") or ARCHIVE_COMMITTED).strip()
                    if state not in (ARCHIVE_PENDING, ARCHIVE_COMMITTED):
                        continue
                    job_records[job_id] = {
                        "date": day,
                        "jobs": jobs,
                        "labels": labels,
                        "state": state,
                    }
                    continue
                entry = daily_totals.setdefault(day, {"jobs": 0, "labels": 0})
                entry["jobs"] += jobs
                entry["labels"] += labels
    except FileNotFoundError:
        pass
    return daily_totals, job_records


def _ledger_totals(daily_totals: dict, job_records: dict) -> dict:
    totals = {
        day: {"jobs": entry["jobs"], "labels": entry["labels"]}
        for day, entry in daily_totals.items()
    }
    for record in job_records.values():
        if record["state"] != ARCHIVE_COMMITTED:
            continue
        entry = totals.setdefault(record["date"], {"jobs": 0, "labels": 0})
        entry["jobs"] += record["jobs"]
        entry["labels"] += record["labels"]
    return totals


def load_ledger(ledger_path: str = LEDGER_PATH) -> dict:
    """Read committed usage as ``{date: {"jobs": int, "labels": int}}``."""
    return _ledger_totals(*_load_ledger_state(ledger_path))


def _write_ledger_locked(
    daily_totals: dict, job_records: dict, ledger_path: str
) -> None:
    """Rewrite the ledger atomically; caller holds the on-disk lock."""
    directory = os.path.dirname(ledger_path) or "."
    handle = tempfile.NamedTemporaryFile(
        "w", newline="", dir=directory, delete=False, prefix=".usage_daily-"
    )
    try:
        with handle:
            writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
            writer.writeheader()
            for day in sorted(daily_totals):
                entry = daily_totals[day]
                writer.writerow(
                    {
                        "date": day.isoformat(),
                        "jobs": int(entry["jobs"]),
                        "labels": int(entry["labels"]),
                        "job_id": "",
                        "state": ARCHIVE_COMMITTED,
                    }
                )
            for job_id in sorted(job_records):
                record = job_records[job_id]
                writer.writerow(
                    {
                        "date": record["date"].isoformat(),
                        "jobs": int(record["jobs"]),
                        "labels": int(record["labels"]),
                        "job_id": job_id,
                        "state": record["state"],
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, LEDGER_FILE_MODE)
        os.replace(handle.name, ledger_path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def record_daily_totals(daily_totals: dict, ledger_path: str = LEDGER_PATH) -> None:
    """Add ``{date: {"jobs": n, "labels": n}}`` into the ledger.

    Serialized by a thread lock and an ``flock`` on a sidecar file so a manual
    ``--prune`` run cannot interleave with the app's background prune.
    """
    if not daily_totals:
        return

    os.makedirs(os.path.dirname(ledger_path) or ".", exist_ok=True)
    lock_path = ledger_path + ".lock"

    with _ledger_lock:
        with open(lock_path, "a+") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            try:
                stored_totals, job_records = _load_ledger_state(ledger_path)
                for day, entry in daily_totals.items():
                    running = stored_totals.setdefault(
                        day, {"jobs": 0, "labels": 0}
                    )
                    running["jobs"] += int(entry.get("jobs", 0))
                    running["labels"] += int(entry.get("labels", 0))
                _write_ledger_locked(stored_totals, job_records, ledger_path)
            finally:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)


def prune_job_dirs(
    retention_days: int = JOB_RETENTION_DAYS,
    jobs_dir: str = JOBS_DIR,
    ledger_path: str = LEDGER_PATH,
    now: float | None = None,
) -> dict:
    """Delete job directories older than ``retention_days``, keeping the counts.

    Each directory is measured and recorded as pending before deletion.  A
    successful deletion changes that same idempotent job record to committed.
    Pending records are reconciled on the next run after an interruption.
    """
    if retention_days <= 0:
        return {"removed": 0, "failed": 0, "labels": 0, "bytes_freed": 0}

    cutoff = (now if now is not None else _now()) - retention_days * 86400
    removed = 0
    failed = 0
    labels_archived = 0
    bytes_freed = 0

    os.makedirs(os.path.dirname(ledger_path) or ".", exist_ok=True)
    lock_path = ledger_path + ".lock"

    with _ledger_lock:
        with open(lock_path, "a+") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            try:
                daily_totals, job_records = _load_ledger_state(ledger_path)

                # If a prepared job has vanished, its deletion succeeded before
                # the process could commit the ledger entry.
                reconciled = False
                for job_id, record in job_records.items():
                    if record["state"] != ARCHIVE_PENDING:
                        continue
                    if os.path.basename(job_id) != job_id:
                        continue
                    if not os.path.isdir(os.path.join(jobs_dir, job_id)):
                        record["state"] = ARCHIVE_COMMITTED
                        reconciled = True

                candidates = []
                pending_added = False
                for job_id, job_path in iter_job_dirs(jobs_dir):
                    existing = job_records.get(job_id)
                    if existing and existing["state"] == ARCHIVE_COMMITTED:
                        continue

                    try:
                        if os.path.getmtime(job_path) >= cutoff:
                            continue
                    except OSError:
                        continue

                    if existing is None:
                        stats = job_dir_stats(job_path)
                        if stats is None:
                            continue
                        job_date, label_count = stats
                        existing = {
                            "date": job_date,
                            "jobs": 1,
                            "labels": label_count,
                            "state": ARCHIVE_PENDING,
                        }
                        job_records[job_id] = existing
                        pending_added = True

                    candidates.append((job_id, job_path, _dir_size(job_path)))

                # Prepare every candidate durably before deleting any of them.
                if reconciled or pending_added:
                    _write_ledger_locked(daily_totals, job_records, ledger_path)

                committed = False
                for job_id, job_path, size in candidates:
                    record = job_records[job_id]
                    try:
                        shutil.rmtree(job_path)
                    except FileNotFoundError:
                        # A prepared deletion completed outside this sweep.
                        pass
                    except OSError:
                        failed += 1
                        continue

                    record["state"] = ARCHIVE_COMMITTED
                    committed = True
                    removed += 1
                    labels_archived += record["labels"]
                    bytes_freed += size

                if committed:
                    _write_ledger_locked(daily_totals, job_records, ledger_path)
            finally:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)

    return {
        "removed": removed,
        "failed": failed,
        "labels": labels_archived,
        "bytes_freed": bytes_freed,
    }


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    continue
    return total


def _now() -> float:
    import time

    return time.time()


def daily_usage(jobs_dir: str = JOBS_DIR, ledger_path: str = LEDGER_PATH) -> list:
    """Merge archived ledger days with surviving job directories.

    Returns rows of ``{"date": date, "jobs": int, "labels": int}`` sorted by
    date.  Pruned directories are gone from ``jobs_dir``, so a directory is
    never counted twice.
    """
    daily_totals, job_records = _load_ledger_state(ledger_path)
    totals = _ledger_totals(daily_totals, job_records)
    committed_job_ids = {
        job_id
        for job_id, record in job_records.items()
        if record["state"] == ARCHIVE_COMMITTED
    }

    for job_id, job_path in iter_job_dirs(jobs_dir):
        if job_id in committed_job_ids:
            continue
        stats = job_dir_stats(job_path)
        if stats is None:
            continue
        job_date, label_count = stats
        entry = totals.setdefault(job_date, {"jobs": 0, "labels": 0})
        entry["jobs"] += 1
        entry["labels"] += label_count

    return [
        {"date": day, "jobs": totals[day]["jobs"], "labels": totals[day]["labels"]}
        for day in sorted(totals)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--prune",
        action="store_true",
        help=f"Archive and delete job directories older than {JOB_RETENTION_DAYS} days",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=JOB_RETENTION_DAYS,
        help="Override the retention window for this run",
    )
    parser.add_argument(
        "--status", action="store_true", help="Print ledger and job-directory totals"
    )
    args = parser.parse_args()

    if args.prune:
        result = prune_job_dirs(retention_days=args.retention_days)
        print(
            f"Pruned {result['removed']} job directories "
            f"({result['bytes_freed'] / 1_048_576:.1f} MB, "
            f"{result['labels']} labels archived); {result['failed']} could not be removed."
        )

    if args.status or not args.prune:
        rows = daily_usage()
        live = sum(1 for _ in iter_job_dirs())
        archived_days = len(load_ledger())
        print(f"Ledger: {LEDGER_PATH}")
        print(f"  archived days:      {archived_days}")
        print(f"  live job dirs:      {live}")
        if rows:
            print(f"  usage range:        {rows[0]['date']} .. {rows[-1]['date']}")
            print(f"  total jobs:         {sum(r['jobs'] for r in rows)}")
            print(f"  total labels (est): {sum(r['labels'] for r in rows)}")


if __name__ == "__main__":
    main()
