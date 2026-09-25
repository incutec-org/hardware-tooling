import contextlib
import copy
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HARDWARE = Path(__file__).resolve().parents[1] / "hardware"
sys.path.insert(0, str(HARDWARE))

import onshape_api as api  # noqa: E402
import onshape_model_check as mc  # noqa: E402

DID, WID, BRANCH = "d" * 24, "w" * 24, "b" * 24
PS, NUTS, ASM, DRW, EXT = "e1" * 12, "e2" * 12, "e3" * 12, "e4" * 12, "x" * 24
OLD = "v" * 24

PARTS_HEADER = "part,type,material,thickness_mm,qty_per_set,process,sku,onshape_part\n"
HARDWARE_HEADER = "item,standard,size,qty_per_set,notes\n"


def registry(**extra):
    reg = {
        "source": "onshape",
        "document": {"id": DID, "name": "Frame", "owner": "Owner", "url": "u"},
        "workspace": {"id": WID, "name": '5"'},
        "elements": [
            {"id": PS, "name": "frame", "type": "PARTSTUDIO"},
            {"id": NUTS, "name": "pressnuts", "type": "PARTSTUDIO"},
            {"id": ASM, "name": "Frame assembly", "type": "ASSEMBLY"},
            {"id": DRW, "name": "Drawing 1", "type": "DRAWING"},
        ],
        "parts": [
            {"partStudio": PS, "partId": "JHD", "name": "Arm", "release": True},
            {"partStudio": PS, "partId": "J/D", "name": "Cam-Mount-L", "release": True},
            {"partStudio": PS, "partId": "RzD", "name": "Cam-Mount-R", "release": True},
            {"partStudio": NUTS, "partId": "JQD", "name": "m3 pressnut", "release": False},
        ],
    }
    reg.update(extra)
    return reg


def part(pid, name, material="Carbon"):
    return {"partId": pid, "name": name, "material": {"displayName": material} if material else None}


def inst(iid, name, element=PS, pid=None, version=None, std=False, doc=DID):
    return {"id": iid, "name": name, "type": "Part", "documentId": doc, "elementId": element,
            "partId": pid, "documentVersion": version, "isStandardContent": std, "suppressed": False}


def clean_assembly():
    instances = [
        inst("A1", "Arm <1>", pid="JHD"), inst("A2", "Arm <2>", pid="JHD"),
        inst("CL", "Cam-Mount-L <1>", pid="J/D"), inst("CR", "Cam-Mount-R <1>", pid="RzD"),
        inst("N1", "m3 pressnut <1>", element=NUTS, pid="JQD"),
        inst("S1", "Socket button head screw M3x0.5 x 8 <1>", element=EXT, pid="K", std=True, doc=EXT),
    ]
    return instances


def routes(wid=WID, instances=None, parts=None, elements=None):
    instances = clean_assembly() if instances is None else instances
    return {
        ("GET", f"/api/v10/documents/d/{DID}/w/{wid}/elements"): elements or [
            {"id": PS, "name": "frame", "elementType": "PARTSTUDIO", "dataType": "onshape/partstudio"},
            {"id": NUTS, "name": "pressnuts", "elementType": "PARTSTUDIO", "dataType": "onshape/partstudio"},
            {"id": ASM, "name": "Frame assembly", "elementType": "ASSEMBLY", "dataType": "onshape/assembly"},
            {"id": DRW, "name": "Drawing 1", "elementType": "APPLICATION", "dataType": "onshape-app/drawing"},
        ],
        ("GET", f"/api/v10/parts/d/{DID}/w/{wid}/e/{PS}"): parts or [
            part("JHD", "Arm"), part("J/D", "Cam-Mount-L", "PLA"), part("RzD", "Cam-Mount-R", "PLA")],
        ("GET", f"/api/v10/assemblies/d/{DID}/w/{wid}/e/{ASM}"): {
            "rootAssembly": {"documentId": DID, "instances": instances,
                             "occurrences": [{"path": [i["id"]]} for i in instances]},
            "subAssemblies": []},
        ("GET", f"/api/v10/documents/d/{DID}/versions"): [{"id": OLD, "name": "V4"}],
        ("GET", f"/api/v10/parts/d/{DID}/v/{OLD}/e/{PS}"): [part("REBH", "VTX-Mount", None)],
    }


class FakeTransport:
    base_url = "https://fake"

    def __init__(self, table):
        self.table = table
        self.calls = []

    def send(self, method, url, headers, body):
        path = url.split("https://fake", 1)[1].split("?", 1)[0]
        self.calls.append((method, path))
        for (m, pattern), answer in self.table.items():
            if m == method and re.fullmatch(re.escape(pattern), path):
                return 200, json.dumps(answer).encode()
        return 404, b'{"message": "no route"}'


