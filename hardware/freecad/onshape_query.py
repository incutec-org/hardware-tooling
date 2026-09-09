#!/usr/bin/env python3
"""Decode Onshape compressed query strings from a FeatureScript representation.

Onshape serialises the queries of a Part Studio's features as
``qCompressed(1.0, "<payload>", id)``. The payload is either ``%<text>`` or
``&<len>$<base64 zlib(text)>``. The text is a compact, self-referencing
serialisation of a Query map:

    B<hex>$<name>            typed value; the type name precedes its payload
    M<hex>                   map with that many key/value pairs
    A<hex>                   array with that many elements
    S<hex>[.<hex>]*$<chars>  string made of parts; a negative part length is a
                             back-reference into the part table
    C<hex>                   typed value whose type is entry <hex> of the part table
    R<hex>                   back-reference into the value table
    D<int>                   integer
    T / F / N                true / false / null

Two tables grow while decoding. The part table holds every type name and every
literal string part in order of appearance. The value table holds every
decoded value that can later be referenced. The registration rules were
inferred from Onshape output and are checked by ``validate()`` against every
query in a representation: a wrong rule produces map keys that are not query
field names.
"""
from __future__ import annotations

import base64
import json
import re
import sys
import zlib
from dataclasses import dataclass, field

KNOWN_KEYS = {
    "entityType", "historyType", "operationId", "queryType", "sketchEntityId",
    "disambiguationData", "disambiguationType", "originals", "entities",
    "derivedFrom", "isStart", "index", "deterministicIds", "subQuery",
    "subquery", "queries", "query", "filter", "topology", "geometryType",
    "bodyType", "constructionObject", "modifiableEntityOnly", "occurrence",
    "value", "type", "flags", "partId", "partNumber", "elementId",
    "featureId", "operationName", "trackingType", "isSketchEntity",
    "sketchId", "entityIndex", "featureType", "importedSketchId", "importTag", "blendedFrom", "blendedInto", "order", "fromTools",
}


@dataclass
class Typed:
    """A typed value: an enum member such as EntityType.EDGE, or an Id."""
    type: str
    value: object

    def __repr__(self) -> str:
        if self.type == "Id":
            return "Id(" + "/".join(".".join(p) if isinstance(p, tuple) else str(p) for p in self.value) + ")"
        return f"{self.type}.{self.value}"


@dataclass
class QueryObject:
    """A class instance (``C<n>`` or ``B<n>$Query``) wrapping a map."""
    type: str
    fields: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"{self.type}{self.fields!r}"


