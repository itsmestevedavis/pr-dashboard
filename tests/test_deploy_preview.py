import unittest
import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import deploy, github


class PreviewBranchTest(unittest.TestCase):
    def test_preview_branch_naming(self):
        cases = [
            ("feat/NG-261-projects-table", "preview/NG-261-projects-table"),
            ("fix/NG-9-y", "preview/NG-9-y"),
            ("NG-123-standalone", "preview/NG-123-standalone"),
            ("random-branch", "preview/random-branch"),
            ("preview/NG-1-z", "preview/NG-1-z"),
        ]
        for head_ref, expected in cases:
            with self.subTest(head_ref=head_ref):
                self.assertEqual(deploy.preview_branch(head_ref), expected)


class PushPreviewTest(unittest.TestCase):
    @mock.patch.object(github, "gh_run")
    def test_updates_existing_ref(self, gh_run):
        gh_run.side_effect = [
            '{"object": {"sha": "abc123def"}}',  # sha lookup
            "{}",                                # PATCH ref succeeds
        ]
        out = deploy.push_preview("Cognota/next-gen", "feat/NG-261-x")
        self.assertEqual(out, {"branch": "preview/NG-261-x", "sha": "abc123def"})
        patch_args = gh_run.call_args_list[1][0][0]
        self.assertIn("repos/Cognota/next-gen/git/refs/heads/preview/NG-261-x", patch_args)
        self.assertIn("PATCH", patch_args)
        self.assertIn("force=true", patch_args)
        self.assertIn("sha=abc123def", patch_args)

    @mock.patch.object(github, "gh_run")
    def test_creates_ref_when_update_fails(self, gh_run):
        gh_run.side_effect = [
            '{"object": {"sha": "abc123def"}}',
            RuntimeError("gh api failed (exit 1): HTTP 422: Reference does not exist"),
            "{}",  # POST create succeeds
        ]
        out = deploy.push_preview("Cognota/next-gen", "feat/NG-261-x")
        self.assertEqual(out["branch"], "preview/NG-261-x")
        post_args = gh_run.call_args_list[2][0][0]
        self.assertIn("repos/Cognota/next-gen/git/refs", post_args)
        self.assertIn("POST", post_args)
        self.assertIn("ref=refs/heads/preview/NG-261-x", post_args)
        self.assertIn("sha=abc123def", post_args)

    @mock.patch.object(github, "gh_run")
    def test_sha_lookup_failure_raises(self, gh_run):
        gh_run.side_effect = RuntimeError("gh api failed (exit 1): HTTP 404: Not Found")
        with self.assertRaises(RuntimeError) as ctx:
            deploy.push_preview("Cognota/next-gen", "feat/NG-261-x")
        self.assertIn("Could not resolve feat/NG-261-x", str(ctx.exception))
        self.assertIn("404", str(ctx.exception))

    @mock.patch.object(github, "gh_run")
    def test_update_and_create_both_failing_raises(self, gh_run):
        gh_run.side_effect = [
            '{"object": {"sha": "abc123def"}}',
            RuntimeError("gh api failed (exit 1): HTTP 403: forbidden"),
            RuntimeError("gh api failed (exit 1): HTTP 403: forbidden"),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            deploy.push_preview("Cognota/next-gen", "feat/NG-261-x")
        self.assertIn("Could not push preview/NG-261-x", str(ctx.exception))
        self.assertIn("403", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
