#!/usr/bin/env python3
"""Lineage-gate shim: a reverse proxy that enforces derivation-closed read
scope in front of a MemClaw-compatible memory server.

Rule (Sangaraju & Vissa, "Lineage-Aware Memory Governance", IEEE Access,
2026, DOI 10.1109/ACCESS.2026.3730363): a reader may receive memory M only
if the reader may also read every memory in M's transitive
``metadata.derived_from`` ancestry.

How the rule is enforced here
-----------------------------
The shim never decides entitlements itself. For every memory it is about to
return, it walks ``metadata.derived_from`` and re-fetches each ancestor from
the upstream server *with the caller's own credentials*. The upstream server
stays the sole authority on "may this caller read this row". If any ancestor
is denied (401/403/404), the memory is withheld.

Gated read paths (everything else is forwarded unchanged):

* ``GET  /api/v1/memories/{id}`` - withheld -> 404 ``{"detail": "Memory not found"}``
* ``POST /api/v1/search``        - withheld items are dropped from ``items``
* ``GET  /api/v1/memories``      - withheld items are dropped from ``items``

Fail-closed behaviour:

* ancestor denied, missing, malformed id, or chain deeper than ``--max-depth``
  -> the memory is withheld;
* ancestor lookup fails for a transient reason (5xx / 429 / network) after
  retries -> ``503`` for a single fetch, so the caller sees an error, never a
  silent allow. For list/search responses the whole response becomes ``503``.

``--gate off`` turns the shim into a transparent pass-through so the same
setup can produce the gate-off baseline and the gate-on result.

This is a measurement shim, not a production control: ``derived_from`` is
client-written metadata, so a writer that omits it is not gated. A
production gate would record lineage server-side.

Dependencies: Python 3.10+ and ``httpx``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

__version__ = "0.1.0"

log = logging.getLogger("lineage_gate_shim")

API_PREFIX = "/api/v1"
# Path segments under /memories/ that are endpoints, not memory ids.
_NON_ID_SEGMENTS = frozenset({"stats", "redistribute", "search", "bulk", "batch"})
# Ancestor ids are interpolated into an upstream URL path, so only accept a
# conservative id alphabet (UUIDs and typical opaque ids). Anything else is
# treated as malformed lineage and fails closed.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MEMORY_PATH_RE = re.compile(r"^/api/v1/memories/([^/]+)$")

# RFC 7230 hop-by-hop headers, plus headers the proxy must recompute.
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
    }
)
_RESP_DROP_HEADERS = _HOP_HEADERS | {"content-encoding"}

DENY_STATUSES = frozenset({401, 403, 404})


class GateUnavailable(Exception):
    """An ancestor's readability could not be established (transient error)."""


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    ancestors_checked: int = 0
    denied_ancestor: str | None = None


@dataclass
class _RequestContext:
    """Per-request state: caller credentials and an ancestor-decision memo.

    The memo lives only for one inbound request. Nothing is cached across
    requests, so a permission change upstream takes effect immediately.
    """

    headers: dict[str, str]
    tenant_id: str | None
    memo: dict[str, tuple[bool, list[str]]] = field(default_factory=dict)


def _caller_tag(headers: dict[str, str]) -> str:
    """Short, non-reversible tag for log correlation. Never logs the key."""
    key = next((v for k, v in headers.items() if k.lower() == "x-api-key"), "")
    if not key:
        return "anon"
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _parents_of(payload: Any) -> list[str] | None:
    """Return the ``derived_from`` ids of a memory payload.

    ``[]`` means "no ancestry". ``None`` means the field is present but
    malformed, which the caller must treat as fail-closed.
    """
    if not isinstance(payload, dict):
        return []
    meta = payload.get("metadata")
    if meta is None:
        return []
    if not isinstance(meta, dict):
        return None
    raw = meta.get("derived_from")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for item in raw:
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            return None
        out.append(str(item))
    return out


