import unittest
import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, team


def _ticket(key, assignee, epic=None, status_label="In Progress"):
    return {"key": key, "summary": key, "assignee": assignee, "assignee_name": assignee,
            "status_label": status_label, "epic": epic, "url": f"https://x/browse/{key}"}


# ---- resolve_members: cache behaviour ----

class ResolveMembersTest(unittest.TestCase):
    @mock.patch("app.team._save_cache")
    @mock.patch("app.team._load_cache", return_value={"alice@x.com": {"accountId": "a1", "displayName": "Alice"}})
    @mock.patch("app.team.jira.resolve_account")
    def test_cache_hit_skips_api(self, resolve, _load, _save):
        members = team.resolve_members(["alice@x.com"])
        resolve.assert_not_called()
        self.assertEqual(members[0]["accountId"], "a1")
        self.assertFalse(members[0]["unresolved"])
        _save.assert_not_called()  # nothing changed

    @mock.patch("app.team._save_cache")
    @mock.patch("app.team._load_cache", return_value={})
    @mock.patch("app.team.jira.resolve_account", return_value={"accountId": "b2", "displayName": "Bob"})
    def test_cache_miss_resolves_and_persists(self, resolve, _load, save):
        members = team.resolve_members(["bob@x.com"])
        resolve.assert_called_once_with("bob@x.com")
        self.assertEqual(members[0]["accountId"], "b2")
        save.assert_called_once()
        self.assertEqual(save.call_args[0][0]["bob@x.com"]["accountId"], "b2")

    @mock.patch("app.team._save_cache")
    @mock.patch("app.team._load_cache", return_value={})
    @mock.patch("app.team.jira.resolve_account", return_value=None)
    def test_unresolvable_email_is_flagged(self, resolve, _load, _save):
        members = team.resolve_members(["ghost@x.com"])
        self.assertTrue(members[0]["unresolved"])
        self.assertIsNone(members[0]["accountId"])


# ---- team_overview ----

class TeamOverviewTest(unittest.TestCase):
    def _patch_config(self, team_emails=("alice@x.com", "bob@x.com"), board="123"):
        return [
            mock.patch.object(config, "JIRA_TEAM", list(team_emails)),
            mock.patch.object(config, "JIRA_BOARD_ID", board),
        ]

    @mock.patch("app.team.jira.jira_configured", return_value=False)
    def test_unconfigured_returns_configured_false(self, _jc):
        with mock.patch.object(config, "JIRA_TEAM", []), mock.patch.object(config, "JIRA_BOARD_ID", ""):
            out = team.team_overview()
        self.assertFalse(out["configured"])
        self.assertEqual(out["people"], [])

    @mock.patch("app.team.jira.jira_configured", return_value=True)
    @mock.patch("app.team.resolve_members")
    @mock.patch("app.team.jira.active_sprint", return_value={"id": 42, "name": "Sprint 24", "goal": "Ship it"})
    @mock.patch("app.team.jira.sprint_issues")
    def test_active_sprint_buckets_by_person(self, sprint_issues, _sprint, resolve_members, _jc):
        resolve_members.return_value = [
            {"email": "alice@x.com", "accountId": "a1", "displayName": "Alice", "unresolved": False},
            {"email": "bob@x.com", "accountId": "b2", "displayName": "Bob", "unresolved": False},
        ]
        epic = {"key": "CGP-1", "summary": "Alpha", "url": "u"}
        sprint_issues.return_value = [
            _ticket("CGP-10", "a1", epic), _ticket("CGP-11", "a1"), _ticket("CGP-12", "b2", epic),
        ]
        with mock.patch.object(config, "JIRA_TEAM", ["alice@x.com", "bob@x.com"]), \
             mock.patch.object(config, "JIRA_BOARD_ID", "123"):
            out = team.team_overview()

        self.assertTrue(out["configured"])
        self.assertEqual(out["sprint"], {"name": "Sprint 24", "goal": "Ship it"})
        people = {p["displayName"]: p for p in out["people"]}
        self.assertEqual([t["key"] for t in people["Alice"]["tickets"]], ["CGP-10", "CGP-11"])
        self.assertEqual([t["key"] for t in people["Bob"]["tickets"]], ["CGP-12"])
        self.assertEqual([e["key"] for e in out["epics"]], ["CGP-1"])
        sprint_issues.assert_called_once_with(42, ["a1", "b2"])

    @mock.patch("app.team.jira.jira_configured", return_value=True)
    @mock.patch("app.team.resolve_members")
    @mock.patch("app.team.jira.active_sprint", return_value=None)
    @mock.patch("app.team.jira.board_issues")
    def test_no_active_sprint_falls_back_to_board_issues(self, board_issues, _sprint, resolve_members, _jc):
        resolve_members.return_value = [
            {"email": "alice@x.com", "accountId": "a1", "displayName": "Alice", "unresolved": False},
        ]
        board_issues.return_value = [_ticket("CGP-20", "a1")]
        with mock.patch.object(config, "JIRA_TEAM", ["alice@x.com"]), \
             mock.patch.object(config, "JIRA_BOARD_ID", "123"):
            out = team.team_overview()
        self.assertIsNone(out["sprint"])
        board_issues.assert_called_once_with("123", ["a1"])
        self.assertEqual([t["key"] for t in out["people"][0]["tickets"]], ["CGP-20"])

    @mock.patch("app.team.jira.jira_configured", return_value=True)
    @mock.patch("app.team.resolve_members")
    @mock.patch("app.team.jira.active_sprint", return_value={"id": 42, "name": "S", "goal": ""})
    @mock.patch("app.team.jira.sprint_issues")
    def test_unresolved_member_excluded_from_query(self, sprint_issues, _sprint, resolve_members, _jc):
        resolve_members.return_value = [
            {"email": "alice@x.com", "accountId": "a1", "displayName": "Alice", "unresolved": False},
            {"email": "ghost@x.com", "accountId": None, "displayName": "", "unresolved": True},
        ]
        sprint_issues.return_value = [_ticket("CGP-30", "a1")]
        with mock.patch.object(config, "JIRA_TEAM", ["alice@x.com", "ghost@x.com"]), \
             mock.patch.object(config, "JIRA_BOARD_ID", "123"):
            out = team.team_overview()
        sprint_issues.assert_called_once_with(42, ["a1"])  # ghost excluded
        ghost = [p for p in out["people"] if p["unresolved"]][0]
        self.assertEqual(ghost["tickets"], [])


if __name__ == "__main__":
    unittest.main()
