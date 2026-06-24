#!/usr/bin/env python3
"""Local PR review dashboard.

Lists open GitHub PRs (from a fixed author list) that require my attention,
and lets me kick off a Claude Code review against each.
"""

import concurrent.futures
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import base64
import urllib.error
import urllib.parse
import urllib.request

import cleanup
from app.http import static_files

# ---- Configuration ---------------------------------------------------------

def _load_dotenv(path):
    """Tiny stdlib .env loader. Lines like KEY=value populate os.environ
    (unless KEY is already set). Quotes around values are stripped.
    Comments (#) and blank lines are ignored. Silently skips missing file.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
# Serializes .env rewrites and the config globals (DEPLOY_TARGET, etc.) that
# settings POSTs reassign while job threads / status GETs read them.
# Reentrant so a handler can hold it across both write_env_var and the global update.
_env_lock = threading.RLock()
_load_dotenv(_ENV_PATH)


def _env_list(name, default=()):
    raw = os.environ.get(name, "")
    return [s.strip() for s in raw.split(",") if s.strip()] or list(default)


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "30"))  # seconds

REVIEW_PROMPT = "Review PR #{number} in {repo}.\n\n"
RE_REVIEW_PROMPT = (
    "Re-review PR #{number} in {repo}. "
    "Check whether your previous review comments have been addressed since your last review.\n\n"
)

STATUS_ORDER = {
    "re_requested": 0,
    "new_commits": 1,
    "author_replied": 2,
    "untouched": 3,
}

STATUS_LABELS = {
    "re_requested": "Re-review requested",
    "new_commits": "Re-review needed",
    "author_replied": "Author replied",
    "untouched": "New",
}

# ---- My PRs (author=@me) ---------------------------------------------------

MY_STATUS_ORDER = {
    "approved": 0,
    "has_comments": 1,
    "not_reviewed_yet": 2,
}

MY_STATUS_LABELS = {
    "approved": "Approved",
    "has_comments": "Has comments",
    "not_reviewed_yet": "Not reviewed yet",
}

# Default reviewers to ping when a PR has no reviews yet.
FRESH_REVIEWERS = _env_list("FRESH_REVIEWERS")

# Team Slack channel for broadcast-style review requests.
TEAM_CHANNEL_ID = os.environ.get("TEAM_CHANNEL_ID", "")

# Extra named teams selectable from the Channel / Nudge dropdowns.
# Format: JSON array of {name, channel_id, reviewers: [...]} objects.
def _parse_teams():
    raw = os.environ.get("TEAMS", "").strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("TEAMS must be a JSON array")
        out = []
        for i in items:
            name = (i.get("name") or "").strip()
            chan = (i.get("channel_id") or "").strip()
            if not name or not chan:
                print(f"[config] WARNING: skipping TEAMS entry with missing name/channel_id: {i!r}", flush=True)
                continue
            out.append({
                "name": name,
                "channel_id": chan,
                "reviewers": [str(r) for r in i.get("reviewers", [])],
            })
        return out
    except Exception as e:
        print(f"[config] WARNING: failed to parse TEAMS: {e}", flush=True)
        return []

TEAMS = _parse_teams()

# Maps GitHub logins to Slack handles (e.g. {"alice": "@alice"}) or member IDs
# (e.g. {"alice": "U01ABCDEF"}). Used in run_nudge() to pass the Slack identity
# directly, so the nudge workflow can DM without a GitHub→Slack lookup.
def _parse_slack_ids():
    raw = os.environ.get("SLACK_IDS", "").strip()
    if not raw:
        return {}
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            print(f"[config] WARNING: SLACK_IDS pair missing ':' — {pair!r}", flush=True)
            continue
        login, slack_id = (s.strip() for s in pair.split(":", 1))
        if not login or not (slack_id.startswith("@") or slack_id.startswith("U")):
            print(f"[config] WARNING: invalid SLACK_IDS pair (value must be a Slack "
                  f"@handle or U… member ID) — {pair!r}", flush=True)
            continue
        result[login] = slack_id
    return result

SLACK_ID_MAP = _parse_slack_ids()

# Default deploy environment for all PRs (e.g. "csi-3"). Empty = no Deploy button shown.
DEPLOY_TARGET = os.environ.get("DEPLOY_TARGET", "")

# Editor command for opening the config folder. Empty = auto-detect (code → open/xdg-open).
EDITOR_CMD = os.environ.get("EDITOR_CMD", "")

# Local repo paths the Cleanup tab scans for stale branches/worktrees (comma list,
# ~ expanded). The agent-clone cache is always scanned in addition to these.
CLEANUP_REPOS = _env_list("CLEANUP_REPOS")

# "Me" for the Cleanup tab's authored-by-me filter. Empty = use each repo's
# `git config user.email`.
CLEANUP_AUTHOR_EMAIL = os.environ.get("CLEANUP_AUTHOR_EMAIL", "")

# ---- Jira (assigned tickets) ----------------------------------------------
def _normalize_jira_site(raw):
    """Accept a Jira site as a bare host or a full URL; return just the host.

    Users routinely paste "https://org.atlassian.net/"; the API URL is built as
    https://{site}{path}, so a scheme/trailing slash here yields an unresolvable
    host. Strip them at the boundary so both forms work.
    """
    return re.sub(r"^https?://", "", (raw or "").strip(), flags=re.IGNORECASE).rstrip("/")


# Atlassian site host (e.g. "cognota.atlassian.net"), account email, and an API
# token from id.atlassian.com. All three empty = Tickets tab shows a config hint.
JIRA_SITE = _normalize_jira_site(os.environ.get("JIRA_SITE", ""))
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
# Status names to show on the Tickets tab (comma list in .env). Empty = show all.
JIRA_STATUS_FILTER = _env_list("JIRA_STATUS_FILTER")

# Tickets assigned to me that are not finished, most-recently-updated first.
JIRA_JQL = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"

# Jira issue keys look like ABC-123 — validate before interpolating into API paths.
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def _is_human_author(author):
    """True if the GraphQL author node is a real user (not a Bot, etc)."""
    if not author:
        return False
    typename = author.get("__typename")
    # When __typename isn't fetched, fall back to accepting it (legacy callers).
    return typename in (None, "User", "Mannequin", "EnterpriseUserAccount")


def determine_my_pr_status(pr, me):
    """Categorize one of my open PRs.

    Returns dict {status, status_label, active_commenters} or None if the
    PR should be excluded (drafts).
    """
    if pr.get("isDraft"):
        return None

    latest_reviews = (pr.get("latestReviews") or {}).get("nodes") or []
    threads = (pr.get("reviewThreads") or {}).get("nodes") or []
    comments = (pr.get("comments") or {}).get("nodes") or []
    review_decision = pr.get("reviewDecision")

    approvers = {
        (r.get("author") or {}).get("login")
        for r in latest_reviews
        if r.get("state") == "APPROVED" and _is_human_author(r.get("author"))
    }
    approvers.discard(None)

    # Build a per-reviewer picture of their inline threads so we can tell
    # whether their CHANGES_REQUESTED is still actionable by me.
    reviewer_has_active_thread: dict = {}   # login → True if any thread is live
    reviewer_has_any_thread: dict = {}      # login → True if they left any thread
    for t in threads:
        cnodes = (t.get("comments") or {}).get("nodes") or []
        if not cnodes:
            continue
        author = cnodes[0].get("author") or {}
        if not _is_human_author(author):
            continue
        login = author.get("login")
        if not login:
            continue
        reviewer_has_any_thread[login] = True
        # Whoever commented last decides whose court the ball is in. If I (the PR
        # author) replied last, I've already addressed it — even if the thread is
        # still open — so it shouldn't keep the PR in "has comments to address".
        last_nodes = (t.get("lastComment") or {}).get("nodes") or []
        last_author = (last_nodes[0].get("author") or {}).get("login") if last_nodes else None
        author_replied_last = last_author == me
        # A thread is "active" only if it is neither resolved nor outdated and I
        # haven't already replied. Outdated means the code changed under the
        # comment — addressed by new commits, so no longer something I need to fix.
        if not t.get("isResolved") and not t.get("isOutdated") and not author_replied_last:
            reviewer_has_active_thread[login] = True

    unresolved_inline_authors = {
        login for login, _ in reviewer_has_active_thread.items()
        if login not in approvers
    }

    review_body_authors = set()
    for r in latest_reviews:
        if r.get("state") != "CHANGES_REQUESTED":
            continue
        author = r.get("author") or {}
        if not _is_human_author(author):
            continue
        login = author.get("login")
        if not login:
            continue
        # If the reviewer had inline threads but all are now resolved or
        # outdated, their changes-request has been addressed — the ball is
        # in their court, not mine. Exclude them from "active" so the PR
        # doesn't sit in "has comments to address" forever.
        if reviewer_has_any_thread.get(login) and not reviewer_has_active_thread.get(login):
            continue
        review_body_authors.add(login)
    review_body_authors -= approvers

    general_comment_authors = set()
    for c in comments:
        author = c.get("author") or {}
        if not _is_human_author(author):
            continue
        login = author.get("login")
        if login and login != me and login not in approvers:
            general_comment_authors.add(login)

    active = (
        unresolved_inline_authors | review_body_authors | general_comment_authors
    )

    stale_reviewers = set()
    for r in latest_reviews:
        if r.get("state") not in ("CHANGES_REQUESTED", "COMMENTED"):
            continue
        author = r.get("author") or {}
        if not _is_human_author(author):
            continue
        login = author.get("login")
        if login and login != me:
            stale_reviewers.add(login)

    if review_decision == "APPROVED" and not active:
        status = "approved"
    elif active:
        status = "has_comments"
    else:
        status = "not_reviewed_yet"

    any_human_review = any(_is_human_author(r.get("author")) for r in latest_reviews)
    if stale_reviewers:
        nudge_mode = "re_review"
        nudge_targets = sorted(stale_reviewers)
    elif not any_human_review:
        nudge_mode = "fresh"
        nudge_targets = list(FRESH_REVIEWERS)
    else:
        nudge_mode = None
        nudge_targets = []

    return {
        "status": status,
        "status_label": MY_STATUS_LABELS[status],
        "active_commenters": sorted(active),
        "stale_reviewers": sorted(stale_reviewers),
        "nudge_mode": nudge_mode,
        "nudge_targets": nudge_targets,
    }


MY_PRS_GRAPHQL = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 50) {
    nodes {
      ... on PullRequest {
        number
        title
        url
        isDraft
        updatedAt
        headRefName
        baseRefName
        mergeStateStatus
        author { login __typename }
        repository {
          nameWithOwner
          viewerDefaultMergeMethod
        }
        reviewDecision
        latestReviews(first: 50) {
          nodes {
            author { login __typename }
            state
            submittedAt
          }
        }
        reviewThreads(first: 50) {
          nodes {
            isResolved
            isOutdated
            comments(first: 1) {
              nodes { author { login __typename } }
            }
            lastComment: comments(last: 1) {
              nodes { author { login __typename } }
            }
          }
        }
        comments(last: 50) {
          nodes {
            author { login __typename }
            createdAt
          }
        }
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
                contexts(first: 100) {
                  totalCount
                  nodes {
                    __typename
                    ... on CheckRun { name conclusion status detailsUrl }
                    ... on StatusContext { context state targetUrl }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


_CHECK_PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_CHECK_FAILED = {
    "FAILURE", "ERROR", "TIMED_OUT", "CANCELLED",
    "ACTION_REQUIRED", "STARTUP_FAILURE",
}


def summarize_checks(rollup: dict) -> dict:
    """Bucket a statusCheckRollup's contexts into passed/pending/failed.

    Normalizes both node types: CheckRun (GitHub Actions etc.) uses
    status/conclusion, while the legacy StatusContext uses a single state.
    Returns {passed: int, pending: [{name,url}], failed: [{name,url}], truncated: bool}.
    Greens are counted (not named); pending/failed are named with a details URL.
    """
    contexts = (rollup.get("contexts") or {})
    nodes = contexts.get("nodes") or []
    passed = 0
    pending: list = []
    failed: list = []
    for node in nodes:
        if node.get("__typename") == "CheckRun":
            name = node.get("name") or "check"
            url = node.get("detailsUrl") or ""
            # An incomplete CheckRun has no conclusion yet -> pending.
            verdict = node.get("conclusion") if node.get("status") == "COMPLETED" else None
        else:  # StatusContext (legacy commit status)
            name = node.get("context") or "check"
            url = node.get("targetUrl") or ""
            verdict = node.get("state")
        verdict = (verdict or "").upper()
        if verdict in _CHECK_PASSED:
            passed += 1
        elif verdict in _CHECK_FAILED:
            failed.append({"name": name, "url": url})
        else:
            pending.append({"name": name, "url": url})
    total = contexts.get("totalCount") or len(nodes)
    return {
        "passed": passed,
        "pending": pending,
        "failed": failed,
        "truncated": total > len(nodes),
    }


def pr_behind_count(repo: str, base: str, head: str) -> int:
    """Commits the PR's head branch is behind its base, via the compare API.

    GitHub's `mergeStateStatus` only reports BEHIND when an up-to-date-branch
    protection rule makes being behind the *governing* merge blocker. When a
    behind branch is also blocked by reviews/checks the status collapses to
    BLOCKED and the "behind" signal is hidden — so it can't tell us whether a
    branch can be rebased. The REST compare endpoint always exposes the real
    divergence. Returns 0 on missing refs or any error (never blocks the UI).
    """
    if not (repo and base and head):
        return 0
    try:
        out = gh_run([
            "api", f"repos/{repo}/compare/{base}...{head}?per_page=1",
            "--jq", ".behind_by",
        ]).strip()
    except Exception as e:
        print(f"[warn] compare failed for {repo} {base}...{head}: {e}", flush=True)
        return 0
    return int(out) if out.isdigit() else 0


def list_my_prs():
    """Return my open PRs across all repos, enriched with status."""
    me = get_my_login()
    q = f"is:pr is:open author:{me} archived:false"
    out = gh_run([
        "api", "graphql",
        "-f", f"query={MY_PRS_GRAPHQL}",
        "-f", f"q={q}",
    ])
    payload = json.loads(out) if out.strip() else {}
    nodes = (
        ((payload.get("data") or {}).get("search") or {}).get("nodes")
        or []
    )

    out_list = []
    for pr in nodes:
        if not pr:
            continue
        status = determine_my_pr_status(pr, me)
        if status is None:
            continue
        repo = (pr.get("repository") or {}).get("nameWithOwner") or ""
        commit_nodes = ((pr.get("commits") or {}).get("nodes") or [])
        rollup = (
            ((commit_nodes[0].get("commit") or {}).get("statusCheckRollup") or {})
            if commit_nodes else {}
        )
        out_list.append({
            "number": pr.get("number"),
            "title": pr.get("title") or "",
            "url": pr.get("url") or "",
            "updatedAt": pr.get("updatedAt") or "",
            "headRefName": pr.get("headRefName") or "",
            "baseRefName": pr.get("baseRefName") or "",
            "repository": repo,
            "defaultMergeMethod": (
                (pr.get("repository") or {}).get("viewerDefaultMergeMethod")
                or "MERGE"
            ),
            "review_decision": pr.get("reviewDecision") or "",
            "check_state": rollup.get("state") or "",
            "checks": summarize_checks(rollup),
            "merge_state_status": pr.get("mergeStateStatus") or "",
            **status,
        })

    # GitHub's mergeStateStatus hides "behind" behind higher-priority block
    # reasons: a PR that is behind *and* blocked by reviews/checks reports
    # BLOCKED, not BEHIND. Ask the compare API per PR for the real divergence
    # so the rebase signal fires whenever the branch can be rebased. These are
    # independent blocking `gh` round trips, so fan out (mirrors list_prs).
    def with_behind(pr):
        return {**pr, "behind_by": pr_behind_count(
            pr["repository"], pr["baseRefName"], pr["headRefName"])}

    if out_list:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(out_list))
        ) as pool:
            out_list = list(pool.map(with_behind, out_list))

    out_list.sort(key=lambda p: p["updatedAt"])
    out_list.sort(key=lambda p: MY_STATUS_ORDER[p["status"]])
    return out_list


# ---- Globals ---------------------------------------------------------------

_me = None
_detail_cache = {}  # (repo, number) -> (timestamp, payload)
_cache_lock = threading.Lock()


# ---- gh helpers ------------------------------------------------------------

def gh_run(args):
    """Run `gh` with args, return stdout text or raise."""
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def gh_json(args):
    out = gh_run(args)
    return json.loads(out) if out.strip() else None


def get_my_login():
    global _me
    if _me is None:
        data = gh_json(["api", "user"])
        _me = data["login"]
    return _me


# ---- Jira helpers ----------------------------------------------------------

def jira_configured():
    """True only when all three Jira credentials are present."""
    return bool(JIRA_SITE and JIRA_EMAIL and JIRA_API_TOKEN)


def jira_request(method, path, params=None, body=None):
    """Call the Jira Cloud REST API. Returns parsed JSON (None for empty body).

    Raises RuntimeError with a readable message on any non-2xx or transport error.
    """
    url = f"https://{JIRA_SITE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Jira {method} {path} failed (HTTP {e.code}): {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Jira {method} {path} failed: {e.reason}")
    return json.loads(raw) if raw.strip() else None


def parse_ticket(issue, site):
    """Map one Jira issue JSON object into the dashboard's ticket dict.

    `status` carries the Jira statusCategory key ("new" | "indeterminate"),
    which the frontend's group-by-status render() uses as the column key.
    """
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    category = (status.get("statusCategory") or {}).get("key") or "new"
    priority = fields.get("priority") or {}
    issuetype = fields.get("issuetype") or {}
    key = issue.get("key", "")
    return {
        "key": key,
        "summary": fields.get("summary") or "",
        "status": category,
        "status_label": status.get("name") or "",
        "status_category": category,
        "priority": priority.get("name"),
        "type": issuetype.get("name") or "",
        "url": f"https://{site}/browse/{key}",
        "updatedAt": fields.get("updated") or "",
    }


def jira_search():
    """Return assigned, not-done tickets as a list of ticket dicts."""
    data = jira_request("GET", "/rest/api/3/search/jql", params={
        "jql": JIRA_JQL,
        "fields": "summary,status,priority,issuetype,updated",
        "maxResults": "100",
    })
    issues = (data or {}).get("issues") or []
    return [parse_ticket(i, JIRA_SITE) for i in issues]


def jira_transitions(key):
    """Available workflow transitions for an issue: [{id, name}]."""
    data = jira_request("GET", f"/rest/api/3/issue/{key}/transitions")
    return [
        {"id": t["id"], "name": t["name"]}
        for t in (data or {}).get("transitions") or []
    ]


def jira_do_transition(key, transition_id):
    """Move an issue through the given transition id."""
    jira_request(
        "POST", f"/rest/api/3/issue/{key}/transitions",
        body={"transition": {"id": str(transition_id)}},
    )


def jira_statuses():
    """Distinct, non-Done status names available in Jira.

    Sorted by category (To Do before In Progress) then name. Done-category
    statuses are dropped since the Tickets JQL never returns them.
    """
    data = jira_request("GET", "/rest/api/3/status")
    cat_by_name = {}
    for s in data or []:
        category = (s.get("statusCategory") or {}).get("key") or "new"
        if category == "done":
            continue
        name = s.get("name") or ""
        if name and name not in cat_by_name:
            cat_by_name[name] = category
    rank = {"new": 0, "indeterminate": 1}
    return sorted(cat_by_name, key=lambda n: (rank.get(cat_by_name[n], 2), n.lower()))


# ---- PR enrichment ---------------------------------------------------------

def fetch_detail(repo, number, fresh=False):
    """Fetch (and cache) the PR detail blob used for status determination."""
    key = (repo, number)
    now = time.time()
    if not fresh:
        with _cache_lock:
            entry = _detail_cache.get(key)
        if entry and now - entry[0] < CACHE_TTL:
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
        if entry and now - entry[0] < CACHE_TTL:
            return entry[1]
    out = gh_run([
        "api", f"repos/{repo}/pulls/{number}/comments", "--paginate",
    ])
    comments = json.loads(out) if out.strip() else []
    with _cache_lock:
        _detail_cache[key] = (now, comments)
    return comments


def author_reply_count(repo, number, me, author_login, since_iso, fresh):
    """Replies from the PR author to my review comments since my last review."""
    if not author_login or not since_iso:
        return 0
    comments = fetch_review_comments(repo, number, fresh=fresh)
    my_ids = {
        c["id"]
        for c in comments
        if (c.get("user") or {}).get("login") == me
    }
    count = 0
    for c in comments:
        if (c.get("user") or {}).get("login") != author_login:
            continue
        in_reply = c.get("in_reply_to_id")
        if in_reply is None or in_reply not in my_ids:
            continue
        if (c.get("created_at") or "") <= since_iso:
            continue
        count += 1
    return count


def determine_status(repo, number, detail, me, fresh):
    """Apply the spec's status rules. Return dict or None to exclude."""
    reviews = detail.get("reviews") or []
    review_requests = detail.get("reviewRequests") or []
    commits = detail.get("commits") or []
    pr_author = (detail.get("author") or {}).get("login")

    my_reviews = sorted(
        (r for r in reviews
         if (r.get("author") or {}).get("login") == me
         and r.get("submittedAt")),
        key=lambda r: r["submittedAt"],
    )
    last_my_review = my_reviews[-1] if my_reviews else None
    last_my_review_at = last_my_review.get("submittedAt") if last_my_review else None

    commit_dates = [c.get("committedDate") for c in commits if c.get("committedDate")]
    last_commit_date = max(commit_dates) if commit_dates else None

    me_in_requests = any(
        (u or {}).get("login") == me for u in review_requests
    )
    re_requested = bool(last_my_review) and me_in_requests

    has_new_commits = bool(
        last_my_review and last_commit_date and last_commit_date > last_my_review_at
    )
    new_commits_count = (
        sum(
            1 for c in commits
            if (c.get("committedDate") or "") > (last_my_review_at or "")
        )
        if has_new_commits else 0
    )

    replies = 0
    if last_my_review:
        replies = author_reply_count(
            repo, number, me, pr_author, last_my_review_at, fresh,
        )

    # Exclude: I've reviewed, nothing new since.
    if last_my_review and not has_new_commits and not re_requested and replies == 0:
        return None

    # Priority: re_requested > new_commits > author_replied > untouched
    if re_requested:
        status = "re_requested"
        detail_str = "Re-requested after your last review"
    elif has_new_commits:
        status = "new_commits"
        noun = "commit" if new_commits_count == 1 else "commits"
        detail_str = f"{new_commits_count} new {noun} since your review"
    elif replies > 0:
        status = "author_replied"
        noun = "reply" if replies == 1 else "replies"
        detail_str = f"{replies} {noun} to your comments"
    elif last_my_review is None:
        # Skip PRs that another reviewer has already given a verdict on — they
        # don't need a pile-on from me unless I'm explicitly re-requested.
        latest_reviews = detail.get("latestReviews") or []
        other_verdicts = [
            r for r in latest_reviews
            if (r.get("author") or {}).get("login") not in (me, None, pr_author)
            and r.get("state") in ("APPROVED", "CHANGES_REQUESTED")
        ]
        if other_verdicts:
            return None
        status = "untouched"
        detail_str = None
    else:
        return None

    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "status_detail": detail_str,
    }


