from __future__ import annotations

import os
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# Optional: point at an ArgusFleet checkout (the folder named ``argusfleet``)
# when it is not pip-installed, so the end-to-end tests can import it.
_af = os.environ.get("ARGUSFLEET_DIR")
if _af:
    sys.path.insert(0, str(Path(_af).resolve().parent))

from fake_memclaw import FakeMemClaw  # noqa: E402

from lineage_gate_shim import ShimServer, build_server  # noqa: E402

TENANT_KEY = "tenant-key-test"
TENANT = "mc_test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serving(server) -> Iterator[str]:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


@pytest.fixture
def fake() -> Iterator[FakeMemClaw]:
    srv = FakeMemClaw(("127.0.0.1", _free_port()), tenant_key=TENANT_KEY)
    with serving(srv) as url:
        srv.url = url  # type: ignore[attr-defined]
        yield srv


@contextmanager
def shim_for(upstream_url: str, *, gate_on: bool, **kw) -> Iterator[tuple[ShimServer, str]]:
    srv = build_server("127.0.0.1", _free_port(), upstream_url, gate_on=gate_on, **kw)
    with serving(srv) as url:
        yield srv, url
