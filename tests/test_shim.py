"""Shim behaviour over real HTTP against the fake MemClaw server.

Cast: agent A (writer) and agent C (reader) share fleet ``platform``.
S is A's agent-scoped secret; M is a team-scoped memory derived from S;
D is a clean team-scoped memory with no ancestry.
"""

from __future__ import annotations

import concurrent.futures as cf
import json

import httpx
import pytest
from conftest import TENANT, TENANT_KEY, shim_for

A_KEY, C_KEY = "ak_A_secret", "ak_C_secret"


@pytest.fixture
def world(fake):
    fake.add_agent(A_KEY, "agent-a", "platform")
    fake.add_agent(C_KEY, "agent-c", "platform")
    s = fake.put(tenant_id=TENANT, agent_id="agent-a", fleet_id="platform",
                 visibility="scope_agent", content="secret S")
    m = fake.put(tenant_id=TENANT, agent_id="agent-a", fleet_id="platform",
                 visibility="scope_team", content="summary M", derived_from=[s])
    d = fake.put(tenant_id=TENANT, agent_id="agent-a", fleet_id="platform",
                 visibility="scope_team", content="clean D")
    return {"S": s, "M": m, "D": d}


def get(url: str, mid: str, key: str) -> httpx.Response:
    return httpx.get(f"{url}/api/v1/memories/{mid}", params={"tenant_id": TENANT},
                     headers={"X-API-Key": key}, timeout=10)


def search(url: str, query: str, key: str) -> httpx.Response:
    return httpx.post(f"{url}/api/v1/search", json={"tenant_id": TENANT, "query": query, "top_k": 5},
                      headers={"X-API-Key": key}, timeout=10)


def team(fake, content: str, parents=None, **kw) -> str:
    return fake.put(tenant_id=TENANT, agent_id="agent-a", fleet_id="platform",
                    visibility="scope_team", content=content, derived_from=parents, **kw)


# --------------------------------------------------------------------------- #
# Core rule                                                                    #
# --------------------------------------------------------------------------- #


def test_gate_off_is_transparent(fake, world):
    with shim_for(fake.url, gate_on=False) as (_, url):
        r = get(url, world["M"], C_KEY)
        assert r.status_code == 200 and r.json()["id"] == world["M"]
        assert r.headers["X-Lineage-Gate"] == "off"
        assert r.headers["X-Upstream"] == "fake-memclaw"  # upstream headers pass through
        assert get(url, world["S"], C_KEY).status_code == 404


def test_gate_on_withholds_derived_from_unreadable_source(fake, world):
    with shim_for(fake.url, gate_on=True) as (_, url):
        r = get(url, world["M"], C_KEY)
        assert r.status_code == 404
        assert r.json() == {"detail": "Memory not found"}  # same shape as upstream deny
        assert r.headers["X-Lineage-Gate"] == "on"


def test_gate_on_keeps_every_control(fake, world):
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert get(url, world["D"], C_KEY).status_code == 200      # clean, no ancestry
        assert get(url, world["M"], A_KEY).status_code == 200      # owner can read S
        assert get(url, world["M"], TENANT_KEY).status_code == 200  # tenant key is tenant-wide
        assert get(url, world["S"], C_KEY).status_code == 404      # upstream deny untouched


def test_ancestors_are_checked_with_the_callers_own_key(fake, world):
    with shim_for(fake.url, gate_on=True) as (_, url):
        fake.get_calls.clear()
        get(url, world["M"], C_KEY)
    # One GET for M (forwarded) and one for S (ancestry), both as agent-c.
    assert (world["S"], "agent-c") in fake.get_calls
    assert all(caller == "agent-c" for _, caller in fake.get_calls)


def test_depth_four_chain(fake, world):
    prev = world["S"]
    for i in range(4):
        prev = team(fake, f"hop {i}", [prev])
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert get(url, prev, C_KEY).status_code == 404
        assert get(url, prev, A_KEY).status_code == 200


