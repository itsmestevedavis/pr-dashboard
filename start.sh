#!/usr/bin/env bash
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
SERVER="$DIR/server.py"
URL="http://${HOST:-127.0.0.1}:${PORT:-8765}"

# Run from the repo root so headless `claude -p` jobs inherit this cwd and load the
# project CLAUDE.md (e.g. the Slack-nudge runbook).
cd "$DIR" || exit 1

# Note: killing/respawning the server (and spawned claude/gh/git children) makes macOS
# print benign "MallocStackLogging: can't turn off ..." lines. It's OS noise, not a bug —
# see docs/mallocstacklogging-message.md.
PID=""
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null; exit 0; }
trap cleanup INT TERM

# Open browser once after the first successful start
(until curl -sf "$URL" -o /dev/null; do sleep 0.3; done; open "$URL") &

while true; do
  python3 "$SERVER" &
  PID=$!
  MTIME=$(python3 -c "import os; print(os.stat('$SERVER').st_mtime_ns)")
  CHANGED=false

  while kill -0 "$PID" 2>/dev/null; do
    sleep 1
    NEW=$(python3 -c "import os; print(os.stat('$SERVER').st_mtime_ns)")
    if [ "$NEW" != "$MTIME" ]; then
      echo "↺  server.py changed — restarting…"
      kill "$PID"
      wait "$PID" 2>/dev/null
      CHANGED=true
      break
    fi
  done

  if [ "$CHANGED" != "true" ]; then
    wait "$PID" 2>/dev/null
    break
  fi
done