def quiet(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


class ModelCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "Frame"
        (self.repo / "cad").mkdir(parents=True)
        self.write_registry(registry())
        (self.repo / "parts.csv").write_text(
            PARTS_HEADER + "Arm,arm,,,2,,,JHD\nCam-Mount-L,mount,,,1,,,J/D\nCam-Mount-R,mount,,,1,,,RzD\n")
        (self.repo / "hardware.csv").write_text(
            HARDWARE_HEADER + "m3 pressnut,,,1,\nSocket button head screw M3x0.5 x 8,ISO 7380,M3x8,1,\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write_registry(self, reg):
        (self.repo / "cad" / "onshape.json").write_text(json.dumps(reg))

    def run_check(self, table, *extra, repo=True):
        transport = FakeTransport(table)
        argv = [str(self.repo / "cad" / "onshape.json"), *extra]
        if repo:
            argv += ["--repo", str(self.repo)]
        code, out, err = quiet(mc.main, argv, client=api.Client(transport))
        return code, out, err, transport

    def findings(self, table, *extra):
        code, out, err, _ = self.run_check(table, "--json", *extra)
        self.assertIn(code, (0, 1), err)
        return {(f["check"], f["message"]) for f in json.loads(out)["findings"]}

    def test_clean_model_exits_zero_and_only_reads(self):
        code, out, err, transport = self.run_check(routes())
        self.assertEqual(code, 0, out + err)
        self.assertIn("| All | No findings |", out)
        self.assertTrue(all(method == "GET" for method, _ in transport.calls))

    def test_table_format_matches_readme(self):
        instances = [i for i in clean_assembly() if i["id"] != "CR"]
        code, out, _, _ = self.run_check(routes(instances=instances))
        self.assertEqual(code, 1)
        lines = out.strip().splitlines()
        self.assertEqual(lines[:2], ["| Check | Finding |", "|---|---|"])
        self.assertIn("| Unused parts | `Cam-Mount-R` is in the `frame` Part Studio but not in the assembly |", lines)
        self.assertIn("| Parts list | `Cam-Mount-R`: parts.csv 1, assembly 0 |", lines)

    def test_pinned_dangling_and_sourceless_instances(self):
        instances = clean_assembly() + [
            inst("V", "VTX-Mount <1>", pid="REBH", version=OLD),
            inst("G", "Ghost <1>", pid="GONE"),
            {"id": "B", "name": "Bumper <1>"},
        ]
        found = self.findings(routes(instances=instances))
        self.assertIn(("Missing parts", "`VTX-Mount <1>` points at part `REBH` in version `V4`, not at the "
                                        "workspace; the workspace `frame` Part Studio has no part of that name"), found)
        self.assertIn(("Missing parts", "`Ghost <1>` points at part id `GONE`, which is not in `frame`"), found)
        self.assertIn(("Missing parts", "`Bumper <1>` has no source part (deleted or not shared)"), found)
        self.assertIn(("Materials", "`VTX-Mount` has no material set in the model"), found)
        self.assertIn(("Parts list", "`VTX-Mount`: assembly 1, not in parts.csv"), found)

    def test_pinned_instance_names_the_workspace_part_of_the_same_name(self):
        parts = [part("JHD", "Arm"), part("J/D", "Cam-Mount-L", "PLA"), part("RzD", "Cam-Mount-R", "PLA"),
                 part("RDBH", "VTX-Mount", "PLA")]
        instances = clean_assembly() + [inst("V", "VTX-Mount <1>", pid="REBH", version=OLD)]
        found = self.findings(routes(instances=instances, parts=parts))
        self.assertIn(("Missing parts", "`VTX-Mount <1>` points at part `REBH` in version `V4`, not at the "
                                        "workspace; the workspace `frame` Part Studio has it as `RDBH`"), found)

    def test_materials_cover_released_and_used_parts_only(self):
        parts = [part("JHD", "Arm", None), part("J/D", "Cam-Mount-L", "PLA"), part("RzD", "Cam-Mount-R", None),
                 part("P19", "Part 19", None)]
        found = self.findings(routes(parts=parts))
        self.assertIn(("Materials", "`Arm` and `Cam-Mount-R` have no material set in the model"), found)
        self.assertIn(("Unused parts", "`Part 19` is in the `frame` Part Studio but not in the assembly"), found)

    def test_link_file_ids_names_and_drawing(self):
        reg = registry()
        reg["parts"][0]["name"] = "Old arm"
        reg["parts"].append({"partStudio": PS, "partId": "ZZZ", "name": "Gone", "release": True})
        reg["elements"] = [e for e in reg["elements"] if e["type"] != "DRAWING"]
        self.write_registry(reg)
        found = self.findings(routes())
        self.assertIn(("Link file", "part `Old arm` (JHD) is named `Arm` in the model"), found)
        self.assertIn(("Link file", "part `Gone` (ZZZ) is not in its Part Studio"), found)
        self.assertIn(("Drawing", "the link file lists no drawing"), found)

    def test_drawing_element_that_is_not_a_drawing(self):
        elements = routes()[("GET", f"/api/v10/documents/d/{DID}/w/{WID}/elements")]
        elements = copy.deepcopy(elements)
        elements[3]["dataType"] = "application/pdf"
        found = self.findings(routes(elements=elements))
        self.assertIn(("Drawing", "`Drawing 1` is a application/pdf, not a drawing"), found)

    def test_parts_csv_both_directions(self):
        (self.repo / "parts.csv").write_text(
            PARTS_HEADER + "Arm,arm,,,2,,,JHD\nCam-Mount-L,mount,,,1,,,J/D\nCamR,mount,,,1,,,RzD\n"
            "Pad,pad,,,1,,,\nOld,plate,,,1,,,QQQ\n")
        found = self.findings(routes())
        self.assertIn(("Parts list", "`CamR` in parts.csv is `Cam-Mount-R` in the Part Studio"), found)
        self.assertIn(("Parts list", "`Pad`: parts.csv 1, assembly 0"), found)
        self.assertIn(("Parts list", "`Old` (QQQ) is in parts.csv but not in the Part Studio"), found)

    def test_hardware_counts_and_ignore_list(self):
        instances = clean_assembly() + [
            inst("M5", "Prevailing torque nut M5x0.80 <1>", element=EXT, pid="K", std=True, doc=EXT),
            inst("S2", "Socket button head screw M3x0.5 x 8 <2>", element=EXT, pid="K", std=True, doc=EXT)]
        (self.repo / "hardware.csv").write_text(
            HARDWARE_HEADER + "m3 pressnut,,,1,\nSocket button head screw M3x0.5 x 8,,,1,\nFlat head screw,,,5,\n")
        found = self.findings(routes(instances=instances))
        self.assertIn(("Hardware", "`Socket button head screw M3x0.5 x 8`: hardware.csv 1, assembly 2"), found)
        self.assertIn(("Hardware", "`Flat head screw`: hardware.csv 5, assembly 0"), found)
        self.assertIn(("Hardware", "`Prevailing torque nut M5x0.80`: assembly 1, not in hardware.csv"), found)
        self.write_registry(registry(modelCheck={"ignoreHardware": ["Prevailing torque nut M5x0.80"]}))
        found = self.findings(routes(instances=instances))
        self.assertNotIn(("Hardware", "`Prevailing torque nut M5x0.80`: assembly 1, not in hardware.csv"), found)

    def test_hardware_inside_a_flattened_import_counts_by_part_name(self):
        instances = clean_assembly() + [
            inst(f"F{n}", f"Flat head screw:1__Body{n} <1>", element="i" * 24, pid=f"F{n}") for n in (1, 2)]
        (self.repo / "hardware.csv").write_text(
            HARDWARE_HEADER + "m3 pressnut,,,1,\nSocket button head screw M3x0.5 x 8,,,1,\nFlat head screw,,,2,\n")
        self.assertNotIn("Hardware", {check for check, _ in self.findings(routes(instances=instances))})

    def test_without_repo_skips_the_csv_checks(self):
        (self.repo / "hardware.csv").write_text(HARDWARE_HEADER + "Nothing,,,9,\n")
        code, out, err, _ = self.run_check(routes(), repo=False)
        self.assertEqual(code, 0, out + err)

    def test_workspace_override_reads_the_branch(self):
        code, out, err, transport = self.run_check(routes(wid=BRANCH), "--workspace", BRANCH)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(all(WID not in path for _, path in transport.calls))

    def test_suppressed_instances_are_ignored(self):
        instances = clean_assembly()
        instances[3]["suppressed"] = True
        found = self.findings(routes(instances=instances))
        self.assertIn(("Parts list", "`Cam-Mount-R`: parts.csv 1, assembly 0"), found)

    def test_errors_exit_two_and_unknown_arguments_are_refused(self):
        code, _, err, _ = self.run_check({}, "--json")
        self.assertEqual(code, 2)
        self.assertIn("HTTP 404", err)
        with self.assertRaises(SystemExit):
            quiet(mc.main, [str(self.repo / "cad" / "onshape.json"), "--apply"], client=api.Client(FakeTransport({})))

    def test_pipe_in_a_name_is_escaped(self):
        self.assertIn("A\\|b", mc.table([{"check": "Materials", "message": "a|b", "subject": None}]))


if __name__ == "__main__":
    unittest.main()
