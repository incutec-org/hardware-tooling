#!/usr/bin/env python3
"""Rebuild an Onshape Part Studio as a native FreeCAD PartDesign tree.

Runs inside FreeCAD:

    freecadcmd -c "import sys; sys.argv=['x', '--fsrep', F, '--resolved', R, '--out', O, '--report', J,
                   '--metadata', M, '--bodydetails', B]; exec(open('onshape_to_freecad.py').read())"

``--metadata`` and ``--bodydetails`` (the captured part metadata and body
details of the same Part Studio) are optional; with them the FreeCAD bodies
are labelled after the Onshape parts they reproduce, matched by bounding box.

Inputs are the captured FeatureScript representation (``onshape_fsrep``) and
the resolved-query cache (``onshape_resolve``). Sketches become Sketcher
objects with their solved geometry and every constraint that maps cleanly.
Extrudes become Pads and Pockets on region objects (``fc_regions``) that stay
linked to the master sketch. Fillets, chamfers, mirrors and booleans use the
resolved geometry to find their targets in the FreeCAD model.

Everything that could not be translated is listed in the JSON report with the
Onshape feature id and name, so the gap is explicit rather than silent.
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else "/Users/stan/Incutec/scripts/hardware/freecad"
sys.path.insert(0, HERE)

import FreeCAD as App  # noqa: E402
import Part  # noqa: E402

import fc_regions  # noqa: E402
import fc_sketch  # noqa: E402
import onshape_fsrep as fr  # noqa: E402
import onshape_query as oq  # noqa: E402

MM = 1000.0
DEFAULT_PLANES = {
    "Top": {"origin": [0, 0, 0], "normal": [0, 0, 1], "x": [1, 0, 0]},
    "Front": {"origin": [0, 0, 0], "normal": [0, -1, 0], "x": [1, 0, 0]},
    "Right": {"origin": [0, 0, 0], "normal": [1, 0, 0], "x": [0, 1, 0]},
}


def V(a):
    return App.Vector(float(a[0]), float(a[1]), float(a[2]))


def placement_from_plane(plane, at_origin=True):
    """Placement whose local XY is the plane; origin in mm.

    A plane read back from a face carries that face's own parametric origin,
    which is not where Onshape puts the sketch coordinate system: a sketch on a
    plane is measured from the global origin projected onto it. Keep the plane
    and its axes, move the origin to that projection.
    """
    o = V(plane["origin"])
    n = V(plane["normal"]).normalize()
    if at_origin:
        o = n * o.dot(n)
    x = V(plane["x"]).normalize()
    y = n.cross(x)
    m = App.Matrix(x.x, y.x, n.x, o.x, x.y, y.y, n.y, o.y, x.z, y.z, n.z, o.z, 0, 0, 0, 1)
    return App.Placement(m)


def qlist(v):
    return v if isinstance(v, list) else ([v] if v is not None else [])


def op_id(q):
    """First component of a query's operationId, or None."""
    if isinstance(q, oq.QueryObject):
        op = q.fields.get("operationId")
        if isinstance(op, oq.Typed) and op.value:
            first = op.value[0]
            return first if isinstance(first, tuple) else (first,)
    return None