def test_multi_parent_any_unreadable_parent_withholds(fake, world):
    mixed = team(fake, "fan-in mixed", [world["D"], world["S"]])
    clean = team(fake, "fan-in clean", [world["D"], team(fake, "other clean")])
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert get(url, mixed, C_KEY).status_code == 404
        assert get(url, clean, C_KEY).status_code == 200


def test_string_derived_from_is_accepted(fake, world):
    m = team(fake, "string parent", world["S"])
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert get(url, m, C_KEY).status_code == 404
        assert get(url, m, A_KEY).status_code == 200


# --------------------------------------------------------------------------- #
# Termination and fail-closed                                                  #
# --------------------------------------------------------------------------- #


def test_self_cycle_terminates(fake, world):
    mid = team(fake, "self loop", None)
    fake.store[mid]["metadata"]["derived_from"] = [mid]
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert get(url, mid, C_KEY).status_code == 200


def test_two_cycle_terminates(fake, world):
    a = team(fake, "cycle a", None)
    b = team(fake, "cycle b", [a])
    fake.store[a]["metadata"]["derived_from"] = [b]
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert get(url, a, C_KEY).status_code == 200


def test_chain_deeper_than_max_depth_is_withheld(fake, world):
    prev = team(fake, "root", None)
    for i in range(5):
        prev = team(fake, f"deep {i}", [prev])
    with shim_for(fake.url, gate_on=True, max_depth=3) as (_, url):
        assert get(url, prev, C_KEY).status_code == 404
    with shim_for(fake.url, gate_on=True, max_depth=5) as (_, url):
        assert get(url, prev, C_KEY).status_code == 200


def test_missing_ancestor_is_withheld(fake, world):
    m = team(fake, "orphan", ["00000000-0000-4000-8000-000000000000"])
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert get(url, m, A_KEY).status_code == 404


@pytest.mark.parametrize("bad", [{"x": 1}, [{"nested": 1}], [True], 7.5])
def test_malformed_derived_from_is_withheld(fake, world, bad):
    m = team(fake, f"malformed {bad!r}", bad)
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert get(url, m, A_KEY).status_code == 404


def test_non_dict_metadata_is_withheld(fake, world):
    m = fake.put(tenant_id=TENANT, agent_id="agent-a", fleet_id="platform",
                 visibility="scope_team", content="weird meta", metadata=["not", "a", "dict"])
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert get(url, m, A_KEY).status_code == 404


def test_unsafe_ancestor_id_is_never_requested(fake, world):
    m = team(fake, "path trick", ["../admin/agent-keys/provision"])
    with shim_for(fake.url, gate_on=True) as (_, url):
        fake.get_calls.clear()
        assert get(url, m, A_KEY).status_code == 404
    assert [mid for mid, _ in fake.get_calls] == [m]  # only the forwarded GET


def test_ancestor_server_error_is_503_not_allow(fake, world):
    fake.fail_get_ids.add(world["S"])
    with shim_for(fake.url, gate_on=True) as (srv, url):
        srv.gate.retry_backoff_s = 0.0
        r = get(url, world["M"], A_KEY)
        assert r.status_code == 503
        assert search(url, "summary M", A_KEY).status_code == 503


# --------------------------------------------------------------------------- #
# Search and list                                                              #
# --------------------------------------------------------------------------- #


def test_search_drops_derived_item_for_reader(fake, world):
    with shim_for(fake.url, gate_on=False) as (_, url):
        assert [i["id"] for i in search(url, "summary M", C_KEY).json()["items"]] == [world["M"]]
    with shim_for(fake.url, gate_on=True) as (_, url):
        body = search(url, "summary M", C_KEY).json()
        assert body["items"] == [] and body["count"] == 0
        assert [i["id"] for i in search(url, "summary M", A_KEY).json()["items"]] == [world["M"]]
        assert [i["id"] for i in search(url, "clean D", C_KEY).json()["items"]] == [world["D"]]


def test_search_without_metadata_still_gated(fake, world):
    fake.search_omits_metadata = True
    with shim_for(fake.url, gate_on=True) as (_, url):
        assert search(url, "summary M", C_KEY).json()["items"] == []
        assert len(search(url, "summary M", A_KEY).json()["items"]) == 1
        assert len(search(url, "clean D", C_KEY).json()["items"]) == 1


