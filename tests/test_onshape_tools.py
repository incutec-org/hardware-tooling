import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HARDWARE = Path(__file__).resolve().parents[1] / "hardware"
sys.path.insert(0, str(HARDWARE))


import mechanical_check  # noqa: E402
import onshape_api as api  # noqa: E402
import onshape_fit as fit  # noqa: E402
import onshape_release as release  # noqa: E402

DID, WID, PS, DRW, ASM = "d" * 24, "w" * 24, "e1" * 12, "e2" * 12, "e3" * 12


def registry():
    return {
        "source": "onshape",
        "document": {"id": DID, "name": "Frame", "owner": "Owner", "url": "u"},
        "workspace": {"id": WID, "name": '5"'},
        "elements": [
            {"id": PS, "name": "frame", "type": "PARTSTUDIO"},
            {"id": ASM, "name": "Frame assembly", "type": "ASSEMBLY"},
            {"id": DRW, "name": "Drawing 1", "type": "DRAWING"},
        ],
        "parts": [
            {"partStudio": PS, "partId": "JHD", "name": "Arm", "release": True},
            {"partStudio": PS, "partId": "J/D", "name": "Airtag/Antenna mount", "release": True},
            {"partStudio": PS, "partId": "RhD", "name": "Part 19", "release": False},
        ],
    }


class FakeTransport:
    """Answers requests from a route table and records them."""

    base_url = "https://fake"

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def send(self, method, url, headers, body):
        path = url.split("https://fake", 1)[1].split("?", 1)[0]
        self.calls.append((method, path, body))
        for (m, pattern), answer in self.routes.items():
            if m == method and re.fullmatch(pattern, path):
                result = answer(body) if callable(answer) else answer
                if isinstance(result, tuple):
                    return result
                if isinstance(result, bytes):
                    return 200, result
                return 200, json.dumps(result).encode()
        return 404, b'{"message": "no route"}'

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


OLD = "o" * 24


def asm_instance(pid, version=None, element=PS):
    return {"id": f"I{pid}", "name": f"{pid} <1>", "type": "Part", "documentId": DID, "elementId": element,
            "partId": pid, "documentVersion": version, "documentMicroversion": "q" * 24 if version else "m" * 24}


def release_routes(versions=None, fail_pdf=False, instances=None, wid=WID):
    counter = {"n": 0}

    def start(body):
        counter["n"] += 1
        return {"id": f"t{counter['n']}", "requestState": "ACTIVE"}

    return {
        ("GET", f"/api/v10/documents/{DID}"): {"name": "Frame", "owner": {"name": "Owner"}},
        ("GET", f"/api/v10/assemblies/d/{DID}/w/{wid}/e/{ASM}"): {
            "rootAssembly": {"instances": instances if instances is not None else
                             [asm_instance("JHD"), asm_instance("J/D")]}, "subAssemblies": []},
        ("GET", f"/api/v10/documents/d/{DID}/w/{wid}/elements"): [
            {"id": PS, "name": "frame", "elementType": "PARTSTUDIO"},
            {"id": ASM, "name": "Frame assembly", "elementType": "ASSEMBLY"},
            {"id": DRW, "name": "Drawing 1", "elementType": "APPLICATION"},
        ],
        ("GET", f"/api/v10/parts/d/{DID}/w/{wid}/e/{PS}"): [
            {"partId": "JHD"}, {"partId": "J/D"}, {"partId": "RhD"}],
        ("GET", f"/api/v10/parts/d/{DID}/v/{OLD}/e/{PS}"): [{"partId": "REBH"}],
        ("POST", f"/api/v10/partstudios/d/{DID}/v/{OLD}/e/{PS}/translations"): start,
        ("GET", f"/api/v10/documents/d/{DID}/versions"): versions or [],
        ("POST", f"/api/v10/documents/d/{DID}/versions"): {"id": "v" * 24, "microversion": "m" * 24},
        ("POST", f"/api/v10/partstudios/d/{DID}/v/{'v' * 24}/e/{PS}/translations"): start,
        ("POST", f"/api/v10/drawings/d/{DID}/v/{'v' * 24}/e/{DRW}/translations"):
            (500, b"boom") if fail_pdf else start,
        ("GET", r"/api/v10/translations/(t\d+)"): lambda body: {
            "requestState": "DONE", "resultExternalDataIds": ["f1"]},
        ("GET", f"/api/v10/documents/d/{DID}/externaldata/f1"): b"ISO-10303-21;data",
    }


