import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import standup


# ---- fixtures ----

def _my_tickets():
    """A jira_search()-shaped list: my tickets across projects, not just CGP."""
    return [
        {"key": "CGP-180", "status_label": "In Review", "type": "Story",
         "summary": "Export crash fix", "status_category": "indeterminate"},
        {"key": "CP-10900", "status_label": "Delivery Backlog", "type": "Chore",
         "summary": "Configure SSO for Otis", "status_category": "new"},
    ]


def _my_prs():
    return [{"title": "Fix export crash", "repository": "cognota/app",
             "status_label": "Approved", "review_decision": "APPROVED",
             "check_state": "SUCCESS", "behind_by": 2}]


def _my_issues():
    """A github.list_my_issues()-shaped list: open issues assigned to me."""
    return [
        {"number": 3998, "title": "Currency picker loses selection",
         "repository": "Cognota/next-gen",
         "url": "https://github.com/Cognota/next-gen/issues/3998",
         "updatedAt": "2026-08-18T12:00:00Z"},
    ]


# ---- _build_prompt (pure) ----

class BuildPromptTest(unittest.TestCase):
    def test_includes_all_my_tickets_across_projects(self):
        # The whole point of sourcing from jira_search: CP chores and other
        # non-CGP tickets must appear alongside board tickets.
        p = standup._build_prompt(_my_tickets(), _my_prs(), _my_issues())
        self.assertIn("CGP-180", p)
        self.assertIn("Export crash fix", p)
        self.assertIn("CP-10900", p)
        self.assertIn("Configure SSO for Otis", p)

    def test_includes_status_and_type(self):
        p = standup._build_prompt(_my_tickets(), _my_prs(), _my_issues())
        self.assertIn("In Review", p)
        self.assertIn("(Chore)", p)

    def test_no_tickets_says_none(self):
        p = standup._build_prompt([], _my_prs(), _my_issues())
        self.assertIn("Tickets assigned to me", p)
        self.assertIn("- (none)", p)

    def test_includes_my_prs_with_state(self):
        p = standup._build_prompt(_my_tickets(), _my_prs(), _my_issues())
        self.assertIn("Fix export crash", p)
        self.assertIn("Approved", p)

    def test_no_prs_says_none(self):
        p = standup._build_prompt(_my_tickets(), [], _my_issues())
        self.assertIn("My open pull requests:\n- (none)", p)

    def test_includes_my_github_issues(self):
        # GitHub-assigned work (e.g. Cognota/next-gen#3998) must feed the
        # standup alongside Jira tickets — issues are tracked there too.
        p = standup._build_prompt(_my_tickets(), _my_prs(), _my_issues())
        self.assertIn("Cognota/next-gen#3998", p)
        self.assertIn("Currency picker loses selection", p)

    def test_no_issues_says_none(self):
        p = standup._build_prompt(_my_tickets(), _my_prs(), [])
        self.assertIn("GitHub issues assigned to me:\n- (none)", p)


# ---- _parse_result ----

