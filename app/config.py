"""app/config.py — dependency-free configuration root.

Loads .env, defines all constants (immutable and LIVE_CONFIG), helper
functions, and all prompt strings.  Every other app module imports from here;
this module imports only stdlib (os, re) to prevent import cycles.
"""

import json
import os
import re

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


_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
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


# ---- Paths / directories ---------------------------------------------------

LOG_DIR = "/tmp/pr-reviewer"

_WORKFLOW_DIR = os.path.expanduser("~/.config/pr-dashboard")
REVIEW_WORKFLOW        = os.path.join(_WORKFLOW_DIR, "review_workflow.md")
ADDRESS_WORKFLOW       = os.path.join(_WORKFLOW_DIR, "address_workflow.md")
FIX_PIPELINE_WORKFLOW  = os.path.join(_WORKFLOW_DIR, "fix_pipeline_workflow.md")
REBASE_WORKFLOW        = os.path.join(_WORKFLOW_DIR, "rebase_workflow.md")
RE_REVIEW_WORKFLOW     = os.path.join(_WORKFLOW_DIR, "re_review_workflow.md")

# ---- Prompts ---------------------------------------------------------------

ADDRESS_PROMPT = (
    "Address review comments on PR #{number} in {repo}. "
    "PR head branch on origin: {head_ref}. "
    "Local branch in this worktree: {local_branch}. "
    "Push with: git push origin {local_branch}:{head_ref}\n\n"
)

FIX_PIPELINE_PROMPT = (
    "Fix the failing CI pipeline on PR #{number} in {repo}. "
    "PR head branch on origin: {head_ref}. "
    "Local branch in this worktree: {local_branch}. "
    "Push fixes with: git push origin {local_branch}:{head_ref}\n\n"
)

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

REBASE_PROMPT = (
    "Rebase PR #{number} in {repo} onto the base branch. "
    "Base branch: {base_ref}. "
    "PR head branch on origin: {head_ref}. "
    "Local branch in this worktree: {local_branch}. "
    "Push with: git push origin {local_branch}:{head_ref} --force-with-lease\n\n"
)
