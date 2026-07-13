"""app/team.py — "Team" tab orchestration.

Assembles the team-overview payload from the Jira client: resolve configured
teammates (email -> accountId, cached to disk), read the board's active sprint,
fetch each teammate's sprint tickets (or all open tickets when there's no active
sprint), bucket them by person, and derive the epics they roll up to.

All Jira credentials / config are read at call time via `config.<NAME>` so live
settings changes are picked up without a restart (same contract as app/jira.py).
"""

import json
import os

from app import config, jira

# Cache of resolved accounts, so we don't hit Jira user-search on every tab load.
# Sits alongside deploy_targets.json under the shared config dir.
_CACHE_PATH = os.path.join(os.path.expanduser("~/.config/pr-dashboard"), "jira_team.json")


def _load_cache():
    """Load the email -> {accountId, displayName} cache; {} if missing/corrupt."""
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache):
    """Persist the resolution cache, creating the config dir if needed."""
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    with open(_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def team_configured():
    """True only when Jira creds, a board id, and at least one teammate are set."""
    return bool(jira.jira_configured() and config.JIRA_BOARD_ID and config.JIRA_TEAM)


def resolve_members(emails):
    """Resolve a list of emails to member dicts, using (and updating) the cache.

    Each member: {email, accountId, displayName, unresolved}. A cache miss triggers
    exactly one Jira user-search per new email; the result is written back so the
    lookup happens at most once. Emails that don't resolve are flagged, not dropped.
    """
    cache = _load_cache()
    changed = False
    members = []
    for email in emails:
        entry = cache.get(email)
        if not entry:
            acct = jira.resolve_account(email)
            if acct and acct.get("accountId"):
                entry = {"accountId": acct["accountId"], "displayName": acct.get("displayName") or ""}
                cache[email] = entry
                changed = True
        if entry:
            members.append({
                "email": email,
                "accountId": entry["accountId"],
                "displayName": entry.get("displayName") or "",
                "unresolved": False,
            })
        else:
            members.append({"email": email, "accountId": None, "displayName": "", "unresolved": True})
    if changed:
        _save_cache(cache)
    return members


def team_overview():
    """Build the /api/team payload (see docs spec for the shape)."""
    if not team_configured():
        return {
            "configured": False,
            "jira_configured": jira.jira_configured(),
            "board_id_set": bool(config.JIRA_BOARD_ID),
            "team_set": bool(config.JIRA_TEAM),
            "sprint": None,
            "epics": [],
            "people": [],
        }

    members = resolve_members(config.JIRA_TEAM)
    account_ids = [m["accountId"] for m in members if not m["unresolved"]]

    sprint = jira.active_sprint(config.JIRA_BOARD_ID)
    if account_ids:
        tickets = jira.sprint_issues(sprint["id"], account_ids) if sprint else jira.open_issues(account_ids)
    else:
        tickets = []

    by_acct = {}
    for t in tickets:
        by_acct.setdefault(t.get("assignee"), []).append(t)

    people = [{
        "email": m["email"],
        "displayName": m["displayName"],
        "accountId": m["accountId"],
        "unresolved": m["unresolved"],
        "tickets": by_acct.get(m["accountId"], []),
    } for m in members]

    return {
        "configured": True,
        "board_id_set": True,
        "sprint": {"name": sprint["name"], "goal": sprint["goal"]} if sprint else None,
        "epics": jira.derive_epics(tickets),
        "people": people,
    }