def test_list_endpoint_is_gated(fake, world):
    with shim_for(fake.url, gate_on=True) as (_, url):
        r = httpx.get(f"{url}/api/v1/memories", params={"tenant_id": TENANT},
                      headers={"X-API-Key": C_KEY}, timeout=10)
        ids = {i["id"] for i in r.json()["items"]}
        assert world["D"] in ids and world["M"] not in ids and world["S"] not in ids
        assert r.json()["total"] == len(ids)


# --------------------------------------------------------------------------- #
# Pass-through and hygiene                                                     #
# --------------------------------------------------------------------------- #


def test_other_endpoints_pass_through(fake, world):
    with shim_for(fake.url, gate_on=True) as (_, url):
        r = httpx.post(f"{url}/api/v1/admin/agent-keys/provision",
                       json={"agent_id": "x", "label": "l", "initial_fleet": "platform", "initial_trust": 1},
                       headers={"X-API-Key": TENANT_KEY}, timeout=10)
        assert r.status_code == 201 and r.json()["raw_key"].startswith("ak_fake_")
        r = httpx.post(f"{url}/api/v1/memories",
                       json={"tenant_id": TENANT, "agent_id": "agent-a", "content": "w"},
                       headers={"X-API-Key": A_KEY, "X-Trace-Id": "trace-123"}, timeout=10)
        assert r.status_code == 201
        assert fake.request_log[-1]["trace"] == "trace-123"
        r = httpx.get(f"{url}/api/v1/memories/stats", params={"tenant_id": TENANT},
                      headers={"X-API-Key": TENANT_KEY}, timeout=10)
        assert r.status_code == 200 and "total" in r.json()
        r = httpx.post(f"{url}/api/v1/admin/agent-keys/provision", json={"agent_id": "y"},
                       headers={"X-API-Key": C_KEY}, timeout=10)
        assert r.status_code == 403  # upstream errors are not rewritten
        r = httpx.delete(f"{url}/api/v1/memories", params={"tenant_id": "other"},
                         headers={"X-API-Key": TENANT_KEY}, timeout=10)
        assert r.status_code == 204


def test_gzip_upstream_is_decoded(fake, world):
    fake.gzip_responses = True
    with shim_for(fake.url, gate_on=True) as (_, url):
        r = get(url, world["D"], C_KEY)
        assert r.status_code == 200 and r.json()["id"] == world["D"]
        assert "content-encoding" not in {k.lower() for k in r.headers}
        assert get(url, world["M"], C_KEY).status_code == 404


def test_audit_log_never_contains_keys(fake, world, tmp_path):
    audit = tmp_path / "audit.jsonl"
    with shim_for(fake.url, gate_on=True, audit_path=str(audit)) as (_, url):
        get(url, world["M"], C_KEY)
        search(url, "summary M", C_KEY)
    text = audit.read_text()
    rows = [json.loads(line) for line in text.splitlines()]
    assert {r["event"] for r in rows} == {"fetch", "list"}
    assert any(r["event"] == "fetch" and r["allowed"] is False and r["denied_ancestor"] == world["S"]
               for r in rows)
    for k in (A_KEY, C_KEY, TENANT_KEY):
        assert k not in text


def test_concurrent_requests(fake, world):
    with shim_for(fake.url, gate_on=True) as (_, url):
        jobs = [(world["M"], C_KEY, 404), (world["M"], A_KEY, 200), (world["D"], C_KEY, 200)] * 30
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            got = list(ex.map(lambda j: get(url, j[0], j[1]).status_code, jobs))
        assert got == [j[2] for j in jobs]


def test_health_endpoint(fake):
    with shim_for(fake.url, gate_on=True) as (_, url):
        r = httpx.get(f"{url}/__lineage_gate/health", timeout=5).json()
        assert r["ok"] is True and r["gate"] == "on" and r["upstream"] == fake.url
