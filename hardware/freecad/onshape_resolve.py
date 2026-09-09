#!/usr/bin/env python3
"""Resolve the queries of every feature in a Part Studio to 3D geometry.

For each feature that carries compressed queries, one FeatureScript
evaluation is run at ``rollbackBarIndex`` equal to the feature index, which is
the model state the feature's selections refer to. The result is a JSON cache
keyed by feature id, then query variable, holding for every selected entity its
type and probe geometry in millimetres:

    vertex: {"kind": "VERTEX", "point": [x, y, z]}
    edge:   {"kind": "EDGE", "mid": [...], "start": [...], "end": [...], "length": l}
    face:   {"kind": "FACE", "centroid": [...], "normal": [...], "area": a, "box": [[min], [max]]}
    body:   {"kind": "BODY", "volume": v, "box": [[min], [max]]}

The Onshape evaluation quota is small (100 calls per day at the time of
writing), so the cache is written after every call and existing entries are
skipped on rerun.

Usage:
    onshape_resolve.py <fsrep.json> <did> <wid> <eid> <out.json> [--limit N] [--only i,j,k] [--planes]
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import onshape_fsrep as fr  # noqa: E402

FS_TEMPLATE = r'''
function(context is Context, queries is map)
{
    var out = {};
    %QUERIES%
    return out;
}
'''

FS_ONE = r"""
    try silent
    {
        var q = qCompressed(1.0, "%PAYLOAD%", newId());
        var ents = evaluateQuery(context, q);
        var arr = [];
        for (var e in ents)
        {
            var rec = {};
            if (!isQueryEmpty(context, qEntityFilter(e, EntityType.VERTEX)))
            {
                rec = { "kind" : "VERTEX", "point" : evVertexPoint(context, {"vertex" : e}) / millimeter };
            }
            else if (!isQueryEmpty(context, qEntityFilter(e, EntityType.EDGE)))
            {
                rec = { "kind" : "EDGE",
                        "mid" : evEdgeTangentLine(context, {"edge" : e, "parameter" : 0.5}).origin / millimeter,
                        "start" : evEdgeTangentLine(context, {"edge" : e, "parameter" : 0.0}).origin / millimeter,
                        "end" : evEdgeTangentLine(context, {"edge" : e, "parameter" : 1.0}).origin / millimeter,
                        "length" : evLength(context, {"entities" : e}) / millimeter };
            }
            else if (!isQueryEmpty(context, qEntityFilter(e, EntityType.FACE)))
            {
                var pl = evFaceTangentPlane(context, {"face" : e, "parameter" : vector(0.5, 0.5)});
                var bx = evBox3d(context, {"topology" : e});
                var sp = undefined;
                try silent { sp = evPlane(context, {"face" : e}); }
                rec = { "kind" : "FACE",
                        "plane" : sp == undefined ? undefined : { "origin" : sp.origin / millimeter, "normal" : sp.normal, "x" : sp.x },
                        "centroid" : evApproximateCentroid(context, {"entities" : e}) / millimeter,
                        "normal" : pl.normal, "origin" : pl.origin / millimeter, "xdir" : pl.x,
                        "area" : evArea(context, {"entities" : e}) / millimeter^2,
                        "bmin" : bx.minCorner / millimeter, "bmax" : bx.maxCorner / millimeter };
            }
            else if (!isQueryEmpty(context, qEntityFilter(e, EntityType.BODY)))
            {
                var bx = evBox3d(context, {"topology" : e});
                rec = { "kind" : "BODY", "volume" : evVolume(context, {"entities" : e}) / millimeter^3,
                        "bmin" : bx.minCorner / millimeter, "bmax" : bx.maxCorner / millimeter };
            }
            arr = append(arr, rec);
        }
        out["%VAR%"] = arr;
    }
