#!/usr/bin/env python3
"""Parse an Onshape Part Studio FeatureScript representation into plain data.

The representation (``GET .../featurescriptrepresentation``) is the parse tree
of the generated FeatureScript module. Every feature is an assignment
``features.<id> = function(id) { ... }`` whose body declares the feature's
queries, then calls the feature function with a parameter map. Sketches add
one ``sk*`` call per entity, one ``skConstraint`` per constraint and an
``initialGuess`` map holding the solved entity positions.

``parse(fsrep)`` returns a list of feature dicts in document order:

    {"id", "type", "name", "params", "queries", "sketch"}

``params`` holds plain Python values: numbers in SI units (metres, radians),
enum member names, booleans, lists, and for query parameters a list of decoded
query objects (see ``onshape_query``). ``sketch`` is present for ``newSketch``
features and holds ``entities`` with solved geometry in metres and
``constraints``.
"""
from __future__ import annotations

import json
import math
import sys

import onshape_query as oq

_SKIP_SK = {"skSolve", "skSetInitialGuess"}
_UNIT = {"millimeter": 1e-3, "meter": 1.0, "centimeter": 1e-2, "inch": 0.0254, "foot": 0.3048,
         "degree": math.pi / 180.0, "radian": 1.0}


class _Expr:
    """Evaluate the small expression subset used by feature parameter maps."""

    def __init__(self, queries: dict):
        self.queries = queries

    def ev(self, o):
        if not isinstance(o, dict):
            return o
        bt = o.get("btType", "").split("-")[0]
        if bt == "BTPLiteralString":
            return _unquote(o.get("text", ""))
        if bt == "BTPLiteralNumber":
            return float(o.get("value", 0.0))
        if bt == "BTPLiteralBoolean":
            return bool(o.get("value", False))
        if bt == "BTPLiteralMap":
            return {self.ev(e["key"]): self.ev(e["value"]) for e in o.get("entries", [])}
        if bt == "BTPLiteralArray":
            return [self.ev(v) for v in o.get("value", [])]
        if bt == "BTPIdentifier":
            return o.get("identifier")
        if bt == "BTPName":
            return self.ev(o.get("identifier"))
        if bt == "BTPExpressionVarReference":
            name = self.ev(o.get("name"))
            if name in self.queries:
                return self.queries[name]
            if name in _UNIT:
                return _UNIT[name]
            return {"var": name}
        if bt == "BTPExpressionAccess":
            base = self.ev(o.get("base"))
            acc = self.ev(o.get("accessor"))
            if isinstance(base, dict) and "var" in base:
                return acc                       # Enum.MEMBER -> "MEMBER"
            if isinstance(base, dict) and acc in base:
                return base[acc]                 # {value:..}.value
            return {"access": (base, acc)}
        if bt == "BTPExpressionTry":
            return self.ev(o.get("expression"))
        if bt == "BTPExpressionOperator":
            a, b, op = self.ev(o.get("operand1")), self.ev(o.get("operand2")), o.get("operator")
            if op == "TIMES" and isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return a * b
            if op == "PLUS" and isinstance(a, str) and isinstance(b, str):
                return a + b
            if op == "MINUS" and isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return a - b
            if op == "DIVIDE" and isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return a / b
            return {"op": op, "a": a, "b": b}
        if bt == "BTPExpressionCall":
            fn = self.ev(o.get("functionExpression"))
            if isinstance(fn, dict) and "var" in fn:
                fn = fn["var"]
            args = [self.ev(a) for a in o.get("arguments", [])]
            if fn == "qUnion":
                out = []
                for a in args[0] if args else []:
                    out.extend(a if isinstance(a, list) else [a])
                return out
            if fn == "featureList":
                return []
            return {"call": fn, "args": args}
        if bt == "BTPExpressionAs":
            return self.ev(o.get("expression")) if "expression" in o else {"as": None}
        if bt == "BTPExpressionFunction":
            return {"function": None}
        return {"node": bt}


def _unquote(t: str) -> str:
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    return t


