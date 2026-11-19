# lineage-gate-shim

A small reverse proxy that adds a **lineage gate** in front of a MemClaw-compatible memory server, so you can rerun ArgusFleet's `derived-leakage` experiment ([PR #3](https://github.com/caura-ai/argusfleet/pull/3)) with the gate **off** and **on** against your own staging server and compare.

**The rule:** a reader may receive memory `M` only if the reader may also read every memory in `M`'s `metadata.derived_from` ancestry. This is the derivation gate from Sangaraju & Vissa, *Lineage-Aware Memory Governance*, IEEE Access, 2026 ([DOI 10.1109/ACCESS.2026.3730363](https://doi.org/10.1109/ACCESS.2026.3730363)).

Nothing on your server changes. The shim runs on your machine, ArgusFleet talks to the shim, and the shim forwards every request to your server.

```
ArgusFleet (PR #3)  ──>  lineage-gate-shim (127.0.0.1:8787)  ──>  your MemClaw server
                         gate on: withholds memories whose
                         ancestry the caller can't read
```

## Expected result

Your Nov 15 staging run (seed 42, 20 chains, depth 1) is the gate-off baseline: teammate C read the derived summary M 20/20 times, while being denied the secret S 0/20 times. With the gate on, the target is **0/20 derived leaks on fetch and on search, with every control unchanged**.

This is what the full A/B run produces against the bundled fake server, which models today's server (entry-level scope enforced, `derived_from` ignored on reads):

| Probe | Expected | Gate OFF fetch | Gate OFF search | Gate ON fetch | Gate ON search |
|---|---|---|---|---|---|
| C reads derived summary M | deny | 20/20 | 20/20 | **0/20** | **0/20** |
| C reads secret S | deny | 0/20 | 0/20 | 0/20 | 0/20 |
| C reads clean team memory D | allow | 20/20 | 20/20 | 20/20 | 20/20 |
| A reads its own summary M | allow | 20/20 | 20/20 | 20/20 | 20/20 |

Both runs are valid, with 60/60 writes and 0 broken chains. The gate-on derived leak rate is 0.0 (95% Wilson interval 0.0–0.161). These are fake-server numbers; your staging run is the real measurement.

## What you need

- Your ArgusFleet checkout with PR #3 at its current head (commit `9f8be18`), installed in its virtualenv (`pip install -e .`). If your checkout is older, pull the `derived-leakage-probe` branch; the experiment code is unchanged, only the DOI citation was added.
- The same `.env` as that run: `MEMCLAW_API_KEY` (a tenant key that can call `POST /api/v1/admin/agent-keys/provision`) and `MEMCLAW_TENANT_ID` (a **test** tenant).
- Nothing else. The shim uses only `httpx`, which ArgusFleet already installs.

```bash
git clone https://github.com/sangaraju1988/lineage-gate-shim.git
```

## Step 1 (optional, about 1 minute): offline dry run

This checks your setup with no network calls to your server. Start the bundled fake server in one terminal:

```bash
cd lineage-gate-shim
python tests/fake_memclaw.py --port 8790 --tenant-key dryrun-tenant-key
```

In a second terminal, from your ArgusFleet checkout with its virtualenv active:

```bash
MEMCLAW_API_KEY=dryrun-tenant-key MEMCLAW_TENANT_ID=mc_dryrun \
UPSTREAM=http://127.0.0.1:8790 /path/to/lineage-gate-shim/run_ab.sh
```

The values on the command line override your `.env` for this run only, so nothing reaches your server. It should end with:

```
PASS: gate ON closed the derived leak (0/20 fetch, 0/20 search) with every control unchanged.
```

Stop the fake server with Ctrl-C.

## Step 2: run against your staging server

From your ArgusFleet checkout with its virtualenv active, use the same server URL as your Nov 15 run:

```bash
UPSTREAM=https://<your-staging-server> /path/to/lineage-gate-shim/run_ab.sh
```

The script runs these steps (about 10 minutes at the defaults):

1. **Purges the test tenant** in `MEMCLAW_TENANT_ID`, using ArgusFleet's own `cleanup` command. Without this, the server's near-duplicate detector would reject the second run's seed-42 writes with HTTP 409 and corrupt the result. Set `PURGE=0` to skip.
2. Starts the shim with the gate **off** and runs `python -m argusfleet run derived-leakage --seed 42`.
3. Purges again, restarts the shim with the gate **on** and runs the same experiment.
4. Prints the comparison table and `PASS` or `CHECK`, with exit code 0 or 1.

