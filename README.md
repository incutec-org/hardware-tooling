# Incutec hardware tooling

Hardware-agnostic automation and repository templates used by Incutec to
create, inspect, release, and hand off hardware projects. Product portfolios
such as OpenDrone keep their own policy, accepted exceptions, naming, and
publication orchestration in their own repositories.

The repository is intentionally split by concern:

```text
hardware/kicad/                 reusable KiCad inspection and export tools
hardware/release/               hardware release preparation and approval gates
templates/hardware-repository/ generic starting point for a hardware repo
tests/                          hardware-tool regression tests
```

## Requirements

Most board tools require KiCad's bundled Python because they import `pcbnew`:

```sh
KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3
$KPY hardware/kicad/render_board.py path/to/board.kicad_pcb --outdir images
```

Tools that use only the Python standard library run with `python3`. STEP repair
and post-processing tools additionally require `cadquery-ocp`. Read `--help`
before using a tool that writes source files; writes are opt-in unless the
command explicitly says otherwise.

## Board inspection

| Tool | Purpose |
| --- | --- |
| `netlist_extract.py` | Export component, sheet, and power-net summaries from a KiCad netlist. |
| `pcb_extract.py` | Export footprints, pad connectivity, and net counts from a board. |
| `connectivity_report.py` | Produce CSV and Markdown connectivity reports. |
| `check_models.py` | Check board and library 3D-model references before export. |
| `check_export.py` | Compare a fabrication export with its board and schematic. |

## Manufacturing data

| Tool | Purpose |
| --- | --- |
| `fab_export.py` | Run the KiCad Fabrication Toolkit headlessly. |
| `universal_bom.py` | Generate a manufacturer/MPN-aware BOM. |
| `quote_pack.py` | Assemble generic and supplier-formatted fabrication inputs. |
| `portal_gerbers.py` | Produce a compatibility copy for limited upload parsers. |
| `gerber_check.py` | Classify and validate a Gerber archive. |
| `assembly_drawing.py` | Render per-side assembly drawings with pin-1 markings. |
| `import_part.py` | Import an LCSC part into an explicitly selected project library. |
| `set_edgecuts_width.py` | Normalize `Edge.Cuts` widths; dry-run unless `--write` is passed. |

## Images and CAD exports

| Tool | Purpose |
| --- | --- |
| `render_board.py` | Render standardized top and bottom board PNGs. |
| `packaging_art.py` | Generate flat vector board artwork from PCB geometry. |
| `dimension_overlay.py` | Add dimensions to an existing board image. |
| `export_step.py` | Export normalized board STEP models. |
| `step_post.py` | Post-process STEP geometry using Open CASCADE. |
| `wrl_to_step.py` | Convert VRML meshes to STEP and repair model trees. |
| `model_audit.py` | Measure 3D-model cost and find replacement candidates. |
| `apply_models.py` | Apply an explicit model map or correction catalogue. |

All tools above live under `hardware/kicad/`. Batch operations require an
explicit root; project-specific values belong in the consuming repository.

## Release preparation

`hardware/release/kicad_release.py` composes the generic KiCad checks and
exports into a release-preparation chain. A design does not need an empty ERC
or DRC report: the command accepts a product or portfolio-owned approvals file
and passes findings at or below their reviewed maximum. A new finding or a
higher count blocks until a human reviews it.

```sh
python3 hardware/release/kicad_release.py path/to/board.kicad_pcb \
  --approved-violations path/to/approved-violations.json \
  --approval-key project/hardware/board
```

## Multi-board plugin

`hardware/kicad/multiboard/` is an MIT-licensed fork of Kicad-Multi-PCB for
projects in which one schematic drives several PCB layouts. The upstream
licence is retained as `LICENSE.upstream`.

```sh
sh hardware/kicad/multiboard/install.sh
$KPY hardware/kicad/multiboard/update.py path/to/project [board ...]
```

## Repository template

`templates/hardware-repository/` defines the hardware-agnostic repository
contract. A product organization may layer its own README, license, library,
status, community, and release profile on top; OpenDrone's concrete profile
lives in `OpenDrone-hw/hardware-template`.

## Ownership rule

A tool belongs here when its behavior works for unrelated hardware projects
through explicit inputs or configuration. Product names, portfolio topology,
accepted release exceptions, brand rules, and publication policy belong to
the product organization. Supplier conversations, orders, stock, test evidence,
and compliance records stay in their owning Incutec repositories.

MIT licensed. See `LICENSE`.
