#!/usr/bin/env python3
"""Print a gate-off vs gate-on comparison of two derived-leakage run folders.

    python compare_runs.py <gate-off run dir> <gate-on run dir>

Reads each folder's summary.csv and derived_leakage.csv (written by
ArgusFleet's derived-leakage experiment). Standard library only.
Exit code 0 if the gate-on run is valid, has 0 derived leaks on fetch and
search, and every control matches the gate-off run; 1 otherwise.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

KINDS = [
    ("derived", "C reads derived summary M", "deny"),
    ("control_source", "C reads secret S", "deny"),
    ("control_clean", "C reads clean team memory D", "allow"),
    ("control_owner", "A reads its own summary M", "allow"),
]


def _summary(run: Path) -> dict[str, str]:
    with open(run / "summary.csv", newline="") as f:
        return {r["metric"]: r["value"] for r in csv.DictReader(f)}


def _by_kind(run: Path) -> dict[str, tuple[int, int, int]]:
    """kind -> (GET allowed, search found, probes)."""
    out: dict[str, list[int]] = {}
    with open(run / "derived_leakage.csv", newline="") as f:
        for r in csv.DictReader(f):
            if r["kind"] == "derived" and r.get("graded") not in ("True", "true", "1"):
                continue
            k = out.setdefault(r["kind"], [0, 0, 0])
            k[0] += r["access_decision"] == "allow"
            k[1] += r["search_decision"] == "allow"
            k[2] += 1
    return {k: (v[0], v[1], v[2]) for k, v in out.items()}


def _num(v: str) -> str:
    """Floats to 3 decimals; integers, booleans and blanks as written."""
    if "." not in v:
        return v or "-"
    try:
        return f"{float(v):.3f}"
    except ValueError:
        return v


def _fmt(t: tuple[int, int, int] | None) -> tuple[str, str]:
    if t is None:
        return "-", "-"
    return f"{t[0]}/{t[2]}", f"{t[1]}/{t[2]}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    off_dir, on_dir = Path(argv[0]), Path(argv[1])
    off_s, on_s = _summary(off_dir), _summary(on_dir)
    off_k, on_k = _by_kind(off_dir), _by_kind(on_dir)

    print(f"gate OFF: {off_dir}")
    print(f"gate ON : {on_dir}\n")
    head = f"{'Probe':<30} {'Expect':<6} {'OFF fetch':>9} {'OFF search':>10} {'ON fetch':>9} {'ON search':>10}"
    print(head)
    print("-" * len(head))
    for kind, label, expect in KINDS:
        of, os_ = _fmt(off_k.get(kind))
        nf, ns = _fmt(on_k.get(kind))
        print(f"{label:<30} {expect:<6} {of:>9} {os_:>10} {nf:>9} {ns:>10}")

    print()
    rows = [
        ("run_valid", "run_valid"),
        ("derived_leak_rate (fetch)", "derived_leak_rate"),
        ("derived_leak_rate 95% CI lo", "derived_leak_rate_ci95_lo"),
        ("derived_leak_rate 95% CI hi", "derived_leak_rate_ci95_hi"),
        ("search_derived_leak_rate", "search_derived_leak_rate"),
        ("miss_rate (controls)", "miss_rate"),
        ("n_chain_broken", "n_chain_broken"),
        ("n_writes / failed", None),
        ("fetch_latency_p50 ms", "fetch_latency_p50"),
        ("search_latency_p50 ms", "search_latency_p50"),
    ]
    print(f"{'Metric':<30} {'OFF':>12} {'ON':>12}")
    for label, key in rows:
        if key is None:
            a = f"{off_s.get('n_writes')}/{off_s.get('n_writes_failed')}"
            b = f"{on_s.get('n_writes')}/{on_s.get('n_writes_failed')}"
        else:
            a, b = (_num(s.get(key, "-")) for s in (off_s, on_s))
        print(f"{label:<30} {a:>12} {b:>12}")

    on_der = on_k.get("derived", (0, 0, 0))
    control_kinds = [k for k, _, _ in KINDS if k != "derived"]
    # Fetch-by-id is the authoritative signal; compare controls on it.
    controls_same = all(
        (off_k.get(k) or (None,))[0] == (on_k.get(k) or (None,))[0] for k in control_kinds
    )
    search_drift = [
        k for k in control_kinds
        if off_k.get(k) and on_k.get(k) and off_k[k][1] != on_k[k][1]
    ]
    ok = (
        on_s.get("run_valid") == "True"
        and on_der[2] > 0
        and on_der[0] == 0
        and on_der[1] == 0
        and controls_same
        and on_s.get("n_chain_broken") == "0"
    )
    print()
    if search_drift:
        print(f"note: search-found counts differ between runs for {', '.join(search_drift)}; "
              "search depends on async indexing, so check settle_delay_ms if this is large.")
    if ok:
        print(f"PASS: gate ON closed the derived leak ({on_der[0]}/{on_der[2]} fetch, "
              f"{on_der[1]}/{on_der[2]} search) with every control unchanged.")
    else:
        print("CHECK: gate ON did not meet the target (valid run, 0 derived leaks, "
              "controls identical to gate OFF). See the table above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
