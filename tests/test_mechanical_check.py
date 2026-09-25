"""Tests for hardware/mechanical_check.py against the mechanical template."""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hardware"))

import mechanical_check  # noqa: E402

TEMPLATE = ROOT / "templates" / "mechanical-repository"


class MechanicalCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        shutil.copytree(TEMPLATE, self.repo)
        (self.repo / "LICENSE").write_text("CERN-OHL-S-2.0\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_template_passes_with_only_warnings(self):
        errors, warnings = mechanical_check.check(self.repo)
        self.assertEqual(errors, [])
        self.assertTrue(any("no rows yet" in w for w in warnings))

    def test_kicad_file_is_an_error(self):
        (self.repo / "board.kicad_pcb").write_text("")
        errors, _ = mechanical_check.check(self.repo)
        self.assertTrue(any("KiCad file" in e for e in errors))

    def test_bad_quantity_is_an_error(self):
        with open(self.repo / "parts.csv", "a") as fh:
            fh.write("Arm,plate,T700 carbon,6,four,CNC,ACC-FRM-ARM-5,\n")
        errors, _ = mechanical_check.check(self.repo)
        self.assertTrue(any("qty_per_set" in e for e in errors))

    def test_release_hash_mismatch_is_an_error(self):
        rev = self.repo / "releases" / "v1.0" / "step"
        rev.mkdir(parents=True)
        (rev / "arm.step").write_bytes(b"geometry")
        good = hashlib.sha256(b"geometry").hexdigest()
        manifest = {"files": [{"path": "step/arm.step", "sha256": good}]}
        (rev.parent / "manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(mechanical_check.check(self.repo)[0], [])
        (rev / "arm.step").write_bytes(b"edited")
        errors, _ = mechanical_check.check(self.repo)
        self.assertTrue(any("SHA-256 differs" in e for e in errors))

    def test_unlisted_release_file_is_a_warning(self):
        rev = self.repo / "releases" / "v1.0"
        rev.mkdir(parents=True)
        (rev / "manifest.json").write_text(json.dumps({"files": []}))
        (rev / "extra.pdf").write_bytes(b"x")
        errors, warnings = mechanical_check.check(self.repo)
        self.assertEqual(errors, [])
        self.assertTrue(any("not in the manifest" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