class Builder:
    def __init__(self, feats, cache, doc, report, constraints="safe"):
        self.constraints = constraints
        self.feats = feats
        self.cache = cache
        self.doc = doc
        self.report = report
        self.planes = dict(DEFAULT_PLANES)
        dp = cache.get("_default_planes") or {}
        for k in ("Top", "Front", "Right"):
            if k in dp and "normal" in dp[k]:
                self.planes[k] = dp[k]
        self.sketches = {}          # onshape sketch feature id -> (sketch obj, index, feature)
        self.bodies = []            # list of dicts: {"obj": Body, "name": str, "alive": bool}
        self.group = doc.addObject("App::DocumentObjectGroup", "Sketches")
        self.notes = []
        self.match_misses = []
        self.aligned = set()

    # ------------------------------------------------------------ helpers
    def note(self, feat, status, detail=""):
        self.report["features"].append({"index": feat["_i"], "id": feat["id"], "type": feat["type"],
                                        "name": feat["name"], "status": status, "detail": detail})

    def resolved(self, feat, var):
        c = self.cache.get(feat["id"])
        if not c:
            return None
        return c["queries"].get(var)

    def resolved_for_param(self, feat, param):
        """Resolved records for the queries listed in a feature parameter."""
        pairs = self.resolved_pairs(feat, param)
        if pairs is None:
            return None
        return [r for _, recs in pairs for r in recs]

    def resolved_pairs(self, feat, param):
        """[(query object, resolved records)] for a feature parameter."""
        vals = qlist(feat["params"].get(param))
        c = self.cache.get(feat["id"])
        if not c:
            return None
        out = []
        for var, q in feat["queries"].items():
            if any(q is v for v in vals):
                out.append((q, c["queries"].get(var, [])))
        return out

    def live_bodies(self):
        return [b for b in self.bodies if b["alive"]]

    def body_by_record(self, rec, exclude=(), among=None):
        """Match a BODY record (volume mm3, bbox) to a live FreeCAD body."""
        best = None
        for b in (self.live_bodies() if among is None else among):
            if b["obj"] in exclude or not b["obj"].Shape.isValid() or b["obj"].Shape.Volume <= 0:
                continue
            sh = b["obj"].Shape
            bb = sh.BoundBox
            c = App.Vector(bb.XMin + bb.XMax, bb.YMin + bb.YMax, bb.ZMin + bb.ZMax) * 0.5
            rc = (V(rec["bmin"]) + V(rec["bmax"])) * 0.5
            dv = abs(sh.Volume - rec["volume"]) / max(rec["volume"], 1e-9)
            dc = (c - rc).Length
            size = App.Vector(bb.XLength, bb.YLength, bb.ZLength)
            rsize = V(rec["bmax"]) - V(rec["bmin"])
            ds = (size - rsize).Length
            score = dv * 10 + dc + ds
            if best is None or score < best[0]:
                best = (score, b, dv, dc, ds)
        # A candidate set from the query is already an identity match; geometry
        # only orders it. Otherwise the geometry has to carry the whole claim,
        # loosely, because fillets and chamfers not yet applied shift a body by
        # a few percent.
        if best and (among is not None or (best[2] < 0.15 and best[3] < 1.0 and best[4] < 2.0)):
            return best[1]
        self.match_misses.append({"want_volume": round(rec.get("volume", 0.0), 1),
                                  "best": best[1]["name"] if best else None,
                                  "dvol": round(best[2], 3) if best else None,
                                  "dcentre_mm": round(best[3], 3) if best else None,
                                  "dsize_mm": round(best[4], 3) if best else None})
        return None

    def bodies_by_records(self, recs, exclude=()):
        self.match_misses = []
        out = []
        for r in recs or []:
            if r.get("kind") == "BODY":
                b = self.body_by_record(r, exclude)
                if b and b not in out:
                    out.append(b)
        return out

    def bodies_for_param(self, feat, param, exclude=()):
        """Bodies a feature parameter selects.

        An Onshape body query carries an ORIGINAL_DEPENDENCY on the sketch
        entities the body was first built from, so the feature that created it
        is named in the query itself. That is an exact identity and survives
        the model drifting away from Onshape's, which volume and bounding box
        matching does not. Geometry is used only to choose between bodies with
        the same origin, such as a body and its mirror.
        """
        pairs = self.resolved_pairs(feat, param)
        if pairs is None:
            return None
        self.match_misses = []
        out = []
        for q, recs in pairs:
            bodies = [r for r in recs if r.get("kind") == "BODY"]
            if not bodies:
                continue
            origin = set(_feature_ids_in(q))
            for r in bodies:
                cands = [b for b in self.live_bodies()
                         if b["obj"] not in exclude and b["origin"] & origin]
                if len(cands) == 1:
                    # The query named exactly one body. Nothing about its
                    # current shape can make that a different body.
                    if cands[0] not in out:
                        out.append(cands[0])
                    continue
                b = self.body_by_record(r, exclude, among=cands or None)
                if b and b not in out:
                    out.append(b)
                elif not cands:
                    self.match_misses.append({"query_origin": sorted(origin),
                                              "live": {x["name"]: sorted(x["origin"]) for x in self.live_bodies()}})
        return out

    def new_body(self, name, origin=()):
        obj = self.doc.addObject("PartDesign::Body", fc_name(name))
        obj.Label = name
        b = {"obj": obj, "name": name, "alive": True, "origin": set(origin)}
        self.bodies.append(b)
        return b

    def find_edges(self, body, recs, tol=0.05):
        """Edge names of body's tip matching resolved EDGE records."""
        shape = body["obj"].Shape
        names = []
        for r in recs:
            if r.get("kind") != "EDGE":
                continue
            mid = V(r["mid"])
            best = None
            for i, e in enumerate(shape.Edges):
                d = e.distToShape(Part.Vertex(mid))[0]
                if d < tol and abs(e.Length - r["length"]) < 0.02 * r["length"] + 1e-3:
                    if best is None or d < best[0]:
                        best = (d, i)
            if best:
                names.append("Edge%d" % (best[1] + 1))
        return sorted(set(names), key=lambda n: int(n[4:]))

    def find_face(self, body, rec, tol=0.5):
        shape = body["obj"].Shape
        c = V(rec["centroid"]) if "centroid" in rec else V(rec["origin"])
        n = V(rec["normal"])
        best = None
        for i, f in enumerate(shape.Faces):
            d = f.distToShape(Part.Vertex(c))[0]
            if d < tol:
                fn = f.normalAt(0.5, 0.5) if f.Surface.__class__.__name__ == "Plane" else None
                if fn is not None and abs(abs(fn.dot(n)) - 1) > 1e-3:
                    continue
                if best is None or d < best[0]:
                    best = (d, i)
        return "Face%d" % (best[1] + 1) if best else None

    # ------------------------------------------------------------ planes
    def plane_for_query(self, feat, q, var):
        """Plane dict (origin mm, normal, x) for a sketch plane or cPlane reference."""
        oid = op_id(q)
        if oid and oid[0] in self.planes:
            return self.planes[oid[0]], "default"
        if oid and oid[0] in self.planes:
            return self.planes[oid[0]], "cplane"
        for fid, pl in self.planes.items():
            if oid and oid[0] == fid:
                return pl, "cplane"
        recs = self.resolved(feat, var)
        if recs:
            for r in recs:
                if r.get("kind") == "FACE" and r.get("plane"):
                    return r["plane"], "resolved"
        return None, "unresolved"

    # ------------------------------------------------------------ features
    def build(self):
        for i, f in enumerate(self.feats):
            f["_i"] = i
            try:
                handler = getattr(self, "f_" + f["type"], None)
                if handler is None:
                    self.note(f, "unsupported", "no handler for feature type")
                    continue
                handler(f)
            except Exception as ex:  # noqa: BLE001
                self.note(f, "error", repr(ex)[:300])
            self.doc.recompute()

    def f_newSketch(self, f):
        qs = qlist(f["params"].get("sketchPlane"))
        var = next((v for v, q in f["queries"].items() if qs and q is qs[0]), None)
        plane, how = self.plane_for_query(f, qs[0] if qs else None, var)
        if plane is None:
            self.note(f, "skipped", "sketch plane unresolved")
            return
        pl = placement_from_plane(plane)
        sk, index, rep = fc_sketch.build_sketch(self.group, f["name"] or f["id"], f["sketch"], placement=pl,
                                                with_constraints=self.constraints != "none",
                                                mode=self.constraints)
        sk.Label = f["name"] or f["id"]
        self.sketches[f["id"]] = (sk, index, f)
        self.note(f, "ok", "plane=%s geometry=%d constraints kept=%d dropped=%d" % (how, rep["entities"], rep["kept"], len(rep["dropped"])))
        self.report["sketch_constraints"][f["id"]] = {"name": f["name"], "kept": rep["kept"], "dropped": [list(d) for d in rep["dropped"]]}

    def f_cPlane(self, f):
        p = f["params"]
        if p.get("cplaneType") != "OFFSET":
            self.note(f, "unsupported", "cPlane type %s" % p.get("cplaneType"))
            return
        recs = self.resolved_for_param(f, "entities")
        ref = next((r for r in (recs or []) if r.get("kind") == "FACE" and r.get("plane")), None)
        if ref is None:
            self.note(f, "skipped", "reference face unresolved")
            return
        base = ref["plane"]
        n = V(base["normal"]).normalize()
        d = float(p.get("offset", 0.0)) * MM * (-1.0 if p.get("oppositeDirection") else 1.0)
        o = V(base["origin"]) + n * d
        self.planes[f["id"]] = {"origin": [o.x, o.y, o.z], "normal": base["normal"], "x": base["x"]}
        self.note(f, "ok", "offset plane at %s" % [round(v, 3) for v in (o.x, o.y, o.z)])

    def align_sketch(self, sk, wanted, index):
        """Correct a sketch placement against the region centroids Onshape reports.

        Onshape's sketch coordinate system is not always the reference face's
        own plane frame, so a sketch built on a resolved plane can land offset
        inside its plane. The regions an extrude selects give the true global
        centroid of geometry we also have locally, so the offset is measurable:
        take the in-plane shift that lines up every selected region at once, and
        move the sketch by it. A shift is applied only when one value fits all
        the regions, so an ambiguous or already correct sketch is left alone.
        """
        if not wanted or sk.Name in self.aligned:
            return None
        regions = fc_regions.compute_regions(sk.Shape, placement=sk.Placement)
        if not regions:
            return None
        n = sk.Placement.Rotation.multVec(App.Vector(0, 0, 1))
        common = None
        for c, area in wanted:
            deltas = []
            for f in regions:
                if abs(f.Area - area) > 0.15 * area + 1e-6:
                    continue
                d = V(c) - f.CenterOfMass
                d = d - n * d.dot(n)          # keep the shift inside the plane
                deltas.append(d)
            if not deltas:
                return None
            common = deltas if common is None else [a for a in common
                                                    if any((a - b).Length < 1e-3 for b in deltas)]
            if not common:
                return None
        d = common[0]
        self.aligned.add(sk.Name)
        if d.Length < 1e-6:
            return None
        pl = App.Placement(sk.Placement)
        pl.Base = pl.Base + d
        sk.Placement = pl
        self.doc.recompute()
        return [round(v, 4) for v in tuple(d)]

    def sketch_for_regions(self, f):
        """The master sketch whose regions an extrude uses."""
        for q in qlist(f["params"].get("entities")):
            for oid in _sketch_ids_in(q):
                if oid in self.sketches:
                    return self.sketches[oid]
        return None

    def f_extrude(self, f):
        p = f["params"]
        if p.get("bodyType") not in (None, "SOLID"):
            self.note(f, "unsupported", "bodyType %s" % p.get("bodyType"))
            return
        src = self.sketch_for_regions(f)
        if src is None:
            self.note(f, "skipped", "source sketch not built")
            return
        sk, index, skf = src
        # regions: resolved FACE records (centroid+area) preferred
        recs = self.resolved_for_param(f, "entities") or []
        on_plane = [(r["centroid"], r["area"]) for r in recs if r.get("kind") == "FACE"
                    and abs((V(r["centroid"]) - sk.Placement.Base).dot(
                        sk.Placement.Rotation.multVec(App.Vector(0, 0, 1)))) < 1e-4]
        shift = self.align_sketch(sk, on_plane, index)
        if shift:
            self.report["sketch_alignment"].append({"sketch": sk.Label, "by": f["name"] or f["id"], "shift_mm": shift})
        probes = []
        inv = sk.Placement.inverse()
        for r in recs:
            if r.get("kind") == "FACE":
                c = inv.multVec(V(r["centroid"]))
                probes.append((round(c.x, 6), round(c.y, 6), 0.0, r["area"]))
        wanted = None
        if not probes:
            wanted = [set(_entity_ids_in(q)) for q in qlist(p.get("entities"))]
        # start offset
        offset = 0.0
        if p.get("startOffset"):
            n = sk.Placement.Rotation.multVec(App.Vector(0, 0, 1))
            if p.get("startOffsetBound") == "ENTITY":
                ents = self.resolved_for_param(f, "startOffsetEntity") or []
                fr_ = next((r for r in ents if r.get("kind") == "FACE"), None)
                if fr_ is None:
                    self.note(f, "skipped", "start offset entity unresolved")
                    return
                offset = (V(fr_["origin"]) - sk.Placement.Base).dot(n)
            else:
                offset = float(p.get("startOffsetDistance", 0.0)) * MM * (-1.0 if p.get("startOffsetOppositeDirection") else 1.0)
        op = p.get("operationType", "NEW")
        depth = float(p.get("depth", 0.0)) * MM
        bound = p.get("endBound", "BLIND")
        targets = []
        if op == "NEW":
            targets = [self.new_body(f["name"] or f["id"], origin={f["id"], skf["id"]})]
        elif op in ("ADD", "REMOVE"):
            targets = self.bodies_for_param(f, "booleanScope") or []
            if not targets:
                targets = self.bodies_intersecting(sk, probes, wanted, index, depth, p, offset)
            if not targets:
                self.report.setdefault("scope_misses", []).append({"index": f["_i"], "name": f["name"], "misses": self.match_misses})
                self.note(f, "skipped", "no target body for %s" % op)
                return
        else:
            self.note(f, "unsupported", "operationType %s" % op)
            return
        made, depths = [], []
        for b in targets:
            body = b["obj"]
            reg = fc_regions.make_region(body, fc_name((f["name"] or f["id"]) + "_profile"), sk, wanted, index, probes=probes, offset=offset)
            reg.Label = (f["name"] or f["id"]) + " profile"
            if probes and any(r.get("got") is None for r in (reg.Proxy.report or [])):
                # Retry with the live solids cutting the sketch plane: the
                # region Onshape selected may be bounded by an imprinted edge
                # rather than by a curve of the sketch.
                before = sum(1 for r in reg.Proxy.report if r.get("got") is not None)
                reg.Cutters = [x["obj"] for x in self.live_bodies()]
                reg.recompute()
                after = sum(1 for r in (reg.Proxy.report or []) if r.get("got") is not None)
                if after <= before:
                    reg.Cutters = []
                    reg.recompute()
            if reg.Shape.isNull() or not reg.Shape.Faces:
                self.note(f, "skipped", "no region matched: %s" % json.dumps(reg.Proxy.report, default=str)[:300])
                body.removeObject(reg)
                self.doc.removeObject(reg.Name)
                if op == "NEW":
                    self.doc.removeObject(body.Name)
                    b["alive"] = False
                return
            typ = "PartDesign::Pad" if op in ("NEW", "ADD") else "PartDesign::Pocket"
            feat = body.newObject(typ, fc_name(f["name"] or f["id"]))
            feat.Label = f["name"] or f["id"]
            feat.Profile = (reg, [])
            feat.Reversed = bool(p.get("oppositeDirection"))
            if p.get("symmetric"):
                feat.Midplane = True
                feat.Length = depth
            elif bound == "BLIND":
                feat.Type = "Length"
                feat.Length = depth
            elif bound == "THROUGH_ALL":
                feat.Type = "ThroughAll" if typ.endswith("Pocket") else "UpToLast"
            elif bound == "UP_TO_NEXT":
                feat.Type = "UpToFirst"
            elif bound in ("UP_TO_SURFACE", "UP_TO_BODY"):
                fr_ = next((r for r in (self.resolved_for_param(f, "endBoundEntityFace") or []) if r.get("kind") == "FACE"), None)
                fname = self.find_face(b, fr_) if fr_ else None
                if fname is not None:
                    feat.Type = "UpToFace"
                    feat.UpToFace = (body.Tip if body.Tip and body.Tip is not feat else body, [fname])
                elif fr_ is not None:
                    # The target face is not in this body's current shape, but
                    # Onshape resolved where it is, so the distance from the
                    # profile to that plane is the length in every case where
                    # the face is parallel to the sketch, which is what an
                    # up-to-surface bound is used for here.
                    n = sk.Placement.Rotation.multVec(App.Vector(0, 0, 1))
                    L = (V(fr_["origin"]) - (sk.Placement.Base + n * offset)).dot(n)
                    feat.Type = "Length"
                    feat.Length = abs(L)
                    feat.Reversed = bool(p.get("oppositeDirection")) != (L < 0)
                    depths.append("%s=%.3f" % (b["name"], abs(L)))
                else:
                    self.note(f, "skipped", "up-to face unresolved in %s" % b["name"])
                    continue
            else:
                self.note(f, "unsupported", "endBound %s" % bound)
                continue
            if p.get("hasDraft"):
                feat.TaperAngle = math.degrees(float(p.get("draftAngle", 0.0))) * (-1.0 if p.get("draftPullDirection") else 1.0)
            self.doc.recompute()
            if not feat.isValid():
                self.note(f, "error", "%s invalid in body %s: %s" % (typ, b["name"], feat.State))
                continue
            made.append(b["name"])
        if made:
            detail = "%s %s in %s (%d regions)" % (op, bound, made, len(probes) if probes else len(wanted or []))
            if depths:
                detail += " up-to as length " + ", ".join(depths)
            approx = sum(1 for r in (reg.Proxy.report or []) if r.get("approximate"))
            if approx:
                detail += " [%d region(s) matched approximately]" % approx
            self.note(f, "partial" if (approx or len(made) < len(targets)) else "ok", detail)

    def bodies_intersecting(self, sk, probes, wanted, index, depth, p, offset):
        """Fallback scope: live bodies touched by the extrusion prism."""
        regions = fc_regions.compute_regions(sk.Shape, placement=sk.Placement)
        sources = fc_regions.edge_sources(sk, index)
        faces = fc_regions.match_by_probe(regions, probes, sk.Placement)[0] if probes else fc_regions.match_regions(regions, sources, wanted)[0]
        if not faces:
            return []
        n = sk.Placement.Rotation.multVec(App.Vector(0, 0, 1))
        L = depth if depth > 0 else 200.0
        if p.get("endBound") in ("THROUGH_ALL", "UP_TO_SURFACE", "UP_TO_NEXT"):
            L = 500.0
        vec = n * (-L if p.get("oppositeDirection") else L)
        solids = []
        for fc in faces:
            g = fc.copy()
            g.translate(n * offset)
            if p.get("symmetric"):
                g.translate(-vec * 0.5)
            solids.append(g.extrude(vec))
        prism = Part.makeCompound(solids)
        out = []
        for b in self.live_bodies():
            sh = b["obj"].Shape
            if sh.isNull() or not sh.Solids:
                continue
            try:
                if sh.common(prism).Volume > 1e-6:
                    out.append(b)
            except Exception:  # noqa: BLE001
                pass
        return out

    def _blend(self, f, kind):
        recs = self.resolved_for_param(f, "entities")
        if recs is None:
            self.note(f, "skipped", "edges unresolved (no cache entry)")
            return
        edges = [r for r in recs if r.get("kind") == "EDGE"]
        if not edges:
            self.note(f, "skipped", "no edge records resolved")
            return
        per_body = {}
        for b in self.live_bodies():
            names = self.find_edges(b, edges)
            if names:
                per_body[b["name"]] = (b, names)
        found = sum(len(v[1]) for v in per_body.values())
        if not per_body:
            self.note(f, "skipped", "none of %d edges found in any body" % len(edges))
            return
        p = f["params"]
        done = []
        for name, (b, names) in per_body.items():
            body = b["obj"]
            if kind == "fillet":
                feat = body.newObject("PartDesign::Fillet", fc_name(f["name"] or f["id"]))
                feat.Radius = float(p.get("radius", 0.0)) * MM
            else:
                feat = body.newObject("PartDesign::Chamfer", fc_name(f["name"] or f["id"]))
                ct = p.get("chamferType", "EQUAL_OFFSETS")
                if ct == "EQUAL_OFFSETS":
                    feat.ChamferType = "Equal distance"
                    feat.Size = float(p.get("width", 0.0)) * MM
                elif ct == "TWO_OFFSETS":
                    feat.ChamferType = "Two distances"
                    feat.Size = float(p.get("width1", 0.0)) * MM
                    feat.Size2 = float(p.get("width2", 0.0)) * MM
                    feat.FlipDirection = bool(p.get("oppositeDirection"))
                else:
                    feat.ChamferType = "Distance and angle"
                    feat.Size = float(p.get("width1", p.get("width", 0.0))) * MM
                    feat.Angle = math.degrees(float(p.get("angle", math.pi / 4)))
                    feat.FlipDirection = bool(p.get("oppositeDirection"))
            feat.Label = f["name"] or f["id"]
            feat.Base = (body.Tip if body.Tip is not feat else feat.BaseFeature, names)
            self.doc.recompute()
            if not feat.isValid():
                self.note(f, "error", "%s failed in %s on %s" % (kind, name, names[:6]))
                body.removeObject(feat)
                self.doc.removeObject(feat.Name)
                self.doc.recompute()
                continue
            done.append("%s:%d" % (name, len(names)))
        self.note(f, "ok" if found == len(edges) else "partial", "%s edges %d/%d in %s" % (kind, found, len(edges), done))

    def f_fillet(self, f):
        self._blend(f, "fillet")

    def f_chamfer(self, f):
        self._blend(f, "chamfer")

    def f_mirror(self, f):
        p = f["params"]
        if p.get("patternType") != "PART":
            self.note(f, "unsupported", "mirror type %s" % p.get("patternType"))
            return
        srcs = self.bodies_for_param(f, "entities")
        if srcs is None:
            self.note(f, "skipped", "mirror bodies unresolved (no cache entry)")
            return
        if not srcs:
            self.note(f, "skipped", "mirror source bodies not matched")
            return
        mq = qlist(p.get("mirrorPlane"))
        var = next((v for v, q in f["queries"].items() if mq and q is mq[0]), None)
        plane, how = self.plane_for_query(f, mq[0] if mq else None, var)
        if plane is None:
            self.note(f, "skipped", "mirror plane unresolved")
            return
        names = []
        for b in srcs:
            mir = self.doc.addObject("Part::Mirroring", fc_name(b["name"] + "_mirror"))
            mir.Source = b["obj"]
            mir.Base = V(plane["origin"])
            mir.Normal = V(plane["normal"])
            self.doc.recompute()
            nb = self.new_body(b["name"] + " mirror", origin=b["origin"] | {f["id"]})
            nb["obj"].BaseFeature = mir
            names.append(nb["name"])
        self.note(f, "ok", "mirrored %s -> %s" % ([b["name"] for b in srcs], names))

    def f_booleanBodies(self, f):
        p = f["params"]
        if p.get("operationType") not in ("SUBTRACTION", "UNION", "INTERSECTION"):
            self.note(f, "unsupported", "boolean %s" % p.get("operationType"))
            return
        tools = self.bodies_for_param(f, "tools")
        if tools is None:
            self.note(f, "skipped", "boolean bodies unresolved (no cache entry)")
            return
        targets = self.bodies_for_param(f, "targets", exclude=[t["obj"] for t in tools]) or []
        if not tools or not targets:
            self.note(f, "skipped", "boolean tools/targets not matched")
            return
        typ = {"SUBTRACTION": "Cut", "UNION": "Fuse", "INTERSECTION": "Common"}[p["operationType"]]
        for t in targets:
            bo = t["obj"].newObject("PartDesign::Boolean", fc_name(f["name"] or f["id"]))
            bo.Type = typ
            bo.addObjects([x["obj"] for x in tools])
            self.doc.recompute()
        if not p.get("keepTools", True):
            for x in tools:
                x["alive"] = False
                self.doc.removeObject(x["obj"].Name)
        self.note(f, "ok", "%s %s with %s" % (typ, [t["name"] for t in targets], [x["name"] for x in tools]))

    def f_deleteBodies(self, f):
        gone_recs = self.bodies_for_param(f, "entities")
        if gone_recs is None:
            self.note(f, "skipped", "bodies unresolved (no cache entry)")
            return
        gone = []
        for b in gone_recs:
            b["alive"] = False
            self.doc.removeObject(b["obj"].Name)
            gone.append(b["name"])
        self.note(f, "ok" if gone else "skipped", "deleted %s" % gone)

    def f_importForeign(self, f):
        self.note(f, "unsupported", "foreign import (STEP/DXF) must be placed manually")

    def f_unknown(self, f):
        self.note(f, "unsupported", "unknown feature")


