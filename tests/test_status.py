import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, prs


def pr(
    review_decision="REVIEW_REQUIRED",
    is_draft=False,
    latest_reviews=None,
    review_threads=None,
    comments=None,
):
    return {
        "isDraft": is_draft,
        "reviewDecision": review_decision,
        "latestReviews": {"nodes": latest_reviews or []},
        "reviewThreads": {"nodes": review_threads or []},
        "comments": {"nodes": comments or []},
    }


def review(login, state, submitted_at="2026-05-01T00:00:00Z", typename="User"):
    return {
        "author": {"login": login, "__typename": typename},
        "state": state,
        "submittedAt": submitted_at,
    }


def thread(login, resolved=False, typename="User", outdated=False, last_author=None):
    # `login` owns the thread (first comment). `last_author` is whoever commented
    # last; defaults to the owner for a single-comment thread.
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": [{"author": {"login": login, "__typename": typename}}]},
        "lastComment": {
            "nodes": [{"author": {"login": last_author or login, "__typename": "User"}}]
        },
    }


def comment(login, created_at="2026-05-01T00:00:00Z", typename="User"):
    return {"author": {"login": login, "__typename": typename}, "createdAt": created_at}


class DetermineMyPrStatus(unittest.TestCase):
    me = "abir-halwa"

    def test_draft_gets_draft_status(self):
        out = prs.determine_my_pr_status(pr(is_draft=True), self.me)
        self.assertEqual(out["status"], "draft")
        self.assertEqual(out["status_label"], "Draft")
        self.assertIsNone(out["nudge_mode"])
        self.assertEqual(out["nudge_targets"], [])
        self.assertEqual(out["active_commenters"], [])

    def test_draft_wins_over_review_state(self):
        # A draft isn't ready for review, so it stays in the Draft group even
        # when reviews already exist on it.
        out = prs.determine_my_pr_status(
            pr(is_draft=True, review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")]),
            self.me,
        )
        self.assertEqual(out["status"], "draft")

    def test_draft_sorts_after_every_other_status(self):
        others = [v for k, v in config.MY_STATUS_ORDER.items() if k != "draft"]
        self.assertGreater(config.MY_STATUS_ORDER["draft"], max(others))

    def test_approved_with_no_open_threads(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")]),
            self.me,
        )
        self.assertEqual(out["status"], "approved")
        self.assertEqual(out["active_commenters"], [])

    def test_approved_but_unresolved_thread_from_non_approver(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")],
               review_threads=[thread("bob", resolved=False)]),
            self.me,
        )
        self.assertEqual(out["status"], "has_comments")
        self.assertEqual(out["active_commenters"], ["bob"])

    def test_resolved_thread_doesnt_count(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")],
               review_threads=[thread("bob", resolved=True)]),
            self.me,
        )
        self.assertEqual(out["status"], "approved")

    def test_thread_from_someone_who_then_approved_doesnt_count(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")],
               review_threads=[thread("alice", resolved=False)]),
            self.me,
        )
        self.assertEqual(out["status"], "approved")

    def test_thread_addressed_when_author_replied_last(self):
        # Reviewer opened a thread; I replied last. Nothing left for me to
        # address — the ball is in the reviewer's court.
        out = prs.determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               review_threads=[thread("alice", resolved=False, last_author=self.me)]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")
        self.assertEqual(out["active_commenters"], [])

    def test_author_replied_last_thread_inactive(self):
        # Author got the last word on an unresolved thread → not my-court, no
        # active commenter, status falls back to not_reviewed_yet.
        out = prs.determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               review_threads=[thread("alice", resolved=False, last_author=self.me)]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")
        self.assertEqual(out["active_commenters"], [])

    def test_reviewer_replied_after_me_still_active(self):
        # Back-and-forth where the reviewer got the last word → still my court.
        out = prs.determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               review_threads=[thread("alice", resolved=False, last_author="alice")]),
            self.me,
        )
        self.assertEqual(out["status"], "has_comments")
        self.assertEqual(out["active_commenters"], ["alice"])

    def test_outdated_thread_doesnt_count(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               review_threads=[thread("alice", resolved=False, outdated=True)]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")
        self.assertEqual(out["active_commenters"], [])

    def test_changes_requested_review_body(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               latest_reviews=[review("alice", "CHANGES_REQUESTED")]),
            self.me,
        )
        self.assertEqual(out["status"], "has_comments")
        self.assertEqual(out["active_commenters"], ["alice"])

    def test_general_pr_comment_from_non_approver_counts(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               comments=[comment("alice")]),
            me="me",
        )
        self.assertEqual(out["status"], "has_comments")

    def test_my_own_general_comments_dont_count(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               comments=[comment(self.me)]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")

    def test_no_reviews_no_comments(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED"),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")

    def test_bot_general_comment_doesnt_count(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               comments=[comment("codacy-production", typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")
        self.assertEqual(out["active_commenters"], [])

    def test_bot_inline_comment_doesnt_count(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               review_threads=[thread("codacy-production", typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")

    def test_bot_changes_requested_review_doesnt_count(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               latest_reviews=[review("codacy-production", "CHANGES_REQUESTED",
                                      typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")

    def test_human_and_bot_mixed_keeps_only_human(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               comments=[comment("alice"),
                         comment("codacy-production", typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["status"], "has_comments")
        self.assertEqual(out["active_commenters"], ["alice"])


def _pr_with_reviews(latest_reviews_list):
    """Build a minimal PR fixture with the given latestReviews nodes.

    Uses the same dict shape as the existing `pr()` helper above but
    focuses on latestReviews, leaving everything else empty/default.
    """
    return pr(
        review_decision="REVIEW_REQUIRED",
        latest_reviews=latest_reviews_list,
    )


class NudgeFields(unittest.TestCase):
    """Tests for the stale_reviewers / nudge_mode / nudge_targets fields."""

    def test_stale_reviewer_drives_re_review_nudge(self):
        fixture = _pr_with_reviews([
            {"state": "CHANGES_REQUESTED", "author": {"login": "alice", "__typename": "User"}},
        ])
        out = prs.determine_my_pr_status(fixture, me="me")
        self.assertEqual(out["stale_reviewers"], ["alice"])
        self.assertEqual(out["nudge_mode"], "re_review")
        self.assertEqual(out["nudge_targets"], ["alice"])

    def test_commented_reviewer_drives_re_review_nudge(self):
        fixture = _pr_with_reviews([
            {"state": "COMMENTED", "author": {"login": "bob", "__typename": "User"}},
        ])
        out = prs.determine_my_pr_status(fixture, me="me")
        self.assertEqual(out["stale_reviewers"], ["bob"])
        self.assertEqual(out["nudge_mode"], "re_review")
        self.assertEqual(out["nudge_targets"], ["bob"])

    def test_general_commenter_drives_re_review_nudge(self):
        # A human general comment (no review submitted) keeps the PR in
        # has_comments even when approved — so the nudge must target that
        # commenter for another review, not go quiet.
        out = prs.determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")],
               comments=[comment("pratik12")]),
            me="me",
        )
        self.assertEqual(out["status"], "has_comments")
        self.assertEqual(out["stale_reviewers"], ["pratik12"])
        self.assertEqual(out["nudge_mode"], "re_review")
        self.assertEqual(out["nudge_targets"], ["pratik12"])

    def test_approvers_general_comment_does_not_nudge(self):
        # An approver who also left a comment has nothing left to re-review.
        out = prs.determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")],
               comments=[comment("alice")]),
            me="me",
        )
        self.assertEqual(out["status"], "approved")
        self.assertIsNone(out["nudge_mode"])

    def test_no_human_reviews_drives_fresh_nudge(self):
        original = config.FRESH_REVIEWERS
        config.FRESH_REVIEWERS = ["bob", "carol"]
        try:
            fixture = _pr_with_reviews([])
            out = prs.determine_my_pr_status(fixture, me="me")
            self.assertEqual(out["nudge_mode"], "fresh")
            self.assertEqual(out["nudge_targets"], ["bob", "carol"])
        finally:
            config.FRESH_REVIEWERS = original

    def test_me_excluded_from_stale_reviewers(self):
        fixture = _pr_with_reviews([
            {"state": "CHANGES_REQUESTED", "author": {"login": "me", "__typename": "User"}},
        ])
        out = prs.determine_my_pr_status(fixture, me="me")
        self.assertEqual(out["stale_reviewers"], [])
        self.assertIsNone(out["nudge_mode"])
        self.assertEqual(out["nudge_targets"], [])

    def test_bot_reviewer_excluded_from_stale(self):
        fixture = _pr_with_reviews([
            {"state": "CHANGES_REQUESTED", "author": {"login": "codecov", "__typename": "Bot"}},
        ])
        out = prs.determine_my_pr_status(fixture, me="me")
        self.assertEqual(out["stale_reviewers"], [])

    def test_approved_review_gives_none_nudge(self):
        fixture = _pr_with_reviews([
            {"state": "APPROVED", "author": {"login": "alice", "__typename": "User"}},
        ])
        out = prs.determine_my_pr_status(fixture, me="me")
        self.assertEqual(out["stale_reviewers"], [])
        self.assertIsNone(out["nudge_mode"])
        self.assertEqual(out["nudge_targets"], [])

    def test_stale_reviewers_sorted(self):
        fixture = _pr_with_reviews([
            {"state": "CHANGES_REQUESTED", "author": {"login": "zara", "__typename": "User"}},
            {"state": "COMMENTED", "author": {"login": "alice", "__typename": "User"}},
        ])
        out = prs.determine_my_pr_status(fixture, me="me")
        self.assertEqual(out["stale_reviewers"], ["alice", "zara"])
        self.assertEqual(out["nudge_targets"], ["alice", "zara"])


class BotLoginFiltering(unittest.TestCase):
    """A machine *user* account (GraphQL __typename 'User') named in BOT_LOGINS
    must not count as human review feedback — mirrors the CognotaBot case on
    Cognota/tasks#294, where a User-typename bot wrongly pinned the PR in
    'Has comments'."""

    me = "me"

    def setUp(self):
        self._orig = config.BOT_LOGINS
        config.BOT_LOGINS = {"cognotabot"}  # stored lower-cased, matched case-insensitively

    def tearDown(self):
        config.BOT_LOGINS = self._orig

    def test_is_human_author_excludes_user_typename_bot(self):
        # Same login, different membership in BOT_LOGINS.
        self.assertFalse(config._is_human_author({"login": "CognotaBot", "__typename": "User"}))
        self.assertTrue(config._is_human_author({"login": "realperson", "__typename": "User"}))
        # Case-insensitive.
        self.assertFalse(config._is_human_author({"login": "cognotaBOT", "__typename": "User"}))

    def test_bot_comment_does_not_make_pr_need_addressing(self):
        # A PR whose ONLY general comment is from the bot user → not "has_comments".
        out = prs.determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               comments=[comment("CognotaBot")]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")
        self.assertEqual(out["active_commenters"], [])

    def test_real_human_comment_still_counts(self):
        out = prs.determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               comments=[comment("realperson")]),
            self.me,
        )
        self.assertEqual(out["status"], "has_comments")
        self.assertEqual(out["active_commenters"], ["realperson"])


if __name__ == "__main__":
    unittest.main()