class ParseResultTest(unittest.TestCase):
    def test_parses_plain_json(self):
        out = standup._parse_result('{"me": "I am on CGP-180."}')
        self.assertEqual(out, {"me": "I am on CGP-180."})

    def test_parses_json_wrapped_in_prose_or_fences(self):
        text = 'Sure! Here you go:\n```json\n{"me": "M"}\n```'
        out = standup._parse_result(text)
        self.assertEqual(out, {"me": "M"})

    def test_garbage_falls_back_to_raw_text_as_me_line(self):
        out = standup._parse_result("totally not json")
        self.assertEqual(out["me"], "totally not json")


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
                             "me": "cached me"})
        out = standup.standup_summary(force=False)
        run.assert_not_called()
        self.assertTrue(out["cached"])
        self.assertEqual(out["me"], "cached me")
        self.assertEqual(out["generated_at"], "2026-07-13T10:00:00+00:00")

    @mock.patch.object(standup, "_run_claude")
    def test_force_false_reads_me_from_old_two_line_cache(self, run):
        # Caches written before the team line was removed still have a "team"
        # key — the me line must still load; the stale team text is ignored.
        standup._save_cache({"generated_at": "2026-07-13T10:00:00+00:00",
                             "team": "old team line", "me": "cached me"})
        out = standup.standup_summary(force=False)
        run.assert_not_called()
        self.assertEqual(out["me"], "cached me")
        self.assertNotIn("team", out)

    @mock.patch.object(standup, "_run_claude")
    def test_force_false_no_cache_returns_empty_without_calling_claude(self, run):
        out = standup.standup_summary(force=False)
        run.assert_not_called()
        self.assertFalse(out["cached"])
        self.assertEqual(out["me"], "")
        self.assertIsNone(out["generated_at"])

    @mock.patch.object(standup, "github")
    @mock.patch.object(standup, "prs")
    @mock.patch.object(standup, "jira")
    @mock.patch.object(standup, "_run_claude")
    def test_force_true_generates_parses_and_caches(self, run, jira_mod, prs_mod, gh_mod):
        jira_mod.jira_configured.return_value = True
        jira_mod.jira_search.return_value = _my_tickets()
        prs_mod.list_my_prs.return_value = _my_prs()
        gh_mod.list_my_issues.return_value = _my_issues()
        run.return_value = '{"me": "I am closing CGP-180 and posting SQL for CP-10900."}'
        out = standup.standup_summary(force=True)
        run.assert_called_once()
        self.assertFalse(out["cached"])
        self.assertTrue(out["configured"])
        self.assertEqual(out["me"], "I am closing CGP-180 and posting SQL for CP-10900.")
        self.assertTrue(out["generated_at"])
        with open(self.cache_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["me"], "I am closing CGP-180 and posting SQL for CP-10900.")

    @mock.patch.object(standup, "jira")
    @mock.patch.object(standup, "_run_claude")
    def test_force_true_not_configured_returns_hint_without_claude(self, run, jira_mod):
        jira_mod.jira_configured.return_value = False
        out = standup.standup_summary(force=True)
        run.assert_not_called()
        self.assertFalse(out["configured"])
        self.assertFalse(os.path.exists(self.cache_path))

    @mock.patch.object(standup, "github")
    @mock.patch.object(standup, "prs")
    @mock.patch.object(standup, "jira")
    @mock.patch.object(standup, "_run_claude")
    def test_force_true_claude_failure_returns_error_and_does_not_cache(self, run, jira_mod, prs_mod, gh_mod):
        jira_mod.jira_configured.return_value = True
        jira_mod.jira_search.return_value = _my_tickets()
        prs_mod.list_my_prs.return_value = []
        gh_mod.list_my_issues.return_value = []
        run.side_effect = RuntimeError("claude exited 1: boom")
        out = standup.standup_summary(force=True)
        self.assertIsNotNone(out["error"])
        self.assertEqual(out["me"], "")
        self.assertFalse(os.path.exists(self.cache_path))

    @mock.patch.object(standup, "github")
    @mock.patch.object(standup, "prs")
    @mock.patch.object(standup, "jira")
    @mock.patch.object(standup, "_run_claude")
    def test_my_pr_fetch_failure_still_generates_from_jira(self, run, jira_mod, prs_mod, gh_mod):
        # A GitHub hiccup on the PR fetch must not sink the whole summary — the
        # personal line is enriched by PRs, not dependent on them.
        jira_mod.jira_configured.return_value = True
        jira_mod.jira_search.return_value = _my_tickets()
        prs_mod.list_my_prs.side_effect = RuntimeError("gh boom")
        gh_mod.list_my_issues.return_value = _my_issues()
        run.return_value = '{"me": "On CGP-180."}'
        out = standup.standup_summary(force=True)
        run.assert_called_once()
        self.assertIsNone(out["error"])
        self.assertEqual(out["me"], "On CGP-180.")

    @mock.patch.object(standup, "github")
    @mock.patch.object(standup, "prs")
    @mock.patch.object(standup, "jira")
    @mock.patch.object(standup, "_run_claude")
    def test_issue_fetch_failure_still_generates(self, run, jira_mod, prs_mod, gh_mod):
        # Same degradation contract as PRs: GitHub issues enrich the summary,
        # they must never sink it.
        jira_mod.jira_configured.return_value = True
        jira_mod.jira_search.return_value = _my_tickets()
        prs_mod.list_my_prs.return_value = _my_prs()
        gh_mod.list_my_issues.side_effect = RuntimeError("gh boom")
        run.return_value = '{"me": "On CGP-180."}'
        out = standup.standup_summary(force=True)
        run.assert_called_once()
        self.assertIsNone(out["error"])
        self.assertEqual(out["me"], "On CGP-180.")


if __name__ == "__main__":
    unittest.main()