def list_prs(fresh=False):
    me = get_my_login()
    results = gh_json([
        "search", "prs",
        "--review-requested=@me",
        "--state=open",
        "--json", "number,title,author,repository,updatedAt,url",
    ]) or []
    candidates = []
    for pr in results:
        repo = (pr.get("repository") or {}).get("nameWithOwner")
        number = pr.get("number")
        if not repo or number is None:
            continue
        candidates.append({
            "number": number,
            "title": pr.get("title") or "",
            "author": (pr.get("author") or {}).get("login") or "",
            "repository": repo,
            "updatedAt": pr.get("updatedAt") or "",
            "url": pr.get("url") or "",
        })

    # Enrich each candidate in parallel: fetch_detail + determine_status both make
    # blocking `gh` calls, so a sequential loop is O(N) round trips. The detail cache
    # (_detail_cache/_cache_lock) is thread-safe, so fan out across a small pool.
    def enrich(pr):
        try:
            detail = fetch_detail(pr["repository"], pr["number"], fresh=fresh)
        except Exception as e:
            print(f"[warn] detail fetch failed for {pr['repository']}#{pr['number']}: {e}", flush=True)
            return None
        status = determine_status(pr["repository"], pr["number"], detail, me, fresh)
        if status is None:
            return None
        return {**pr, **status}

    enriched = []
    if candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
            for result in pool.map(enrich, candidates):
                if result is not None:
                    enriched.append(result)

    enriched.sort(key=lambda p: p["updatedAt"])
    enriched.sort(key=lambda p: STATUS_ORDER[p["status"]])
    return enriched


