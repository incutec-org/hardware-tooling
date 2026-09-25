#!/usr/bin/env python3
"""Minimal Onshape REST client shared by onshape_release.py and onshape_fit.py.

    python3 hardware/onshape_api.py whoami     # check the API keys, read-only

Standard library only; certifi is used for TLS roots when it is installed.
ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY are read from, first match wins:
the environment, `.env` at this repository's root, then the per-user file
`$INCUTEC_CREDENTIALS_FILE` or `~/.config/incutec/credentials.env`.
Values are never printed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE_URL = "https://cad.onshape.com"
API = "/api/v10"
NAMES = ("ONSHAPE_ACCESS_KEY", "ONSHAPE_SECRET_KEY")
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CREDENTIALS_FILE = Path.home() / ".config" / "incutec" / "credentials.env"
METRE_TO_MM = 1000.0


class OnshapeError(RuntimeError):
    pass


def credentials_file() -> Path:
    override = os.environ.get("INCUTEC_CREDENTIALS_FILE")
    return Path(override).expanduser() if override else DEFAULT_CREDENTIALS_FILE


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip().strip("\"'")
        if value:
            values[key] = value
    return values


def load_keys(environ=None, repo_root: Path = REPO_ROOT, user_file: Path | None = None):
    """Return (access, secret, source per name) without exposing values in errors."""
    environ = os.environ if environ is None else environ
    sources = [
        ("environment", {k: environ[k] for k in NAMES if environ.get(k)}),
        (str(repo_root / ".env"), read_env_file(repo_root / ".env")),
        (str(user_file or credentials_file()), read_env_file(user_file or credentials_file())),
    ]
    found: dict[str, tuple[str, str]] = {}
    for label, values in sources:
        for name in NAMES:
            if name not in found and values.get(name):
                found[name] = (values[name], label)
    missing = [name for name in NAMES if name not in found]
    if missing:
        raise OnshapeError(
            f"missing {', '.join(missing)}: set it in the environment, {repo_root / '.env'} "
            f"or {user_file or credentials_file()}"
        )
    return found[NAMES[0]][0], found[NAMES[1]][0], {n: found[n][1] for n in NAMES}


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Transport:
    """Real HTTP. Tests replace it with a fake exposing the same `send`."""

    def __init__(self, access: str, secret: str, base_url: str = BASE_URL):
        token = base64.b64encode(f"{access}:{secret}".encode()).decode()
        self._auth = f"Basic {token}"
        self.base_url = base_url
        self._ctx = ssl_context()

    def send(self, method: str, url: str, headers: dict, body: bytes | None):
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={**headers, "Authorization": self._auth})
        try:
            with urllib.request.urlopen(request, context=self._ctx, timeout=300) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


class Client:
    def __init__(self, transport, sleep=time.sleep):
        self.transport = transport
        self.sleep = sleep

    @classmethod
    def from_environment(cls):
        access, secret, _ = load_keys()
        return cls(Transport(access, secret))

    def url(self, path: str, query: dict | None = None) -> str:
        base = getattr(self.transport, "base_url", BASE_URL)
        full = path if path.startswith("/api/") else API + path
        return base + full + ("?" + urllib.parse.urlencode(query) if query else "")

    def raw(self, method: str, path: str, query=None, body=None, headers=None, accept="application/json"):
        data = None
        hdrs = {"Accept": accept}
        if headers:
            hdrs.update(headers)
        if body is not None and not isinstance(body, bytes):
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        elif isinstance(body, bytes):
            data = body
        status, payload = self.transport.send(method, self.url(path, query), hdrs, data)
        if status >= 400:
            text = payload[:300].decode("utf-8", "replace") if payload else ""
            raise OnshapeError(f"{method} {path}: HTTP {status} {text}")
        return status, payload

    def json(self, method: str, path: str, query=None, body=None):
        status, payload = self.raw(method, path, query=query, body=body)
        if status == 204 or not payload:
            # `raw` already raised on status >= 400, so any empty body reaching
            # here came from a 2xx (or 3xx) response: the write happened. Some
            # Onshape writes (assembly instance insert, transform) answer this
            # way. Never report a completed 2xx write as "nothing was done" -
            # that invites a caller retry that duplicates the write.
            if method in ("GET", "DELETE"):
                return None
            return {"status": status}
        return json.loads(payload)

    def get(self, path, **query):
        return self.json("GET", path, query=query or None)

    def post(self, path, body, **query):
        return self.json("POST", path, query=query or None, body=body)

    def upload(self, path: str, file_path: Path, fields: dict):
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        parts.append(file_path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        status, payload = self.raw("POST", path, body=b"".join(parts),
                                   headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return json.loads(payload)

    def wait_translation(self, translation_id: str, timeout_s: float = 600, interval_s: float = 3) -> dict:
        waited = 0.0
        while True:
            state = self.get(f"/translations/{translation_id}")
            if state.get("requestState") == "DONE":
                return state
            if state.get("requestState") == "FAILED":
                raise OnshapeError(f"translation {translation_id} failed: {state.get('failureReason')}")
            if waited >= timeout_s:
                raise OnshapeError(f"translation {translation_id} not done after {timeout_s:.0f} s")
            self.sleep(interval_s)
            waited += interval_s

    def download_translation(self, did: str, state: dict) -> bytes:
        ids = state.get("resultExternalDataIds") or []
        if len(ids) != 1:
            raise OnshapeError(f"translation {state.get('id')} returned {len(ids)} files, expected 1")
        _, payload = self.raw("GET", f"/documents/d/{did}/externaldata/{ids[0]}", accept="*/*")
        if not payload:
            raise OnshapeError(f"translation {state.get('id')} produced an empty file")
        return payload


def quote(value: str) -> str:
    """Percent-encode a path segment. Part ids such as `J/D` contain a slash."""
    return urllib.parse.quote(value, safe="")


def load_registry(path: Path) -> dict:
    """Read and validate a one-workspace cad/onshape.json link file."""
    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OnshapeError(f"cannot read {path}: {exc}") from exc
    if registry.get("source") != "onshape":
        raise OnshapeError(f'{path}: "source" must be "onshape"')
    for key in ("document", "workspace"):
        if not isinstance(registry.get(key), dict) or not registry[key].get("id"):
            raise OnshapeError(f"{path}: {key}.id is required")
    for key in ("elements", "parts"):
        if not isinstance(registry.get(key), list):
            raise OnshapeError(f'{path}: "{key}" must be a list')
    known = {e.get("id") for e in registry["elements"]}
    for part in registry["parts"]:
        if part.get("partStudio") not in known:
            raise OnshapeError(f"{path}: part {part.get('name')!r} names an unknown partStudio")
    return registry


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami", help="print the Onshape user the keys belong to and where they were found")
    args = parser.parse_args(argv)
    try:
        access, secret, sources = load_keys()
        client = Client(Transport(access, secret))
        if args.command == "whoami":
            info = client.get("/users/sessioninfo")
            print(f"Onshape user: {info.get('name')}")
            for name, source in sources.items():
                print(f"  {name} from {source}")
        return 0
    except OnshapeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
