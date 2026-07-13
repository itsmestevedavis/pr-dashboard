"""app/jobs/runners.py — Claude job runners for all PR workflow actions."""

import json
import os
import re
import subprocess
import time

from app import config, workflows
from app import github
from app.jobs import clones, events
from app.config import (
    LOG_DIR,
    REVIEW_PROMPT, RE_REVIEW_PROMPT,
    ADDRESS_PROMPT, FIX_PIPELINE_PROMPT, NUDGE_PROMPT, REBASE_PROMPT,
    REVIEW_WORKFLOW, RE_REVIEW_WORKFLOW,
    ADDRESS_WORKFLOW, FIX_PIPELINE_WORKFLOW, REBASE_WORKFLOW, NUDGE_WORKFLOW,
)

CLAUDE_BASE_ARGS = [
    "--permission-mode", "bypassPermissions",
    "--output-format", "stream-json",
    "--verbose",
]

_RE_GIT_PUSH = re.compile(r"(^|[\s;&|])git\s+push\b")
_RE_INLINE_REPLY = re.compile(r"gh\s+api[^|;&]*\bpulls/\d+/comments/\d+/replies\b")
_RE_GENERAL_PR_COMMENT = re.compile(r"gh\s+pr\s+comment\b")
_RE_RERQUEST = re.compile(r"gh\s+api[^|;&]*\bpulls/\d+/requested_reviewers\b")


def _job_log_path(prefix, repo, number):
    """Build (and ensure the dir for) a per-job log path."""
    os.makedirs(LOG_DIR, exist_ok=True)
    return f"{LOG_DIR}/{prefix}{clones.repo_flat(repo)}-{number}-{int(time.time())}.log"


def _fill_refs(text, **refs):
    """Substitute {key} placeholders in a user-editable workflow body.

    Unlike str.format, this only touches the named keys via literal replace, so
    stray braces in the file (JSON snippets, shell ${VAR}, etc.) pass through
    untouched instead of raising KeyError/ValueError and killing the job.
    """
    for key, val in refs.items():
        text = text.replace("{" + key + "}", val)
    return text


def _stream_claude_job(job, prompt, log_path, *, cwd=None, stop_verb="Stopped."):
    """Spawn `claude -p`, stream stdout to the log file and job, handle exit.

    Returns the list of parsed JSON events on success (exit 0). Returns None if
    the job has already been finished here (spawn failure, stream error, stop, or
    non-zero exit) — callers should return immediately without deriving a result.
    """
    stream_events = []
    try:
        proc = subprocess.Popen(
            ["claude", "-p", prompt, *CLAUDE_BASE_ARGS],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
        )
    except Exception as e:
        job.append(f"Failed to spawn claude: {e}")
        job.finish("failed", "spawn_error")
        return None

    job.proc = proc

    try:
        with open(log_path, "w") as logf:
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stream_events.append(ev)
                friendly = events.format_event(ev)
                if friendly:
                    job.append(friendly)
        proc.wait()
    except Exception as e:
        job.append(f"Stream error: {e}")
        job.finish("failed", "stream_error")
        return None

    if proc.returncode != 0:
        if job._stop_requested:
            job.append(stop_verb)
            job.finish("stopped", "stopped")
        else:
            job.append(f"claude exited with code {proc.returncode}")
            job.finish("failed", f"exit:{proc.returncode}")
        return None

    return stream_events


def _review_result_label(result):
    """Map a derive_result() string to a human label for review/re-review."""
    label = {
        "approved": "Approved PR ✓",
        "no_action": "Finished (no GitHub action taken)",
    }.get(result)
    if label is None and result.startswith("commented:"):
        n = result.split(":", 1)[1]
        label = f"Posted {n} pending comment(s)"
    return label


def _run_review_job(job, log_prefix, workflow_path, prompt_template, log_tag, stop_verb):
    """Shared body for review and re-review (identical except labels/paths)."""
    number, repo = job.number, job.repo
    log_path = _job_log_path(log_prefix, repo, number)
    job.log_path = log_path
    job.append(f"Starting {log_tag} of #{number} in {repo}")
    print(f"[{log_tag}] starting #{number} in {repo} (log: {log_path})", flush=True)

    try:
        workflow = workflows._load_workflow(workflow_path)
    except FileNotFoundError:
        job.append(f"Workflow file not found: {workflow_path}")
        job.append("Use the Status tab to create it.")
        job.finish("failed", "missing_workflow")
        return

    prompt = prompt_template.format(number=number, repo=repo) + workflow
    job_events = _stream_claude_job(job, prompt, log_path, stop_verb=stop_verb)
    if job_events is None:
        return

    try:
        me = github.get_my_login()
    except Exception:
        me = None
    result = events.derive_result(job_events, repo, number, me)
    job.append(_review_result_label(result) or f"Finished: {result}")
    job.finish("done", result)
    print(f"[{log_tag}] finished #{number} result={result}", flush=True)


