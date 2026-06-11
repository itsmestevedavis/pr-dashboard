import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cleanup


class FakeRunner:
    """Maps a git arg-tuple to (returncode, stdout, stderr)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((tuple(args), cwd))
        return self.responses.get(tuple(args), (0, "", ""))


class ParseGoneTest(unittest.TestCase):
    SAMPLE = (
        "* develop              abc1234 [origin/develop] latest\n"
        "  feature-merged       def5678 [origin/feature-merged: gone] old work\n"
        "  behind-only          bbb2222 [origin/behind-only: behind 2] wip\n"
        "  local-only           aaa1111 no upstream\n"
        "+ wt-branch             ccc3333 [origin/wt-branch: gone] in a worktree\n"
    )

    def test_finds_only_gone(self):
        self.assertEqual(
            cleanup.parse_gone(self.SAMPLE),
            ["feature-merged", "wt-branch"],
        )


class ParseWorktreesTest(unittest.TestCase):
    SAMPLE = (
        "worktree /home/me/repo\n"
        "HEAD aaa\n"
        "branch refs/heads/develop\n"
        "\n"
        "worktree /home/me/repo-wt/feature\n"
        "HEAD bbb\n"
        "branch refs/heads/feature-x\n"
        "\n"
        "worktree /home/me/repo-wt/detached\n"
        "HEAD ccc\n"
        "detached\n"
    )

    def test_parses_entries_and_marks_main(self):
        entries = cleanup.parse_worktrees(self.SAMPLE)
        self.assertEqual(len(entries), 3)
        self.assertTrue(entries[0]["is_main"])
        self.assertEqual(entries[0]["branch"], "develop")
        self.assertFalse(entries[1]["is_main"])
        self.assertEqual(entries[1]["branch"], "feature-x")
        self.assertEqual(entries[1]["path"], "/home/me/repo-wt/feature")
        self.assertTrue(entries[2]["detached"])
        self.assertIsNone(entries[2]["branch"])


class ParseMergedTest(unittest.TestCase):
    SAMPLE = "  feature-done\n* develop\n  another-done\n"

    def test_excludes_default_and_current(self):
        out = cleanup.parse_merged(self.SAMPLE, {"develop"})
        self.assertEqual(out, ["feature-done", "another-done"])


class ParseRemoteMergedTest(unittest.TestCase):
    SAMPLE = (
        "  origin/HEAD -> origin/develop\n"
        "  origin/develop\n"
        "  origin/feature-done\n"
        "  origin/release-1\n"
    )

    def test_strips_prefix_skips_head_and_default(self):
        out = cleanup.parse_remote_merged(self.SAMPLE, "develop")
        self.assertEqual(out, ["feature-done", "release-1"])


class DefaultBranchTest(unittest.TestCase):
    def test_from_symbolic_ref(self):
        run = FakeRunner({
            ("symbolic-ref", "refs/remotes/origin/HEAD"):
                (0, "refs/remotes/origin/develop\n", ""),
        })
        self.assertEqual(cleanup.default_branch(run, "/x"), "develop")

    def test_fallback_prefers_develop(self):
        run = FakeRunner({
            ("symbolic-ref", "refs/remotes/origin/HEAD"): (128, "", "no HEAD"),
            ("branch", "-a", "--format=%(refname:short)"):
                (0, "main\norigin/main\norigin/develop\n", ""),
        })
        self.assertEqual(cleanup.default_branch(run, "/x"), "develop")


class DeleteCandidateTest(unittest.TestCase):
    def test_local_safe_delete(self):
        run = FakeRunner({("branch", "-d", "--", "feat"): (0, "Deleted", "")})
        ok, err = cleanup.delete_candidate(
            run, {"kind": "local_merged", "repo_path": "/x", "name": "feat"})
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_local_force_uses_capital_d(self):
        run = FakeRunner({("branch", "-D", "--", "feat"): (0, "Deleted", "")})
        ok, err = cleanup.delete_candidate(
            run, {"kind": "local_gone", "repo_path": "/x", "name": "feat", "force": True})
        self.assertTrue(ok)
        self.assertEqual(run.calls[0][0], ("branch", "-D", "--", "feat"))

    def test_local_delete_reports_unmerged(self):
        run = FakeRunner({("branch", "-d", "--", "feat"):
                          (1, "", "error: branch 'feat' is not fully merged")})
        ok, err = cleanup.delete_candidate(
            run, {"kind": "local_merged", "repo_path": "/x", "name": "feat"})
        self.assertFalse(ok)
        self.assertIn("not fully merged", err)

    def test_worktree_remove(self):
        run = FakeRunner({("worktree", "remove", "--", "/wt/p"): (0, "", "")})
        ok, err = cleanup.delete_candidate(
            run, {"kind": "worktree", "repo_path": "/x", "name": "b", "worktree_path": "/wt/p"})
        self.assertTrue(ok)

    def test_worktree_remove_force(self):
        run = FakeRunner({("worktree", "remove", "--force", "--", "/wt/p"): (0, "", "")})
        ok, err = cleanup.delete_candidate(
            run, {"kind": "worktree", "repo_path": "/x", "name": "b",
                  "worktree_path": "/wt/p", "force": True})
        self.assertTrue(ok)

    def test_remote_delete(self):
        run = FakeRunner({("push", "origin", "--delete", "feat"): (0, "", "")})
        ok, err = cleanup.delete_candidate(
            run, {"kind": "remote_merged", "repo_path": "/x", "name": "feat"})
        self.assertTrue(ok)
        self.assertEqual(run.calls[0][0], ("push", "origin", "--delete", "feat"))

    def test_rejects_flag_like_name(self):
        run = FakeRunner({})
        ok, err = cleanup.delete_candidate(
            run, {"kind": "local_merged", "repo_path": "/x", "name": "--exec=evil"})
        self.assertFalse(ok)
        self.assertIn("flag", err)
        self.assertEqual(run.calls, [])  # never shelled out to git

    def test_rejects_flag_like_worktree_path(self):
        run = FakeRunner({})
        ok, err = cleanup.delete_candidate(
            run, {"kind": "worktree", "repo_path": "/x", "name": "b", "worktree_path": "--force"})
        self.assertFalse(ok)
        self.assertEqual(run.calls, [])


class BranchAuthorsTest(unittest.TestCase):
    def test_parses_and_lowercases(self):
        run = FakeRunner({
            ("for-each-ref", "--format=%(refname:short)%09%(authoremail)",
             "refs/heads", "refs/remotes"): (
                0,
                "develop\t<Me@Example.com>\n"
                "feature-x\t<other@example.com>\n"
                "origin/feature-x\t<other@example.com>\n",
                "",
            ),
        })
        authors = cleanup.branch_authors(run, "/x")
        self.assertEqual(authors["develop"], "me@example.com")
        self.assertEqual(authors["origin/feature-x"], "other@example.com")


class ScanRepoTest(unittest.TestCase):
    def _runner(self):
        return FakeRunner({
            ("symbolic-ref", "refs/remotes/origin/HEAD"): (0, "refs/remotes/origin/develop\n", ""),
            ("branch", "--show-current"): (0, "develop\n", ""),
            ("config", "user.email"): (0, "me@example.com\n", ""),
            ("for-each-ref", "--format=%(refname:short)%09%(authoremail)",
             "refs/heads", "refs/remotes"): (
                0,
                "develop\t<me@example.com>\n"
                "gone-branch\t<me@example.com>\n"
                "wt-feature\t<teammate@example.com>\n"
                "merged-branch\t<me@example.com>\n"
                "origin/old-remote\t<me@example.com>\n",
                "",
            ),
            ("branch", "-vv"): (
                0,
                "* develop        a [origin/develop] x\n"
                "  gone-branch    b [origin/gone-branch: gone] x\n"
                "  wt-feature     c [origin/wt-feature: gone] x\n",
                "",
            ),
            ("branch", "--merged", "develop"): (0, "  merged-branch\n* develop\n", ""),
            ("worktree", "list", "--porcelain"): (
                0,
                "worktree /repo\nHEAD a\nbranch refs/heads/develop\n\n"
                "worktree /repo-wt/wtf\nHEAD c\nbranch refs/heads/wt-feature\n",
                "",
            ),
            ("branch", "-r", "--merged", "origin/develop"): (
                0, "  origin/HEAD -> origin/develop\n  origin/develop\n  origin/old-remote\n", ""),
        })

    def test_classifies_candidates(self):
        run = self._runner()
        cands = cleanup.scan_repo(run, "/repo")
        by = {(c["kind"], c["name"]) for c in cands}
        # wt-feature is gone AND checked out in a linked worktree -> worktree candidate
        self.assertIn(("worktree", "wt-feature"), by)
        # gone-branch is a plain local gone branch
        self.assertIn(("local_gone", "gone-branch"), by)
        # merged-branch merged into default
        self.assertIn(("local_merged", "merged-branch"), by)
        # remote merged
        self.assertIn(("remote_merged", "old-remote"), by)
        # never the default branch, and wt-feature must NOT also appear as local_gone
        self.assertNotIn(("local_gone", "wt-feature"), by)
        self.assertNotIn(("local_merged", "develop"), by)

    def test_annotates_mine_by_tip_author(self):
        cands = {c["name"]: c for c in cleanup.scan_repo(self._runner(), "/repo")}
        self.assertTrue(cands["gone-branch"]["mine"])       # me@example.com
        self.assertTrue(cands["merged-branch"]["mine"])
        self.assertTrue(cands["old-remote"]["mine"])
        self.assertFalse(cands["wt-feature"]["mine"])       # teammate@example.com

    def test_author_email_override(self):
        cands = {c["name"]: c for c in
                 cleanup.scan_repo(self._runner(), "/repo", author_email="teammate@example.com")}
        self.assertTrue(cands["wt-feature"]["mine"])
        self.assertFalse(cands["gone-branch"]["mine"])


if __name__ == "__main__":
    unittest.main()
