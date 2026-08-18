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
    "draft": 3,
}

MY_STATUS_LABELS = {
    "approved": "Approved",
    "has_comments": "Has comments",
    "not_reviewed_yet": "Not reviewed yet",
    "draft": "Draft",
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

# Default deploy environment for all PRs (e.g. "dev-box"). Empty = no Deploy button shown.
DEPLOY_TARGET = os.environ.get("DEPLOY_TARGET", "")

# URL where the dev box serves the deployed app (VPN), linked from the Deployed
# tab. Empty = no link shown.
DEV_BOX_URL = os.environ.get("DEV_BOX_URL", "")

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

# ---- Team tab -------------------------------------------------------------
# Teammates to show on the Team tab, configured by email (comma list in .env)
# and resolved to Jira account ids (cached under ~/.config/pr-dashboard/).
JIRA_TEAM = _env_list("JIRA_TEAM")
# Numeric Scrum board id whose active sprint drives the Team tab's goal/epics.
# Team tab needs Jira creds + this + at least one teammate; else it shows a hint.
JIRA_BOARD_ID = os.environ.get("JIRA_BOARD_ID", "").strip()

# Jira issue keys look like ABC-123 — validate before interpolating into API paths.
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

# Logins of bot/machine *user* accounts whose comments must not count as human
# review feedback (e.g. an org's CognotaBot service account). GitHub App bots
# surface as __typename "Bot" and are filtered automatically; these are
# indistinguishable from real users by type, so they must be named explicitly.
# Comma list in .env, matched case-insensitively.
BOT_LOGINS = {s.lower() for s in _env_list("BOT_LOGINS")}


def _is_human_author(author):
    """True if the GraphQL author node is a real user (not a Bot, etc).

    Excludes two kinds of bots: GitHub Apps (__typename "Bot") and machine
    *user* accounts listed in BOT_LOGINS (which look like real users by type).
    """
    if not author:
        return False
    if (author.get("login") or "").lower() in BOT_LOGINS:
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
NUDGE_WORKFLOW         = os.path.join(_WORKFLOW_DIR, "nudge_workflow.md")

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

# Data block only — the how-to (mode templates, send rules) lives in the editable
# nudge_workflow.md, which run_nudge appends to this prompt (like REVIEW_PROMPT does).
NUDGE_PROMPT = (
    "I want to nudge these GitHub reviewers on Slack about my open PR:\n"
    "  PR: {url}\n"
    "  Title: {title}\n"
    "  Reviewers (GitHub logins, Slack @handles, or Slack member IDs): {reviewers}\n"
    "  Mode: {mode}\n"
    "  Channel ID: {channel}\n\n"
)

REBASE_PROMPT = (
    "Rebase PR #{number} in {repo} onto the base branch. "
    "Base branch: {base_ref}. "
    "PR head branch on origin: {head_ref}. "
    "Local branch in this worktree: {local_branch}. "
    "Push with: git push origin {local_branch}:{head_ref} --force-with-lease\n\n"
)

# Standup summary for the Team tab. Instructions only — the per-request data block
# (all tickets assigned to me across projects, my PRs) is appended by
# app/standup.py:_build_prompt. NOT interpolated with str.format because the JSON
# example below contains literal braces; the data is concatenated, not formatted.
STANDUP_PROMPT = (
    "You are helping me prepare for my team's daily standup. Below are the Jira "
    "tickets currently assigned to me — across every project (stories, bugs, "
    "chores, anything) — and my own open pull requests.\n\n"
    "Write one short update, grounded ONLY in the data below — do not invent work "
    "that isn't listed:\n"
    "\"me\": 1-3 sentences, first person, on what I'm working on — lead with "
    "in-progress tickets and the state of my open PRs (e.g. waiting on review, "
    "needs a rebase, checks failing), and mention chores or other one-off work "
    "worth calling out. Skip untouched backlog items. If I have nothing assigned, "
    "say so briefly.\n\n"
    "Respond with ONLY a JSON object and nothing else — no markdown, no code fence, "
    "no prose around it:\n"
    "{\"me\": \"...\"}"
)
