#!/usr/bin/env python3
"""Local PR review dashboard.

Lists open GitHub PRs (from a fixed author list) that require my attention,
and lets me kick off a Claude Code review against each.
"""

import sys
from http.server import ThreadingHTTPServer

from app import github
from app.config import HOST, PORT
from app.http.handler import Handler

# ---- Configuration (moved to app/config.py) --------------------------------
# Serializes .env rewrites and the config globals (DEPLOY_TARGET, etc.) that
# settings POSTs reassign while job threads / status GETs read them.

# ---- PR domain (moved to app/prs.py) --------------------------------------
# determine_my_pr_status, MY_PRS_GRAPHQL, _CHECK_PASSED, _CHECK_FAILED,
# summarize_checks, pr_behind_count, list_my_prs, author_reply_count,
# determine_status, list_prs all live in app/prs.py.

# ---- Workflow templates and loader (moved to app/workflows.py) -------------
# _DEFAULT_*_WORKFLOW and _load_workflow live in app/workflows.py.

# ---- Deploy targets (moved to app/deploy.py) --------------------------------
# DEPLOY_TARGETS_PATH, _DEFAULT_DEPLOY_TARGETS, _load_deploy_targets,
# DEPLOY_TARGETS, get_deployed live in app/deploy.py.

# ---- Job registry (moved to app/jobs/job.py) --------------------------------
# Job, _jobs, _jobs_lock, get_or_create_job live in app/jobs/job.py.

# ---- Events (moved to app/jobs/events.py) ----------------------------------
# format_event, count_pending_comments, derive_result, count_slack_sends,
# derive_nudge_result, _RE_APPROVE/_RE_REVIEW_API/_RE_COMMENTS_API live there.

# ---- Runners (moved to app/jobs/runners.py) --------------------------------
# CLAUDE_BASE_ARGS, _job_log_path, _fill_refs, _stream_claude_job,
# _review_result_label, _run_review_job, run_review, run_re_review,
# GH_MERGE_METHOD_FLAG, run_merge, _run_worktree_job, run_address,
# run_fix_pipeline, run_nudge, run_rebase, derive_address_result live there.
# _load_workflow moved to app/workflows.py.

# ---- Agent clones (moved to app/jobs/clones.py) ----------------------------
# AGENT_CLONES_DIR, _repo_locks, _repo_locks_lock, repo_flat, agent_clone_path,
# get_repo_lock, prepare_agent_clone, _git_runner, cleanup_repo_targets live there.

# ---- Scan cleanup (moved to app/cleanup_scan.py) ---------------------------
# scan_cleanup lives in app/cleanup_scan.py.

# ---- Branch listing (moved to app/branches.py) -----------------------------
# list_my_branches lives in app/branches.py.

# ---- Deployed runs (moved to app/deploy.py) ---------------------------------
# get_deployed lives in app/deploy.py.

# ---- System status / env writer / editor (moved to app/sysstatus.py) -------
# write_env_var, get_status, open_in_editor, _env_lock live in app/sysstatus.py.

# ---- HTTP routing + handler (moved to app/http/) ---------------------------
# _req_ref, _extract_merge, _JOB_POST_ROUTES live in app/http/routes.py.
# Handler lives in app/http/handler.py.


def main():
    try:
        me = github.get_my_login()
    except Exception as e:
        print(f"Failed to determine GitHub login via gh: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"PR review dashboard")
    print(f"  reviewer: {me}")
    print(f"  listening: http://{HOST}:{PORT}")
    print(f"  stop: Ctrl+C")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
