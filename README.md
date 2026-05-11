# PR dashboard

Local web UI with two views over the user's GitHub PRs:

- **Awaiting my review** — PRs from a fixed author list that need a review.
- **My PRs** — the user's open PRs, grouped by review status. Approved PRs get a Merge button; PRs with comments get an Address button that spawns Claude in a dedicated agent clone to apply or reply to feedback.

## Setup

1. Clone:
   ```sh
   git clone https://github.com/<you>/pr-dashboard.git ~/pr-reviewer
   ```
2. Copy `.env.example` to `.env` and fill in:
   ```sh
   cp ~/pr-reviewer/.env.example ~/pr-reviewer/.env
   # then edit ~/pr-reviewer/.env
   ```
   - `AUTHORS` — comma-separated GitHub logins whose open PRs you want to surface in the "Awaiting my review" tab.
   - `FRESH_REVIEWERS` — GitHub logins to DM/tag when nobody has reviewed your PR yet.
   - `TEAM_CHANNEL_ID` — Slack channel ID the "#Channel" button posts in.
3. (Optional) symlink `~/.local/bin/prs → ~/pr-reviewer/start.sh` so `prs` works from any directory.

## Run

```sh
prs                       # if you symlinked it
# or:
bash ~/pr-reviewer/start.sh
```

Open http://127.0.0.1:8765 in a browser. Stop with `Ctrl+C`.

URL params: `?tab=mine` opens the My PRs view directly.

## Views

### Awaiting my review

Lists open PRs the user is asked to review, by a fixed author list. Clicking **Review** spawns `claude` with the existing PR-review workflow (see "PR Reviews" in the user's CLAUDE.md).

### My PRs

Lists open PRs where the user is the author. Three statuses, derived from GitHub's `reviewDecision` plus an "active commenters" set (anyone with unresolved comments who hasn't approved):

- **Approved** → green **Merge** button. Runs `gh pr merge <n> --<viewerDefaultMergeMethod>` (no `--auto`, no `--delete-branch`). Emulates the GitHub Merge button: requires checks green, respects the repo's branch-deletion setting.
- **Has comments** → blue **Address** button. Runs `claude` against a dedicated agent clone (see below) per the "Addressing PR comments" workflow in CLAUDE.md (evaluate each comment, then either fix-and-push or reply-and-decline, then re-request review from those whose comments were fixed).
- **Not reviewed yet** → no action button.
- **Nudge** → purple button on every row. DMs stale reviewers (or asks Steve+Pratik for a first review) on Slack via the Slack MCP. Mode and target list are computed server-side.

## Agent clone

The Address job runs in `~/.cache/pr-tools/clones/<owner>_<name>` — a dedicated clone, separate from any IDE workspace under `~/Desktop`. First Address job per repo clones from origin (~30-60s); subsequent jobs reuse the clone (sub-second prep). Per-repo mutex serializes simultaneous jobs against the same repo.

Your IDE clone (e.g. `~/Desktop/<repo>`) is never touched by the dashboard.

Logs persist at `/tmp/pr-tools/logs/`.

## Customizing

`.env` knobs (see `.env.example`):

| Variable          | What it changes                                                                |
|-------------------|--------------------------------------------------------------------------------|
| `AUTHORS`         | Comma-separated GitHub logins surfaced in the "Awaiting my review" tab.        |
| `FRESH_REVIEWERS` | Comma-separated GitHub logins to nudge when nobody has reviewed your PR yet.   |
| `TEAM_CHANNEL_ID` | Slack channel ID the "#Channel" button posts in.                               |
| `HOST`, `PORT`    | Local bind address (default 127.0.0.1:8765).                                   |
| `CACHE_TTL`       | Seconds the per-PR detail blob is cached for the incoming view (default 30).   |

Other knobs (at the top of `server.py`, edit directly):

| Constant            | What it changes                                                              |
|---------------------|------------------------------------------------------------------------------|
| `REVIEW_PROMPT`     | Prompt for Claude on Review.                                                 |
| `ADDRESS_PROMPT`    | Prompt for Claude on Address. Points at the CLAUDE.md workflow.              |
| `NUDGE_PROMPT`      | Prompt for Claude on Nudge. Points at the CLAUDE.md workflow.                |
| `AGENT_CLONES_DIR`  | Root for agent clones (default ~/.cache/pr-tools/clones).                    |
| `LOG_DIR`           | Root for stream logs.                                                        |

## Auto-sync

`scripts/auto-sync.py` is a tiny stdlib watcher that polls the repo every 2s and, after 5s of no further changes, runs `git add -A && git commit -m "auto: <timestamp>" && git push origin main`. Wire it as a macOS LaunchAgent for hands-off syncing:

```sh
cp scripts/com.example.pr-dashboard-sync.plist.template \
   ~/Library/LaunchAgents/com.example.pr-dashboard-sync.plist
# edit the plist: replace /Users/USER paths with your own
launchctl load ~/Library/LaunchAgents/com.example.pr-dashboard-sync.plist
```

Stop with `launchctl unload <plist>`. Logs at `/tmp/pr-dashboard-sync.log`.

## Tests

```sh
cd ~/pr-reviewer && python3 -m unittest discover -s tests -v
```

Unit tests cover the pure status/result-derivation logic. The HTTP/git/Claude integration is exercised manually.

## Requirements

- `gh` (authenticated)
- `git`
- `claude` CLI on PATH
- Python 3.8+
- For Slack nudges: the Slack MCP must be configured for `claude`.


