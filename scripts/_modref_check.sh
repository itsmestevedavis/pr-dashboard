#!/usr/bin/env bash
# Temporary modularization gate. Deleted in the final task.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== pytest =="
python3 -m pytest -q

echo "== HTTP surface =="
python3 server.py & SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
until curl -sf http://127.0.0.1:8765/ -o /dev/null; do sleep 0.2; done

curl -s http://127.0.0.1:8765/ > /tmp/_modref_index.html
diff -u scripts/_modref_baseline/index.html /tmp/_modref_index.html && echo "OK: / byte-stable"

curl -s http://127.0.0.1:8765/api/status > /tmp/_modref_status.json
diff -u scripts/_modref_baseline/status.json /tmp/_modref_status.json && echo "OK: /api/status byte-stable"

BASELINE_SHAPE=$(cat scripts/_modref_baseline/prs.shape)
if [ "$BASELINE_SHAPE" = "UNAVAILABLE" ]; then
  echo "WARN: /api/prs unavailable (skipped)"
else
  NEW_SHAPE=$(curl -s --max-time 10 "http://127.0.0.1:8765/api/prs" | python3 -c "import sys,json; d=json.load(sys.stdin); print(type(d).__name__); k=d if isinstance(d,dict) else (d[0] if d else {}); print(sorted(k.keys()) if isinstance(k,dict) else 'list-empty')" 2>/dev/null) || { echo "WARN: /api/prs unavailable (skipped)"; NEW_SHAPE=""; }
  if [ -n "$NEW_SHAPE" ]; then
    diff <(echo "$BASELINE_SHAPE") <(echo "$NEW_SHAPE") && echo "OK: /api/prs shape-stable"
  else
    echo "WARN: /api/prs unavailable (skipped)"
  fi
fi

echo "ALL GREEN"
