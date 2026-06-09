import json
import unittest
import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


class JiraRequestTest(unittest.TestCase):
    @mock.patch.object(server, "JIRA_API_TOKEN", "tok")
    @mock.patch.object(server, "JIRA_EMAIL", "me@example.com")
    @mock.patch.object(server, "JIRA_SITE", "ex.atlassian.net")
    @mock.patch("server.urllib.request.urlopen")
    def test_get_sets_auth_and_parses_json(self, urlopen):
        resp = mock.MagicMock()
        resp.read.return_value = b'{"ok": true}'
        resp.__enter__.return_value = resp
        urlopen.return_value = resp

        out = server.jira_request("GET", "/rest/api/3/myself")

        self.assertEqual(out, {"ok": True})
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://ex.atlassian.net/rest/api/3/myself")
        self.assertTrue(req.get_header("Authorization").startswith("Basic "))


def _issue(key="CSI-1234", summary="Do the thing", cat="indeterminate",
           status_name="In Progress", priority="High", itype="Story",
           updated="2026-06-09T10:00:00.000+0000"):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status_name, "statusCategory": {"key": cat}},
            "priority": {"name": priority} if priority else None,
            "issuetype": {"name": itype},
            "updated": updated,
        },
    }


class ParseTicketTest(unittest.TestCase):
    def test_maps_all_fields(self):
        t = server.parse_ticket(_issue(), "ex.atlassian.net")
        self.assertEqual(t["key"], "CSI-1234")
        self.assertEqual(t["summary"], "Do the thing")
        self.assertEqual(t["status"], "indeterminate")
        self.assertEqual(t["status_label"], "In Progress")
        self.assertEqual(t["priority"], "High")
        self.assertEqual(t["type"], "Story")
        self.assertEqual(t["url"], "https://ex.atlassian.net/browse/CSI-1234")
        self.assertEqual(t["updatedAt"], "2026-06-09T10:00:00.000+0000")

    def test_missing_priority_is_none(self):
        t = server.parse_ticket(_issue(priority=None), "ex.atlassian.net")
        self.assertIsNone(t["priority"])

    def test_missing_status_defaults_to_new(self):
        issue = {"key": "X-1", "fields": {"summary": "s"}}
        t = server.parse_ticket(issue, "ex.atlassian.net")
        self.assertEqual(t["status"], "new")


class JiraSearchTest(unittest.TestCase):
    @mock.patch.object(server, "JIRA_SITE", "ex.atlassian.net")
    @mock.patch("server.jira_request")
    def test_parses_issue_list(self, jr):
        jr.return_value = {"issues": [_issue(), _issue(key="CSI-2", cat="new")]}
        tickets = server.jira_search()
        self.assertEqual([t["key"] for t in tickets], ["CSI-1234", "CSI-2"])
        self.assertEqual(jr.call_args[0][0], "GET")
        self.assertEqual(jr.call_args[0][1], "/rest/api/3/search/jql")


class JiraConfiguredTest(unittest.TestCase):
    @mock.patch.object(server, "JIRA_API_TOKEN", "")
    @mock.patch.object(server, "JIRA_EMAIL", "me@example.com")
    @mock.patch.object(server, "JIRA_SITE", "ex.atlassian.net")
    def test_false_when_token_missing(self):
        self.assertFalse(server.jira_configured())

    @mock.patch.object(server, "JIRA_API_TOKEN", "tok")
    @mock.patch.object(server, "JIRA_EMAIL", "me@example.com")
    @mock.patch.object(server, "JIRA_SITE", "ex.atlassian.net")
    def test_true_when_all_present(self):
        self.assertTrue(server.jira_configured())


if __name__ == "__main__":
    unittest.main()
