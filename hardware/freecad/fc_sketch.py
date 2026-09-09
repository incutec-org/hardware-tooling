"""Build FreeCAD sketches from parsed Onshape sketch data. Runs inside FreeCAD.

Geometry is placed at the solved Onshape coordinates, so the sketch is exact
before any constraint is added. Constraints are then added one at a time and
kept only if the sketch still solves; dropped constraints are reported.
"""
import math

import FreeCAD as App
import Part
import Sketcher

MM = 1000.0


def _v(p):
    return App.Vector(p[0] * MM, p[1] * MM, 0)


def add_geometry(sk, entities):
    """Add solved entities; return {onshape id: (geo index, kind)}."""
    index = {}
    add_geometry.reversed = set()
    for e in entities:
        k = e["kind"]
        cons = e.get("construction", False)
        if k == "linesegment" and "start" in e:
            if math.dist(e["start"], e["end"]) < 1e-9:
                continue
            g = sk.addGeometry(Part.LineSegment(_v(e["start"]), _v(e["end"])), cons)
        elif k == "point" and "point" in e:
            g = sk.addGeometry(Part.Point(_v(e["point"])), True)
        elif k == "circle" and "center" in e:
            g = sk.addGeometry(Part.Circle(_v(e["center"]), App.Vector(0, 0, 1), e["radius"] * MM), cons)
        elif k == "arc" and "a0" in e:
            circ = Part.Circle(_v(e["center"]), App.Vector(0, 0, 1), e["radius"] * MM)
            g = sk.addGeometry(Part.ArcOfCircle(circ, e["a0"], e["a1"]), cons)
            if e.get("reversed"):
                add_geometry.reversed.add(e["id"])
        else:
            continue
        index[e["id"]] = (g, k)
    return index


# point id of an entity sub-reference in Sketcher terms: 1 start, 2 end, 3 center
_SUB = {"start": 1, "end": 2, "center": 3}


def _ref(index, ref):
    """Resolve 'entity', 'entity.start', 'entity.end', 'entity.center'."""
    if ref in index:
        g, k = index[ref]
        if k == "point":
            return g, 1
        return g, None
    base, _, sub = ref.rpartition(".")
    if base in index and sub in _SUB:
        g, k = index[base]
        if sub == "center" and k not in ("circle", "arc"):
            return None
        if base in add_geometry.reversed and sub in ("start", "end"):
            sub = "end" if sub == "start" else "start"
        return g, _SUB[sub]
    return None


def _merge_tangents(constraints):
    """Turn TANGENT + COINCIDENT at a shared endpoint into one endpoint tangency."""
    coinc = {}
    for i, c in enumerate(constraints):
        if c.get("constraintType") == "COINCIDENT" and c.get("localFirst") and c.get("localSecond"):
            a, b = c["localFirst"], c["localSecond"]
            if a.rsplit(".", 1)[-1] in ("start", "end") and b.rsplit(".", 1)[-1] in ("start", "end"):
                coinc[(a.rsplit(".", 1)[0], b.rsplit(".", 1)[0])] = i
                coinc[(b.rsplit(".", 1)[0], a.rsplit(".", 1)[0])] = i
    out, skip = [], set()
    for i, c in enumerate(constraints):
        if c.get("constraintType") == "TANGENT" and (c.get("localFirst"), c.get("localSecond")) in coinc:
            j = coinc[(c["localFirst"], c["localSecond"])]
            cc = constraints[j]
            skip.add(j)
            out.append({"constraintType": "TANGENT_AT", "localFirst": cc["localFirst"], "localSecond": cc["localSecond"]})
        else:
            out.append(c)
    return [c for i, c in enumerate(out) if i not in skip]


