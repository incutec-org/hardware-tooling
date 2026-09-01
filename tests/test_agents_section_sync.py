import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "hardware" / "agents_section_sync.py"

TEMPLATE = "# T\n\n## Rules\n\ncanonical body\n\n## Tail\n\ntail\n"
DRIFTED = "# B\n\n## Rules\n\nold body\n\n## Tail\n\nother tail\n"
MISSING = "# B\n\n## Tail\n\ntail\n"


def run(*argv):
    return subprocess.run(
        [sys.executable, str(TOOL), *argv], capture_output=True, text=True
    )


def test_check_reports_drift(tmp_path):
    template = tmp_path / "template.md"
    target = tmp_path / "target.md"
    template.write_text(TEMPLATE)
    target.write_text(DRIFTED)
    result = run("--template", str(template), "--section", "Rules",
                 "--check", str(target))
    assert result.returncode == 1
    assert "DRIFT" in result.stdout
    assert target.read_text() == DRIFTED


def test_sync_rewrites_only_the_section(tmp_path):
    template = tmp_path / "template.md"
    target = tmp_path / "target.md"
    template.write_text(TEMPLATE)
    target.write_text(DRIFTED)
    result = run("--template", str(template), "--section", "Rules",
                 str(target))
    assert result.returncode == 0
    assert target.read_text() == "# B\n\n## Rules\n\ncanonical body\n\n## Tail\n\nother tail\n"
    check = run("--template", str(template), "--section", "Rules",
                "--check", str(target))
    assert check.returncode == 0


def test_missing_section_is_drift(tmp_path):
    template = tmp_path / "template.md"
    target = tmp_path / "target.md"
    template.write_text(TEMPLATE)
    target.write_text(MISSING)
    result = run("--template", str(template), "--section", "Rules",
                 "--check", str(target))
    assert result.returncode == 1
    assert "MISSING" in result.stdout


def test_skip_missing(tmp_path):
    template = tmp_path / "template.md"
    target = tmp_path / "target.md"
    template.write_text(TEMPLATE)
    target.write_text(MISSING)
    result = run("--template", str(template), "--section", "Rules",
                 "--check", "--skip-missing", str(target))
    assert result.returncode == 0
    assert "skipped" in result.stdout
