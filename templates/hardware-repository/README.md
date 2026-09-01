# Hardware repository template

Use this directory as the hardware-agnostic foundation of an Incutec-managed
hardware repository. Product organizations add their own identity, licensing,
shared-library configuration, publication workflow, and release profile.

```text
repository/
├── AGENTS.md
├── README.md
├── hardware/
│   └── tools/          product-specific scripts only
├── images/
└── release/
    └── approved-violations.json
```

Reusable KiCad commands must come from Incutec hardware tooling rather than
being copied into each product repository. The product repository owns its
design sources, product configuration, and product-only tools.

ERC and DRC reports do not need to be empty. `release/approved-violations.json`
records only findings a human has reviewed; a new type or increased count is a
release regression.
