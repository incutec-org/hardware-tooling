import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "hardware" / "kicad" / "assembly_pack.py"
SPEC = importlib.util.spec_from_file_location("assembly_pack", MODULE_PATH)
assembly_pack = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["assembly_pack"] = assembly_pack
SPEC.loader.exec_module(assembly_pack)


class FindIbomTest(unittest.TestCase):
    """The plugin is installed per KiCad version and calls the pcbnew API, so
    picking the wrong major version fails on an API it does not know."""

    def _tree(self, root, versions):
        for version in versions:
            plugin = (Path(root) / "Documents" / "KiCad" / version / "3rdparty"
                      / "plugins" / "org_openscopeproject_InteractiveHtmlBom")
            plugin.mkdir(parents=True)
            (plugin / "generate_interactive_bom.py").write_text("")

    def test_ten_beats_nine(self):
        with tempfile.TemporaryDirectory() as home:
            self._tree(home, ["9.0", "10.0"])
            original = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                found = assembly_pack.find_ibom()
            finally:
                if original is not None:
                    os.environ["HOME"] = original
            self.assertIsNotNone(found)
            self.assertIn("/10.0/", found, "lexical sort would pick 9.0")

    def test_override_wins(self):
        with tempfile.NamedTemporaryFile(suffix=".py") as handle:
            os.environ["INCUTEC_IBOM"] = handle.name
            try:
                self.assertEqual(assembly_pack.find_ibom(), handle.name)
            finally:
                del os.environ["INCUTEC_IBOM"]

    def test_override_must_exist(self):
        os.environ["INCUTEC_IBOM"] = "/nonexistent/generate_interactive_bom.py"
        try:
            self.assertIsNone(assembly_pack.find_ibom())
        finally:
            del os.environ["INCUTEC_IBOM"]


class FindSchematicTest(unittest.TestCase):
    def test_prefers_matching_name(self):
        with tempfile.TemporaryDirectory() as work:
            board = Path(work) / "board.kicad_pcb"
            board.write_text("")
            (Path(work) / "board.kicad_sch").write_text("")
            (Path(work) / "other.kicad_sch").write_text("")
            self.assertTrue(
                assembly_pack.find_schematic(str(board)).endswith("board.kicad_sch"))

    def test_ambiguous_returns_none(self):
        with tempfile.TemporaryDirectory() as work:
            board = Path(work) / "board.kicad_pcb"
            board.write_text("")
            (Path(work) / "a.kicad_sch").write_text("")
            (Path(work) / "b.kicad_sch").write_text("")
            self.assertIsNone(assembly_pack.find_schematic(str(board)))


class FormatReportTest(unittest.TestCase):
    def test_clean_board_passes(self):
        lines = assembly_pack.format_report({
            "duplicates": {}, "unsourced": [], "placements": 164,
            "unique": 164, "symbols": 166})
        self.assertTrue(lines[0].startswith("A1 PASS"))
        self.assertTrue(lines[1].startswith("A2 PASS"))

    def test_duplicates_and_unsourced_warn(self):
        lines = assembly_pack.format_report({
            "duplicates": {"CL50": 2, "CL51": 2}, "unsourced": ["CL50", "CL51"],
            "placements": 191, "unique": 189, "symbols": 170})
        joined = "\n".join(lines)
        self.assertIn("A1 WARN", joined)
        self.assertIn("CL50 x2", joined)
        self.assertIn("A2 WARN 2 placements have no schematic symbol", joined)

    def test_missing_schematic_is_reported_not_passed(self):
        lines = assembly_pack.format_report({
            "duplicates": {}, "unsourced": [], "placements": 10,
            "unique": 10, "symbols": None})
        self.assertIn("A2 WARN schematic not read", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
