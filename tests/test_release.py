import collections
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "hardware" / "release" / "kicad_release.py"
SPEC = importlib.util.spec_from_file_location("kicad_release", MODULE)
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(release)


class ApprovedViolationTests(unittest.TestCase):
    def test_approved_errors_do_not_block(self):
        actual = collections.Counter({"pin_not_driven:error": 24})
        self.assertEqual(
            [],
            release.compare_violations(
                "erc", actual, {"pin_not_driven:error": 24}
            ),
        )

    def test_lower_count_does_not_block(self):
        actual = collections.Counter({"pin_not_driven:error": 20})
        self.assertEqual(
            [],
            release.compare_violations(
                "erc", actual, {"pin_not_driven:error": 24}
            ),
        )

    def test_new_finding_blocks(self):
        actual = collections.Counter({"new_problem:error": 1})
        self.assertEqual(
            ["drc new_problem:error: 1, no approval"],
            release.compare_violations("drc", actual, {}),
        )

    def test_count_above_approval_blocks(self):
        actual = collections.Counter({"clearance:error": 2})
        self.assertEqual(
            ["drc clearance:error: 2 > approved maximum 1"],
            release.compare_violations("drc", actual, {"clearance:error": 1}),
        )

    def test_loads_portfolio_approval_file(self):
        data = {
            "schema_version": 1,
            "boards": {"project/board": {"erc": {"pin:error": 3}, "drc": {}}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approved.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            approved = release.load_approved(path, "project/board")
        self.assertEqual(approved["erc"], {"pin:error": 3})


if __name__ == "__main__":
    unittest.main()
