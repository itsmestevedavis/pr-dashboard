# pr-dashboard

A local PR dashboard server (`server.py`) that spawns headless `claude -p` jobs for
PR workflows. Launched via `./start.sh` (which `cd`s into this repo first, so jobs run
with their working directory here and this file loads as project memory).

## Nudging reviewers on Slack

This workflow is invoked by the `pr-dashboard` Nudge/#Channel buttons, which spawn
a headless `claude -p` session. The prompt gives you: a PR URL + title, a Reviewers
list (each entry is a Slack `@handle`, a Slack member ID like `U01ABC...`, or a bare
GitHub login), a Mode, and a Channel ID.

1. **Resolve each reviewer to a Slack user:**
   - If it starts with `@`, it's a Slack handle — DM that handle directly.
   - If it starts with `U`, it's a Slack member ID — use it directly.
   - Otherwise it's a GitHub login — look them up via the Slack MCP (search by the
     login / display name). Skip anyone you can't resolve and note it.
2. **Pick the message by Mode and SEND it for real** with the Slack send tool — the tool
   whose name contains `slack_send_message`. **Never use a `*draft*` variant** (the
   dashboard only counts a real `slack_send_message` call as a success).
   - **fresh** — DM each reviewer individually:
     `👋 Could you take a first look at my PR when you get a chance? <title> <url>`
   - **re_review** — DM each reviewer individually:
     `🙏 I've addressed your review comments on <title> — mind taking another look? <url>`
   - **channel** — post ONE message to the given Channel ID, opening with `<!here>`
     (do **not** @-mention the individual reviewers):
     `<!here> Review requested on <title>: <url>`
3. Send exactly one message per target. Never draft. When done, briefly report who you
   messaged (and anyone you couldn't resolve).
