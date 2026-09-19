# Incutec hardware tooling

Hardware-agnostic automation and repository templates shared by every
Incutec hardware project. Product names, release exceptions, portfolio
policy, brand rules and publication orchestration belong to the product
repositories, not here.

Tools live in `hardware/`, repository templates in `templates/`,
documentation scanning in `docs/`, and the `OVERVIEW.md` checker at the root.
`README.md` is the per-tool index and states which tools need KiCad's bundled
Python (`$KPY`) and which extra packages.

## Rules

- A tool belongs here only when it works for unrelated hardware projects
  through explicit inputs. Require explicit paths, validate destructive
  targets, and make writes and external side effects opt-in flags.
- No embedded credentials, product names or company records in code.
- Keep behaviour deterministic and safe to rerun. Preserve documented
  compatibility; change the tests in the same commit as the behaviour.
- `OVERVIEW.md` is the diagram-only map of this repository. Change it in the
  same commit that adds, removes or renames a top-level directory, a
  pipeline step or a handoff.

## Validation

```
python3 -m pytest tests/
python3 overview_check.py .
```

## By task

- Render a board: `$KPY hardware/kicad/render_board.py <board.kicad_pcb> --outdir images`
  (`$KPY` per `README.md`, "Requirements"; `--sides top` for one side,
  `--help` for sizes).
- Run a fab export: `$KPY hardware/kicad/fab_export.py <board.kicad_pcb> [--name <archive>]`;
  reads `fabrication-toolkit-options.json` beside the board, writes
  `<board dir>/production/`, needs the JLCPCB Fabrication Toolkit plugin
  installed in KiCad.
- Prepare a release: `python3 hardware/release/kicad_release.py <board.kicad_pcb> --approved-violations <file> --approval-key <key>`
  (`README.md`, "Release preparation").
- Check an `OVERVIEW.md`: `python3 overview_check.py <repo-root>` (several
  roots in one call are accepted).
- Sync a shared `AGENTS.md` section: `python3 hardware/agents_section_sync.py --template <template.md> --section <name> --check <targets...>`;
  drop `--check` to write.
- Run the test suite: `python3 -m pytest tests/`.