# ---- Review dispatch -------------------------------------------------------

LOG_DIR = "/tmp/pr-reviewer"

_WORKFLOW_DIR = os.path.expanduser("~/.config/pr-dashboard")
REVIEW_WORKFLOW        = os.path.join(_WORKFLOW_DIR, "review_workflow.md")
ADDRESS_WORKFLOW       = os.path.join(_WORKFLOW_DIR, "address_workflow.md")
FIX_PIPELINE_WORKFLOW  = os.path.join(_WORKFLOW_DIR, "fix_pipeline_workflow.md")
REBASE_WORKFLOW        = os.path.join(_WORKFLOW_DIR, "rebase_workflow.md")
RE_REVIEW_WORKFLOW     = os.path.join(_WORKFLOW_DIR, "re_review_workflow.md")

_DEFAULT_REVIEW_WORKFLOW = """\
## Review steps

1. Read the PR title, description, and metadata:
   `gh pr view {number} --repo {repo}`

2. Get the list of changed files and their individual patches (do NOT use `gh pr diff` — it produces one giant file that is too large):
   `gh api repos/{repo}/pulls/{number}/files --paginate`
   This returns JSON. Each entry has: filename, patch, additions, deletions, status.
   Review each file's patch field one at a time.

3. Review the changes for bugs, logic errors, missing edge cases, and style issues.

4. Post inline comments on specific lines where needed:
   `gh api repos/{repo}/pulls/{number}/comments --method POST -f body="..." -f commit_id="<sha from step 1>" -f path="<filename>" -F line=<line number>`

5. Submit your final review:
   - If the code is good: `gh pr review {number} --repo {repo} --approve --body "..."`
   - If changes are needed: `gh pr review {number} --repo {repo} --request-changes --body "..."`
   - If you only want to comment: `gh pr review {number} --repo {repo} --comment --body "..."`

Do not use `gh pr diff`. Do not ask the user any questions. Complete the review autonomously.
"""

_DEFAULT_ADDRESS_WORKFLOW = """\
## Address steps

For each open review thread on this PR:

1. Decide: apply the fix in code, or reply explaining why the change is not appropriate.

2. If fixing:
   - Edit the relevant file.
   - Commit with a clear message.

3. If replying without a code change:
   - `gh api repos/{repo}/pulls/{number}/comments/<comment_id>/replies --method POST -f body="..."`
   - Or: `gh pr comment {number} --repo {repo} --body "..."`

After all threads are addressed:

4. Push: `git push origin {local_branch}:{head_ref}`

5. Re-request review from original reviewers:
   `gh api repos/{repo}/pulls/{number}/requested_reviewers --method POST -f "reviewers[]=<login>"`

Do not ask questions. Do not open new PRs. Only modify files referenced in the comments.
"""

_DEFAULT_FIX_PIPELINE_WORKFLOW = """\
## Fix failing pipeline steps

1. Find the failing checks on this PR:
   `gh pr checks {number} --repo {repo}`

2. Identify the failing workflow run ID from the output and fetch its logs:
   `gh run view <run-id> --repo {repo} --log-failed`

   If the run ID is not obvious from the checks output, list recent runs:
   `gh run list --repo {repo} --branch {head_ref} --limit 5`

3. Read the error output carefully. Identify the root cause (failing test,
   type error, lint violation, build error, etc.).

4. Open the relevant source files and fix the issue. Make the smallest
   change that makes the check pass — do not refactor unrelated code.

5. Commit the fix with a clear message explaining what was broken and why.

6. Push: `git push origin {local_branch}:{head_ref}`

Do not ask questions. Do not open new PRs. Do not modify files unrelated to the failure.
"""

_DEFAULT_REBASE_WORKFLOW = """\
## Rebase steps

1. Fetch the latest from origin:
   `git fetch origin`

2. Rebase onto the base branch:
   `git rebase origin/{base_ref}`

3. If there are conflicts, resolve them:
   - For each conflicted file, open it and resolve the conflict markers.
   - Prefer the intent of this branch's changes — do not silently discard them.
   - Stage resolved files: `git add <file>`
   - Continue: `git rebase --continue`
   - Repeat until no conflicts remain.

4. Push the rebased branch:
   `git push origin {local_branch}:{head_ref} --force-with-lease`

Do not ask questions. Do not open new PRs. Do not squash commits unless explicitly required.
"""

_DEFAULT_RE_REVIEW_WORKFLOW = """\
## Re-review steps

You are re-reviewing this PR. Your job is NOT to do a fresh review — focus exclusively on
whether your previous comments have been addressed, and check what changed since your last review.

1. Fetch your previous review comments to understand what you originally flagged:
   `gh api repos/{repo}/pulls/{number}/comments --paginate`
   Filter for comments where `user.login` is your GitHub login.
   Also check review-level feedback:
   `gh api repos/{repo}/pulls/{number}/reviews --paginate`

2. Find your most recent review's commit SHA from the reviews list (field: `commit_id`).
   Compare what changed since then:
   `gh api "repos/{repo}/pulls/{number}/files" --paginate`
   This gives the full diff. Focus only on files you previously commented on.

3. For each of your original threads:
   - If `resolved: true` — the author addressed it. No action needed.
   - If the relevant code has changed in a way that addresses your concern — approve the thread's intent,
     even if the thread is still technically open.
   - If your concern is unaddressed — note it in your final review body.

4. Submit your verdict:
   - All concerns resolved: `gh pr review {number} --repo {repo} --approve --body "..."`
   - Concerns remain: `gh pr review {number} --repo {repo} --request-changes --body "..."`
   - Progress acknowledged, minor notes: `gh pr review {number} --repo {repo} --comment --body "..."`

Do not leave new inline comments on code unrelated to your original feedback.
Do not re-review files you never commented on in your original review.
Do not ask questions. Complete the re-review autonomously.
"""

DEPLOY_TARGETS_PATH = os.path.join(_WORKFLOW_DIR, "deploy_targets.json")

