import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import standup


# ---- fixtures ----

_UNSET = object()  # sentinel so `sprint=None` can mean "no active sprint"


def _overview(people=None, sprint=_UNSET):
    """A team_overview()-shaped payload for prompt/summary tests."""
    return {
        "configured": True,
        "sprint": sprint if sprint is not _UNSET else {"name": "Sprint 24", "goal": "Ship the export fixes"},
        "epics": [],
        "people": people if people is not None else [
            {"email": "alice@x.com", "displayName": "Alice", "unresolved": False,
             "tickets": [{"key": "CGP-171", "status_label": "In Progress",
                          "summary": "Completed Status", "status_category": "indeterminate"}]},
            {"email": "steve.davis@cognota.com", "displayName": "Steve", "unresolved": False,
             "tickets": [{"key": "CGP-180", "status_label": "In Review",
                          "summary": "Export crash fix", "status_category": "indeterminate"}]},
        ],
    }


def _my_prs():
    return [{"title": "Fix export crash", "repository": "cognota/app",
             "status_label": "Approved", "review_decision": "APPROVED",
             "check_state": "SUCCESS", "behind_by": 2}]


# ---- _build_prompt (pure) ----

class BuildPromptTest(unittest.TestCase):
    ME = "steve.davis@cognota.com"

    def test_includes_sprint_goal(self):
        p = standup._build_prompt(_overview(), _my_prs(), self.ME)
        self.assertIn("Ship the export fixes", p)

    def test_includes_each_person_and_their_tickets(self):
        p = standup._build_prompt(_overview(), _my_prs(), self.ME)
        self.assertIn("Alice", p)
        self.assertIn("CGP-171", p)
        self.assertIn("Completed Status", p)
        self.assertIn("In Progress", p)

    def test_marks_me_on_my_own_line(self):
        p = standup._build_prompt(_overview(), _my_prs(), self.ME)
        # "(me)" must be attached to my own name in the data block, so claude knows
        # whose work the first-person line is about.
        self.assertIn("Steve (me)", p)
        self.assertNotIn("Alice (me)", p)

    def test_includes_my_prs_with_state(self):
        p = standup._build_prompt(_overview(), _my_prs(), self.ME)
        self.assertIn("Fix export crash", p)
        self.assertIn("Approved", p)

    def test_no_prs_says_none(self):
        p = standup._build_prompt(_overview(), [], self.ME)
        self.assertIn("(none)", p)

    def test_no_active_sprint_is_stated(self):
        p = standup._build_prompt(_overview(sprint=None), [], self.ME)
        self.assertIn("No active sprint", p)


# ---- _parse_result ----

class ParseResultTest(unittest.TestCase):
    def test_parses_plain_json(self):
        out = standup._parse_result('{"team": "Team is shipping.", "me": "I am on CGP-180."}')
        self.assertEqual(out, {"team": "Team is shipping.", "me": "I am on CGP-180."})

    def test_parses_json_wrapped_in_prose_or_fences(self):
        text = 'Sure! Here you go:\n```json\n{"team": "T", "me": "M"}\n```'
        out = standup._parse_result(text)
        self.assertEqual(out, {"team": "T", "me": "M"})

    def test_garbage_falls_back_to_raw_text_as_team_line(self):
        out = standup._parse_result("totally not json")
        self.assertEqual(out["team"], "totally not json")
        self.assertEqual(out["me"], "")


# ---- standup_summary ----

class StandupSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmp, "standup.json")
        self._patch = mock.patch.object(standup, "_CACHE_PATH", self.cache_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    @mock.patch.object(standup, "_run_claude")
    def test_force_false_with_cache_returns_cache_without_calling_claude(self, run):
        standup._save_cache({"generated_at": "2026-07-13T10:00:00+00:00",
                             "team": "cached team", "me": "cached me"})
        out = standup.standup_summary(force=False)
        run.assert_not_called()
        self.assertTrue(out["cached"])
        self.assertEqual(out["team"], "cached team")
        self.assertEqual(out["me"], "cached me")
        self.assertEqual(out["generated_at"], "2026-07-13T10:00:00+00:00")

    @mock.patch.object(standup, "_run_claude")
    def test_force_false_no_cache_returns_empty_without_calling_claude(self, run):
        out = standup.standup_summary(force=False)
        run.assert_not_called()
        self.assertFalse(out["cached"])
        self.assertEqual(out["team"], "")
        self.assertEqual(out["me"], "")
        self.assertIsNone(out["generated_at"])

    @mock.patch.object(standup, "prs")
    @mock.patch.object(standup, "team")
    @mock.patch.object(standup, "_run_claude")
    def test_force_true_generates_parses_and_caches(self, run, team_mod, prs_mod):
        team_mod.team_configured.return_value = True
        team_mod.team_overview.return_value = _overview()
        prs_mod.list_my_prs.return_value = _my_prs()
        run.return_value = '{"team": "Team shipping export fixes.", "me": "I am closing CGP-180."}'
        out = standup.standup_summary(force=True)
        run.assert_called_once()
        self.assertFalse(out["cached"])
        self.assertTrue(out["configured"])
        self.assertEqual(out["team"], "Team shipping export fixes.")
        self.assertEqual(out["me"], "I am closing CGP-180.")
        self.assertTrue(out["generated_at"])
        with open(self.cache_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["team"], "Team shipping export fixes.")
        self.assertEqual(saved["me"], "I am closing CGP-180.")

    @mock.patch.object(standup, "team")
    @mock.patch.object(standup, "_run_claude")
    def test_force_true_not_configured_returns_hint_without_claude(self, run, team_mod):
        team_mod.team_configured.return_value = False
        out = standup.standup_summary(force=True)
        run.assert_not_called()
        self.assertFalse(out["configured"])
        self.assertFalse(os.path.exists(self.cache_path))

    @mock.patch.object(standup, "prs")
    @mock.patch.object(standup, "team")
    @mock.patch.object(standup, "_run_claude")
    def test_force_true_claude_failure_returns_error_and_does_not_cache(self, run, team_mod, prs_mod):
        team_mod.team_configured.return_value = True
        team_mod.team_overview.return_value = _overview()
        prs_mod.list_my_prs.return_value = []
        run.side_effect = RuntimeError("claude exited 1: boom")
        out = standup.standup_summary(force=True)
        self.assertIsNotNone(out["error"])
        self.assertEqual(out["team"], "")
        self.assertEqual(out["me"], "")
        self.assertFalse(os.path.exists(self.cache_path))

    @mock.patch.object(standup, "prs")
    @mock.patch.object(standup, "team")
    @mock.patch.object(standup, "_run_claude")
    def test_my_pr_fetch_failure_still_generates_from_jira(self, run, team_mod, prs_mod):
        # A GitHub hiccup on the PR fetch must not sink the whole summary — the
        # personal line is enriched by PRs, not dependent on them.
        team_mod.team_configured.return_value = True
        team_mod.team_overview.return_value = _overview()
        prs_mod.list_my_prs.side_effect = RuntimeError("gh boom")
        run.return_value = '{"team": "Team shipping.", "me": "On CGP-180."}'
        out = standup.standup_summary(force=True)
        run.assert_called_once()
        self.assertIsNone(out["error"])
        self.assertEqual(out["team"], "Team shipping.")


if __name__ == "__main__":
    unittest.main()
