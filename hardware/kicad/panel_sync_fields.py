"""Copy LCSC / Manufacturer / MPN onto the panel from its source boards.

The panel is a merged copy and its part fields lag the boards. Every panel
footprint is joined back to its source board through the refmap written by
panel_renumber.py, so the panel BOM is identical, part for part, to the
individual board BOMs.

Fields come from each board's released `_bom_universal.csv` first and from the
board footprints only as a fallback. That matters: a board BOM resolves the
manufacturer and MPN by joining its schematic, and a panel has no schematic to
join, so reading the footprints alone leaves most of the panel BOM with an LCSC
code and no manufacturer part number.

Dry run unless --write is passed.
"""
import argparse, collections, csv, os, sys
import pcbnew

FIELDS = ('LCSC', 'Manufacturer', 'MPN')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('panel')
    ap.add_argument('--refmap', required=True)
    ap.add_argument('--boards-root', required=True)
    ap.add_argument('--board-bom', action='append', default=[], metavar='BOARD=CSV',
                    help='released _bom_universal.csv for a board, repeatable; '
                         'the authoritative source of Manufacturer and MPN')
    ap.add_argument('--write', action='store_true')
    a = ap.parse_args()

    ref_to_src = {}
    for row in csv.DictReader(open(a.refmap)):
        ref_to_src[row['panel_ref']] = (row['board'], row['board_ref'])

    src = {}
    for board in sorted({b for b, _ in ref_to_src.values()}):
        path = os.path.join(a.boards_root, board, 'hardware', f'{board}.kicad_pcb')
        b = pcbnew.LoadBoard(path)
        for fp in b.GetFootprints():
            src[(board, fp.GetReference())] = dict(fp.GetFieldsText())
    # the released BOM wins: it carries the manufacturer and MPN the schematic
    # join resolved, which the footprints mostly do not have
    for spec in a.board_bom:
        board, _, path = spec.partition('=')
        if not os.path.exists(path):
            sys.exit(f"missing board BOM: {path}")
        with open(path, encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                for ref in (x.strip() for x in row['Designator'].split(',')):
                    if not ref:
                        continue
                    cur = src.setdefault((board, ref), {})
                    for name in FIELDS:
                        if row.get(name, '').strip():
                            cur[name] = row[name].strip()

    panel = pcbnew.LoadBoard(a.panel)
    changed, missing, mismatch = [], [], []
    for fp in panel.GetFootprints():
        if fp.IsExcludedFromBOM():
            continue
        key = ref_to_src.get(fp.GetReference())
        if key is None:
            missing.append(fp.GetReference())
            continue
        s = src.get(key)
        if s is None:
            missing.append(f'{fp.GetReference()} -> {key}')
            continue
        if s.get('Value') and fp.GetValue() != s.get('Value', fp.GetValue()):
            mismatch.append((fp.GetReference(), fp.GetValue(), s.get('Value')))
        cur = dict(fp.GetFieldsText())
        for name in FIELDS:
            want = s.get(name, '')
            if want and cur.get(name, '') != want:
                fp.SetField(name, want)
                if fp.HasField(name):
                    fp.GetField(name).SetVisible(False)
                changed.append(f'{fp.GetReference()} ({key[0]} {key[1]}) {name}: '
                               f'{cur.get(name, "") or "-"} -> {want}')

    for line in changed:
        print('  ', line)
    print(f'{len(changed)} field writes')
    if mismatch:
        print('VALUE MISMATCH panel vs board:', mismatch)
    if missing:
        print('no source for:', sorted(set(missing)))
    gaps = [(fp.GetReference(), fp.GetValue()) for fp in panel.GetFootprints()
            if not fp.IsExcludedFromBOM() and not dict(fp.GetFieldsText()).get('LCSC')]
    print('without LCSC after sync:', sorted(gaps))
    nompn = sorted(fp.GetReference() for fp in panel.GetFootprints()
                   if not fp.IsExcludedFromBOM() and not dict(fp.GetFieldsText()).get('MPN'))
    print(f'without MPN after sync: {len(nompn)}'
          + (f' {nompn[:12]}' if nompn else ''))
    if a.write:
        panel.Save(a.panel)
        print('panel saved')
    else:
        print('dry run, panel not saved')


main()
