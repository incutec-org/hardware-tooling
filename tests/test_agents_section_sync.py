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


def test_missing_section_prevents_all_writes(tmp_path):
    template = tmp_path / "template.md"
    drifted = tmp_path / "drifted.md"
    missing = tmp_path / "missing.md"
    template.write_text(TEMPLATE)
    drifted.write_text(DRIFTED)
    missing.write_text(MISSING)
    for targets in ((drifted, missing), (missing, drifted)):
        result = run("--template", str(template), "--section", "Rules",
                     *(str(target) for target in targets))
        assert result.returncode == 1
        assert "MISSING" in result.stdout
        assert "synced" not in result.stdout
        assert drifted.read_text() == DRIFTED
        assert missing.read_text() == MISSING
    check = run("--template", str(template), "--section", "Rules", "--check",
                str(drifted), str(missing))
    assert check.returncode == 1
    assert "MISSING" in check.stdout and "DRIFT" in check.stdout


def test_skip_missing_allows_other_sections_to_be_written(tmp_path):
    template = tmp_path / "template.md"
    drifted = tmp_path / "drifted.md"
    missing = tmp_path / "missing.md"
    template.write_text(TEMPLATE)
    drifted.write_text(DRIFTED)
    missing.write_text(MISSING)
    result = run("--template", str(template), "--section", "Rules",
                 "--skip-missing", str(drifted), str(missing))
    assert result.returncode == 0
    assert "canonical body" in drifted.read_text()
    assert missing.read_text() == MISSING


def test_unavailable_target_prevents_all_writes(tmp_path):
    template = tmp_path / "template.md"
    drifted = tmp_path / "drifted.md"
    template.write_text(TEMPLATE)
    drifted.write_text(DRIFTED)
    result = run("--template", str(template), "--section", "Rules",
                 str(drifted), str(tmp_path / "not-present.md"))
    assert result.returncode == 2
    assert drifted.read_text() == DRIFTED