def add_constraints(sk, index, constraints, mode="safe"):
    """Map Onshape constraints onto Sketcher; keep only those that solve."""
    kept, dropped, pending = [], [], []
    constraints = _merge_tangents(constraints)
    for c in constraints:
        t = c.get("constraintType")
        a = _ref(index, c.get("localFirst", "")) if c.get("localFirst") else None
        b = _ref(index, c.get("localSecond", "")) if c.get("localSecond") else None
        cands = []
        if t == "COINCIDENT" and a and b:
            if a[1] and b[1]:
                cands.append(Sketcher.Constraint("Coincident", a[0], a[1], b[0], b[1]))
            elif a[1] and not b[1]:
                cands.append(Sketcher.Constraint("PointOnObject", a[0], a[1], b[0]))
            elif b[1] and not a[1]:
                cands.append(Sketcher.Constraint("PointOnObject", b[0], b[1], a[0]))
        elif t == "HORIZONTAL" and a:
            if a[1] and b and b[1]:
                cands.append(Sketcher.Constraint("Horizontal", a[0], a[1], b[0], b[1]))
            elif not a[1]:
                cands.append(Sketcher.Constraint("Horizontal", a[0]))
        elif t == "VERTICAL" and a:
            if a[1] and b and b[1]:
                cands.append(Sketcher.Constraint("Vertical", a[0], a[1], b[0], b[1]))
            elif not a[1]:
                cands.append(Sketcher.Constraint("Vertical", a[0]))
        elif t == "PARALLEL" and a and b and not a[1] and not b[1]:
            cands.append(Sketcher.Constraint("Parallel", a[0], b[0]))
        elif t == "PERPENDICULAR" and a and b and not a[1] and not b[1]:
            cands.append(Sketcher.Constraint("Perpendicular", a[0], b[0]))
        elif t == "TANGENT" and a and b and not a[1] and not b[1]:
            cands.append(Sketcher.Constraint("Tangent", a[0], b[0]))
        elif t == "TANGENT_AT" and a and b and a[1] and b[1]:
            cands.append(Sketcher.Constraint("Tangent", a[0], a[1], b[0], b[1]))
        elif t == "MIRROR":
            m = _ref(index, c.get("localMirror", c.get("localMaster", "")))
            e1 = _ref(index, c.get("localFirst", ""))
            e2 = _ref(index, c.get("localSecond", ""))
            if m and not m[1] and e1 and e2:
                if e1[1] and e2[1]:
                    cands.append(Sketcher.Constraint("Symmetric", e1[0], e1[1], e2[0], e2[1], m[0]))
                elif not e1[1] and not e2[1]:
                    cands.append(Sketcher.Constraint("Symmetric", e1[0], 1, e2[0], 1, m[0]))
                    cands.append(Sketcher.Constraint("Symmetric", e1[0], 1, e2[0], 2, m[0]))
        elif t == "EQUAL" and a and b and not a[1] and not b[1]:
            cands.append(Sketcher.Constraint("Equal", a[0], b[0]))
        elif t == "CONCENTRIC" and a and b and not a[1] and not b[1]:
            cands.append(Sketcher.Constraint("Coincident", a[0], 3, b[0], 3))
        elif t == "MIDPOINT":
            m = _ref(index, c.get("localMidpoint", ""))
            e1 = _ref(index, c.get("localEntity1", ""))
            e2 = _ref(index, c.get("localEntity2", ""))
            if m and m[1] and e1 and e2 and e1[1] and e2[1]:
                cands.append(Sketcher.Constraint("Symmetric", e1[0], e1[1], e2[0], e2[1], m[0], m[1]))
            elif m and m[1] and e1 and not e1[1] and not e2:
                cands.append(Sketcher.Constraint("Symmetric", e1[0], 1, e1[0], 2, m[0], m[1]))
        elif t == "LENGTH" and a and not a[1] and "length" in c:
            cands.append(Sketcher.Constraint("Distance", a[0], c["length"] * MM))
        elif t == "RADIUS" and a and not a[1] and "length" in c:
            cands.append(Sketcher.Constraint("Radius", a[0], c["length"] * MM))
        elif t == "DIAMETER" and a and not a[1] and "length" in c:
            cands.append(Sketcher.Constraint("Diameter", a[0], c["length"] * MM))
        elif t == "DISTANCE" and a and b and "length" in c:
            L = c["length"] * MM
            d = c.get("direction", "MINIMUM")
            if a[1] and b[1]:
                if d == "HORIZONTAL":
                    cands.append(Sketcher.Constraint("DistanceX", a[0], a[1], b[0], b[1], L))
                    cands.append(Sketcher.Constraint("DistanceX", b[0], b[1], a[0], a[1], L))
                elif d == "VERTICAL":
                    cands.append(Sketcher.Constraint("DistanceY", a[0], a[1], b[0], b[1], L))
                    cands.append(Sketcher.Constraint("DistanceY", b[0], b[1], a[0], a[1], L))
                else:
                    cands.append(Sketcher.Constraint("Distance", a[0], a[1], b[0], b[1], L))
            elif a[1] and not b[1] and index_kind(index, b[0]) == "linesegment":
                cands.append(Sketcher.Constraint("Distance", a[0], a[1], b[0], L))
            elif b[1] and not a[1] and index_kind(index, a[0]) == "linesegment":
                cands.append(Sketcher.Constraint("Distance", b[0], b[1], a[0], L))
        elif t == "ANGLE" and a and b and not a[1] and not b[1] and "angle" in c:
            cands.append(Sketcher.Constraint("Angle", a[0], 1, b[0], 1, c["angle"]))
            cands.append(Sketcher.Constraint("Angle", a[0], 1, b[0], 1, -c["angle"]))
            cands.append(Sketcher.Constraint("Angle", b[0], 1, a[0], 1, c["angle"]))
        elif t == "FIX" and a:
            if a[1]:
                cands.append(Sketcher.Constraint("Block", a[0]))
            else:
                cands.append(Sketcher.Constraint("Block", a[0]))
        if not cands:
            dropped.append((t, "unmapped", c.get("localFirst"), c.get("localSecond")))
            continue
        ok = False
        for cand in cands:
            if _residual(sk, cand) < 1e-6:
                pending.append((t, cand))
                ok = True
                break
        if not ok:
            dropped.append((t, "not satisfied by solved geometry"))
    # add all consistent constraints, then make sure the solver keeps the geometry
    before = _snapshot(sk)
    if not pending:
        return [], dropped
    if mode == "full":
        for t, cand in pending:
            sk.addConstraint(cand)
        kept, extra = _stabilise(sk, before, pending)
        dropped.extend(extra)
        return kept, dropped
    # "safe": at most three whole-set attempts, no per-constraint bisection.
    # Dimensions (lengths, radii, angles) are what usually pulls the imported
    # geometry off its solved position, so they are the first thing to go.
    attempts = [("all", pending),
                ("no dimensions", [x for x in pending if x[1].Type not in DIMENSIONAL])]
    for label, items in attempts:
        if not items:
            continue
        sk.deleteAllConstraints()
        _restore(sk, before)
        for t, cand in items:
            sk.addConstraint(cand)
        sk.solve()
        if not (sk.ConflictingConstraints or sk.MalformedConstraints or sk.RedundantConstraints) \
                and _moved(sk, before) <= 1e-5:
            if label != "all":
                dropped.extend((t, "dropped with " + label) for t, c in pending if c.Type in DIMENSIONAL)
            return [t for t, _ in items], dropped
    sk.deleteAllConstraints()
    _restore(sk, before)
    sk.solve()
    dropped.extend((t, "whole set moved or conflicted the geometry") for t, _ in pending)
    return [], dropped


