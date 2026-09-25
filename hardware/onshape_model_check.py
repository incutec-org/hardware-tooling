#!/usr/bin/env python3
"""Check an Onshape model against its link file and parts lists, read-only.

    python3 hardware/onshape_model_check.py <repo>/cad/onshape.json [--repo <repo>]
        [--workspace <wid> | --agent-branch] [--json]

Reads the live workspace named by the link file (or --workspace, or with
--agent-branch the link file's "agentBranch", for example a branch workspace)
and reports:

    Link file      a listed element or part is not in the workspace, or a part
                   with "sourceVersion" is not in that version or the assembly
                   references it from another version
    Missing parts  an assembly instance has no source, names a part id the
                   Part Studio no longer has, or references a document version
                   that does not have the part
    Unused parts   a Part Studio part is not instanced in the assembly
    Materials      a part used in the assembly or marked "release": true has no material
    Drawing        the link file lists no drawing, or the listed one is not a drawing
    Parts list     parts.csv and the assembly disagree on a part or its count (--repo)
    Hardware       hardware.csv and the assembly disagree on a count (--repo)

"Part Studio" means every Part Studio that holds a part marked "release": true.
Hardware in the assembly is standard content plus instances from the other
listed Part Studios; hardware names are counted without the instance number
and without the ":1__Body3" suffix a flattened STEP import adds. An item listed in the link file under
"modelCheck": {"ignoreHardware": [...]} is left out of the hardware check, and a
part name under "modelCheck": {"ignoreUnused": [...]} out of the unused-parts check.

An instance that references an older document version is a deliberate source,
not a missing part, as long as that version has the part. The link file may
name such a part with "sourceVersion" (and "sourceMicroversion") plus "elementId".

Prints a Markdown table in the "Model checks" format of a mechanical repository
README (| Check | Finding |), or JSON with --json. Exit 0 no findings,
1 findings, 2 error. Never writes to Onshape.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from onshape_api import Client, OnshapeError, load_registry, part_element, part_source, select_workspace  # noqa: E402

CHECKS = ["Link file", "Missing parts", "Unused parts", "Materials", "Drawing", "Parts list", "Hardware"]
DRAWING_TYPE = "onshape-app/drawing"
INSTANCE_SUFFIX = re.compile(r"\s*<\d+>$")
IMPORT_BODY = re.compile(r"\s*:\d+__.*$")  # a flattened STEP import names bodies "<part>:1__Body3"


def base_name(name: str) -> str:
    return INSTANCE_SUFFIX.sub("", name or "")


def hardware_name(name: str) -> str:
    return IMPORT_BODY.sub("", base_name(name))


def code(text: str) -> str:
    return f"`{text}`"


def names_list(names: list[str]) -> str:
    quoted = [code(n) for n in names]
    if len(quoted) == 1:
        return quoted[0]
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def material_name(part: dict) -> str | None:
    material = part.get("material") or {}
    return material.get("displayName") or material.get("id") or None


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class Model:
    """Everything the checks need, read from the live workspace once."""

    def __init__(self, client: Client, registry: dict, wid: str):
        self.client = client
        self.registry = registry
        self.did = registry["document"]["id"]
        self.wid = wid
        self.elements = {e["id"]: e for e in client.get(f"/documents/d/{self.did}/w/{wid}/elements")}
        self.release_studios = sorted({part_element(p) for p in registry["parts"] if p.get("release") is True})
        self.studio_parts: dict[str, dict[str, dict]] = {}
        for eid in self.release_studios:
            if eid in self.elements:
                self.studio_parts[eid] = {p["partId"]: p for p in client.get(f"/parts/d/{self.did}/w/{wid}/e/{eid}")}
        assemblies = [e for e in registry["elements"] if e.get("type") == "ASSEMBLY"]
        self.assembly_entry = assemblies[0] if assemblies else None
        self.occurrences: list[dict] = []
        if self.assembly_entry and self.assembly_entry["id"] in self.elements:
            self._read_assembly(self.assembly_entry["id"])
        self._versions = None
        self._version_parts: dict[tuple[str, str], dict[str, dict]] = {}
        self.pinned_parts: dict[str, dict] = {}  # partId -> part, from instances that reference a version

    def _read_assembly(self, aid: str):
        data = self.client.get(f"/assemblies/d/{self.did}/w/{self.wid}/e/{aid}")
        root = data["rootAssembly"]
        instances = {i["id"]: i for i in root.get("instances", [])}
        for sub in data.get("subAssemblies", []):
            for inst in sub.get("instances", []):
                instances.setdefault(inst["id"], inst)
        for occ in root.get("occurrences", []):
            inst = instances.get(occ["path"][-1])
            if inst is None or inst.get("suppressed") or inst.get("type") == "Assembly":
                continue
            self.occurrences.append(inst)

    def version_name(self, vid: str) -> str:
        if self._versions is None:
            self._versions = {v["id"]: v.get("name") for v in self.client.get(f"/documents/d/{self.did}/versions") or []}
        return self._versions.get(vid) or vid

    def version_parts(self, vid: str, eid: str, kind: str = "v") -> dict[str, dict]:
        key = (kind, vid, eid)
        if key not in self._version_parts:
            try:
                parts = self.client.get(f"/parts/d/{self.did}/{kind}/{vid}/e/{eid}")
            except OnshapeError:
                parts = []
            self._version_parts[key] = {p["partId"]: p for p in parts}
        return self._version_parts[key]

    def microversion_parts(self, mid: str, eid: str) -> dict[str, dict]:
        return self.version_parts(mid, eid, kind="m")

    def is_frame_instance(self, inst: dict) -> bool:
        return (inst.get("type") == "Part" and inst.get("documentId") == self.did
                and inst.get("elementId") in self.release_studios)

    def is_workspace_ref(self, inst: dict) -> bool:
        return not inst.get("documentVersion")


def check(model: Model, repo: Path | None) -> list[dict]:
    """Return findings as {check, message, subject}."""
    findings: list[dict] = []
    reg = model.registry

    def add(check_name, message, subject=None):
        findings.append({"check": check_name, "message": message, "subject": subject})

    # Link file
    for element in reg["elements"]:
        if element["id"] not in model.elements:
            add("Link file", f"element {code(element['name'])} ({element['id']}) is not in the workspace")
    sourced = {}  # (eid, partId) -> link file version id
    for part in reg["parts"]:
        source = part_source(part)
        if source:
            if source[0] == "v":
                sourced[(part_element(part), part["partId"])] = source[1]
                pinned = model.version_parts(source[1], part_element(part)).get(part["partId"])
                where = f"version {code(model.version_name(source[1]))}"
            else:
                pinned = model.microversion_parts(source[1], part_element(part)).get(part["partId"])
                where = f"microversion {code(source[1])}"
            if pinned is None:
                add("Link file", f"part {code(part['name'])} ({part['partId']}) is not in {where}")
            elif pinned["name"] != part["name"]:
                add("Link file", f"part {code(part['name'])} ({part['partId']}) is named "
                                 f"{code(pinned['name'])} in {where}")
            continue
        live = model.studio_parts.get(part["partStudio"])
        if live is not None and part["partId"] not in live:
            add("Link file", f"part {code(part['name'])} ({part['partId']}) is not in its Part Studio")
        elif live is not None and live[part["partId"]]["name"] != part["name"]:
            add("Link file", f"part {code(part['name'])} ({part['partId']}) is named "
                             f"{code(live[part['partId']]['name'])} in the model")

    # Missing parts, and what the assembly uses from the Part Studios
    used: Counter = Counter()          # (eid, partId) -> count, workspace references
    used_names: Counter = Counter()    # part name -> count, any frame instance
    used_parts: list[dict] = []        # resolved part dicts of used frame parts
    for inst in model.occurrences:
        if not inst.get("type"):
            add("Missing parts", f"{code(inst.get('name', inst.get('id')))} has no source part "
                                 "(deleted or not shared)", inst.get("name"))
            continue
        if not model.is_frame_instance(inst):
            continue
        eid, pid = inst["elementId"], inst.get("partId")
        live = model.studio_parts.get(eid, {})
        studio = model.elements.get(eid, {}).get("name", eid)
        if not model.is_workspace_ref(inst):
            vid = inst["documentVersion"]
            pinned = model.version_parts(vid, eid).get(pid)
            name = pinned["name"] if pinned else base_name(inst["name"])
            if pinned is None:
                add("Missing parts", f"{code(inst['name'])} points at part {code(pid)} in version "
                                     f"{code(model.version_name(vid))}, which does not have it", name)
            else:
                model.pinned_parts[pid] = pinned
                if pinned not in used_parts:
                    used_parts.append(pinned)
            listed_version = sourced.get((eid, pid))
            if listed_version and listed_version != vid:
                add("Link file", f"part {code(name)} ({pid}): the link file names version "
                                 f"{code(model.version_name(listed_version))}, the assembly references "
                                 f"{code(model.version_name(vid))}", name)
            used_names[name] += 1
            continue
        if pid not in live:
            add("Missing parts", f"{code(inst['name'])} points at part id {code(pid)}, "
                                 f"which is not in {code(studio)}", base_name(inst["name"]))
            used_names[base_name(inst["name"])] += 1
            continue
        used[(eid, pid)] += 1
        used_names[live[pid]["name"]] += 1
        if used[(eid, pid)] == 1:
            used_parts.append(live[pid])

    # Unused parts
    if model.assembly_entry is None:
        add("Unused parts", "the link file lists no assembly")
    else:
        ignore_unused = set((reg.get("modelCheck") or {}).get("ignoreUnused") or [])
        for eid, parts in model.studio_parts.items():
            unused = [p["name"] for pid, p in parts.items() if used[(eid, pid)] == 0 and p["name"] not in ignore_unused]
            if unused:
                verb = "is" if len(unused) == 1 else "are"
                studio = model.elements[eid].get("name", eid)
                add("Unused parts", f"{names_list(unused)} {verb} in the {code(studio)} Part Studio "
                                    "but not in the assembly", unused)

    # Materials
    released = []
    for p in reg["parts"]:
        if p.get("release") is not True:
            continue
        source = part_source(p)
        if source is None:
            released.append(model.studio_parts.get(p["partStudio"], {}).get(p["partId"]))
        elif source[0] == "v":
            released.append(model.version_parts(source[1], part_element(p)).get(p["partId"]))
        else:
            released.append(model.microversion_parts(source[1], part_element(p)).get(p["partId"]))
    checked, missing = set(), []
    for part in used_parts + released:
        if not part or part["name"] in checked:
            continue
        checked.add(part["name"])
        if not material_name(part):
            missing.append(part["name"])
    if missing:
        verb = "has" if len(missing) == 1 else "have"
        add("Materials", f"{names_list(missing)} {verb} no material set in the model", missing)

    # Drawing
    drawings = [e for e in reg["elements"] if e.get("type") == "DRAWING"]
    if not drawings:
        add("Drawing", "the link file lists no drawing")
    for element in drawings:
        live = model.elements.get(element["id"])
        if live and live.get("dataType") != DRAWING_TYPE:
            add("Drawing", f"{code(element['name'])} is a {live.get('dataType') or live.get('elementType')}, not a drawing")

    if repo is None:
        return findings

    # Parts list
    parts_csv = repo / "parts.csv"
    if parts_csv.is_file():
        live_by_id = {**model.pinned_parts,
                      **{pid: p for parts in model.studio_parts.values() for pid, p in parts.items()}}
        listed = set()
        for row in read_csv(parts_csv):
            name, pid = row.get("part", ""), (row.get("onshape_part") or "").strip()
            qty = int(row["qty_per_set"]) if (row.get("qty_per_set") or "").isdigit() else None
            if pid and pid not in live_by_id:
                add("Parts list", f"{code(name)} ({pid}) is in parts.csv but not in the Part Studio", name)
            elif pid and live_by_id[pid]["name"] != name:
                add("Parts list", f"{code(name)} in parts.csv is {code(live_by_id[pid]['name'])} "
                                  "in the Part Studio", name)
            model_name = live_by_id[pid]["name"] if pid in live_by_id else name
            listed.add(model_name)
            count = used_names.get(model_name, 0)
            if qty is not None and count != qty:
                add("Parts list", f"{code(name)}: parts.csv {qty}, assembly {count}", name)
        for name, count in sorted(used_names.items()):
            if name not in listed:
                add("Parts list", f"{code(name)}: assembly {count}, not in parts.csv", name)

    # Hardware
    hardware_csv = repo / "hardware.csv"
    if hardware_csv.is_file():
        ignore = set((reg.get("modelCheck") or {}).get("ignoreHardware") or [])
        other_studios = {part_element(p) for p in reg["parts"]} - set(model.release_studios)
        counts: Counter = Counter(hardware_name(i.get("name", "")) for i in model.occurrences if i.get("type"))
        hardware = {hardware_name(i["name"]) for i in model.occurrences
                    if i.get("isStandardContent") or (i.get("documentId") == model.did
                                                      and i.get("elementId") in other_studios)}
        listed = set()
        for row in read_csv(hardware_csv):
            item = row.get("item", "")
            listed.add(item)
            qty = int(row["qty_per_set"]) if (row.get("qty_per_set") or "").isdigit() else None
            if qty is not None and counts.get(item, 0) != qty:
                add("Hardware", f"{code(item)}: hardware.csv {qty}, assembly {counts.get(item, 0)}", item)
        for item in sorted(hardware - listed - ignore):
            add("Hardware", f"{code(item)}: assembly {counts[item]}, not in hardware.csv", item)
    return findings


def table(findings: list[dict]) -> str:
    rows: "OrderedDict[str, list[str]]" = OrderedDict()
    for name in CHECKS:
        messages = [f["message"] for f in findings if f["check"] == name]
        if messages:
            rows[name] = messages
    lines = ["| Check | Finding |", "|---|---|"]
    if not rows:
        lines.append("| All | No findings |")
    for name, messages in rows.items():
        text = "; ".join(messages).replace("|", "\\|")
        lines.append(f"| {name} | {text[0].upper() + text[1:]} |")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     epilog="Credentials: see hardware/onshape_api.py.")
    result.add_argument("link", help="path to the one-workspace link file, normally <repo>/cad/onshape.json")
    result.add_argument("--repo", type=Path, help="product repository root; adds the parts.csv and hardware.csv checks")
    result.add_argument("--workspace", help="workspace id to check instead of the link file's, e.g. a branch")
    result.add_argument("--agent-branch", action="store_true",
                        help="check the link file's \"agentBranch\" workspace")
    result.add_argument("--json", action="store_true", help="print JSON instead of the Markdown table")
    return result


def main(argv=None, client: Client | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        registry = load_registry(Path(args.link))
        if args.repo and not args.repo.is_dir():
            raise OnshapeError(f"--repo {args.repo} is not a directory")
        wid = select_workspace(registry, args.workspace, args.agent_branch)
        model = Model(client or Client.from_environment(), registry, wid)
        findings = check(model, args.repo)
    except OnshapeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"document": registry["document"]["id"], "workspace": wid,
                          "findings": findings}, indent=2, ensure_ascii=False))
    else:
        print(table(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
