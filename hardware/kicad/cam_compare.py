#!/usr/bin/env python3
"""Compare a fabricator's CAM ("work"/"working") Gerber set with the released
design Gerbers, and review the result in an interactive workspace.

    python3 cam_compare.py --design release.zip --cam fab_cam.rar -o outdir
    python3 cam_compare.py --serve -o outdir [--port 8765]

The first form analyses and writes outdir/report.txt, outdir/findings.json and
outdir/index.html. The second serves that directory on 127.0.0.1 so the
workspace saves edits to outdir/review.json and can re-run the analysis with
new tolerances; opened directly from disk, the workspace keeps edits in the
browser and exports them. The workspace shows the analysis (design, CAM,
overlay, flagged differences, whole CAM panel, holes, markers, measure, notes)
beside an editable report with per-finding disposition and comments, and
exports Markdown, standalone HTML, PDF (print), an engineering-question CSV
for the fab, and the review state as JSON.

Inputs are file sets, not EDA projects: a zip, a directory, or any archive
`bsdtar` can read (rar, 7z, tgz). Layers are identified from X2 FileFunction
attributes, then from KiCad, Protel and Genesis/InCAM naming (gtl, g2, l2, tl,
drl, 2rl, sk, gko, ko, ...). Fab CAM files are commonly flattened panels with
no attributes; the tool finds every board instance in the panel from the drill
pattern (any rotation or mirror), so the fab's coordinate system, panel layout
and step-and-repeat do not matter.

Checks, per board instance found in the CAM panel:
  C1  layer set: same copper layer count, every mask/silk/paste role present
  C2  placement: instances found, rotation/mirror, rows, columns, pitch
  C3  drills: design diameter -> CAM tool table (plating compensation, CAM
      drills smaller than the finished size flagged), holes moved by more than
      --hole-tol, holes absent, CAM holes with no design hole, and which design
      holes each extra CAM drill layer (via plug, second drill) carries
  C4  geometry: per layer, copper/mask/silk/paste the CAM added or removed
      beyond --tol inside the board; the band within --edge-band of the
      outline and pads removed around plated holes are reported apart; copper
      edge offset (etch compensation) per layer
  C5  inner layer order: which CAM inner layer best matches each design one
  C6  connectivity: nets from copper islands joined by plated holes, mapped by
      overlap; opens and shorts introduced by CAM, the location of each break
      and what the separated copper carries (plated holes, exposed pads)
  C7  panel consistency: every CAM instance matches the first, per layer

Status: PASS, INFO (a fact), REVIEW (a difference a human must accept or
query), FAIL (missing layer, board not found, hole absent, copper island
absent, an open that cuts off holes or pads, or a short). Exit 1 on any FAIL.

Requires numpy, scipy, pillow and gerbonara 1.6.3; gerbonara's declared web
dependencies are not used:  python3 -m pip install --no-deps gerbonara==1.6.3
Rasters come straight from gerbonara primitives; four gerbonara 1.6.3 defects
that CAM output exercises (rotated rectangles, step-and-repeat, regions in
step-and-repeat, draws with rectangular apertures) are corrected here.
"""
import argparse, collections, copy, json, math, os, re, shutil, subprocess, sys, tempfile, warnings, zipfile

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.spatial import cKDTree

from gerbonara import ExcellonFile, GerberFile
from gerbonara import graphic_primitives as _gp
from gerbonara import rs274x as _rs274x
from gerbonara.utils import MM, approximate_arc

warnings.filterwarnings('ignore', category=SyntaxWarning)

# The patches below correct gerbonara 1.6.3 behaviour that CAM output exercises; they were verified
# against an independent renderer (tracespace) on real fab data. Re-verify before trusting another version.
try:
    from importlib.metadata import version as _version
    if _version('gerbonara') != '1.6.3':
        print(f'warning: gerbonara {_version("gerbonara")} installed; cam_compare patches target 1.6.3', file=sys.stderr)
except Exception:
    pass


def _rectangle_arc_poly(self):
    # gerbonara 1.6.3 Rectangle.to_arc_poly returns the axis-aligned bounding box of a rotated
    # rectangle, which grows every rotated pad (e.g. KiCad RoundRect macro edges at 45 degrees).
    # Rotation is counter-clockwise radians about the centre, as in gerbonara's own macro code.
    c, s = math.cos(self.rotation), math.sin(self.rotation)
    hw, hh = self.w / 2, self.h / 2
    return _gp.ArcPoly([(self.x + dx * c - dy * s, self.y + dx * s + dy * c)
                        for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))],
                       polarity_dark=self.polarity_dark)


_gp.Rectangle.to_arc_poly = _rectangle_arc_poly


class _StepRepeatBlock(list):
    # gerbonara 1.6.3 tests the open block with `if self.step_repeat_objects:`, so an empty block
    # is falsy and every object inside %SR% goes straight to the file unrepeated
    def __bool__(self):
        return True


def _parse_step_repeat(self, match):
    # also: offsets are in file units (gerbonara applied them as mm), and a new %SR...% or
    # %SRX1Y1...% closes the open block, per the Gerber specification
    if self.step_repeat_coords is not None:
        x, y, i, j = self.step_repeat_coords
        block, self.step_repeat_coords, self.step_repeat_objects = self.step_repeat_objects, None, None
        for nx in range(x):
            for ny in range(y):
                for obj in block:
                    new = copy.copy(obj)
                    new.offset(i * nx, j * ny, self.file_settings.unit)
                    self.target.objects.append(new)
    if match['coords']:
        x, y = int(match['X']), int(match['Y'])
        if x * y > 1:
            self.step_repeat_coords = (x, y, float(match['I']), float(match['J']))
            self.step_repeat_objects = _StepRepeatBlock()


def _parse_region_end(self, _match):
    # gerbonara 1.6.3 appends a finished region to the file even inside an open %SR% block
    if self.current_region is None:
        raise SyntaxError('Region end command (G37) outside of region')
    if self.current_region:
        dest = self.step_repeat_objects if self.step_repeat_objects is not None else self.target.objects
        dest.append(self.current_region)
    self.current_region = None


_orig_parse_eof = _rs274x.GerberParser._parse_eof


def _parse_eof(self, match):
    # a block still open at M02 is closed by it (CAM output often never closes the last %SR%)
    if self.step_repeat_coords is not None:
        _parse_step_repeat(self, {'coords': None})
    _orig_parse_eof(self, match)


_rs274x.GerberParser._parse_step_repeat = _parse_step_repeat
_rs274x.GerberParser._parse_eof = _parse_eof
_rs274x.GerberParser._parse_region_end = _parse_region_end

# ------------------------------------------------------------------ inputs

SKIP_EXT = ('.pdf', '.txt', '.csv', '.xlsx', '.xls', '.md', '.json', '.png', '.jpg', '.tgz', '.zip', '.rar',
            '.gbrjob', '.command', '.html', '.ipc', '.d356', '.step', '.stp')


def gather(src, tmp):
    """[(name, bytes)] from a directory, zip, or bsdtar-readable archive."""
    if os.path.isdir(src):
        root = src
    elif zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            return [(n, z.read(n)) for n in z.namelist() if not n.endswith('/') and '__MACOSX' not in n]
    else:
        if not shutil.which('bsdtar'):
            sys.exit(f'bsdtar is needed to unpack {src}')
        subprocess.run(['bsdtar', '-xf', src, '-C', tmp], check=True)
        root = tmp
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if not d.startswith('.') and d != '__MACOSX')
        for f in sorted(fn):
            if not f.startswith('.'):
                with open(os.path.join(dp, f), 'rb') as fh:
                    out.append((os.path.relpath(os.path.join(dp, f), root), fh.read()))
    return out