DIMENSIONAL = {"Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle"}


def _stabilise(sk, before, pending):
    """Keep the largest prefix of constraints that leaves the geometry in place."""
    def bad():
        """Only what actually damages the model counts as bad.

        Onshape sketches routinely translate into constraint sets FreeCAD calls
        partially redundant. That is a warning the user can clean up in the
        GUI; treating it as a failure throws away most of the design intent and
        makes the bisection thrash. Geometry motion, conflicts, fully redundant
        and malformed constraints are real failures.
        """
        sk.solve()
        if sk.ConflictingConstraints or sk.MalformedConstraints or sk.RedundantConstraints:
            return True
        return _moved(sk, before) > 1e-5
    if not bad():
        return [t for t, _ in pending], []
    # bisect: find the first constraint whose addition breaks stability, drop it, continue
    kept, dropped = [], []
    sk.deleteAllConstraints()
    _restore(sk, before)
    lo = 0
    items = list(pending)
    while lo < len(items):
        hi = len(items)
        # try adding items[lo:hi]; shrink hi until stable
        while True:
            n0 = sk.ConstraintCount
            for t, cand in items[lo:hi]:
                sk.addConstraint(cand)
            if not bad():
                kept.extend(t for t, _ in items[lo:hi])
                lo = hi
                break
            # remove what we just added and try a smaller batch
            for i in range(sk.ConstraintCount - 1, n0 - 1, -1):
                sk.delConstraint(i)
            _restore(sk, before)
            sk.solve()
            if hi - lo == 1:
                dropped.append((items[lo][0], "destabilises solver"))
                lo += 1
                break
            hi = lo + (hi - lo) // 2
    return kept, dropped


def _restore(sk, snapshot):
    """Put every geometry element back to the snapshot (exact copies)."""
    cons = [sk.getConstruction(i) for i in range(len(sk.Geometry))]
    sk.Geometry = [g.copy() for g in snapshot.geometry]
    for i, c in enumerate(cons):
        if c != sk.getConstruction(i):
            sk.toggleConstruction(i)


