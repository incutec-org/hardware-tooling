"""Copy LCSC / Manufacturer / MPN onto the panel from the four source boards.

The panel is a merged copy and its part fields lag the boards. Every panel
footprint is joined back to its source board through the refmap written by
renumber_panel.py, so the panel BOM is identical, part for part, to the four
individual board BOMs. Dry run unless --write is passed.
"""
import argparse, collections, csv, os, sys
import pcbnew

FIELDS = ('LCSC', 'Manufacturer', 'MPN')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('panel')
    ap.add_argument('--refmap', required=True)
    ap.add_argument('--boards-root', required=True)
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
    if a.write:
        panel.Save(a.panel)
        print('panel saved')
    else:
        print('dry run, panel not saved')


main()