# name keys: (role, side, copper index); copper index 1 = top, 99 = bottom
NAME_KEYS = {
    'gtl': ('cu', 'top', 1), 'tl': ('cu', 'top', 1), 'f_cu': ('cu', 'top', 1), 'top': ('cu', 'top', 1),
    'gbl': ('cu', 'bot', 99), 'bl': ('cu', 'bot', 99), 'b_cu': ('cu', 'bot', 99), 'bottom': ('cu', 'bot', 99),
    'gts': ('mask', 'top', 0), 'ts': ('mask', 'top', 0), 'f_mask': ('mask', 'top', 0),
    'gbs': ('mask', 'bot', 0), 'bs': ('mask', 'bot', 0), 'b_mask': ('mask', 'bot', 0),
    'gto': ('silk', 'top', 0), 'to': ('silk', 'top', 0), 'f_silkscreen': ('silk', 'top', 0), 'f_silks': ('silk', 'top', 0),
    'gbo': ('silk', 'bot', 0), 'bo': ('silk', 'bot', 0), 'b_silkscreen': ('silk', 'bot', 0), 'b_silks': ('silk', 'bot', 0),
    'gtp': ('paste', 'top', 0), 'tp': ('paste', 'top', 0), 'f_paste': ('paste', 'top', 0),
    'gbp': ('paste', 'bot', 0), 'bp': ('paste', 'bot', 0), 'b_paste': ('paste', 'bot', 0),
    'gko': ('outline', '', 0), 'ko': ('outline', '', 0), 'gm1': ('outline', '', 0), 'gml': ('outline', '', 0),
    'edge_cuts': ('outline', '', 0), 'outline': ('outline', '', 0),
    'drl': ('drill', '', 0), 'xln': ('drill', '', 0), 'exc': ('drill', '', 0), 'ncd': ('drill', '', 0),
}


def classify(name, text, excellon):
    """(role, side, index, basis). Roles: cu mask silk paste outline drill map other."""
    m = re.search(r'(?:%TF|G04 #@! TF|; #@! TF)\.FileFunction,([^*\n]+)', text[:6000])
    if m:
        ff = [f.strip() for f in m.group(1).split(',')]
        side = 'top' if 'Top' in ff else 'bot' if 'Bot' in ff else ''
        if ff[0] == 'Copper':
            idx = int(ff[1][1:]) if len(ff) > 1 and ff[1][1:].isdigit() else 50
            return 'cu', side or 'inner', 99 if side == 'bot' else idx, 'X2'
        table = {'Soldermask': 'mask', 'Legend': 'silk', 'Paste': 'paste'}
        if ff[0] in table and side:
            return table[ff[0]], side, 0, 'X2'
        if ff[0] == 'Profile':
            return 'outline', '', 0, 'X2'
        if ff[0] in ('Plated', 'NonPlated'):
            return 'drill', 'np' if ff[0] == 'NonPlated' else 'pth', 0, 'X2'
        if ff[0].startswith('Drillmap'):
            return 'map', '', 0, 'X2'
    base = os.path.basename(name).lower()
    norm = base.replace('-', '_').replace(' ', '_')
    if 'drl_map' in norm or 'drill_map' in norm:
        return 'map', '', 0, 'name'
    stem, ext = os.path.splitext(base)
    ext = ext.lstrip('.')
    for key in (ext, stem):
        if key in NAME_KEYS:
            role, side, idx = NAME_KEYS[key]
            if role == 'drill':
                side = 'np' if 'npth' in norm else 'pth' if 'pth' in norm else ''
            return role, side, idx, 'name'
        mm = re.fullmatch(r'(?:g|gl|l)(\d+)', key)  # KiCad/Protel .g1 = In1 = layer 2; Genesis l2 = layer 2
        if mm:
            n = int(mm.group(1))
            return 'cu', 'inner', n + 1 if key.startswith('g') and not key.startswith('gl') else n, 'name'
    for key, (role, side, idx) in NAME_KEYS.items():
        if len(key) > 3 and key in norm:
            return role, side, idx, 'name'
    mm = re.search(r'in(\d+)_cu', norm)
    if mm:
        return 'cu', 'inner', int(mm.group(1)) + 1, 'name'
    if excellon:
        return 'drill', 'np' if 'npth' in norm else 'pth' if 'pth' in norm else '', 0, 'content'
    key = ext or stem
    if re.fullmatch(r'\d?[a-z]{1,3}', key):  # CAM auxiliary layer, e.g. 2rl, sk
        return 'aux', key, 0, 'name'
    return 'other', '', 0, 'none'


