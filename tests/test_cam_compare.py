import math
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hardware" / "kicad"))

try:
    import cam_compare as cc  # noqa: E402  (needs numpy, scipy, pillow, gerbonara)
except ImportError as exc:  # pragma: no cover - reported as a skip
    cc = None
    SKIP = str(exc)

# Design: 20 x 20 mm board, five plated holes in a non-symmetric pattern.
# Top copper joins h1-h2 and h3-h4, bottom copper joins h2-h4, h5 stands alone:
# two nets, {h1, h2, h3, h4} and {h5}.
HOLES = [(3.0, 3.0), (15.0, 4.0), (6.0, 14.0), (17.0, 17.0), (10.0, 9.0)]
TOP_TRACES = [(0, 1), (2, 3)]
BOT_TRACES = [(1, 3)]


def mm_gerber(body):
    return "%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,1.000000*%\n%ADD11C,0.300000*%\n%ADD12C,0.100000*%\n" + body + "M02*\n"


def n6(v):
    return str(int(round(v * 1e6)))


def copper_mm(traces, holes=HOLES, extra=""):
    out = ["D10*"] + [f"X{n6(x)}Y{n6(y)}D03*" for x, y in holes] + ["D11*"]
    for a, b in traces:
        (x1, y1), (x2, y2) = holes[a], holes[b]
        out += [f"X{n6(x1)}Y{n6(y1)}D02*", f"X{n6(x2)}Y{n6(y2)}D01*"]
    return mm_gerber("\n".join(out) + "\n" + extra)


def outline_mm():
    pts = [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]
    body = "D12*\n" + f"X{n6(pts[0][0])}Y{n6(pts[0][1])}D02*\n" + "".join(f"X{n6(x)}Y{n6(y)}D01*\n" for x, y in pts[1:])
    return mm_gerber(body)


def excellon(holes=HOLES):
    return "M48\nMETRIC,TZ\nT1C0.300\n%\nG90\nG05\nT1\n" + "".join(f"X{x:.3f}Y{y:.3f}\n" for x, y in holes) + "M30\n"


def design_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("t-F_Cu.gtl", copper_mm(TOP_TRACES))
        z.writestr("t-B_Cu.gbl", copper_mm(BOT_TRACES))
        z.writestr("t-Edge_Cuts.gm1", outline_mm())
        z.writestr("t-PTH.drl", excellon())


# CAM: PCBGogo/Genesis style. Inch, extensionless names, the board stepped 2 x 1 with %SR%,
# drills written as Gerber flashes, one step-and-repeat block left open until M02.
PANEL = (5.0, 5.0)
PITCH = 25.0


def inch_gerber(body, close_sr=True):
    head = "%FSLAX26Y26*%\n%MOIN*%\n%ADD10C,0.039370*%\n%ADD11C,0.011811*%\n%ADD12C,0.003937*%\n%ADD13C,0.011811*%\n"
    sr = f"%SRX2Y1I{PITCH / 25.4:.6f}J0*%\n"
    return head + sr + body + ("%SR*%\n" if close_sr else "") + "M02*\n"


def n_in(v):
    return str(int(round(v / 25.4 * 1e6)))


def cam_copper(traces, holes=HOLES, extra=""):
    ox, oy = PANEL
    out = ["G54D10*"] + [f"X{n_in(x + ox)}Y{n_in(y + oy)}D03*" for x, y in holes] + ["G54D11*"]
    for a, b in traces:
        (x1, y1), (x2, y2) = holes[a], holes[b]
        out += [f"X{n_in(x1 + ox)}Y{n_in(y1 + oy)}D02*", f"X{n_in(x2 + ox)}Y{n_in(y2 + oy)}D01*"]
    return "\n".join(out) + "\n" + extra


def cam_dir(root, top=TOP_TRACES, bottom=BOT_TRACES, holes=HOLES, top_extra=""):
    ox, oy = PANEL
    root.mkdir(parents=True, exist_ok=True)
    (root / "gtl").write_text(inch_gerber(cam_copper(top, extra=top_extra), close_sr=False))
    (root / "gbl").write_text(inch_gerber(cam_copper(bottom)))
    pts = [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]
    body = "G54D12*\n" + f"X{n_in(ox)}Y{n_in(oy)}D02*\n" + "".join(f"X{n_in(x + ox)}Y{n_in(y + oy)}D01*\n" for x, y in pts[1:])
    (root / "gko").write_text(inch_gerber(body))
    (root / "drl").write_text(inch_gerber("G54D13*\n" + "".join(f"X{n_in(x + ox)}Y{n_in(y + oy)}D03*\n" for x, y in holes)))


def lines(R, code):
    return [line for line in R.lines if line.startswith(code)]


