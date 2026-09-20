#!/usr/bin/env python3
"""Assembly review artifacts for a fab: interactive BOM, assembly PDF, and a
schematic-to-board reference report.

    $KPY assembly_pack.py <board.kicad_pcb> [--out DIR] [--stem NAME]
                          [--sch FILE] [--no-ibom] [--no-pdf]

Written after every turnkey fab asked the same three questions off a Gerber
package alone: where is pin 1, what part goes in this position, and why does
this reference appear twice. The positions file and the assembly maps answer
the first two only if the reviewer opens them side by side, and the SMT
department routinely never receives them.

Produces, next to the other release artifacts:

  <stem>_ibom.html      one self-contained page: board render both sides,
                        pin 1 highlighted on every part, searchable BOM with
                        MPN and LCSC, click a line to light up the part.
                        Opens in a browser with no install and no network.
                        Silkscreen only: drawing the fabrication layer as well
                        covers parts with the filled body outlines some
                        footprints carry on F.Fab.
  <stem>_assembly.pdf   the assembly drawings as one printable vector file,
                        one page per side, the attachment a fab expects on
                        the order.
  reference report      returned to the caller, not written here: duplicate
                        board references and board references with no
                        schematic symbol.

The interactive BOM is InteractiveHtmlBom, shipped as a KiCad 3rdparty plugin.
Set INCUTEC_IBOM to override discovery. Schematic fields (MPN, LCSC,
Manufacturer) reach it through a kicad-cli netlist export, so a board whose
schematic is absent still renders, with those columns empty.

Read only: nothing here writes to the board, the schematic or the project.

Determinism: the interactive BOM is byte-stable, so regenerating it from the
same board gives the same hash. The assembly PDF is not, because kicad-cli
stamps each export and honours no SOURCE_DATE_EPOCH. Treat the PDF as a
convenience attachment, not as a hash-pinned release artifact.
"""
import argparse, collections, glob, os, re, shutil, subprocess, sys, tempfile

try:
    import pcbnew
except ImportError:  # discovery and formatting work without KiCad
    pcbnew = None


def require_pcbnew():
    if pcbnew is None:
        sys.exit("needs KiCad's bundled Python (pcbnew); see README 'Requirements'")
    return pcbnew

EXTRA_FIELDS = ('LCSC', 'MPN', 'Manufacturer')
SHOW_FIELDS = ('Value', 'Footprint', 'MPN', 'LCSC')


def kicad_cli():
    """kicad-cli next to the running pcbnew, else on PATH."""
    for candidate in (
            '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli',
            shutil.which('kicad-cli')):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def kicad_version():
    """(major, minor) of the running pcbnew, for plugin selection."""
    if pcbnew is None:
        return (0, 0)
    try:
        parts = re.findall(r'\d+', pcbnew.GetBuildVersion())
        return (int(parts[0]), int(parts[1])) if len(parts) > 1 else (0, 0)
    except Exception:
        return (0, 0)


def find_ibom():
    """generate_interactive_bom.py matching the running KiCad.

    The plugin is installed per KiCad version and calls the pcbnew API
    directly, so a plugin from another major version fails on an API it does
    not know. Version directories are compared numerically: a lexical sort
    puts "10.0" before "9.0" and silently selects the wrong one.
    """
    override = os.environ.get('INCUTEC_IBOM')
    if override:
        return override if os.path.isfile(override) else None
    patterns = ('~/Documents/KiCad/*/3rdparty/plugins/*InteractiveHtmlBom*',
                '~/.local/share/kicad/*/3rdparty/plugins/*InteractiveHtmlBom*')
    found = {}
    for pattern in patterns:
        for root in glob.glob(os.path.expanduser(pattern)):
            entry = os.path.join(root, 'generate_interactive_bom.py')
            if not os.path.isfile(entry):
                continue
            digits = re.findall(r'\d+', os.path.basename(
                os.path.dirname(os.path.dirname(os.path.dirname(root)))))
            version = tuple(int(d) for d in digits[:2]) or (0, 0)
            found.setdefault(version, entry)
    if not found:
        return None
    running = kicad_version()
    for version in sorted(found, reverse=True):
        if version[0] == running[0]:
            return found[version]
    return found[max(found)]


