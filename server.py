#!/usr/bin/env python3
"""Local PR review dashboard.

Lists open GitHub PRs (from a fixed author list) that require my attention,
and lets me kick off a Claude Code review against each.
"""

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

    unresolved_inline_authors = set()
    for t in threads:
        if t.get("isResolved"):
            continue
        cnodes = (t.get("comments") or {}).get("nodes") or []
        if not cnodes:
            continue
        author = cnodes[0].get("author") or {}
        if not _is_human_author(author):
            continue
        login = author.get("login")
        if login and login not in approvers:
            unresolved_inline_authors.add(login)

    review_body_authors = set()
    for r in latest_reviews:
        if r.get("state") != "CHANGES_REQUESTED":
            continue
        author = r.get("author") or {}
        if not _is_human_author(author):
            continue
        login = author.get("login")
        if login:
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
        out_list.append({
            "number": pr.get("number"),
            "title": pr.get("title") or "",
            "url": pr.get("url") or "",
            "updatedAt": pr.get("updatedAt") or "",
            "headRefName": pr.get("headRefName") or "",
            "repository": repo,
            "defaultMergeMethod": (
                (pr.get("repository") or {}).get("viewerDefaultMergeMethod")
                or "MERGE"
            ),
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
REVIEW_WORKFLOW  = os.path.join(_WORKFLOW_DIR, "review_workflow.md")
ADDRESS_WORKFLOW = os.path.join(_WORKFLOW_DIR, "address_workflow.md")
NUDGE_WORKFLOW   = os.path.join(_WORKFLOW_DIR, "nudge_workflow.md")

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
  .btn-address:hover:not(:disabled) { background: #388bfd; }
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
  .status-list { list-style: none; padding: 0; margin: 0; }
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
    <button class="tab" data-tab="status">Status</button>
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
let currentTab = ['mine', 'status'].includes(_tab) ? _tab : 'incoming';

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
}

function renderIncomingPR(p) {
  const detail = p.status_detail
    ? `<div class="pr-detail">${escapeHtml(p.status_detail)}</div>` : '';
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
      <button class="btn-review" type="button">Review</button>
    </div>
  </div>`;
}

function renderMyPR(p) {
  const commenters = p.active_commenters && p.active_commenters.length
    ? `<div class="pr-detail">From: ${escapeHtml(p.active_commenters.join(', '))}</div>`
    : '';
  const targets = (p.nudge_targets || []).join(',');
  const mode = p.nudge_mode || '';
  let actionBtn = '';
  if (p.status === 'approved') {
    actionBtn = `<button class="btn-merge" type="button">Merge</button>`;
  } else if (p.status === 'has_comments') {
    actionBtn = `<button class="btn-address" type="button">Address</button>`;
  }
  const nudgeTitle = mode === 'fresh'
    ? 'DM Steve and Pratik to ask for first review'
    : (mode === 're_review'
        ? 'DM stale reviewers asking them to take another look'
        : 'No one to nudge');
  const nudgeBtn = `<button class="btn-nudge" type="button" title="${escapeHtml(nudgeTitle)}">Nudge</button>`;
  const channelBtn = `<button class="btn-channel" type="button" title="Post in team channel tagging Steve and Pratik">#Channel</button>`;
  const deployTarget = CONFIG.deploy_target || '';
  const deployWorkflow = deployTarget && (CONFIG.deploy_targets || {})[p.repository]?.[deployTarget];
  const deployControls = deployWorkflow
    ? `<button class="btn-deploy" type="button" data-env="${escapeHtml(deployTarget)}">Deploy to ${escapeHtml(deployTarget.toUpperCase())}</button>`
    : '';
  return `
  <div class="pr"
       data-number="${p.number}"
       data-repo="${escapeHtml(p.repository)}"
       data-url="${escapeHtml(p.url)}"
       data-title="${escapeHtml(p.title)}"
       data-head="${escapeHtml(p.headRefName)}"
       data-method="${escapeHtml(p.defaultMergeMethod)}"
       data-targets="${escapeHtml(targets)}"
       data-mode="${escapeHtml(mode)}">
    <div class="pr-main">
      <div class="pr-meta">${escapeHtml(p.repository)} · #${p.number}<span class="badge badge-${p.status}">${escapeHtml(p.status_label)}</span></div>
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

function toast(msg, error) {
  const el = document.createElement('div');
  el.className = 'toast' + (error ? ' error' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}

const TAB_TITLES = { incoming: '📋 PRs awaiting your review', mine: '🚀 My open PRs', status: '⚙️ App status' };

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

function renderStatus(checks) {
  const content = document.getElementById('content');
  const items = checks.map(c => {
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
  content.innerHTML = `<ul class="status-list">${items}</ul>`;
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

async function load(fresh) {
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true;
  document.getElementById('content').innerHTML = '<div class="empty">Loading…</div>';
  try {
    if (currentTab === 'status') {
      const res = await fetch('/api/status');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      renderStatus(await res.json());
    } else {
      const tab = TABS[currentTab];
      const url = tab.endpoint + (fresh ? '?fresh=1' : '');
      const res = await fetch(url);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      render(await res.json());
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
        (REVIEW_WORKFLOW,  "review_workflow.md",  "Review workflow instructions"),
        (ADDRESS_WORKFLOW, "address_workflow.md", "Address workflow instructions"),
        (NUDGE_WORKFLOW,   "nudge_workflow.md",   "Nudge workflow instructions"),
        (DEPLOY_TARGETS_PATH, "deploy_targets.json",
         "Deploy targets (repo → env → workflow name)"),
    ]:
        ok = os.path.isfile(wf_path)
        check(
            wf_name, wf_label, ok,
            workflow_excerpt(wf_path),
            fix={"action": "create_file", "path": wf_path} if not ok else None,
        )

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
            if kind not in ("review", "merge", "address", "nudge"):
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
        if parsed.path == "/api/merge":
            self._handle_merge_post()
            return
        if parsed.path == "/api/address":
            self._handle_address_post()
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

    def _handle_create_file_post(self):
        _defaults = {
            os.path.realpath(REVIEW_WORKFLOW):    _DEFAULT_REVIEW_WORKFLOW,
            os.path.realpath(ADDRESS_WORKFLOW):   _DEFAULT_ADDRESS_WORKFLOW,
            os.path.realpath(NUDGE_WORKFLOW):     _DEFAULT_NUDGE_WORKFLOW,
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
