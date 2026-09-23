import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "docs" / "stale_check.py"
SPEC = importlib.util.spec_from_file_location("stale_check", MODULE_PATH)
stale_check = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["stale_check"] = stale_check
SPEC.loader.exec_module(stale_check)

WORKSPACE_TERMS = {
    "schema_version": 1,
    "description": "term list for the test workspace",
    "terms": [
        {
            "id": "retired_system",
            "type": "literal",
            "pattern": "OldTracker",
            "reason": "OldTracker is retired.",
        },
        {
            "id": "old_contact",
            "type": "literal",
            "pattern": "sales@old.example",
            "reason": "Outbound mail moved to another domain.",
        },
    ],
    "allowlist_globs": ["root/.incutec/stale_terms.json"],
}


def run_git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-q")
    run_git(path, "config", "user.email", "test@example.com")
    run_git(path, "config", "user.name", "Test")


def commit_all(path: Path, message="commit"):
    run_git(path, "add", "-A")
    run_git(path, "commit", "-q", "-m", message)


class StaleCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_workspace_terms(self, data=None):
        terms_path = self.workspace / ".incutec" / "stale_terms.json"
        terms_path.parent.mkdir(parents=True, exist_ok=True)
        terms_path.write_text(
            json.dumps(data if data is not None else WORKSPACE_TERMS), encoding="utf-8"
        )
        return terms_path

    def compiled_workspace_terms(self):
        return stale_check.compile_terms(WORKSPACE_TERMS["terms"])

    def build_workspace(self):
        init_repo(self.workspace)
        self.write_workspace_terms()
        (self.workspace / "repos.json").write_text(
            json.dumps(
                {
                    "repositories": [
                        {"path": ".", "url": "x", "branch": "main"},
                        {"path": "erp", "url": "x", "branch": "main"},
                        {"path": "not-cloned", "url": "x", "branch": "main"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.workspace / "AGENTS.md").write_text(
            "We used OldTracker for stock.\n", encoding="utf-8"
        )
        commit_all(self.workspace)

        erp = self.workspace / "erp"
        init_repo(erp)
        (erp / "PLAN.md").write_text("OldTracker is retired.\n", encoding="utf-8")
        (erp / "README.md").write_text(
            "Contact sales@old.example for now.\n", encoding="utf-8"
        )
        (erp / "notes.bin").write_bytes(b"\x00\x01binary, not scanned\n")
        commit_all(erp)

    def test_allowlisted_path_is_excluded_from_hits(self):
        self.build_workspace()
        hits = stale_check.scan_repository(
            "erp", self.workspace / "erp", self.compiled_workspace_terms(), ["erp/PLAN.md"]
        )
        plan_hits = [h for h in hits if h.path == "PLAN.md"]
        readme_hits = [h for h in hits if h.path == "README.md"]

        self.assertTrue(plan_hits)
        self.assertTrue(all(h.allowlisted for h in plan_hits))
        self.assertTrue(readme_hits)
        self.assertTrue(all(not h.allowlisted for h in readme_hits))

    def test_binary_and_untracked_extensions_are_skipped(self):
        self.build_workspace()
        hits = stale_check.scan_repository(
            "erp", self.workspace / "erp", self.compiled_workspace_terms(), []
        )
        self.assertFalse(any(h.path == "notes.bin" for h in hits))

    def test_uncloned_repository_is_skipped_not_errored(self):
        self.build_workspace()
        scanned, skipped = stale_check.resolve_repositories(self.workspace, None)
        names = {name for name, _ in scanned}
        self.assertIn("root", names)
        self.assertIn("erp", names)
        self.assertIn("not-cloned", skipped)

    def test_repo_filter_limits_scan(self):
        self.build_workspace()
        scanned, _ = stale_check.resolve_repositories(self.workspace, {"erp"})
        self.assertEqual([name for name, _ in scanned], ["erp"])

    def test_explicit_unknown_or_unavailable_repositories_fail_before_scanning(self):
        self.build_workspace()
        for selection in ("typo", "not-cloned"):
            with self.subTest(selection=selection):
                result = subprocess.run(
                    [sys.executable, str(MODULE_PATH), "--workspace-root", str(self.workspace),
                     "--repo", "erp", "--repo", selection, "--json"],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(selection, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_directory_inside_parent_checkout_is_not_a_repository(self):
        self.build_workspace()
        (self.workspace / "not-cloned").mkdir()
        scanned, skipped = stale_check.resolve_repositories(self.workspace, None)
        self.assertIn("not-cloned", skipped)
        self.assertNotIn("not-cloned", [name for name, _ in scanned])
        with self.assertRaisesRegex(ValueError, "not available Git checkouts"):
            stale_check.resolve_repositories(self.workspace, {"not-cloned"})

    def test_example_terms_file_is_loadable(self):
        terms, allowlist = stale_check.load_terms(stale_check.EXAMPLE_TERMS_PATH)
        self.assertTrue(terms)
        self.assertTrue(allowlist)
        stale_check.compile_terms(terms)

    def test_terms_resolution_prefers_explicit_then_workspace_then_example(self):
        init_repo(self.workspace)
        elsewhere = self.workspace / "elsewhere.json"

        self.assertEqual(
            stale_check.resolve_terms_path(elsewhere, self.workspace), elsewhere
        )
        self.assertEqual(
            stale_check.resolve_terms_path(None, self.workspace),
            stale_check.EXAMPLE_TERMS_PATH,
        )
        workspace_terms = self.write_workspace_terms()
        self.assertEqual(
            stale_check.resolve_terms_path(None, self.workspace), workspace_terms
        )

    def test_cli_json_output_is_valid_and_exit_code_reflects_hits(self):
        self.build_workspace()
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            Path(payload["terms_file"]),
            (self.workspace / ".incutec" / "stale_terms.json").resolve(),
        )
        self.assertGreater(payload["total_hits"], 0)
        self.assertIn("root", payload["repos_scanned"])
        self.assertIn("erp", payload["repos_scanned"])
        self.assertIn("not-cloned", payload["repos_skipped"])

    def test_missing_explicit_terms_file_is_an_error(self):
        self.build_workspace()
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--workspace-root",
                str(self.workspace),
                "--terms",
                str(self.workspace / "no-such-terms.json"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("term list not found", result.stderr)

    def test_clean_workspace_exits_zero(self):
        init_repo(self.workspace)
        self.write_workspace_terms()
        (self.workspace / "repos.json").write_text(
            json.dumps({"repositories": [{"path": ".", "url": "x", "branch": "main"}]}),
            encoding="utf-8",
        )
        (self.workspace / "README.md").write_text("Nothing stale here.\n", encoding="utf-8")
        commit_all(self.workspace)

        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--workspace-root", str(self.workspace), "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["total_hits"], 0)


if __name__ == "__main__":
    unittest.main()
