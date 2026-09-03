import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "overview_check.py"

GOOD = """# Example

## Map

```mermaid
flowchart LR
    A["tools/"] --> B["docs/"]
```
"""


def run(*roots: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, roots)],
        capture_output=True,
        text=True,
    )


class OverviewCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tools").mkdir()
        (self.root / "docs").mkdir()
        (self.root / ".hidden").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_passes_when_every_directory_is_shown(self):
        (self.root / "OVERVIEW.md").write_text(GOOD, encoding="utf-8")
        result = run(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_file_is_a_finding(self):
        result = run(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stderr)

    def test_narrative_outside_diagram_is_a_finding(self):
        (self.root / "OVERVIEW.md").write_text(
            GOOD + "\nSome prose about tools and docs.\n", encoding="utf-8"
        )
        result = run(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("narrative outside diagram", result.stderr)

    def test_unshown_directory_is_a_finding(self):
        (self.root / "extra").mkdir()
        (self.root / "OVERVIEW.md").write_text(GOOD, encoding="utf-8")
        result = run(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("extra/", result.stderr)

    def test_repository_root_uses_tracked_directories(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "tools" / "a.py").write_text("", encoding="utf-8")
        (self.root / "untracked").mkdir()
        (self.root / "untracked" / "b").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "tools"], check=True)
        (self.root / "OVERVIEW.md").write_text(GOOD, encoding="utf-8")
        result = run(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