def normalize_gerber(text):
    """Rewrite AM primitive 22 (lower-left line, deprecated), which gerbonara
    rejects, as the equivalent primitive 21 (centre line; both rotate about
    the macro origin). Geometry is unchanged."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    def p22(m):
        f = m.group(1).split(',')
        w, h, x, y = (float(v) for v in f[1:5])
        rot = f[5] if len(f) > 5 else '0'
        return '21,%s,%.8f,%.8f,%.8f,%.8f,%s*' % (f[0], w, h, x + w / 2, y + h / 2, rot)
    return re.sub(r'(?m)^\s*22,([^*]+)\*', p22, text)


class Layer:
    def __init__(self, name, data):
        self.name = name
        text = data.decode('latin-1')
        head = text[:20000]
        self.is_gerber = bool(re.search(r'%FS[LTD]?[AI]', head) or re.search(r'%MO(IN|MM)', head))
        self.is_excellon = not self.is_gerber and bool(re.search(r'^M48', head, re.M) or re.search(r'^T\d+C[\d.]', head, re.M))
        self.role, self.side, self.index, self.basis = classify(name, text, self.is_excellon)
        self.file = None
        self.error = None
        if self.role in ('map', 'other') or not (self.is_gerber or self.is_excellon):
            return
        try:
            if self.is_gerber:
                self.file = GerberFile.from_string(normalize_gerber(text), filename=name)
            else:
                self.file = ExcellonFile.from_string(text, filename=name)
        except Exception as e:  # reported, never silently dropped
            self.error = f'{type(e).__name__}: {e}'

    @property
    def label(self):
        return os.path.basename(self.name)

    def holes(self, plated_default=None):
        """(N,4) array x, y, diameter, plated(1/0/-1) in mm; slots by centre."""
        rows = []
        if self.file is None:
            return np.zeros((0, 4))
        pl = {'pth': 1, 'np': 0}.get(self.side, -1 if plated_default is None else plated_default)
        for o in self.file.objects:
            ap = getattr(o, 'aperture', None)
            d = getattr(ap, 'diameter', None)
            if d is None:
                continue
            if ap.unit is not None and ap.unit != MM:
                d = ap.unit.convert_to(MM, d)
            pc = pl if getattr(o, 'plated', None) is None else int(bool(o.plated))
            if hasattr(o, 'x1'):
                x1, y1, x2, y2 = (o.unit.convert_to(MM, v) if o.unit else v for v in (o.x1, o.y1, o.x2, o.y2))
                rows.append(((x1 + x2) / 2, (y1 + y2) / 2, d, pc))
            else:
                x, y = (o.unit.convert_to(MM, v) if o.unit else v for v in (o.x, o.y))
                rows.append((x, y, d, pc))
        return np.array(rows, dtype=float).reshape(-1, 4)


def load_set(src):
    with tempfile.TemporaryDirectory() as tmp:
        items = gather(src, tmp)
    layers = []
    for name, data in items:
        if name.lower().endswith(SKIP_EXT):
            continue
        L = Layer(name, data)
        if L.role in ('map', 'other') and not L.error:
            continue
        layers.append(L)
    return layers


# ------------------------------------------------------------------ raster

class Grid:
    def __init__(self, x0, y0, x1, y1, res):
        self.res = res
        self.x0, self.y1 = x0, y1
        self.w = int(math.ceil((x1 - x0) / res)) + 1
        self.h = int(math.ceil((y1 - y0) / res)) + 1

    def to_px(self, pts):
        return [((x - self.x0) / self.res, (self.y1 - y) / self.res) for x, y in pts]

    def centres(self):
        cols = self.x0 + (np.arange(self.w) + 0.5) * self.res
        rows = self.y1 - (np.arange(self.h) + 0.5) * self.res
        return np.meshgrid(cols, rows)

    def mm(self, r, c):
        return self.x0 + (c + 0.5) * self.res, self.y1 - (r + 0.5) * self.res


def _hull(points):
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]


def _swept(obj, err):
    """Polygons for a draw made with a rectangle or obround aperture: the aperture shape swept
    along the path (Gerber spec). gerbonara 1.6.3 renders these as round-capped strokes, which
    turns e.g. a painted 4 mm square pad into a 5.8 mm disc."""
    ap = getattr(obj, 'aperture', None)
    kind = type(ap).__name__
    if kind not in ('RectangleAperture', 'ObroundAperture') or getattr(ap, 'hole_dia', None):
        return None
    w, h = (ap.unit.convert_to(MM, v) if ap.unit is not None else v for v in (ap.w, ap.h))
    rot = getattr(ap, 'rotation', 0) or 0
    if kind == 'RectangleAperture':
        shape = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    else:  # stadium: two semicircles joined along the long axis
        r, a = min(w, h) / 2, abs(w - h) / 2
        n = max(8, int(math.pi * r / max(err, 1e-3)))
        half = [(a + r * math.cos(t), r * math.sin(t)) for t in
                (math.pi * (k / n - 0.5) for k in range(n + 1))]
        shape = half + [(-x, -y) for x, y in half]
        if h > w:
            shape = [(-y, x) for x, y in shape]
    if rot:
        c, s_ = math.cos(rot), math.sin(rot)
        shape = [(x * c - y * s_, x * s_ + y * c) for x, y in shape]
    u = obj.unit
    conv = (lambda v: u.convert_to(MM, v)) if u is not None else (lambda v: v)
    if hasattr(obj, 'cx'):  # arc: sample the path
        x1, y1, x2, y2 = conv(obj.x1), conv(obj.y1), conv(obj.x2), conv(obj.y2)
        cx_, cy_ = x1 + conv(obj.cx), y1 + conv(obj.cy)
        path = approximate_arc(cx_, cy_, x1, y1, x2, y2, obj.clockwise, max_error=err)
    else:
        path = [(conv(obj.x1), conv(obj.y1)), (conv(obj.x2), conv(obj.y2))]
    polys = []
    for (xa, ya), (xb, yb) in zip(path, path[1:] or path):
        polys.append(_hull([(xa + dx, ya + dy) for dx, dy in shape] + [(xb + dx, yb + dy) for dx, dy in shape]))
    return polys


def rasterize(layer, grid):
    img = Image.new('1', (grid.w, grid.h), 0)
    draw = ImageDraw.Draw(img)
    err = grid.res / 3
    for obj in layer.file.objects:
        if type(obj).__name__ in ('Line', 'Arc'):
            polys = _swept(obj, err)
            if polys is not None:
                for pts in polys:
                    if len(pts) >= 3:
                        draw.polygon(grid.to_px(pts), fill=1 if obj.polarity_dark else 0)
                continue
        for prim in obj.to_primitives(unit=MM):
            try:
                poly = prim.to_arc_poly().approximate_arcs(max_error=err)
            except Exception:
                continue
            pts = poly.outline
            if len(pts) < 2:
                continue
            px = grid.to_px(pts)
            if len(px) == 2:
                draw.line(px, fill=1 if prim.polarity_dark else 0)
            else:
                draw.polygon(px, fill=1 if prim.polarity_dark else 0)
    return np.array(img, dtype=bool)


def disk(radius_px):
    r = max(int(round(radius_px)), 0)
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def board_region(outline_raster):
    """Inside of a closed outline: everything not 4-connected to the grid border."""
    stroke = ndimage.binary_dilation(outline_raster, iterations=2)
    lab, n = ndimage.label(~stroke)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    inside = ~np.isin(lab, list(border)) & (lab > 0)
    closed = inside.any()
    return (inside | outline_raster) if closed else None


# ------------------------------------------------------------------ alignment

ORIENT = [(r, m) for m in (False, True) for r in (0, 90, 180, 270)]


def omat(rot, mirror):
    a = math.radians(rot)
    R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    if mirror:
        R = R @ np.array([[-1.0, 0.0], [0.0, 1.0]])
    return np.round(R, 12)


def find_instances(dxy, cxy, hole_tol, move_tol=0.15):
    """Every placement p_cam = M @ p_design + t of the design hole pattern in
    the CAM hole pattern. Returns (rot, mirror, M, [t...], [matched...])."""
    if len(dxy) < 3 or len(cxy) < 3:
        return None
    tree = cKDTree(cxy)
    rng = np.random.default_rng(0)
    probe = dxy[rng.choice(len(dxy), size=min(len(dxy), 60), replace=False)]
    best = None
    for rot, mir in ORIENT:
        M = omat(rot, mir)
        pd = probe @ M.T
        cand = (cxy[None, :, :] - pd[:, None, :]).reshape(-1, 2)
        q = np.round(cand / 0.1).astype(np.int64)
        keys, counts = np.unique(q[:, 0] * 10_000_000 + q[:, 1], return_counts=True)
        order = np.argsort(-counts)[:400]
        found = []
        full = dxy @ M.T
        for k in order:
            if counts[k] < 0.3 * len(probe):
                break
            sel = (q[:, 0] * 10_000_000 + q[:, 1]) == keys[k]
            t0 = cand[sel].mean(axis=0)
            d, i = tree.query(full + t0, distance_upper_bound=hole_tol * 4)
            ok = np.isfinite(d)
            if ok.sum() < 0.9 * len(dxy):
                continue
            t = np.median(cxy[i[ok]] - full[ok], axis=0)  # refine; median ignores holes the CAM moved
            d, i = tree.query(full + t, distance_upper_bound=max(hole_tol, move_tol))  # CAM may move holes
            n_ok = int(np.isfinite(d).sum())
            if n_ok >= 0.9 * len(dxy) and all(np.hypot(*(t - f)) > 1.0 for f, _ in found):
                found.append((t, n_ok))
        score = sum(n for _, n in found)
        if found and (best is None or score > best[0]):
            best = (score, rot, mir, M, found)
    if best is None:
        return None
    _, rot, mir, M, found = best
    found.sort(key=lambda f: (-round(f[0][1], 1), round(f[0][0], 1)))  # rows top to bottom, then left to right
    return rot, mir, M, [f[0] for f in found], [f[1] for f in found]


def sample_unit(cam_raster, cam_grid, dgrid, M, t):
    """CAM raster resampled into the design grid for one board instance."""
    X, Y = dgrid.centres()
    P = np.stack([X.ravel(), Y.ravel()], axis=1) @ M.T + t
    c = np.floor((P[:, 0] - cam_grid.x0) / cam_grid.res).astype(np.int64)
    r = np.floor((cam_grid.y1 - P[:, 1]) / cam_grid.res).astype(np.int64)
    ok = (c >= 0) & (c < cam_grid.w) & (r >= 0) & (r < cam_grid.h)
    out = np.zeros(X.size, dtype=bool)
    out[ok] = cam_raster[r[ok], c[ok]]
    return out.reshape(X.shape)


# ------------------------------------------------------------------ report

class Review:
    """Summary lines for the text report plus located findings for the review workspace."""

    def __init__(self):
        self.lines, self.findings, self.fails = [], [], 0
        self.meta = collections.OrderedDict()

    def line(self, check, status, msg):
        if status == 'FAIL':
            self.fails += 1
        self.lines.append(f'{check} {status:6s} {msg}')

    def find(self, check, status, layer, title, x=None, y=None, w=0.0, h=0.0, **extra):
        # stable across re-runs, so a saved review keeps its dispositions when the analysis is repeated
        import hashlib
        basis = f'{check}|{layer}|{extra.get("kind", "")}|{extra.get("instance", "")}|' + (
            f'{round(x, 1)}|{round(y, 1)}' if x is not None else re.sub(r'[\d.]+', '#', title))
        fid = f'{check}-' + hashlib.sha1(basis.encode()).hexdigest()[:6]
        while any(f['id'] == fid for f in self.findings):
            fid += 'x'
        f = {'id': fid, 'check': check, 'status': status, 'layer': layer,
             'title': title, 'x': None if x is None else round(float(x), 3),
             'y': None if y is None else round(float(y), 3), 'w': round(float(w), 3), 'h': round(float(h), 3)}
        f.update(extra)
        self.findings.append(f)
        return f


def blob_list(mask, grid, min_px, limit=None):
    """[(x, y, w, h, area_mm2)] of 8-connected blobs >= min_px, largest first."""
    lab, n = ndimage.label(mask, structure=np.ones((3, 3)))
    if n == 0:
        return []
    sizes = ndimage.sum_labels(mask, lab, index=np.arange(1, n + 1))
    objs = ndimage.find_objects(lab)
    keep = [k for k in np.argsort(-sizes) if sizes[k] >= min_px]
    out = []
    for k in keep[:limit]:
        sl = objs[k]
        x, y = grid.mm((sl[0].start + sl[0].stop - 1) / 2, (sl[1].start + sl[1].stop - 1) / 2)
        out.append((x, y, (sl[1].stop - sl[1].start) * grid.res, (sl[0].stop - sl[0].start) * grid.res,
                    float(sizes[k] * grid.res ** 2)))
    return out


def pick(layers, role, side=None):
    return [L for L in layers if L.role == role and (side is None or L.side == side) and L.file is not None]


def copper_stack(layers):
    return sorted(pick(layers, 'cu'), key=lambda L: L.index)


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    if os.path.isdir(path):
        for dp, dn, fn in sorted(os.walk(path)):
            dn.sort()
            for f in sorted(fn):
                with open(os.path.join(dp, f), 'rb') as fh:
                    h.update(f.encode() + fh.read())
    else:
        with open(path, 'rb') as fh:
            h.update(fh.read())
    return h.hexdigest()


def tool_revision():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        rev = subprocess.run(['git', '-C', here, 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(['git', '-C', here, 'status', '--porcelain', '--', os.path.basename(__file__)],
                               capture_output=True, text=True).stdout.strip()
        return rev + ('+modified' if dirty else '') if rev else 'unknown'
    except OSError:
        return 'unknown'


def compare(design_src, cam_src, res=0.02, tol=0.08, min_area=0.02, hole_tol=0.05, move_tol=0.15,
            edge_band=0.5, panel_res=0.1):
    """Run every check. Returns (Review, viewer data dict with numpy masks)."""
    R = Review()
    D, C = load_set(design_src), load_set(cam_src)
    R.meta.update(design=os.path.abspath(design_src), cam=os.path.abspath(cam_src),
                  design_sha256=sha256(design_src), cam_sha256=sha256(cam_src), tool='cam_compare.py',
                  tool_revision=tool_revision(),
                  params=dict(res=res, tol=tol, min_area=min_area, hole_tol=hole_tol, move_tol=move_tol,
                              edge_band=edge_band))
    V = {'layers': [], 'holes': {}, 'panel': {}}

    # ---- C1 layer set
    for tag, S in (('design', D), ('CAM', C)):
        for L in S:
            if L.error:
                R.line('C1', 'FAIL', f'{tag} {L.label}: unreadable ({L.error[:160]})')
                R.find('C1', 'FAIL', '', f'{tag} file {L.label} unreadable', detail=L.error[:400])
        R.line('C1', 'INFO', f'{tag} layers: ' + ', '.join(
            f'{L.label}={L.role}{("/" + L.side) if L.side else ""}{("#" + str(L.index)) if L.role == "cu" else ""}'
            for L in sorted(S, key=lambda L: (L.role, L.index, L.name))))
    R.meta['layers'] = {tag: [{'file': L.label, 'role': L.role, 'side': L.side, 'index': L.index, 'basis': L.basis}
                              for L in S] for tag, S in (('design', D), ('cam', C))}
    dcu, ccu = copper_stack(D), copper_stack(C)
    ok_cu = len(dcu) == len(ccu) and bool(dcu)
    R.line('C1', 'PASS' if ok_cu else 'FAIL', f'copper layers: design {len(dcu)}, CAM {len(ccu)}')
    if not ok_cu:
        R.find('C1', 'FAIL', '', f'copper layer count differs: design {len(dcu)}, CAM {len(ccu)}')
    pairs = []
    if ok_cu:
        pairs += [(f'cu L{k + 1}', a, b) for k, (a, b) in enumerate(zip(dcu, ccu))]
    elif dcu and ccu:
        pairs += [('cu top', dcu[0], ccu[0]), ('cu bottom', dcu[-1], ccu[-1])]
    for role in ('mask', 'silk', 'paste'):
        for side in ('top', 'bot'):
            a, b = pick(D, role, side), pick(C, role, side)
            if a and b:
                pairs.append((f'{role} {side}', a[0], b[0]))
            elif a:
                R.line('C1', 'FAIL', f'{role} {side}: in design, not in CAM')
                R.find('C1', 'FAIL', '', f'{role} {side} layer missing from the CAM set')
            elif b:
                R.line('C1', 'INFO', f'{role} {side}: in CAM only ({b[0].label})')

    # ---- C2 placement from the drill pattern
    ddr = pick(D, 'drill')
    dh = np.concatenate([L.holes() for L in ddr]) if ddr else np.zeros((0, 4))
    chl = pick(C, 'drill') + pick(C, 'aux')  # CAM tools often split one design drill set over several files
    parts = [np.column_stack([L.holes(), np.full(len(L.holes()), k)]) for k, L in enumerate(chl)]
    ch = np.concatenate(parts) if parts else np.zeros((0, 5))
    R.line('C2', 'INFO', f'design holes {len(dh)} ({", ".join(L.label for L in ddr)}); CAM drill-layer flashes '
                         + ', '.join(f'{L.label} {len(p)}' for L, p in zip(chl, parts)))
    inst = find_instances(dh[:, :2], ch[:, :2], hole_tol, move_tol) if len(dh) >= 3 and len(ch) >= 3 else None
    if inst is None:
        R.line('C2', 'FAIL', 'design hole pattern not found in the CAM drill data')
        R.find('C2', 'FAIL', '', 'design hole pattern not found in the CAM drill data; nothing else can be compared')
        return R, None
    rot, mir, M, ts, nmatch = inst
    xs, ys = sorted({round(t[0], 1) for t in ts}), sorted({round(t[1], 1) for t in ts})
    pitch = lambda v: sorted({float(round(b - a, 2)) for a, b in zip(v, v[1:])})
    R.line('C2', 'INFO', f'{len(ts)} board instance(s) in CAM, rotation {rot} deg, '
                         f'{"mirrored" if mir else "not mirrored"}; {len(xs)} columns x {len(ys)} rows; '
                         f'pitch x {pitch(xs) or "-"} mm, y {pitch(ys) or "-"} mm')
    R.meta['instances'] = {'count': len(ts), 'rotation': rot, 'mirrored': mir, 'columns': len(xs), 'rows': len(ys),
                           'pitch_x': pitch(xs), 'pitch_y': pitch(ys),
                           'matrix': M.tolist(), 'offsets': [[float(t[0]), float(t[1])] for t in ts]}

    dout = pick(D, 'outline')
    if dout:
        (bx0, by0), (bx1, by1) = dout[0].file.bounding_box(MM)
    else:
        bx0, by0, bx1, by1 = dh[:, 0].min() - 2, dh[:, 1].min() - 2, dh[:, 0].max() + 2, dh[:, 1].max() + 2
    m = 1.0
    dgrid = Grid(bx0 - m, by0 - m, bx1 + m, by1 + m, res)
    outline_r = rasterize(dout[0], dgrid) if dout else np.zeros((dgrid.h, dgrid.w), dtype=bool)
    region = board_region(outline_r) if dout else None
    if region is None:
        R.line('C2', 'INFO', 'design outline missing or not closed: the whole outline bounding box is compared')
        region = np.ones((dgrid.h, dgrid.w), dtype=bool)
    region_in = ndimage.binary_erosion(region, structure=disk(tol / res + 1))
    interior = ndimage.binary_erosion(region, structure=disk(edge_band / res))
    band = region_in & ~interior
    cb = [L.file.bounding_box(MM) for L in C if L.file is not None]
    cgrid = Grid(min(b[0][0] for b in cb) - m, min(b[0][1] for b in cb) - m,
                 max(b[1][0] for b in cb) + m, max(b[1][1] for b in cb) + m, res)
    R.line('C2', 'INFO', f'design outline {bx1 - bx0:.2f} x {by1 - by0:.2f} mm; '
                         f'CAM extents {cgrid.w * res - 2 * m:.2f} x {cgrid.h * res - 2 * m:.2f} mm')

    def in_mask(mask, pts):
        rr = np.floor((dgrid.y1 - pts[:, 1]) / res).astype(int)
        cc = np.floor((pts[:, 0] - dgrid.x0) / res).astype(int)
        ok = (rr >= 0) & (rr < dgrid.h) & (cc >= 0) & (cc < dgrid.w)
        out = np.zeros(len(pts), dtype=bool)
        out[ok] = mask[rr[ok], cc[ok]]
        return out

    # ---- C3 drills: exact, moved, missing, extra
    inv = np.linalg.inv(M)
    ctree, dtree = cKDTree(ch[:, :2]), cKDTree(dh[:, :2])
    table = collections.Counter()
    n_moved = n_missing = 0
    moved1, missing1 = [], []
    for k, t in enumerate(ts):
        d, i = ctree.query(dh[:, :2] @ M.T + t)
        for j in range(len(dh)):
            if d[j] <= move_tol:
                table[(round(dh[j, 2], 3), int(dh[j, 3]), chl[int(ch[i[j], 4])].label, round(ch[i[j], 2], 3))] += 1
            if hole_tol < d[j] <= move_tol:
                n_moved += 1
                if k == 0:
                    off = (ch[i[j], :2] - t) @ inv.T - dh[j, :2]
                    moved1.append((dh[j], off, d[j]))
            elif d[j] > move_tol:
                n_missing += 1
                if k == 0:
                    missing1.append(dh[j])
    for (dd, pl, lab, cd), n in sorted(table.items()):
        smaller = pl == 1 and cd < dd - 1e-3
        st = 'REVIEW' if smaller else 'INFO'
        msg = (f'design {dd:.3f} mm {"PTH" if pl == 1 else "NPTH" if pl == 0 else "hole"} -> CAM {lab} '
               f'{cd:.3f} mm ({cd - dd:+.3f}): {n / len(ts):g} per board')
        R.line('C3', st, msg + (' (CAM drill smaller than the design finished size)' if smaller else ''))
        if smaller:
            R.find('C3', 'REVIEW', 'drill', f'plated {dd:.3f} mm holes drilled {cd:.3f} mm in CAM ({lab})',
                   detail='CAM tool is smaller than the design finished hole size.')
    R.line('C3', 'PASS' if not n_moved else 'REVIEW',
           f'design holes moved by {hole_tol:g}-{move_tol:g} mm in CAM: {n_moved / len(ts):g} per board')
    for hole, off, dist in moved1:
        R.find('C3', 'REVIEW', 'drill', f'{hole[2]:.3f} mm hole moved {dist:.3f} mm in CAM',
               x=hole[0], y=hole[1], w=0.5, h=0.5, detail=f'offset dx {off[0]:+.3f}, dy {off[1]:+.3f} mm')
    R.line('C3', 'PASS' if not n_missing else 'FAIL',
           f'design holes absent from CAM (none within {move_tol:g} mm): {n_missing} across {len(ts)} instance(s)')
    for hole in missing1:
        R.find('C3', 'FAIL', 'drill', f'{hole[2]:.3f} mm design hole absent from CAM', x=hole[0], y=hole[1], w=0.5, h=0.5)
    extra = collections.Counter()
    cam_holes_1 = []
    for k, t in enumerate(ts):
        p = (ch[:, :2] - t) @ inv.T
        inside, inner = in_mask(region, p), in_mask(interior, p)
        dd, _ = dtree.query(p)
        for j in np.nonzero(inside)[0]:
            if k == 0:
                cam_holes_1.append([round(float(p[j, 0]), 4), round(float(p[j, 1]), 4), round(float(ch[j, 2]), 4),
                                    chl[int(ch[j, 4])].label])
            if dd[j] > move_tol:
                zone = 'interior' if inner[j] else 'edge band'
                extra[(zone, chl[int(ch[j, 4])].label, round(ch[j, 2], 3))] += 1
                if k == 0:
                    R.find('C3', 'REVIEW' if zone == 'interior' else 'INFO', 'drill',
                           f'CAM {chl[int(ch[j, 4])].label} {ch[j, 2]:.3f} mm hole with no design hole ({zone})',
                           x=p[j, 0], y=p[j, 1], w=0.6, h=0.6)
    for zone in ('interior', 'edge band'):
        items = {k: n for k, n in extra.items() if k[0] == zone}
        R.line('C3', 'PASS' if not items else ('REVIEW' if zone == 'interior' else 'INFO'),
               f'CAM holes with no design hole, board {zone}: ' +
               (', '.join(f'{n / len(ts):g} per board of {lab} {d:.3f} mm' for (_, lab, d), n in sorted(items.items()))
                or 'none'))
    for L in pick(C, 'aux'):
        hit = collections.Counter()
        at = L.holes()
        if len(at):
            d, _ = cKDTree(at[:, :2]).query(dh[:, :2] @ M.T + ts[0])
            for j in np.nonzero(d <= move_tol)[0]:
                hit[round(dh[j, 2], 3)] += 1
        tot = collections.Counter(round(v, 3) for v in dh[:, 2])
        R.line('C3', 'INFO', f'CAM layer {L.label} marks, per board: ' +
               (', '.join(f'{n} of {tot[dd]} design {dd:.3f} mm holes' for dd, n in sorted(hit.items())) or 'no design holes'))
    V['holes'] = {'design': [[round(float(a), 4), round(float(b), 4), round(float(c), 4), int(e)] for a, b, c, e in dh],
                  'cam': cam_holes_1}

    # ---- C4 geometry, C7 panel consistency
    tol_px, min_px = tol / res, max(1, int(round(min_area / res ** 2)))
    dil = disk(tol_px)
    hp = dh[dh[:, 3] != 0] if (dh[:, 3] != 0).any() else dh
    hole_px = np.zeros((dgrid.h, dgrid.w), dtype=bool)
    hr = np.floor((dgrid.y1 - hp[:, 1]) / res).astype(int)
    hc = np.floor((hp[:, 0] - dgrid.x0) / res).astype(int)
    ok = (hr >= 0) & (hr < dgrid.h) & (hc >= 0) & (hc < dgrid.w)
    hole_px[hr[ok], hc[ok]] = True
    pgrid = Grid(cgrid.x0, cgrid.y1 - cgrid.h * res, cgrid.x0 + cgrid.w * res, cgrid.y1, panel_res)
    d_cu, c_cu, outside = [], [], []
    for key, a, b in pairs:
        dr = rasterize(a, dgrid)
        cpanel = rasterize(b, cgrid)
        units = [sample_unit(cpanel, cgrid, dgrid, M, t) for t in ts]
        cr = units[0]
        V['panel'][key] = rasterize(b, pgrid)
        del cpanel
        out_px = int((dr & ~region).sum())
        if out_px:
            outside.append(f'{key} ({a.label}): {out_px * res * res:.2f} mm2')
        dmask, cmask = dr & region_in, cr & region_in
        removed = dmask & ~ndimage.binary_dilation(cmask, structure=dil)
        added = cmask & ~ndimage.binary_dilation(dmask, structure=dil)
        pads_gone = 0
        if key.startswith('cu'):
            lab, n = ndimage.label(removed & interior, structure=np.ones((3, 3)))
            if n:
                sizes = ndimage.sum_labels(removed, lab, index=np.arange(1, n + 1))
                on_hole = set(np.unique(lab[hole_px & (lab > 0)]).tolist()) - {0}
                drop = [q for q in on_hole if sizes[q - 1] * res * res < 1.0]
                pads_gone = len(drop)
                removed &= ~np.isin(lab, drop)
        ir, ia = blob_list(removed & interior, dgrid, min_px), blob_list(added & interior, dgrid, min_px)
        er, ea = blob_list(removed & band, dgrid, min_px, 40), blob_list(added & band, dgrid, min_px, 40)
        di, ci = dr & interior, cr & interior
        per = ndimage.binary_dilation(di) ^ di
        growth = ((ci.sum() - di.sum()) / max(per.sum(), 1)) * res
        st = 'PASS' if not ir and not ia else 'REVIEW'
        stats = {'design_area': round(float(di.sum() * res * res), 2), 'cam_area': round(float(ci.sum() * res * res), 2),
                 'edge_offset': round(float(growth), 4), 'removed': len(ir), 'added': len(ia),
                 'removed_area': round(sum(q[4] for q in ir), 3), 'added_area': round(sum(q[4] for q in ia), 3),
                 'edge_removed_area': round(float((removed & band).sum() * res * res), 3),
                 'edge_added_area': round(float((added & band).sum() * res * res), 3), 'pads_removed': pads_gone}
        R.line('C4', st, f'{key}: design {a.label} vs CAM {b.label}; interior area {stats["design_area"]} -> '
                         f'{stats["cam_area"]} mm2, mean edge offset {growth:+.3f} mm; interior removed {len(ir)} '
                         f'({stats["removed_area"]} mm2), added {len(ia)} ({stats["added_area"]} mm2); edge band removed '
                         f'{stats["edge_removed_area"]} mm2, added {stats["edge_added_area"]} mm2'
                         + (f'; {pads_gone} pads around plated holes removed' if pads_gone else ''))
        for kind, items, status in (('removed', ir, 'REVIEW'), ('added', ia, 'REVIEW'),
                                    ('removed', er, 'INFO'), ('added', ea, 'INFO')):
            for x, y, w, h, area in items:
                R.find('C4', status, key, f'{key}: {"design" if kind == "removed" else "CAM"}-only area '
                       f'{w:.2f} x {h:.2f} mm{" (edge band)" if status == "INFO" else ""}',
                       x=x, y=y, w=w, h=h, kind=kind, area=round(area, 4))
        bad = []
        for k, u in enumerate(units[1:], start=2):
            lacks = ndimage.binary_opening(cr & ~u & region_in, structure=disk(1))
            extra_ = ndimage.binary_opening(u & ~cr & region_in, structure=disk(1))
            for what, mask in (('lacks', lacks), ('has extra', extra_)):
                for x, y, w, h, area in blob_list(mask, dgrid, min_px, 20):
                    bad.append(k)
                    R.find('C7', 'REVIEW', key, f'{key}: instance {k} {what} {w:.2f} x {h:.2f} mm vs instance 1',
                           x=x, y=y, w=w, h=h, instance=k, kind=what)
        bad = sorted(set(bad))
        R.line('C7', 'PASS' if not bad else 'REVIEW',
               f'{key}: all {len(units)} instances match instance 1' if not bad else f'{key}: instances {bad} differ from instance 1')
        V['layers'].append({'key': key, 'design_file': a.label, 'cam_file': b.label, 'role': a.role, 'side': a.side,
                            'status': st, 'stats': stats, 'design': dr, 'cam': cr,
                            'flag': removed & interior | added & interior})
        if key.startswith('cu'):
            d_cu.append(dr & region)
            c_cu.append(cr & region)
    if outside:
        R.line('C4', 'INFO', 'design features outside the board outline (not compared): ' + '; '.join(outside))

    # ---- C5 inner layer order
    if ok_cu and len(d_cu) > 3:
        inner = range(1, len(d_cu) - 1)
        best = {}
        for i in inner:
            sc = {j: int(((d_cu[i] ^ c_cu[j]) & region_in).sum()) for j in inner}
            best[i] = i if sc[i] == min(sc.values()) else min(sc, key=sc.get)
        good = all(best[i] == i for i in inner)
        R.line('C5', 'PASS' if good else 'FAIL', 'inner layer best match: ' +
               ', '.join(f'design L{i + 1} -> CAM L{best[i] + 1}' for i in inner))
        if not good:
            R.find('C5', 'FAIL', '', 'inner layer order differs: ' + ', '.join(
                f'design L{i + 1} matches CAM L{best[i] + 1}' for i in inner if best[i] != i))

    # ---- C6 connectivity
    if ok_cu and d_cu:
        masks = {L['key']: L['design'] for L in V['layers'] if L['key'] in ('mask top', 'mask bot')}
        pads = [None] * len(d_cu)
        pads[0], pads[-1] = masks.get('mask top'), masks.get('mask bot')
        connectivity(R, d_cu, c_cu, dh, dgrid, interior, band, min_px, pads)

    V.update(grid={'x0': dgrid.x0, 'y1': dgrid.y1, 'res': res, 'w': dgrid.w, 'h': dgrid.h},
             pgrid={'x0': pgrid.x0, 'y1': pgrid.y1, 'res': panel_res, 'w': pgrid.w, 'h': pgrid.h},
             outline=outline_r, region=region, band=band,
             board=[bx0, by0, bx1, by1])
    return R, V


def connectivity(R, d_cu, c_cu, dh, grid, interior, band, min_px, pads=None):
    """Nets: copper islands per layer joined through plated holes. Each design island is mapped to
    the CAM islands it overlaps; a design net that maps to several CAM nets is an open, a CAM net
    that collects several design nets is a short. Overlap mapping is immune to etch compensation,
    removed non-functional pads and cleared holes inside pads."""
    st = ndimage.generate_binary_structure(2, 1)
    pads = pads or [None] * len(d_cu)
    ld = [ndimage.label(a, structure=st)[0] for a in d_cu]
    lc = [ndimage.label(b, structure=st)[0] for b in c_cu]
    plated = dh[dh[:, 3] != 0] if (dh[:, 3] != 0).any() else dh
    rr = np.clip(np.floor((grid.y1 - plated[:, 1]) / grid.res).astype(int), 0, grid.h - 1)
    cc = np.clip(np.floor((plated[:, 0] - grid.x0) / grid.res).astype(int), 0, grid.w - 1)

    def uf(labs):
        parent = {}

        def find(a):
            while parent.setdefault(a, a) != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        for r, c in zip(rr, cc):
            nodes = [(li, int(lab[r, c])) for li, lab in enumerate(labs) if lab[r, c]]
            for n in nodes[1:]:
                parent[find(nodes[0])] = find(n)
        return find
    fd, fc = uf(ld), uf(lc)
    hole_nodes = {(li, int(lab[r, c])) for li, lab in enumerate(ld) for r, c in zip(rr, cc) if lab[r, c]}
    d2c = collections.defaultdict(dict)   # design node -> {cam node: (count, flat index)}
    for li in range(len(ld)):
        both = (ld[li] > 0) & (lc[li] > 0)
        idx = np.flatnonzero(both)
        if not len(idx):
            continue
        key = ld[li].ravel()[idx].astype(np.int64) * 10_000_000 + lc[li].ravel()[idx]
        keys, first, counts = np.unique(key, return_index=True, return_counts=True)
        for k_, f_, n_ in zip(keys, first, counts):
            if n_ >= min_px:
                d2c[(li, int(k_ // 10_000_000))][(li, int(k_ % 10_000_000))] = (int(n_), int(idx[f_]))
    missing, nfp, trimmed = [], 0, 0
    by_dnet, by_cnet = collections.defaultdict(set), collections.defaultdict(dict)
    for li in range(len(ld)):
        n = int(ld[li].max())
        if not n:
            continue
        objs = ndimage.find_objects(ld[li])
        area = ndimage.sum_labels(d_cu[li], ld[li], index=np.arange(1, n + 1))
        for k in range(1, n + 1):
            node = (li, k)
            cams = d2c.get(node)
            sl = objs[k - 1]
            sub = ld[li][sl] == k
            if not (interior[sl] & sub).any():
                trimmed += area[k - 1] >= min_px
                continue
            if not cams:
                if area[k - 1] * grid.res ** 2 < 1.0 and node in hole_nodes:
                    nfp += 1
                else:
                    missing.append((li, sl))
                continue
            dn = fd(node)
            for cn, (cnt, flat) in cams.items():
                cnet = fc(cn)
                by_dnet[dn].add(cnet)
                by_cnet[cnet].setdefault(dn, (li, flat))
    R.line('C6', 'INFO', f'{len({fd(n) for n in d2c})} design nets (copper islands joined by plated holes); '
                         f'{nfp} isolated hole pads absent in CAM (non-functional pad removal); '
                         f'{trimmed} islands lying only in the edge band (not compared)')

    def loc(li, flat):
        r, c = divmod(flat, grid.w)
        x, y = grid.mm(r, c)
        return li, x, y
    R.line('C6', 'PASS' if not missing else 'FAIL', f'design copper islands absent from CAM: {len(missing)}')
    for li, sl in missing:
        x, y = grid.mm((sl[0].start + sl[0].stop - 1) / 2, (sl[1].start + sl[1].stop - 1) / 2)
        R.find('C6', 'FAIL', f'cu L{li + 1}', f'cu L{li + 1}: design copper island absent from CAM',
               x=x, y=y, w=(sl[1].stop - sl[1].start) * grid.res, h=(sl[0].stop - sl[0].start) * grid.res)
    opens = [dn for dn, cs in by_dnet.items() if len(cs) > 1]
    cam_nodes = collections.defaultdict(list)
    for cams in d2c.values():
        for cn in cams:
            cam_nodes[fc(cn)].append(cn)
    n_before = len(R.findings)
    for dn in opens:
        # locate the break: removed design copper lying within a small distance of two CAM pieces
        # that belong to different CAM nets (the bridge the CAM took away)
        best = None
        for (li, k), cams in d2c.items():
            nets = {}
            for cn, (cnt, _) in cams.items():
                nets.setdefault(fc(cn), []).append(cn[1])
            if fd((li, k)) != dn or len(nets) < 2:
                continue
            island = ld[li] == k
            gone = island & ~c_cu[li]
            groups = sorted(nets.values(), key=lambda ls: -sum(cams[(li, q)][0] for q in ls))
            A, B = np.isin(lc[li], groups[0]), np.isin(lc[li], groups[1])
            for r_ in range(2, 16):
                bridge = gone & ndimage.binary_dilation(A, iterations=r_) & ndimage.binary_dilation(B, iterations=r_)
                if bridge.any():
                    rs, cs = np.nonzero(bridge)
                    glab = ndimage.label(gone, structure=np.ones((3, 3)))[0]
                    comp = np.isin(glab, np.unique(glab[bridge]))
                    neck = 2 * float(ndimage.distance_transform_edt(comp).max()) * grid.res
                    cand = (r_, li, rs.min(), rs.max(), cs.min(), cs.max(), neck)
                    if best is None or cand[0] < best[0]:
                        best = cand
                    break
        pieces = len(by_dnet[dn])
        info = []
        for cn in by_dnet[dn]:
            nodes = cam_nodes.get(cn, [])
            holes_on = sum(1 for r, c in zip(rr, cc) if any(fc((li, int(lab[r, c]))) == cn for li, lab in enumerate(lc) if lab[r, c]))
            pad = 0.0
            for li, pm in enumerate(pads):
                if pm is None:
                    continue
                labs_here = [q for (l_, q) in nodes if l_ == li]
                if labs_here:
                    pad += float((pm & np.isin(lc[li], labs_here)).sum()) * grid.res ** 2
            info.append((holes_on, pad))
        info.sort()
        small_holes, small_pad = info[0]
        severe = small_holes > 0 or small_pad > 0.01
        status = 'FAIL' if severe else 'REVIEW'
        what = (f'separated piece carries {small_holes} plated hole(s) and {small_pad:.2f} mm2 of exposed pad'
                if severe else 'separated piece carries no plated hole and no exposed pad (isolated copper)')
        if best:
            _, li, r0, r1, c0, c1, neck = best
            x, y = grid.mm((r0 + r1) / 2, (c0 + c1) / 2)
            w, h = (c1 - c0 + 1) * grid.res, (r1 - r0 + 1) * grid.res
            R.find('C6', status, f'cu L{li + 1}', f'open: design net splits into {pieces} CAM nets; {what}',
                   x=x, y=y, w=w, h=h, neck=round(neck, 3), holes=small_holes, pad_area=round(small_pad, 3),
                   detail=f'box: design copper removed in CAM that joined the separated pieces; the removed '
                          f'copper is about {neck:.2f} mm wide')
        else:
            cn0 = min(by_dnet[dn], key=lambda cn: len(by_cnet[cn]))
            li, x, y = loc(*by_cnet[cn0][dn])
            R.find('C6', status, f'cu L{li + 1}', f'open: design net splits into {pieces} CAM nets; {what}',
                   x=x, y=y, w=1.0, h=1.0, holes=small_holes, pad_area=round(small_pad, 3),
                   detail='break not localised; location is a feature of one separated piece')
    new = R.findings[n_before:]
    hard = sum(1 for f in new if f['status'] == 'FAIL')
    R.line('C6', 'FAIL' if hard else ('REVIEW' if opens else 'PASS'),
           f'opens (one design net split into several CAM nets): {len(opens)}; '
           f'{hard} cut off plated holes or pads, {len(opens) - hard} only isolate bare copper')
    shorts = [cn for cn, ds in by_cnet.items() if len(ds) > 1]
    R.line('C6', 'PASS' if not shorts else 'FAIL', f'shorts (several design nets joined in CAM): {len(shorts)}')
    for cn in shorts:
        (li, flat) = list(by_cnet[cn].values())[-1]
        li, x, y = loc(li, flat)
        R.find('C6', 'FAIL', f'cu L{li + 1}', f'short: {len(by_cnet[cn])} design nets joined in CAM',
               x=x, y=y, w=1.0, h=1.0, detail='location is where one of the joined design nets meets the CAM copper')


# ------------------------------------------------------------------ outputs

def png_b64(mask):
    import base64, io
    buf = io.BytesIO()
    Image.fromarray(np.asarray(mask, dtype=bool)).convert('1').save(buf, format='PNG', optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def write_outputs(R, V, outdir):
    os.makedirs(outdir, exist_ok=True)
    text = '\n'.join(R.lines) + f'\n== {"FAIL" if R.fails else "PASS"} ({R.fails} failing checks)\n'
    with open(os.path.join(outdir, 'report.txt'), 'w') as fh:
        fh.write(text)
    data = {'meta': R.meta, 'lines': R.lines, 'fails': R.fails, 'findings': R.findings}
    with open(os.path.join(outdir, 'findings.json'), 'w') as fh:
        json.dump(data, fh, indent=1)
    if V is not None:
        view = {'grid': V['grid'], 'pgrid': V['pgrid'], 'board': V['board'], 'holes': V['holes'],
                'outline': png_b64(V['outline']), 'region': png_b64(V['region']), 'band': png_b64(V['band']),
                'layers': [{k: (png_b64(v) if k in ('design', 'cam', 'flag') else v) for k, v in L.items()}
                           for L in V['layers']],
                'panel': {k: png_b64(v) for k, v in V['panel'].items()}}
        data['view'] = view
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cam_compare_viewer.html')
        with open(tpl, encoding='utf-8') as fh:
            page = fh.read()
        page = page.replace('/*__DATA__*/null', json.dumps(data, separators=(',', ':')).replace('</', '<\\/'))
        with open(os.path.join(outdir, 'index.html'), 'w', encoding='utf-8') as fh:
            fh.write(page)
    print(text, end='')
    return 1 if R.fails else 0


def serve(outdir, port):
    """Serve the review workspace and store edits in <outdir>/review.json (the only file written)."""
    import http.server, functools
    outdir = os.path.abspath(outdir)

    class H(http.server.SimpleHTTPRequestHandler):
        def do_PUT(self):
            if self.path.split('?')[0] != '/review.json':
                self.send_error(403)
                return
            n = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(n)
            json.loads(body)  # refuse anything that is not JSON
            tmp = os.path.join(outdir, '.review.json.tmp')
            with open(tmp, 'wb') as fh:
                fh.write(body)
            os.replace(tmp, os.path.join(outdir, 'review.json'))
            self.send_response(204)
            self.end_headers()

        def do_GET(self):
            if self.path.split('?')[0] == '/review.json' and not os.path.exists(os.path.join(outdir, 'review.json')):
                self.send_response(200)  # no review yet
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'null')
                return
            super().do_GET()

        def do_POST(self):
            # re-run the analysis on the recorded inputs with new parameters; review.json is untouched
            if self.path.split('?')[0] != '/rerun':
                self.send_error(403)
                return
            n = int(self.headers.get('Content-Length', 0))
            params = json.loads(self.rfile.read(n) or b'{}')
            with open(os.path.join(outdir, 'findings.json')) as fh:
                meta = json.load(fh)['meta']
            allowed = {'res', 'tol', 'min_area', 'hole_tol', 'move_tol', 'edge_band'}
            kw = dict(meta['params'])
            kw.update({k: float(v) for k, v in params.items() if k in allowed})
            try:
                R, V = compare(meta['design'], meta['cam'], **kw)
                write_outputs(R, V, outdir)
                body, code = json.dumps({'ok': True, 'fails': R.fails}).encode(), 200
            except Exception as e:
                body, code = json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'}).encode(), 500
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)

        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), functools.partial(H, directory=outdir))
    print(f'review workspace: http://127.0.0.1:{port}/  (edits saved to {os.path.join(outdir, "review.json")}; Ctrl+C stops)')
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--design', help='released Gerber set (zip, dir, archive)')
    ap.add_argument('--cam', help="fabricator's CAM Gerber set (zip, dir, archive)")
    ap.add_argument('-o', '--out', required=True, help='output directory (report.txt, findings.json, index.html)')
    ap.add_argument('--res', type=float, default=0.02, help='raster resolution, mm per pixel (default 0.02)')
    ap.add_argument('--tol', type=float, default=0.08, help='geometry tolerance per edge, mm (default 0.08)')
    ap.add_argument('--min-area', type=float, default=0.02, help='smallest reported difference, mm2 (default 0.02)')
    ap.add_argument('--hole-tol', type=float, default=0.05, help='exact hole match distance, mm (default 0.05)')
    ap.add_argument('--move-tol', type=float, default=0.15, help='moved hole match distance, mm (default 0.15)')
    ap.add_argument('--edge-band', type=float, default=0.5,
                    help='band inside the outline reported apart as edge clearance, mm (default 0.5)')
    ap.add_argument('--serve', action='store_true', help='serve an existing output directory for review (no analysis)')
    ap.add_argument('--port', type=int, default=8765)
    a = ap.parse_args()
    if a.serve:
        return serve(a.out, a.port)
    if not (a.design and a.cam):
        ap.error('--design and --cam are required unless --serve is given')
    R, V = compare(a.design, a.cam, a.res, a.tol, a.min_area, a.hole_tol, a.move_tol, a.edge_band)
    sys.exit(write_outputs(R, V, a.out))


if __name__ == '__main__':
    main()
