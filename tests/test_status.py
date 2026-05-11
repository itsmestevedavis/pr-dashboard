import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import determine_my_pr_status, FRESH_REVIEWERS


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


def thread(login, resolved=False, typename="User"):
    return {
        "isResolved": resolved,
        "comments": {"nodes": [{"author": {"login": login, "__typename": typename}}]},
    }


def comment(login, created_at="2026-05-01T00:00:00Z", typename="User"):
    return {"author": {"login": login, "__typename": typename}, "createdAt": created_at}


class DetermineMyPrStatus(unittest.TestCase):
    me = "abir-halwa"

    def test_draft_returns_none(self):
        self.assertIsNone(determine_my_pr_status(pr(is_draft=True), self.me))

    def test_approved_with_no_open_threads(self):
        out = determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")]),
            self.me,
        )
        self.assertEqual(out["status"], "approved")
        self.assertEqual(out["active_commenters"], [])

    def test_approved_but_unresolved_thread_from_non_approver(self):
        out = determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")],
               review_threads=[thread("bob", resolved=False)]),
            self.me,
        )
        self.assertEqual(out["status"], "has_comments")
        self.assertEqual(out["active_commenters"], ["bob"])

    def test_resolved_thread_doesnt_count(self):
        out = determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")],
               review_threads=[thread("bob", resolved=True)]),
            self.me,
        )
        self.assertEqual(out["status"], "approved")

    def test_thread_from_someone_who_then_approved_doesnt_count(self):
        out = determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")],
               review_threads=[thread("alice", resolved=False)]),
            self.me,
        )
        self.assertEqual(out["status"], "approved")

    def test_changes_requested_review_body(self):
        out = determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               latest_reviews=[review("alice", "CHANGES_REQUESTED")]),
            self.me,
        )
        self.assertEqual(out["status"], "has_comments")
        self.assertEqual(out["active_commenters"], ["alice"])

    def test_general_pr_comment_from_non_approver_counts(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               comments=[comment("alice")]),
            self.me,
        )
        self.assertEqual(out["status"], "has_comments")

    def test_my_own_general_comments_dont_count(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               comments=[comment(self.me)]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")

    def test_no_reviews_no_comments(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED"),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")

    def test_bot_general_comment_doesnt_count(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               comments=[comment("codacy-production", typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")
        self.assertEqual(out["active_commenters"], [])

    def test_bot_inline_comment_doesnt_count(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               review_threads=[thread("codacy-production", typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")

    def test_bot_changes_requested_review_doesnt_count(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               latest_reviews=[review("codacy-production", "CHANGES_REQUESTED",
                                      typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["status"], "not_reviewed_yet")

    def test_human_and_bot_mixed_keeps_only_human(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               comments=[comment("alice"),
                         comment("codacy-production", typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["status"], "has_comments")
        self.assertEqual(out["active_commenters"], ["alice"])

    def test_stale_reviewers_changes_requested(self):
        out = determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               latest_reviews=[review("alice", "CHANGES_REQUESTED")]),
            self.me,
        )
        self.assertEqual(out["stale_reviewers"], ["alice"])

    def test_stale_reviewers_commented_only(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               latest_reviews=[review("alice", "COMMENTED")]),
            self.me,
        )
        self.assertEqual(out["stale_reviewers"], ["alice"])

    def test_stale_reviewers_excludes_approvers(self):
        out = determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")]),
            self.me,
        )
        self.assertEqual(out["stale_reviewers"], [])

    def test_stale_reviewers_excludes_bots(self):
        out = determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               latest_reviews=[review("codacy-production", "CHANGES_REQUESTED",
                                      typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["stale_reviewers"], [])

    def test_stale_reviewers_when_no_reviews(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED"),
            self.me,
        )
        self.assertEqual(out["stale_reviewers"], [])

    def test_stale_reviewers_mixed_states(self):
        out = determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               latest_reviews=[
                   review("alice", "CHANGES_REQUESTED"),
                   review("bob", "APPROVED"),
                   review("carol", "COMMENTED"),
               ]),
            self.me,
        )
        self.assertEqual(out["stale_reviewers"], ["alice", "carol"])

    def test_nudge_re_review_when_stale_reviewers_exist(self):
        out = determine_my_pr_status(
            pr(review_decision="CHANGES_REQUESTED",
               latest_reviews=[review("alice", "CHANGES_REQUESTED")]),
            self.me,
        )
        self.assertEqual(out["nudge_mode"], "re_review")
        self.assertEqual(out["nudge_targets"], ["alice"])

    def test_nudge_fresh_when_no_human_reviews(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED"),
            self.me,
        )
        self.assertEqual(out["nudge_mode"], "fresh")
        self.assertEqual(out["nudge_targets"], list(FRESH_REVIEWERS))

    def test_nudge_fresh_ignores_bot_reviews(self):
        out = determine_my_pr_status(
            pr(review_decision="REVIEW_REQUIRED",
               latest_reviews=[review("codacy-production", "COMMENTED",
                                      typename="Bot")]),
            self.me,
        )
        self.assertEqual(out["nudge_mode"], "fresh")
        self.assertEqual(out["nudge_targets"], list(FRESH_REVIEWERS))

    def test_nudge_idle_when_everyone_approved(self):
        out = determine_my_pr_status(
            pr(review_decision="APPROVED",
               latest_reviews=[review("alice", "APPROVED")]),
            self.me,
        )
        self.assertIsNone(out["nudge_mode"])
        self.assertEqual(out["nudge_targets"], [])


if __name__ == "__main__":
    unittest.main()
