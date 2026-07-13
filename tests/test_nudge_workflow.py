import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, workflows
from app.jobs import runners
from app.jobs.job import Job


class NudgeWorkflowWiring(unittest.TestCase):
    def test_default_template_covers_all_three_modes(self):
        wf = workflows._DEFAULT_NUDGE_WORKFLOW
        self.assertTrue(wf.strip())
        for mode in ("fresh", "re_review", "channel"):
            self.assertIn(mode, wf)
        # Channel mode must broadcast with <!here>, never @-mention individuals.
        self.assertIn("<!here>", wf)
        # The dashboard only counts a real send (never a draft) as success.
        self.assertIn("slack_send_message", wf)

    def test_workflow_path_lives_in_the_config_dir(self):
        self.assertEqual(
            os.path.dirname(config.NUDGE_WORKFLOW),
            config._WORKFLOW_DIR,
        )
        self.assertTrue(config.NUDGE_WORKFLOW.endswith("nudge_workflow.md"))


class RunNudgeMissingFile(unittest.TestCase):
    def setUp(self):
        self._orig = runners.NUDGE_WORKFLOW
        runners.NUDGE_WORKFLOW = "/nonexistent/pr-dashboard/nudge_workflow.md"

    def tearDown(self):
        runners.NUDGE_WORKFLOW = self._orig

    def test_hard_fails_when_workflow_file_absent(self):
        job = Job("acme/widgets", 1, "nudge")
        # Should return early (no claude subprocess) and mark the job failed.
        runners.run_nudge(
            job, url="https://x/pr/1", title="Fix",
            reviewers=["alice"], mode="fresh", channel_id="C123",
        )
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.result, "missing_workflow")


if __name__ == "__main__":
    unittest.main()
