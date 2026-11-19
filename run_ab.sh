#!/usr/bin/env bash
# Run ArgusFleet's derived-leakage experiment twice through the lineage-gate
# shim -- gate OFF, then gate ON -- and print the comparison.
#
# Run from your ArgusFleet checkout (the folder with your .env), with the
# ArgusFleet virtualenv active:
#
#   UPSTREAM=https://your-staging-server /path/to/lineage-gate-shim/run_ab.sh
#
# Optional: PORT (default 8787), SEED (default 42), PYTHON (default python),
#           PURGE (default 1).
#
# Use a TEST tenant. Before each run the script soft-deletes every memory in
# MEMCLAW_TENANT_ID (ArgusFleet's own `cleanup` command), because the server's
# near-duplicate detector would otherwise reject the second run's seed-42
# writes with HTTP 409 and corrupt its numbers. Set PURGE=0 to skip.
set -euo pipefail

SHIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python}"
PORT="${PORT:-8787}"
SEED="${SEED:-42}"
PURGE="${PURGE:-1}"
: "${UPSTREAM:?Set UPSTREAM to your MemClaw server URL, e.g. UPSTREAM=https://staging.example}"

"$PY" -c "import httpx, argusfleet" 2>/dev/null || {
  echo "error: '$PY' cannot import httpx and argusfleet. Activate the ArgusFleet virtualenv first." >&2
  exit 2
}
if "$PY" -c "import httpx,sys; httpx.get('http://127.0.0.1:${PORT}/', timeout=1); sys.exit(0)" 2>/dev/null; then
  echo "error: something is already listening on 127.0.0.1:${PORT}. Set PORT to a free port." >&2
  exit 2
fi

OUT_ROOT="${ARGUSFLEET_OUTPUT_DIR:-$PWD/outputs}"
mkdir -p "$OUT_ROOT"
SHIM_PID=""
cleanup() { if [[ -n "$SHIM_PID" ]]; then kill "$SHIM_PID" 2>/dev/null || true; wait "$SHIM_PID" 2>/dev/null || true; fi; }
trap cleanup EXIT

run_mode() {
  local mode="$1"
  local audit="$OUT_ROOT/lineage-gate-audit-${mode}.jsonl"
  local MODE
  MODE="$(printf '%s' "$mode" | tr '[:lower:]' '[:upper:]')"
  if [[ "$PURGE" == "1" ]]; then
    echo "=== gate ${MODE}: purging test tenant(s) in MEMCLAW_TENANT_ID before the run" >&2
    MEMCLAW_BASE_URL="$UPSTREAM" "$PY" -m argusfleet cleanup --yes >&2
  fi
  echo "=== gate ${MODE}: starting shim on 127.0.0.1:${PORT} -> ${UPSTREAM}" >&2
  "$PY" "$SHIM_DIR/lineage_gate_shim.py" --upstream "$UPSTREAM" --gate "$mode" \
        --port "$PORT" --audit-log "$audit" >"$OUT_ROOT/lineage-gate-shim-${mode}.log" 2>&1 &
  SHIM_PID=$!
  local ready=0
  for _ in $(seq 1 50); do
    if "$PY" -c "import httpx,sys; r=httpx.get('http://127.0.0.1:${PORT}/__lineage_gate/health', timeout=1); sys.exit(0 if r.json().get('gate')=='${mode}' else 1)" 2>/dev/null; then
      ready=1; break
    fi
    if ! kill -0 "$SHIM_PID" 2>/dev/null; then
      echo "error: shim exited; see $OUT_ROOT/lineage-gate-shim-${mode}.log" >&2; exit 1
    fi
    sleep 0.2
  done
  if [[ "$ready" != "1" ]]; then
    echo "error: shim not healthy with gate ${mode} after 10s; see $OUT_ROOT/lineage-gate-shim-${mode}.log" >&2; exit 1
  fi
  echo "=== gate ${MODE}: running derived-leakage (seed ${SEED})" >&2
  local json
  json="$(MEMCLAW_BASE_URL="http://127.0.0.1:${PORT}" "$PY" -m argusfleet run derived-leakage \
          --seed "$SEED" --label "derived-leakage-gate-${mode}")"
  kill "$SHIM_PID" 2>/dev/null || true; wait "$SHIM_PID" 2>/dev/null || true; SHIM_PID=""
  RUN_DIR="$(printf '%s' "$json" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['run_dir'])")"
  echo "=== gate ${MODE}: results in ${RUN_DIR}" >&2
}

# Called directly (not inside $(...)) so `set -e` and the EXIT trap apply.
RUN_DIR=""
run_mode off
OFF_DIR="$RUN_DIR"
run_mode on
ON_DIR="$RUN_DIR"
echo >&2
"$PY" "$SHIM_DIR/compare_runs.py" "$OFF_DIR" "$ON_DIR"
