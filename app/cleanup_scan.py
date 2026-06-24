"""app/cleanup_scan.py — scan repos for stale branches and worktrees."""

import os

import cleanup
from app import config
from app.jobs import clones


def scan_cleanup(fresh=False):
    """Scan all target repos. Returns {"repos": [{path,label,kind,ok,error?,candidates}]}."""
    repos = []
    for path, label, kind in clones.cleanup_repo_targets():
        entry = {"path": path, "label": label, "kind": kind, "ok": True, "candidates": []}
        if not os.path.isdir(os.path.join(path, ".git")):
            entry["ok"] = False
            entry["error"] = "not a git repo"
            repos.append(entry)
            continue
        try:
            if fresh:
                clones._git_runner(["fetch", "--prune"], path)
            entry["candidates"] = cleanup.scan_repo(
                clones._git_runner, path, author_email=config.CLEANUP_AUTHOR_EMAIL or None
            )
        except Exception as e:  # pragma: no cover - defensive
            entry["ok"] = False
            entry["error"] = str(e)
        repos.append(entry)
    return {"repos": repos}