@unittest.skipIf(cc is None, "cam_compare dependencies missing")
class CamCompareTests(unittest.TestCase):
    def run_pair(self, **cam_kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            design_zip(tmp / "design.zip")
            cam_dir(tmp / "cam", **cam_kwargs)
            return cc.compare(str(tmp / "design.zip"), str(tmp / "cam"), res=0.02)

    def test_identical_panel_passes(self):
        R, V = self.run_pair()
        self.assertEqual(R.fails, 0, "\n".join(R.lines))
        self.assertIn("2 board instance(s) in CAM, rotation 0 deg, not mirrored; 2 columns x 1 rows", "\n".join(R.lines))
        self.assertTrue(any("pitch x [25.0]" in line for line in R.lines))
        self.assertTrue(all("PASS" in line for line in lines(R, "C7")))
        self.assertIn("C6 PASS   opens", "\n".join(R.lines))

    def test_cut_trace_is_an_open_that_cuts_off_a_hole(self):
        R, V = self.run_pair(top=[(2, 3)])  # h1-h2 missing on top copper
        opens = [f for f in R.findings if f["check"] == "C6" and f["title"].startswith("open")]
        self.assertEqual(len(opens), 1, "\n".join(R.lines))
        self.assertEqual(opens[0]["status"], "FAIL")
        self.assertGreaterEqual(opens[0]["holes"], 1)
        self.assertEqual(opens[0]["layer"], "cu L1")
        self.assertGreater(R.fails, 0)

    def test_bridge_is_a_short(self):
        h5, h3 = HOLES[4], HOLES[2]
        ox, oy = PANEL
        bridge = f"G54D11*\nX{n_in(h5[0] + ox)}Y{n_in(h5[1] + oy)}D02*\nX{n_in(h3[0] + ox)}Y{n_in(h3[1] + oy)}D01*\n"
        R, V = self.run_pair(top_extra=bridge)
        shorts = [f for f in R.findings if f["check"] == "C6" and f["title"].startswith("short")]
        self.assertEqual(len(shorts), 1, "\n".join(R.lines))
        self.assertEqual(shorts[0]["status"], "FAIL")

    def test_moved_hole_is_reported_with_its_offset(self):
        moved = list(HOLES)
        moved[4] = (HOLES[4][0] + 0.1, HOLES[4][1])
        R, V = self.run_pair(holes=moved)
        found = [f for f in R.findings if f["check"] == "C3" and "moved" in f["title"]]
        self.assertEqual(len(found), 1, "\n".join(R.lines))
        self.assertIn("dx +0.100", found[0]["detail"])
        self.assertTrue(any(line.startswith("C3 REVIEW design holes moved") for line in R.lines))

    def test_workspace_embeds_the_analysis(self):
        import json, re
        R, V = self.run_pair(top=[(2, 3)])
        with tempfile.TemporaryDirectory() as out:
            code = cc.write_outputs(R, V, out)
            page = (Path(out) / "index.html").read_text()
            self.assertEqual(code, 1)
            self.assertNotIn("/*__DATA__*/null", page)
            data = json.loads(re.search(r"const DATA = (\{.*?\});\n", page, re.S).group(1))
            self.assertEqual({f["id"] for f in data["findings"]}, {f["id"] for f in R.findings})
            self.assertEqual(len(data["view"]["layers"]), 2)
            self.assertTrue((Path(out) / "findings.json").exists() and (Path(out) / "report.txt").exists())

    def test_finding_ids_are_stable_across_runs(self):
        a = [f["id"] for f in self.run_pair(top=[(2, 3)])[0].findings]
        b = [f["id"] for f in self.run_pair(top=[(2, 3)])[0].findings]
        self.assertEqual(a, b)


@unittest.skipIf(cc is None, "cam_compare dependencies missing")
class GerbonaraCorrectionTests(unittest.TestCase):
    def test_primitive_22_becomes_equivalent_21(self):
        out = cc.normalize_gerber("%AMX*\n22,1,0.2,0.1,-0.1,-0.05,0*\n%")
        self.assertIn("21,1,0.20000000,0.10000000,0.00000000,0.00000000,0*", out)

    def test_rotated_rectangle_keeps_its_area(self):
        from gerbonara import graphic_primitives as gp
        poly = gp.Rectangle(0, 0, 2.0, 0.5, math.radians(45)).to_arc_poly().outline
        area = 0.5 * abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1])))
        self.assertAlmostEqual(area, 1.0, places=6)

    def test_step_repeat_expands_in_file_units(self):
        text = inch_gerber("G54D10*\nX0Y0D03*\n")
        L = cc.Layer("x.gtl", text.encode())
        xs = sorted(round(o.unit.convert_to(cc.MM, o.x), 3) for o in L.file.objects)
        self.assertEqual(xs, [0.0, PITCH])

    def test_open_step_repeat_is_closed_by_m02(self):
        L = cc.Layer("x.gtl", inch_gerber("G54D10*\nX0Y0D03*\n", close_sr=False).encode())
        self.assertEqual(len(L.file.objects), 2)

    def test_rectangle_aperture_draw_is_swept(self):
        body = "%ADD20R,1.000000X1.000000*%\nD20*\nX0Y0D02*\nX3000000Y0D01*\n"
        L = cc.Layer("x.gtl", mm_gerber(body).encode())
        grid = cc.Grid(-2, -2, 5, 2, 0.01)
        area = cc.rasterize(L, grid).sum() * 0.01 ** 2
        self.assertAlmostEqual(area, 4.0, delta=0.1)  # 1 mm square swept 3 mm, not a round-capped stroke


@unittest.skipIf(cc is None, "cam_compare dependencies missing")
class ClassifyTests(unittest.TestCase):
    def test_names(self):
        cases = {
            "gtl": ("cu", 1), "l2": ("cu", 2), "l5": ("cu", 5), "tl": ("cu", 1), "bl": ("cu", 99),
            "a89888axy7.gbl": ("cu", 99), "board-In1_Cu.g1": ("cu", 2), "board-F_Cu.gtl": ("cu", 1),
            "gko": ("outline", 0), "ko": ("outline", 0), "board-Edge_Cuts.gm1": ("outline", 0),
            "drl": ("drill", 0), "board-PTH.drl": ("drill", 0),
        }
        for name, (role, index) in cases.items():
            r, side, idx, basis = cc.classify(name, "", False)
            self.assertEqual((r, idx if r == "cu" else 0), (role, index), name)
        self.assertEqual(cc.classify("sk", "", False)[0], "aux")
        self.assertEqual(cc.classify("2rl", "", False)[0], "aux")
        self.assertEqual(cc.classify("board-PTH-drl_map.gbr", "", False)[0], "map")


if __name__ == "__main__":
    unittest.main()