def find_schematic(board_path, explicit=None):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    candidate = os.path.splitext(board_path)[0] + '.kicad_sch'
    if os.path.isfile(candidate):
        return candidate
    sheets = glob.glob(os.path.join(os.path.dirname(board_path), '*.kicad_sch'))
    return sheets[0] if len(sheets) == 1 else None


def export_netlist(sch_path, out_dir):
    """kicadxml netlist, so the interactive BOM can show MPN and LCSC."""
    cli = kicad_cli()
    if not cli or not sch_path:
        return None
    target = os.path.join(out_dir, 'netlist.xml')
    result = subprocess.run(
        [cli, 'sch', 'export', 'netlist', '--format', 'kicadxml',
         '-o', target, sch_path],
        capture_output=True, text=True)
    return target if result.returncode == 0 and os.path.isfile(target) else None


def reference_report(board_path, netlist_xml=None):
    """{'duplicates': {ref: count}, 'unsourced': [ref], 'counts': (...)}.

    A duplicate reference is two footprints sharing a name: the fab cannot tell
    the two apart in a BOM line, which is why the supplier BOM renames them.
    An unsourced reference is a footprint with no schematic symbol, so ERC and
    the netlist cannot see it.
    """
    board = require_pcbnew().LoadBoard(board_path)
    refs = [fp.GetReference() for fp in board.GetFootprints()]
    counts = collections.Counter(refs)
    report = {
        'duplicates': {r: n for r, n in sorted(counts.items()) if n > 1},
        'unsourced': [],
        'placements': len(refs),
        'unique': len(counts),
        'symbols': None,
    }
    if netlist_xml and os.path.isfile(netlist_xml):
        with open(netlist_xml, errors='replace') as handle:
            symbols = set(re.findall(r'<comp ref="([^"]+)"', handle.read()))
        report['symbols'] = len(symbols)
        report['unsourced'] = sorted(set(refs) - symbols)
    return report


def render_ibom(board_path, stem, out_dir, sch_path=None, netlist_xml=None):
    """Write <stem>_ibom.html. Returns the path, or None when unavailable."""
    entry = find_ibom()
    if not entry:
        return None
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        if netlist_xml is None:
            netlist_xml = export_netlist(
                find_schematic(board_path, sch_path), work)
        command = [
            sys.executable, entry, board_path,
            '--no-browser', '--dest-dir', os.path.abspath(out_dir),
            '--name-format', f'{stem}_ibom',
            '--highlight-pin1', 'all',
            '--layer-view', 'FB',
            '--bom-view', 'left-right',
            '--sort-order', 'C,R,L,D,Q,U,J,TP',
            '--extra-fields', ','.join(EXTRA_FIELDS),
            '--show-fields', ','.join(SHOW_FIELDS),
            '--group-fields', 'Value,Footprint,MPN',
        ]
        if netlist_xml:
            command += ['--extra-data-file', netlist_xml]
        result = subprocess.run(command, capture_output=True, text=True)
    target = os.path.join(out_dir, f'{stem}_ibom.html')
    if result.returncode != 0 or not os.path.isfile(target):
        tail = (result.stderr or result.stdout or '').strip().splitlines()
        detail = tail[-1] if tail else 'no output'
        print(f'interactive BOM not produced: {detail}', file=sys.stderr)
        return None
    return target


def svg_to_pdf(svg_path, pdf_path):
    """First converter that produces a file wins."""
    for tool, command in (
            ('rsvg-convert', ['rsvg-convert', '-f', 'pdf', '-o', pdf_path, svg_path]),
            ('cairosvg', ['cairosvg', svg_path, '-o', pdf_path]),
            ('magick', ['magick', svg_path, pdf_path])):
        if not shutil.which(tool):
            continue
        subprocess.run(command, capture_output=True)
        if os.path.isfile(pdf_path) and os.path.getsize(pdf_path):
            return True
    return False


