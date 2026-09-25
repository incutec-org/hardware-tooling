#!/usr/bin/env python3
"""Release a mechanical design from Onshape into a product repository.

    python3 hardware/onshape_release.py <repo>/cad/onshape.json --version <label>          # dry run
    python3 hardware/onshape_release.py <repo>/cad/onshape.json --version <label> --apply

The link file names one Onshape document and workspace, its elements and its
parts (templates/mechanical-repository/cad/onshape.json). The dry run reads the
live document, checks every listed element and part still exists, and prints
what a release would write. It changes nothing.

--apply creates a named Onshape version <label> of the workspace (a write to
the live document), then exports from that version one STEP per part marked
"release": true and one PDF per DRAWING element, and writes

    <repo>/releases/<label>/step/<part>.step
    <repo>/releases/<label>/drawings/<drawing>.pdf
    <repo>/releases/<label>/manifest.json

The manifest lists every file with its SHA-256 (the format
hardware/mechanical_check.py verifies) plus the document, version id,
microversion, elements, parts and export time. An existing release directory
is never overwritten, and a label already used as an Onshape version name is
refused. Files are written to a hidden staging directory and moved into place
only after every export succeeded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from onshape_api import Client, OnshapeError, load_registry  # noqa: E402

LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_name(name: str) -> str:
    cleaned = UNSAFE.sub("-", name).strip(" .-")
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned or "unnamed"


def plan_files(registry: dict) -> list[dict]:
    """Every file a release writes, with its source, in a stable order."""
    files: list[dict] = []
    used: set[str] = set()

    def unique(path: str, suffix_id: str) -> str:
        if path.lower() in used:
            stem, ext = path.rsplit(".", 1)
            path = f"{stem}-{safe_name(suffix_id)}.{ext}"
        used.add(path.lower())
        return path

    for part in registry["parts"]:
        if part.get("release") is True:
            path = unique(f"step/{safe_name(part['name'])}.step", part["partId"])
            files.append({"kind": "step", "path": path, "element": part["partStudio"],
                          "partId": part["partId"], "name": part["name"]})
    for element in registry["elements"]:
        if element.get("type") == "DRAWING":
            path = unique(f"drawings/{safe_name(element['name'])}.pdf", element["id"])
            files.append({"kind": "pdf", "path": path, "element": element["id"], "name": element["name"]})
    return files


def repo_root(registry_path: Path, override: Path | None) -> Path:
    if override:
        return override.resolve()
    registry_path = registry_path.resolve()
    if registry_path.parent.name != "cad":
        raise OnshapeError("the link file is not <repo>/cad/onshape.json; pass --repo")
    return registry_path.parent.parent


def check_live(client: Client, registry: dict) -> list[str]:
    """Compare the link file with the live workspace. Returns problems."""
    did = registry["document"]["id"]
    wid = registry["workspace"]["id"]
    problems = []
    live = {e["id"]: e for e in client.get(f"/documents/d/{did}/w/{wid}/elements")}
    for element in registry["elements"]:
        if element["id"] not in live:
            problems.append(f"element {element['name']!r} ({element['id']}) is not in the workspace")
    studios = sorted({p["partStudio"] for p in registry["parts"] if p.get("release") is True})
    for eid in studios:
        if eid not in live:
            continue
        ids = {p["partId"] for p in client.get(f"/parts/d/{did}/w/{wid}/e/{eid}")}
        for part in registry["parts"]:
            if part["partStudio"] == eid and part.get("release") is True and part["partId"] not in ids:
                problems.append(f"part {part['name']!r} ({part['partId']}) is not in Part Studio {eid}")
    return problems


def existing_version(client: Client, did: str, label: str):
    for version in client.get(f"/documents/d/{did}/versions") or []:
        if version.get("name") == label:
            return version
    return None


def export_file(client: Client, did: str, vid: str, item: dict) -> bytes:
    if item["kind"] == "step":
        body = {"formatName": "STEP", "partIds": item["partId"], "storeInDocument": False,
                "flattenAssemblies": False}
        start = client.post(f"/partstudios/d/{did}/v/{vid}/e/{item['element']}/translations", body)
    else:
        body = {"formatName": "PDF", "storeInDocument": False}
        start = client.post(f"/drawings/d/{did}/v/{vid}/e/{item['element']}/translations", body)
    state = client.wait_translation(start["id"])
    return client.download_translation(did, state)


def run(args, client: Client, now=None) -> int:
    registry_path = Path(args.link)
    registry = load_registry(registry_path)
    root = repo_root(registry_path, args.repo)
    if not LABEL.fullmatch(args.version):
        raise OnshapeError("--version must be 1-64 of letters, digits, '.', '_' or '-', starting alphanumeric")
    target = root / "releases" / args.version
    did = registry["document"]["id"]
    wid = registry["workspace"]["id"]
    files = plan_files(registry)
    doc = client.get(f"/documents/{did}")

    print(f"Document   {doc.get('name')} ({did}), owner {doc.get('owner', {}).get('name')}")
    print(f"Workspace  {registry['workspace'].get('name')} ({wid})")
    print(f"Target     {target}")
    problems = check_live(client, registry)
    if target.exists():
        problems.append(f"{target} exists; a release directory is never overwritten")
    if existing_version(client, did, args.version):
        problems.append(f"Onshape version {args.version!r} already exists in this document")
    if not files:
        problems.append("nothing to release: no part has \"release\": true and no DRAWING element is listed")
    print(("Would create" if not args.apply else "Creating") + f" Onshape version {args.version!r} of workspace {wid}")
    for item in files:
        source = f"part {item['partId']} of {item['element']}" if item["kind"] == "step" else f"drawing {item['element']}"
        print(f"  {item['path']:<48} {source}  ({item['name']})")
    print(f"  manifest.json")
    print(f"{len(files)} file(s): {sum(f['kind'] == 'step' for f in files)} STEP, "
          f"{sum(f['kind'] == 'pdf' for f in files)} PDF")
    if problems:
        for line in problems:
            print(f"refused: {line}", file=sys.stderr)
        return 1
    if not args.apply:
        print("Dry run: nothing written. Add --apply to create the version and export.")
        return 0

    version = client.post(f"/documents/d/{did}/versions", {
        "documentId": did, "workspaceId": wid, "name": args.version,
        "description": f"Release {args.version}, exported by onshape_release.py",
    })
    vid = version["id"]
    microversion = version.get("microversion") or client.get(f"/documents/d/{did}/versions/{vid}").get("microversion")
    print(f"Version    {args.version} = {vid}, microversion {microversion}")

    staging = target.parent / f".{args.version}.partial"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        entries = []
        for item in files:
            data = export_file(client, did, vid, item)
            out = staging / item["path"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            entries.append({"path": item["path"], "sha256": hashlib.sha256(data).hexdigest()})
            print(f"  wrote {item['path']} ({len(data)} bytes)")
        stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "files": entries,
            "document": {"id": did, "name": doc.get("name"), "url": registry["document"].get("url")},
            "workspace": {"id": wid, "name": registry["workspace"].get("name")},
            "version": {"id": vid, "name": args.version},
            "microversion": microversion,
            "elements": registry["elements"],
            "parts": [{"partStudio": f["element"], "partId": f["partId"], "name": f["name"], "path": f["path"]}
                      for f in files if f["kind"] == "step"],
            "exported_at": stamp,
            "tool": "onshape_release.py",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
        if target.exists():
            raise OnshapeError(f"{target} appeared while exporting; leaving it untouched")
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"Export failed. Onshape version {args.version!r} ({vid}) was created and is kept; "
              "rerun with a new label after fixing the cause.", file=sys.stderr)
        raise
    print(f"Released {len(entries)} file(s) to {target}. Commit that directory on its own.")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     epilog="Credentials: see hardware/onshape_api.py.")
    result.add_argument("link", help="path to the one-workspace link file, normally <repo>/cad/onshape.json")
    result.add_argument("--version", required=True, help="release label, used as Onshape version name and directory")
    result.add_argument("--repo", type=Path, help="product repository root when the link file is not <repo>/cad/")
    result.add_argument("--apply", action="store_true", help="create the Onshape version and write the files")
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
