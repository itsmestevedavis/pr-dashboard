#!/usr/bin/env python3
"""Auto-sync this repo to its GitHub remote on file change.

Polls every POLL seconds. After DEBOUNCE seconds of no further changes,
runs `git add -A && git commit -m 'auto: <timestamp>' && git push origin main`.

Designed to be run as a launchd LaunchAgent. Logs to stdout, which launchd
redirects to /tmp/pr-dashboard-sync.log.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)
POLL = 2          # seconds between mtime polls
DEBOUNCE = 5      # seconds of quiet after last change before syncing
IGNORE_DIRS = {".git", "__pycache__", "node_modules", "docs"}


def collect_mtimes():
    out = {}
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                out[p] = os.stat(p).st_mtime
            except OSError:
                pass
    return out


def run(args):
    return subprocess.run(
        args, cwd=REPO, capture_output=True, text=True, check=False,
    )


def has_unpushed_commits():
    return bool(run(
        ["git", "log", "origin/main..HEAD", "--oneline"]
    ).stdout.strip())


def sync():
    run(["git", "add", "-A"])
    # Returncode 0 = no diff staged.
    nothing_staged = run(["git", "diff", "--cached", "--quiet"]).returncode == 0
    if nothing_staged:
        # Maybe a previous push failed; try again.
        if has_unpushed_commits():
            push = run(["git", "push", "origin", "main"])
            if push.returncode == 0:
                print("[sync] pushed pending commits", flush=True)
            else:
                print(f"[sync] push failed: {push.stderr.strip()}", flush=True)
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    commit = run(["git", "commit", "-m", f"auto: {ts}"])
    if commit.returncode != 0:
        print(f"[sync] commit failed: {commit.stderr.strip()}", flush=True)
        return
    push = run(["git", "push", "origin", "main"])
    if push.returncode == 0:
        print(f"[sync] committed + pushed at {ts}", flush=True)
    else:
        print(f"[sync] commit OK, push failed: {push.stderr.strip()}", flush=True)


def main():
    print(f"[sync] watching {REPO} (poll {POLL}s, debounce {DEBOUNCE}s)",
          flush=True)
    prev = collect_mtimes()
    last_change = None
    while True:
        time.sleep(POLL)
        cur = collect_mtimes()
        if cur != prev:
            last_change = time.time()
            prev = cur
            continue
        if last_change is not None and (time.time() - last_change) >= DEBOUNCE:
            try:
                sync()
            except Exception as e:
                print(f"[sync] error: {e}", flush=True)
            prev = collect_mtimes()
            last_change = None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