def _feature_ids_in(q, out=None):
    """Every feature id named by an operationId anywhere inside a query."""
    out = set() if out is None else out
    if isinstance(q, oq.QueryObject):
        oid = op_id(q)
        if oid:
            out.add(oid[0])
        for v in q.fields.values():
            _feature_ids_in(v, out)
    elif isinstance(q, dict):
        for v in q.values():
            _feature_ids_in(v, out)
    elif isinstance(q, list):
        for v in q:
            _feature_ids_in(v, out)
    elif isinstance(q, oq.Typed):
        _feature_ids_in(q.value, out)
    return out


def _sketch_ids_in(q, out=None):
    out = [] if out is None else out
    if isinstance(q, oq.QueryObject):
        if q.fields.get("queryType") == "SKETCH_ENTITY":
            oid = op_id(q)
            if oid:
                out.append(oid[0])
        for v in q.fields.values():
            _sketch_ids_in(v, out)
    elif isinstance(q, dict):
        for v in q.values():
            _sketch_ids_in(v, out)
    elif isinstance(q, list):
        for v in q:
            _sketch_ids_in(v, out)
    elif isinstance(q, oq.Typed):
        _sketch_ids_in(q.value, out)
    return out


def _entity_ids_in(q, out=None):
    out = [] if out is None else out
    if isinstance(q, oq.QueryObject):
        if q.fields.get("queryType") == "SKETCH_ENTITY":
            e = q.fields.get("sketchEntityId")
            out.append(".".join(e) if isinstance(e, tuple) else e)
        for v in q.fields.values():
            _entity_ids_in(v, out)
    elif isinstance(q, dict):
        for v in q.values():
            _entity_ids_in(v, out)
    elif isinstance(q, list):
        for v in q:
            _entity_ids_in(v, out)
    elif isinstance(q, oq.Typed):
        _entity_ids_in(q.value, out)
    return out


