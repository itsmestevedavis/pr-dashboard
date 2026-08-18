"""app/jira.py — Jira Cloud REST API integration.

All Jira credentials are read at call time via `config.<NAME>` so that live
configuration changes (LIVE_CONFIG) are picked up without a restart.
"""

import base64
import json
import urllib.error
import urllib.request
import urllib.parse

from app import config
from app.config import JIRA_JQL


# ---- Jira helpers ----------------------------------------------------------

class JiraError(RuntimeError):
    """Jira API failure. `status` is the upstream HTTP code (None for transport errors)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def jira_configured():
    """True only when all three Jira credentials are present."""
    return bool(config.JIRA_SITE and config.JIRA_EMAIL and config.JIRA_API_TOKEN)


def jira_request(method, path, params=None, body=None):
    """Call the Jira Cloud REST API. Returns parsed JSON (None for empty body).

    Raises RuntimeError with a readable message on any non-2xx or transport error.
    """
    url = f"https://{config.JIRA_SITE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    token = base64.b64encode(f"{config.JIRA_EMAIL}:{config.JIRA_API_TOKEN}".encode()).decode()
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
        raise JiraError(f"Jira {method} {path} failed (HTTP {e.code}): {detail}", status=e.code)
    except urllib.error.URLError as e:
        raise JiraError(f"Jira {method} {path} failed: {e.reason}")
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
    assignee = fields.get("assignee") or {}
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
        # Team-tab additions (additive; ignored by the personal Tickets tab).
        "assignee": assignee.get("accountId"),
        "assignee_name": assignee.get("displayName"),
        "epic": _parse_epic(fields.get("parent"), site),
    }


def _parse_epic(parent, site):
    """Return {key, summary, url} when the issue's parent is an Epic, else None.

    Uses the unified `fields.parent` link (modern Jira). A parent that is a Story
    or other non-Epic type (e.g. a sub-task's parent) is not an epic and yields None.
    """
    parent = parent or {}
    pfields = parent.get("fields") or {}
    ptype = (pfields.get("issuetype") or {}).get("name") or ""
    pkey = parent.get("key") or ""
    if not pkey or ptype != "Epic":
        return None
    return {
        "key": pkey,
        "summary": pfields.get("summary") or "",
        "url": f"https://{site}/browse/{pkey}",
    }


def jira_search():
    """Return assigned, not-done tickets as a list of ticket dicts."""
    data = jira_request("GET", "/rest/api/3/search/jql", params={
        "jql": JIRA_JQL,
        "fields": "summary,status,priority,issuetype,updated",
        "maxResults": "100",
    })
    issues = (data or {}).get("issues") or []
    return [parse_ticket(i, config.JIRA_SITE) for i in issues]


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


# ---- Team tab: account resolution, sprints, team issues, epics --------------

# Fields fetched for every team ticket (adds assignee + parent over the Tickets tab).
_TEAM_FIELDS = "summary,status,priority,issuetype,updated,assignee,parent"


def _assignee_in_clause(account_ids):
    """Build a JQL `assignee in ("id", ...)` clause from account ids.

    Account ids are quoted (they can contain characters like ':') so the clause
    is safe to interpolate.
    """
    quoted = ", ".join('"{}"'.format(a.replace('"', "")) for a in account_ids)
    return "assignee in ({})".format(quoted)


def resolve_account(email):
    """Resolve an email to {accountId, displayName} via Jira user search, or None.

    When the search returns several users, prefer an exact (case-insensitive)
    email match; otherwise fall back to the sole result. Never guesses between
    multiple ambiguous matches.
    """
    results = jira_request("GET", "/rest/api/3/user/search", params={"query": email}) or []
    if not results:
        return None
    want = (email or "").strip().lower()
    chosen = None
    for u in results:
        if (u.get("emailAddress") or "").strip().lower() == want:
            chosen = u
            break
    if chosen is None:
        if len(results) != 1:
            return None
        chosen = results[0]
    return {"accountId": chosen.get("accountId"), "displayName": chosen.get("displayName") or ""}


# Boards whose sprint endpoint 400'd ("does not support sprints") — remembered so
# every Team-tab load doesn't repeat a request that can only fail. Cleared when
# JIRA_BOARD_ID changes (see clear_sprint_support_cache).
_NO_SPRINT_BOARDS = set()


def clear_sprint_support_cache():
    """Forget which boards lack sprint support (call when JIRA_BOARD_ID changes)."""
    _NO_SPRINT_BOARDS.clear()


def active_sprint(board_id):
    """Return the board's active sprint as {id, name, goal}, or None.

    Only Scrum boards have sprints. A Scrum board between sprints yields an
    empty list; team-managed (Kanban-style) boards reject the endpoint outright
    with HTTP 400 "does not support sprints" — both mean None here (the 400 is
    remembered per board). Any other error (auth, network) propagates.
    """
    if board_id in _NO_SPRINT_BOARDS:
        return None
    try:
        data = jira_request(
            "GET", f"/rest/agile/1.0/board/{board_id}/sprint", params={"state": "active"}
        )
    except JiraError as e:
        if e.status == 400 and "does not support sprints" in str(e):
            _NO_SPRINT_BOARDS.add(board_id)
            return None
        raise
    values = (data or {}).get("values") or []
    if not values:
        return None
    s = values[0]
    return {"id": s.get("id"), "name": s.get("name") or "", "goal": s.get("goal") or ""}


def sprint_issues(sprint_id, account_ids):
    """Tickets in the given sprint assigned to any of account_ids (parsed)."""
    data = jira_request(
        "GET", f"/rest/agile/1.0/sprint/{sprint_id}/issue",
        params={"jql": _assignee_in_clause(account_ids), "fields": _TEAM_FIELDS, "maxResults": "100"},
    )
    issues = (data or {}).get("issues") or []
    return [parse_ticket(i, config.JIRA_SITE) for i in issues]


def board_issues(board_id, account_ids):
    """Fallback: the board's not-Done tickets assigned to account_ids (parsed).

    Used when the board has no active sprint (e.g. a team-managed board). Board-
    scoped on purpose: a bare JQL search would also surface teammates' tickets
    from every other project they touch.
    """
    jql = f"{_assignee_in_clause(account_ids)} AND statusCategory != Done ORDER BY updated DESC"
    data = jira_request(
        "GET", f"/rest/agile/1.0/board/{board_id}/issue",
        params={"jql": jql, "fields": _TEAM_FIELDS, "maxResults": "100"},
    )
    issues = (data or {}).get("issues") or []
    return [parse_ticket(i, config.JIRA_SITE) for i in issues]


def derive_epics(tickets):
    """Distinct epics referenced by a list of parsed tickets, first-seen order."""
    seen = {}
    for t in tickets:
        epic = t.get("epic")
        if epic and epic.get("key") and epic["key"] not in seen:
            seen[epic["key"]] = epic
    return list(seen.values())
