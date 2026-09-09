"""Sketch regions for FreeCAD, the way Onshape sees them. Runs inside FreeCAD.

An Onshape sketch is a soup of curves; an extrude picks *regions*, the faces
of the planar graph formed by all non-construction curves. FreeCAD's Pad wants
closed profiles, so this module computes the regions of a master sketch and
exposes a chosen subset as a ``Part::Part2DObjectPython`` inside a Body. The
region object keeps a global link to the master sketch and recomputes when the
sketch changes, so the tree stays parametric.

Regions are identified by the Onshape ids of the sketch entities on their
boundary, which is exactly how Onshape's own region queries describe them.
"""
import FreeCAD as App
import Part

TOL = 1e-6

# Some Onshape regions come out a few percent larger here, so a region that
# sits exactly on the reported centre of mass is accepted up to this area
# difference and flagged approximate rather than dropped.
LOOSE_AREA = 0.10


def section_edges(solids, placement):
    """Edges where solids cross the sketch plane.

    Onshape lets a sketch region be bounded by an edge imprinted from a solid
    the sketch sits on, which is not a curve of the sketch at all. Slicing the
    live bodies at the sketch plane reproduces those boundaries.
    """
    n = placement.Rotation.multVec(App.Vector(0, 0, 1))
    d = placement.Base.dot(n)
    out = []
    for sh in solids:
        if sh is None or sh.isNull() or not sh.Solids:
            continue
        # A sketch drawn on a face of a solid is coplanar with it, and slicing
        # a solid exactly at one of its own faces is degenerate, so take that
        # face's edges directly and fall back to a section elsewhere.
        flat = []
        for f in sh.Faces:
            if f.Surface.__class__.__name__ != "Plane":
                continue
            fn = f.Surface.Axis
            if abs(abs(fn.dot(n)) - 1.0) > 1e-6:
                continue
            if abs(f.Surface.Position.dot(n) - d) > 1e-6:
                continue
            flat.extend(f.Edges)
        if flat:
            out.extend(flat)
            continue
        try:
            for w in sh.slice(n, d):
                out.extend(w.Edges)
        except Exception:  # noqa: BLE001
            pass
    return out


def compute_regions(sketch_shape, margin=10.0, placement=None, extra=()):
    """Split a bounding face by the sketch edges; return faces not touching the border.

    The cut is done in the sketch's own plane, so this works for a sketch on
    any plane, not only XY. ``placement`` is the sketch placement that maps
    sketch coordinates to global ones; the returned faces are global, matching
    the coordinate system of ``sketch_shape``.
    """
    edges = list(sketch_shape.Edges) + list(extra)
    if not edges:
        return []
    pl = placement if placement is not None else App.Placement()
    inv = pl.inverse()
    local = []
    for e in edges:
        c = e.copy()
        c.Placement = inv.multiply(c.Placement)
        local.append(c)
    bb = Part.makeCompound(local).BoundBox
    big = Part.makePlane(bb.XLength + 2 * margin, bb.YLength + 2 * margin,
                         App.Vector(bb.XMin - margin, bb.YMin - margin, 0.0))
    res = big.generalFuse(local, TOL)[0]
    out = []
    xb = big.BoundBox
    for f in res.Faces:
        border = False
        for v in f.Vertexes:
            p = v.Point
            if (abs(p.x - xb.XMin) < TOL or abs(p.x - xb.XMax) < TOL
                    or abs(p.y - xb.YMin) < TOL or abs(p.y - xb.YMax) < TOL):
                border = True
                break
        if not border:
            g = f.copy()
            g.Placement = pl.multiply(g.Placement)
            out.append(g)
    return out


def edge_sources(sketch, index):
    """[(onshape entity id, Part edge)] for every non-construction sketch curve."""
    out = []
    by_geo = {g: eid for eid, (g, k) in index.items()}
    for i, g in enumerate(sketch.Geometry):
        if i not in by_geo or sketch.getConstruction(i) or not hasattr(g, "toShape"):
            continue
        try:
            e = g.toShape()
        except Exception:  # noqa: BLE001
            continue
        e.Placement = sketch.Placement.multiply(e.Placement)
        out.append((by_geo[i], e))
    return out