def render_assembly_pdf(board_path, stem, out_dir):
    """Write <stem>_assembly.pdf, one page per side. Returns path or None.

    Built from the assembly drawings rather than a kicad-cli layer plot. The
    drawings already carry what a fab reviewer needs, a title, a legend, pin 1
    in red and not-placed parts hatched, with the board filling the page. A
    layer plot puts a 38 mm board 1:1 in the middle of an A4 frame with an
    empty title block, which is unreadable.
    """
    os.makedirs(out_dir, exist_ok=True)
    sides = [os.path.join(out_dir, f'{stem}_assembly_{side}.svg')
             for side in ('top', 'bottom')]
    if not all(os.path.isfile(path) for path in sides):
        try:
            import assembly_drawing
            assembly_drawing.render(board_path, stem, out_dir, dpi=300, png=False)
        except Exception:
            return None
    sides = [path for path in sides if os.path.isfile(path)]
    if not sides:
        return None
    target = os.path.join(out_dir, f'{stem}_assembly.pdf')
    with tempfile.TemporaryDirectory() as work:
        pages = []
        for index, svg in enumerate(sides):
            page = os.path.join(work, f'{index}.pdf')
            if svg_to_pdf(svg, page):
                pages.append(page)
        if not pages:
            return None
        if len(pages) == 1:
            shutil.copy2(pages[0], target)
        elif not merge_pdf(pages, target):
            shutil.copy2(pages[0], target)
    return target if os.path.isfile(target) else None


def merge_pdf(pages, target):
    """Concatenate with whatever is available; one page is better than none."""
    for tool, command in (
            ('pdfunite', ['pdfunite', *pages, target]),
            ('gs', ['gs', '-dBATCH', '-dNOPAUSE', '-q', '-sDEVICE=pdfwrite',
                    f'-sOutputFile={target}', *pages])):
        if shutil.which(tool):
            if subprocess.run(command, capture_output=True).returncode == 0:
                return os.path.isfile(target)
    return False


def render(board_path, stem, out_dir, sch_path=None, ibom=True, pdf=True):
    """Everything this module makes, plus the reference report."""
    with tempfile.TemporaryDirectory() as work:
        netlist = export_netlist(find_schematic(board_path, sch_path), work)
        made = {
            'ibom': render_ibom(board_path, stem, out_dir, sch_path, netlist)
            if ibom else None,
            'pdf': render_assembly_pdf(board_path, stem, out_dir)
            if pdf else None,
            'references': reference_report(board_path, netlist),
        }
    return made


def format_report(report):
    """The reference report as DFM-report lines."""
    lines = [
        f"A1 {'WARN' if report['duplicates'] else 'PASS'} references: "
        f"{report['placements']} placements, {report['unique']} unique"
        + (f", {len(report['duplicates'])} duplicated"
           if report['duplicates'] else '')
    ]
    if report['duplicates']:
        listed = ', '.join(f'{r} x{n}' for r, n in report['duplicates'].items())
        lines.append(f"A1 WARN duplicate board references: {listed}")
    if report['symbols'] is None:
        lines.append('A2 WARN schematic not read: reference sync unchecked')
    elif report['unsourced']:
        lines.append(
            f"A2 WARN {len(report['unsourced'])} placements have no schematic "
            f"symbol: {', '.join(report['unsourced'])}")
    else:
        lines.append(f"A2 PASS every placement has a schematic symbol "
                     f"({report['symbols']} symbols)")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('board')
    parser.add_argument('--out', default=None)
    parser.add_argument('--stem', default=None)
    parser.add_argument('--sch', default=None)
    parser.add_argument('--no-ibom', action='store_true')
    parser.add_argument('--no-pdf', action='store_true')
    args = parser.parse_args()

    board_path = os.path.abspath(args.board)
    stem = args.stem or os.path.splitext(os.path.basename(board_path))[0]
    out_dir = os.path.abspath(args.out or os.path.dirname(board_path))

    made = render(board_path, stem, out_dir, args.sch,
                  ibom=not args.no_ibom, pdf=not args.no_pdf)
    for key in ('ibom', 'pdf'):
        print(f"{key}: {made[key] or 'not produced'}")
    for line in format_report(made['references']):
        print(line)


if __name__ == '__main__':
    main()
