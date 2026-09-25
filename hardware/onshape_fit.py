#!/usr/bin/env python3
"""Check a KiCad board STEP against a frame assembly in Onshape.

    python3 hardware/onshape_fit.py --board-step <board.step> --cad <repo>/cad/onshape.json \\
        --target-document <did> [--align-to <element name>] [--offset X,Y,Z] [--apply]

The link file (templates/mechanical-repository/cad/onshape.json) names the
frame document, workspace, its ASSEMBLY element and the frame parts. The frame
is only read.

--apply imports the board STEP, flattened, as a new Part Studio tab in the
target document, then compares every board body with every instance of a
listed frame part in the frame assembly and prints the overlaps in
millimetres. The target document must differ from the frame document; use a
scratch or fit-check document you are allowed to write. The imported tab is
kept unless --remove-tab is passed. Without --apply nothing is written.

Placement: a KiCad STEP keeps KiCad's coordinates. --align-to <element name>
moves the board so its bounding-box centre sits on the centre of that Part
Studio's instances in the frame assembly (for example the board it replaces).
--offset adds a translation in millimetres afterwards.

The comparison is axis-aligned bounding-box overlap per body: a reported
overlap is a candidate for a collision, not proof of one, and a clean result
proves the boxes are clear. Onshape returns metres; the interference tool in
onshape-mcp reports inches. Everything printed here is millimetres.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from onshape_api import METRE_TO_MM, Client, OnshapeError, load_registry, quote  # noqa: E402

TOLERANCE_M = 1e-6
IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]

BODY_BOXES_FS = """
function(context is Context, queries is map)
{
    var out = "";
    for (var body in evaluateQuery(context, qAllModifiableSolidBodies()))
    {
        var bb = evBox3d(context, { "topology" : body, "tight" : true });
        var name = getProperty(context, { "entity" : body, "propertyType" : PropertyType.NAME });
        out = out ~ name ~ "\\t" ~ toString(bb.minCorner[0] / meter) ~ "\\t" ~ toString(bb.minCorner[1] / meter)
            ~ "\\t" ~ toString(bb.minCorner[2] / meter) ~ "\\t" ~ toString(bb.maxCorner[0] / meter)
            ~ "\\t" ~ toString(bb.maxCorner[1] / meter) ~ "\\t" ~ toString(bb.maxCorner[2] / meter) ~ "\\n";
    }
    return out;
}
"""


def parse_offset(text: str) -> tuple[float, float, float]:
    try:
        values = [float(v) for v in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--offset takes X,Y,Z in millimetres") from exc
    if len(values) != 3:
        raise argparse.ArgumentTypeError("--offset takes X,Y,Z in millimetres")
    return values[0] / METRE_TO_MM, values[1] / METRE_TO_MM, values[2] / METRE_TO_MM


def box_from_api(data: dict) -> tuple:
    return (data["lowX"], data["lowY"], data["lowZ"], data["highX"], data["highY"], data["highZ"])


def transform_box(box: tuple, m: list) -> tuple:
    corners = [(x, y, z) for x in (box[0], box[3]) for y in (box[1], box[4]) for z in (box[2], box[5])]
    pts = [(m[0] * x + m[1] * y + m[2] * z + m[3],
            m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11]) for x, y, z in corners]
    return (min(p[0] for p in pts), min(p[1] for p in pts), min(p[2] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts), max(p[2] for p in pts))


def union(boxes) -> tuple:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), min(b[2] for b in boxes),
            max(b[3] for b in boxes), max(b[4] for b in boxes), max(b[5] for b in boxes))


def centre(box: tuple) -> tuple:
    return ((box[0] + box[3]) / 2, (box[1] + box[4]) / 2, (box[2] + box[5]) / 2)


def shift(box: tuple, d: tuple) -> tuple:
    return (box[0] + d[0], box[1] + d[1], box[2] + d[2], box[3] + d[0], box[4] + d[1], box[5] + d[2])


def overlap(a: tuple, b: tuple):
    dims = tuple(min(a[i + 3], b[i + 3]) - max(a[i], b[i]) for i in range(3))
    return dims if all(d > TOLERANCE_M for d in dims) else None


def parse_body_boxes(result: dict) -> list[tuple[str, tuple]]:
    errors = [n.get("message") for n in (result or {}).get("notices") or [] if n.get("level") == "ERROR"]
    if errors:
        raise OnshapeError(f"FeatureScript failed: {'; '.join(errors)}")
    value = ((result or {}).get("result") or {}).get("value", "")
    bodies = []
    for line in value.splitlines():
        cols = line.split("\t")
        if len(cols) != 7:
            continue
        bodies.append((cols[0], tuple(float(c) for c in cols[1:])))
    return bodies


def frame_instances(client: Client, registry: dict, assembly_id: str) -> tuple[list, dict]:
    """World boxes of every instance of a listed frame part, and the definition."""
    did = registry["document"]["id"]
    wid = registry["workspace"]["id"]
    definition = client.get(f"/assemblies/d/{did}/w/{wid}/e/{assembly_id}")
    root = definition.get("rootAssembly", {})
    transforms = {o["path"][0]: o.get("transform") or IDENTITY
                  for o in root.get("occurrences", []) if len(o.get("path", [])) == 1}
    wanted = {(p["partStudio"], p["partId"]): p["name"] for p in registry["parts"]}
    local: dict = {}
    boxes = []
    for inst in root.get("instances", []):
        key = (inst.get("elementId"), inst.get("partId"))
        if inst.get("type") != "Part" or inst.get("suppressed") or key not in wanted:
            continue
        if inst.get("documentId", did) != did:
            continue
        if key not in local:
            data = client.get(f"/parts/d/{did}/w/{wid}/e/{key[0]}/partid/{quote(key[1])}/boundingboxes")
            local[key] = box_from_api(data)
        boxes.append((inst.get("name", wanted[key]), transform_box(local[key], transforms.get(inst["id"], IDENTITY))))
    return boxes, definition


def reference_box(client: Client, registry: dict, definition: dict, element_name: str) -> tuple:
    did = registry["document"]["id"]
    wid = registry["workspace"]["id"]
    matches = [e for e in client.get(f"/documents/d/{did}/w/{wid}/elements")
               if e["name"] == element_name and e["elementType"] == "PARTSTUDIO"]
    if len(matches) != 1:
        raise OnshapeError(f"--align-to {element_name!r}: {len(matches)} Part Studios with that name")
    eid = matches[0]["id"]
    root = definition.get("rootAssembly", {})
    ids = [i["id"] for i in root.get("instances", []) if i.get("elementId") == eid and not i.get("suppressed")]
    if not ids:
        raise OnshapeError(f"--align-to {element_name!r}: not instanced in the frame assembly")
    transforms = {o["path"][0]: tuple(o.get("transform") or IDENTITY)
                  for o in root.get("occurrences", []) if len(o.get("path", [])) == 1}
    distinct = {transforms.get(i, tuple(IDENTITY)) for i in ids}
    if len(distinct) != 1:
        raise OnshapeError(f"--align-to {element_name!r}: its instances are placed with {len(distinct)} different transforms")
    local = box_from_api(client.get(f"/partstudios/d/{did}/w/{wid}/e/{eid}/boundingboxes"))
    return transform_box(local, list(distinct.pop()))


def mm(values) -> str:
    return ", ".join(f"{v * METRE_TO_MM:.2f}" for v in values)


def run(args, client: Client) -> int:
    registry = load_registry(args.cad)
    frame_did = registry["document"]["id"]
    board = Path(args.board_step)
    if not board.is_file():
        raise OnshapeError(f"no such board STEP: {board}")
    if args.target_document == frame_did:
        raise OnshapeError("the target document is the frame document; import into a scratch or fit-check document")
    assemblies = [e for e in registry["elements"] if e.get("type") == "ASSEMBLY"]
    if len(assemblies) != 1:
        raise OnshapeError(f"{args.cad}: expected one ASSEMBLY element, found {len(assemblies)}")
    target = client.get(f"/documents/{args.target_document}")
    twid = args.target_workspace or target["defaultWorkspace"]["id"]
    if target.get("permission") not in ("WRITE", "RESHARE", "OWNER", "FULL"):
        raise OnshapeError(f"no write permission on target document {target.get('name')!r}")

    print(f"Frame      {registry['document'].get('name')} / {registry['workspace'].get('name')}, "
          f"assembly {assemblies[0]['name']!r} ({assemblies[0]['id']})")
    print(f"Board      {board} ({board.stat().st_size} bytes)")
    print(f"Target     {target.get('name')} ({args.target_document}), workspace {twid}, "
          f"owner {target.get('owner', {}).get('name')}")
    print(f"Placement  " + (f"centre on {args.align_to!r}" if args.align_to else "KiCad STEP coordinates")
          + (f", then offset {mm(args.offset)} mm" if any(args.offset) else ""))
    if not args.apply:
        print("Dry run: would import the board, flattened, as a new Part Studio tab in the target "
              "document and compare it with the frame parts. Add --apply.")
        return 0

    frame, definition = frame_instances(client, registry, assemblies[0]["id"])
    if not frame:
        raise OnshapeError("no instance of a listed frame part in the assembly")
    move = args.offset
    ref = reference_box(client, registry, definition, args.align_to) if args.align_to else None

    started = client.upload(f"/translations/d/{args.target_document}/w/{twid}", board, {
        "encodedFilename": board.name, "translate": "true", "flattenAssemblies": "true",
        "importAppearances": "true", "createDrawingIfPossible": "false", "storeInDocument": "true",
        "yAxisIsUp": "false", "unit": "millimeter",
    })
    state = client.wait_translation(started["id"])
    tabs = state.get("resultElementIds") or []
    elements = {e["id"]: e for e in client.get(f"/documents/d/{args.target_document}/w/{twid}/elements")}
    studios = [t for t in tabs if elements.get(t, {}).get("elementType") == "PARTSTUDIO"]
    if len(studios) != 1:
        raise OnshapeError(f"import produced {len(studios)} Part Studios ({tabs}); expected 1")
    tab = studios[0]
    print(f"Imported   Part Studio {elements[tab]['name']!r} ({tab})")
    try:
        result = client.post(f"/partstudios/d/{args.target_document}/w/{twid}/e/{tab}/featurescript",
                             {"script": BODY_BOXES_FS})
        bodies = parse_body_boxes(result)
        if not bodies:
            raise OnshapeError("the imported Part Studio has no solid bodies")
        board_box = union(b for _, b in bodies)
        if ref:
            c_ref, c_board = centre(ref), centre(board_box)
            move = tuple(c_ref[i] - c_board[i] + args.offset[i] for i in range(3))
        bodies = [(name, shift(box, move)) for name, box in bodies]
        board_box = shift(board_box, move)
        print(f"Board box  min ({mm(board_box[:3])}) max ({mm(board_box[3:])}) mm, {len(bodies)} bodies, "
              f"moved by ({mm(move)}) mm")
        hits = []
        for fname, fbox in frame:
            for bname, bbox in bodies:
                dims = overlap(fbox, bbox)
                if dims:
                    hits.append((min(dims), fname, bname, dims))
        hits.sort(key=lambda h: h[0], reverse=True)
        print(f"Checked    {len(bodies)} board bodies x {len(frame)} frame instances")
        if not hits:
            print("Overlaps   none: every board body box is clear of every frame part box")
        else:
            print(f"Overlaps   {len(hits)} box overlap(s), deepest first (X, Y, Z overlap in mm):")
            for depth, fname, bname, dims in hits[: args.limit]:
                print(f"  {fname:<28} x {bname:<32} {mm(dims)}  (min {depth * METRE_TO_MM:.2f})")
            if len(hits) > args.limit:
                print(f"  ... {len(hits) - args.limit} more; raise --limit to list them")
    finally:
        if args.remove_tab:
            client.raw("DELETE", f"/elements/d/{args.target_document}/w/{twid}/e/{tab}")
            print(f"Removed    tab {tab}")
        else:
            print(f"Kept       tab {tab} in the target document; delete it when done")
    return 1 if hits else 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     epilog="Exit 0 clear, 1 overlaps found, 2 error. Credentials: see hardware/onshape_api.py.")
    result.add_argument("--board-step", required=True, help="board STEP exported from KiCad")
    result.add_argument("--cad", required=True, type=Path, help="the frame's one-workspace link file")
    result.add_argument("--target-document", required=True, help="document id to import into; never the frame document")
    result.add_argument("--target-workspace", help="workspace id in the target document; default its default workspace")
    result.add_argument("--align-to", help="name of a Part Studio in the frame workspace whose assembly position the board takes")
    result.add_argument("--offset", type=parse_offset, default=(0.0, 0.0, 0.0), help="X,Y,Z translation in mm")
    result.add_argument("--limit", type=int, default=40, help="overlaps to list (default 40)")
    result.add_argument("--remove-tab", action="store_true", help="delete the imported tab after the check")
    result.add_argument("--apply", action="store_true", help="import the board and run the check")
    return result


def main(argv=None, client: Client | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args, client or Client.from_environment())
    except OnshapeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
