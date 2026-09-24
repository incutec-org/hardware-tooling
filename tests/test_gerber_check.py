import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "hardware" / "kicad" / "gerber_check.py"
sys.path.insert(0, str(TOOL.parent))

import gerber_check  # noqa: E402

HEAD = "%FSLAX46Y46*%\n%MOMM*%\n%TF.FileFunction,{}*%\n%ADD10C,0.100000*%\n"


def gerber(function, body=""):
    return HEAD.format(function) + body + "M02*\n"


def outline(w, h):
    pts = [(0, 0), (w, 0), (w, h), (0, h), (0, 0)]
    body = "D10*\n" + "".join(
        f"X{int(x * 1e6)}Y{int(y * 1e6)}{'D02' if i == 0 else 'D01'}*\n"
        for i, (x, y) in enumerate(pts))
    return gerber("Profile,NP", body)


def drill(holes, w, h):
    body = "M48\nMETRIC\nT1C0.200\n%\nT1\n"
    for i in range(holes):
        body += f"X{1 + (i % 10) * (w - 2) / 10:.3f}Y{1 + (i // 10) * 0.05:.3f}\n"
    return body + "M30\n"


def make_zip(path, holes, w=10.0, h=10.0):
    files = {
        "b-F_Cu.gtl": gerber("Copper,L1,Top"), "b-B_Cu.gbl": gerber("Copper,L2,Bot"),
        "b-F_Mask.gts": gerber("Soldermask,Top"), "b-B_Mask.gbs": gerber("Soldermask,Bot"),
        "b-F_Paste.gtp": gerber("Paste,Top"), "b-B_Paste.gbp": gerber("Paste,Bot"),
        "b-F_Silkscreen.gto": gerber("Legend,Top"), "b-B_Silkscreen.gbo": gerber("Legend,Bot"),
        "b-Edge_Cuts.gm1": outline(w, h), "b-PTH.drl": drill(holes, w, h),
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)


def run(path, *extra):
    return subprocess.run([sys.executable, str(TOOL), str(path), *extra],
                          capture_output=True, text=True)


class HoleDensity(unittest.TestCase):
    def test_density_is_holes_per_square_metre_of_the_bbox(self):
        self.assertAlmostEqual(gerber_check.hole_density(90, (0, 0, 10, 10)), 900000)
        self.assertIsNone(gerber_check.hole_density(5, None))

    def test_dense_board_warns_but_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.zip"
            make_zip(path, holes=110)  # 1.10M/m2 on 10 x 10 mm
            r = run(path)
            self.assertEqual(r.returncode, 0, r.stdout)
            line = next(l for l in r.stdout.splitlines() if "hole density" in l)
            self.assertTrue(line.startswith("G3 WARN"), line)
            self.assertIn("1.10M/m2", line)
            self.assertIn("about 20 holes over", line)
            self.assertIn("== PASS", r.stdout)

    def test_sparse_board_passes_and_zero_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.zip"
            make_zip(path, holes=40)
            self.assertIn("G3 PASS hole density 0.40M/m2", run(path).stdout)
            self.assertNotIn("hole density", run(path, "--max-hole-density", "0").stdout)


if __name__ == "__main__":
    unittest.main()
