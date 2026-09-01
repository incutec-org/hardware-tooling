#!/usr/bin/env python3
"""Run a configurable KiCad release preparation chain.

This is Incutec management software, not product policy. Product repositories
or portfolio standards supply the approved ERC/DRC counts explicitly:

    python3 hardware/release/kicad_release.py path/to/board.kicad_pcb \
      --approved-violations path/to/approved-violations.json \
      --approval-key project/hardware/board

ERC and DRC reports do not need to be empty. A finding blocks only when its
type is not approved or its count exceeds the approved maximum. Tool failures,
blocking 3D-model findings, failed fabrication export, failed STEP export, and
failed schematic export still stop the chain.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
KICAD_TOOLS = Path(
    os.environ.get("INCUTEC_KICAD_TOOLS", HERE.parent / "kicad")
).expanduser().resolve()
KICAD_CLI = os.environ.get(
    "KICAD_CLI", "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
)
KPY = os.environ.get(
    "KPY",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, **kwargs)


def violation_counts(report: Path) -> collections.Counter[str]:
    data = json.loads(report.read_text(encoding="utf-8"))
    violations = list(data.get("violations", []))
    violations.extend(
        violation
        for sheet in data.get("sheets", [])
        for violation in sheet.get("violations", [])
    )
    return collections.Counter(
        f"{violation['type']}:{violation['severity']}" for violation in violations
    )


def load_approved(path: Path | None, key: str) -> dict[str, dict[str, int]]:
    if path is None:
        return {"erc": {}, "drc": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version", 1) != 1 or not isinstance(data.get("boards"), dict):
        raise ValueError("approved-violations file must use schema_version 1 and contain boards")
    board = data["boards"].get(key, {})
    approved = {"erc": board.get("erc", {}), "drc": board.get("drc", {})}
    for kind, entries in approved.items():
        if not isinstance(entries, dict) or any(
            not isinstance(name, str) or not isinstance(count, int) or count < 0
            for name, count in entries.items()
        ):
            raise ValueError(f"invalid {kind} approvals for {key}")
    return approved


def compare_violations(
    kind: str,
    actual: collections.Counter[str],
    approved: dict[str, int],
) -> list[str]:
    problems = []
    for name, count in sorted(actual.items()):
        maximum = approved.get(name)
        if maximum is None:
            problems.append(f"{kind} {name}: {count}, no approval")
        elif count > maximum:
            problems.append(f"{kind} {name}: {count} > approved maximum {maximum}")
        else:
            print(f"    approved {kind} {name}: {count}/{maximum}")
    return problems


def report_command(command: list[str], report: Path, label: str) -> list[str]:
    result = run(command)
    if report.is_file():
        return []
    detail = (result.stderr or result.stdout).strip()[-300:]
    return [f"{label} did not produce a report (exit {result.returncode}): {detail}"]


def design_gate(
    board: Path,
    schematic: Path,
    temporary: Path,
    approved: dict[str, dict[str, int]],
) -> list[str]:
    erc = temporary / "erc.json"
    drc = temporary / "drc.json"
    problems = report_command(
        [
            KICAD_CLI,
            "sch",
            "erc",
            "--format",
            "json",
            "-o",
            str(erc),
            "--severity-error",
            "--severity-warning",
            str(schematic),
        ],
        erc,
        "ERC",
    )
    problems.extend(
        report_command(
            [
                KICAD_CLI,
                "pcb",
                "drc",
                "--format",
                "json",
                "-o",
                str(drc),
                "--schematic-parity",
                "--refill-zones",
                "--severity-error",
                "--severity-warning",
                str(board),
            ],
            drc,
            "DRC",
        )
    )
    if problems:
        return problems
    problems.extend(compare_violations("erc", violation_counts(erc), approved["erc"]))
    problems.extend(compare_violations("drc", violation_counts(drc), approved["drc"]))
    return problems


def command_gate(command: list[str], label: str) -> list[str]:
    result = run(command)
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-300:]
        return [f"{label} failed: {detail}"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path)
    parser.add_argument("--approved-violations", type=Path)
    parser.add_argument(
        "--approval-key",
        help="board key in the approval file (default: board filename without suffix)",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="STEP and schematic output directory (default: <board-dir>/export)",
    )
    parser.add_argument(
        "--skip-fab-export",
        action="store_true",
        help="check the existing quote pack instead of regenerating it",
    )
    args = parser.parse_args(argv)

    board = args.board.expanduser().resolve()
    schematic = board.with_suffix(".kicad_sch")
    if not board.is_file():
        parser.error(f"board does not exist: {board}")
    if not schematic.is_file():
        parser.error(f"matching schematic does not exist: {schematic}")
    missing_tools = [
        name
        for name in ("check_models.py", "quote_pack.py", "export_step.py")
        if not (KICAD_TOOLS / name).is_file()
    ]
    if missing_tools:
        parser.error(f"missing Incutec KiCad tools in {KICAD_TOOLS}: {', '.join(missing_tools)}")

    approval_path = (
        args.approved_violations.expanduser().resolve()
        if args.approved_violations
        else None
    )
    approval_key = args.approval_key or board.stem
    try:
        approved = load_approved(approval_path, approval_key)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))

    artifact_dir = (
        args.artifact_dir.expanduser().resolve()
        if args.artifact_dir
        else board.parent / "export"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    step = artifact_dir / f"{board.stem}.step"
    schematic_pdf = artifact_dir / f"{board.stem}-schematic.pdf"

    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        gates = [
            (
                "G1 ERC/DRC against approved findings",
                lambda: design_gate(board, schematic, temporary, approved),
            ),
            (
                "G2 blocking 3D-model findings",
                lambda: command_gate(
                    [KPY, str(KICAD_TOOLS / "check_models.py"), str(board), "--blocking-only"],
                    "3D-model check",
                ),
            ),
            (
                "G3 fabrication set and export checks",
                lambda: command_gate(
                    [KPY, str(KICAD_TOOLS / "quote_pack.py"), str(board)]
                    + (["--skip-ft"] if args.skip_fab_export else []),
                    "fabrication export",
                ),
            ),
            (
                "G4 STEP export",
                lambda: command_gate(
                    [KPY, str(KICAD_TOOLS / "export_step.py"), str(board), "-o", str(step)],
                    "STEP export",
                ),
            ),
            (
                "G5 schematic PDF",
                lambda: command_gate(
                    [KICAD_CLI, "sch", "export", "pdf", "-o", str(schematic_pdf), str(schematic)],
                    "schematic export",
                ),
            ),
        ]
        for name, gate in gates:
            print(f"== {name}")
            problems = gate()
            if problems:
                for problem in problems:
                    print(f"   FAIL {problem}")
                print(f"stopped at {name}")
                return 1

    print(f"release preparation passed for {board}")
    print(f"artifacts: {artifact_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
