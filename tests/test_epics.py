import unittest
import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, jira, prs


def _issue_with_parent(parent):
    return {"key": "CGP-96", "fields": {"parent": parent}}


def _epic_parent(key="CGP-90", summary="Big epic"):
    return {"key": key, "fields": {"summary": summary, "issuetype": {"name": "Epic"}}}


@mock.patch.object(config, "JIRA_SITE", "ex.atlassian.net")
class IssueEpicTest(unittest.TestCase):
    def setUp(self):
        jira._EPIC_CACHE.clear()

    @mock.patch("app.jira.jira_request")
    def test_returns_parent_epic(self, jr):
        jr.return_value = _issue_with_parent(_epic_parent())
        epic = jira.issue_epic("CGP-96")
        self.assertEqual(epic, {
            "key": "CGP-90",
            "summary": "Big epic",
            "url": "https://ex.atlassian.net/browse/CGP-90",
        })
        self.assertEqual(jr.call_args[0], ("GET", "/rest/api/3/issue/CGP-96"))
        self.assertEqual(jr.call_args[1]["params"], {"fields": "parent"})

    @mock.patch("app.jira.jira_request")
    def test_non_epic_parent_yields_none(self, jr):
        story_parent = {"key": "CGP-50", "fields": {"summary": "A story", "issuetype": {"name": "Story"}}}
        jr.return_value = _issue_with_parent(story_parent)
        self.assertIsNone(jira.issue_epic("CGP-96"))

    @mock.patch("app.jira.jira_request")
    def test_result_is_cached(self, jr):
        jr.return_value = _issue_with_parent(_epic_parent())
        jira.issue_epic("CGP-96")
        jira.issue_epic("CGP-96")
        self.assertEqual(jr.call_count, 1)

    @mock.patch("app.jira.jira_request")
    def test_missing_issue_caches_none(self, jr):
        jr.side_effect = jira.JiraError("not found", status=404)
        self.assertIsNone(jira.issue_epic("BOGUS-1"))
        jr.side_effect = None
        self.assertIsNone(jira.issue_epic("BOGUS-1"))  # cached — no new request
        self.assertEqual(jr.call_count, 1)

    @mock.patch("app.jira.jira_request")
    def test_other_errors_propagate(self, jr):
        jr.side_effect = jira.JiraError("boom", status=500)
        with self.assertRaises(jira.JiraError):
            jira.issue_epic("CGP-96")


@mock.patch.object(config, "JIRA_SITE", "ex.atlassian.net")
class IssueEpicsTest(unittest.TestCase):
    def setUp(self):
        jira._EPIC_CACHE.clear()

    @mock.patch("app.jira.issue_epic")
    def test_maps_each_key(self, ie):
        ie.side_effect = lambda k: {"key": "CGP-90"} if k == "CGP-96" else None
        out = jira.issue_epics(["CGP-96", "CGP-97"])
        self.assertEqual(out, {"CGP-96": {"key": "CGP-90"}, "CGP-97": None})

    @mock.patch("app.jira.issue_epic")
    def test_per_key_failure_yields_none(self, ie):
        ie.side_effect = jira.JiraError("boom", status=500)
        out = jira.issue_epics(["CGP-96"])
        self.assertEqual(out, {"CGP-96": None})

    def test_empty_keys_no_requests(self):
        self.assertEqual(jira.issue_epics([]), {})


class TicketKeyTest(unittest.TestCase):
    def test_slug_from_branch(self):
        self.assertEqual(prs.ticket_key({"headRefName": "CGP-96-add-filters", "title": "x"}), "CGP-96")

    def test_falls_back_to_title(self):
        self.assertEqual(prs.ticket_key({"headRefName": "fix-stuff", "title": "[NG-123] fix"}), "NG-123")

    def test_typed_branch_prefix(self):
        self.assertEqual(prs.ticket_key({"headRefName": "feat/NG-123-slug", "title": ""}), "NG-123")

    def test_no_slug_is_empty(self):
        self.assertEqual(prs.ticket_key({"headRefName": "misc-tweak", "title": "tweak"}), "")


def _pr(head="CGP-96-x", title="t"):
    return {"headRefName": head, "title": title, "number": 1}


class AttachEpicsTest(unittest.TestCase):
    @mock.patch("app.prs.jira")
    def test_attaches_epic_per_pr(self, jira_mod):
        jira_mod.jira_configured.return_value = True
        jira_mod.issue_epics.return_value = {"CGP-96": {"key": "CGP-90"}, "NG-5": None}
        out = prs.attach_epics([_pr(), _pr(head="feat/NG-5-y"), _pr(head="no-ticket", title="none")])
        self.assertEqual(out[0]["epic"], {"key": "CGP-90"})
        self.assertIsNone(out[1]["epic"])
        self.assertIsNone(out[2]["epic"])
        jira_mod.issue_epics.assert_called_once_with(["CGP-96", "NG-5"])

    @mock.patch("app.prs.jira")
    def test_unconfigured_jira_skips_lookup(self, jira_mod):
        jira_mod.jira_configured.return_value = False
        out = prs.attach_epics([_pr()])
        self.assertIsNone(out[0]["epic"])
        jira_mod.issue_epics.assert_not_called()

    @mock.patch("app.prs.jira")
    def test_jira_failure_never_breaks_list(self, jira_mod):
        jira_mod.jira_configured.return_value = True
        jira_mod.issue_epics.side_effect = RuntimeError("jira down")
        out = prs.attach_epics([_pr()])
        self.assertIsNone(out[0]["epic"])

    @mock.patch("app.prs.jira")
    def test_no_tickets_skips_lookup(self, jira_mod):
        jira_mod.jira_configured.return_value = True
        out = prs.attach_epics([_pr(head="no-ticket", title="none")])
        self.assertIsNone(out[0]["epic"])
        jira_mod.issue_epics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
