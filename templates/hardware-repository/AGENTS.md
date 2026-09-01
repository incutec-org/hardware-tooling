# Hardware repository

This repository contains one hardware product or reusable hardware library.
Use Incutec hardware tooling for repository-independent KiCad inspection,
export, rendering, and release mechanics. Keep product-specific scripts under
`hardware/tools/` and keep generated outputs separate from design sources.

Do not change nets, routing, placement, footprint assignment, release state,
or physical hardware unless the user's request explicitly includes that work.
Run the repository's documented ERC, DRC, and validation commands before
reporting a design or release ready.
