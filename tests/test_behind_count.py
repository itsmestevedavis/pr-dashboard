import unittest
import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from server import pr_behind_count
from app import github


class PrBehindCount(unittest.TestCase):
    def test_parses_behind_by_from_compare(self):
        with mock.patch.object(github, "gh_run", return_value="61\n") as gh:
            self.assertEqual(pr_behind_count("o/r", "develop", "feature"), 61)
        # Reads behind_by from the compare endpoint for base...head.
        (called_args,), _ = gh.call_args
        self.assertIn("api", called_args)
        self.assertTrue(
            any("compare/develop...feature" in a for a in called_args),
            called_args,
        )

    def test_zero_when_up_to_date(self):
        with mock.patch.object(github, "gh_run", return_value="0\n"):
            self.assertEqual(pr_behind_count("o/r", "main", "feat"), 0)

    def test_zero_on_missing_ref_skips_gh_call(self):
        with mock.patch.object(
            github, "gh_run", side_effect=AssertionError("should not call gh")
        ):
            self.assertEqual(pr_behind_count("", "main", "feat"), 0)
            self.assertEqual(pr_behind_count("o/r", "", "feat"), 0)
            self.assertEqual(pr_behind_count("o/r", "main", ""), 0)

    def test_zero_on_gh_error(self):
        with mock.patch.object(github, "gh_run", side_effect=RuntimeError("404")):
            self.assertEqual(pr_behind_count("o/r", "main", "feat"), 0)

    def test_zero_on_null_output(self):
        with mock.patch.object(github, "gh_run", return_value="null\n"):
            self.assertEqual(pr_behind_count("o/r", "main", "feat"), 0)


if __name__ == "__main__":
    unittest.main()
