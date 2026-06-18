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
# Serializes .env rewrites and the config globals (FRESH_REVIEWERS, DEPLOY_TARGET,
# etc.) that settings POSTs reassign while job threads / status GETs read them.
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

    # Reviewers who left inline threads that I've since addressed (none still
    # active) are waiting to take another look — nudge them for re-review, the
    # same as a stale formal review. A reviewer with a still-active thread is
    # left out: the ball is in my court, not theirs.
    addressed_thread_reviewers = {
        login for login in reviewer_has_any_thread
        if login != me and login not in reviewer_has_active_thread
    }
    stale_reviewers |= addressed_thread_reviewers

    if review_decision == "APPROVED" and not active:
        status = "approved"
    elif active:
        status = "has_comments"
    else:
        status = "not_reviewed_yet"

    # Inline review threads count as a human review even when the formal
    # latestReviews list is empty (e.g. a dismissed/superseded review).
    any_human_review = bool(reviewer_has_any_thread) or any(
        _is_human_author(r.get("author")) for r in latest_reviews
    )
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
NUDGE_WORKFLOW         = os.path.join(_WORKFLOW_DIR, "nudge_workflow.md")
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

_DEFAULT_NUDGE_WORKFLOW = """\
## Nudge steps

1. For each GitHub login in the reviewers list, use the Slack MCP to find their Slack user ID by searching for their display name.

2. Based on mode, send the appropriate message:
   - `fresh`: DM each reviewer asking them to review the PR for the first time.
   - `re_review`: DM each reviewer saying you have addressed their comments and asking them to take another look.
   - `channel`: Post ONE message in the Slack channel tagging all reviewers with @mentions. Do NOT send individual DMs.

3. Keep messages brief and friendly. Always include the PR title and URL.

Do not DM when mode is `channel`. Do not post to channel when mode is `fresh` or `re_review`.
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
        self.kind = kind  # review | re_review | merge | address | fix_pipeline | rebase | nudge
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


# ---- Nudge dispatch --------------------------------------------------------

NUDGE_PROMPT = (
    "I want to nudge these GitHub reviewers on Slack about my open PR:\n"
    "  PR: {url}\n"
    "  Title: {title}\n"
    "  Reviewers (GitHub logins): {reviewers}\n"
    "  Mode: {mode}\n"
    "  Channel ID: {channel}\n\n"
    "Mode meanings:\n"
    "  - re_review: I've addressed their previous review comments; "
    "DM each one asking them to take another look.\n"
    "  - fresh: nobody has reviewed this PR yet; "
    "DM each one asking them to review it for the first time.\n"
    "  - channel: post ONE message in the team channel (the Channel ID above) "
    "tagging the listed reviewers with Slack mentions.\n\n"
)


def _load_workflow(path):
    """Read a workflow .md file. Raises FileNotFoundError if missing."""
    with open(path) as f:
        return f.read().strip()


def derive_nudge_result(events, mode="re_review"):
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


def run_nudge(job, url, title, reviewers, mode):
    """Spawn Claude to DM the reviewers on Slack via the Slack MCP."""
    repo, number = job.repo, job.number
    log_path = _job_log_path("nudge-", repo, number)
    job.log_path = log_path
    venue = (
        f"#channel {TEAM_CHANNEL_ID}" if mode == "channel"
        else f"{len(reviewers)} DM(s)"
    )
    job.append(f"Nudging on Slack ({mode}, {venue}): {', '.join(reviewers)}")
    print(f"[nudge] starting #{number} in {repo} mode={mode} reviewers={reviewers}", flush=True)

    try:
        workflow = _load_workflow(NUDGE_WORKFLOW)
    except FileNotFoundError:
        job.append(f"Nudge workflow file not found: {NUDGE_WORKFLOW}")
        job.append("Use the Status tab to create it.")
        job.finish("failed", "missing_workflow")
        return

    prompt = NUDGE_PROMPT.format(
        url=url, title=title, reviewers=", ".join(reviewers),
        mode=mode, channel=TEAM_CHANNEL_ID,
    ) + "\n" + workflow
    events = _stream_claude_job(job, prompt, log_path)
    if events is None:
        return

    result = derive_nudge_result(events, mode=mode)
    job.append(result["label"])
    job.finish("done", result["label"])
    print(f"[nudge] finished #{number} result={result['label']}", flush=True)


# ---- Job POST routing ------------------------------------------------------

def _req_ref(data, key):
    """Pull a required, non-empty ref string from a request body."""
    val = str(data[key])
    if not val:
        raise ValueError(f"{key} required")
    return val


def _extract_merge(d):
    return (str(d.get("defaultMergeMethod") or "MERGE"),)


def _extract_nudge(d):
    url = str(d["url"])
    title = str(d.get("title") or "")
    reviewers = [str(r) for r in (d.get("reviewers") or []) if r]
    mode = str(d.get("mode") or "re_review")
    if mode not in ("re_review", "fresh", "channel"):
        raise ValueError("mode must be re_review, fresh, or channel")
    if not reviewers:
        raise ValueError("no reviewers to nudge")
    return (url, title, reviewers, mode)


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
    "/api/nudge":        ("nudge",        run_nudge,        _extract_nudge),
}


# ---- HTTP server -----------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PRs awaiting your review</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📋</text></svg>">
<style>
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --green: #238636;
    --green-hover: #2ea043;
    --blue: #2f81f7;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }
  .wrap { max-width: 960px; margin: 0 auto; padding: 32px 20px; }
  header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; }
  h1 { font-size: 20px; margin: 0; font-weight: 600; }
  button.refresh {
    background: var(--card);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
  }
  button.refresh:hover { background: #1c2128; }
  button.refresh:disabled { opacity: 0.6; cursor: wait; }
  .group-header {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    margin: 24px 0 8px;
  }
  .group-header:first-child { margin-top: 0; }
  .ticket-group {
    border-left: 2px solid var(--blue);
    padding-left: 12px;
    margin-bottom: 8px;
  }
  .ticket-header {
    font-size: 13px;
    font-weight: 700;
    color: var(--text);
    margin: 4px 0 6px;
  }
  .ticket-header .ticket-count {
    font-weight: 500;
    font-size: 11px;
    color: var(--muted);
    margin-left: 6px;
  }
  .ticket-header .ticket-link { color: var(--blue); text-decoration: none; }
  .ticket-group .pr:last-child { margin-bottom: 0; }
  .pr {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
    display: flex;
    align-items: flex-start;
    gap: 12px;
    margin-bottom: 8px;
  }
  .pr-main { flex: 1; min-width: 0; }
  .pr-meta { color: var(--muted); font-size: 12px; }
  .pr-title { font-size: 15px; font-weight: 600; margin: 4px 0; }
  .pr-title a { color: var(--text); text-decoration: none; }
  .pr-title a:hover { color: var(--blue); }
  .pr-sub { color: var(--muted); font-size: 12px; }
  .pr-detail { color: var(--muted); font-size: 12px; margin-top: 4px; font-style: italic; }
  .pr-checks { font-size: 12px; margin-top: 4px; display: flex; flex-wrap: wrap; gap: 2px 10px; align-items: baseline; }
  .pr-checks .chk { white-space: normal; }
  .pr-checks .chk-pass { color: var(--muted); }
  .pr-checks .chk-pending { color: #d29922; }
  .pr-checks .chk-fail { color: #f85149; }
  .pr-checks .chk-name { color: inherit; text-decoration: none; border-bottom: 1px dotted currentColor; }
  .pr-checks .chk-name:hover { text-decoration: none; opacity: 0.85; }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-left: 8px;
    vertical-align: middle;
  }
  .badge-re_requested { background: #fb8500; color: #fff; }
  .badge-new_commits { background: #1f6feb; color: #fff; }
  .badge-author_replied { background: #8957e5; color: #fff; }
  .badge-untouched { background: #30363d; color: var(--text); }
  .badge-approved { background: #238636; color: #fff; }
  .badge-has_comments { background: #fb8500; color: #fff; }
  .badge-not_reviewed_yet { background: #30363d; color: var(--text); }
  .badge-warning { background: #7d4e00; color: #f0a93a; border-radius: 4px; padding: 1px 7px; font-size: 11px; font-weight: 600; margin-left: 6px; }
  .badge-new { background: #30363d; color: var(--text); }
  .badge-indeterminate { background: #1f6feb; color: #fff; }
  .badge-meta { background: #21262d; color: #8b949e; border-radius: 4px; padding: 1px 7px; font-size: 11px; font-weight: 600; margin-left: 6px; white-space: nowrap; }
  select.ticket-move { background: var(--card); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; font-size: 13px; }
  .btn-fetch-statuses { background: var(--card); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px; font-size: 13px; cursor: pointer; margin-top: 6px; }
  .btn-fetch-statuses:hover:not(:disabled) { border-color: var(--blue); }
  .status-checks { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 10px; }
  .status-check { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text); }
  .cl-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
  .cl-hint { color: #8b949e; font-size: 13px; }
  .cl-repo { margin-bottom: 18px; }
  .cl-note { color: #8b949e; font-size: 13px; padding: 4px 0 8px; }
  .cl-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; padding: 6px 0; border-bottom: 1px solid var(--border); }
  .cl-main { display: flex; align-items: center; gap: 8px; flex: 1; cursor: pointer; min-width: 0; }
  .cl-name { font-family: ui-monospace, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cl-reason { color: #8b949e; font-size: 12px; white-space: nowrap; }
  .cl-force { display: none; align-items: center; gap: 4px; font-size: 12px; color: #f0a93a; cursor: pointer; white-space: nowrap; }
  .cl-force.cl-force-show { display: inline-flex; }
  .cl-result { font-size: 12px; color: #8b949e; }
  .cl-result:not(:empty) { flex-basis: 100%; }
  .cl-done { opacity: 0.5; }
  .cleanup.mine-only .cl-row[data-mine="0"] { display: none; }
  .cleanup.mine-only .cl-repo:not(:has(.cl-row[data-mine="1"])) { display: none; }
  .btn-merge {
    background: var(--green);
    color: #fff;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
  }
  .btn-merge:hover:not(:disabled) { background: var(--green-hover); }
  .btn-merge:disabled { background: #1c2128; cursor: not-allowed; opacity: 0.7; }
  .btn-merge-blocked { background: #6e2a2a !important; color: #f85149 !important; border: 1px solid #f8514940 !important; }
  .btn-fix-pipeline,
  .btn-rebase,
  .btn-address {
    background: #1f6feb;
    color: #fff;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
  }
  .btn-fix-pipeline:hover:not(:disabled),
  .btn-rebase:hover:not(:disabled),
  .btn-address:hover:not(:disabled) { background: #388bfd; }
  .btn-fix-pipeline:disabled,
  .btn-rebase:disabled,
  .btn-address:disabled { background: #1c2128; cursor: not-allowed; opacity: 0.7; }
  .btn-nudge {
    background: #6e40c9;
    color: #fff;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
  }
  .btn-nudge:hover:not(:disabled) { background: #8957e5; }
  .btn-nudge:disabled { background: #1c2128; cursor: not-allowed; opacity: 0.7; }
  .btn-channel {
    background: #d97706;
    color: #fff;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
  }
  .btn-channel:hover:not(:disabled) { background: #f59e0b; }
  .btn-channel:disabled { background: #1c2128; cursor: not-allowed; opacity: 0.7; }
  .btn-deploy {
    background: #0d1117;
    color: #3fb950;
    border: 1px solid #238636;
    border-radius: 6px;
    padding: 5px 12px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 500;
  }
  .btn-deploy:hover:not(:disabled) { background: #238636; color: #fff; }
  .btn-deploy:disabled { opacity: 0.6; cursor: wait; }
  .btn-deploy-live {
    background: transparent !important;
    color: #3fb950 !important;
    border: 1px solid #238636 !important;
  }
  .btn-deploy-live:hover:not(:disabled) { background: #1a2e1a !important; }
  .review-status.merged { background: rgba(35,134,54,0.15); color: #56d364; border-color: rgba(35,134,54,0.4); }
  .pr-actions { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
  .btn-open {
    color: var(--blue);
    text-decoration: none;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 13px;
    border: 1px solid var(--border);
  }
  .btn-open:hover { background: #1c2128; }
  .btn-review {
    background: var(--green);
    color: #fff;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
  }
  .btn-review:hover:not(:disabled) { background: var(--green-hover); }
  .btn-review:disabled { background: #1c2128; cursor: not-allowed; opacity: 0.7; }
  .btn-re-review {
    background: #b45309;
    color: #fff;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
  }
  .btn-re-review:hover:not(:disabled) { background: #d97706; }
  .btn-re-review:disabled { background: #1c2128; cursor: not-allowed; opacity: 0.7; }
  .empty { text-align: center; color: var(--muted); padding: 48px; font-size: 16px; }
  .toast {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 10px 16px;
    border-radius: 6px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    transition: opacity 0.3s;
  }
  .toast.error { border-color: #f85149; }
  .review-log {
    margin-top: 10px;
    padding: 8px 10px;
    background: #0a0e14;
    border: 1px solid var(--border);
    border-radius: 6px;
    max-height: 240px;
    overflow-y: auto;
    font: 11.5px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    color: #c9d1d9;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .review-log-line { padding: 1px 0; }
  .review-log-line.system { color: var(--muted); }
  .review-status {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 500;
    background: #1c2128;
    color: var(--muted);
    border: 1px solid var(--border);
  }
  .review-status.approved { background: rgba(35,134,54,0.15); color: #56d364; border-color: rgba(35,134,54,0.4); }
  .review-status.commented { background: rgba(47,129,247,0.15); color: #79c0ff; border-color: rgba(47,129,247,0.4); }
  .review-status.failed { background: rgba(248,81,73,0.15); color: #ff7b72; border-color: rgba(248,81,73,0.4); }
  .review-status.running .spinner {
    width: 10px; height: 10px;
    border: 2px solid var(--muted);
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .btn-stop {
    background: #6e7681;
    color: #fff;
    border: none;
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 500;
  }
  .btn-stop:hover { background: #8b949e; }
  .review-status.stopped { background: rgba(110,118,129,0.15); color: #8b949e; border-color: rgba(110,118,129,0.4); }
  .tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
  }
  .tab {
    background: none;
    border: none;
    color: var(--muted);
    padding: 8px 14px;
    font: inherit;
    font-size: 14px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
  }
  .tab.active { color: var(--text); border-bottom-color: var(--blue); }
  .tab:hover:not(.active) { color: var(--text); }
  .settings-form { max-width: 560px; display: flex; flex-direction: column; gap: 20px; padding: 8px 0 24px; }
  .settings-row { display: flex; flex-direction: column; gap: 5px; }
  .settings-label { font-weight: 600; color: #c9d1d9; font-size: 14px; }
  .settings-desc { color: #8b949e; font-size: 12px; line-height: 1.4; }
  .settings-input { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 8px 12px; font-size: 14px; width: 100%; box-sizing: border-box; font-family: inherit; }
  .settings-input:focus { outline: none; border-color: #388bfd; box-shadow: 0 0 0 3px rgba(56,139,253,0.15); }
  .settings-restart { color: #f59e0b; font-size: 11px; margin-top: 2px; }
  .btn-settings-save { background: #238636; color: #fff; border: none; border-radius: 6px; padding: 9px 22px; font-size: 14px; font-weight: 500; cursor: pointer; }
  .btn-settings-save:hover:not(:disabled) { background: #2ea043; }
  .btn-settings-save:disabled { opacity: 0.6; cursor: wait; }
  .status-toolbar { display: flex; justify-content: flex-end; margin-bottom: 12px; }
  .btn-open-editor {
    background: transparent;
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 12px;
    cursor: pointer;
  }
  .btn-open-editor:hover:not(:disabled) { color: var(--text); border-color: #8b949e; }
  .btn-open-editor:disabled { opacity: 0.6; cursor: wait; }
  .status-list { list-style: none; padding: 0; margin: 0; }
  .status-separator {
    border: none;
    border-top: 1px solid var(--border);
    margin: 16px 0;
  }
  .status-item {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 8px;
  }
  .status-item details { width: 100%; }
  .status-item summary {
    display: flex;
    align-items: center;
    gap: 14px;
    cursor: pointer;
    list-style: none;
    outline: none;
    user-select: none;
  }
  .status-item summary::-webkit-details-marker { display: none; }
  .status-icon { font-size: 18px; flex-shrink: 0; }
  .status-name { font-size: 13px; font-weight: 600; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .status-desc { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .status-chevron { margin-left: auto; color: var(--muted); font-size: 11px; transition: transform 0.15s; flex-shrink: 0; }
  .status-item details[open] .status-chevron { transform: rotate(90deg); }
  .status-excerpt {
    margin-top: 10px;
    padding: 8px 10px;
    background: #0a0e14;
    border: 1px solid var(--border);
    border-radius: 6px;
    font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    color: #c9d1d9;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .fix-row { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
  .fix-input {
    flex: 1;
    background: #0a0e14;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 10px;
    color: var(--text);
    font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }
  .btn-fix {
    background: #1f6feb;
    color: #fff;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    white-space: nowrap;
  }
  .btn-fix:hover:not(:disabled) { background: #388bfd; }
  .btn-fix:disabled { opacity: 0.6; cursor: wait; }
  .deployed-section {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 10px;
    overflow: hidden;
  }
  .deployed-env-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    cursor: pointer;
    list-style: none;
    user-select: none;
    outline: none;
  }
  .deployed-env-header::-webkit-details-marker { display: none; }
  .deployed-env-label {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: var(--muted);
  }
  .deployed-env-count {
    font-size: 11px;
    background: #30363d;
    color: var(--muted);
    border-radius: 10px;
    padding: 1px 7px;
    font-weight: 600;
  }
  .deployed-chevron { margin-left: auto; color: var(--muted); font-size: 10px; transition: transform 0.15s; }
  .deployed-section[open] .deployed-chevron { transform: rotate(90deg); }
  .deployed-items { padding: 0 14px 6px; }
  .deployed-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 0;
    border-top: 1px solid var(--border);
    font-size: 13px;
  }
  .deployed-icon { font-size: 15px; flex-shrink: 0; width: 20px; text-align: center; }
  .deployed-repo { font-weight: 600; min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .deployed-branch {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11px;
    background: #1c2433;
    border: 1px solid var(--border);
    padding: 2px 7px;
    border-radius: 4px;
    white-space: nowrap;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .deployed-meta { font-size: 11px; color: var(--muted); white-space: nowrap; }
  .deployed-error { font-size: 11px; color: #f85149; }
  .deployed-deploy-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0 0 8px 30px;
    border-top: none;
  }
  .deployed-branch-select {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 4px;
    padding: 3px 6px;
    font-size: 12px;
    flex: 1;
    min-width: 0;
    max-width: 280px;
  }
  .deployed-branch-select:disabled { opacity: 0.5; }
  .deployed-deploy-btn {
    background: #238636;
    color: #fff;
    border: none;
    border-radius: 4px;
    padding: 4px 12px;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .deployed-deploy-btn:hover:not(:disabled) { background: #2ea043; }
  .deployed-deploy-btn:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 id="pageTitle">📋 PRs awaiting your review</h1>
    <button class="refresh" id="refreshBtn">Refresh</button>
  </header>
  <div class="tabs">
    <button class="tab" data-tab="incoming">Awaiting my review</button>
    <button class="tab" data-tab="mine">My PRs</button>
    <button class="tab" data-tab="tickets">Tickets</button>
    <button class="tab" data-tab="deployed">Deployed</button>
    <button class="tab" data-tab="status">Status</button>
    <button class="tab" data-tab="cleanup">Cleanup</button>
    <button class="tab" data-tab="settings">Settings</button>
  </div>
  <main id="content">Loading…</main>
</div>
<script>
const CONFIG = __PR_DASHBOARD_CONFIG__;
const GROUPS = ['re_requested', 'new_commits', 'author_replied', 'untouched'];
const HEADERS = {
  re_requested: 'Re-review requested',
  new_commits: 'New commits',
  author_replied: 'Author replied',
  untouched: 'New',
};

const TABS = {
  incoming: {
    title: '📋 PRs awaiting your review',
    endpoint: '/api/prs',
    groups: ['re_requested', 'new_commits', 'author_replied', 'untouched'],
    headers: HEADERS,
    render: renderIncomingPR,
  },
  mine: {
    title: '🚀 My open PRs',
    endpoint: '/api/prs/mine',
    groups: ['approved', 'has_comments', 'not_reviewed_yet'],
    headers: {
      approved: 'Approved — ready to merge',
      has_comments: 'Has comments to address',
      not_reviewed_yet: 'Not reviewed yet',
    },
    render: renderMyPR,
    subgroup: ticketKey,
  },
  tickets: {
    title: '🎫 My Jira tickets',
    endpoint: '/api/tickets',
    // groups/headers are set dynamically in load() (one column per status).
    groups: [],
    headers: {},
    groupKey: 'status_label',
    render: renderTicket,
  },
};

const _tab = (new URLSearchParams(location.search)).get('tab');
let currentTab = ['mine', 'deployed', 'status', 'settings', 'tickets', 'cleanup'].includes(_tab) ? _tab : 'incoming';
let deployedState = {};  // environments map from /api/deployed, populated when mine tab loads

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Only allow http(s) in hrefs. escapeHtml does NOT stop a javascript:/data: scheme,
// and check URLs come from GitHub commit-status targetUrl (settable by CI/collaborators).
function safeUrl(u) {
  try {
    const p = new URL(u);
    return (p.protocol === 'https:' || p.protocol === 'http:') ? u : '';
  } catch { return ''; }
}

function relativeTime(iso) {
  const t = new Date(iso).getTime();
  if (!t) return '';
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  if (diff < 604800) return Math.floor(diff/86400) + 'd ago';
  return new Date(iso).toLocaleDateString();
}

// Extract a Jira ticket slug (e.g. "CGP-96") so PRs for the same ticket can be
// grouped. The slug lives in the branch name by convention; fall back to the
// title. Returns '' when no ticket is present so such PRs render ungrouped.
function ticketKey(p) {
  const m = /\b([A-Z][A-Z0-9]+-\d+)\b/.exec(`${p.headRefName || ''} ${p.title || ''}`);
  return m ? m[1] : '';
}

function render(prs) {
  const content = document.getElementById('content');
  const tab = TABS[currentTab];
  if (!prs.length) {
    const emptyMsg = currentTab === 'mine' ? '🎉 No open PRs.'
      : currentTab === 'tickets' ? '🎉 No open tickets.'
      : '🎉 No PRs waiting. Inbox zero.';
    content.innerHTML = '<div class="empty">' + emptyMsg + '</div>';
    return;
  }
  const gk = tab.groupKey || 'status';
  const grouped = {};
  for (const g of tab.groups) grouped[g] = [];
  for (const p of prs) {
    if (grouped[p[gk]]) grouped[p[gk]].push(p);
  }
  let html = '';
  for (const g of tab.groups) {
    if (!grouped[g].length) continue;
    html += `<div class="group-header">${escapeHtml(tab.headers[g])}</div>`;
    html += renderGroupBody(grouped[g], tab);
  }
  content.innerHTML = html;
  // PR-card buttons are handled by the delegated #content listener (see initDelegation).
}

// Render the cards within one status group. When the tab opts into subgrouping
// (tab.subgroup), PRs sharing a key (e.g. a Jira ticket) are clustered under a
// sub-header. Clusters appear at the position of their first member, preserving
// the backend ordering; singletons and keyless PRs render inline as before.
function renderGroupBody(prs, tab) {
  if (!tab.subgroup) return prs.map(tab.render).join('');
  const counts = {};
  for (const p of prs) {
    const k = tab.subgroup(p);
    if (k) counts[k] = (counts[k] || 0) + 1;
  }
  const rendered = new Set();
  let html = '';
  for (const p of prs) {
    const k = tab.subgroup(p);
    if (!k || counts[k] < 2) { html += tab.render(p); continue; }
    if (rendered.has(k)) continue;
    rendered.add(k);
    const members = prs.filter(q => tab.subgroup(q) === k);
    html += `<div class="ticket-group">`
      + `<div class="ticket-header">${escapeHtml(k)}${ticketLink(k)} <span class="ticket-count">${members.length} PRs</span></div>`
      + members.map(tab.render).join('')
      + `</div>`;
  }
  return html;
}

// Link a Jira ticket key to its issue when a Jira site is configured.
function ticketLink(key) {
  const site = CONFIG.jira_site || '';
  if (!site) return '';
  const url = safeUrl(`https://${site}/browse/${key}`);
  return url ? ` <a class="ticket-link" href="${escapeHtml(url)}" target="_blank" rel="noopener">↗</a>` : '';
}

function renderIncomingPR(p) {
  const detail = p.status_detail
    ? `<div class="pr-detail">${escapeHtml(p.status_detail)}</div>` : '';
  const isReReview = ['re_requested', 'new_commits', 'author_replied'].includes(p.status);
  const actionBtn = isReReview
    ? `<button class="btn-re-review" type="button" title="Check whether your previous comments were addressed">Re-review</button>`
    : `<button class="btn-review" type="button">Review</button>`;
  return `
  <div class="pr" data-number="${p.number}" data-repo="${escapeHtml(p.repository)}" data-url="${escapeHtml(p.url)}">
    <div class="pr-main">
      <div class="pr-meta">${escapeHtml(p.repository)} · #${p.number}<span class="badge badge-${p.status}">${escapeHtml(p.status_label)}</span></div>
      <div class="pr-title"><a href="${escapeHtml(safeUrl(p.url))}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a></div>
      <div class="pr-sub">by ${escapeHtml(p.author)} · updated ${relativeTime(p.updatedAt)}</div>
      ${detail}
    </div>
    <div class="pr-actions">
      <a class="btn-open" href="${escapeHtml(safeUrl(p.url))}" target="_blank" rel="noopener">Open ↗</a>
      ${actionBtn}
    </div>
  </div>`;
}

// Compact per-check summary line: greens collapse to a count; pending/failed
// checks are named and linked to their logs. Returns '' when there are no checks.
function renderChecks(p) {
  const c = p.checks;
  if (!c) return '';
  const total = c.passed + (c.pending || []).length + (c.failed || []).length;
  if (!total) return '';
  const linkNames = (list) => list.map(ck => {
    const href = safeUrl(ck.url);
    return href
      ? `<a class="chk-name" href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(ck.name)}</a>`
      : `<span class="chk-name">${escapeHtml(ck.name)}</span>`;
  }).join(', ');
  const parts = [];
  if (c.passed) parts.push(`<span class="chk chk-pass">✅ ${c.passed}</span>`);
  if ((c.pending || []).length) parts.push(`<span class="chk chk-pending">🔄 ${linkNames(c.pending)}</span>`);
  if ((c.failed || []).length) parts.push(`<span class="chk chk-fail">❌ ${linkNames(c.failed)}</span>`);
  if (c.truncated) parts.push(`<span class="chk">…</span>`);
  return `<div class="pr-checks">${parts.join(' · ')}</div>`;
}

function renderMyPR(p) {
  const commenters = p.active_commenters && p.active_commenters.length
    ? `<div class="pr-detail">From: ${escapeHtml(p.active_commenters.join(', '))}</div>`
    : '';
  const targets = (p.nudge_targets || []).join(',');
  const mode = p.nudge_mode || '';
  const needsRebase = p.merge_state_status === 'BEHIND';
  const hasConflicts = p.merge_state_status === 'DIRTY';
  const ciBlocked = ['FAILURE', 'ERROR'].includes(p.check_state);
  const reviewBlocked = p.review_decision === 'CHANGES_REQUESTED';

  // The infra-fix button surfaces whenever the branch is behind/conflicted or CI
  // is failing — independent of review status, so a PR that also has comments
  // still offers a way to fix the pipeline. Rebase takes precedence: a behind or
  // conflicted branch must be rebased before its CI result means anything.
  let infraFixBtn = '';
  if (needsRebase || hasConflicts) {
    const why = needsRebase ? 'Needs rebase' : 'Has conflicts';
    infraFixBtn = `<button class="btn-rebase" type="button" title="Fix: ${escapeHtml(why)}">Rebase</button>`;
  } else if (ciBlocked) {
    infraFixBtn = `<button class="btn-fix-pipeline" type="button" title="Fix: CI failing">Fix pipeline</button>`;
  }

  let actionBtn = '';
  if (p.status === 'approved') {
    const blocked = ciBlocked || reviewBlocked || needsRebase || hasConflicts;
    const reasons = [
      ciBlocked && 'CI failing',
      reviewBlocked && 'Changes requested',
      needsRebase && 'Needs rebase',
      hasConflicts && 'Has conflicts',
    ].filter(Boolean);
    const blockReason = reasons.join(' · ');
    const mergeBtn = `<button class="btn-merge${blocked ? ' btn-merge-blocked' : ''}" type="button" ${blocked ? `disabled title="${escapeHtml(blockReason)}"` : ''}>Merge</button>`;
    actionBtn = infraFixBtn + mergeBtn;
  } else if (p.status === 'has_comments') {
    actionBtn = infraFixBtn + `<button class="btn-address" type="button">Address</button>`;
  } else {
    // not_reviewed_yet etc. — still expose an infra fix if the pipeline/branch is broken.
    actionBtn = infraFixBtn;
  }
  const nudgeTargetNames = (p.nudge_targets || []).join(' and ') || 'reviewers';
  const channelTargetNames = (CONFIG.fresh_reviewers || []).join(' and ') || 'reviewers';
  const nudgeTitle = mode === 'fresh'
    ? `DM ${nudgeTargetNames} to ask for first review`
    : (mode === 're_review'
        ? `DM ${nudgeTargetNames} asking them to take another look`
        : 'No one to nudge');
  const nudgeBtn = `<button class="btn-nudge" type="button" title="${escapeHtml(nudgeTitle)}">Nudge</button>`;
  const channelBtn = `<button class="btn-channel" type="button" title="Post in team channel tagging ${escapeHtml(channelTargetNames)}">#Channel</button>`;
  const deployTarget = CONFIG.deploy_target || '';
  const deployWorkflow = deployTarget && (CONFIG.deploy_targets || {})[p.repository]?.[deployTarget];
  const alreadyDeployed = deployTarget && (deployedState[deployTarget] || [])
    .some(d => d.repo === p.repository && d.branch === p.headRefName && d.conclusion === 'success');
  const deployControls = deployWorkflow
    ? (alreadyDeployed
        ? `<button class="btn-deploy btn-deploy-live" type="button" data-env="${escapeHtml(deployTarget)}" title="Branch ${escapeHtml(p.headRefName)} is live — click to re-deploy">✅ ${escapeHtml(deployTarget.toUpperCase())}</button>`
        : `<button class="btn-deploy" type="button" data-env="${escapeHtml(deployTarget)}">Deploy to ${escapeHtml(deployTarget.toUpperCase())}</button>`)
    : '';
  return `
  <div class="pr"
       data-number="${p.number}"
       data-repo="${escapeHtml(p.repository)}"
       data-url="${escapeHtml(p.url)}"
       data-title="${escapeHtml(p.title)}"
       data-head="${escapeHtml(p.headRefName)}"
       data-base="${escapeHtml(p.baseRefName)}"
       data-method="${escapeHtml(p.defaultMergeMethod)}"
       data-targets="${escapeHtml(targets)}"
       data-mode="${escapeHtml(mode)}">
    <div class="pr-main">
      <div class="pr-meta">${escapeHtml(p.repository)} · #${p.number}<span class="badge badge-${p.status}">${escapeHtml(p.status_label)}</span>${needsRebase ? '<span class="badge-warning">⚠ Needs rebase</span>' : hasConflicts ? '<span class="badge-warning">⚠ Has conflicts</span>' : ''}</div>
      <div class="pr-title"><a href="${escapeHtml(safeUrl(p.url))}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a></div>
      <div class="pr-sub">updated ${relativeTime(p.updatedAt)}</div>
      ${renderChecks(p)}
      ${commenters}
    </div>
    <div class="pr-actions">
      <a class="btn-open" href="${escapeHtml(safeUrl(p.url))}" target="_blank" rel="noopener">Open ↗</a>
      ${deployControls}
      ${channelBtn}
      ${nudgeBtn}
      ${actionBtn}
    </div>
  </div>`;
}

// Distinct status names present, ordered by category (To Do before In Progress)
// then alphabetically — used as the Tickets columns when no filter is saved.
function ticketStatusOrder(tickets) {
  const catByName = {};
  for (const t of tickets) catByName[t.status_label] = t.status_category;
  const rank = { new: 0, indeterminate: 1 };
  return Object.keys(catByName).sort((a, b) =>
    ((rank[catByName[a]] ?? 2) - (rank[catByName[b]] ?? 2)) || a.localeCompare(b));
}

function renderTicket(t) {
  const priority = t.priority ? `<span class="badge-meta">${escapeHtml(t.priority)}</span>` : '';
  const type = t.type ? `<span class="badge-meta">${escapeHtml(t.type)}</span>` : '';
  return `
  <div class="pr" data-key="${escapeHtml(t.key)}" data-url="${escapeHtml(t.url)}">
    <div class="pr-main">
      <div class="pr-meta">${escapeHtml(t.key)}<span class="badge badge-${escapeHtml(t.status_category)}">${escapeHtml(t.status_label)}</span>${type}${priority}</div>
      <div class="pr-title"><a href="${escapeHtml(safeUrl(t.url))}" target="_blank" rel="noopener">${escapeHtml(t.summary)}</a></div>
      <div class="pr-sub">updated ${relativeTime(t.updatedAt)}</div>
    </div>
    <div class="pr-actions">
      <a class="btn-open" href="${escapeHtml(safeUrl(t.url))}" target="_blank" rel="noopener">Open ↗</a>
      <button class="btn-move" type="button" title="Move to another status">Move ▾</button>
    </div>
  </div>`;
}

// ---- PR-card action handlers ----------------------------------------------
// Buttons are wired via one delegated listener on #content (see initDelegation),
// so every handler receives the clicked button and re-rendered buttons (e.g. the
// "…again" buttons in stopped branches) work without re-attaching listeners.

function cardCtx(btn) {
  const card = btn.closest('.pr');
  return {
    card,
    number: parseInt(card.dataset.number, 10),
    repo: card.dataset.repo,
    url: card.dataset.url,
  };
}

// POST to `endpoint`; on success switch the card to its running state and stream.
// Shows a toast and bails on failure. Shared by every job-starting handler.
async function startJob(ctx, endpoint, body, kind, runningLabel, finish) {
  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      throw new Error(b.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(ctx.card, runningLabel, kind);
  streamJob(ctx.card, kind, ctx.repo, ctx.number, ctx.url, finish);
}

function onReview(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/review', { number: ctx.number, repo: ctx.repo },
    'review', 'Reviewing…', finishReview);
}

function onReReview(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/re-review', { number: ctx.number, repo: ctx.repo },
    're_review', 'Re-reviewing…', finishReReview);
}

function onMerge(btn) {
  const ctx = cardCtx(btn);
  if (!confirm(`Merge ${ctx.repo} #${ctx.number}?`)) return;
  startJob(ctx, '/api/merge',
    { number: ctx.number, repo: ctx.repo, defaultMergeMethod: ctx.card.dataset.method },
    'merge', 'Merging…', finishMerge);
}

function onAddress(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/address',
    { number: ctx.number, repo: ctx.repo, headRefName: ctx.card.dataset.head },
    'address', 'Addressing…', finishAddress);
}

function onFixPipeline(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/fix-pipeline',
    { number: ctx.number, repo: ctx.repo, headRefName: ctx.card.dataset.head },
    'fix_pipeline', 'Fixing pipeline…', finishFixPipeline);
}

function onRebase(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/rebase',
    { number: ctx.number, repo: ctx.repo,
      headRefName: ctx.card.dataset.head, baseRefName: ctx.card.dataset.base },
    'rebase', 'Rebasing…', finishRebase);
}

function onNudge(btn) {
  const ctx = cardCtx(btn);
  const title = ctx.card.dataset.title || '';
  const mode = ctx.card.dataset.mode || '';
  const targets = (ctx.card.dataset.targets || '').split(',').map(s => s.trim()).filter(Boolean);
  if (!mode || !targets.length) {
    toast('No one to nudge — everyone has approved already.');
    return;
  }
  const promptLabel = mode === 'fresh'
    ? `Ask ${targets.join(' and ')} on Slack to review this PR?`
    : `Nudge on Slack to re-review: ${targets.join(', ')}?`;
  if (!confirm(promptLabel)) return;
  startJob(ctx, '/api/nudge',
    { number: ctx.number, repo: ctx.repo, url: ctx.url, title, reviewers: targets, mode },
    'nudge', 'Nudging…', finishNudge);
}

function onChannelPing(btn) {
  const ctx = cardCtx(btn);
  const title = ctx.card.dataset.title || '';
  const targets = (CONFIG.fresh_reviewers || []).slice();
  if (!targets.length) {
    toast('No FRESH_REVIEWERS configured — set them in .env.', true);
    return;
  }
  if (!CONFIG.team_channel_id) {
    toast('No TEAM_CHANNEL_ID configured — set it in .env.', true);
    return;
  }
  if (!confirm(`Post in team channel tagging ${targets.join(' and ')} to review this PR?`)) return;
  startJob(ctx, '/api/nudge',
    { number: ctx.number, repo: ctx.repo, url: ctx.url, title, reviewers: targets, mode: 'channel' },
    'nudge', 'Posting in channel…', finishNudge);
}

async function onDeploy(btn) {
  const card = btn.closest('.pr');
  const repo = card.dataset.repo;
  const headRef = card.dataset.head;
  const env = btn.dataset.env;

  if (!confirm(`Deploy ${repo} (${headRef}) to ${env.toUpperCase()}?`)) return;

  btn.disabled = true;
  btn.textContent = 'Dispatching…';
  try {
    const res = await fetch('/api/deploy', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ repo, env, head_ref: headRef }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
    btn.textContent = 'Dispatched ✓';
    btn.style.cssText = 'background:#238636;color:#fff;';
    setTimeout(() => { btn.textContent = 'Deploy'; btn.style.cssText = ''; btn.disabled = false; }, 4000);
  } catch (e) {
    toast(`Deploy failed: ${e.message}`, true);
    btn.textContent = 'Deploy';
    btn.disabled = false;
  }
}

// ---- card terminal-state rendering ----------------------------------------

// Final (done/failed) state: a status pill + Open-PR link.
function setFinalStatus(card, cls, label, url) {
  card.querySelector('.pr-actions').innerHTML =
    `<span class="review-status ${cls}">${escapeHtml(label)}</span>`
    + `<a class="btn-open" href="${escapeHtml(safeUrl(url))}" target="_blank" rel="noopener">Open PR ↗</a>`;
}

// Stopped state: pill + optional "…again" button (delegation re-wires it) + Open-PR.
function setStoppedStatus(card, url, againBtn) {
  card.querySelector('.pr-actions').innerHTML =
    `<span class="review-status stopped">⏹ Stopped</span>`
    + (againBtn || '')
    + `<a class="btn-open" href="${escapeHtml(safeUrl(url))}" target="_blank" rel="noopener">Open PR ↗</a>`;
}

function finishMerge(card, url, data) {
  const ok = data.status === 'done' && data.result === 'merged';
  setFinalStatus(card, ok ? 'merged' : 'failed', ok ? '✅ Merged' : '❌ Merge failed', url);
}

function finishAddress(card, url, data) {
  if (data.status === 'stopped') {
    setStoppedStatus(card, url, `<button class="btn-address" type="button">Address again</button>`);
    return;
  }
  let cls = 'failed', label = '❌ Failed';
  if (data.status === 'done') {
    if (data.result === 'No action') { cls = 'commented'; label = 'ℹ No action'; }
    else if (data.result === 'Replied only') { cls = 'commented'; label = '💬 Replied only'; }
    else { cls = 'approved'; label = '✅ ' + data.result; }
  }
  setFinalStatus(card, cls, label, url);
}

function finishFixPipeline(card, url, data) {
  if (data.status === 'stopped') { setStoppedStatus(card, url); return; }
  const ok = data.status === 'done';
  setFinalStatus(card, ok ? 'approved' : 'failed', ok ? '✅ Fix pushed' : '❌ Failed', url);
}

function finishRebase(card, url, data) {
  if (data.status === 'stopped') { setStoppedStatus(card, url); return; }
  const ok = data.status === 'done';
  setFinalStatus(card, ok ? 'approved' : 'failed', ok ? '✅ Rebased & pushed' : '❌ Rebase failed', url);
}

function finishNudge(card, url, data) {
  if (data.status === 'stopped') {
    setStoppedStatus(card, url, `<button class="btn-nudge" type="button">Nudge again</button>`);
    return;
  }
  let cls = 'failed', label = '❌ Failed';
  if (data.status === 'done') {
    if (data.result === 'No DMs sent' || data.result === 'Channel post failed') {
      cls = 'commented'; label = 'ℹ ' + data.result;
    } else { cls = 'approved'; label = '✅ ' + data.result; }
  }
  setFinalStatus(card, cls, label, url);
}

// review and re-review share identical terminal logic (only the "again" button differs).
function finishReviewLike(card, url, data, againBtn) {
  if (data.status === 'stopped') {
    setStoppedStatus(card, url, againBtn);
    return;
  }
  let cls = 'failed', label = '❌ Failed';
  if (data.status === 'done') {
    if (data.result === 'approved') {
      cls = 'approved'; label = '✅ Approved';
    } else if ((data.result || '').startsWith('commented:')) {
      const n = data.result.split(':')[1];
      cls = 'commented';
      label = `💬 ${n} pending comment${n === '1' ? '' : 's'} left`;
    } else {
      cls = 'commented'; label = 'ℹ Done';
    }
  }
  setFinalStatus(card, cls, label, url);
}

function finishReview(card, url, data) {
  finishReviewLike(card, url, data, `<button class="btn-review" type="button">Review again</button>`);
}

function finishReReview(card, url, data) {
  finishReviewLike(card, url, data, `<button class="btn-re-review" type="button">Re-review again</button>`);
}

function setRunning(card, label, kind) {
  const main = card.querySelector('.pr-main');
  const actions = card.querySelector('.pr-actions');
  let panel = main.querySelector('.review-log');
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'review-log';
    main.appendChild(panel);
  } else {
    panel.innerHTML = '';
  }
  // label and kind are client-supplied literals; escaped defensively before insertion.
  // The Stop button is wired via the delegated #content listener.
  const stopBtn = kind
    ? `<button class="btn-stop" data-kind="${escapeHtml(kind)}">Stop</button>`
    : '';
  actions.innerHTML = `<span class="review-status running"><span class="spinner"></span>${escapeHtml(label)}</span>${stopBtn}`;
}

function appendLogLine(card, text, cls) {
  const panel = card.querySelector('.review-log');
  if (!panel) return;
  const line = document.createElement('div');
  line.className = 'review-log-line' + (cls ? ' ' + cls : '');
  line.textContent = text;
  panel.appendChild(line);
  panel.scrollTop = panel.scrollHeight;
}

function streamJob(card, kind, repo, number, url, finishLabel) {
  const params = new URLSearchParams({ kind, repo, number: String(number) });
  const es = new EventSource(`/api/job/stream?${params}`);
  es.addEventListener('message', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    if (data.type === 'done') {
      es.close();
      finishLabel(card, url, data);
      return;
    }
    if (data.type === 'line' && data.text) {
      appendLogLine(card, data.text);
    }
  });
  es.addEventListener('error', () => {
    es.close();
    const actions = card.querySelector('.pr-actions');
    if (actions && !actions.querySelector('.btn-open')) {
      setFinalStatus(card, 'failed', '⚠ Stream lost', url);
    }
  });
}

async function onStop(btn) {
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const kind = btn.dataset.kind;
  btn.disabled = true;
  btn.textContent = 'Stopping…';
  try {
    await fetch('/api/job/stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, kind }),
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Stop';
  }
}

// Tickets: fetch the issue's available transitions, swap the Move button for a
// <select>, and POST the chosen transition. Refreshes the list on success.
async function onMove(btn) {
  const card = btn.closest('.pr');
  const key = card.dataset.key;
  btn.disabled = true;
  let transitions;
  try {
    const res = await fetch('/api/tickets/transitions?key=' + encodeURIComponent(key));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    transitions = (await res.json()).transitions || [];
  } catch (e) {
    toast('Failed to load transitions: ' + e.message, true);
    btn.disabled = false;
    return;
  }
  if (!transitions.length) {
    toast('No transitions available', true);
    btn.disabled = false;
    return;
  }
  const sel = document.createElement('select');
  sel.className = 'ticket-move';
  sel.innerHTML = '<option value="" disabled selected>Move to…</option>'
    + transitions.map(t => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)}</option>`).join('');
  let submitting = false;
  sel.addEventListener('change', async () => {
    const transitionId = sel.value;
    if (!transitionId || submitting) return;
    submitting = true;
    sel.disabled = true;
    try {
      const res = await fetch('/api/tickets/transition', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ key, transitionId }),
      });
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new Error(b.error || ('HTTP ' + res.status));
      }
    } catch (e) {
      toast('Transition failed: ' + e.message, true);
      submitting = false;
      sel.disabled = false;
      return;
    }
    toast('Moved ' + key);
    load(true);
  });
  btn.replaceWith(sel);
}

// One delegated click listener for all PR-card buttons (attached once to #content).
// Class tokens are exact-match, so .btn-review and .btn-re-review never collide.
const CARD_ACTIONS = {
  'btn-review': onReview,
  'btn-re-review': onReReview,
  'btn-merge': onMerge,
  'btn-address': onAddress,
  'btn-fix-pipeline': onFixPipeline,
  'btn-rebase': onRebase,
  'btn-nudge': onNudge,
  'btn-channel': onChannelPing,
  'btn-deploy': onDeploy,
  'btn-stop': onStop,
  'btn-move': onMove,
};

function onContentClick(ev) {
  for (const cls in CARD_ACTIONS) {
    const btn = ev.target.closest('.' + cls);
    if (btn) { CARD_ACTIONS[cls](btn); return; }
  }
}

function toast(msg, error) {
  const el = document.createElement('div');
  el.className = 'toast' + (error ? ' error' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}

const TAB_TITLES = { incoming: '📋 PRs awaiting your review', mine: '🚀 My open PRs', deployed: '🚢 Currently deployed', status: '⚙️ App status', settings: '⚙️ Settings', tickets: '🎫 My Jira tickets', cleanup: '🧹 Branch cleanup' };

function setActiveTab(tab) {
  currentTab = tab;
  for (const el of document.querySelectorAll('.tab')) {
    el.classList.toggle('active', el.dataset.tab === tab);
  }
  document.getElementById('pageTitle').textContent = TAB_TITLES[tab] || '';
  const url = new URL(location.href);
  if (tab === 'incoming') url.searchParams.delete('tab');
  else url.searchParams.set('tab', tab);
  history.replaceState({}, '', url);
}

async function onOpenEditor() {
  const btn = document.getElementById('openEditorBtn');
  btn.disabled = true;
  btn.textContent = 'Opening…';
  try {
    const res = await fetch('/api/open-dir', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path: CONFIG.workflow_dir }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || 'HTTP ' + res.status);
    btn.textContent = 'Opened ✓';
    setTimeout(() => { btn.textContent = 'Open config folder ↗'; btn.disabled = false; }, 2000);
  } catch (e) {
    toast(`Failed to open: ${e.message}`, true);
    btn.textContent = 'Open config folder ↗';
    btn.disabled = false;
  }
}

function renderStatus(checks) {
  const content = document.getElementById('content');
  const items = checks.map(c => {
    if (c.separator) return '<li role="separator" class="status-separator"></li>';
    const excerpt = c.excerpt
      ? `<div class="status-excerpt">${escapeHtml(c.excerpt)}</div>`
      : '';
    let fixHtml = '';
    if (!c.ok && c.fix) {
      if (c.fix.action === 'create_dir') {
        fixHtml = `<div class="fix-row"><button class="btn-fix" data-action="create_dir" data-path="${escapeHtml(c.fix.path)}">Create directory</button></div>`;
      } else if (c.fix.action === 'set_env') {
        fixHtml = `<div class="fix-row"><input class="fix-input" type="text" placeholder="${escapeHtml(c.fix.placeholder)}" data-key="${escapeHtml(c.fix.key)}"><button class="btn-fix" data-action="set_env" data-key="${escapeHtml(c.fix.key)}">Save</button></div>`;
      } else if (c.fix.action === 'create_file') {
        fixHtml = `<div class="fix-row"><button class="btn-fix" data-action="create_file" data-path="${escapeHtml(c.fix.path)}">Create file</button></div>`;
      }
    }
    return `
    <li class="status-item">
      <details>
        <summary>
          <span class="status-icon">${c.ok ? '✅' : '❌'}</span>
          <div>
            <div class="status-name">${escapeHtml(c.name)}</div>
            <div class="status-desc">${escapeHtml(c.description)}</div>
          </div>
          <span class="status-chevron">▶</span>
        </summary>
        ${excerpt}
        ${fixHtml}
      </details>
    </li>`;
  }).join('');
  content.innerHTML = `
    <div class="status-toolbar">
      <button class="btn-open-editor" id="openEditorBtn">Open config folder ↗</button>
    </div>
    <ul class="status-list">${items}</ul>`;
  document.getElementById('openEditorBtn').addEventListener('click', onOpenEditor);
  for (const btn of content.querySelectorAll('.btn-fix')) {
    btn.addEventListener('click', onFix);
  }
}

async function onFix(ev) {
  const btn = ev.currentTarget;
  const action = btn.dataset.action;
  btn.disabled = true;
  try {
    if (action === 'create_dir') {
      const res = await fetch('/api/status/create-dir', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ path: btn.dataset.path }),
      });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.error || 'HTTP ' + res.status); }
    } else if (action === 'set_env') {
      const row = btn.closest('.fix-row');
      const value = row.querySelector('.fix-input').value.trim();
      if (!value) { btn.disabled = false; return; }
      const res = await fetch('/api/status/set-env', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ key: btn.dataset.key, value }),
      });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.error || 'HTTP ' + res.status); }
    } else if (action === 'create_file') {
      const res = await fetch('/api/status/create-file', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ path: btn.dataset.path }),
      });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.error || 'HTTP ' + res.status); }
    }
    load(false);
  } catch (e) {
    toast(`Fix failed: ${e.message}`, true);
    btn.disabled = false;
  }
}

const CLEANUP_KIND_LABEL = {
  local_gone: 'local · upstream gone',
  local_merged: 'local · merged',
  worktree: 'worktree',
  remote_merged: 'remote · merged',
};

function renderCleanup(data) {
  const content = document.getElementById('content');
  const repos = (data && data.repos) || [];
  if (!repos.length) {
    content.innerHTML = '<div class="empty">No repos to scan. Add paths in <code>CLEANUP_REPOS</code> on the Settings tab (the agent-clone cache is scanned automatically).</div>';
    return;
  }
  let total = 0;
  let body = '';
  for (const repo of repos) {
    body += `<div class="cl-repo"><div class="group-header">${escapeHtml(repo.label)} <span class="badge-meta">${escapeHtml(repo.kind)}</span></div>`;
    if (!repo.ok) {
      body += `<div class="cl-note">${escapeHtml(repo.error || 'scan failed')}</div></div>`;
      continue;
    }
    if (!repo.candidates.length) {
      body += '<div class="cl-note">Nothing to clean up. 🎉</div></div>';
      continue;
    }
    for (const c of repo.candidates) {
      total++;
      const key = repo.path + '|' + c.kind + '|' + c.name;
      const forceable = c.kind !== 'remote_merged';
      body += `
        <div class="cl-row" data-key="${escapeHtml(key)}" data-mine="${c.mine ? '1' : '0'}">
          <label class="cl-main">
            <input type="checkbox" class="cl-pick"
              data-repo="${escapeHtml(repo.path)}" data-kind="${escapeHtml(c.kind)}"
              data-name="${escapeHtml(c.name)}" data-wt="${escapeHtml(c.worktree_path || '')}"
              data-remote="${c.kind === 'remote_merged' ? '1' : ''}">
            <span class="cl-name">${escapeHtml(c.name)}</span>
            <span class="badge-meta">${escapeHtml(CLEANUP_KIND_LABEL[c.kind] || c.kind)}</span>
            <span class="cl-reason">${escapeHtml(c.reason || '')}${c.author && !c.mine ? ' · ' + escapeHtml(c.author) : ''}</span>
          </label>
          ${forceable ? '<label class="cl-force" title="Force: override git safety refusal — may discard unmerged work"><input type="checkbox" class="cl-forcebox"> force</label>' : ''}
          <span class="cl-result"></span>
        </div>`;
    }
    body += '</div>';
  }
  content.innerHTML = `
    <div class="cl-bar">
      <button class="btn-settings-save" id="cleanupDeleteBtn">Delete selected</button>
      <label class="status-check"><input type="checkbox" id="cleanupMineOnly" checked> Only my branches</label>
      <span class="cl-hint">${total} candidate${total === 1 ? '' : 's'}. Tick items, then Delete. Use Refresh (top-right) to fetch &amp; prune first.</span>
    </div>
    <div class="cleanup mine-only" id="cleanupList">${body}</div>`;
  const btn = document.getElementById('cleanupDeleteBtn');
  if (btn) btn.addEventListener('click', onCleanupDelete);
  const mineOnly = document.getElementById('cleanupMineOnly');
  if (mineOnly) mineOnly.addEventListener('change', e => {
    document.getElementById('cleanupList').classList.toggle('mine-only', e.target.checked);
  });
}

// Map git's (often very verbose) stderr to a short row message; full text -> title.
function cleanupShortError(err) {
  if (!err) return 'failed';
  if (/not fully merged/i.test(err)) return 'not fully merged — tick Force to delete';
  if (/not clean|is dirty|contains modified|locked working tree|use .*--force/i.test(err)) return 'worktree not clean — tick Force';
  if (/protected branch/i.test(err)) return 'protected branch';
  if (/not a current cleanup candidate/i.test(err)) return 'no longer a candidate — Refresh';
  return err.length > 80 ? err.slice(0, 80) + '…' : err;
}

async function onCleanupDelete() {
  const picks = [...document.querySelectorAll('.cl-pick:checked')];
  if (!picks.length) { toast('Nothing selected', true); return; }
  const actions = picks.map(p => {
    const row = p.closest('.cl-row');
    return {
      repo_path: p.dataset.repo, kind: p.dataset.kind, name: p.dataset.name,
      worktree_path: p.dataset.wt || undefined,
      force: !!row.querySelector('.cl-forcebox:checked'),
      remote: p.dataset.remote === '1',
    };
  });
  const remoteCount = actions.filter(a => a.remote).length;
  const forceCount = actions.filter(a => a.force).length;
  let msg = `Delete ${actions.length} item${actions.length === 1 ? '' : 's'}?`;
  if (remoteCount) msg += `\n• ${remoteCount} REMOTE branch deletion(s) — pushed to origin, not easily undone.`;
  if (forceCount) msg += `\n• ${forceCount} forced deletion(s) — may discard unmerged work.`;
  if (!confirm(msg)) return;
  const btn = document.getElementById('cleanupDeleteBtn');
  btn.disabled = true; btn.textContent = 'Deleting…';
  let results;
  try {
    const res = await fetch('/api/cleanup/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actions: actions.map(({ remote, ...a }) => a) }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(json.error || 'HTTP ' + res.status);
    results = json.results || [];
  } catch (e) {
    toast('Delete failed: ' + e.message, true);
    btn.disabled = false; btn.textContent = 'Delete selected';
    return;
  }
  const rowsByKey = {};
  for (const row of document.querySelectorAll('.cl-row')) rowsByKey[row.dataset.key] = row;
  let okCount = 0;
  for (const r of results) {
    const row = rowsByKey[r.repo_path + '|' + r.kind + '|' + r.name];
    if (!row) continue;
    const out = row.querySelector('.cl-result');
    const pick = row.querySelector('.cl-pick');
    if (r.ok) {
      okCount++;
      row.classList.add('cl-done');
      if (pick) { pick.checked = false; pick.disabled = true; }
      if (out) out.textContent = '✓ removed';
    } else {
      if (out) { out.textContent = '✗ ' + cleanupShortError(r.error); out.title = r.error || ''; }
      // Reveal the force toggle so the user can retry a refused safe delete.
      const fb = row.querySelector('.cl-force');
      if (fb) fb.classList.add('cl-force-show');
    }
  }
  btn.disabled = false; btn.textContent = 'Delete selected';
  toast(`${okCount}/${results.length} removed`);
}

function renderSettings() {
  const c = CONFIG;
  const fields = [
    { key: 'FRESH_REVIEWERS', label: 'Fresh reviewers', type: 'text',
      desc: 'GitHub logins to DM on Slack when nobody has reviewed your PR yet (comma-separated).',
      value: (c.fresh_reviewers || []).join(',') },
    { key: 'TEAM_CHANNEL_ID', label: 'Team Slack channel ID', type: 'text',
      desc: 'Slack channel ID for the #Channel button. Find it in Slack: right-click channel → View channel details.',
      value: c.team_channel_id || '' },
    { key: 'DEPLOY_TARGET', label: 'Deploy target environment', type: 'text',
      desc: 'Default environment for the Deploy button on each PR card (e.g. csi-3).',
      value: c.deploy_target || '' },
    { key: 'JIRA_SITE', label: 'Jira site', type: 'text',
      desc: 'Atlassian site host for the Tickets tab, e.g. your-org.atlassian.net.',
      value: c.jira_site || '' },
    { key: 'JIRA_EMAIL', label: 'Jira email', type: 'text',
      desc: 'Atlassian account email used with the API token for the Tickets tab.',
      value: c.jira_email || '' },
    { key: 'JIRA_API_TOKEN', label: 'Jira API token', type: 'password',
      desc: 'API token from id.atlassian.com/manage-profile/security/api-tokens. '
        + (c.jira_token_set ? 'Currently set — leave blank to keep it.' : 'Not set.'),
      value: '' },
    { key: 'CACHE_TTL', label: 'Cache TTL (seconds)', type: 'number',
      desc: 'How long per-PR detail data is cached before a background refresh.',
      value: c.cache_ttl ?? 30 },
    { key: 'CLEANUP_REPOS', label: 'Cleanup repo paths', type: 'text',
      desc: 'Local repo paths the Cleanup tab scans (comma-separated, e.g. ~/git/app,~/git/api). The agent-clone cache is always scanned too.',
      value: (c.cleanup_repos || []).join(',') },
    { key: 'CLEANUP_AUTHOR_EMAIL', label: 'Cleanup: my email', type: 'text',
      desc: "Email treated as \"me\" for the Cleanup tab's \"Only my branches\" filter. Leave blank to use each repo's git config user.email.",
      value: c.cleanup_author_email || '' },
    { key: 'EDITOR_CMD', label: 'Editor command', type: 'text',
      desc: 'Command used by "Open config folder ↗" on the Status tab. E.g. "code", "cursor", "subl". Leave blank to auto-detect (VS Code → system default).',
      value: c.editor_cmd || '' },
    { key: 'HOST', label: 'Bind host', type: 'text',
      desc: 'Local address to bind the server to.', restart: true,
      value: c.host || '127.0.0.1' },
    { key: 'PORT', label: 'Port', type: 'number',
      desc: 'Port to listen on.', restart: true,
      value: c.port ?? 8765 },
  ];
  const rows = fields.map(f => `
    <div class="settings-row">
      <label class="settings-label" for="s-${f.key}">${escapeHtml(f.label)}</label>
      <div class="settings-desc">${escapeHtml(f.desc)}</div>
      ${f.restart ? '<div class="settings-restart">⚠ Requires server restart to take effect.</div>' : ''}
      <input class="settings-input" id="s-${f.key}" data-key="${escapeHtml(f.key)}"
             type="${f.type}" value="${escapeHtml(String(f.value))}">
    </div>`).join('');
  document.getElementById('content').innerHTML = `
    <div class="settings-form">
      ${rows}
      <div class="settings-row">
        <label class="settings-label">Tickets: visible statuses</label>
        <div class="settings-desc">Only these statuses show on the Tickets tab (one column each). None checked = show all. Fetch the list from Jira, then check the ones you want.</div>
        <button class="btn-fetch-statuses" id="fetchStatusesBtn" type="button">Fetch statuses from Jira</button>
        <div id="statusChecks" class="status-checks"></div>
      </div>
      <div><button class="btn-settings-save" id="settingsSaveBtn">Save &amp; Reload</button></div>
    </div>`;
  // Pre-render the saved selection (all checked) so it shows without fetching.
  renderStatusChecks(c.jira_status_filter || [], c.jira_status_filter || []);
  document.getElementById('fetchStatusesBtn').addEventListener('click', fetchStatuses);
  document.getElementById('settingsSaveBtn').addEventListener('click', saveSettings);
}

// Render a checkbox per status name; `checked` is the subset that starts ticked.
function renderStatusChecks(names, checked) {
  const on = new Set(checked);
  const el = document.getElementById('statusChecks');
  if (!el) return;
  if (!names.length) {
    el.innerHTML = '<div class="settings-desc">No statuses yet — click “Fetch statuses from Jira”.</div>';
    return;
  }
  el.innerHTML = names.map(n => `
    <label class="status-check">
      <input type="checkbox" value="${escapeHtml(n)}" ${on.has(n) ? 'checked' : ''}>
      ${escapeHtml(n)}
    </label>`).join('');
}

async function fetchStatuses() {
  const btn = document.getElementById('fetchStatusesBtn');
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = 'Fetching…';
  try {
    const res = await fetch('/api/jira/statuses');
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || 'HTTP ' + res.status);
    // Keep whatever is currently ticked checked in the refreshed list.
    const stillChecked = [...document.querySelectorAll('#statusChecks input:checked')].map(b => b.value);
    const saved = CONFIG.jira_status_filter || [];
    renderStatusChecks(body.statuses || [], stillChecked.length ? stillChecked : saved);
  } catch (e) {
    toast('Failed to fetch statuses: ' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

async function saveSettings() {
  const btn = document.getElementById('settingsSaveBtn');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  const settings = {};
  for (const input of document.querySelectorAll('.settings-input')) {
    settings[input.dataset.key] = input.value.trim();
  }
  // Status filter: serialize the checked boxes. Only send the key when boxes are
  // present, so an unfetched/empty list never clobbers a saved filter — but an
  // explicit "none checked" (boxes present, none ticked) clears it.
  const statusBoxes = document.querySelectorAll('#statusChecks input[type=checkbox]');
  if (statusBoxes.length) {
    settings['JIRA_STATUS_FILTER'] =
      [...statusBoxes].filter(b => b.checked).map(b => b.value).join(',');
  }
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || 'HTTP ' + res.status);
    location.reload();
  } catch (e) {
    toast(`Save failed: ${e.message}`, true);
    btn.disabled = false;
    btn.textContent = 'Save & Reload';
  }
}

function deployedStatusIcon(item) {
  if (item.error) return '❓';
  if (item.status === 'in_progress') return '🔄';
  if (item.status === 'queued') return '⏳';
  if (item.conclusion === 'success') return '✅';
  if (item.conclusion === 'failure') return '❌';
  if (item.conclusion === 'cancelled') return '🚫';
  return '❓';
}

function renderDeployed(data) {
  const content = document.getElementById('content');
  const envs = data.environments || {};
  const envKeys = Object.keys(envs).sort();
  if (!envKeys.length) {
    const hint = data.target_env
      ? `No deploy targets found for <strong>${escapeHtml(data.target_env)}</strong> in <code>deploy_targets.json</code>.`
      : 'No deploy targets configured. Add a <code>deploy_targets.json</code> via the Status tab.';
    content.innerHTML = `<div class="empty">${hint}</div>`;
    return;
  }
  const deployTargets = CONFIG.deploy_targets || {};
  let html = '';
  for (const env of envKeys) {
    const items = (envs[env] || []).slice().sort((a, b) => (a.repo || '').localeCompare(b.repo || ''));
    html += `<details class="deployed-section" open>
      <summary class="deployed-env-header">
        <span class="deployed-env-label">${escapeHtml(env)}</span>
        <span class="deployed-env-count">${items.length}</span>
        <span class="deployed-chevron">▶</span>
      </summary>
      <div class="deployed-items">`;
    for (const item of items) {
      const icon = deployedStatusIcon(item);
      const repoShort = (item.repo || '').replace(/^[^/]+\//, '');
      const branchHtml = item.branch
        ? `<span class="deployed-branch" title="${escapeHtml(item.branch)}">${escapeHtml(item.branch)}</span>` : '';
      const metaHtml = item.error
        ? `<span class="deployed-error">${escapeHtml(item.error)}</span>`
        : `<span class="deployed-meta">${item.createdAt ? relativeTime(item.createdAt) : ''}${item.displayTitle ? ' · ' + escapeHtml(item.displayTitle) : ''}</span>`;
      const hasWorkflow = !!((deployTargets[item.repo] || {})[env]);
      const deployRow = hasWorkflow
        ? `<div class="deployed-deploy-row">
          <select class="deployed-branch-select" data-repo="${escapeHtml(item.repo)}" data-env="${escapeHtml(env)}" disabled>
            <option value="">Loading branches…</option>
          </select>
          <button class="deployed-deploy-btn" data-repo="${escapeHtml(item.repo)}" data-env="${escapeHtml(env)}" disabled>Deploy</button>
        </div>`
        : '';
      html += `<div class="deployed-item">
        <span class="deployed-icon">${icon}</span>
        <span class="deployed-repo" title="${escapeHtml(item.repo || '')}">${escapeHtml(repoShort)}</span>
        ${branchHtml}
        ${metaHtml}
      </div>${deployRow}`;
    }
    html += '</div></details>';
  }
  content.innerHTML = html;
  for (const btn of content.querySelectorAll('.deployed-deploy-btn')) {
    btn.addEventListener('click', onDeployFromDeployed);
  }
  loadDeployedBranches(content);
}

async function loadDeployedBranches(container) {
  const selects = [...container.querySelectorAll('.deployed-branch-select')];
  const repos = [...new Set(selects.map(s => s.dataset.repo))];
  await Promise.all(repos.map(async repo => {
    let branches = [], baseBranch = '';
    try {
      const res = await fetch(`/api/branches?repo=${encodeURIComponent(repo)}`);
      const json = res.ok ? await res.json() : {};
      branches = json.branches || [];
      baseBranch = json.base_branch || '';
    } catch (_) {}
    const hasOptions = baseBranch || branches.length;
    for (const sel of selects.filter(s => s.dataset.repo === repo)) {
      if (hasOptions) {
        let opts = '<option value="">— select branch —</option>';
        if (baseBranch) {
          opts += `<optgroup label="Base branch"><option value="${escapeHtml(baseBranch)}">${escapeHtml(baseBranch)}</option></optgroup>`;
        }
        if (branches.length) {
          opts += `<optgroup label="My branches">${branches.map(b => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join('')}</optgroup>`;
        }
        sel.innerHTML = opts;
        sel.disabled = false;
        sel.addEventListener('change', () => {
          const btn = sel.closest('.deployed-deploy-row').querySelector('.deployed-deploy-btn');
          if (btn) btn.disabled = !sel.value;
        });
      } else {
        sel.innerHTML = '<option value="">No branches found</option>';
      }
    }
  }));
}

async function onDeployFromDeployed(ev) {
  const btn = ev.currentTarget;
  const repo = btn.dataset.repo;
  const env = btn.dataset.env;
  const sel = btn.closest('.deployed-deploy-row').querySelector('.deployed-branch-select');
  const headRef = sel?.value;
  if (!headRef) return;
  if (!confirm(`Deploy ${repo} (${headRef}) to ${env.toUpperCase()}?`)) return;
  btn.disabled = true;
  btn.textContent = 'Deploying…';
  try {
    const res = await fetch('/api/deploy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, env, head_ref: headRef }),
    });
    const json = await res.json();
    if (!res.ok) throw new Error(json.error || 'Deploy failed');
    btn.textContent = '✅ Dispatched';
    btn.style.background = '#1a7f37';
    setTimeout(() => {
      btn.textContent = 'Deploy';
      btn.style.cssText = '';
      btn.disabled = !sel?.value;
    }, 4000);
  } catch (e) {
    toast(`Deploy failed: ${e.message}`, true);
    btn.textContent = 'Deploy';
    btn.disabled = !sel?.value;
  }
}

async function load(fresh) {
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true;
  document.getElementById('content').innerHTML = '<div class="empty">Loading…</div>';
  try {
    if (currentTab === 'settings') {
      renderSettings();
    } else if (currentTab === 'status') {
      const res = await fetch('/api/status');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      renderStatus(await res.json());
    } else if (currentTab === 'deployed') {
      const res = await fetch('/api/deployed');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      renderDeployed(await res.json());
    } else if (currentTab === 'cleanup') {
      const res = await fetch('/api/cleanup' + (fresh ? '?fresh=1' : ''));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      renderCleanup(await res.json());
    } else if (currentTab === 'tickets') {
      const res = await fetch('/api/tickets' + (fresh ? '?fresh=1' : ''));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (!data.configured) {
        document.getElementById('content').innerHTML =
          '<div class="empty">Configure Jira in <code>.env</code>: set JIRA_SITE, JIRA_EMAIL, and JIRA_API_TOKEN, then refresh.</div>';
        return;
      }
      const filter = CONFIG.jira_status_filter || [];
      let tickets = data.tickets;
      if (filter.length) {
        const allow = new Set(filter);
        tickets = tickets.filter(t => allow.has(t.status_label));
      }
      // One column per status: saved-filter order if set, else statuses present
      // ordered by category (To Do before In Progress) then name.
      const order = filter.length ? filter.slice() : ticketStatusOrder(tickets);
      TABS.tickets.groups = order;
      TABS.tickets.headers = Object.fromEntries(order.map(s => [s, s]));
      render(tickets);
    } else {
      const tab = TABS[currentTab];
      const url = tab.endpoint + (fresh ? '?fresh=1' : '');
      if (currentTab === 'mine') {
        const [prsRes, depRes] = await Promise.all([fetch(url), fetch('/api/deployed')]);
        if (!prsRes.ok) throw new Error('HTTP ' + prsRes.status);
        deployedState = depRes.ok ? (await depRes.json()).environments || {} : {};
        render(await prsRes.json());
      } else {
        const res = await fetch(url);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        render(await res.json());
      }
    }
  } catch (e) {
    document.getElementById('content').innerHTML =
      `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('refreshBtn').addEventListener('click', () => load(true));
// Single delegated listener for all PR-card buttons across re-renders.
document.getElementById('content').addEventListener('click', onContentClick);
for (const el of document.querySelectorAll('.tab')) {
  el.addEventListener('click', () => {
    if (el.dataset.tab === currentTab) return;
    setActiveTab(el.dataset.tab);
    load(false);
  });
}
setActiveTab(currentTab);
load(false);
</script>
</body>
</html>
"""


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
        (NUDGE_WORKFLOW,        "nudge_workflow.md",        "Nudge workflow instructions"),
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

    check("FRESH_REVIEWERS", "Slack nudge targets (.env)",
          bool(FRESH_REVIEWERS),
          "Logins: " + ", ".join(FRESH_REVIEWERS) if FRESH_REVIEWERS else "Not set — add FRESH_REVIEWERS=login1,login2 to .env",
          fix={"action": "set_env", "key": "FRESH_REVIEWERS", "placeholder": "login1,login2"})

    check("TEAM_CHANNEL_ID", "Team Slack channel (.env)",
          bool(TEAM_CHANNEL_ID),
          f"Channel ID: {TEAM_CHANNEL_ID}" if TEAM_CHANNEL_ID else "Not set — add TEAM_CHANNEL_ID=C... to .env",
          fix={"action": "set_env", "key": "TEAM_CHANNEL_ID", "placeholder": "C0123456789"})

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
        if parsed.path == "/":
            config_json = json.dumps({
                "fresh_reviewers": FRESH_REVIEWERS,
                "team_channel_id": TEAM_CHANNEL_ID,
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
            })
            body = INDEX_HTML.replace(
                "__PR_DASHBOARD_CONFIG__", config_json,
            ).encode("utf-8")
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
            if kind not in ("review", "re_review", "merge", "address", "nudge", "fix_pipeline", "rebase"):
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
        global FRESH_REVIEWERS, TEAM_CHANNEL_ID, DEPLOY_TARGET
        try:
            data = self._read_json_body()
            key = str(data["key"])
            value = str(data["value"]).strip()
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if key not in ("FRESH_REVIEWERS", "TEAM_CHANNEL_ID", "DEPLOY_TARGET"):
            self._send_json(403, {"error": "key not allowed"})
            return
        if not value:
            self._send_json(400, {"error": "value required"})
            return
        try:
            with _env_lock:
                write_env_var(key, value)
                if key == "FRESH_REVIEWERS":
                    FRESH_REVIEWERS = _env_list("FRESH_REVIEWERS")
                elif key == "TEAM_CHANNEL_ID":
                    TEAM_CHANNEL_ID = value
                elif key == "DEPLOY_TARGET":
                    DEPLOY_TARGET = value
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_settings_post(self):
        global FRESH_REVIEWERS, TEAM_CHANNEL_ID, DEPLOY_TARGET, CACHE_TTL, EDITOR_CMD
        global JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_STATUS_FILTER
        global CLEANUP_REPOS, CLEANUP_AUTHOR_EMAIL
        _allowed = {"FRESH_REVIEWERS", "TEAM_CHANNEL_ID", "DEPLOY_TARGET",
                    "CACHE_TTL", "HOST", "PORT", "EDITOR_CMD",
                    "JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN",
                    "JIRA_STATUS_FILTER", "CLEANUP_REPOS", "CLEANUP_AUTHOR_EMAIL"}
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
                    "JIRA_STATUS_FILTER", "CLEANUP_REPOS", "CLEANUP_AUTHOR_EMAIL"
                ):
                    continue
                try:
                    write_env_var(key, value)
                except Exception as e:
                    self._send_json(500, {"error": f"failed to write {key}: {e}"})
                    return
                if key == "FRESH_REVIEWERS":
                    FRESH_REVIEWERS = _env_list("FRESH_REVIEWERS")
                elif key == "TEAM_CHANNEL_ID":
                    TEAM_CHANNEL_ID = value
                elif key == "DEPLOY_TARGET":
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
            os.path.realpath(NUDGE_WORKFLOW):         _DEFAULT_NUDGE_WORKFLOW,
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
