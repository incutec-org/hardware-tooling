#!/usr/bin/env python3
"""Scan the workspace for stale documentation claims.

De-biasing tool for a workspace-wide documentation refactor: walks every
git-tracked text file of the root repository and of every repository listed
in `repos.json` that exists on disk, and reports `path:line:term` for a term
list (retired systems stated as current authority, an old contact address, an
old primary domain, or leftover placeholder text).

Usage:
    python3 docs/stale_check.py [--workspace-root PATH] [--repo NAME ...]
                                 [--terms PATH] [--summary] [--json]

The term list is workspace record, not tool configuration, so it lives in the
workspace being scanned. `--terms` selects it explicitly; otherwise it is
`<workspace-root>/.incutec/stale_terms.json` when that file exists, and
`docs/stale_terms.example.json` next to this script otherwise. The example
carries only generic placeholder terms and documents the file format.

`--workspace-root` defaults to the directory that holds `repos.json`, found
by walking up from the current directory; pass it explicitly when running
from elsewhere. `--repo` limits the scan to one or more repository names
("root" for the workspace root, otherwise the `path` field from
`repos.json`, for example "erp" or "OpenDrone/hardware/OpenFrame").

A repository entry is scanned only when its directory exists on disk and is
a Git checkout; entries that are not cloned locally are silently skipped, so
this tool degrades gracefully when it runs from a worktree that has no
sibling checkouts. Exit status 1 when a hit exists outside the term list's
allowlist, 0 otherwise (including when nothing was scanned).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

TRACKED_EXTENSIONS = {
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".py",
    ".ts",
    ".tsx",
    ".sh",
}

EXAMPLE_TERMS_PATH = Path(__file__).with_name("stale_terms.example.json")
WORKSPACE_TERMS_RELPATH = Path(".incutec") / "stale_terms.json"


@dataclass
class Hit:
    repo: str
    path: str
    line: int
    term_id: str
    text: str
    allowlisted: bool


def find_workspace_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "repos.json").is_file():
            return candidate
    return None


def resolve_terms_path(explicit: Path | None, workspace_root: Path) -> Path:
    """Pick the term list: an explicit `--terms`, else the scanned workspace's
    own list, else the generic example shipped with this tool."""
    if explicit is not None:
        return explicit
    workspace_terms = workspace_root / WORKSPACE_TERMS_RELPATH
    if workspace_terms.is_file():
        return workspace_terms
    return EXAMPLE_TERMS_PATH


def load_terms(terms_path: Path) -> tuple[list[dict], list[str]]:
    data = json.loads(terms_path.read_text(encoding="utf-8"))
    return data["terms"], data.get("allowlist_globs", [])


def compile_terms(terms: list[dict]) -> list[tuple[str, re.Pattern]]:
    compiled = []
    for term in terms:
        flags = re.IGNORECASE if "i" in term.get("flags", "") else 0
        if term["type"] == "literal":
            pattern = re.compile(re.escape(term["pattern"]), flags)
        elif term["type"] == "regex":
            pattern = re.compile(term["pattern"], flags)
        else:
            raise ValueError(f"unknown term type: {term['type']!r}")
        compiled.append((term["id"], pattern))
    return compiled


def is_git_checkout(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def tracked_files(path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(path), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    raw = result.stdout.decode("utf-8", "surrogateescape")
    return [entry for entry in raw.split("\0") if entry]


def resolve_repositories(
    workspace_root: Path, only: set[str] | None
) -> tuple[list[tuple[str, Path]], list[str]]:
    manifest = json.loads((workspace_root / "repos.json").read_text(encoding="utf-8"))
    scanned: list[tuple[str, Path]] = []
    skipped: list[str] = []

    for entry in manifest["repositories"]:
        rel_path = entry["path"]
        name = "root" if rel_path == "." else rel_path
        if only is not None and name not in only:
            continue
        disk_path = workspace_root if rel_path == "." else workspace_root / rel_path
        if not disk_path.is_dir() or not is_git_checkout(disk_path):
            skipped.append(name)
            continue
        scanned.append((name, disk_path))

    return scanned, skipped


def scan_repository(
    name: str,
    disk_path: Path,
    compiled_terms: list[tuple[str, re.Pattern]],
    allowlist_globs: list[str],
) -> list[Hit]:
    hits: list[Hit] = []
    for rel in tracked_files(disk_path):
        if Path(rel).suffix not in TRACKED_EXTENSIONS:
            continue
        file_path = disk_path / rel
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        doc_id = f"{name}/{rel}"
        allowlisted = any(fnmatch.fnmatch(doc_id, glob) for glob in allowlist_globs)

        for line_number, line in enumerate(text.splitlines(), start=1):
            for term_id, pattern in compiled_terms:
                if pattern.search(line):
                    hits.append(
                        Hit(
                            repo=name,
                            path=rel,
                            line=line_number,
                            term_id=term_id,
                            text=line.strip()[:200],
                            allowlisted=allowlisted,
                        )
                    )
    return hits


def build_summary(hits: list[Hit]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for hit in hits:
        if not hit.allowlisted:
            summary[hit.repo] = summary.get(hit.repo, 0) + 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace-root", type=Path, default=None)
    parser.add_argument("--repo", action="append", default=None, metavar="NAME")
    parser.add_argument("--terms", type=Path, default=None)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    workspace_root = args.workspace_root or find_workspace_root(Path.cwd())
    if workspace_root is None:
        print("error: could not find repos.json; pass --workspace-root", file=sys.stderr)
        return 2
    workspace_root = workspace_root.resolve()

    terms_path = resolve_terms_path(args.terms, workspace_root)
    if not terms_path.is_file():
        print(f"error: term list not found: {terms_path}", file=sys.stderr)
        return 2
    terms, allowlist_globs = load_terms(terms_path)
    compiled_terms = compile_terms(terms)
    only = set(args.repo) if args.repo else None

    scanned, skipped = resolve_repositories(workspace_root, only)

    all_hits: list[Hit] = []
    for name, disk_path in scanned:
        all_hits.extend(scan_repository(name, disk_path, compiled_terms, allowlist_globs))

    non_allowlisted = [hit for hit in all_hits if not hit.allowlisted]
    summary = build_summary(all_hits)

    if args.json:
        payload = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "workspace_root": str(workspace_root),
            "repos_scanned": [name for name, _ in scanned],
            "repos_skipped": skipped,
            "terms_file": str(terms_path),
            "total_hits": len(non_allowlisted),
            "total_allowlisted": len(all_hits) - len(non_allowlisted),
            "summary": summary,
            "hits": [asdict(hit) for hit in all_hits],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.summary:
        for name in sorted(summary, key=lambda repo: (-summary[repo], repo)):
            print(f"{name}\t{summary[name]}")
        print(f"total\t{len(non_allowlisted)}")
        if skipped:
            print(f"skipped (not on disk): {', '.join(sorted(skipped))}", file=sys.stderr)
    else:
        for hit in sorted(non_allowlisted, key=lambda h: (h.repo, h.path, h.line)):
            print(f"{hit.repo}/{hit.path}:{hit.line}:{hit.term_id}")

    return 1 if non_allowlisted else 0


if __name__ == "__main__":
    sys.exit(main())
