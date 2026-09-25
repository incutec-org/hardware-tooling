"""Tests for hardware/mermaid_check.py; the renderer is mocked, no network."""
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hardware"))

import mermaid_check  # noqa: E402

GOOD = 'flowchart LR\n  A["Board<br/>5 inch"] --> B["Release"]\n'
BAD = 'flowchart LR\n  O["Onshape workspace 5""] --> V["Named version"]\n'
PARSE_ERROR = (
    "Error: Parse error on line 2:\n"
    '...pe workspace 5""] --> V\n'
    "-----------------------^\n"
    "Expecting 'SQE', got 'STR'\n"
    "Parser.parseError (https://mermaid-cli-intercept.invalid/chunk.mjs:1:1)\n"
    "    at async renderMermaid (file:///index.js:1:1)\n"
)


def fake_run(cmd, **kwargs):
    source = Path(cmd[cmd.index("-i") + 1]).read_text(encoding="utf-8")
    if '5""' in source:
        return subprocess.CompletedProcess(cmd, 1, "", PARSE_ERROR)
    return subprocess.CompletedProcess(cmd, 0, "", "")


class MermaidCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, *blocks):
        body = "# Title\n\nText.\n\n" + "\n".join(f"```mermaid\n{b}```\n" for b in blocks)
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def run_main(self, *paths):
        out = io.StringIO()
        with mock.patch.object(mermaid_check, "find_renderer", return_value=["mmdc"]), \
                mock.patch.object(mermaid_check.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(out):
            code = mermaid_check.main([str(p) for p in paths])
        return code, out.getvalue()

    def test_extract_reports_fence_line(self):
        blocks = mermaid_check.extract_blocks("a\n\n```mermaid\nflowchart LR\n  A --> B\n```\n")
        self.assertEqual(blocks, [(3, "flowchart LR\n  A --> B")])

    def test_good_block_passes(self):
        code, out = self.run_main(self.write("ok.md", GOOD))
        self.assertEqual(code, 0)
        self.assertIn("1 blocks, 0 failed", out)

    def test_broken_block_fails_with_file_line_and_parser_error(self):
        path = self.write("bad.md", GOOD, BAD)
        code, out = self.run_main(path)
        self.assertEqual(code, 1)
        # Second block fence is on line 10; parser line 2 maps to file line 12.
        self.assertIn(f"{path}:12: Error: Parse error on line 2:", out)
        self.assertIn("Expecting 'SQE', got 'STR'", out)
        self.assertNotIn("renderMermaid", out)

    def test_directory_skips_node_modules(self):
        self.write("docs/a.md", GOOD)
        self.write("node_modules/pkg/README.md", BAD)
        code, out = self.run_main(self.tmp)
        self.assertEqual(code, 0)
        self.assertIn("1 blocks", out)

    def test_lint_warnings(self):
        warnings = mermaid_check.lint(
            'flowchart LR\n  A["x "y" z"] --> B["a<b"]\n  A -. probe .-> C["ok<br/>ok"]\n  subgraph S\n  end\n')
        messages = [f"{offset}:{text.split(';')[0]}" for offset, text in warnings]
        self.assertIn("2:raw double quote inside or around a label", messages)
        self.assertIn("2:angle bracket that is not <br/>", messages)
        self.assertIn("4:subgraph does not render in Notion", messages)
        self.assertFalse([w for w in warnings if w[0] == 3])

    def test_skips_without_renderer(self):
        out = io.StringIO()
        with mock.patch.object(mermaid_check, "find_renderer", return_value=None), redirect_stdout(out):
            code = mermaid_check.main([str(self.write("bad.md", BAD))])
        self.assertEqual(code, 0)
        self.assertIn("skipped", out.getvalue())

    def test_missing_path_exits_2(self):
        with mock.patch.object(mermaid_check, "find_renderer", return_value=["mmdc"]), \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(mermaid_check.main([str(self.tmp / "absent.md")]), 2)


if __name__ == "__main__":
    unittest.main()
