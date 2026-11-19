#!/usr/bin/env python3
"""A small in-memory, real-HTTP stand-in for a MemClaw server.

It models the server as Caura's Nov 15 staging run observed it: entry-level
scope is enforced, ``derived_from`` is stored verbatim and ignored on reads.

* identity comes from ``X-API-Key``; the tenant key is tenant-wide and may
  provision agent keys; an agent key reads ``scope_agent`` rows it wrote and
  ``scope_team`` rows in its fleet;
* an entry-level deny on ``GET /memories/{id}`` is a 404, as observed on staging;
* ``/search`` matches on exact content (the harness searches by the target's
  prose) and filters by entry-level scope.

Used by the test suite and by the README's offline dry run:

    python tests/fake_memclaw.py --port 8790 --tenant-key dryrun-tenant-key
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

_GET_RE = re.compile(r"^/api/v1/memories/([^/]+)$")


class FakeMemClaw(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr: tuple[str, int],
        *,
        tenant_key: str,
        gzip_responses: bool = False,
        search_omits_metadata: bool = False,
    ) -> None:
        super().__init__(addr, _Handler)
        self.tenant_key = tenant_key
        self.gzip_responses = gzip_responses
        self.search_omits_metadata = search_omits_metadata
        self.keys: dict[str, dict[str, Any]] = {}
        self.store: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        # Test hooks
        self.fail_get_ids: set[str] = set()  # GET on these ids returns 500
        self.get_calls: list[tuple[str, str]] = []  # (memory_id, caller tag)
        self.request_log: list[dict[str, Any]] = []

    # identity / policy -------------------------------------------------------

    def identity(self, key: str | None) -> dict[str, Any] | None:
        if key and key == self.tenant_key:
            return {"kind": "tenant"}
        return self.keys.get(key or "")

    @staticmethod
    def entry_ok(who: dict[str, Any] | None, rec: dict[str, Any]) -> bool:
        if who is None:
            return False
        if who["kind"] == "tenant":
            return True
        vis = rec.get("visibility")
        if vis == "scope_agent":
            return who["agent_id"] == rec["agent_id"]
        if vis == "scope_team":
            return who["fleet_id"] == rec["fleet_id"]
        return True

    # direct seeding for unit tests -----------------------------------------

    def add_agent(self, key: str, agent_id: str, fleet_id: str) -> None:
        self.keys[key] = {"kind": "agent", "agent_id": agent_id, "fleet_id": fleet_id, "trust": 1}

    def put(self, *, tenant_id: str, agent_id: str, fleet_id: str, visibility: str,
            content: str, derived_from: Any = None, mem_id: str | None = None,
            metadata: Any = None) -> str:
        mem_id = mem_id or str(uuid.uuid4())
        meta: Any = dict(metadata or {}) if metadata is None or isinstance(metadata, dict) else metadata
        if derived_from is not None and isinstance(meta, dict):
            meta["derived_from"] = derived_from
        self.store[mem_id] = {"id": mem_id, "tenant_id": tenant_id, "agent_id": agent_id,
                              "fleet_id": fleet_id, "visibility": visibility,
                              "content": content, "metadata": meta}
        return mem_id


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: FakeMemClaw  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _json(self, status: int, payload: Any) -> None:
        body = b"" if status == 204 else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Upstream", "fake-memclaw")
        if body and self.server.gzip_responses:
            body = gzip.compress(body)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw else {}

    def _who(self) -> dict[str, Any] | None:
        return self.server.identity(self.headers.get("X-API-Key"))

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self._body()
        self.server.request_log.append({"method": "POST", "path": path,
                                        "trace": self.headers.get("X-Trace-Id")})
        if path == "/api/v1/admin/agent-keys/provision":
            if self.headers.get("X-API-Key") != self.server.tenant_key:
                return self._json(403, {"detail": "admin capability required"})
            raw = f"ak_fake_{uuid.uuid4().hex}"
            with self.server.lock:
                self.server.keys[raw] = {"kind": "agent", "agent_id": body["agent_id"],
                                         "fleet_id": body["initial_fleet"],
                                         "trust": body["initial_trust"]}
            return self._json(201, {"raw_key": raw, "agent_id": body["agent_id"]})
        if path == "/api/v1/memories":
            who = self._who()
            if who is None:
                return self._json(401, {"detail": "bad key"})
            new_id = str(uuid.uuid4())
            rec = {"id": new_id, "tenant_id": body["tenant_id"],
                   "agent_id": who.get("agent_id") or body["agent_id"],
                   "fleet_id": who.get("fleet_id") or body.get("fleet_id"),
                   "visibility": body.get("visibility", "scope_team"),
                   "content": body["content"], "metadata": dict(body.get("metadata") or {})}
            with self.server.lock:
                self.server.store[new_id] = rec
            return self._json(201, rec)
        if path == "/api/v1/search":
            who = self._who()
            q = body.get("query") or ""
            with self.server.lock:
                rows = list(self.server.store.values())
            items = [r for r in rows if r["tenant_id"] == body.get("tenant_id")
                     and r["content"] == q and self.server.entry_ok(who, r)]
            items = items[: int(body.get("top_k", 10))]
            if self.server.search_omits_metadata:
                items = [{"id": r["id"], "content": r["content"]} for r in items]
            return self._json(200, {"items": items, "count": len(items)})
        return self._json(404, {"detail": "no route"})

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        params = parse_qs(split.query)
        tenant = (params.get("tenant_id") or [None])[0]
        who = self._who()
        if split.path == "/api/v1/memories/stats":
            with self.server.lock:
                total = sum(1 for r in self.server.store.values() if r["tenant_id"] == tenant)
            return self._json(200, {"total": total})
        if split.path == "/api/v1/memories":
            with self.server.lock:
                rows = [r for r in self.server.store.values() if r["tenant_id"] == tenant]
            items = [r for r in rows if self.server.entry_ok(who, r)]
            return self._json(200, {"items": items, "total": len(items)})
        m = _GET_RE.match(split.path)
        if m:
            mid = m.group(1)
            caller = (who or {}).get("agent_id") or (who or {}).get("kind") or "anon"
            self.server.get_calls.append((mid, caller))
            if mid in self.server.fail_get_ids:
                return self._json(500, {"detail": "injected failure"})
            rec = self.server.store.get(mid)
            if rec is None or rec["tenant_id"] != tenant:
                return self._json(404, {"detail": "Memory not found"})
            if not self.server.entry_ok(who, rec):
                return self._json(404, {"detail": "Memory not found"})
            return self._json(200, rec)
        return self._json(404, {"detail": "no route"})

    def do_DELETE(self) -> None:
        split = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        tenant = (parse_qs(split.query).get("tenant_id") or [None])[0]
        if split.path == "/api/v1/memories":
            with self.server.lock:
                for k in [k for k, r in self.server.store.items() if r["tenant_id"] == tenant]:
                    del self.server.store[k]
            return self._json(204, None)
        return self._json(404, {"detail": "no route"})


def main() -> None:
    p = argparse.ArgumentParser(description="In-memory fake MemClaw server (no derivation gate).")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--tenant-key", default="dryrun-tenant-key")
    a = p.parse_args()
    srv = FakeMemClaw((a.host, a.port), tenant_key=a.tenant_key)
    print(f"fake MemClaw on http://{a.host}:{a.port} (tenant key: {a.tenant_key})", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    main()