def _pt(sk, g, pos):
    geo = sk.Geometry[g]
    if pos == 1:
        return geo.StartPoint if hasattr(geo, "StartPoint") else App.Vector(geo.X, geo.Y, 0)
    if pos == 2:
        return geo.EndPoint
    if pos == 3:
        return geo.Center
    return None


def _dir(sk, g):
    geo = sk.Geometry[g]
    d = geo.EndPoint - geo.StartPoint
    return d.normalize() if d.Length > 0 else App.Vector(1, 0, 0)


def _residual(sk, c):
    """How far the current geometry is from satisfying a constraint, in mm."""
    t = c.Type
    try:
        if t == "Coincident":
            return (_pt(sk, c.First, c.FirstPos) - _pt(sk, c.Second, c.SecondPos)).Length
        if t == "PointOnObject":
            p = _pt(sk, c.First, c.FirstPos)
            return sk.Geometry[c.Second].toShape().distToShape(Part.Vertex(p))[0]
        if t == "Horizontal":
            if c.FirstPos:
                return abs(_pt(sk, c.First, c.FirstPos).y - _pt(sk, c.Second, c.SecondPos).y)
            return abs(_dir(sk, c.First).y) * 10
        if t == "Vertical":
            if c.FirstPos:
                return abs(_pt(sk, c.First, c.FirstPos).x - _pt(sk, c.Second, c.SecondPos).x)
            return abs(_dir(sk, c.First).x) * 10
        if t == "Parallel":
            return abs(_dir(sk, c.First).cross(_dir(sk, c.Second)).Length) * 10
        if t == "Perpendicular":
            return abs(_dir(sk, c.First).dot(_dir(sk, c.Second))) * 10
        if t == "Tangent":
            g1, g2 = sk.Geometry[c.First], sk.Geometry[c.Second]
            if c.FirstPos and c.SecondPos:
                p = (_pt(sk, c.First, c.FirstPos) - _pt(sk, c.Second, c.SecondPos)).Length
                t1 = g1.tangent(g1.parameter(_pt(sk, c.First, c.FirstPos)))[0]
                t2 = g2.tangent(g2.parameter(_pt(sk, c.Second, c.SecondPos)))[0]
                return p + abs(t1.cross(t2).Length) * 10
            if hasattr(g1, "Center") and hasattr(g2, "Center"):
                d = (g1.Center - g2.Center).Length
                return min(abs(d - (g1.Radius + g2.Radius)), abs(d - abs(g1.Radius - g2.Radius)))
            line, circ = (g1, g2) if hasattr(g2, "Center") else (g2, g1)
            if hasattr(circ, "Center") and hasattr(line, "StartPoint"):
                if hasattr(line, "Center"):
                    return 1e9
                d = line.toShape().distToShape(Part.Vertex(circ.Center))[0]
                ln = Part.LineSegment(line.StartPoint, line.EndPoint)
                inf = Part.Line(line.StartPoint, line.EndPoint)
                d = inf.toShape().distToShape(Part.Vertex(circ.Center))[0]
                return abs(d - circ.Radius)
            if not hasattr(g1, "Center") and not hasattr(g2, "Center"):
                # collinear lines
                return abs(_dir(sk, c.First).cross(_dir(sk, c.Second)).Length) * 10 + Part.Line(g1.StartPoint, g1.EndPoint).toShape().distToShape(Part.Vertex(g2.StartPoint))[0]
            return 1e9
        if t == "Equal":
            g1, g2 = sk.Geometry[c.First], sk.Geometry[c.Second]
            if hasattr(g1, "Radius") and hasattr(g2, "Radius"):
                return abs(g1.Radius - g2.Radius)
            if hasattr(g1, "Radius") or hasattr(g2, "Radius"):
                return 1e9
            return abs((g1.EndPoint - g1.StartPoint).Length - (g2.EndPoint - g2.StartPoint).Length)
        if t == "Symmetric":
            p1 = _pt(sk, c.First, c.FirstPos)
            p2 = _pt(sk, c.Second, c.SecondPos)
            if c.ThirdPos:
                m = _pt(sk, c.Third, c.ThirdPos)
                return ((p1 + p2) * 0.5 - m).Length
            line = sk.Geometry[c.Third]
            inf = Part.Line(line.StartPoint, line.EndPoint).toShape()
            d1 = inf.distToShape(Part.Vertex(p1))[0]
            d2 = inf.distToShape(Part.Vertex(p2))[0]
            mid = (p1 + p2) * 0.5
            return abs(d1 - d2) + inf.distToShape(Part.Vertex(mid))[0]
        if t == "Distance":
            if c.SecondPos:
                return abs((_pt(sk, c.First, c.FirstPos) - _pt(sk, c.Second, c.SecondPos)).Length - c.Value)
            if c.FirstPos:
                line = sk.Geometry[c.Second]
                d = Part.Line(line.StartPoint, line.EndPoint).toShape().distToShape(Part.Vertex(_pt(sk, c.First, c.FirstPos)))[0]
                return abs(d - c.Value)
            g = sk.Geometry[c.First]
            return abs((g.EndPoint - g.StartPoint).Length - c.Value)
        if t == "DistanceX":
            return abs((_pt(sk, c.Second, c.SecondPos).x - _pt(sk, c.First, c.FirstPos).x) - c.Value)
        if t == "DistanceY":
            return abs((_pt(sk, c.Second, c.SecondPos).y - _pt(sk, c.First, c.FirstPos).y) - c.Value)
        if t == "Radius":
            return abs(sk.Geometry[c.First].Radius - c.Value)
        if t == "Diameter":
            return abs(2 * sk.Geometry[c.First].Radius - c.Value)
        if t == "Angle":
            d1, d2 = _dir(sk, c.First), _dir(sk, c.Second)
            a = math.atan2(d1.cross(d2).z, d1.dot(d2))
            return abs(a - c.Value) * 10
        if t == "Block":
            return 0.0
    except Exception:  # noqa: BLE001
        return 1e9
    return 1e9


