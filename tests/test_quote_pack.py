import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hardware" / "kicad"))

import quote_pack  # noqa: E402

ROWS = [{"Designator": "C1,C2", "Value": "100nF", "Footprint": "C_0402", "Quantity": "2",
         "LCSC": "C1525", "Manufacturer": "Samsung", "MPN": "CL05B104KO5NNNC"}]


class JlcpcbBom(unittest.TestCase):
    def test_fabrication_toolkit_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.csv"
            quote_pack.write_jlcpcb(ROWS, path)
            rows = list(csv.reader(path.open()))
        self.assertEqual(rows[0], ["Designator", "Footprint", "Quantity", "Value", "LCSC Part #"])
        self.assertEqual(rows[1], ["C1,C2", "C_0402", "2", "100nF", "C1525"])


if __name__ == "__main__":
    unittest.main()
