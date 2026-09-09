"""Make every reference on the OpenRX panel unique, in per-board blocks of 100.

Sub-boards are found by their X position on the strip. Board 0 keeps its
original references; each following board has 100 added to the numeric part
of every reference, so a panel reference maps back to its board by integer
division: U217 is board 2's U17. Mouse-bite footprints (REF**) are excluded
from the BOM and are left alone.

Writes <board>_refmap.csv next to the board and, with --write, saves the board.
"""
import argparse, collections, csv, os, re, sys
import pcbnew

# left edge, right edge, name; boards are laid out left to right on the strip
BOARDS = [
    (100.0, 118.0, 'OpenRX-Gemini'),
    (122.0, 133.5, 'OpenRX-Mono'),
    (137.0, 149.0, 'OpenRX-Lite-UFL'),
    (153.0, 164.5, 'OpenRX-Lite'),
]
BLOCK = 100
SKIP = {'REF**', 'G***'}
REF_RE = re.compile(r'^([A-Za-z]+)(\d+)$')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('board')
    ap.add_argument('--write', action='store_true')
    a = ap.parse_args()

    b = pcbnew.LoadBoard(a.board)
    rows, unplaced, collisions = [], [], collections.Counter()
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        x = fp.GetPosition().x / 1e6
        idx = next((i for i, (lo, hi, _) in enumerate(BOARDS) if lo <= x <= hi), None)
        if idx is None:
            unplaced.append((ref, round(x, 2)))
            continue
        if ref in SKIP:
            continue
        m = REF_RE.match(ref)
        if not m:
            unplaced.append((ref, round(x, 2)))
            continue
        prefix, num = m.group(1), int(m.group(2))
        new = f'{prefix}{num + idx * BLOCK}'
        collisions[new] += 1
        rows.append({'panel_ref': new, 'board': BOARDS[idx][2], 'board_ref': ref,
                     'value': fp.GetValue(), 'side': 'bottom' if fp.IsFlipped() else 'top'})
        if a.write:
            fp.SetReference(new)

    dup = {k: v for k, v in collisions.items() if v > 1}
    if dup:
        sys.exit(f'renumber would still collide: {dup}')
    if unplaced:
        print(f'not renumbered ({len(unplaced)}): {sorted(set(r for r, _ in unplaced))}')

    out = os.path.splitext(a.board)[0] + '_refmap.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['panel_ref', 'board', 'board_ref', 'value', 'side'])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r['board'], r['panel_ref'])))
    print(f'{len(rows)} references, all unique -> {out}')
    for name in (n for _, _, n in BOARDS):
        print(f'   {name}: {sum(1 for r in rows if r["board"] == name)}')
    if a.write:
        b.Save(a.board)
        print('board saved')
    else:
        print('dry run, board not saved')


main()
