#!/usr/bin/env python3
"""Render every Mermaid block in Markdown files and report the ones that fail.

    python3 hardware/mermaid_check.py <file.md | directory> [...]

Each ```mermaid block is rendered with the Mermaid CLI (mmdc). A block that
fails to render is an error: the tool prints file:line and the parser message
and exits 1. It also warns, without failing, about patterns that break other
renderers: a raw double quote inside a quoted label, an angle bracket that is
not <br/> or part of an arrow, and subgraph (Notion does not render it).

The renderer is $MMDC when set, else mmdc on PATH, else
npx -y @mermaid-js/mermaid-cli. Without any of these the check is skipped
with a message and exit code 0. Directories are searched for *.md, skipping
node_modules and .git.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*mermaid\s*$")
PARSE_LINE = re.compile(r"Parse error on line (\d+)")
LABEL = re.compile(r'(\[\[|\[\(|\(\(|\[|\(|\{\{|\{|>)"[^"]*"(\]\]|\)\]|\)\)|\]|\)|\}\}|\})')
EDGE_LABEL = re.compile(r'\|"[^"]*"\|')
BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
ARROW = re.compile(r"<?(--+|==+|-\.+-?|\.-+)[^<>]*?>|<(--+|==+)")
SKIP_DIRS = {"node_modules", ".git"}


def markdown_files(paths):
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for found in sorted(path.rglob("*.md")):
                if not SKIP_DIRS.intersection(found.relative_to(path).parts):
                    yield found
        elif path.is_file():
            yield path
        else:
            raise FileNotFoundError(raw)


def extract_blocks(text):
    """Return (line, code) for each Mermaid block; line is the 1-based fence line."""
    blocks = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        match = FENCE.match(lines[index])
        if match:
            indent, fence = match.group(1), match.group(2)
            end = index + 1
            while end < len(lines) and not lines[end].strip().startswith(fence):
                end += 1
            body = [ln[len(indent):] if ln.startswith(indent) else ln for ln in lines[index + 1:end]]
            blocks.append((index + 1, "\n".join(body)))
            index = end
        index += 1
    return blocks


def lint(code):
    """Return (offset, message) warnings; offset is the 1-based line inside the block."""
    warnings = []
    for offset, line in enumerate(code.split("\n"), start=1):
        if line.strip().startswith("%%"):
            continue
        stripped = EDGE_LABEL.sub("|E|", LABEL.sub("L", line))
        if '"' in stripped:
            warnings.append((offset, "raw double quote inside or around a label; use #quot;"))
        if re.search(r"[<>]", ARROW.sub("", BR.sub("", line))):
            warnings.append((offset, "angle bracket that is not <br/>; use #lt; or #gt;"))
        if re.match(r"\s*subgraph\b", line):
            warnings.append((offset, "subgraph does not render in Notion"))
    return warnings


def find_renderer():
    if os.environ.get("MMDC"):
        return [os.environ["MMDC"]]
    if shutil.which("mmdc"):
        return ["mmdc"]
    if shutil.which("npx"):
        return ["npx", "-y", "@mermaid-js/mermaid-cli"]
    return None


def render(renderer, code):
    """Render one block. Return None on success, else the error text."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "in.mmd"
        source.write_text(code + "\n", encoding="utf-8")
        config = Path(tmp) / "puppeteer.json"
        config.write_text('{"args": ["--no-sandbox"]}', encoding="utf-8")
        result = subprocess.run(
            renderer + ["-q", "-p", str(config), "-i", str(source), "-o", str(Path(tmp) / "out.svg")],
            capture_output=True, text=True, timeout=180)
    if result.returncode == 0:
        return None
    message = []
    for line in (result.stderr or result.stdout).splitlines():
        if line.startswith(("Parser.", "    at ")) or " (file://" in line or " (https://" in line:
            break
        message.append(line)
    return "\n".join(message).strip() or f"renderer exited {result.returncode}"


def check(paths, renderer):
    """Return (failures, warnings, block count) as printable strings."""
    failures, warnings, count = [], [], 0
    for path in markdown_files(paths):
        for line, code in extract_blocks(path.read_text(encoding="utf-8")):
            count += 1
            for offset, message in lint(code):
                warnings.append(f"{path}:{line + offset}: warning: {message}")
            error = render(renderer, code)
            if error:
                parsed = PARSE_LINE.search(error)
                where = line + int(parsed.group(1)) if parsed else line
                failures.append(f"{path}:{where}: {error}")
    return failures, warnings, count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="+", help="Markdown files or directories")
    args = parser.parse_args(argv)
    renderer = find_renderer()
    if renderer is None:
        print("mermaid_check: skipped, no mmdc or npx found; install Node.js to render Mermaid")
        return 0
    try:
        failures, warnings, count = check(args.paths, renderer)
    except FileNotFoundError as missing:
        print(f"mermaid_check: no such file or directory: {missing}", file=sys.stderr)
        return 2
    for line in warnings + failures:
        print(line)
    print(f"mermaid_check: {count} blocks, {len(failures)} failed, {len(warnings)} warnings")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
