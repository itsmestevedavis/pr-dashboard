import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.prs import determine_status

ME = "itsmestevedavis"


def detail(latest_reviews=None, reviews=None, author="someauthor"):
    """Minimal `gh pr view` detail blob: I'm review-requested, no review of mine."""
    return {
        "author": {"login": author},
        "reviews": reviews or [],
        "reviewRequests": [{"login": ME}],
        "commits": [],
        "latestReviews": latest_reviews or [],
    }


class OtherVerdictsSkip(unittest.TestCase):
    """The 'no pile-on' skip must only count verdicts from human reviewers."""

    def test_untouched_pr_with_no_other_reviews_is_included(self):
        result = determine_status("o/r", 1, detail(), ME, fresh=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "untouched")

    def test_human_approval_excludes_pr(self):
        d = detail(latest_reviews=[
            {"author": {"login": "michaelsynapse2022"}, "state": "APPROVED"},
        ])
        self.assertIsNone(determine_status("o/r", 1, d, ME, fresh=False))

    def test_bot_login_approval_does_not_exclude_pr(self):
        d = detail(latest_reviews=[
            {"author": {"login": "cognota-ai-pr-review-gate"}, "state": "APPROVED"},
        ])
        with mock.patch.object(config, "BOT_LOGINS", {"cognota-ai-pr-review-gate"}):
            result = determine_status("o/r", 1, d, ME, fresh=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "untouched")

    def test_bot_typename_approval_does_not_exclude_pr(self):
        d = detail(latest_reviews=[
            {"author": {"login": "some-app", "__typename": "Bot"}, "state": "APPROVED"},
        ])
        result = determine_status("o/r", 1, d, ME, fresh=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "untouched")

    def test_bot_approval_alongside_human_verdict_still_excludes(self):
        d = detail(latest_reviews=[
            {"author": {"login": "cognota-ai-pr-review-gate"}, "state": "APPROVED"},
            {"author": {"login": "realperson"}, "state": "CHANGES_REQUESTED"},
        ])
        with mock.patch.object(config, "BOT_LOGINS", {"cognota-ai-pr-review-gate"}):
            self.assertIsNone(determine_status("o/r", 1, d, ME, fresh=False))


if __name__ == "__main__":
    unittest.main()
