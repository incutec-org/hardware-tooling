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

    def build_workspace(self):
        init_repo(self.workspace)
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
            "We used Shopify for checkout.\n", encoding="utf-8"
        )
        commit_all(self.workspace)

        erp = self.workspace / "erp"
        init_repo(erp)
        (erp / "PLAN.md").write_text(
            "Shopify and InvenTree are retired.\n", encoding="utf-8"
        )
        (erp / "README.md").write_text(
            "Contact sales@incutec.eu for now.\n", encoding="utf-8"
        )
        (erp / "notes.bin").write_bytes(b"\x00\x01binary, not scanned\n")
        commit_all(erp)

    def test_allowlisted_path_is_excluded_from_hits(self):
        self.build_workspace()
        terms, allowlist = stale_check.load_terms(stale_check.DEFAULT_TERMS_PATH)
        compiled = stale_check.compile_terms(terms)
        hits = stale_check.scan_repository(
            "erp", self.workspace / "erp", compiled, ["erp/PLAN.md"]
        )
        plan_hits = [h for h in hits if h.path == "PLAN.md"]
        readme_hits = [h for h in hits if h.path == "README.md"]

        self.assertTrue(plan_hits)
        self.assertTrue(all(h.allowlisted for h in plan_hits))
        self.assertTrue(readme_hits)
        self.assertTrue(all(not h.allowlisted for h in readme_hits))

    def test_binary_and_untracked_extensions_are_skipped(self):
        self.build_workspace()
        terms, _ = stale_check.load_terms(stale_check.DEFAULT_TERMS_PATH)
        compiled = stale_check.compile_terms(terms)
        hits = stale_check.scan_repository(
            "erp", self.workspace / "erp", compiled, []
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
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertGreater(payload["total_hits"], 0)
        self.assertIn("root", payload["repos_scanned"])
        self.assertIn("erp", payload["repos_scanned"])
        self.assertIn("not-cloned", payload["repos_skipped"])

    def test_clean_workspace_exits_zero(self):
        init_repo(self.workspace)
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
