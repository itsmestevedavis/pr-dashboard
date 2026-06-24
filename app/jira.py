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
from app.config import JIRA_JQL, _JIRA_KEY_RE


# ---- Jira helpers ----------------------------------------------------------

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