# Workflow names per repo per environment. Keys are "owner/repo"; values map
# env slug to the exact GitHub Actions workflow name used for dispatch.
_DEFAULT_DEPLOY_TARGETS = {
    "Cognota/cognota-frontend": {
        "csi-1": "CSI 1 Pipeline",
        "csi-2": "CSI 2 Pipeline",
        "csi-3": "CSI 3 Pipeline",
    },
    "Cognota/cognota-be": {
        "csi-1": "CSI-1 Deploy",
        "csi-2": "CSI-2 Deploy",
        "csi-3": "CSI-3 Deploy",
    },
    "Cognota/learnops": {
        "csi-1": "CSI-1 Pipeline",
        "csi-2": "CSI-2 Pipeline",
        "csi-3": "CSI-3 Pipeline",
    },
    "Cognota/learnops-frontend": {
        "csi-1": "CSI1 Pipeline",
        "csi-2": "CSI2 Pipeline",
        "csi-3": "CSI3 Pipeline",
    },
}


def _load_deploy_targets():
    if not os.path.isfile(DEPLOY_TARGETS_PATH):
        return {}
    with open(DEPLOY_TARGETS_PATH) as f:
        return json.load(f)


DEPLOY_TARGETS = _load_deploy_targets()

_jobs = {}  # (repo, number, kind) -> Job
_jobs_lock = threading.Lock()


class Job:
    def __init__(self, repo, number, kind):
        self.repo = repo
        self.number = number
        self.kind = kind  # review | re_review | merge | address | fix_pipeline | rebase
        self.status = "running"  # running | done | failed | stopped
        self.result = None
        self.log = []
        self.subscribers = []
        self.lock = threading.Lock()
        self.start_time = time.time()
        self.log_path = None
        self.proc = None
        self._stop_requested = False

    def stop(self):
        with self.lock:
            self._stop_requested = True
            proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()

    def append(self, text):
        line = {"ts": time.time(), "type": "line", "text": text}
        with self.lock:
            self.log.append(line)
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(line)
            except queue.Full:
                pass

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            for line in self.log:
                q.put_nowait(line)
            self.subscribers.append(q)
            if self.status != "running":
                q.put_nowait({"ts": time.time(), "type": "done",
                              "status": self.status, "result": self.result})
        return q

    def unsubscribe(self, q):
        with self.lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass

    def finish(self, status, result):
        with self.lock:
            self.status = status
            self.result = result
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait({"ts": time.time(), "type": "done",
                              "status": status, "result": result})
            except queue.Full:
                pass


def get_or_create_job(repo, number, kind):
    key = (repo, number, kind)
    with _jobs_lock:
        existing = _jobs.get(key)
        if existing and existing.status == "running":
            return existing, False
        job = Job(repo, number, kind)
        _jobs[key] = job
    return job, True


