# Incutec hardware tooling

This repository contains hardware-agnostic automation and repository templates
used across Incutec-managed hardware projects. OpenDrone product names, release
exceptions, portfolio policy, brand rules, and publication orchestration do not
belong here.

Tools live in `hardware/`, repository templates in `templates/`. See
`README.md` for the per-tool index.

The user's request is the task; scripts must not discover and execute unrelated
work automatically. Keep behavior deterministic, parameterized, and safe to
rerun. Require explicit inputs and paths, validate destructive targets, avoid
embedded credentials or company records, and make external side effects opt-in.
Preserve documented compatibility and update tests when behavior changes.

Validate with `python3 -m pytest tests/`.

## Visual index

`OVERVIEW.md` is the diagram-only map of this repository: structure, pipelines,
and cross-repository handoffs. Change it in the same commit that adds, removes,
or renames a top-level directory, a pipeline step, or a handoff. Check with
`python3 overview_check.py .`.