def index_kind(index, g):
    for gi, k in index.values():
        if gi == g:
            return k
    return None


class _Snapshot(list):
    """Cheap point sample of a sketch, plus geometry copies for exact restore.

    ``_points`` is called on every stability check inside the bisection, so it
    must not copy geometry; the copies are taken once, by ``_snapshot``.
    """

    geometry = ()


def _points(sk):
    pts = []
    for g in sk.Geometry:
        if hasattr(g, "Center") and hasattr(g, "Radius"):
            c = g.Center
            fp = g.FirstParameter if hasattr(g, "StartPoint") else None
            lp = g.LastParameter if fp is not None else None
            pts.append((c.x, c.y, g.Radius, 0.0, fp, lp))
        elif hasattr(g, "StartPoint"):
            a, b = g.StartPoint, g.EndPoint
            pts.append((a.x, a.y, b.x, b.y, None, None))
        elif hasattr(g, "X"):
            pts.append((g.X, g.Y, 0.0, 0.0, None, None))
        else:
            pts.append((0.0, 0.0, 0.0, 0.0, None, None))
    return pts


def _snapshot(sk):
    pts = _Snapshot(_points(sk))
    pts.geometry = [g.copy() for g in sk.Geometry]
    return pts


def _moved(sk, before):
    after = _points(sk)
    if len(after) != len(before):
        return 1e9
    worst = 0.0
    for a, b in zip(after, before):
        worst = max(worst, math.hypot(a[0] - b[0], a[1] - b[1]),
                    math.hypot(a[2] - b[2], a[3] - b[3]))
        if a[4] is not None and b[4] is not None:
            worst = max(worst, abs(a[4] - b[4]) * 10, abs(a[5] - b[5]) * 10)
    return worst


def build_sketch(container, name, sketch, placement=None, support=None, with_constraints=True,
                 mode="safe"):
    if hasattr(container, "newObject"):
        sk = container.newObject("Sketcher::SketchObject", name)
    else:
        sk = container.addObject("Sketcher::SketchObject", name)
    if support is not None:
        sk.AttachmentSupport = support
        sk.MapMode = "FlatFace"
    else:
        sk.MapMode = "Deactivated"
        if placement is not None:
            sk.Placement = placement
    index = add_geometry(sk, sketch["entities"])
    sk.solve()
    report = {"entities": len(index), "kept": 0, "dropped": []}
    if with_constraints:
        frozen = _snapshot(sk)
        kept, dropped = add_constraints(sk, index, sketch["constraints"], mode)
        report["kept"] = len(kept)
        report["dropped"] = dropped
        # The solver runs again on every document recompute and can land on a
        # different solution than the one add_constraints checked. Geometry
        # fidelity outranks parametric intent here: if the recompute moves the
        # profile, the constraints go and the imported geometry stays.
        sk.recompute()
        drift = _moved(sk, frozen)
        if drift > 1e-5:
            sk.deleteAllConstraints()
            _restore(sk, frozen)
            sk.recompute()
            report["kept"] = 0
            report["dropped"] = [("*all*", "recompute moved geometry by %.4f mm" % drift)]
    return sk, index, report