def format_event(ev):
    """Turn a stream-json event into a one-line human-readable string, or None to skip."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        model = ev.get("model") or "?"
        return f"Started session ({model})"
    if t == "assistant":
        for c in ev.get("message", {}).get("content", []):
            ct = c.get("type")
            if ct == "text":
                txt = (c.get("text") or "").strip()
                if txt:
                    return f"💬 {txt[:240]}"
            elif ct == "tool_use":
                name = c.get("name") or "?"
                inp = c.get("input") or {}
                if name == "Bash":
                    cmd = (inp.get("command") or "").splitlines()[0]
                    return f"$ {cmd[:240]}"
                if name == "Read":
                    return f"Read {inp.get('file_path', '?')}"
                if name == "Edit":
                    return f"Edit {inp.get('file_path', '?')}"
                if name == "Write":
                    return f"Write {inp.get('file_path', '?')}"
                if name == "Grep":
                    return f"Grep {inp.get('pattern', '?')}"
                if name == "Glob":
                    return f"Glob {inp.get('pattern', '?')}"
                if name == "Task":
                    desc = inp.get("description") or inp.get("subagent_type") or "?"
                    return f"→ subagent: {desc}"
                if name == "WebFetch":
                    return f"Fetch {inp.get('url', '?')}"
                return f"→ {name}"
    if t == "user":
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") == "tool_result" and c.get("is_error"):
                content = c.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        x.get("text", "") for x in content if x.get("type") == "text"
                    )
                return f"⚠️ tool error: {str(content)[:200]}"
    return None


_RE_APPROVE = re.compile(r"gh\s+pr\s+review\b[^|;&]*--approve")
_RE_REVIEW_API = re.compile(r"gh\s+api\s+repos/[^\s]+/pulls/\d+/reviews\b")
_RE_COMMENTS_API = re.compile(r"gh\s+api\s+repos/[^\s]+/pulls/\d+/comments\b")


def count_pending_comments(repo, number, me):
    """Sum comments across all of my pending reviews on this PR."""
    try:
        reviews = gh_json([
            "api", f"repos/{repo}/pulls/{number}/reviews", "--paginate",
        ]) or []
    except Exception:
        return 0
    total = 0
    for r in reviews:
        if r.get("state") != "PENDING":
            continue
        if (r.get("user") or {}).get("login") != me:
            continue
        rid = r.get("id")
        try:
            comments = gh_json([
                "api",
                f"repos/{repo}/pulls/{number}/reviews/{rid}/comments",
                "--paginate",
            ]) or []
        except Exception:
            continue
        total += len(comments)
    return total


def derive_result(events, repo, number, me):
    """Look at the tool_use stream + GH state to figure out what claude actually did."""
    approves = 0
    review_calls = 0
    comment_calls = 0
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") != "tool_use" or c.get("name") != "Bash":
                continue
            cmd = (c.get("input") or {}).get("command") or ""
            if _RE_APPROVE.search(cmd):
                approves += 1
            if _RE_REVIEW_API.search(cmd):
                review_calls += 1
            if _RE_COMMENTS_API.search(cmd):
                comment_calls += 1
    if approves > 0:
        return "approved"
    if review_calls > 0 or comment_calls > 0:
        n = count_pending_comments(repo, number, me)
        if n <= 0:
            # fallback: at least one comment per API call observed
            n = max(review_calls, comment_calls)
        return f"commented:{n}"
    return "no_action"


CLAUDE_BASE_ARGS = [
    "--permission-mode", "bypassPermissions",
    "--output-format", "stream-json",
    "--verbose",
]


def _job_log_path(prefix, repo, number):
    """Build (and ensure the dir for) a per-job log path."""
    os.makedirs(LOG_DIR, exist_ok=True)
    return f"{LOG_DIR}/{prefix}{repo_flat(repo)}-{number}-{int(time.time())}.log"


def _fill_refs(text, **refs):
    """Substitute {key} placeholders in a user-editable workflow body.

    Unlike str.format, this only touches the named keys via literal replace, so
    stray braces in the file (JSON snippets, shell ${VAR}, etc.) pass through
    untouched instead of raising KeyError/ValueError and killing the job.
    """
    for key, val in refs.items():
        text = text.replace("{" + key + "}", val)
    return text


def _stream_claude_job(job, prompt, log_path, *, cwd=None, stop_verb="Stopped."):
    """Spawn `claude -p`, stream stdout to the log file and job, handle exit.

    Returns the list of parsed JSON events on success (exit 0). Returns None if
    the job has already been finished here (spawn failure, stream error, stop, or
    non-zero exit) — callers should return immediately without deriving a result.
    """
    events = []
    try:
        proc = subprocess.Popen(
            ["claude", "-p", prompt, *CLAUDE_BASE_ARGS],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
        )
    except Exception as e:
        job.append(f"Failed to spawn claude: {e}")
        job.finish("failed", "spawn_error")
        return None

    job.proc = proc

    try:
        with open(log_path, "w") as logf:
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(ev)
                friendly = format_event(ev)
                if friendly:
                    job.append(friendly)
        proc.wait()
    except Exception as e:
        job.append(f"Stream error: {e}")
        job.finish("failed", "stream_error")
        return None

    if proc.returncode != 0:
        if job._stop_requested:
            job.append(stop_verb)
            job.finish("stopped", "stopped")
        else:
            job.append(f"claude exited with code {proc.returncode}")
            job.finish("failed", f"exit:{proc.returncode}")
        return None

    return events


def _review_result_label(result):
    """Map a derive_result() string to a human label for review/re-review."""
    label = {
        "approved": "Approved PR ✓",
        "no_action": "Finished (no GitHub action taken)",
    }.get(result)
    if label is None and result.startswith("commented:"):
        n = result.split(":", 1)[1]
        label = f"Posted {n} pending comment(s)"
    return label


def _run_review_job(job, log_prefix, workflow_path, prompt_template, log_tag, stop_verb):
    """Shared body for review and re-review (identical except labels/paths)."""
    number, repo = job.number, job.repo
    log_path = _job_log_path(log_prefix, repo, number)
    job.log_path = log_path
    job.append(f"Starting {log_tag} of #{number} in {repo}")
    print(f"[{log_tag}] starting #{number} in {repo} (log: {log_path})", flush=True)

    try:
        workflow = _load_workflow(workflow_path)
    except FileNotFoundError:
        job.append(f"Workflow file not found: {workflow_path}")
        job.append("Use the Status tab to create it.")
        job.finish("failed", "missing_workflow")
        return

    prompt = prompt_template.format(number=number, repo=repo) + workflow
    events = _stream_claude_job(job, prompt, log_path, stop_verb=stop_verb)
    if events is None:
        return

    try:
        me = get_my_login()
    except Exception:
        me = None
    result = derive_result(events, repo, number, me)
    job.append(_review_result_label(result) or f"Finished: {result}")
    job.finish("done", result)
    print(f"[{log_tag}] finished #{number} result={result}", flush=True)


def run_review(job):
    _run_review_job(
        job, "", REVIEW_WORKFLOW, REVIEW_PROMPT,
        log_tag="review", stop_verb="Review stopped.",
    )


def run_re_review(job):
    _run_review_job(
        job, "re-review-", RE_REVIEW_WORKFLOW, RE_REVIEW_PROMPT,
        log_tag="re-review", stop_verb="Re-review stopped.",
    )


# ---- Merge dispatch --------------------------------------------------------

GH_MERGE_METHOD_FLAG = {
    "MERGE": "--merge",
    "SQUASH": "--squash",
    "REBASE": "--rebase",
}


def run_merge(job, default_method):
    """Run `gh pr merge` with the repo's default method. Emulates the
    GitHub Merge button: no --auto, no --delete-branch.
    """
    flag = GH_MERGE_METHOD_FLAG.get(default_method or "MERGE", "--merge")
    job.append(f"Merging #{job.number} in {job.repo} ({flag})")
    print(f"[merge] starting #{job.number} in {job.repo} method={flag}", flush=True)

    proc = subprocess.run(
        ["gh", "pr", "merge", str(job.number),
         "--repo", job.repo, flag],
        capture_output=True, text=True,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            job.append(line)
    if proc.stderr:
        for line in proc.stderr.splitlines():
            job.append(line)

    if proc.returncode == 0:
        job.append("Merged ✓")
        job.finish("done", "merged")
        print(f"[merge] finished #{job.number} merged", flush=True)
    else:
        job.append(f"gh exited with code {proc.returncode}")
        job.finish("failed", f"exit:{proc.returncode}")
        print(f"[merge] finished #{job.number} failed", flush=True)


# ---- Agent-clone management ------------------------------------------------

AGENT_CLONES_DIR = os.path.expanduser("~/.cache/pr-tools/clones")
_repo_locks = {}
_repo_locks_lock = threading.Lock()


def repo_flat(repo_full):
    """owner/repo -> owner_repo."""
    return repo_full.replace("/", "_")


def agent_clone_path(repo_full):
    """Path to the dedicated agent clone for a repo."""
    return os.path.join(AGENT_CLONES_DIR, repo_flat(repo_full))


def get_repo_lock(repo_full):
    """Per-repo mutex so two agent jobs against the same repo serialize."""
    with _repo_locks_lock:
        lock = _repo_locks.get(repo_full)
        if lock is None:
            lock = threading.Lock()
            _repo_locks[repo_full] = lock
        return lock


def prepare_agent_clone(repo_full, head_ref):
    """Ensure ~/.cache/pr-tools/clones/<repo_flat> has the PR head checked out.

    Clones if missing, then fetches origin/<head_ref>, discards local state,
    and force-checks out the branch. Returns (clone_path, branch_name).

    Raises subprocess.CalledProcessError on git failure.
    """
    path = agent_clone_path(repo_full)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(AGENT_CLONES_DIR, exist_ok=True)
        # `gh repo clone` handles auth via the user's gh session.
        subprocess.run(
            ["gh", "repo", "clone", repo_full, path],
            check=True, capture_output=True, text=True,
        )
    subprocess.run(
        ["git", "-C", path, "fetch", "origin", head_ref],
        check=True, capture_output=True, text=True,
    )
    # Discard any leftover state from a prior run before switching branch.
    subprocess.run(
        ["git", "-C", path, "reset", "--hard"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", path, "clean", "-fd"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", path, "checkout", "-B", head_ref, f"origin/{head_ref}"],
        check=True, capture_output=True, text=True,
    )
    return path, head_ref


# ---- Cleanup (stale branches / worktrees) ----------------------------------

def _git_runner(args, cwd):
    """Runner injected into cleanup.py: run `git -C cwd <args>` -> (code, out, err)."""
    proc = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True
    )
    return proc.returncode, proc.stdout, proc.stderr


def cleanup_repo_targets():
    """(path, label, kind) tuples to scan: configured repos + each agent clone."""
    targets = []
    for raw in CLEANUP_REPOS:
        targets.append((os.path.abspath(os.path.expanduser(raw)), raw, "configured"))
    if os.path.isdir(AGENT_CLONES_DIR):
        for name in sorted(os.listdir(AGENT_CLONES_DIR)):
            full = os.path.join(AGENT_CLONES_DIR, name)
            if os.path.isdir(os.path.join(full, ".git")):
                targets.append((full, name, "clone"))
    return targets


def scan_cleanup(fresh=False):
    """Scan all target repos. Returns {"repos": [{path,label,kind,ok,error?,candidates}]}."""
    repos = []
    for path, label, kind in cleanup_repo_targets():
        entry = {"path": path, "label": label, "kind": kind, "ok": True, "candidates": []}
        if not os.path.isdir(os.path.join(path, ".git")):
            entry["ok"] = False
            entry["error"] = "not a git repo"
            repos.append(entry)
            continue
        try:
            if fresh:
                _git_runner(["fetch", "--prune"], path)
            entry["candidates"] = cleanup.scan_repo(
                _git_runner, path, author_email=CLEANUP_AUTHOR_EMAIL or None
            )
        except Exception as e:  # pragma: no cover - defensive
            entry["ok"] = False
            entry["error"] = str(e)
        repos.append(entry)
    return {"repos": repos}


# ---- Address dispatch ------------------------------------------------------

ADDRESS_PROMPT = (
    "Address review comments on PR #{number} in {repo}. "
    "PR head branch on origin: {head_ref}. "
    "Local branch in this worktree: {local_branch}. "
    "Push with: git push origin {local_branch}:{head_ref}\n\n"
)

_RE_GIT_PUSH = re.compile(r"(^|[\s;&|])git\s+push\b")
_RE_INLINE_REPLY = re.compile(r"gh\s+api[^|;&]*\bpulls/\d+/comments/\d+/replies\b")
_RE_GENERAL_PR_COMMENT = re.compile(r"gh\s+pr\s+comment\b")
_RE_RERQUEST = re.compile(r"gh\s+api[^|;&]*\bpulls/\d+/requested_reviewers\b")


def derive_address_result(events):
    pushes = 0
    replies = 0
    rerequests = 0
    slack_dms = 0
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") != "tool_use":
                continue
            tool_name = c.get("name") or ""
            # Slack DMs: any MCP tool ending in slack_send_message (not draft).
            if "slack_send_message" in tool_name and "draft" not in tool_name:
                slack_dms += 1
                continue
            if tool_name != "Bash":
                continue
            cmd = (c.get("input") or {}).get("command") or ""
            if _RE_GIT_PUSH.search(cmd):
                pushes += 1
            if _RE_INLINE_REPLY.search(cmd) or _RE_GENERAL_PR_COMMENT.search(cmd):
                replies += 1
            if _RE_RERQUEST.search(cmd):
                rerequests += 1

    parts = []
    if pushes:
        parts.append(f"Pushed {pushes} commit{'s' if pushes != 1 else ''}")
    if replies:
        parts.append(f"replied to {replies}")
    if rerequests:
        parts.append(f"re-requested {rerequests}")
    if slack_dms:
        parts.append(f"DM'd {slack_dms}")
    if pushes:
        label = ", ".join(parts)
    elif replies:
        label = "Replied only"
    else:
        label = "No action"

    return {
        "pushes": pushes,
        "replies": replies,
        "rerequests": rerequests,
        "slack_dms": slack_dms,
        "label": label,
    }


def _run_worktree_job(job, head_ref, *, log_prefix, workflow_path, log_tag, build_prompt):
    """Shared body for the worktree-based jobs (address / fix-pipeline / rebase).

    Refreshes the per-repo agent clone under its lock, loads the workflow, builds
    the prompt via build_prompt(local_branch, workflow), and streams claude in the
    clone. Returns the parsed events on success, or None if the job was already
    finished here (clone failure, missing workflow, or any _stream_claude_job exit).
    """
    repo, number = job.repo, job.number
    with get_repo_lock(repo):
        try:
            clone_path, local_branch = prepare_agent_clone(repo, head_ref)
        except subprocess.CalledProcessError as e:
            job.append(f"Agent-clone setup failed: {(e.stderr or '').strip()}")
            job.finish("failed", "clone_error")
            return None

        log_path = _job_log_path(log_prefix, repo, number)
        job.log_path = log_path
        job.append(f"Agent clone ready at {clone_path} (branch: {local_branch})")
        print(f"[{log_tag}] starting #{number} in {repo} (clone: {clone_path})", flush=True)

        try:
            workflow = _load_workflow(workflow_path)
        except FileNotFoundError:
            job.append(f"Workflow file not found: {workflow_path}")
            job.append("Use the Status tab to create it.")
            job.finish("failed", "missing_workflow")
            return None

        return _stream_claude_job(
            job, build_prompt(local_branch, workflow), log_path, cwd=clone_path,
        )


def run_address(job, head_ref):
    """Spawn Claude in a worktree to address PR comments."""
    events = _run_worktree_job(
        job, head_ref,
        log_prefix="address-", workflow_path=ADDRESS_WORKFLOW, log_tag="address",
        build_prompt=lambda local_branch, workflow: ADDRESS_PROMPT.format(
            number=job.number, repo=job.repo,
            head_ref=head_ref, local_branch=local_branch,
        ) + workflow,
    )
    if events is None:
        return
    result = derive_address_result(events)
    job.append(result["label"])
    job.finish("done", result["label"])
    print(f"[address] finished #{job.number} result={result['label']}", flush=True)


# ---- Fix-pipeline dispatch -------------------------------------------------

FIX_PIPELINE_PROMPT = (
    "Fix the failing CI pipeline on PR #{number} in {repo}. "
    "PR head branch on origin: {head_ref}. "
    "Local branch in this worktree: {local_branch}. "
    "Push fixes with: git push origin {local_branch}:{head_ref}\n\n"
)


def run_fix_pipeline(job, head_ref: str) -> None:
    """Spawn Claude in a worktree to diagnose and fix failing CI checks."""
    events = _run_worktree_job(
        job, head_ref,
        log_prefix="fix-pipeline-", workflow_path=FIX_PIPELINE_WORKFLOW, log_tag="fix-pipeline",
        build_prompt=lambda local_branch, workflow: FIX_PIPELINE_PROMPT.format(
            number=job.number, repo=job.repo,
            head_ref=head_ref, local_branch=local_branch,
        ) + workflow,
    )
    if events is None:
        return
    job.append("Pipeline fix complete.")
    job.finish("done", "pipeline_fixed")
    print(f"[fix-pipeline] finished #{job.number}", flush=True)


# ---- Nudge dispatch --------------------------------------------------------

NUDGE_PROMPT = (
    "I want to nudge these GitHub reviewers on Slack about my open PR:\n"
    "  PR: {url}\n"
    "  Title: {title}\n"
    "  Reviewers (GitHub logins, Slack @handles, or Slack member IDs): {reviewers}\n"
    "  Mode: {mode}\n"
    "  Channel ID: {channel}\n\n"
    "Mode meanings:\n"
    "  - re_review: I've addressed their previous review comments; "
    "DM each one asking them to take another look.\n"
    "  - fresh: nobody has reviewed this PR yet; "
    "DM each one asking them to review it for the first time.\n"
    "  - channel: post ONE message in the team channel (the Channel ID above) "
    "using `<!here>` (do NOT tag the listed reviewers individually).\n\n"
    "Follow the 'Nudging reviewers on Slack' workflow in your CLAUDE.md "
    "exactly. Pick the message template that matches the mode. Do not deviate."
)


def count_slack_sends(events):
    """Count `slack_send_message` tool calls in a Claude stream (excluding drafts)."""
    sent = 0
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") != "tool_use":
                continue
            name = c.get("name") or ""
            if "slack_send_message" in name and "draft" not in name:
                sent += 1
    return sent


def derive_nudge_result(events, mode="re_review"):
    sent = count_slack_sends(events)
    if sent == 0:
        return {
            "sent": 0,
            "label": "Channel post failed" if mode == "channel" else "No DMs sent",
        }
    if mode == "channel":
        label = "Posted in team channel"
    else:
        label = f"DM'd {sent} reviewer{'s' if sent != 1 else ''}"
    return {"sent": sent, "label": label}


def run_nudge(job, url, title, reviewers, mode, channel_id=None):
    """Spawn Claude to DM the reviewers on Slack via the Slack MCP."""
    if channel_id is None:
        channel_id = TEAM_CHANNEL_ID
    resolved_reviewers = [SLACK_ID_MAP.get(r, r) for r in reviewers]
    log_path = _job_log_path("nudge-", job.repo, job.number)
    job.log_path = log_path
    venue = f"#channel {channel_id}" if mode == "channel" else f"{len(resolved_reviewers)} DM(s)"
    job.append(f"Nudging on Slack ({mode}, {venue}): {', '.join(reviewers)}")
    print(f"[nudge] starting #{job.number} in {job.repo} mode={mode} reviewers={reviewers}", flush=True)
    prompt = NUDGE_PROMPT.format(
        url=url, title=title, reviewers=", ".join(resolved_reviewers),
        mode=mode, channel=channel_id,
    )
    events = _stream_claude_job(job, prompt, log_path, stop_verb="Nudge stopped.")
    if events is None:
        return
    result = derive_nudge_result(events, mode=mode)
    job.append(result["label"])
    job.finish("done", result["label"])
    print(f"[nudge] finished #{job.number} result={result['label']}", flush=True)


# ---- Rebase dispatch -------------------------------------------------------

REBASE_PROMPT = (
    "Rebase PR #{number} in {repo} onto the base branch. "
    "Base branch: {base_ref}. "
    "PR head branch on origin: {head_ref}. "
    "Local branch in this worktree: {local_branch}. "
    "Push with: git push origin {local_branch}:{head_ref} --force-with-lease\n\n"
)


def run_rebase(job, head_ref: str, base_ref: str) -> None:
    """Spawn Claude in a worktree to rebase the PR branch onto its base."""
    events = _run_worktree_job(
        job, head_ref,
        log_prefix="rebase-", workflow_path=REBASE_WORKFLOW, log_tag="rebase",
        build_prompt=lambda local_branch, workflow: REBASE_PROMPT.format(
            number=job.number, repo=job.repo,
            head_ref=head_ref, local_branch=local_branch, base_ref=base_ref,
        ) + _fill_refs(
            workflow, base_ref=base_ref, head_ref=head_ref, local_branch=local_branch,
        ),
    )
    if events is None:
        return
    job.append("Rebase complete.")
    job.finish("done", "rebased")
    print(f"[rebase] finished #{job.number}", flush=True)


def _load_workflow(path):
    """Read a workflow .md file. Raises FileNotFoundError if missing."""
    with open(path) as f:
        return f.read().strip()


# ---- Job POST routing ------------------------------------------------------

def _req_ref(data, key):
    """Pull a required, non-empty ref string from a request body."""
    val = str(data[key])
    if not val:
        raise ValueError(f"{key} required")
    return val


def _extract_merge(d):
    return (str(d.get("defaultMergeMethod") or "MERGE"),)


# path -> (job kind, runner fn, extract-extra-args fn or None).
# The dispatcher (_dispatch_job_post) handles the shared number/repo parse,
# job creation, thread spawn, and 202 envelope; `extract` returns the extra
# positional args each runner needs (and may raise to reject the request).
_JOB_POST_ROUTES = {
    "/api/review":       ("review",       run_review,       None),
    "/api/re-review":    ("re_review",    run_re_review,    None),
    "/api/merge":        ("merge",        run_merge,        _extract_merge),
    "/api/address":      ("address",      run_address,      lambda d: (_req_ref(d, "headRefName"),)),
    "/api/fix-pipeline": ("fix_pipeline", run_fix_pipeline, lambda d: (_req_ref(d, "headRefName"),)),
    "/api/rebase":       ("rebase",       run_rebase,       lambda d: (_req_ref(d, "headRefName"), _req_ref(d, "baseRefName"))),
}


# ---- HTTP server -----------------------------------------------------------



def write_env_var(key, value):
    """Update or append key=value in the .env file and os.environ.

    Holds _env_lock so concurrent settings saves can't interleave their
    read-modify-write and corrupt the file.
    """
    with _env_lock:
        try:
            with open(_ENV_PATH) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        updated = False
        new_lines = []
        for line in lines:
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                new_lines.append(f"{key}={value}\n")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"{key}={value}\n")
        with open(_ENV_PATH, "w") as f:
            f.writelines(new_lines)
        os.environ[key] = value


def get_status():
    """Return a list of status checks for the app configuration."""
    checks = []

    def check(name, description, ok, excerpt="", fix=None):
        checks.append({"name": name, "description": description, "ok": ok, "excerpt": excerpt, "fix": fix})

    def separator():
        checks.append({"separator": True})

    def prompt_excerpt(prompt):
        if not prompt or not prompt.strip():
            return "Not set or empty."
        text = prompt.strip()
        return text[:500] + ("\n… (truncated)" if len(text) > 500 else "")

    def dir_excerpt(path):
        if not os.path.isdir(path):
            return f"Path: {path}\nStatus: does not exist (created automatically on first use)"
        try:
            entries = sorted(os.listdir(path))
            contents = ", ".join(entries[:20]) + ("…" if len(entries) > 20 else "") if entries else "(empty)"
            return f"Path: {path}\nStatus: exists\nContents: {contents}"
        except Exception as e:
            return f"Path: {path}\nError reading directory: {e}"

    def workflow_excerpt(path):
        if not os.path.isfile(path):
            return f"File not found: {path}\nClick 'Create file' to generate it with sensible defaults."
        try:
            with open(path) as f:
                text = f.read().strip()
            return text[:500] + ("\n… (truncated)" if len(text) > 500 else "")
        except Exception as e:
            return f"Error reading {path}: {e}"

    for wf_path, wf_name, wf_label in [
        (REVIEW_WORKFLOW,       "review_workflow.md",       "Review workflow instructions"),
        (RE_REVIEW_WORKFLOW,    "re_review_workflow.md",    "Re-review workflow instructions"),
        (ADDRESS_WORKFLOW,      "address_workflow.md",      "Address workflow instructions"),
        (FIX_PIPELINE_WORKFLOW, "fix_pipeline_workflow.md", "Fix pipeline workflow instructions"),
        (REBASE_WORKFLOW,       "rebase_workflow.md",       "Rebase workflow instructions"),
        (DEPLOY_TARGETS_PATH,   "deploy_targets.json",
         "Deploy targets (repo → env → workflow name)"),
    ]:
        ok = os.path.isfile(wf_path)
        check(
            wf_name, wf_label, ok,
            workflow_excerpt(wf_path),
            fix={"action": "create_file", "path": wf_path} if not ok else None,
        )

    separator()

    check("AGENT_CLONES_DIR", "Agent clones directory",
          os.path.isdir(AGENT_CLONES_DIR),
          dir_excerpt(AGENT_CLONES_DIR),
          fix={"action": "create_dir", "path": AGENT_CLONES_DIR})

    check("LOG_DIR", "Log directory",
          os.path.isdir(LOG_DIR),
          dir_excerpt(LOG_DIR),
          fix={"action": "create_dir", "path": LOG_DIR})

    claude_path = shutil.which("claude")
    check("claude", "claude CLI on PATH",
          claude_path is not None,
          f"Found at: {claude_path}" if claude_path else "Not found on PATH")

    gh_result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    gh_output = (gh_result.stdout + gh_result.stderr).strip()
    check("gh", "gh CLI authenticated",
          gh_result.returncode == 0,
          gh_output[:500] if gh_output else "No output from gh auth status")

    check("DEPLOY_TARGET", "Default deploy environment (.env)",
          bool(DEPLOY_TARGET),
          f"Target: {DEPLOY_TARGET}" if DEPLOY_TARGET else "Not set — add DEPLOY_TARGET=csi-3 to .env to show Deploy buttons",
          fix={"action": "set_env", "key": "DEPLOY_TARGET", "placeholder": "csi-3"})

    check("JIRA", "Jira credentials for the Tickets tab (.env)",
          jira_configured(),
          f"Configured: {JIRA_EMAIL} @ {JIRA_SITE}" if jira_configured()
          else "Not set — add JIRA_SITE, JIRA_EMAIL, and JIRA_API_TOKEN on the Settings tab")

    return checks


def open_in_editor(path: str) -> None:
    """Open *path* in the configured or auto-detected editor.

    If EDITOR_CMD is set, that command is used directly (e.g. "cursor", "code",
    "subl", "open -a TextEdit"). Otherwise: tries ``code``, then falls back to
    the OS default (``open`` on macOS, ``xdg-open`` on Linux).
    """
    import platform
    import shlex
    if EDITOR_CMD:
        cmd = shlex.split(EDITOR_CMD) + [path]
        subprocess.Popen(cmd)
        return
    if shutil.which("code"):
        subprocess.Popen(["code", path])
        return
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", path])
    elif system == "Linux":
        subprocess.Popen(["xdg-open", path])
    else:
        raise RuntimeError(f"No editor found for platform {system!r}")


def list_my_branches(repo: str) -> dict:
    """Return branches whose HEAD commit was authored or committed by the current user.

    Strategy: parallel REST page fetches (page numbers, not cursors) to collect all
    branch names + SHAs in one wave, then parallel GraphQL alias batches to check the
    commit author for each SHA. For a 200-branch repo this is ~1.5s vs ~4s sequential.
    """
    me = get_my_login()
    me_lower = me.lower()
    owner, repo_name = repo.split("/", 1)

    def fetch_page(page: int) -> list:
        try:
            return json.loads(gh_run(["api", f"repos/{repo}/branches?per_page=100&page={page}"]))
        except Exception:
            return []

    # Wave 1: default branch + first page in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        base_fut = pool.submit(
            lambda: json.loads(gh_run(["api", f"repos/{repo}"])).get("default_branch", "main")
        )
        page1_fut = pool.submit(fetch_page, 1)
        base_branch = base_fut.result()
        page1 = page1_fut.result()

    # Wave 2: fetch remaining pages in parallel (page 2 onward) if needed
    all_branches = list(page1)
    if len(page1) == 100:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            # Fetch pages 2-9 all at once; discard empty pages at the tail
            for page in pool.map(fetch_page, range(2, 10)):
                all_branches.extend(page)

    if not all_branches:
        return {"branches": [], "base_branch": base_branch}

    # Wave 3: batch-check commit author via GraphQL aliases (50 per query, parallel)
    # Each alias resolves a commit SHA to its author/committer login in one round trip.
    branch_sha_pairs = [(b["name"], b["commit"]["sha"]) for b in all_branches]

    def check_author_batch(pairs: list) -> list:
        aliases = " ".join(
            f'b{i}:object(expression:"{sha}")'
            f'{{...on Commit{{author{{user{{login}}}}committer{{user{{login}}}}}}}}'
            for i, (_, sha) in enumerate(pairs)
        )
        query = f"query($o:String!,$n:String!){{repository(owner:$o,name:$n){{{aliases}}}}}"
        try:
            data = json.loads(gh_run([
                "api", "graphql",
                "-f", f"query={query}",
                "-f", f"o={owner}",
                "-f", f"n={repo_name}",
            ]))
            repo_data = (data.get("data") or {}).get("repository") or {}
        except Exception:
            return []
        result = []
        for i, (branch_name, _) in enumerate(pairs):
            obj = repo_data.get(f"b{i}") or {}
            a = ((obj.get("author") or {}).get("user") or {}).get("login", "")
            c = ((obj.get("committer") or {}).get("user") or {}).get("login", "")
            if a.lower() == me_lower or c.lower() == me_lower:
                result.append(branch_name)
        return result

    batches = [branch_sha_pairs[i:i + 50] for i in range(0, len(branch_sha_pairs), 50)]
    my_branches: list = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(batches)) as pool:
        futs = [pool.submit(check_author_batch, b) for b in batches]
        for fut in concurrent.futures.as_completed(futs):
            my_branches.extend(fut.result())

    return {"branches": sorted(my_branches), "base_branch": base_branch}


def get_deployed() -> dict:
    """Return the latest workflow run per repo for the configured DEPLOY_TARGET.

    If DEPLOY_TARGET is set (e.g. "csi-3"), only that environment's workflow is
    queried for each repo. If not set, all configured environments are queried.
    The workflow name comes from DEPLOY_TARGETS[repo][env] — the value in
    deploy_targets.json — and is passed to ``gh run list -w`` which accepts
    either a workflow filename or its display name.
    """
    target_env = DEPLOY_TARGET

    if target_env:
        combos = [
            (repo, target_env, envs[target_env])
            for repo, envs in DEPLOY_TARGETS.items()
            if target_env in envs
        ]
    else:
        combos = [
            (repo, env, wf)
            for repo, envs in DEPLOY_TARGETS.items()
            for env, wf in envs.items()
        ]

    if not combos:
        return {"environments": {}, "target_env": target_env}

    def fetch_one(combo: tuple) -> tuple:
        repo, env, workflow_name = combo
        try:
            result = subprocess.run(
                [
                    "gh", "run", "list",
                    "-R", repo,
                    "-w", workflow_name,
                    "--limit", "1",
                    "--json", "status,conclusion,headBranch,createdAt,displayTitle",
                ],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout).strip() or "gh run list failed"
                return repo, env, {"repo": repo, "env": env, "error": err}
            if not result.stdout.strip():
                return repo, env, {"repo": repo, "env": env, "error": "no runs found"}
            runs = json.loads(result.stdout)
            if not runs:
                return repo, env, {"repo": repo, "env": env, "error": "no runs found"}
            run = runs[0]
            return repo, env, {
                "repo": repo,
                "env": env,
                "branch": run.get("headBranch", ""),
                "status": run.get("status", ""),
                "conclusion": run.get("conclusion", ""),
                "createdAt": run.get("createdAt", ""),
                "displayTitle": run.get("displayTitle", ""),
            }
        except Exception as e:
            return repo, env, {"repo": repo, "env": env, "error": str(e)}

    by_env: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for repo, env, data in pool.map(fetch_one, combos):
            by_env.setdefault(env, []).append(data)

    return {"environments": by_env, "target_env": target_env}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected mid-response (e.g. closed a tab); nothing to do.
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/static/"):
            try:
                body, ctype = static_files.serve_asset(parsed.path[len("/static/"):])
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/":
            config_json = json.dumps({
                "deploy_targets": DEPLOY_TARGETS,
                "deploy_target": DEPLOY_TARGET,
                "cache_ttl": CACHE_TTL,
                "host": HOST,
                "port": PORT,
                "workflow_dir": _WORKFLOW_DIR,
                "editor_cmd": EDITOR_CMD,
                "jira_site": JIRA_SITE,
                "jira_email": JIRA_EMAIL,
                # Never echo the token itself to the browser — only whether it is set.
                "jira_token_set": bool(JIRA_API_TOKEN),
                "jira_status_filter": JIRA_STATUS_FILTER,
                "cleanup_repos": CLEANUP_REPOS,
                "cleanup_author_email": CLEANUP_AUTHOR_EMAIL,
                "fresh_reviewers": FRESH_REVIEWERS,
                "team_channel_id": TEAM_CHANNEL_ID,
                "teams": TEAMS,
                "slack_ids": ",".join(f"{k}:{v}" for k, v in SLACK_ID_MAP.items()),
            })
            body = static_files.serve_index(config_json)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/status":
            self._send_json(200, get_status())
            return
        if parsed.path == "/api/branches":
            qs = parse_qs(parsed.query or "")
            repo = qs.get("repo", [""])[0]
            if "/" not in repo:
                self._send_json(400, {"error": "repo required"})
                return
            try:
                self._send_json(200, list_my_branches(repo))
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/deployed":
            try:
                self._send_json(200, get_deployed())
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/prs":
            qs = parse_qs(parsed.query or "")
            fresh = qs.get("fresh", ["0"])[0] in ("1", "true", "yes")
            try:
                prs = list_prs(fresh=fresh)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return
            self._send_json(200, prs)
            return
        if parsed.path == "/api/prs/mine":
            try:
                prs = list_my_prs()
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return
            self._send_json(200, prs)
            return
        if parsed.path == "/api/tickets":
            if not jira_configured():
                self._send_json(200, {"configured": False, "tickets": []})
                return
            try:
                self._send_json(200, {"configured": True, "tickets": jira_search()})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/tickets/transitions":
            if not jira_configured():
                self._send_json(503, {"error": "Jira not configured"})
                return
            qs = parse_qs(parsed.query or "")
            key = qs.get("key", [""])[0]
            if not _JIRA_KEY_RE.match(key):
                self._send_json(400, {"error": "invalid issue key"})
                return
            try:
                self._send_json(200, {"transitions": jira_transitions(key)})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/jira/statuses":
            if not jira_configured():
                self._send_json(503, {"error": "Jira not configured"})
                return
            try:
                self._send_json(200, {"statuses": jira_statuses()})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/cleanup":
            qs = parse_qs(parsed.query or "")
            fresh = qs.get("fresh", ["0"])[0] in ("1", "true", "yes")
            try:
                self._send_json(200, scan_cleanup(fresh=fresh))
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path in ("/api/job/stream", "/api/review/stream"):
            qs = parse_qs(parsed.query or "")
            repo = qs.get("repo", [""])[0]
            try:
                number = int(qs.get("number", ["0"])[0])
            except ValueError:
                self.send_error(400, "bad number")
                return
            kind = qs.get("kind", ["review"])[0]
            if kind not in ("review", "re_review", "merge", "address", "fix_pipeline", "rebase", "nudge"):
                self.send_error(400, "bad kind")
                return
            if "/" not in repo or number <= 0:
                self.send_error(400, "bad params")
                return
            self._stream_job(repo, number, kind)
            return
        self.send_error(404)

    def _stream_job(self, repo, number, kind):
        with _jobs_lock:
            job = _jobs.get((repo, number, kind))
        if not job:
            self._send_json(404, {"error": "no such job"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        q = job.subscribe()
        try:
            while True:
                try:
                    line = q.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(line)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if line.get("type") == "done":
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            job.unsubscribe(q)

    def do_POST(self):
        parsed = urlparse(self.path)
        route = _JOB_POST_ROUTES.get(parsed.path)
        if route:
            self._dispatch_job_post(*route)
            return
        if parsed.path == "/api/nudge":
            self._handle_nudge_post()
            return
        if parsed.path == "/api/job/stop":
            self._handle_stop_post()
            return
        if parsed.path == "/api/status/create-dir":
            self._handle_create_dir_post()
            return
        if parsed.path == "/api/status/set-env":
            self._handle_set_env_post()
            return
        if parsed.path == "/api/status/create-file":
            self._handle_create_file_post()
            return
        if parsed.path == "/api/deploy":
            self._handle_deploy_post()
            return
        if parsed.path == "/api/settings":
            self._handle_settings_post()
            return
        if parsed.path == "/api/tickets/transition":
            self._handle_ticket_transition_post()
            return
        if parsed.path == "/api/cleanup/delete":
            self._handle_cleanup_delete_post()
            return
        if parsed.path == "/api/open-dir":
            self._handle_open_dir_post()
            return
        self.send_error(404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def _dispatch_job_post(self, kind, runner, extract):
        """Shared handler for all job-spawning POST routes (see _JOB_POST_ROUTES).

        Parses the common number/repo fields, runs the route's `extract` for any
        extra runner args (which may raise to reject), creates/dedupes the job,
        spawns the runner thread, and returns the standard 202 envelope.
        """
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            if "/" not in repo:
                raise ValueError("repo must be owner/name")
            extra = extract(data) if extract else ()
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, kind)
        if started:
            threading.Thread(
                target=runner, args=(job, *extra), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": kind,
        })

    def _handle_stop_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            kind = str(data.get("kind") or "review")
            if "/" not in repo:
                raise ValueError("repo must be owner/name")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        with _jobs_lock:
            job = _jobs.get((repo, number, kind))
        if not job or job.status != "running":
            self._send_json(404, {"error": "no running job"})
            return
        job.stop()
        self._send_json(200, {"ok": True})

    def _handle_nudge_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            url = str(data["url"])
            title = str(data.get("title") or "")
            reviewers = data.get("reviewers") or []
            mode = str(data.get("mode") or "re_review")
            channel_id = str(data.get("channel_id") or TEAM_CHANNEL_ID)
            allowed_channels = {TEAM_CHANNEL_ID, *(t["channel_id"] for t in TEAMS)} - {""}
            if mode == "channel" and not channel_id:
                raise ValueError("channel mode requires a channel_id (TEAM_CHANNEL_ID not configured)")
            if channel_id and channel_id not in allowed_channels:
                raise ValueError("channel_id not in allow-list")
            if "/" not in repo:
                raise ValueError("repo must be owner/name")
            if mode not in ("re_review", "fresh", "channel"):
                raise ValueError("mode must be re_review, fresh, or channel")
            reviewers = [str(r) for r in reviewers if r]
            if not reviewers:
                raise ValueError("no reviewers to nudge")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, "nudge")
        if started:
            threading.Thread(
                target=run_nudge, args=(job, url, title, reviewers, mode, channel_id), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": "nudge",
        })

    def _handle_create_dir_post(self):
        try:
            data = self._read_json_body()
            path = str(data["path"])
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        allowed = (AGENT_CLONES_DIR, LOG_DIR)
        if path not in allowed:
            self._send_json(403, {"error": "path not allowed"})
            return
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_set_env_post(self):
        global DEPLOY_TARGET
        try:
            data = self._read_json_body()
            key = str(data["key"])
            value = str(data["value"]).strip()
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if key not in ("DEPLOY_TARGET",):
            self._send_json(403, {"error": "key not allowed"})
            return
        if not value:
            self._send_json(400, {"error": "value required"})
            return
        try:
            with _env_lock:
                write_env_var(key, value)
                if key == "DEPLOY_TARGET":
                    DEPLOY_TARGET = value
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_settings_post(self):
        global DEPLOY_TARGET, CACHE_TTL, EDITOR_CMD
        global JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_STATUS_FILTER
        global CLEANUP_REPOS, CLEANUP_AUTHOR_EMAIL
        global FRESH_REVIEWERS, TEAM_CHANNEL_ID, TEAMS, SLACK_ID_MAP
        _allowed = {"DEPLOY_TARGET",
                    "CACHE_TTL", "HOST", "PORT", "EDITOR_CMD",
                    "JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN",
                    "JIRA_STATUS_FILTER", "CLEANUP_REPOS", "CLEANUP_AUTHOR_EMAIL",
                    "FRESH_REVIEWERS", "TEAM_CHANNEL_ID", "TEAMS", "SLACK_IDS"}
        try:
            data = self._read_json_body()
            settings = data.get("settings", {})
            if not isinstance(settings, dict):
                raise ValueError("settings must be a dict")
        except Exception as e:
            self._send_json(400, {"error": str(e)})
            return
        unknown = set(settings) - _allowed
        if unknown:
            self._send_json(403, {"error": f"unknown keys: {', '.join(sorted(unknown))}"})
            return
        with _env_lock:
            for key, value in settings.items():
                value = str(value).strip()
                if key == "JIRA_SITE":
                    value = _normalize_jira_site(value)
                # These keys may be written empty (clears them); every other key
                # keeps its current value when blank.
                if not value and key not in (
                    "JIRA_STATUS_FILTER", "CLEANUP_REPOS", "CLEANUP_AUTHOR_EMAIL",
                    "FRESH_REVIEWERS", "TEAM_CHANNEL_ID", "TEAMS", "SLACK_IDS"
                ):
                    continue
                try:
                    write_env_var(key, value)
                except Exception as e:
                    self._send_json(500, {"error": f"failed to write {key}: {e}"})
                    return
                if key == "DEPLOY_TARGET":
                    DEPLOY_TARGET = value
                elif key == "CACHE_TTL":
                    try:
                        CACHE_TTL = int(value)
                    except ValueError:
                        pass
                elif key == "EDITOR_CMD":
                    EDITOR_CMD = value
                elif key == "JIRA_SITE":
                    JIRA_SITE = value
                elif key == "JIRA_EMAIL":
                    JIRA_EMAIL = value
                elif key == "JIRA_API_TOKEN":
                    JIRA_API_TOKEN = value
                elif key == "JIRA_STATUS_FILTER":
                    JIRA_STATUS_FILTER = _env_list("JIRA_STATUS_FILTER")
                elif key == "CLEANUP_REPOS":
                    CLEANUP_REPOS = _env_list("CLEANUP_REPOS")
                elif key == "CLEANUP_AUTHOR_EMAIL":
                    CLEANUP_AUTHOR_EMAIL = value
                elif key == "FRESH_REVIEWERS":
                    FRESH_REVIEWERS = _env_list("FRESH_REVIEWERS")
                elif key == "TEAM_CHANNEL_ID":
                    TEAM_CHANNEL_ID = value
                elif key == "TEAMS":
                    TEAMS = _parse_teams()
                elif key == "SLACK_IDS":
                    SLACK_ID_MAP = _parse_slack_ids()
        self._send_json(200, {"ok": True})

    def _handle_open_dir_post(self):
        try:
            data = self._read_json_body()
            path = str(data.get("path") or "")
        except Exception as e:
            self._send_json(400, {"error": str(e)})
            return
        if os.path.realpath(path) != os.path.realpath(_WORKFLOW_DIR):
            self._send_json(403, {"error": "path not allowed"})
            return
        try:
            open_in_editor(path)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_create_file_post(self):
        _defaults = {
            os.path.realpath(REVIEW_WORKFLOW):    _DEFAULT_REVIEW_WORKFLOW,
            os.path.realpath(RE_REVIEW_WORKFLOW): _DEFAULT_RE_REVIEW_WORKFLOW,
            os.path.realpath(ADDRESS_WORKFLOW):   _DEFAULT_ADDRESS_WORKFLOW,
            os.path.realpath(FIX_PIPELINE_WORKFLOW):  _DEFAULT_FIX_PIPELINE_WORKFLOW,
            os.path.realpath(REBASE_WORKFLOW):         _DEFAULT_REBASE_WORKFLOW,
            os.path.realpath(DEPLOY_TARGETS_PATH): json.dumps(
                _DEFAULT_DEPLOY_TARGETS, indent=2
            ) + "\n",
        }
        try:
            data = self._read_json_body()
            path = os.path.realpath(os.path.expanduser(str(data["path"])))
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if path not in _defaults:
            self._send_json(403, {"error": "path not allowed"})
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(_defaults[path])
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_deploy_post(self):
        try:
            data = self._read_json_body()
            repo     = str(data["repo"])
            env      = str(data["env"])
            head_ref = str(data["head_ref"])
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        workflow_name = DEPLOY_TARGETS.get(repo, {}).get(env)
        if not workflow_name:
            self._send_json(400, {"error": f"No workflow configured for {repo} / {env}"})
            return
        result = subprocess.run(
            ["gh", "workflow", "run", workflow_name, "-R", repo, "--ref", head_ref],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip() or "workflow dispatch failed"
            self._send_json(500, {"error": err})
            return
        print(f"[deploy] dispatched '{workflow_name}' on {repo}@{head_ref} for {env}", flush=True)
        self._send_json(200, {"ok": True})

    def _handle_ticket_transition_post(self):
        if not jira_configured():
            self._send_json(503, {"error": "Jira not configured"})
            return
        try:
            data = self._read_json_body()
            key = str(data["key"])
            transition_id = str(data["transitionId"])
            if not _JIRA_KEY_RE.match(key):
                raise ValueError("invalid issue key")
            if not re.match(r"^\d+$", transition_id):
                raise ValueError("invalid transition id")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        try:
            jira_do_transition(key, transition_id)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        print(f"[tickets] transitioned {key} via {transition_id}", flush=True)
        self._send_json(200, {"ok": True})

    def _handle_cleanup_delete_post(self):
        try:
            data = self._read_json_body()
            actions = data.get("actions") or []
            if not isinstance(actions, list):
                raise ValueError("actions must be a list")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        allowed = {path for path, _label, _kind in cleanup_repo_targets()}
        # Per-repo set of (kind, name, worktree_path) the scan actually surfaced.
        # A client may only delete candidates the server itself flagged — this
        # blocks crafted actions (e.g. deleting the default branch, or any branch
        # the scan never offered) and subsumes the default/current-branch guard.
        surfaced = {}

        def _surfaced_for(path):
            if path not in surfaced:
                try:
                    cands = cleanup.scan_repo(_git_runner, path)
                except Exception:
                    cands = []
                surfaced[path] = {
                    (c["kind"], c["name"], c.get("worktree_path") or "") for c in cands
                }
            return surfaced[path]

        results = []
        for action in actions:
            result = dict(action)
            repo_path = str(action.get("repo_path") or "")
            if repo_path not in allowed:
                result.update(ok=False, error="repo not allowed")
                results.append(result)
                continue
            triple = (action.get("kind"), action.get("name"),
                      str(action.get("worktree_path") or ""))
            if triple not in _surfaced_for(repo_path):
                result.update(ok=False, error="not a current cleanup candidate")
                results.append(result)
                continue
            ok, err = cleanup.delete_candidate(_git_runner, action)
            result.update(ok=ok, error=err)
            print(f"[cleanup] {action.get('kind')} {action.get('name')} "
                  f"in {repo_path} -> {'ok' if ok else err}", flush=True)
            results.append(result)
        self._send_json(200, {"results": results})


def main():
    try:
        me = get_my_login()
    except Exception as e:
        print(f"Failed to determine GitHub login via gh: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"PR review dashboard")
    print(f"  reviewer: {me}")
    print(f"  listening: http://{HOST}:{PORT}")
    print(f"  stop: Ctrl+C")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
