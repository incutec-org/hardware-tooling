#!/usr/bin/env python3
"""Check a repository's OVERVIEW.md visual index.

An OVERVIEW.md is a diagram-only document: Markdown headings and Mermaid
fences, nothing else. It must name every top-level directory of the
repository it describes so that the picture cannot silently drift from the
tree. Run from anywhere:

    python3 overview_check.py REPO_ROOT [REPO_ROOT ...]

Directory names come from `git ls-files` when REPO_ROOT is the top of a Git
work tree and from the filesystem otherwise (for plain directories that group
repositories, such as a workspace folder). Hidden entries are ignored. Exit
status 1 on any finding.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def is_repository_root(root: Path) -> bool:
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return Path(top).resolve() == root.resolve()


def tracked_top_level_dirs(root: Path) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    names = set()
    for entry in out.decode("utf-8", "surrogateescape").split("\0"):
        if "/" in entry:
            head = entry.split("/", 1)[0]
            if not head.startswith("."):
                names.add(head)
    return names


def filesystem_top_level_dirs(root: Path) -> set[str]:
    return {
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "node_modules"
    }


def top_level_dirs(root: Path) -> set[str]:
    if is_repository_root(root):
        return tracked_top_level_dirs(root)
    return filesystem_top_level_dirs(root)


def check_overview(root: Path) -> list[str]:
    findings: list[str] = []
    overview = root / "OVERVIEW.md"
    if not overview.is_file():
        return [f"{overview}: missing"]

    text = overview.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_mermaid = False
    fences = 0
    for number, line in enumerate(lines, start=1):
        if line == "```mermaid":
            in_mermaid = True
            fences += 1
            continue
        if line == "```" and in_mermaid:
            in_mermaid = False
            continue
        if not in_mermaid and line and not line.startswith("#"):
            findings.append(f"{overview}:{number}: narrative outside diagram")
    if in_mermaid:
        findings.append(f"{overview}: unclosed Mermaid fence")
    if fences == 0:
        findings.append(f"{overview}: no Mermaid diagram")
    if not lines or not lines[0].startswith("# "):
        findings.append(f"{overview}:1: first line must be a level-1 heading")

    for name in sorted(top_level_dirs(root)):
        if name not in text:
            findings.append(f"{overview}: top-level directory not shown: {name}/")

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("roots", nargs="+", type=Path, metavar="REPO_ROOT")
    args = parser.parse_args(argv)

    findings: list[str] = []
    for root in args.roots:
        findings.extend(check_overview(root.resolve()))

    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print(f"OK: {len(args.roots)} overview file(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