def run_review(job):
    _run_review_job(
        job, "", REVIEW_WORKFLOW, REVIEW_PROMPT,
        log_tag="review", stop_verb="Review stopped.",
    )


def run_re_review(job):
    _run_review_job(
        job, "re-review-", RE_REVIEW_WORKFLOW, RE_REVIEW_PROMPT,
        log_tag="re-review", stop_verb="Re-review stopped.",
    )


GH_MERGE_METHOD_FLAG = {
    "MERGE": "--merge",
    "SQUASH": "--squash",
    "REBASE": "--rebase",
}


def run_merge(job, default_method):
    """Run `gh pr merge` with the repo's default method. Emulates the
    GitHub Merge button: no --auto, no --delete-branch.
    """
    flag = GH_MERGE_METHOD_FLAG.get(default_method or "MERGE", "--merge")
    job.append(f"Merging #{job.number} in {job.repo} ({flag})")
    print(f"[merge] starting #{job.number} in {job.repo} method={flag}", flush=True)

    proc = subprocess.run(
        ["gh", "pr", "merge", str(job.number),
         "--repo", job.repo, flag],
        capture_output=True, text=True,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            job.append(line)
    if proc.stderr:
        for line in proc.stderr.splitlines():
            job.append(line)

    if proc.returncode == 0:
        job.append("Merged ✓")
        job.finish("done", "merged")
        print(f"[merge] finished #{job.number} merged", flush=True)
    else:
        job.append(f"gh exited with code {proc.returncode}")
        job.finish("failed", f"exit:{proc.returncode}")
        print(f"[merge] finished #{job.number} failed", flush=True)


def _run_worktree_job(job, head_ref, *, log_prefix, workflow_path, log_tag, build_prompt):
    """Shared body for the worktree-based jobs (address / fix-pipeline / rebase).

    Refreshes the per-repo agent clone under its lock, loads the workflow, builds
    the prompt via build_prompt(local_branch, workflow), and streams claude in the
    clone. Returns the parsed events on success, or None if the job was already
    finished here (clone failure, missing workflow, or any _stream_claude_job exit).
    """
    repo, number = job.repo, job.number
    with clones.get_repo_lock(repo):
        try:
            clone_path, local_branch = clones.prepare_agent_clone(repo, head_ref)
        except subprocess.CalledProcessError as e:
            job.append(f"Agent-clone setup failed: {(e.stderr or '').strip()}")
            job.finish("failed", "clone_error")
            return None

        log_path = _job_log_path(log_prefix, repo, number)
        job.log_path = log_path
        job.append(f"Agent clone ready at {clone_path} (branch: {local_branch})")
        print(f"[{log_tag}] starting #{number} in {repo} (clone: {clone_path})", flush=True)

        try:
            workflow = workflows._load_workflow(workflow_path)
        except FileNotFoundError:
            job.append(f"Workflow file not found: {workflow_path}")
            job.append("Use the Status tab to create it.")
            job.finish("failed", "missing_workflow")
            return None

        return _stream_claude_job(
            job, build_prompt(local_branch, workflow), log_path, cwd=clone_path,
        )


def derive_address_result(job_events):
    pushes = 0
    replies = 0
    rerequests = 0
    slack_dms = 0
    for ev in job_events:
        if ev.get("type") != "assistant":
            continue
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") != "tool_use":
                continue
            tool_name = c.get("name") or ""
            # Slack DMs: any MCP tool ending in slack_send_message (not draft).
            if "slack_send_message" in tool_name and "draft" not in tool_name:
                slack_dms += 1
                continue
            if tool_name != "Bash":
                continue
            cmd = (c.get("input") or {}).get("command") or ""
            if _RE_GIT_PUSH.search(cmd):
                pushes += 1
            if _RE_INLINE_REPLY.search(cmd) or _RE_GENERAL_PR_COMMENT.search(cmd):
                replies += 1
            if _RE_RERQUEST.search(cmd):
                rerequests += 1

    parts = []
    if pushes:
        parts.append(f"Pushed {pushes} commit{'s' if pushes != 1 else ''}")
    if replies:
        parts.append(f"replied to {replies}")
    if rerequests:
        parts.append(f"re-requested {rerequests}")
    if slack_dms:
        parts.append(f"DM'd {slack_dms}")
    if pushes:
        label = ", ".join(parts)
    elif replies:
        label = "Replied only"
    else:
        label = "No action"

    return {
        "pushes": pushes,
        "replies": replies,
        "rerequests": rerequests,
        "slack_dms": slack_dms,
        "label": label,
    }


def run_address(job, head_ref):
    """Spawn Claude in a worktree to address PR comments."""
    job_events = _run_worktree_job(
        job, head_ref,
        log_prefix="address-", workflow_path=ADDRESS_WORKFLOW, log_tag="address",
        build_prompt=lambda local_branch, workflow: ADDRESS_PROMPT.format(
            number=job.number, repo=job.repo,
            head_ref=head_ref, local_branch=local_branch,
        ) + workflow,
    )
    if job_events is None:
        return
    result = derive_address_result(job_events)
    job.append(result["label"])
    job.finish("done", result["label"])
    print(f"[address] finished #{job.number} result={result['label']}", flush=True)


def run_fix_pipeline(job, head_ref: str) -> None:
    """Spawn Claude in a worktree to diagnose and fix failing CI checks."""
    job_events = _run_worktree_job(
        job, head_ref,
        log_prefix="fix-pipeline-", workflow_path=FIX_PIPELINE_WORKFLOW, log_tag="fix-pipeline",
        build_prompt=lambda local_branch, workflow: FIX_PIPELINE_PROMPT.format(
            number=job.number, repo=job.repo,
            head_ref=head_ref, local_branch=local_branch,
        ) + workflow,
    )
    if job_events is None:
        return
    job.append("Pipeline fix complete.")
    job.finish("done", "pipeline_fixed")
    print(f"[fix-pipeline] finished #{job.number}", flush=True)


def run_nudge(job, url, title, reviewers, mode, channel_id=None):
    """Spawn Claude to DM the reviewers on Slack via the Slack MCP."""
    if channel_id is None:
        channel_id = config.TEAM_CHANNEL_ID
    resolved_reviewers = [config.SLACK_ID_MAP.get(r, r) for r in reviewers]
    log_path = _job_log_path("nudge-", job.repo, job.number)
    job.log_path = log_path

    try:
        workflow = workflows._load_workflow(NUDGE_WORKFLOW)
    except FileNotFoundError:
        job.append(f"Workflow file not found: {NUDGE_WORKFLOW}")
        job.append("Use the Status tab to create it.")
        job.finish("failed", "missing_workflow")
        return

    venue = f"#channel {channel_id}" if mode == "channel" else f"{len(resolved_reviewers)} DM(s)"
    job.append(f"Nudging on Slack ({mode}, {venue}): {', '.join(reviewers)}")
    print(f"[nudge] starting #{job.number} in {job.repo} mode={mode} reviewers={reviewers}", flush=True)
    prompt = NUDGE_PROMPT.format(
        url=url, title=title, reviewers=", ".join(resolved_reviewers),
        mode=mode, channel=channel_id,
    ) + workflow
    job_events = _stream_claude_job(job, prompt, log_path, stop_verb="Nudge stopped.")
    if job_events is None:
        return
    result = events.derive_nudge_result(job_events, mode=mode)
    job.append(result["label"])
    job.finish("done", result["label"])
    print(f"[nudge] finished #{job.number} result={result['label']}", flush=True)


def run_rebase(job, head_ref: str, base_ref: str) -> None:
    """Spawn Claude in a worktree to rebase the PR branch onto its base."""
    job_events = _run_worktree_job(
        job, head_ref,
        log_prefix="rebase-", workflow_path=REBASE_WORKFLOW, log_tag="rebase",
        build_prompt=lambda local_branch, workflow: REBASE_PROMPT.format(
            number=job.number, repo=job.repo,
            head_ref=head_ref, local_branch=local_branch, base_ref=base_ref,
        ) + _fill_refs(
            workflow, base_ref=base_ref, head_ref=head_ref, local_branch=local_branch,
        ),
    )
    if job_events is None:
        return
    job.append("Rebase complete.")
    job.finish("done", "rebased")
    print(f"[rebase] finished #{job.number}", flush=True)
