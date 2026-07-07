#!/usr/bin/env python3

from __future__ import annotations

import argparse
import filecmp
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BYTE_ARTIFACTS = (
    "standard.rtf",
    "minilabel.rtf",
    "fungusfair.rtf",
    "standard.stderr",
    "fungusfair.stderr",
    "fungusfair_rtf.stderr",
    "fungusfair_pdf.stderr",
)
STDOUT_ARTIFACTS = (
    "standard.stdout",
    "fungusfair.stdout",
    "fungusfair_rtf.stdout",
    "fungusfair_pdf.stdout",
)
PDF_ARTIFACTS = ("standard.pdf", "minilabel.pdf", "fungusfair.pdf")


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"Required tool not found on PATH: {name}")
    return path


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{detail}")


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _normalize_stdout(data: bytes) -> bytes:
    text = data.decode("utf-8")
    text = re.sub(r"time \d+\.\d+s", "time <elapsed>", text)
    return text.encode("utf-8")


def _normalize_qdf(data: bytes) -> bytes:
    data = re.sub(rb"/ID\s*\[[^\]]+\]", b"/ID [<normalized><normalized>]", data)
    data = re.sub(rb"/CreationDate\s*\(D:[^)]+\)", b"/CreationDate (D:normalized)", data)
    data = re.sub(rb"/ModDate\s*\(D:[^)]+\)", b"/ModDate (D:normalized)", data)
    return data


def _same_file(left: Path, right: Path) -> bool:
    return filecmp.cmp(left, right, shallow=False)


def _compare_byte_artifacts(before: Path, after: Path) -> list[str]:
    failures = []
    for name in BYTE_ARTIFACTS:
        if not _same_file(before / name, after / name):
            failures.append(f"{name}: byte mismatch")
    for name in STDOUT_ARTIFACTS:
        left = _read_bytes(before / name)
        right = _read_bytes(after / name)
        if _normalize_stdout(left) != _normalize_stdout(right):
            failures.append(f"{name}: normalized stdout mismatch")
    return failures


def _extract_pdf_artifacts(before: Path, after: Path, work_dir: Path) -> None:
    pdftotext = _require_tool("pdftotext")
    qpdf = _require_tool("qpdf")
    pdftocairo = _require_tool("pdftocairo")
    for name in PDF_ARTIFACTS:
        stem = Path(name).stem
        before_pdf = before / name
        after_pdf = after / name
        _run([pdftotext, str(before_pdf), str(work_dir / f"{stem}.before.txt")])
        _run([pdftotext, str(after_pdf), str(work_dir / f"{stem}.after.txt")])
        _run([pdftotext, "-bbox", str(before_pdf), str(work_dir / f"{stem}.before.bbox.html")])
        _run([pdftotext, "-bbox", str(after_pdf), str(work_dir / f"{stem}.after.bbox.html")])
        _run(
            [
                qpdf,
                "--qdf",
                "--object-streams=disable",
                str(before_pdf),
                str(work_dir / f"{stem}.before.qdf.pdf"),
            ]
        )
        _run(
            [
                qpdf,
                "--qdf",
                "--object-streams=disable",
                str(after_pdf),
                str(work_dir / f"{stem}.after.qdf.pdf"),
            ]
        )
        _run([pdftocairo, "-png", "-r", "144", str(before_pdf), str(work_dir / f"{stem}.before")])
        _run([pdftocairo, "-png", "-r", "144", str(after_pdf), str(work_dir / f"{stem}.after")])


def _compare_pngs(stem: str, work_dir: Path) -> list[str]:
    failures = []
    before_pages = sorted(work_dir.glob(f"{stem}.before-*.png"))
    after_pages = sorted(work_dir.glob(f"{stem}.after-*.png"))
    if len(before_pages) != len(after_pages):
        return [f"{stem}.pdf: rendered page count mismatch"]

    compare = shutil.which("compare")
    for before_page, after_page in zip(before_pages, after_pages, strict=True):
        if _same_file(before_page, after_page):
            continue
        if compare is None:
            failures.append(f"{stem}.pdf: rendered PNG mismatch for {before_page.name}")
            continue
        diff_file = work_dir / f"{before_page.stem}.diff.png"
        result = subprocess.run(
            [compare, "-metric", "AE", str(before_page), str(after_page), str(diff_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        metric = (result.stderr or result.stdout).strip()
        if result.returncode != 0 or metric != "0":
            failures.append(
                f"{stem}.pdf: rendered PNG mismatch for {before_page.name} (AE={metric})"
            )
    return failures


def _compare_pdf_artifacts(before: Path, after: Path) -> list[str]:
    failures = []
    raw_matches = []
    with tempfile.TemporaryDirectory(prefix="inat-golden-compare-") as tmp:
        work_dir = Path(tmp)
        _extract_pdf_artifacts(before, after, work_dir)
        for name in PDF_ARTIFACTS:
            stem = Path(name).stem
            raw_matches.append(f"{name}: raw={'yes' if _same_file(before / name, after / name) else 'no'}")
            if not _same_file(work_dir / f"{stem}.before.txt", work_dir / f"{stem}.after.txt"):
                failures.append(f"{name}: pdftotext mismatch")
            if not _same_file(
                work_dir / f"{stem}.before.bbox.html",
                work_dir / f"{stem}.after.bbox.html",
            ):
                failures.append(f"{name}: pdftotext -bbox mismatch")

            before_qdf = _normalize_qdf(_read_bytes(work_dir / f"{stem}.before.qdf.pdf"))
            after_qdf = _normalize_qdf(_read_bytes(work_dir / f"{stem}.after.qdf.pdf"))
            if before_qdf != after_qdf:
                failures.append(f"{name}: normalized qdf mismatch")

            failures.extend(_compare_pngs(stem, work_dir))

    print("PDF raw-byte status: " + ", ".join(raw_matches))
    return failures


def compare_outputs(before: Path, after: Path) -> None:
    failures = []
    failures.extend(_compare_byte_artifacts(before, after))
    failures.extend(_compare_pdf_artifacts(before, after))
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        raise SystemExit(1)
    print("Golden comparison passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare local golden refactor outputs.")
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    args = parser.parse_args()

    compare_outputs(args.before, args.after)


if __name__ == "__main__":
    main()