Afterwards:

- **Revoke the 4 agent keys** the runs created. ArgusFleet prints a `REVOKE REMINDER` with their ids after each run (`af-dlk-a-*` and `af-dlk-c-*`).
- Results are in `outputs/runs/<timestamp>-derived-leakage-gate-off-seed42/` and `...-gate-on-seed42/`, in the same format as your Nov 15 folder.
- The shim's decisions are in `outputs/lineage-gate-audit-on.jsonl`, one line per gated read. Withheld reads name the ancestor that was denied.

To re-print the comparison later:

```bash
python /path/to/lineage-gate-shim/compare_runs.py <gate-off run dir> <gate-on run dir>
```

Options: `PORT` (default 8787), `SEED` (default 42), `PYTHON` (default `python`).

### Running it by hand instead

If you prefer separate terminals, start the shim in terminal 1:

```bash
python /path/to/lineage-gate-shim/lineage_gate_shim.py --upstream https://<your-staging-server> --gate on
```

Then, in terminal 2, from your ArgusFleet checkout:

```bash
python -m argusfleet cleanup --yes
MEMCLAW_BASE_URL=http://127.0.0.1:8787 python -m argusfleet run derived-leakage --label derived-leakage-gate-on
```

For the baseline, use `--gate off` and `--label derived-leakage-gate-off`. In these runs, `events.jsonl` records `base_url` as the shim address (`http://127.0.0.1:8787`); the shim's log shows the upstream server.

## How the gate works

For every memory the shim is about to return, it walks `metadata.derived_from`, including multiple parents and multiple hops, and fetches each ancestor from your server **with the caller's own API key**. Your server stays the only authority on who may read what. If any ancestor comes back 401, 403 or 404, the memory is withheld.

| Endpoint | Gate on |
|---|---|
| `GET /api/v1/memories/{id}` | Withheld memory returns `404 {"detail": "Memory not found"}`, the status your server returned for its own denies on Nov 15 |
| `POST /api/v1/search` | Withheld memories are removed from `items` |
| `GET /api/v1/memories` (list) | Withheld memories are removed from `items` |
| Everything else (writes, provisioning, cleanup, stats) | Forwarded unchanged |

The gate fails closed:

- **Withheld:** a missing ancestor, a malformed `derived_from`, an unsafe ancestor id, a cycle (stopped by a visited set), or more than `--max-depth` hops (default 16).
- **Error, never a silent allow:** if an ancestor can't be checked because of a server error, rate limit or network failure after 3 attempts, the shim returns `503`.

`--gate off` makes the shim a plain pass-through. A test confirms that a run through the shim with the gate off gives the same graded numbers as a run with no shim at all.

The shim never logs API keys; the audit log identifies callers by a short hash only. It listens on `127.0.0.1` by default.

## Limits

- **It's a measurement tool, not a production fix.** `derived_from` is written by the client, so a writer that leaves it out isn't gated. A production gate would record lineage on the server and enforce it in the read path. The shim shows what that gate changes, using the same experiment and the same server.
- **Only the three read endpoints above are gated.** Any other endpoint on your server that returns memory content passes through unchanged.
- **Search gets slower.** Each search result costs one extra GET per ancestor; at depth 1, up to 5 extra GETs per search. The comparison table reports fetch and search latency for both runs.

## Tests

The suite has 32 tests. 26 cover the shim over real HTTP: the core rule, multi-parent and depth-4 chains, cycles, fail-closed cases, search and list filtering, gzip, concurrency, and a check that keys never reach the audit log. 6 run ArgusFleet's PR #3 experiment end to end through the shim: seed 42 at the default config, depths 2 and 4, the comparison verdict, and gate-off matching a direct run with no shim.

```bash
pip install pytest
ARGUSFLEET_DIR=/path/to/argusfleet python -m pytest -q
```

The end-to-end tests are skipped if ArgusFleet can't be imported; `ARGUSFLEET_DIR` isn't needed if it's pip-installed.

## License

MIT. See [LICENSE](LICENSE).
