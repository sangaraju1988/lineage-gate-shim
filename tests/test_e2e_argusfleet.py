"""End to end: ArgusFleet's derived-leakage experiment (PR #3), unmodified,
run over real HTTP through the shim against the fake MemClaw server.

The fake models today's server (entry-level scope only), so:

* gate OFF must reproduce Caura's Nov 15 staging result: 20/20 derived leaks
  on fetch and on search, run valid, controls as expected;
* gate ON must give 0/20 derived leaks with every control unchanged.

Requires ArgusFleet to be importable (``pip install -e <argusfleet>`` or set
``ARGUSFLEET_DIR=/path/to/argusfleet``); skipped otherwise.
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import pytest
from conftest import TENANT, TENANT_KEY, shim_for

derived_leakage = pytest.importorskip("argusfleet.experiments.derived_leakage")
HarnessConfig = pytest.importorskip("argusfleet.harness.config").HarnessConfig


def _cfg(base_url: str, out: Path, *, n_chains: int = 20, depth: int = 1, seed: int = 42) -> HarnessConfig:
    # Mirrors configs/derived_leakage.yaml except settle_delay_ms (the fake
    # has no async indexing) and the output directory.
    return HarnessConfig(
        base_url=base_url,
        api_key=TENANT_KEY,
        output_dir=out,
        seed=seed,
        request_timeout_s=30.0,
        max_retries=3,
        min_request_interval_ms=0.0,
        tenant_ids=[TENANT],
        experiment={"n_chains": n_chains, "chain_depth": depth, "home_fleet": "platform",
                    "settle_delay_ms": 0, "max_in_flight": 10},
    )


def _run(base_url: str, out: Path, **kw) -> dict:
    return asyncio.run(asyncio.wait_for(derived_leakage.run(_cfg(base_url, out, **kw)), timeout=120))


def _summary(result: dict) -> dict[str, str]:
    with open(result["summary_csv"], newline="") as f:
        return {row["metric"]: row["value"] for row in csv.DictReader(f)}


def _controls(result: dict) -> dict[str, tuple[int, int]]:
    """kind -> (GET allowed, search found) over all probes of that kind."""
    out: dict[str, list[int]] = {}
    with open(result["probes_csv"], newline="") as f:
        for row in csv.DictReader(f):
            k = out.setdefault(row["kind"], [0, 0])
            k[0] += row["access_decision"] == "allow"
            k[1] += row["search_decision"] == "allow"
    return {k: (v[0], v[1]) for k, v in out.items()}


def test_seed42_default_config_gate_off_reproduces_staging(fake, tmp_path):
    with shim_for(fake.url, gate_on=False) as (_, url):
        result = _run(url, tmp_path)
    s = _summary(result)
    assert s["run_valid"] == "True"
    assert s["n_derived_graded"] == "20"
    assert s["n_derived_leaks"] == "20" and s["derived_leak_rate"] == "1.0"
    assert s["n_search_derived_leaks"] == "20"
    assert s["n_source_control_allowed"] == "0"
    assert s["n_writes"] == "60" and s["n_writes_failed"] == "0"
    assert s["n_chain_broken"] == "0" and s["n_misses"] == "0"
    assert _controls(result) == {
        "derived": (20, 20),
        "control_clean": (20, 20),
        "control_owner": (20, 20),
        "control_source": (0, 0),
    }


def test_seed42_default_config_gate_on_closes_the_leak(fake, tmp_path):
    with shim_for(fake.url, gate_on=True) as (srv, url):
        result = _run(url, tmp_path)
    s = _summary(result)
    assert s["run_valid"] == "True"
    assert s["n_derived_graded"] == "20"
    assert s["n_derived_leaks"] == "0" and s["derived_leak_rate"] == "0.0"
    assert s["n_search_derived_leaks"] == "0" and s["search_derived_leak_rate"] == "0.0"
    assert s["n_derived_errors"] == "0"
    assert s["n_source_control_allowed"] == "0"
    assert s["n_writes"] == "60" and s["n_writes_failed"] == "0"
    # Controls unchanged: the gate removed only the derived reads.
    assert s["n_chain_broken"] == "0"
    assert s["n_misses"] == "0" and s["n_search_misses"] == "0"
    assert _controls(result) == {
        "derived": (0, 0),
        "control_clean": (20, 20),
        "control_owner": (20, 20),
        "control_source": (0, 0),
    }
    assert srv.stats["gated_fetch_denied"] == 20
    assert srv.stats["gate_unavailable"] == 0


@pytest.mark.parametrize("depth", [2, 4])
def test_deeper_chains(fake, tmp_path, depth):
    with shim_for(fake.url, gate_on=False) as (_, url):
        off = _summary(_run(url, tmp_path / "off", n_chains=6, depth=depth))
    with shim_for(fake.url, gate_on=True) as (_, url):
        on = _summary(_run(url, tmp_path / "on", n_chains=6, depth=depth))
    assert off["run_valid"] == on["run_valid"] == "True"
    assert off["n_chain_broken"] == on["n_chain_broken"] == "0"
    assert off["n_derived_leaks"] == "6" and on["n_derived_leaks"] == "0"
    assert off["n_search_derived_leaks"] == "6" and on["n_search_derived_leaks"] == "0"
    assert on["n_misses"] == "0" and on["n_search_misses"] == "0"


def test_compare_runs_verdict(fake, tmp_path, capsys):
    import compare_runs

    with shim_for(fake.url, gate_on=False) as (_, url):
        off = _run(url, tmp_path / "off", n_chains=5)["run_dir"]
    with shim_for(fake.url, gate_on=True) as (_, url):
        on = _run(url, tmp_path / "on", n_chains=5)["run_dir"]
    assert compare_runs.main([off, on]) == 0
    assert "PASS: gate ON closed the derived leak (0/5 fetch, 0/5 search)" in capsys.readouterr().out
    # Two gate-off runs must not be reported as a pass.
    assert compare_runs.main([off, off]) == 1
    assert "CHECK:" in capsys.readouterr().out


def test_direct_and_shim_off_agree(fake, tmp_path):
    """The shim in OFF mode must not change any graded number."""
    direct = _summary(_run(fake.url, tmp_path / "direct", n_chains=8, seed=7))
    with shim_for(fake.url, gate_on=False) as (_, url):
        via = _summary(_run(url, tmp_path / "via", n_chains=8, seed=7))
    keys = ["run_valid", "n_derived_graded", "n_derived_leaks", "n_search_derived_leaks",
            "n_source_control_allowed", "n_misses", "n_search_misses", "n_chain_broken",
            "n_writes", "n_writes_failed"]
    assert {k: direct[k] for k in keys} == {k: via[k] for k in keys}
