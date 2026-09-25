# Mechanical repository

This repository holds one mechanical product. The design source is the Onshape
document named in `cad/onshape.json`; this repository holds the link, the
released exports and the parts list. Use the Onshape skill for CAD work.

- Never edit a file under `releases/`. Fix the model in Onshape, then release
  a new revision with `onshape_release.py`.
- One release per commit, adding only `releases/<rev>/`.
- Keep `parts.csv` and `hardware.csv` in step with the model: a part added or
  removed in Onshape changes the lists in the same pull request.
- Supplier quotes, prices and RFQ records never enter this repository.
- Validate with `python3 <scripts>/hardware/mechanical_check.py .` before a PR.