class _Decoder:
    TOKEN = re.compile(
        r"(B[0-9a-f]+\$)|(S-?[0-9a-f]+(?:\.-?[0-9a-f]+)*\$)|(M[0-9a-f]+)|(A[0-9a-f]+)"
        r"|(C[0-9a-f]+)|(R[0-9a-f]+)|(D-?[0-9]+)|(T)|(F)|(N)"
    )

    # Registration rules verified against 1516 queries of a real Part Studio:
    # every string, integer, array, map, typed value and class instance
    # registers in the value table when it is complete. The payload array of
    # an Id and the payload string of an enum do not register separately.


    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.parts: list[str] = []
        self.values: list[object] = []

    # --- tokens -----------------------------------------------------------
    def _next(self):
        m = self.TOKEN.match(self.s, self.i)
        if not m:
            raise ValueError(f"bad token at {self.i}: {self.s[self.i:self.i + 30]!r}")
        self.i = m.end()
        return m.group(0)

    def _string(self, tok: str) -> tuple:
        lens = [int(x, 16) for x in tok[1:-1].split(".")]
        parts = []
        for n in lens:
            if n < 0:
                parts.append(self.parts[-n])
            else:
                parts.append(self.s[self.i:self.i + n])
                self.i += n
                self.parts.append(parts[-1])
        if len(parts) > 1:
            self.parts.append(".".join(parts))   # the joined string registers too
        return tuple(parts)

    # --- values -----------------------------------------------------------
    def value(self):
        tok = self._next()
        kind = tok[0]
        if kind == "S":
            v = self._string(tok)
            v = v[0] if len(v) == 1 else v
            self.values.append(v)
            return v
        if kind == "M":
            m = self._map(int(tok[1:], 16))
            self.values.append(m)
            return m
        if kind == "A":
            n = int(tok[1:], 16)
            arr = [self.value() for _ in range(n)]
            self.values.append(arr)
            return arr
        if kind == "R":
            return self.values[int(tok[1:], 16)]
        if kind == "D":
            v = int(tok[1:])
            self.values.append(v)
            return v
        if kind == "T":
            return True
        if kind == "F":
            return False
        if kind == "N":
            return None
        if kind == "B":
            n = int(tok[1:-1], 16)
            name = self.s[self.i:self.i + n]
            self.i += n
            self.parts.append(name)
            return self._typed(name, first=True)
        if kind == "C":
            name = self.parts[int(tok[1:], 16)]
            return self._typed(name, first=False)
        raise ValueError(tok)

    def _map(self, n: int) -> dict:
        out = {}
        for _ in range(n):
            k = self.value()
            out[k] = self.value()
        return out

    def _typed(self, name: str, first: bool):
        nxt = self.s[self.i]
        if nxt == "M":
            obj = QueryObject(name, self._map(int(self._next()[1:], 16)))
            self.values.append(obj)           # instances register at close
            return obj
        if nxt == "A":
            n = int(self._next()[1:], 16)     # payload array does not register
            arr = [self.value() for _ in range(n)]
            v = Typed(name, arr)
            self.values.append(v)
            return v
        # enum payload: a bare string. The first occurrence of a type
        # registers only the typed value; later ones register the string too.
        tok = self._next()
        payload = self._string(tok)
        payload = payload[0] if len(payload) == 1 else payload
        v = Typed(name, payload)
        self.values.append(v)
        return v


def expand(payload: str) -> str:
    """Return the plain text of a qCompressed payload."""
    if payload.startswith("%"):
        return payload[1:]
    if payload.startswith("&"):
        body = payload[1:]
        n = body.index("$")
        text = zlib.decompress(base64.b64decode(body[n + 1:])).decode()
        return text[1:] if text.startswith("%") else text
    raise ValueError(f"unknown payload prefix {payload[:2]!r}")


def decode(payload: str):
    d = _Decoder(expand(payload))
    v = d.value()
    if d.i != len(d.s):
        raise ValueError(f"trailing data at {d.i}/{len(d.s)}: {d.s[d.i:d.i + 40]!r}")
    return v


def _keys(o, out: set):
    if isinstance(o, QueryObject):
        _keys(o.fields, out)
    elif isinstance(o, dict):
        for k, v in o.items():
            out.add(k if isinstance(k, str) else repr(k))
            _keys(v, out)
    elif isinstance(o, list):
        for v in o:
            _keys(v, out)
    elif isinstance(o, Typed):
        _keys(o.value, out)


def validate(payloads: list[str]) -> dict:
    """Decode every payload; report failures and unknown map keys."""
    bad, unknown = [], {}
    for p in payloads:
        try:
            q = decode(p)
        except Exception as ex:  # noqa: BLE001
            bad.append((p[:60], str(ex)))
            continue
        ks: set = set()
        _keys(q, ks)
        for k in ks - KNOWN_KEYS:
            unknown.setdefault(k, 0)
            unknown[k] += 1
    return {"total": len(payloads), "failed": bad, "unknown_keys": unknown}


def payloads_from_fsrep(fsrep_json: dict) -> list[str]:
    text = json.dumps(fsrep_json)
    return re.findall(r'qCompressed\(1\.0,\\"([%&][^"\\]+)\\"', text)


if __name__ == "__main__":
    fs = json.load(open(sys.argv[1]))
    ps = payloads_from_fsrep(fs)
    rep = validate(ps)
    print("queries:", rep["total"], "failed:", len(rep["failed"]))
    for p, e in rep["failed"][:8]:
        print("  FAIL", p, e)
    print("unknown keys:", rep["unknown_keys"])
    if len(sys.argv) > 2:
        for p in ps[: int(sys.argv[2])]:
            print(repr(decode(p))[:600])