def _statements(node) -> list:
    """Flatten the statements of a feature function body."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            bt = o.get("btType", "")
            if bt.startswith("BTPStatement") and not (bt.startswith("BTPStatementBlock") or bt.startswith("BTPStatementIf")):
                out.append(o)
                return
            for k, v in o.items():
                if k != "annotation":
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(node)
    return out


def _annotation_name(st) -> str | None:
    ann = st.get("annotation")
    if not ann:
        return None
    for e in ann.get("value", {}).get("entries", []):
        if _unquote(e["key"].get("text", "")) == "Feature Name":
            return _unquote(e["value"].get("text", ""))
    return None


def _feature_assignments(fsrep: dict) -> list:
    out = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("btType", "").startswith("BTPStatementAssignment"):
                lv = o.get("lvalue", {})
                if lv.get("base", {}).get("name", {}).get("identifier") == "features":
                    out.append(o)
                    return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(fsrep)
    return out


def _sketch_entities(calls: list, guess: dict) -> list:
    """Turn sk* calls plus the initial-guess map into solved entities.

    Initial-guess layouts (metres, radians):
      line   [px, py, dx, dy, t0, t1]      start = p + d*t0, end = p + d*t1
      point  [x, y]
      circle [cx, cy, ax, ay, r, dir]
      arc    [cx, cy, ax, ay, r, dir, t0, t1]   angles measured from axis a
    """
    ents = []
    for fn, eid, opts in calls:
        g = guess.get(eid)
        e = {"id": eid, "kind": fn[2:].lower(), "construction": bool(opts.get("construction", False)),
             "index": opts.get("index")}
        if g is None:
            e["unsolved"] = True
        elif fn == "skLineSegment" and len(g) >= 6:
            px, py, dx, dy, t0, t1 = g[:6]
            e["start"] = (px + dx * t0, py + dy * t0)
            e["end"] = (px + dx * t1, py + dy * t1)
        elif fn == "skPoint" and len(g) >= 2:
            e["point"] = (g[0], g[1])
        elif fn == "skCircle" and len(g) >= 5:
            e["center"] = (g[0], g[1])
            e["radius"] = g[4]
            e["axis"] = (g[2], g[3])
            e["clockwise"] = len(g) > 5 and g[5] < 0
        elif fn == "skArc" and len(g) >= 8:
            # Verified on 277 constrained arc endpoints: the world angle of
            # parameter t is atan2(ay, ax) - d * t, where d is the direction
            # flag. Onshape's start is at t0 and its end at t1.
            cx, cy, ax, ay, r, d, t0, t1 = g[:8]
            e["center"] = (cx, cy)
            e["radius"] = r
            e["axis"] = (ax, ay)
            e["clockwise"] = d < 0
            e["t0"], e["t1"] = t0, t1
            base = math.atan2(ay, ax)
            s = -1.0 if d < 0 else 1.0
            th0, th1 = base - s * t0, base - s * t1
            e["start"] = (cx + r * math.cos(th0), cy + r * math.sin(th0))
            e["end"] = (cx + r * math.cos(th1), cy + r * math.sin(th1))
            # Counter-clockwise angle range for FreeCAD, and whether FreeCAD's
            # start point corresponds to Onshape's end point.
            if s > 0:
                a0, a1, e["reversed"] = th1, th0, True
            else:
                a0, a1, e["reversed"] = th0, th1, False
            while a1 <= a0:
                a1 += 2 * math.pi
            e["a0"], e["a1"] = a0, a1
        else:
            e["raw"] = g
        ents.append(e)
    return ents


def parse(fsrep: dict) -> list:
    features = []
    for a in _feature_assignments(fsrep):
        fid = a["lvalue"]["accessor"]["identifier"]
        sts = _statements(a["rvalue"])
        queries: dict = {}
        feat = {"id": fid, "type": None, "name": None, "params": {}, "queries": queries}
        sk_calls, constraints, guess = [], [], {}
        ex = _Expr(queries)
        for st in sts:
            bt = st["btType"].split("-")[0]
            if bt == "BTPStatementCompressedQuery":
                text = st.get("query", "")
                var, _, rest = text.partition("=")
                var = var.strip()
                if "qCompressed(" in rest:
                    payload = rest.split('"')[1]
                    queries[var] = oq.decode(payload)
                else:
                    queries[var] = {"fs": rest.strip().rstrip(";")}
            elif bt == "BTPStatementConstantDeclaration":
                name = st.get("name", {}).get("identifier", "")
                if name.startswith("initialGuess"):
                    for e in st["value"].get("entries", []):
                        guess[_unquote(e["key"]["text"])] = [float(v.get("value", 0.0)) for v in e["value"].get("value", [])]
            elif bt == "BTPStatementVarDeclaration":
                name = st.get("name", {}).get("identifier", "")
                val = st.get("value")
                if name == "sketch" and val:
                    feat["type"] = "newSketch"
                    feat["name"] = _annotation_name(st)
                    call = ex.ev(val)
                    feat["params"] = call.get("args", [None, None, {}])[2] if isinstance(call, dict) else {}
            elif bt == "BTPStatementExpression":
                e = st.get("expression", {})
                ebt = e.get("btType", "").split("-")[0]
                if ebt == "BTPExpressionTry":
                    continue
                fe = e.get("functionExpression", {})
                fn = fe.get("name", {}).get("identifier", {}) if isinstance(fe.get("name"), dict) else None
                fn = fn.get("identifier") if isinstance(fn, dict) else fn
                if fn is None or fn in _SKIP_SK:
                    continue
                args = e.get("arguments", [])
                if fn == "skConstraint":
                    opts = ex.ev(args[2]) if len(args) > 2 else {}
                    constraints.append({"id": _unquote(args[1].get("text", "")), **opts})
                elif fn and fn.startswith("sk"):
                    opts = ex.ev(args[2]) if len(args) > 2 else {}
                    sk_calls.append((fn, _unquote(args[1].get("text", "")), opts))
                elif fn:
                    feat["type"] = fn
                    feat["name"] = feat["name"] or _annotation_name(st)
                    feat["params"] = ex.ev(args[2]) if len(args) > 2 else {}
        if feat["type"] == "newSketch":
            feat["sketch"] = {"entities": _sketch_entities(sk_calls, guess), "constraints": constraints}
        if feat["type"] is None:
            feat["type"] = "unknown"
        features.append(feat)
    return features


def summary(features: list) -> str:
    lines = []
    for i, f in enumerate(features):
        extra = ""
        if f["type"] == "newSketch":
            sk = f["sketch"]
            extra = f"  entities={len(sk['entities'])} constraints={len(sk['constraints'])}"
        elif f["type"] in ("extrude", "fillet", "chamfer", "cPlane", "mirror", "booleanBodies", "deleteBodies"):
            p = f["params"]
            keys = {"extrude": ("operationType", "endBound", "depth", "oppositeDirection", "symmetric", "startOffset"),
                    "fillet": ("radius",), "chamfer": ("chamferType", "width"),
                    "cPlane": ("cplaneType", "offset", "oppositeDirection"), "mirror": ("patternType", "operationType"),
                    "booleanBodies": ("operationType", "keepTools"), "deleteBodies": ()}[f["type"]]
            extra = "  " + " ".join(f"{k}={p.get(k)}" for k in keys)
            extra += f"  entities={len(p.get('entities', []) or [])}"
        lines.append(f"{i:3d} {f['type']:14s} {f['id']:22s} {str(f['name'] or ''):24s}{extra}")
    return "\n".join(lines)


if __name__ == "__main__":
    feats = parse(json.load(open(sys.argv[1])))
    print(summary(feats))
    if len(sys.argv) > 2:
        f = feats[int(sys.argv[2])]
        print(json.dumps({k: v for k, v in f.items() if k != "queries"}, default=repr, indent=1)[:6000])
        for k, q in f["queries"].items():
            print(k, "=", repr(q)[:400])
