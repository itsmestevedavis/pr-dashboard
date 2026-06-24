import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.prs import summarize_checks


def check_run(name, conclusion, status="COMPLETED", url="u"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": url,
    }


def status_context(name, state, url="u"):
    return {
        "__typename": "StatusContext",
        "context": name,
        "state": state,
        "targetUrl": url,
    }


def rollup(nodes, total=None):
    return {
        "state": "FAILURE",
        "contexts": {
            "totalCount": total if total is not None else len(nodes),
            "nodes": nodes,
        },
    }


class SummarizeChecks(unittest.TestCase):
    def test_empty_rollup(self):
        self.assertEqual(
            summarize_checks({}),
            {"passed": 0, "pending": [], "failed": [], "truncated": False},
        )

    def test_all_green(self):
        out = summarize_checks(rollup([
            check_run("build", "SUCCESS"),
            check_run("test", "SUCCESS"),
        ]))
        self.assertEqual(out["passed"], 2)
        self.assertEqual(out["pending"], [])
        self.assertEqual(out["failed"], [])

    def test_skipped_and_neutral_count_as_passed(self):
        out = summarize_checks(rollup([
            check_run("a", "SKIPPED"),
            check_run("b", "NEUTRAL"),
        ]))
        self.assertEqual(out["passed"], 2)

    def test_incomplete_check_run_is_pending(self):
        # No conclusion yet, status not COMPLETED -> pending regardless of conclusion field.
        out = summarize_checks(rollup([
            check_run("lint", None, status="IN_PROGRESS"),
            check_run("queued", "SUCCESS", status="QUEUED"),  # not COMPLETED -> still pending
        ]))
        self.assertEqual(out["passed"], 0)
        self.assertEqual([c["name"] for c in out["pending"]], ["lint", "queued"])

    def test_failure_buckets(self):
        out = summarize_checks(rollup([
            check_run("e2e", "FAILURE"),
            check_run("deploy", "TIMED_OUT"),
            check_run("x", "CANCELLED"),
        ]))
        self.assertEqual([c["name"] for c in out["failed"]], ["e2e", "deploy", "x"])

    def test_legacy_status_context_normalized(self):
        out = summarize_checks(rollup([
            status_context("ci/passing", "SUCCESS"),
            status_context("ci/broken", "ERROR"),
            status_context("ci/waiting", "PENDING"),
        ]))
        self.assertEqual(out["passed"], 1)
        self.assertEqual([c["name"] for c in out["failed"]], ["ci/broken"])
        self.assertEqual([c["name"] for c in out["pending"]], ["ci/waiting"])

    def test_mixed_checkrun_and_statuscontext(self):
        out = summarize_checks(rollup([
            check_run("build", "SUCCESS", url="b"),
            check_run("e2e", "FAILURE", url="e"),
            check_run("lint", None, status="IN_PROGRESS", url="l"),
            status_context("ci/legacy", "ERROR", url="c"),
        ]))
        self.assertEqual(out["passed"], 1)
        self.assertEqual(out["pending"], [{"name": "lint", "url": "l"}])
        self.assertEqual(
            out["failed"],
            [{"name": "e2e", "url": "e"}, {"name": "ci/legacy", "url": "c"}],
        )

    def test_truncated_when_total_exceeds_returned_nodes(self):
        out = summarize_checks(rollup([check_run("a", "SUCCESS")], total=150))
        self.assertTrue(out["truncated"])


if __name__ == "__main__":
    unittest.main()