def boundary_ids(face, sources):
    ids = set()
    for e in face.Edges:
        m = e.valueAt(0.5 * (e.FirstParameter + e.LastParameter))
        best = None
        for eid, se in sources:
            d = se.distToShape(Part.Vertex(m))[0]
            if best is None or d < best[0]:
                best = (d, eid)
        if best and best[0] < 1e-4:
            ids.add(best[1])
    return ids


def match_regions(regions, sources, wanted):
    """Pick region faces for each wanted boundary-id set.

    ``wanted`` is a list of sets of Onshape entity ids. Returns
    ``(faces, report)`` where report lists the score of every match.
    """
    tagged = [(f, boundary_ids(f, sources)) for f in regions]
    faces, report = [], []
    for want in wanted:
        best = None
        for f, ids in tagged:
            if not ids:
                continue
            inter = len(ids & want)
            union = len(ids | want)
            score = inter / union if union else 0.0
            if best is None or score > best[0]:
                best = (score, f, ids)
        if best and best[0] > 0.0:
            faces.append(best[1])
            report.append({"score": round(best[0], 3), "want": sorted(want), "got": sorted(best[2]), "area": best[1].Area})
        else:
            report.append({"score": 0.0, "want": sorted(want), "got": [], "area": None})
    return faces, report


def _single(regions, p, area, taken):
    """One region whose area and centre of mass are the probe's."""
    best = None
    for rel in (0.02, LOOSE_AREA):
        atol = rel * area + 1e-6
        for i, f in enumerate(regions):
            if i in taken:
                continue
            da = abs(f.Area - area)
            if area > 0 and da > atol:
                continue
            dc = (f.CenterOfMass - p).Length
            # A loose area match has to be carried by position: the centre of
            # mass must sit on Onshape's, not merely inside the face.
            if dc > 0.05 and (rel > 0.02 or not f.isInside(p, 1e-5, True)):
                continue
            score = dc + da / max(area, 1e-9)
            if best is None or score < best[0]:
                best = (score, i, dc)
        if best:
            return best[1], best[2]
    return None, None


def _composite(regions, p, area, taken):
    """Several adjacent regions that together are one Onshape region.

    Onshape treats a region as one face where an extra curve makes this planar
    graph split it in two. Since the pieces tile the same area, the union is
    recognisable: take the pieces nearest the reported centre of mass, and
    accept the group whose total area and area-weighted centre both land on
    the probe.
    """
    cand = sorted((i for i, f in enumerate(regions)
                   if i not in taken and regions[i].Area <= area * (1 + 0.02)),
                  key=lambda i: (regions[i].CenterOfMass - p).Length)[:8]
    tot, cen, group = 0.0, App.Vector(), []
    for i in cand:
        f = regions[i]
        if tot + f.Area > area * 1.02 + 1e-6:
            continue
        group.append(i)
        cen = cen + f.CenterOfMass * f.Area
        tot += f.Area
        if abs(tot - area) <= 0.02 * area + 1e-6 and len(group) > 1:
            if (cen * (1.0 / tot) - p).Length < 0.05:
                return group
    return None


def _fuse(regions, idx):
    sh = regions[idx[0]]
    for i in idx[1:]:
        sh = sh.fuse(regions[i])
    try:
        sh = sh.removeSplitter()
    except Exception:  # noqa: BLE001
        pass
    return sh.Faces[0] if len(sh.Faces) == 1 else sh


