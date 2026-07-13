import unittest
import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, jira


# ---- shared issue factory (mirrors tests/test_tickets.py, adds assignee/parent) ----

def _issue(key="CGP-100", summary="Do the thing", cat="indeterminate",
           status_name="In Progress", priority="High", itype="Story",
           updated="2026-07-09T10:00:00.000+0000",
           assignee_id="acct-alice", assignee_name="Alice",
           parent=None):
    fields = {
        "summary": summary,
        "status": {"name": status_name, "statusCategory": {"key": cat}},
        "priority": {"name": priority} if priority else None,
        "issuetype": {"name": itype},
        "updated": updated,
        "assignee": {"accountId": assignee_id, "displayName": assignee_name} if assignee_id else None,
    }
    if parent is not None:
        fields["parent"] = parent
    return {"key": key, "fields": fields}


def _epic_parent(key="CGP-1", summary="Completed Status"):
    return {"key": key, "fields": {"summary": summary, "issuetype": {"name": "Epic"}}}


# ---- parse_ticket additions: assignee + epic ----

class ParseTicketAssigneeEpicTest(unittest.TestCase):
    SITE = "ex.atlassian.net"

    def test_extracts_assignee_account_and_name(self):
        t = jira.parse_ticket(_issue(), self.SITE)
        self.assertEqual(t["assignee"], "acct-alice")
        self.assertEqual(t["assignee_name"], "Alice")

    def test_unassigned_issue_has_none_assignee(self):
        t = jira.parse_ticket(_issue(assignee_id=None), self.SITE)
        self.assertIsNone(t["assignee"])

    def test_epic_parent_populates_epic(self):
        t = jira.parse_ticket(_issue(parent=_epic_parent()), self.SITE)
        self.assertEqual(t["epic"], {
            "key": "CGP-1",
            "summary": "Completed Status",
            "url": "https://ex.atlassian.net/browse/CGP-1",
        })

    def test_non_epic_parent_is_ignored(self):
        story_parent = {"key": "CGP-9", "fields": {"summary": "A story", "issuetype": {"name": "Story"}}}
        t = jira.parse_ticket(_issue(parent=story_parent), self.SITE)
        self.assertIsNone(t["epic"])

    def test_no_parent_means_no_epic(self):
        t = jira.parse_ticket(_issue(), self.SITE)
        self.assertIsNone(t["epic"])


# ---- resolve_account ----

class ResolveAccountTest(unittest.TestCase):
    @mock.patch("app.jira.jira_request")
    def test_single_result_is_returned(self, jr):
        jr.return_value = [{"accountId": "a1", "displayName": "Alice", "emailAddress": "alice@x.com"}]
        out = jira.resolve_account("alice@x.com")
        self.assertEqual(out, {"accountId": "a1", "displayName": "Alice"})
        self.assertEqual(jr.call_args[0][0], "GET")
        self.assertEqual(jr.call_args[0][1], "/rest/api/3/user/search")
        self.assertEqual(jr.call_args[1]["params"]["query"], "alice@x.com")

    @mock.patch("app.jira.jira_request")
    def test_prefers_exact_email_match_when_multiple(self, jr):
        jr.return_value = [
            {"accountId": "a2", "displayName": "Alicia", "emailAddress": "alicia@x.com"},
            {"accountId": "a1", "displayName": "Alice", "emailAddress": "alice@x.com"},
        ]
        out = jira.resolve_account("alice@x.com")
        self.assertEqual(out["accountId"], "a1")

    @mock.patch("app.jira.jira_request")
    def test_no_results_returns_none(self, jr):
        jr.return_value = []
        self.assertIsNone(jira.resolve_account("ghost@x.com"))


# ---- active_sprint ----

class ActiveSprintTest(unittest.TestCase):
    @mock.patch("app.jira.jira_request")
    def test_returns_first_active_sprint_with_goal(self, jr):
        jr.return_value = {"values": [
            {"id": 42, "name": "Sprint 24", "goal": "Ship it", "state": "active"},
        ]}
        out = jira.active_sprint("123")
        self.assertEqual(out, {"id": 42, "name": "Sprint 24", "goal": "Ship it"})
        self.assertEqual(jr.call_args[0], ("GET", "/rest/agile/1.0/board/123/sprint"))
        self.assertEqual(jr.call_args[1]["params"]["state"], "active")

    @mock.patch("app.jira.jira_request")
    def test_missing_goal_becomes_empty_string(self, jr):
        jr.return_value = {"values": [{"id": 42, "name": "Sprint 24"}]}
        out = jira.active_sprint("123")
        self.assertEqual(out["goal"], "")

    @mock.patch("app.jira.jira_request")
    def test_no_active_sprint_returns_none(self, jr):
        jr.return_value = {"values": []}
        self.assertIsNone(jira.active_sprint("123"))


# ---- sprint_issues / open_issues ----

class SprintIssuesTest(unittest.TestCase):
    @mock.patch.object(config, "JIRA_SITE", "ex.atlassian.net")
    @mock.patch("app.jira.jira_request")
    def test_builds_assignee_jql_and_parses(self, jr):
        jr.return_value = {"issues": [_issue(key="CGP-100"), _issue(key="CGP-101", assignee_id="acct-bob", assignee_name="Bob")]}
        out = jira.sprint_issues(42, ["acct-alice", "acct-bob"])
        self.assertEqual([t["key"] for t in out], ["CGP-100", "CGP-101"])
        self.assertEqual(jr.call_args[0], ("GET", "/rest/agile/1.0/sprint/42/issue"))
        jql = jr.call_args[1]["params"]["jql"]
        self.assertIn("assignee in (", jql)
        self.assertIn('"acct-alice"', jql)
        self.assertIn('"acct-bob"', jql)


class OpenIssuesTest(unittest.TestCase):
    @mock.patch.object(config, "JIRA_SITE", "ex.atlassian.net")
    @mock.patch("app.jira.jira_request")
    def test_open_issues_filters_not_done(self, jr):
        jr.return_value = {"issues": [_issue()]}
        out = jira.open_issues(["acct-alice"])
        self.assertEqual([t["key"] for t in out], ["CGP-100"])
        self.assertEqual(jr.call_args[0][1], "/rest/api/3/search/jql")
        jql = jr.call_args[1]["params"]["jql"]
        self.assertIn("statusCategory != Done", jql)
        self.assertIn('"acct-alice"', jql)


# ---- derive_epics ----

class DeriveEpicsTest(unittest.TestCase):
    SITE = "ex.atlassian.net"

    def test_collects_distinct_epics(self):
        tickets = [
            jira.parse_ticket(_issue(key="A-1", parent=_epic_parent("CGP-1", "Alpha")), self.SITE),
            jira.parse_ticket(_issue(key="A-2", parent=_epic_parent("CGP-1", "Alpha")), self.SITE),  # dup epic
            jira.parse_ticket(_issue(key="A-3", parent=_epic_parent("CGP-2", "Beta")), self.SITE),
            jira.parse_ticket(_issue(key="A-4"), self.SITE),  # no epic
        ]
        epics = jira.derive_epics(tickets)
        self.assertEqual([e["key"] for e in epics], ["CGP-1", "CGP-2"])
        self.assertEqual(epics[0]["summary"], "Alpha")


if __name__ == "__main__":
    unittest.main()
