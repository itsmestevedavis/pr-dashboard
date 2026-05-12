# PR dashboard

Local web UI with two views over your GitHub PRs:

- **Awaiting my review** — all open PRs where you are a requested reviewer, pulled directly from GitHub's review queue.
- **My PRs** — your open PRs, grouped by review status. Approved PRs get a Merge button; PRs with comments get an Address button that spawns Claude in a dedicated agent clone to apply or reply to feedback.

## Requirements

- Python 3.8+
- `gh` CLI (authenticated)
- `claude` CLI on PATH
- For Slack nudges: the Slack MCP must be configured for `claude`

## Setup

1. Clone:
   ```sh
   git clone https://github.com/itsmestevedavis/pr-dashboard.git ~/pr-reviewer
   ```
2. Copy `.env.example` to `.env` and fill in:
   ```sh
   cp ~/pr-reviewer/.env.example ~/pr-reviewer/.env
   # then edit ~/pr-reviewer/.env
   ```
   - `FRESH_REVIEWERS` — GitHub logins to DM on Slack when nobody has reviewed your PR yet.
   - `TEAM_CHANNEL_ID` — Slack channel ID the "#Channel" button posts in.
3. (Optional) symlink so `prs` works from any directory:
   ```sh
   ln -s ~/pr-reviewer/start.sh ~/.local/bin/prs
   ```

## Run

```sh
prs                       # if you symlinked it
# or:
bash ~/pr-reviewer/start.sh
```

The browser opens automatically at http://127.0.0.1:8765. Stop with `Ctrl+C`.

The server watches `server.py` for changes and restarts automatically — no need to stop and restart while developing.

URL params: `?tab=mine` opens the My PRs view directly.

## Views

### Awaiting my review

Lists all open PRs where you are a requested reviewer, sourced directly from GitHub's review-requested queue. Clicking **Review** spawns `claude` to autonomously review the PR using the GitHub files API and post inline comments or an approval. A **Stop** button appears while the review is running so you can abort it mid-flight.

### My PRs

Lists your open PRs, grouped by status:

- **Approved** → green **Merge** button. Runs `gh pr merge` using the repo's default merge method.
- **Has comments** → blue **Address** button. Spawns `claude` in a dedicated agent clone to evaluate each comment, fix or reply, then re-request review.
- **Not reviewed yet** → no action button.
- **Nudge** → purple button on every row. DMs stale reviewers via Slack MCP, or asks `FRESH_REVIEWERS` for a first review.
- **#Channel** → posts in the configured team Slack channel tagging `FRESH_REVIEWERS`.

A **Stop** button appears on Address and Nudge jobs while they are running.

## Agent clone

The Address job runs in `~/.cache/pr-tools/clones/<owner>_<name>` — a dedicated clone separate from any IDE workspace. The first Address job per repo clones from origin (~30-60s); subsequent jobs reuse the clone. Per-repo mutex serializes simultaneous jobs against the same repo.

Logs persist at `/tmp/pr-reviewer/`.

## Customizing

`.env` knobs (see `.env.example`):

| Variable           | What it changes                                                              |
|--------------------|------------------------------------------------------------------------------|
| `FRESH_REVIEWERS`  | GitHub logins to DM on Slack when nobody has reviewed your PR yet.           |
| `TEAM_CHANNEL_ID`  | Slack channel ID the "#Channel" button posts in.                             |
| `HOST`, `PORT`     | Local bind address (default 127.0.0.1:8765).                                 |
| `CACHE_TTL`        | Seconds the per-PR detail blob is cached (default 30).                       |

Prompts and paths (edit at the top of `server.py`):

| Constant           | What it changes                                                              |
|--------------------|------------------------------------------------------------------------------|
| `REVIEW_PROMPT`    | Prompt given to Claude on Review.                                            |
| `ADDRESS_PROMPT`   | Prompt given to Claude on Address.                                           |
| `NUDGE_PROMPT`     | Prompt given to Claude on Nudge.                                             |
| `AGENT_CLONES_DIR` | Root for agent clones (default `~/.cache/pr-tools/clones`).                 |
| `LOG_DIR`          | Root for job logs (default `/tmp/pr-reviewer`).                              |

## Auto-sync

`scripts/auto-sync.py` polls the repo every 2s and, after 5s of no further changes, auto-commits and pushes. Wire it as a macOS LaunchAgent for hands-off syncing:

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

Unit tests cover the status and result-derivation logic. HTTP/git/Claude integration is exercised manually.
