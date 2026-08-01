#!/usr/bin/env python3

from __future__ import annotations

import argparse
import contextlib
import copy
import importlib.util
import io
import os
import sys
import textwrap
import time
from pathlib import Path
from types import ModuleType

DETERMINISTIC_ENV = {
    "SOURCE_DATE_EPOCH": "0",
    "TZ": "UTC",
    "INAT_MAX_WORKERS": "1",
    "INAT_RATE_LIMIT_RPM": "0",
    "INAT_QUIET": "1",
}


def _configure_environment() -> None:
    for key, value in DETERMINISTIC_ENV.items():
        os.environ.setdefault(key, value)
    if hasattr(time, "tzset"):
        time.tzset()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_inat_module(root: Path) -> ModuleType:
    return _load_module("inat_label_module", root / "inat.label.py")


def _load_fixture_module(root: Path) -> ModuleType:
    return _load_module(
        "real_world_label_regression_fixtures",
        root / "tests" / "test_real_world_label_regressions.py",
    )


def _patch_observation_fetcher(inat: ModuleType, fixtures: ModuleType) -> None:
    observations = {
        "183905751": (fixtures._full_inat_observation(), "Fungi"),
        "MO505283": (fixtures._mushroom_observer_observation(), "Fungi"),
    }

    def fake_get_observation_data(observation_id: object) -> tuple[dict, str]:
        return copy.deepcopy(observations[str(observation_id)])

    inat.get_observation_data = fake_get_observation_data


def _standard_labels(inat: ModuleType, fixtures: ModuleType) -> list:
    labels = [
        inat.create_inaturalist_label(
            copy.deepcopy(fixtures._full_inat_observation()),
            "Fungi",
            show_common_names=True,
        ),
        inat.create_inaturalist_label(
            copy.deepcopy(fixtures._mushroom_observer_observation()),
            "Fungi",
            show_common_names=True,
        ),
    ]
    labels = [label for label in labels if label is not None]
    return inat.sort_labels([(i, label, None) for i, label in enumerate(labels)], None, None, None)


def _minilabel_labels(standard_labels: list) -> list:
    return standard_labels + [
        (
            [
                ("BugGuide Number", "2520730"),
                ("BugGuide URL", "https://bugguide.net/node/view/2520730"),
            ],
            "BugGuide",
        )
    ]


def _run_cli(inat: ModuleType, argv: list[str]) -> tuple[str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    old_argv = sys.argv[:]
    try:
        sys.argv = argv
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                inat.main()
            except SystemExit as exc:
                print(f"SystemExit: {exc.code}", file=stderr)
    finally:
        sys.argv = old_argv
    return stdout.getvalue(), stderr.getvalue()


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def generate_outputs(output_dir: Path) -> None:
    _configure_environment()
    root = _repo_root()
    output_dir.mkdir(parents=True, exist_ok=True)

    inat = _load_inat_module(root)
    fixtures = _load_fixture_module(root)
    _patch_observation_fetcher(inat, fixtures)

    standard_labels = _standard_labels(inat, fixtures)
    minilabel_labels = _minilabel_labels(standard_labels)

    _write_text(output_dir / "standard.rtf", inat.create_rtf_content(standard_labels))
    _write_text(
        output_dir / "minilabel.rtf",
        inat.create_minilabel_rtf_content(minilabel_labels),
    )
    inat.create_pdf_content(standard_labels, str(output_dir / "standard.pdf"))
    inat.create_minilabel_pdf_content(
        minilabel_labels,
        str(output_dir / "minilabel.pdf"),
    )

    stdout, stderr = _run_cli(
        inat,
        [
            "inat.label.py",
            "183905751",
            "MO505283",
            "--common-names",
            "--workers",
            "1",
        ],
    )
    _write_text(output_dir / "standard.stdout", stdout)
    _write_text(output_dir / "standard.stderr", stderr)

    csv_file = output_dir / "fair.csv"
    _write_text(
        csv_file,
        textwrap.dedent(
            """\
            Scientific Name,Common Name,Habitat,Spore Print,Edibility
            Armillaria mellea,honey mushroom,on buried wood,white,edible
            Chlorophyllum molybdites,green-spored parasol,lawns,green,toxic
            Mystery cup,,duff,,maybe
            ,missing name,wood chips,brown,inedible
            """
        ),
    )

    stdout, stderr = _run_cli(inat, ["inat.label.py", "--fungusfair", str(csv_file)])
    _write_text(output_dir / "fungusfair.stdout", stdout)
    _write_text(output_dir / "fungusfair.stderr", stderr)

    fungusfair_rtf = output_dir / "fungusfair.rtf"
    stdout, stderr = _run_cli(
        inat,
        ["inat.label.py", "--fungusfair", str(csv_file), "--rtf", str(fungusfair_rtf)],
    )
    _write_text(output_dir / "fungusfair_rtf.stdout", stdout)
    _write_text(output_dir / "fungusfair_rtf.stderr", stderr)

    fungusfair_pdf = output_dir / "fungusfair.pdf"
    stdout, stderr = _run_cli(
        inat,
        ["inat.label.py", "--fungusfair", str(csv_file), "--pdf", str(fungusfair_pdf)],
    )
    _write_text(output_dir / "fungusfair_pdf.stdout", stdout)
    _write_text(output_dir / "fungusfair_pdf.stderr", stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate local golden refactor outputs.")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    generate_outputs(args.output_dir)
    print(f"Wrote golden outputs to {args.output_dir}")


if __name__ == "__main__":
    main()