def quiet(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "OpenFrame-5F"
        (self.repo / "cad").mkdir(parents=True)
        self.link = self.repo / "cad" / "onshape.json"
        self.link.write_text(json.dumps(registry()), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()


class KeyLookupTests(unittest.TestCase):
    def test_order_environment_then_repo_env_then_user_file(self):
        with tempfile.TemporaryDirectory() as d:
            repo, user = Path(d) / "repo", Path(d) / "credentials.env"
            repo.mkdir()
            (repo / ".env").write_text("ONSHAPE_SECRET_KEY=repo-secret\n")
            user.write_text("ONSHAPE_ACCESS_KEY=user-access\nONSHAPE_SECRET_KEY=user-secret\n")
            access, secret, sources = api.load_keys({}, repo, user)
            self.assertEqual((access, secret), ("user-access", "repo-secret"))
            access, _, sources = api.load_keys({"ONSHAPE_ACCESS_KEY": "env"}, repo, user)
            self.assertEqual(access, "env")
            self.assertEqual(sources["ONSHAPE_ACCESS_KEY"], "environment")

    def test_missing_names_the_variables_not_values(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(api.OnshapeError) as ctx:
                api.load_keys({}, Path(d), Path(d) / "absent.env")
        self.assertIn("ONSHAPE_ACCESS_KEY", str(ctx.exception))


class EmptyWriteResponseTests(unittest.TestCase):
    """Onshape answers some writes (assembly instance insert, transform) with a
    2xx and an empty body. The client must report success, never "nothing was
    done" - a caller that saw an error there could retry and duplicate the write."""

    def test_empty_200_body_on_post_is_reported_as_success(self):
        transport = FakeTransport({("POST", "/api/v10/thing"): (200, b"")})
        client = api.Client(transport)
        result = client.post("/thing", {"x": 1})
        self.assertEqual(result, {"status": 200})

    def test_204_no_content_on_post_is_reported_as_success(self):
        transport = FakeTransport({("POST", "/api/v10/thing"): (204, b"")})
        client = api.Client(transport)
        result = client.post("/thing", {"x": 1})
        self.assertEqual(result, {"status": 204})

    def test_empty_200_body_on_put_is_reported_as_success(self):
        transport = FakeTransport({("PUT", "/api/v10/thing"): (200, b"")})
        client = api.Client(transport)
        result = client.json("PUT", "/thing", body={"x": 1})
        self.assertEqual(result, {"status": 200})

    def test_empty_body_on_get_still_returns_none(self):
        transport = FakeTransport({("GET", "/api/v10/thing"): (200, b"")})
        client = api.Client(transport)
        self.assertIsNone(client.get("/thing"))

    def test_empty_body_on_delete_still_returns_none(self):
        transport = FakeTransport({("DELETE", "/api/v10/thing"): (204, b"")})
        client = api.Client(transport)
        self.assertIsNone(client.json("DELETE", "/thing"))


class ReleaseTests(RepoCase):
    def run_release(self, transport, *extra):
        client = api.Client(transport, sleep=lambda s: None)
        return quiet(release.main, [str(self.link), "--version", "r1", *extra], client=client)

    def test_plan_sanitises_names_and_skips_unreleased_parts(self):
        paths = [f["path"] for f in release.plan_files(registry())]
        self.assertEqual(paths, ["step/Arm.step", "step/Airtag-Antenna-mount.step", "drawings/Drawing-1.pdf"])

    def test_dry_run_reads_only(self):
        transport = FakeTransport(release_routes())
        code, out, _ = self.run_release(transport)
        self.assertEqual(code, 0)
        self.assertEqual(transport.writes(), [])
        self.assertIn("Dry run", out)
        self.assertFalse((self.repo / "releases").exists())

    def test_apply_writes_release_the_checker_accepts(self):
        transport = FakeTransport(release_routes())
        code, out, err = self.run_release(transport, "--apply")
        self.assertEqual(code, 0, err)
        target = self.repo / "releases" / "r1"
        manifest = json.loads((target / "manifest.json").read_text())
        self.assertEqual([f["path"] for f in manifest["files"]],
                         ["step/Arm.step", "step/Airtag-Antenna-mount.step", "drawings/Drawing-1.pdf"])
        digest = hashlib.sha256(b"ISO-10303-21;data").hexdigest()
        self.assertTrue(all(f["sha256"] == digest for f in manifest["files"]))
        self.assertEqual(manifest["version"]["id"], "v" * 24)
        self.assertEqual(manifest["microversion"], "m" * 24)
        self.assertIn("exported_at", manifest)
        self.assertEqual([p["partId"] for p in manifest["parts"]], ["JHD", "J/D"])
        errors, warnings = [], []
        mechanical_check.check_releases(self.repo, errors, warnings)
        self.assertEqual((errors, warnings), ([], []))
        step_bodies = [json.loads(c[2]) for c in transport.calls if c[1].endswith("/translations") and "/partstudios/" in c[1]]
        self.assertEqual([b["partIds"] for b in step_bodies], ["JHD", "J/D"])
        self.assertEqual(sum(1 for c in transport.writes() if c[1].endswith("/versions")), 1)

    def test_existing_directory_is_refused_before_any_write(self):
        (self.repo / "releases" / "r1").mkdir(parents=True)
        transport = FakeTransport(release_routes())
        code, _, err = self.run_release(transport, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("never overwritten", err)
        self.assertEqual(transport.writes(), [])

    def test_existing_version_name_is_refused(self):
        transport = FakeTransport(release_routes(versions=[{"name": "r1", "id": "x"}]))
        code, _, err = self.run_release(transport, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(transport.writes(), [])

    def test_failed_export_leaves_no_release_directory(self):
        transport = FakeTransport(release_routes(fail_pdf=True))
        code, _, err = self.run_release(transport, "--apply")
        self.assertEqual(code, 2)
        self.assertFalse((self.repo / "releases" / "r1").exists())
        self.assertEqual([p.name for p in (self.repo / "releases").iterdir()], [])
        self.assertIn("was created and is kept", err)

    def test_missing_live_part_is_refused(self):
        routes = release_routes()
        routes[("GET", f"/api/v10/parts/d/{DID}/w/{WID}/e/{PS}")] = [{"partId": "JHD"}]
        transport = FakeTransport(routes)
        code, _, err = self.run_release(transport, "--apply")
        self.assertEqual(code, 1)
        self.assertIn("not in Part Studio", err)
        self.assertEqual(transport.writes(), [])

    def version_sourced(self, **extra):
        reg = registry()
        reg["parts"].append({"elementId": PS, "partId": "REBH", "name": "VTX-Mount", "release": True, **extra})
        self.link.write_text(json.dumps(reg), encoding="utf-8")

    def test_version_sourced_part_is_exported_from_its_version(self):
        self.version_sourced(sourceVersion=OLD)
        transport = FakeTransport(release_routes(versions=[{"id": OLD, "name": "V4"}]))
        code, out, err = self.run_release(transport)
        self.assertEqual(code, 0, err)
        self.assertIn("from version V4", out)
        self.assertIn("4 file(s): 3 STEP, 1 PDF", out)
        code, out, err = self.run_release(transport, "--apply")
        self.assertEqual(code, 0, err)
        exports = [c[1] for c in transport.calls if c[1].endswith("/translations") and "/partstudios/" in c[1]]
        self.assertIn(f"/api/v10/partstudios/d/{DID}/v/{OLD}/e/{PS}/translations", exports)
        self.assertEqual(sum(f"/v/{'v' * 24}/" in e for e in exports), 2)
        manifest = json.loads((self.repo / "releases" / "r1" / "manifest.json").read_text())
        self.assertEqual([p.get("sourceVersion") for p in manifest["parts"]], [None, None, OLD])

    def test_source_version_is_resolved_from_the_assembly(self):
        reg = registry()
        reg["parts"].append({"partStudio": PS, "partId": "REBH", "name": "VTX-Mount", "release": True})
        self.link.write_text(json.dumps(reg), encoding="utf-8")
        routes = release_routes(instances=[asm_instance("JHD"), asm_instance("J/D"), asm_instance("REBH", OLD)])
        code, out, err = self.run_release(FakeTransport(routes))
        self.assertEqual(code, 0, err)
        self.assertIn(f"part REBH of {PS} from version {OLD}", out)

    def test_version_problems_are_refused(self):
        self.version_sourced(sourceVersion="n" * 24)
        routes = release_routes(instances=[asm_instance("JHD"), asm_instance("J/D"), asm_instance("REBH", OLD)])
        code, _, err = self.run_release(FakeTransport(routes))
        self.assertEqual(code, 1)
        self.assertIn("the assembly references", err)
        self.assertIn(f"is not in version {'n' * 24}", err)
        self.version_sourced(sourceMicroversion="q" * 24)
        code, _, err = self.run_release(FakeTransport(release_routes()))
        self.assertEqual(code, 1)
        self.assertIn("a STEP export needs a version", err)

    def test_part_instanced_from_two_sources_is_refused(self):
        routes = release_routes(instances=[asm_instance("JHD"), asm_instance("JHD", OLD), asm_instance("J/D")])
        code, _, err = self.run_release(FakeTransport(routes))
        self.assertEqual(code, 1)
        self.assertIn("more than one source", err)

    def test_agent_branch_reads_the_branch_workspace(self):
        branch = "b" * 24
        reg = registry()
        reg["agentBranch"] = {"id": branch, "name": "agent/x"}
        self.link.write_text(json.dumps(reg), encoding="utf-8")
        transport = FakeTransport(release_routes(wid=branch))
        code, out, err = self.run_release(transport, "--agent-branch")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Workspace  agent/x ({branch})", out)
        self.assertTrue(all(WID not in c[1] for c in transport.calls))

    def test_bad_label_and_unknown_argument(self):
        code, _, err = self.run_release(FakeTransport(release_routes()), "--apply")
        self.assertEqual(code, 0)
        with self.assertRaises(SystemExit):
            quiet(release.main, [str(self.link), "--version", "r2", "--size", "5"])
        client = api.Client(FakeTransport(release_routes()))
        code, _, err = quiet(release.main, [str(self.link), "--version", "../x"], client=client)
        self.assertEqual(code, 2)


TARGET = "t" * 24
TWID = "x" * 24


def fit_routes():
    board_fs = "\n".join([
        "PCB\t0.0\t0.0\t0.0\t0.03\t0.03\t0.0016",
        "CAP\t0.100\t0.100\t0.0\t0.101\t0.101\t0.001",
    ])
    return {
        ("GET", f"/api/v10/documents/{TARGET}"): {
            "name": "fit", "permission": "OWNER", "owner": {"name": "me"}, "defaultWorkspace": {"id": TWID}},
        ("GET", f"/api/v10/assemblies/d/{DID}/w/{WID}/e/{ASM}"): {"rootAssembly": {
            "instances": [
                {"id": "i1", "type": "Part", "elementId": PS, "partId": "JHD", "name": "Arm <1>", "documentId": DID},
                {"id": "i2", "type": "Part", "elementId": PS, "partId": "J/D", "name": "Mount <1>", "documentId": DID},
                {"id": "i3", "type": "Part", "elementId": "other", "partId": "X", "name": "Motor", "documentId": DID},
            ],
            "occurrences": [
                {"path": ["i1"], "transform": [1, 0, 0, 0.02, 0, 1, 0, 0.02, 0, 0, 1, 0, 0, 0, 0, 1]},
                {"path": ["i2"], "transform": [1, 0, 0, 0.5, 0, 1, 0, 0.5, 0, 0, 1, 0, 0, 0, 0, 1]},
            ]}},
        ("GET", rf"/api/v10/parts/d/{DID}/w/{WID}/e/{PS}/partid/[^/]+/boundingboxes"): {
            "lowX": 0, "lowY": 0, "lowZ": -0.001, "highX": 0.02, "highY": 0.02, "highZ": 0.001},
        ("POST", f"/api/v10/translations/d/{TARGET}/w/{TWID}"): {"id": "t1", "requestState": "ACTIVE"},
        ("GET", "/api/v10/translations/t1"): {"requestState": "DONE", "resultElementIds": ["new"]},
        ("GET", f"/api/v10/documents/d/{TARGET}/w/{TWID}/elements"): [
            {"id": "new", "name": "board", "elementType": "PARTSTUDIO"}],
        ("POST", f"/api/v10/partstudios/d/{TARGET}/w/{TWID}/e/new/featurescript"): {
            "result": {"value": board_fs}, "notices": []},
        ("DELETE", f"/api/v10/elements/d/{TARGET}/w/{TWID}/e/new"): {},
    }


class FitTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.board = self.repo / "board.step"
        self.board.write_bytes(b"ISO-10303-21;")

    def run_fit(self, transport, *extra, target=TARGET):
        client = api.Client(transport, sleep=lambda s: None)
        argv = ["--board-step", str(self.board), "--cad", str(self.link), "--target-document", target, *extra]
        return quiet(fit.main, argv, client=client)

    def test_overlap_geometry(self):
        self.assertIsNone(fit.overlap((0, 0, 0, 1, 1, 1), (1, 0, 0, 2, 1, 1)))
        dims = fit.overlap((0, 0, 0, 1, 1, 1), (0.5, 0.5, 0.5, 2, 2, 2))
        self.assertEqual(dims, (0.5, 0.5, 0.5))
        rotated = fit.transform_box((0, 0, 0, 1, 2, 3), [0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
        self.assertEqual(rotated, (-2, 0, 0, 0, 1, 3))

    def test_frame_document_is_refused(self):
        transport = FakeTransport(fit_routes())
        code, _, err = self.run_fit(transport, "--apply", target=DID)
        self.assertEqual(code, 2)
        self.assertIn("frame document", err)
        self.assertEqual(transport.calls, [])

    def test_dry_run_reads_only(self):
        transport = FakeTransport(fit_routes())
        code, out, _ = self.run_fit(transport)
        self.assertEqual(code, 0)
        self.assertEqual(transport.writes(), [])
        self.assertIn("Dry run", out)

    def test_apply_reports_overlaps_in_millimetres(self):
        transport = FakeTransport(fit_routes())
        code, out, err = self.run_fit(transport, "--apply", "--remove-tab")
        self.assertEqual(code, 1, err)
        self.assertIn("Arm <1>", out)
        self.assertIn("x PCB", out)
        self.assertIn("10.00, 10.00, 1.00", out)
        self.assertNotIn("Motor", out)
        self.assertNotIn("Mount <1>", out)
        self.assertIn(("DELETE", f"/api/v10/elements/d/{TARGET}/w/{TWID}/e/new", None), transport.calls)
        upload = [c for c in transport.calls if c[1].endswith(f"/translations/d/{TARGET}/w/{TWID}")][0]
        self.assertIn(b'name="flattenAssemblies"\r\n\r\ntrue', upload[2])
        self.assertTrue(any("/partid/J%2FD/" in c[1] for c in transport.calls))

    def test_offset_moves_board_clear(self):
        transport = FakeTransport(fit_routes())
        code, out, _ = self.run_fit(transport, "--apply", "--offset", "0,0,10")
        self.assertEqual(code, 0)
        self.assertIn("Overlaps   none", out)
        self.assertIn("Kept       tab new", out)

    def test_featurescript_error_is_reported(self):
        routes = fit_routes()
        routes[("POST", f"/api/v10/partstudios/d/{TARGET}/w/{TWID}/e/new/featurescript")] = {
            "result": None, "notices": [{"level": "ERROR", "message": "parse"}]}
        code, _, err = self.run_fit(FakeTransport(routes), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("FeatureScript failed", err)


if __name__ == "__main__":
    unittest.main()