"""


def _simplify(v):
    if isinstance(v, dict):
        bt = v.get("btType", "")
        if "Map" in bt:
            return {e["key"]["value"]: _simplify(e["value"]) for e in v["value"]}
        if "Array" in bt:
            return [_simplify(x) for x in v["value"]]
        if "value" in v:
            return v["value"]
    return v


def payloads_by_feature(fsrep: dict) -> dict:
    """Map feature id -> {query var: raw payload} straight from the tree."""
    text = json.dumps(fsrep)
    out: dict = {}
    for f in fr.parse(fsrep):
        d = {}
        for var in f["queries"]:
            m = re.search(re.escape(var) + r'=qCompressed\(1\.0,\\"([%&][^"\\]+)\\"', text)
            if m:
                d[var] = m.group(1)
        out[f["id"]] = d
    return out


FS_PLANES = r"""
function(context is Context, queries is map)
{
    var out = {};
    for (var n in ["Top", "Front", "Right"])
    {
        var p = evPlane(context, {"face" : qCreatedBy(makeId(n), EntityType.FACE)});
        out[n] = { "origin" : p.origin / millimeter, "normal" : p.normal, "x" : p.x };
    }
    return out;
}
"""


async def resolve(fsrep: dict, did: str, wid: str, eid: str, out_path: str, limit: int | None = None,
                  gap: float = 1.5, only: list | None = None, planes: bool = False) -> dict:
    sys.path.insert(0, os.path.expanduser("~/Code/onshape-mcp"))
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser("~/Code/onshape-mcp/.env"))
    from onshape_mcp.api.client import OnshapeClient, OnshapeCredentials

    cache = json.load(open(out_path)) if os.path.exists(out_path) else {}
    feats = fr.parse(fsrep)
    payloads = payloads_by_feature(fsrep)
    c = OnshapeClient(OnshapeCredentials(access_key=os.getenv("ONSHAPE_ACCESS_KEY"), secret_key=os.getenv("ONSHAPE_SECRET_KEY")))
    done = 0
    try:
        if planes and "_default_planes" not in cache:
            r = await c.request_raw("POST", f"/api/v17/partstudios/d/{did}/w/{wid}/e/{eid}/featurescript",
                                    json_body={"script": FS_PLANES, "queries": {}})
            j = r.json() if r.status_code == 200 else {}
            cache["_default_planes"] = _simplify(j.get("result")) or {"error": r.text[:300]}
            json.dump(cache, open(out_path, "w"), indent=1)
            print("default planes:", json.dumps(cache["_default_planes"])[:300], "remaining", r.headers.get("x-rate-limit-remaining"), flush=True)
            await asyncio.sleep(gap)
        for i, f in enumerate(feats):
            pl = payloads.get(f["id"], {})
            if not pl or f["id"] in cache or (only is not None and i not in only):
                continue
            if limit is not None and done >= limit:
                break
            body = "".join(FS_ONE.replace("%PAYLOAD%", p.replace('"', '\\"')).replace("%VAR%", v) for v, p in pl.items())
            script = FS_TEMPLATE.replace("%QUERIES%", body)
            r = await c.request_raw("POST", f"/api/v17/partstudios/d/{did}/w/{wid}/e/{eid}/featurescript",
                                    json_body={"script": script, "queries": {}}, params={"rollbackBarIndex": i})
            rem = r.headers.get("x-rate-limit-remaining")
            if r.status_code != 200:
                print(f"{i:3d} {f['type']:12s} {f['id']}: HTTP {r.status_code} remaining={rem} {r.text[:200]}", flush=True)
                if r.status_code == 429:
                    break
                continue
            j = r.json()
            if any(n.get("type") == "PARSE" or n.get("level") == "ERROR" for n in (j.get("notices") or [])):
                print(f"{i:3d} {f['type']:12s} {f['id']}: script error, stopping: {json.dumps(j.get('notices'))[:400]}", flush=True)
                break
            res = _simplify(j.get("result")) or {}
            cache[f["id"]] = {"index": i, "type": f["type"], "name": f["name"], "queries": res,
                              "missing": [v for v in pl if v not in res], "notices": j.get("notices")}
            json.dump(cache, open(out_path, "w"), indent=1)
            done += 1
            print(f"{i:3d} {f['type']:12s} {f['name']!s:24s} queries={len(pl)} resolved={len(res)} remaining={rem}", flush=True)
            await asyncio.sleep(gap)
    finally:
        await c.close()
    return cache


if __name__ == "__main__":
    args = sys.argv[1:]
    limit = None
    if "--limit" in args:
        k = args.index("--limit")
        limit = int(args[k + 1])
        del args[k:k + 2]
    planes = "--planes" in args
    if planes:
        args.remove("--planes")
    only = None
    if "--only" in args:
        k = args.index("--only")
        only = [int(x) for x in args[k + 1].split(",")]
        del args[k:k + 2]
    fsrep_path, did, wid, eid, out_path = args[:5]
    asyncio.run(resolve(json.load(open(fsrep_path)), did, wid, eid, out_path, limit, only=only, planes=planes))
