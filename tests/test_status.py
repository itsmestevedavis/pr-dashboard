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

    def test_draft_returns_none(self):
        self.assertIsNone(prs.determine_my_pr_status(pr(is_draft=True), self.me))

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
            self.me,
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


if __name__ == "__main__":
    unittest.main()