class LineageGate:
    """Ancestry check against an upstream MemClaw server."""

    def __init__(
        self,
        upstream: httpx.Client,
        *,
        max_depth: int = 16,
        max_ancestors: int = 256,
        retries: int = 3,
        retry_backoff_s: float = 0.25,
    ) -> None:
        self.upstream = upstream
        self.max_depth = max_depth
        self.max_ancestors = max_ancestors
        self.retries = max(1, retries)
        self.retry_backoff_s = retry_backoff_s

    # -- upstream fetch with the caller's credentials ----------------------

    def _fetch_as_caller(self, ctx: _RequestContext, memory_id: str) -> httpx.Response:
        params = {"tenant_id": ctx.tenant_id} if ctx.tenant_id else None
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self.upstream.get(
                    f"{API_PREFIX}/memories/{memory_id}", params=params, headers=ctx.headers
                )
            except httpx.HTTPError as e:
                last_exc = e
            else:
                if resp.status_code != 429 and resp.status_code < 500:
                    return resp
                last_exc = GateUnavailable(f"upstream {resp.status_code}")
            if attempt + 1 < self.retries:
                time.sleep(self.retry_backoff_s * (2**attempt))
        raise GateUnavailable(f"ancestor {memory_id}: {last_exc}")

    def _ancestor(self, ctx: _RequestContext, memory_id: str) -> tuple[bool, list[str]]:
        """(caller may read it, its parents). Memoised per request."""
        if memory_id in ctx.memo:
            return ctx.memo[memory_id]
        if not _SAFE_ID_RE.match(memory_id):
            result: tuple[bool, list[str]] = (False, [])
        else:
            resp = self._fetch_as_caller(ctx, memory_id)
            if resp.status_code in DENY_STATUSES:
                result = (False, [])
            elif 200 <= resp.status_code < 300:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = None
                parents = _parents_of(payload)
                # Malformed lineage on an ancestor cannot be followed: deny.
                result = (False, []) if parents is None else (True, parents)
            else:
                # Any other 4xx (e.g. 400/409/422) means readability was not
                # established. Fail closed rather than guess.
                result = (False, [])
        ctx.memo[memory_id] = result
        return result

    # -- the check ----------------------------------------------------------

    def check(self, ctx: _RequestContext, payload: Any) -> GateDecision:
        """Decide whether ``payload`` (a memory the caller already passed
        upstream's entry-level check for) may be returned to the caller.

        Breadth-first over the full ancestry (multi-parent safe). A visited
        set stops cycles; ``max_depth`` and ``max_ancestors`` bound the walk,
        and exceeding either withholds the memory.
        """
        roots = _parents_of(payload)
        if roots is None:
            return GateDecision(False, "malformed_derived_from")
        self_id = str(payload.get("id")) if isinstance(payload, dict) and payload.get("id") else None
        visited: set[str] = {self_id} if self_id else set()
        frontier = [p for p in roots]
        depth = 0
        checked = 0
        while frontier:
            depth += 1
            if depth > self.max_depth:
                return GateDecision(False, "exceeded_max_depth", checked)
            nxt: list[str] = []
            for aid in frontier:
                if aid in visited:
                    continue
                visited.add(aid)
                checked += 1
                if checked > self.max_ancestors:
                    return GateDecision(False, "exceeded_max_ancestors", checked)
                ok, parents = self._ancestor(ctx, aid)
                if not ok:
                    return GateDecision(False, "ancestor_denied", checked, denied_ancestor=aid)
                nxt.extend(parents)
            frontier = nxt
        return GateDecision(True, "no_ancestry" if checked == 0 else "ancestry_readable", checked)


class ShimServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr: tuple[str, int],
        *,
        upstream_url: str,
        gate_on: bool,
        timeout_s: float = 60.0,
        max_depth: int = 16,
        audit_path: str | None = None,
    ) -> None:
        super().__init__(addr, _Handler)
        self.upstream_url = upstream_url.rstrip("/")
        self.gate_on = gate_on
        self.client = httpx.Client(
            base_url=self.upstream_url,
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        self.gate = LineageGate(self.client, max_depth=max_depth)
        self._audit_lock = threading.Lock()
        self._audit = open(audit_path, "a", encoding="utf-8") if audit_path else None  # noqa: SIM115
        self.stats = {"forwarded": 0, "gated_fetch_denied": 0, "gated_items_dropped": 0, "gate_unavailable": 0}
        self._stats_lock = threading.Lock()

    def bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def audit(self, record: dict[str, Any]) -> None:
        record = {"ts": time.time(), **record}
        log.info("gate %s", json.dumps(record, sort_keys=True))
        if self._audit is not None:
            with self._audit_lock:
                self._audit.write(json.dumps(record, sort_keys=True) + "\n")
                self._audit.flush()

    def server_close(self) -> None:
        super().server_close()
        self.client.close()
        if self._audit is not None:
            self._audit.close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ShimServer  # type: ignore[assignment]

    # Quiet default access log; decisions are logged via ShimServer.audit.
    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    # -- plumbing -----------------------------------------------------------

    def _read_body(self) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                size = int(line.split(b";", 1)[0], 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _send(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.send_response(status)
        for k, v in headers:
            if k.lower() not in _RESP_DROP_HEADERS:
                self.send_header(k, v)
        self.send_header("X-Lineage-Gate", "on" if self.server.gate_on else "off")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any, extra: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(payload).encode()
        self._send(status, [("Content-Type", "application/json"), *(extra or [])], body)

    # -- main ---------------------------------------------------------------

    def _handle(self) -> None:
        body = self._read_body()
        fwd_headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_HEADERS}
        split = urlsplit(self.path)
        path = split.path

        if path == "/__lineage_gate/health":
            self._send_json(200, {"ok": True, "gate": "on" if self.server.gate_on else "off",
                                  "upstream": self.server.upstream_url, "version": __version__,
                                  "stats": dict(self.server.stats)})
            return

        try:
            upstream = self.server.client.request(
                self.command, self.path, content=body or None, headers=fwd_headers
            )
        except httpx.HTTPError as e:
            self.server.bump("gate_unavailable")
            self._send_json(502, {"detail": f"lineage gate shim: upstream unreachable ({type(e).__name__})"})
            return
        self.server.bump("forwarded")
        resp_headers = list(upstream.headers.multi_items())

        if not self.server.gate_on or not (200 <= upstream.status_code < 300):
            self._send(upstream.status_code, resp_headers, upstream.content)
            return

        query = parse_qs(split.query)
        try:
            if self.command == "GET" and (m := _MEMORY_PATH_RE.match(path)) and m.group(1) not in _NON_ID_SEGMENTS:
                tenant = (query.get("tenant_id") or [None])[0]
                self._gate_single(upstream, resp_headers, fwd_headers, tenant, m.group(1))
                return
            if (self.command == "POST" and path == f"{API_PREFIX}/search") or (
                self.command == "GET" and path == f"{API_PREFIX}/memories"
            ):
                tenant = (query.get("tenant_id") or [None])[0]
                if tenant is None and body:
                    try:
                        parsed = json.loads(body)
                        if isinstance(parsed, dict) and parsed.get("tenant_id") is not None:
                            tenant = str(parsed["tenant_id"])
                    except ValueError:
                        pass
                self._gate_list(upstream, resp_headers, fwd_headers, tenant, path)
                return
        except GateUnavailable as e:
            self.server.bump("gate_unavailable")
            self.server.audit({"event": "gate_unavailable", "path": path,
                               "caller": _caller_tag(fwd_headers), "error": str(e)})
            self._send_json(503, {"detail": "lineage gate: could not verify ancestry"},
                            [("Retry-After", "1")])
            return

        self._send(upstream.status_code, resp_headers, upstream.content)

    def _gate_single(self, upstream: httpx.Response, resp_headers: list[tuple[str, str]],
                     fwd_headers: dict[str, str], tenant: str | None, memory_id: str) -> None:
        try:
            payload = upstream.json()
        except ValueError:
            # Not a JSON memory row: nothing to gate.
            self._send(upstream.status_code, resp_headers, upstream.content)
            return
        ctx = _RequestContext(headers=fwd_headers, tenant_id=tenant)
        decision = self.server.gate.check(ctx, payload)
        self.server.audit({"event": "fetch", "memory_id": memory_id, "caller": _caller_tag(fwd_headers),
                           "allowed": decision.allowed, "reason": decision.reason,
                           "ancestors_checked": decision.ancestors_checked,
                           "denied_ancestor": decision.denied_ancestor})
        if decision.allowed:
            self._send(upstream.status_code, resp_headers, upstream.content)
            return
        self.server.bump("gated_fetch_denied")
        # Same shape the upstream uses for an entry-level deny, so callers
        # cannot tell a lineage deny from "no such memory".
        self._send_json(404, {"detail": "Memory not found"})

    def _gate_list(self, upstream: httpx.Response, resp_headers: list[tuple[str, str]],
                   fwd_headers: dict[str, str], tenant: str | None, path: str) -> None:
        try:
            payload = upstream.json()
        except ValueError:
            self._send(upstream.status_code, resp_headers, upstream.content)
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            self._send(upstream.status_code, resp_headers, upstream.content)
            return
        ctx = _RequestContext(headers=fwd_headers, tenant_id=tenant)
        items = payload["items"]
        kept: list[Any] = []
        dropped: list[str] = []
        for item in items:
            row = item
            if isinstance(item, dict) and "metadata" not in item and item.get("id") is not None:
                # Listing omitted metadata: fetch the row as the caller to
                # read its lineage (and to confirm the caller may see it).
                ok, parents = self.server.gate._ancestor(ctx, str(item["id"]))
                if not ok:
                    dropped.append(str(item.get("id")))
                    continue
                row = {"id": item["id"], "metadata": {"derived_from": parents}}
            decision = self.server.gate.check(ctx, row)
            if decision.allowed:
                kept.append(item)
            else:
                dropped.append(str(item.get("id")) if isinstance(item, dict) else "?")
        if dropped:
            self.server.bump("gated_items_dropped", len(dropped))
        self.server.audit({"event": "list", "path": path, "caller": _caller_tag(fwd_headers),
                           "returned": len(items), "kept": len(kept), "dropped_ids": dropped})
        out = dict(payload)
        out["items"] = kept
        # Keep simple count fields consistent with the filtered page.
        for key in ("count", "total", "returned"):
            if isinstance(out.get(key), int) and out[key] == len(items):
                out[key] = len(kept)
        self._send_json(upstream.status_code, out,
                        [(k, v) for k, v in resp_headers if k.lower() != "content-type"])


def build_server(host: str, port: int, upstream: str, *, gate_on: bool, max_depth: int = 16,
                 timeout_s: float = 60.0, audit_path: str | None = None) -> ShimServer:
    return ShimServer((host, port), upstream_url=upstream, gate_on=gate_on, timeout_s=timeout_s,
                      max_depth=max_depth, audit_path=audit_path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Lineage-gate shim for a MemClaw-compatible server.")
    p.add_argument("--upstream", required=True, help="Upstream server, e.g. https://caura.dev")
    p.add_argument("--gate", choices=("on", "off"), default="on", help="Enforce the gate (default: on)")
    p.add_argument("--host", default="127.0.0.1", help="Listen address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8787, help="Listen port (default: 8787)")
    p.add_argument("--max-depth", type=int, default=16, help="Max derived_from hops before withholding")
    p.add_argument("--timeout", type=float, default=60.0, help="Upstream timeout in seconds")
    p.add_argument("--audit-log", default=None, help="Append one JSON line per gate decision here")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    srv = build_server(args.host, args.port, args.upstream, gate_on=args.gate == "on",
                       max_depth=args.max_depth, timeout_s=args.timeout, audit_path=args.audit_log)
    log.info("lineage-gate shim %s listening on http://%s:%d -> %s (gate %s)",
             __version__, args.host, args.port, srv.upstream_url, args.gate.upper())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
