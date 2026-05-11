import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import derive_address_result


def bash_event(cmd):
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}},
        ]},
    }


def tool_event(name, inp=None):
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": name, "input": inp or {}},
        ]},
    }


class DeriveAddressResult(unittest.TestCase):
    def test_no_action(self):
        self.assertEqual(derive_address_result([]), {
            "pushes": 0, "replies": 0, "rerequests": 0,
            "slack_dms": 0, "label": "No action",
        })

    def test_push_only(self):
        events = [bash_event("git push origin HEAD:foo")]
        out = derive_address_result(events)
        self.assertEqual(out["pushes"], 1)
        self.assertIn("Pushed 1", out["label"])

    def test_inline_reply(self):
        events = [bash_event(
            "gh api -X POST repos/acme/widgets/pulls/100/comments/200/replies -f body=ok"
        )]
        out = derive_address_result(events)
        self.assertEqual(out["replies"], 1)

    def test_general_pr_comment(self):
        events = [bash_event("gh pr comment 100 --repo acme/widgets --body 'ok'")]
        out = derive_address_result(events)
        self.assertEqual(out["replies"], 1)

    def test_rerequest(self):
        events = [bash_event(
            "gh api -X POST repos/acme/widgets/pulls/100/requested_reviewers "
            "-f 'reviewers[]=alice'"
        )]
        out = derive_address_result(events)
        self.assertEqual(out["rerequests"], 1)

    def test_full_workflow(self):
        events = [
            bash_event("git commit -m 'address review'"),
            bash_event("git push origin HEAD:foo"),
            bash_event(
                "gh api -X POST repos/acme/widgets/pulls/100/comments/200/replies "
                "-f body=ok"
            ),
            bash_event(
                "gh api -X POST repos/acme/widgets/pulls/100/requested_reviewers "
                "-f 'reviewers[]=alice'"
            ),
        ]
        out = derive_address_result(events)
        self.assertEqual(out["pushes"], 1)
        self.assertEqual(out["replies"], 1)
        self.assertEqual(out["rerequests"], 1)
        self.assertIn("1 commit", out["label"])
        self.assertIn("re-requested 1", out["label"])

    def test_replies_only_with_no_push(self):
        events = [bash_event("gh pr comment 100 --repo R --body x")]
        out = derive_address_result(events)
        self.assertEqual(out["label"], "Replied only")

    def test_slack_dm_counted(self):
        events = [tool_event("mcp__claude_ai_Slack__slack_send_message",
                             {"channel": "U123", "text": "ping"})]
        out = derive_address_result(events)
        self.assertEqual(out["slack_dms"], 1)

    def test_slack_draft_does_not_count(self):
        events = [tool_event("mcp__claude_ai_Slack__slack_send_message_draft",
                             {"channel": "U123", "text": "ping"})]
        out = derive_address_result(events)
        self.assertEqual(out["slack_dms"], 0)

    def test_full_workflow_with_slack(self):
        events = [
            bash_event("git push origin HEAD:foo"),
            bash_event(
                "gh api -X POST repos/acme/widgets/pulls/100/requested_reviewers "
                "-f 'reviewers[]=alice'"
            ),
            tool_event("mcp__claude_ai_Slack__slack_send_message",
                       {"channel": "U123", "text": "ping"}),
        ]
        out = derive_address_result(events)
        self.assertEqual(out["pushes"], 1)
        self.assertEqual(out["rerequests"], 1)
        self.assertEqual(out["slack_dms"], 1)
        self.assertIn("DM'd 1", out["label"])


if __name__ == "__main__":
    unittest.main()
