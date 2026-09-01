# Incutec hardware tooling

This repository contains hardware-agnostic automation and repository templates
used across Incutec-managed hardware projects. OpenDrone product names, release
exceptions, portfolio policy, brand rules, and publication orchestration do not
belong here.

The user's request is the task; scripts must not discover and execute unrelated
work automatically. Keep behavior deterministic, parameterized, and safe to
rerun. Require explicit inputs and paths, validate destructive targets, avoid
embedded credentials or company records, and make external side effects opt-in.
Preserve documented compatibility and update tests when behavior changes.
