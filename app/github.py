"""app/github.py — GitHub CLI integration helpers.

Wraps the `gh` CLI for JSON fetching and PR enrichment. Cache globals live here
so they are shared across all callers that import this module.
"""

import json
import subprocess
import threading
import time

from app import config

# ---- Globals ---------------------------------------------------------------

_me = None
_detail_cache = {}  # (repo, number) -> (timestamp, payload)
_cache_lock = threading.Lock()


# ---- gh helpers ------------------------------------------------------------

def gh_run(args, timeout=None):
    """Run `gh` with args, return stdout text or raise."""
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def gh_json(args, timeout=None):
    out = gh_run(args, timeout=timeout)
    return json.loads(out) if out.strip() else None


def get_my_login():
    global _me
    if _me is None:
        data = gh_json(["api", "user"])
        _me = data["login"]
    return _me


# ---- Issues ----------------------------------------------------------------

def list_my_issues():
    """Return open GitHub issues assigned to me across all repos, most recently
    updated first. Issues only — `gh search issues` excludes PRs (those come from
    app.prs.list_my_prs)."""
    me = get_my_login()
    issues = gh_json([
        "search", "issues",
        "--assignee", me,
        "--state", "open",
        "--archived=false",
        "--sort", "updated",
        "--limit", "50",
        "--json", "number,title,url,repository,updatedAt",
    ]) or []
    return [
        {
            "number": issue.get("number"),
            "title": issue.get("title") or "",
            "url": issue.get("url") or "",
            "updatedAt": issue.get("updatedAt") or "",
            "repository": (issue.get("repository") or {}).get("nameWithOwner") or "",
        }
        for issue in issues
    ]


# ---- PR enrichment ---------------------------------------------------------

def fetch_detail(repo, number, fresh=False):
    """Fetch (and cache) the PR detail blob used for status determination."""
    key = (repo, number)
    now = time.time()
    if not fresh:
        with _cache_lock:
            entry = _detail_cache.get(key)
        if entry and now - entry[0] < config.CACHE_TTL:
            return entry[1]
    detail = gh_json([
        "pr", "view", str(number), "--repo", repo,
        "--json", "reviews,reviewRequests,commits,latestReviews,author",
    ])
    with _cache_lock:
        _detail_cache[key] = (now, detail)
    return detail


def fetch_review_comments(repo, number, fresh=False):
    """Fetch all PR review comments (used for in_reply_to detection)."""
    key = ("comments", repo, number)
    now = time.time()
    if not fresh:
        with _cache_lock:
            entry = _detail_cache.get(key)
        if entry and now - entry[0] < config.CACHE_TTL:
            return entry[1]
    out = gh_run([
        "api", f"repos/{repo}/pulls/{number}/comments", "--paginate",
    ])
    comments = json.loads(out) if out.strip() else []
    with _cache_lock:
        _detail_cache[key] = (now, comments)
    return comments
