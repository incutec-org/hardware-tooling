#!/usr/bin/env python3
"""Check a mechanical product repository against templates/mechanical-repository.

    python3 hardware/mechanical_check.py <repository> [--json]

Errors (exit 1): a required file is missing, a KiCad source file is present,
parts.csv or hardware.csv has the wrong header or a bad quantity,
cad/onshape.json is not an Onshape link, or a file listed in a release
manifest is missing or its SHA-256 differs.
Warnings (exit 0): the Onshape link has empty ids, parts.csv has no rows, or a
release directory holds a file its manifest does not list.
"""
import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

REQUIRED = ["README.md", "AGENTS.md", "LICENSE", "parts.csv", "hardware.csv",
            "cad/onshape.json", ".gitattributes", ".gitignore"]
PARTS_HEADER = ["part", "type", "material", "thickness_mm", "qty_per_set",
                "process", "sku", "onshape_part"]
HARDWARE_HEADER = ["item", "standard", "size", "qty_per_set", "notes"]
KICAD_SUFFIXES = (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_sym", ".kicad_dru")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_csv(root, name, header, errors, warnings):
    path = root / name
    if not path.is_file():
        return
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if not rows or rows[0] != header:
        errors.append(f"{name}: header must be {','.join(header)}")
        return
    if len(rows) == 1:
        warnings.append(f"{name}: no rows yet")
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            errors.append(f"{name}:{number}: {len(row)} columns, expected {len(header)}")
            continue
        qty = row[header.index("qty_per_set")]
        if not qty.isdigit() or int(qty) < 1:
            errors.append(f"{name}:{number}: qty_per_set must be a whole number of at least 1, got {qty!r}")


def check_onshape(root, errors, warnings):
    path = root / "cad" / "onshape.json"
    if not path.is_file():
        return
    try:
        link = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"cad/onshape.json: not JSON ({exc})")
        return
    if link.get("source") != "onshape":
        errors.append('cad/onshape.json: "source" must be "onshape"')
    for key in ("document", "workspace"):
        if not isinstance(link.get(key), dict):
            errors.append(f'cad/onshape.json: "{key}" must be an object')
        elif not link[key].get("id"):
            warnings.append(f"cad/onshape.json: {key} id is empty")
    for key in ("elements", "parts"):
        if not isinstance(link.get(key), list):
            errors.append(f'cad/onshape.json: "{key}" must be a list')


def check_releases(root, errors, warnings):
    releases = root / "releases"
    if not releases.is_dir():
        return
    for rev in sorted(p for p in releases.iterdir() if p.is_dir()):
        manifest_path = rev / "manifest.json"
        if not manifest_path.is_file():
            errors.append(f"releases/{rev.name}: no manifest.json")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"releases/{rev.name}/manifest.json: not JSON ({exc})")
            continue
        listed = set()
        for entry in manifest.get("files", []):
            rel = entry.get("path", "")
            listed.add(rel)
            target = rev / rel
            if not target.is_file():
                errors.append(f"releases/{rev.name}: listed file missing: {rel}")
            elif entry.get("sha256") != sha256(target):
                errors.append(f"releases/{rev.name}: SHA-256 differs for {rel}; release files are never edited")
        for path in rev.rglob("*"):
            rel = path.relative_to(rev).as_posix()
            if path.is_file() and rel != "manifest.json" and rel not in listed:
                warnings.append(f"releases/{rev.name}: {rel} is not in the manifest")


def check(root):
    root = Path(root)
    errors, warnings = [], []
    for name in REQUIRED:
        if not (root / name).is_file():
            errors.append(f"missing {name}")
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.suffix in KICAD_SUFFIXES:
            errors.append(f"KiCad file in a mechanical repository: {path.relative_to(root)}")
    check_csv(root, "parts.csv", PARTS_HEADER, errors, warnings)
    check_csv(root, "hardware.csv", HARDWARE_HEADER, errors, warnings)
    check_onshape(root, errors, warnings)
    check_releases(root, errors, warnings)
    return errors, warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repository")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    errors, warnings = check(args.repository)
    if args.json:
        print(json.dumps({"errors": errors, "warnings": warnings}, indent=2))
    else:
        for line in errors:
            print(f"error    {line}")
        for line in warnings:
            print(f"warning  {line}")
        print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