def fc_name(s):
    out = "".join(ch if ch.isalnum() else "_" for ch in str(s))
    return out if out and not out[0].isdigit() else "F_" + out


def name_bodies(b, metadata, bodydetails, report):
    """Rename FreeCAD bodies after the Onshape parts they reproduce (bbox match)."""
    names = {}
    for it in metadata.get("items", []):
        nm = next((pp.get("value") for pp in it.get("properties", []) if pp.get("name") == "Name"), None)
        if it.get("partId") and nm:
            names[it["partId"]] = nm
    boxes = {}
    for bd in bodydetails.get("bodies", []):
        pts = [v["point"] for v in bd.get("vertices", [])]
        if not pts:
            continue
        xs, ys, zs = zip(*[(p["x"], p["y"], p["z"]) for p in pts])
        boxes[bd["id"]] = [min(xs) * MM, min(ys) * MM, min(zs) * MM, max(xs) * MM, max(ys) * MM, max(zs) * MM]
    used = set()
    for body in b.live_bodies():
        sh = body["obj"].Shape
        if not sh.isValid():
            continue
        bb = sh.BoundBox
        mine = [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]
        best = None
        for pid, box in boxes.items():
            if pid in used:
                continue
            d = max(abs(a - c) for a, c in zip(mine, box))
            if best is None or d < best[0]:
                best = (d, pid)
        if best and best[0] < 1.0:
            used.add(best[1])
            nm = names.get(best[1], best[1])
            body["obj"].Label = nm
            body["onshape_part"] = best[1]
            body["part_name"] = nm
            report["part_match"].append({"body": body["name"], "part": nm, "partId": best[1], "bbox_delta_mm": round(best[0], 3)})
        else:
            report["part_match"].append({"body": body["name"], "part": None, "bbox_delta_mm": round(best[0], 3) if best else None})
    missing = [names.get(pid, pid) for pid in boxes if pid not in used]
    report["parts_not_reproduced"] = missing


