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


def _is_human_author(author):
    """True if the GraphQL author node is a real user (not a Bot, etc)."""
    if not author:
        return False
    typename = author.get("__typename")
    # When __typename isn't fetched, fall back to accepting it (legacy callers).
    return typename in (None, "User", "Mannequin", "EnterpriseUserAccount")


# Bots that are regular User accounts (so __typename=User on GraphQL and no
# `[bot]` suffix). Add new entries as they're encountered.
KNOWN_BOT_LOGINS = {"codacy-production"}


def _is_bot_login(login):
    """Heuristic bot detection by login string only (no __typename)."""
    if not login:
        return False
    if login.endswith("[bot]"):
        return True
    return login in KNOWN_BOT_LOGINS


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
        # A thread is "active" only if it is neither resolved nor outdated.
        # Outdated means the code changed under the comment — the change was
        # addressed by new commits, so it's no longer something I need to fix.
        if not t.get("isResolved") and not t.get("isOutdated"):
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

    any_human_review = any(
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
            comments(first: 1) {
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
              }
            }
          }
        }
      }
    }
  }
}
"""


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

    enriched = []
    for pr in candidates:
        try:
            detail = fetch_detail(pr["repository"], pr["number"], fresh=fresh)
        except Exception as e:
            print(f"[warn] detail fetch failed for {pr['repository']}#{pr['number']}: {e}", flush=True)
            continue
        status = determine_status(
            pr["repository"], pr["number"], detail, me, fresh,
        )
        if status is None:
            continue
        enriched.append({**pr, **status})

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
        self.kind = kind  # "review" | "merge" | "address"
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


def run_review(job):
    number = job.number
    repo = job.repo
    os.makedirs(LOG_DIR, exist_ok=True)
    safe_repo = repo.replace("/", "_")
    log_path = f"{LOG_DIR}/{safe_repo}-{number}-{int(time.time())}.log"
    job.log_path = log_path
    job.append(f"Starting review of #{number} in {repo}")
    print(f"[review] starting #{number} in {repo} (log: {log_path})", flush=True)

    try:
        workflow = _load_workflow(REVIEW_WORKFLOW)
    except FileNotFoundError:
        job.append(f"Review workflow file not found: {REVIEW_WORKFLOW}")
        job.append("Use the Status tab to create it.")
        job.finish("failed", "missing_workflow")
        return

    prompt = REVIEW_PROMPT.format(number=number, repo=repo) + workflow
    events = []
    try:
        proc = subprocess.Popen(
            [
                "claude", "-p", prompt,
                "--permission-mode", "bypassPermissions",
                "--output-format", "stream-json",
                "--verbose",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        job.append(f"Failed to spawn claude: {e}")
        job.finish("failed", "spawn_error")
        return

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
        return

    if proc.returncode != 0:
        if job._stop_requested:
            job.append("Review stopped.")
            job.finish("stopped", "stopped")
        else:
            job.append(f"claude exited with code {proc.returncode}")
            job.finish("failed", f"exit:{proc.returncode}")
        return

    try:
        me = get_my_login()
    except Exception:
        me = None
    result = derive_result(events, repo, number, me)
    label = {
        "approved": "Approved PR ✓",
        "no_action": "Finished (no GitHub action taken)",
    }.get(result)
    if label is None and result.startswith("commented:"):
        n = result.split(":", 1)[1]
        label = f"Posted {n} pending comment(s)"
    job.append(label or f"Finished: {result}")
    job.finish("done", result)
    print(f"[review] finished #{number} result={result}", flush=True)


def run_re_review(job):
    number = job.number
    repo = job.repo
    os.makedirs(LOG_DIR, exist_ok=True)
    safe_repo = repo.replace("/", "_")
    log_path = f"{LOG_DIR}/re-review-{safe_repo}-{number}-{int(time.time())}.log"
    job.log_path = log_path
    job.append(f"Starting re-review of #{number} in {repo}")
    print(f"[re-review] starting #{number} in {repo} (log: {log_path})", flush=True)

    try:
        workflow = _load_workflow(RE_REVIEW_WORKFLOW)
    except FileNotFoundError:
        job.append(f"Re-review workflow file not found: {RE_REVIEW_WORKFLOW}")
        job.append("Use the Status tab to create it.")
        job.finish("failed", "missing_workflow")
        return

    prompt = RE_REVIEW_PROMPT.format(number=number, repo=repo) + workflow
    events = []
    try:
        proc = subprocess.Popen(
            [
                "claude", "-p", prompt,
                "--permission-mode", "bypassPermissions",
                "--output-format", "stream-json",
                "--verbose",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        job.append(f"Failed to spawn claude: {e}")
        job.finish("failed", "spawn_error")
        return

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
        return

    if proc.returncode != 0:
        if job._stop_requested:
            job.append("Re-review stopped.")
            job.finish("stopped", "stopped")
        else:
            job.append(f"claude exited with code {proc.returncode}")
            job.finish("failed", f"exit:{proc.returncode}")
        return

    try:
        me = get_my_login()
    except Exception:
        me = None
    result = derive_result(events, repo, number, me)
    label = {
        "approved": "Approved PR ✓",
        "no_action": "Finished (no GitHub action taken)",
    }.get(result)
    if label is None and result.startswith("commented:"):
        n = result.split(":", 1)[1]
        label = f"Posted {n} pending comment(s)"
    job.append(label or f"Finished: {result}")
    job.finish("done", result)
    print(f"[re-review] finished #{number} result={result}", flush=True)


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


def run_address(job, head_ref):
    """Spawn Claude in a worktree to address PR comments."""
    repo = job.repo
    number = job.number

    with get_repo_lock(repo):
        try:
            clone_path, local_branch = prepare_agent_clone(repo, head_ref)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            job.append(f"Agent-clone setup failed: {stderr}")
            job.finish("failed", "clone_error")
            return

        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = f"{LOG_DIR}/address-{repo_flat(repo)}-{number}-{int(time.time())}.log"
        job.log_path = log_path
        job.append(f"Agent clone ready at {clone_path} (branch: {local_branch})")
        print(f"[address] starting #{number} in {repo} (clone: {clone_path})", flush=True)

        try:
            workflow = _load_workflow(ADDRESS_WORKFLOW)
        except FileNotFoundError:
            job.append(f"Address workflow file not found: {ADDRESS_WORKFLOW}")
            job.append("Use the Status tab to create it.")
            job.finish("failed", "missing_workflow")
            return

        prompt = ADDRESS_PROMPT.format(
            number=number, repo=repo,
            head_ref=head_ref, local_branch=local_branch,
        ) + workflow
        events = []
        proc = None
        try:
            proc = subprocess.Popen(
                ["claude", "-p", prompt,
                 "--permission-mode", "bypassPermissions",
                 "--output-format", "stream-json",
                 "--verbose"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=clone_path,
            )
            job.proc = proc
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
            return

        if proc.returncode != 0:
            if job._stop_requested:
                job.append("Stopped.")
                job.finish("stopped", "stopped")
            else:
                job.append(f"claude exited with code {proc.returncode}")
                job.finish("failed", f"exit:{proc.returncode}")
            return

        result = derive_address_result(events)
        job.append(result["label"])
        job.finish("done", result["label"])
        print(f"[address] finished #{number} result={result['label']}", flush=True)


# ---- Fix-pipeline dispatch -------------------------------------------------

FIX_PIPELINE_PROMPT = (
    "Fix the failing CI pipeline on PR #{number} in {repo}. "
    "PR head branch on origin: {head_ref}. "
    "Local branch in this worktree: {local_branch}. "
    "Push fixes with: git push origin {local_branch}:{head_ref}\n\n"
)


def run_fix_pipeline(job, head_ref: str) -> None:
    """Spawn Claude in a worktree to diagnose and fix failing CI checks."""
    repo = job.repo
    number = job.number

    with get_repo_lock(repo):
        try:
            clone_path, local_branch = prepare_agent_clone(repo, head_ref)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            job.append(f"Agent-clone setup failed: {stderr}")
            job.finish("failed", "clone_error")
            return

        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = f"{LOG_DIR}/fix-pipeline-{repo_flat(repo)}-{number}-{int(time.time())}.log"
        job.log_path = log_path
        job.append(f"Agent clone ready at {clone_path} (branch: {local_branch})")
        print(f"[fix-pipeline] starting #{number} in {repo} (clone: {clone_path})", flush=True)

        try:
            workflow = _load_workflow(FIX_PIPELINE_WORKFLOW)
        except FileNotFoundError:
            job.append(f"Fix-pipeline workflow file not found: {FIX_PIPELINE_WORKFLOW}")
            job.append("Use the Status tab to create it.")
            job.finish("failed", "missing_workflow")
            return

        prompt = FIX_PIPELINE_PROMPT.format(
            number=number, repo=repo,
            head_ref=head_ref, local_branch=local_branch,
        ) + workflow
        proc = None
        try:
            proc = subprocess.Popen(
                ["claude", "-p", prompt,
                 "--permission-mode", "bypassPermissions",
                 "--output-format", "stream-json",
                 "--verbose"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=clone_path,
            )
            job.proc = proc
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
                    friendly = format_event(ev)
                    if friendly:
                        job.append(friendly)
            proc.wait()
        except Exception as e:
            job.append(f"Stream error: {e}")
            job.finish("failed", "stream_error")
            return

        if proc.returncode != 0:
            if job._stop_requested:
                job.append("Stopped.")
                job.finish("stopped", "stopped")
            else:
                job.append(f"claude exited with code {proc.returncode}")
                job.finish("failed", f"exit:{proc.returncode}")
            return

        job.append("Pipeline fix complete.")
        job.finish("done", "pipeline_fixed")
        print(f"[fix-pipeline] finished #{number}", flush=True)


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
    repo = job.repo
    number = job.number

    with get_repo_lock(repo):
        try:
            clone_path, local_branch = prepare_agent_clone(repo, head_ref)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            job.append(f"Agent-clone setup failed: {stderr}")
            job.finish("failed", "clone_error")
            return

        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = f"{LOG_DIR}/rebase-{repo_flat(repo)}-{number}-{int(time.time())}.log"
        job.log_path = log_path
        job.append(f"Agent clone ready at {clone_path} (branch: {local_branch})")
        print(f"[rebase] starting #{number} in {repo} (clone: {clone_path})", flush=True)

        try:
            workflow = _load_workflow(REBASE_WORKFLOW)
        except FileNotFoundError:
            job.append(f"Rebase workflow file not found: {REBASE_WORKFLOW}")
            job.append("Use the Status tab to create it.")
            job.finish("failed", "missing_workflow")
            return

        prompt = REBASE_PROMPT.format(
            number=number, repo=repo,
            head_ref=head_ref, local_branch=local_branch, base_ref=base_ref,
        ) + workflow.format(
            base_ref=base_ref, head_ref=head_ref, local_branch=local_branch,
        )
        proc = None
        try:
            proc = subprocess.Popen(
                ["claude", "-p", prompt,
                 "--permission-mode", "bypassPermissions",
                 "--output-format", "stream-json",
                 "--verbose"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=clone_path,
            )
            job.proc = proc
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
                    friendly = format_event(ev)
                    if friendly:
                        job.append(friendly)
            proc.wait()
        except Exception as e:
            job.append(f"Stream error: {e}")
            job.finish("failed", "stream_error")
            return

        if proc.returncode != 0:
            if job._stop_requested:
                job.append("Stopped.")
                job.finish("stopped", "stopped")
            else:
                job.append(f"claude exited with code {proc.returncode}")
                job.finish("failed", f"exit:{proc.returncode}")
            return

        job.append("Rebase complete.")
        job.finish("done", "rebased")
        print(f"[rebase] finished #{number}", flush=True)


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

_RE_SLACK_DM = re.compile(r"slack_send_message\b")


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
    repo = job.repo
    number = job.number
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = f"{LOG_DIR}/nudge-{repo_flat(repo)}-{number}-{int(time.time())}.log"
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
    events = []
    try:
        proc = subprocess.Popen(
            ["claude", "-p", prompt,
             "--permission-mode", "bypassPermissions",
             "--output-format", "stream-json",
             "--verbose"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        job.proc = proc
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
        return

    if proc.returncode != 0:
        if job._stop_requested:
            job.append("Stopped.")
            job.finish("stopped", "stopped")
        else:
            job.append(f"claude exited with code {proc.returncode}")
            job.finish("failed", f"exit:{proc.returncode}")
        return

    result = derive_nudge_result(events, mode=mode)
    job.append(result["label"])
    job.finish("done", result["label"])
    print(f"[nudge] finished #{number} result={result['label']}", flush=True)


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
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--fg);
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
    <button class="tab" data-tab="deployed">Deployed</button>
    <button class="tab" data-tab="status">Status</button>
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
  },
};

const _tab = (new URLSearchParams(location.search)).get('tab');
let currentTab = ['mine', 'deployed', 'status', 'settings'].includes(_tab) ? _tab : 'incoming';
let deployedState = {};  // environments map from /api/deployed, populated when mine tab loads

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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

function render(prs) {
  const content = document.getElementById('content');
  const tab = TABS[currentTab];
  if (!prs.length) {
    content.innerHTML = '<div class="empty">' + (
      currentTab === 'mine'
        ? '🎉 No open PRs.'
        : '🎉 No PRs waiting. Inbox zero.'
    ) + '</div>';
    return;
  }
  const grouped = {};
  for (const g of tab.groups) grouped[g] = [];
  for (const p of prs) {
    if (grouped[p.status]) grouped[p.status].push(p);
  }
  let html = '';
  for (const g of tab.groups) {
    if (!grouped[g].length) continue;
    html += `<div class="group-header">${escapeHtml(tab.headers[g])}</div>`;
    for (const p of grouped[g]) html += tab.render(p);
  }
  content.innerHTML = html;
  for (const btn of document.querySelectorAll('.btn-review')) {
    btn.addEventListener('click', onReview);
  }
  for (const btn of document.querySelectorAll('.btn-re-review')) {
    btn.addEventListener('click', onReReview);
  }
  for (const btn of document.querySelectorAll('.btn-merge')) {
    btn.addEventListener('click', onMerge);
  }
  for (const btn of document.querySelectorAll('.btn-address')) {
    btn.addEventListener('click', onAddress);
  }
  for (const btn of document.querySelectorAll('.btn-nudge')) {
    btn.addEventListener('click', onNudge);
  }
  for (const btn of document.querySelectorAll('.btn-channel')) {
    btn.addEventListener('click', onChannelPing);
  }
  for (const btn of document.querySelectorAll('.btn-deploy')) {
    btn.addEventListener('click', onDeploy);
  }
  for (const btn of document.querySelectorAll('.btn-fix-pipeline')) {
    btn.addEventListener('click', onFixPipeline);
  }
  for (const btn of document.querySelectorAll('.btn-rebase')) {
    btn.addEventListener('click', onRebase);
  }
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
      <div class="pr-title"><a href="${escapeHtml(p.url)}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a></div>
      <div class="pr-sub">by ${escapeHtml(p.author)} · updated ${relativeTime(p.updatedAt)}</div>
      ${detail}
    </div>
    <div class="pr-actions">
      <a class="btn-open" href="${escapeHtml(p.url)}" target="_blank" rel="noopener">Open ↗</a>
      ${actionBtn}
    </div>
  </div>`;
}

function renderMyPR(p) {
  const commenters = p.active_commenters && p.active_commenters.length
    ? `<div class="pr-detail">From: ${escapeHtml(p.active_commenters.join(', '))}</div>`
    : '';
  const targets = (p.nudge_targets || []).join(',');
  const mode = p.nudge_mode || '';
  const needsRebase = p.merge_state_status === 'BEHIND';
  const hasConflicts = p.merge_state_status === 'DIRTY';
  let actionBtn = '';
  if (p.status === 'approved') {
    const ciBlocked = ['FAILURE', 'ERROR'].includes(p.check_state);
    const reviewBlocked = p.review_decision === 'CHANGES_REQUESTED';
    const blocked = ciBlocked || reviewBlocked || needsRebase || hasConflicts;
    const reasons = [
      ciBlocked && 'CI failing',
      reviewBlocked && 'Changes requested',
      needsRebase && 'Needs rebase',
      hasConflicts && 'Has conflicts',
    ].filter(Boolean);
    const blockReason = reasons.join(' · ');
    const mergeBtn = `<button class="btn-merge${blocked ? ' btn-merge-blocked' : ''}" type="button" ${blocked ? `disabled title="${escapeHtml(blockReason)}"` : ''}>Merge</button>`;
    const fixClass = needsRebase || hasConflicts ? 'btn-rebase'
      : ciBlocked ? 'btn-fix-pipeline' : 'btn-address';
    const fixBtn = blocked
      ? `<button class="${fixClass}" type="button" title="Fix: ${escapeHtml(blockReason)}">Fix</button>`
      : '';
    actionBtn = fixBtn + mergeBtn;
  } else if (p.status === 'has_comments') {
    actionBtn = `<button class="btn-address" type="button">Address</button>`;
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
      <div class="pr-title"><a href="${escapeHtml(p.url)}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a></div>
      <div class="pr-sub">updated ${relativeTime(p.updatedAt)}</div>
      ${commenters}
    </div>
    <div class="pr-actions">
      <a class="btn-open" href="${escapeHtml(p.url)}" target="_blank" rel="noopener">Open ↗</a>
      ${deployControls}
      ${channelBtn}
      ${nudgeBtn}
      ${actionBtn}
    </div>
  </div>`;
}

async function onReview(ev) {
  const btn = ev.currentTarget;
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;

  try {
    const res = await fetch('/api/review', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }

  setReviewing(card);
  streamJob(card, 'review', repo, number, url, finishReview);
}

async function onMerge(ev) {
  const btn = ev.currentTarget;
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const defaultMergeMethod = card.dataset.method;

  if (!confirm(`Merge ${repo} #${number}?`)) return;

  try {
    const res = await fetch('/api/merge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, defaultMergeMethod }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, 'Merging…', 'merge');
  streamJob(card, 'merge', repo, number, url, finishMerge);
}

function finishMerge(card, url, data) {
  const actions = card.querySelector('.pr-actions');
  let cls = 'failed', label = '❌ Merge failed';
  if (data.status === 'done' && data.result === 'merged') {
    cls = 'merged'; label = '✅ Merged';
  }
  actions.innerHTML = `
    <span class="review-status ${cls}">${escapeHtml(label)}</span>
    <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
  `;
}

async function onAddress(ev) {
  const btn = ev.currentTarget;
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const headRefName = card.dataset.head;

  try {
    const res = await fetch('/api/address', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, headRefName }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, 'Addressing…', 'address');
  streamJob(card, 'address', repo, number, url, finishAddress);
}

function finishAddress(card, url, data) {
  const actions = card.querySelector('.pr-actions');
  if (data.status === 'stopped') {
    actions.innerHTML = `
      <span class="review-status stopped">⏹ Stopped</span>
      <button class="btn-address" type="button">Address again</button>
      <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
    `;
    actions.querySelector('.btn-address').addEventListener('click', onAddress);
    return;
  }
  let cls = 'failed', label = '❌ Failed';
  if (data.status === 'done') {
    if (data.result === 'No action') {
      cls = 'commented'; label = 'ℹ No action';
    } else if (data.result === 'Replied only') {
      cls = 'commented'; label = '💬 Replied only';
    } else {
      cls = 'approved'; label = '✅ ' + data.result;
    }
  }
  actions.innerHTML = `
    <span class="review-status ${cls}">${escapeHtml(label)}</span>
    <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
  `;
}

async function onFixPipeline(ev) {
  const btn = ev.currentTarget;
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const headRefName = card.dataset.head;

  try {
    const res = await fetch('/api/fix-pipeline', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, headRefName }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, 'Fixing pipeline…', 'fix_pipeline');
  streamJob(card, 'fix_pipeline', repo, number, url, finishFixPipeline);
}

function finishFixPipeline(card, url, data) {
  const actions = card.querySelector('.pr-actions');
  if (data.status === 'stopped') {
    actions.innerHTML = `
      <span class="review-status stopped">⏹ Stopped</span>
      <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
    `;
    return;
  }
  const ok = data.status === 'done';
  actions.innerHTML = `
    <span class="review-status ${ok ? 'approved' : 'failed'}">${ok ? '✅ Fix pushed' : '❌ Failed'}</span>
    <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
  `;
}

async function onRebase(ev) {
  const btn = ev.currentTarget;
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const headRefName = card.dataset.head;
  const baseRefName = card.dataset.base;
  try {
    const res = await fetch('/api/rebase', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, headRefName, baseRefName }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || 'HTTP ' + res.status);
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, 'Rebasing…', 'rebase');
  streamJob(card, 'rebase', repo, number, url, finishRebase);
}

function finishRebase(card, url, data) {
  const actions = card.querySelector('.pr-actions');
  if (data.status === 'stopped') {
    actions.innerHTML = `
      <span class="review-status stopped">⏹ Stopped</span>
      <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
    `;
    return;
  }
  const ok = data.status === 'done';
  actions.innerHTML = `
    <span class="review-status ${ok ? 'approved' : 'failed'}">${ok ? '✅ Rebased & pushed' : '❌ Rebase failed'}</span>
    <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
  `;
}

async function onNudge(ev) {
  const btn = ev.currentTarget;
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const title = card.dataset.title || '';
  const mode = card.dataset.mode || '';
  const targets = (card.dataset.targets || '').split(',').map(s => s.trim()).filter(Boolean);

  if (!mode || !targets.length) {
    toast('No one to nudge — everyone has approved already.');
    return;
  }

  const promptLabel = mode === 'fresh'
    ? `Ask ${targets.join(' and ')} on Slack to review this PR?`
    : `Nudge on Slack to re-review: ${targets.join(', ')}?`;
  if (!confirm(promptLabel)) return;

  try {
    const res = await fetch('/api/nudge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, url, title, reviewers: targets, mode }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, 'Nudging…', 'nudge');
  streamJob(card, 'nudge', repo, number, url, finishNudge);
}

function finishNudge(card, url, data) {
  const actions = card.querySelector('.pr-actions');
  if (data.status === 'stopped') {
    actions.innerHTML = `
      <span class="review-status stopped">⏹ Stopped</span>
      <button class="btn-nudge" type="button">Nudge again</button>
      <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
    `;
    actions.querySelector('.btn-nudge').addEventListener('click', onNudge);
    return;
  }
  let cls = 'failed', label = '❌ Failed';
  if (data.status === 'done') {
    if (data.result === 'No DMs sent' || data.result === 'Channel post failed') {
      cls = 'commented'; label = 'ℹ ' + data.result;
    } else {
      cls = 'approved'; label = '✅ ' + data.result;
    }
  }
  actions.innerHTML = `
    <span class="review-status ${cls}">${escapeHtml(label)}</span>
    <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
  `;
}

async function onChannelPing(ev) {
  const btn = ev.currentTarget;
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const title = card.dataset.title || '';
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

  try {
    const res = await fetch('/api/nudge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, url, title, reviewers: targets, mode: 'channel' }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, 'Posting in channel…', 'nudge');
  streamJob(card, 'nudge', repo, number, url, finishNudge);
}

async function onDeploy(ev) {
  const btn = ev.currentTarget;
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
    if (!res.ok) throw new Error(body.error || 'HTTP ' + res.status);
    btn.textContent = 'Dispatched ✓';
    btn.style.cssText = 'background:#238636;color:#fff;';
    setTimeout(() => { btn.textContent = 'Deploy'; btn.style.cssText = ''; btn.disabled = false; }, 4000);
  } catch (e) {
    toast(`Deploy failed: ${e.message}`, true);
    btn.textContent = 'Deploy';
    btn.disabled = false;
  }
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
  // label and kind are server-controlled strings, escaped before insertion
  const stopBtn = kind
    ? `<button class="btn-stop" data-kind="${escapeHtml(kind)}">Stop</button>`
    : '';
  actions.innerHTML = `<span class="review-status running"><span class="spinner"></span>${escapeHtml(label)}</span>${stopBtn}`;
  const btn = actions.querySelector('.btn-stop');
  if (btn) btn.addEventListener('click', onStop);
}

function setReviewing(card) { setRunning(card, 'Reviewing…', 'review'); }

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
      actions.innerHTML = `<span class="review-status failed">⚠ Stream lost</span><a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>`;
    }
  });
}

async function onStop(ev) {
  const btn = ev.currentTarget;
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

function finishReview(card, url, data) {
  const actions = card.querySelector('.pr-actions');
  if (data.status === 'stopped') {
    actions.innerHTML = `
      <span class="review-status stopped">⏹ Stopped</span>
      <button class="btn-review" type="button">Review again</button>
      <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
    `;
    actions.querySelector('.btn-review').addEventListener('click', onReview);
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
  actions.innerHTML = `
    <span class="review-status ${cls}">${escapeHtml(label)}</span>
    <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
  `;
}

async function onReReview(ev) {
  const btn = ev.currentTarget;
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;

  try {
    const res = await fetch('/api/re-review', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }

  setRunning(card, 'Re-reviewing…', 're_review');
  streamJob(card, 're_review', repo, number, url, finishReReview);
}

function finishReReview(card, url, data) {
  const actions = card.querySelector('.pr-actions');
  if (data.status === 'stopped') {
    actions.innerHTML = `
      <span class="review-status stopped">⏹ Stopped</span>
      <button class="btn-re-review" type="button">Re-review again</button>
      <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
    `;
    actions.querySelector('.btn-re-review').addEventListener('click', onReReview);
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
  actions.innerHTML = `
    <span class="review-status ${cls}">${escapeHtml(label)}</span>
    <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
  `;
}

function toast(msg, error) {
  const el = document.createElement('div');
  el.className = 'toast' + (error ? ' error' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}

const TAB_TITLES = { incoming: '📋 PRs awaiting your review', mine: '🚀 My open PRs', deployed: '🚢 Currently deployed', status: '⚙️ App status', settings: '⚙️ Settings' };

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
    { key: 'CACHE_TTL', label: 'Cache TTL (seconds)', type: 'number',
      desc: 'How long per-PR detail data is cached before a background refresh.',
      value: c.cache_ttl ?? 30 },
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
      <div><button class="btn-settings-save" id="settingsSaveBtn">Save &amp; Reload</button></div>
    </div>`;
  document.getElementById('settingsSaveBtn').addEventListener('click', saveSettings);
}

async function saveSettings() {
  const btn = document.getElementById('settingsSaveBtn');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  const settings = {};
  for (const input of document.querySelectorAll('.settings-input')) {
    settings[input.dataset.key] = input.value.trim();
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
    """Update or append key=value in the .env file and os.environ."""
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if parsed.path == "/api/review":
            self._handle_review_post()
            return
        if parsed.path == "/api/re-review":
            self._handle_re_review_post()
            return
        if parsed.path == "/api/merge":
            self._handle_merge_post()
            return
        if parsed.path == "/api/address":
            self._handle_address_post()
            return
        if parsed.path == "/api/fix-pipeline":
            self._handle_fix_pipeline_post()
            return
        if parsed.path == "/api/rebase":
            self._handle_rebase_post()
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
        if parsed.path == "/api/open-dir":
            self._handle_open_dir_post()
            return
        self.send_error(404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def _handle_review_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            if "/" not in repo:
                raise ValueError("repo must be owner/name")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, "review")
        if started:
            threading.Thread(
                target=run_review, args=(job,), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": "review",
        })

    def _handle_re_review_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            if "/" not in repo:
                raise ValueError("repo must be owner/name")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, "re_review")
        if started:
            threading.Thread(
                target=run_re_review, args=(job,), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": "re_review",
        })

    def _handle_merge_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            default_method = str(data.get("defaultMergeMethod") or "MERGE")
            if "/" not in repo:
                raise ValueError("repo must be owner/name")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, "merge")
        if started:
            threading.Thread(
                target=run_merge, args=(job, default_method), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": "merge",
        })

    def _handle_address_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            head_ref = str(data["headRefName"])
            if "/" not in repo or not head_ref:
                raise ValueError("repo must be owner/name and headRefName required")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, "address")
        if started:
            threading.Thread(
                target=run_address, args=(job, head_ref), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": "address",
        })

    def _handle_fix_pipeline_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            head_ref = str(data["headRefName"])
            if "/" not in repo or not head_ref:
                raise ValueError("repo must be owner/name and headRefName required")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, "fix_pipeline")
        if started:
            threading.Thread(
                target=run_fix_pipeline, args=(job, head_ref), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": "fix_pipeline",
        })

    def _handle_rebase_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            head_ref = str(data["headRefName"])
            base_ref = str(data["baseRefName"])
            if "/" not in repo or not head_ref or not base_ref:
                raise ValueError("repo, headRefName, and baseRefName are required")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, "rebase")
        if started:
            threading.Thread(
                target=run_rebase, args=(job, head_ref, base_ref), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": "rebase",
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
            write_env_var(key, value)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        if key == "FRESH_REVIEWERS":
            FRESH_REVIEWERS = _env_list("FRESH_REVIEWERS")
        elif key == "TEAM_CHANNEL_ID":
            TEAM_CHANNEL_ID = value
        elif key == "DEPLOY_TARGET":
            DEPLOY_TARGET = value
        self._send_json(200, {"ok": True})

    def _handle_settings_post(self):
        global FRESH_REVIEWERS, TEAM_CHANNEL_ID, DEPLOY_TARGET, CACHE_TTL, EDITOR_CMD
        _allowed = {"FRESH_REVIEWERS", "TEAM_CHANNEL_ID", "DEPLOY_TARGET",
                    "CACHE_TTL", "HOST", "PORT", "EDITOR_CMD"}
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
        for key, value in settings.items():
            value = str(value).strip()
            if not value:
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

    def _handle_nudge_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            url = str(data["url"])
            title = str(data.get("title") or "")
            reviewers = data.get("reviewers") or []
            mode = str(data.get("mode") or "re_review")
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
                target=run_nudge, args=(job, url, title, reviewers, mode), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": "nudge",
        })


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
