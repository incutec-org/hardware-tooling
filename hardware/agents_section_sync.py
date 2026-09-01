#!/usr/bin/env python3
"""Sync one named Markdown section from a template file into target files.

The section body between the `## <name>` heading and the next `## ` heading is
taken verbatim from the template and written into each target. Use --check to
report drift without writing; the exit code is 1 when any target differs.

Example:
    python3 agents_section_sync.py --template _template/AGENTS.md \
        --section Rules --check boards/*/AGENTS.md
"""

import argparse
import re
import sys
from pathlib import Path


def split_section(text: str, name: str):
    """Return (start, end) offsets of the section body, or None."""
    pattern = re.compile(
        r"^## " + re.escape(name) + r"[ \t]*$", re.MULTILINE
    )
    match = pattern.search(text)
    if match is None:
        return None
    start = match.end()
    nxt = re.compile(r"^## ", re.MULTILINE).search(text, start)
    end = nxt.start() if nxt else len(text)
    return start, end


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--section", required=True)
    parser.add_argument("--check", action="store_true",
                        help="report drift, write nothing, exit 1 on drift")
    parser.add_argument("--skip-missing", action="store_true",
                        help="ignore targets without the section instead of "
                             "treating them as drift")
    parser.add_argument("targets", nargs="+", type=Path)
    args = parser.parse_args()

    template_text = args.template.read_text(encoding="utf-8")
    span = split_section(template_text, args.section)
    if span is None:
        print(f"error: section '## {args.section}' not in {args.template}",
              file=sys.stderr)
        return 2
    canonical = template_text[span[0]:span[1]]

    drift = False
    for target in args.targets:
        if target.resolve() == args.template.resolve():
            continue
        text = target.read_text(encoding="utf-8")
        tspan = split_section(text, args.section)
        if tspan is None:
            if args.skip_missing:
                print(f"skipped  {target}: no '## {args.section}' section")
            else:
                print(f"MISSING  {target}: no '## {args.section}' section")
                drift = True
            continue
        if text[tspan[0]:tspan[1]] == canonical:
            print(f"ok       {target}")
            continue
        drift = True
        if args.check:
            print(f"DRIFT    {target}")
        else:
            target.write_text(
                text[:tspan[0]] + canonical + text[tspan[1]:],
                encoding="utf-8",
            )
            print(f"synced   {target}")

    return 1 if (drift and args.check) else 0


if __name__ == "__main__":
    sys.exit(main())