def match_by_probe(regions, probes, placement):
    """Select the regions matching Onshape's region probes.

    A probe is the area and the centroid Onshape reports for a region, the
    centroid given in sketch coordinates. Area and centre of mass together
    identify a planar face, and unlike a point-in-face test they do not require
    the centroid to lie inside the face, which is false for any C-shaped or
    annular region. Where the two region graphs disagree, a probe can also be
    the union of several regions here, or several probes can be one region
    here, and both are resolved rather than dropped.
    """
    pts = [(placement.multVec(App.Vector(x, y, z)), area, (x, y, z)) for x, y, z, area in probes]
    faces, report, taken, left = [], [], set(), []
    for p, area, xyz in pts:
        i, dc = _single(regions, p, area, taken)
        if i is not None:
            taken.add(i)
            faces.append(regions[i])
            rec = {"probe": list(xyz), "area": area, "got": regions[i].Area, "centroid_delta_mm": round(dc, 4)}
            if abs(regions[i].Area - area) > 0.02 * area + 1e-6:
                rec["approximate"] = True
            report.append(rec)
            continue
        grp = _composite(regions, p, area, taken)
        if grp:
            taken.update(grp)
            faces.append(_fuse(regions, grp))
            report.append({"probe": list(xyz), "area": area, "got": round(sum(regions[i].Area for i in grp), 3),
                           "fused_regions": len(grp)})
            continue
        left.append((p, area, xyz))
    # One region here can be what Onshape splits into several, when Onshape's
    # sketch is cut by an edge imprinted from a solid that this sketch has no
    # curve for. The union is still exactly what gets extruded.
    if left:
        want = sum(a for _, a, _ in left)
        cen = App.Vector()
        for p, a, _ in left:
            cen = cen + p * a
        cen = cen * (1.0 / want) if want else cen
        i, dc = _single(regions, cen, want, taken)
        if i is not None:
            taken.add(i)
            faces.append(regions[i])
            for p, a, xyz in left:
                report.append({"probe": list(xyz), "area": a, "got": regions[i].Area,
                               "covered_by_one_region": True})
            left = []
    for p, area, xyz in left:
        near = min(regions, key=lambda f: abs(f.Area - area), default=None)
        report.append({"probe": list(xyz), "area": area, "got": None,
                       "nearest_area": round(near.Area, 3) if near is not None else None,
                       "nearest_centroid_delta_mm": round((near.CenterOfMass - p).Length, 3) if near is not None else None})
    return faces, report


class SketchRegion:
    """Proxy for a Part2DObjectPython that exposes selected regions of a sketch."""

    def __init__(self, obj, sketch, wanted, index_ids):
        obj.Proxy = self
        obj.addProperty("App::PropertyLinkGlobal", "Source", "Region", "Master sketch")
        obj.addProperty("App::PropertyStringList", "Boundary", "Region",
                        "One entry per region: comma separated Onshape entity ids on its boundary")
        obj.addProperty("App::PropertyStringList", "GeometryIds", "Region",
                        "Onshape entity id of every sketch geometry, by geometry index")
        obj.addProperty("App::PropertyDistance", "Offset", "Region",
                        "Shift of the profile along the sketch normal (extrude start offset)")
        obj.addProperty("App::PropertyLinkList", "Cutters", "Region",
                        "Bodies whose section at the sketch plane also bounds the regions")
        obj.addProperty("App::PropertyStringList", "Probes", "Region",
                        "Optional: one 'x,y,z,area' per region in sketch coordinates (mm); "
                        "when present a region is selected by containing the point and matching the area")
        obj.Source = sketch
        obj.Boundary = [",".join(sorted(w)) for w in wanted]
        obj.GeometryIds = index_ids
        obj.Placement = sketch.Placement

    def execute(self, obj):
        sk = obj.Source
        index = {}
        for i, eid in enumerate(obj.GeometryIds):
            if eid:
                index[eid] = (i, None)
        extra = section_edges([c.Shape for c in (obj.Cutters or [])], sk.Placement)
        regions = compute_regions(sk.Shape, placement=sk.Placement, extra=extra)
        sources = edge_sources(sk, index)
        if obj.Probes:
            faces, report = match_by_probe(regions, [tuple(float(x) for x in p.split(",")) for p in obj.Probes], sk.Placement)
        else:
            wanted = [set(b.split(",")) for b in obj.Boundary]
            faces, report = match_regions(regions, sources, wanted)
        self.report = report
        inv = sk.Placement.inverse()
        local = []
        for f in faces:
            g = f.copy()
            g.Placement = inv.multiply(g.Placement)
            local.append(g)
        obj.Shape = Part.makeCompound(local) if len(local) != 1 else local[0]
        pl = App.Placement(sk.Placement)
        pl.Base = pl.Base + pl.Rotation.multVec(App.Vector(0, 0, 1)) * float(obj.Offset)
        obj.Placement = pl

    def dumps(self):
        return None

    def loads(self, state):
        return None


def make_region(body, name, sketch, wanted, index, probes=None, offset=0.0, cutters=None):
    ids = [""] * len(sketch.Geometry)
    for eid, (g, k) in index.items():
        ids[g] = eid
    obj = body.newObject("Part::Part2DObjectPython", name)
    SketchRegion(obj, sketch, wanted or [], ids)
    if cutters:
        obj.Cutters = list(cutters)
    if probes:
        obj.Probes = [",".join(str(v) for v in pr) for pr in probes]
    obj.Offset = offset
    obj.recompute()
    return obj