def main(argv):
    args = dict(zip(argv[1::2], argv[2::2]))
    fsrep = json.load(open(args["--fsrep"]))
    cache = json.load(open(args["--resolved"])) if args.get("--resolved") and os.path.exists(args["--resolved"]) else {}
    feats = fr.parse(fsrep)
    doc = App.newDocument("Port")
    report = {"features": [], "sketch_constraints": {}, "sketch_alignment": [], "bodies": [], "part_match": [], "parts_not_reproduced": []}
    b = Builder(feats, cache, doc, report, constraints=args.get("--constraints", "safe"))
    b.build()
    doc.recompute()
    if args.get("--metadata") and args.get("--bodydetails"):
        name_bodies(b, json.load(open(args["--metadata"])), json.load(open(args["--bodydetails"])), report)
    for bd in b.live_bodies():
        sh = bd["obj"].Shape
        report["bodies"].append({"name": bd["name"], "part": bd.get("part_name"), "valid": sh.isValid(), "volume_mm3": round(sh.Volume, 3) if sh.isValid() else None,
                                 "solids": len(sh.Solids), "bbox": [round(v, 3) for v in (sh.BoundBox.XMin, sh.BoundBox.YMin, sh.BoundBox.ZMin, sh.BoundBox.XMax, sh.BoundBox.YMax, sh.BoundBox.ZMax)] if sh.isValid() else None})
    doc.saveAs(args["--out"])
    json.dump(report, open(args["--report"], "w"), indent=1, default=str)
    counts = {}
    for fe in report["features"]:
        counts[fe["status"]] = counts.get(fe["status"], 0) + 1
    print("BUILD DONE", counts, "bodies", len(report["bodies"]), "total volume", round(sum(x["volume_mm3"] or 0 for x in report["bodies"]), 1))


if __name__ == "__main__":
    main(sys.argv)
