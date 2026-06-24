#!/usr/bin/env python3
"""Local PR review dashboard.

Lists open GitHub PRs (from a fixed author list) that require my attention,
and lets me kick off a Claude Code review against each.
"""

import concurrent.futures
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import base64
import urllib.error
import urllib.parse
import urllib.request

import cleanup
from app import config, github, jira, prs, workflows
from app import deploy, branches, cleanup_scan, sysstatus
from app.http import static_files
from app.config import (
    HOST, PORT,
    JIRA_JQL, _JIRA_KEY_RE,
    LOG_DIR, _WORKFLOW_DIR,
    REVIEW_WORKFLOW, RE_REVIEW_WORKFLOW,
    ADDRESS_WORKFLOW, FIX_PIPELINE_WORKFLOW, REBASE_WORKFLOW,
)
from app.jobs import job as job_mod, events, clones, runners

# ---- Configuration (moved to app/config.py) --------------------------------
# Serializes .env rewrites and the config globals (DEPLOY_TARGET, etc.) that
# settings POSTs reassign while job threads / status GETs read them.
# Re-export _is_human_author from config for consumers still in this module.
_is_human_author = config._is_human_author


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
# Re-export frequently accessed names for the Handler still in this module.
_jobs = job_mod._jobs
_jobs_lock = job_mod._jobs_lock
get_or_create_job = job_mod.get_or_create_job


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

# ---- Job POST routing ------------------------------------------------------

def _req_ref(data, key):
    """Pull a required, non-empty ref string from a request body."""
    val = str(data[key])
    if not val:
        raise ValueError(f"{key} required")
    return val


def _extract_merge(d):
    return (str(d.get("defaultMergeMethod") or "MERGE"),)


# path -> (job kind, runner fn, extract-extra-args fn or None).
# The dispatcher (_dispatch_job_post) handles the shared number/repo parse,
# job creation, thread spawn, and 202 envelope; `extract` returns the extra
# positional args each runner needs (and may raise to reject the request).
_JOB_POST_ROUTES = {
    "/api/review":       ("review",       runners.run_review,       None),
    "/api/re-review":    ("re_review",    runners.run_re_review,    None),
    "/api/merge":        ("merge",        runners.run_merge,        _extract_merge),
    "/api/address":      ("address",      runners.run_address,      lambda d: (_req_ref(d, "headRefName"),)),
    "/api/fix-pipeline": ("fix_pipeline", runners.run_fix_pipeline, lambda d: (_req_ref(d, "headRefName"),)),
    "/api/rebase":       ("rebase",       runners.run_rebase,       lambda d: (_req_ref(d, "headRefName"), _req_ref(d, "baseRefName"))),
}


# ---- HTTP server -----------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected mid-response (e.g. closed a tab); nothing to do.
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/static/"):
            try:
                body, ctype = static_files.serve_asset(parsed.path[len("/static/"):])
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/":
            config_json = json.dumps({
                "deploy_targets": deploy.DEPLOY_TARGETS,
                "deploy_target": config.DEPLOY_TARGET,
                "cache_ttl": config.CACHE_TTL,
                "host": HOST,
                "port": PORT,
                "workflow_dir": _WORKFLOW_DIR,
                "editor_cmd": config.EDITOR_CMD,
                "jira_site": config.JIRA_SITE,
                "jira_email": config.JIRA_EMAIL,
                # Never echo the token itself to the browser — only whether it is set.
                "jira_token_set": bool(config.JIRA_API_TOKEN),
                "jira_status_filter": config.JIRA_STATUS_FILTER,
                "cleanup_repos": config.CLEANUP_REPOS,
                "cleanup_author_email": config.CLEANUP_AUTHOR_EMAIL,
                "fresh_reviewers": config.FRESH_REVIEWERS,
                "team_channel_id": config.TEAM_CHANNEL_ID,
                "teams": config.TEAMS,
                "slack_ids": ",".join(f"{k}:{v}" for k, v in config.SLACK_ID_MAP.items()),
            })
            body = static_files.serve_index(config_json)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/status":
            self._send_json(200, sysstatus.get_status())
            return
        if parsed.path == "/api/branches":
            qs = parse_qs(parsed.query or "")
            repo = qs.get("repo", [""])[0]
            if "/" not in repo:
                self._send_json(400, {"error": "repo required"})
                return
            try:
                self._send_json(200, branches.list_my_branches(repo))
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/deployed":
            try:
                self._send_json(200, deploy.get_deployed())
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/prs":
            qs = parse_qs(parsed.query or "")
            fresh = qs.get("fresh", ["0"])[0] in ("1", "true", "yes")
            try:
                pr_list = prs.list_prs(fresh=fresh)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return
            self._send_json(200, pr_list)
            return
        if parsed.path == "/api/prs/mine":
            try:
                pr_list = prs.list_my_prs()
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return
            self._send_json(200, pr_list)
            return
        if parsed.path == "/api/tickets":
            if not jira.jira_configured():
                self._send_json(200, {"configured": False, "tickets": []})
                return
            try:
                self._send_json(200, {"configured": True, "tickets": jira.jira_search()})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/tickets/transitions":
            if not jira.jira_configured():
                self._send_json(503, {"error": "Jira not configured"})
                return
            qs = parse_qs(parsed.query or "")
            key = qs.get("key", [""])[0]
            if not _JIRA_KEY_RE.match(key):
                self._send_json(400, {"error": "invalid issue key"})
                return
            try:
                self._send_json(200, {"transitions": jira.jira_transitions(key)})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/jira/statuses":
            if not jira.jira_configured():
                self._send_json(503, {"error": "Jira not configured"})
                return
            try:
                self._send_json(200, {"statuses": jira.jira_statuses()})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/cleanup":
            qs = parse_qs(parsed.query or "")
            fresh = qs.get("fresh", ["0"])[0] in ("1", "true", "yes")
            try:
                self._send_json(200, cleanup_scan.scan_cleanup(fresh=fresh))
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path in ("/api/job/stream", "/api/review/stream"):
            qs = parse_qs(parsed.query or "")
            repo = qs.get("repo", [""])[0]
            try:
                number = int(qs.get("number", ["0"])[0])
            except ValueError:
                self.send_error(400, "bad number")
                return
            kind = qs.get("kind", ["review"])[0]
            if kind not in ("review", "re_review", "merge", "address", "fix_pipeline", "rebase", "nudge"):
                self.send_error(400, "bad kind")
                return
            if "/" not in repo or number <= 0:
                self.send_error(400, "bad params")
                return
            self._stream_job(repo, number, kind)
            return
        self.send_error(404)

    def _stream_job(self, repo, number, kind):
        with _jobs_lock:
            job = _jobs.get((repo, number, kind))
        if not job:
            self._send_json(404, {"error": "no such job"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        q = job.subscribe()
        try:
            while True:
                try:
                    line = q.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(line)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if line.get("type") == "done":
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            job.unsubscribe(q)

    def do_POST(self):
        parsed = urlparse(self.path)
        route = _JOB_POST_ROUTES.get(parsed.path)
        if route:
            self._dispatch_job_post(*route)
            return
        if parsed.path == "/api/nudge":
            self._handle_nudge_post()
            return
        if parsed.path == "/api/job/stop":
            self._handle_stop_post()
            return
        if parsed.path == "/api/status/create-dir":
            self._handle_create_dir_post()
            return
        if parsed.path == "/api/status/set-env":
            self._handle_set_env_post()
            return
        if parsed.path == "/api/status/create-file":
            self._handle_create_file_post()
            return
        if parsed.path == "/api/deploy":
            self._handle_deploy_post()
            return
        if parsed.path == "/api/settings":
            self._handle_settings_post()
            return
        if parsed.path == "/api/tickets/transition":
            self._handle_ticket_transition_post()
            return
        if parsed.path == "/api/cleanup/delete":
            self._handle_cleanup_delete_post()
            return
        if parsed.path == "/api/open-dir":
            self._handle_open_dir_post()
            return
        self.send_error(404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def _dispatch_job_post(self, kind, runner, extract):
        """Shared handler for all job-spawning POST routes (see _JOB_POST_ROUTES).

        Parses the common number/repo fields, runs the route's `extract` for any
        extra runner args (which may raise to reject), creates/dedupes the job,
        spawns the runner thread, and returns the standard 202 envelope.
        """
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            if "/" not in repo:
                raise ValueError("repo must be owner/name")
            extra = extract(data) if extract else ()
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, kind)
        if started:
            threading.Thread(
                target=runner, args=(job, *extra), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": kind,
        })

    def _handle_stop_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            kind = str(data.get("kind") or "review")
            if "/" not in repo:
                raise ValueError("repo must be owner/name")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        with _jobs_lock:
            job = _jobs.get((repo, number, kind))
        if not job or job.status != "running":
            self._send_json(404, {"error": "no running job"})
            return
        job.stop()
        self._send_json(200, {"ok": True})

    def _handle_nudge_post(self):
        try:
            data = self._read_json_body()
            number = int(data["number"])
            repo = str(data["repo"])
            url = str(data["url"])
            title = str(data.get("title") or "")
            reviewers = data.get("reviewers") or []
            mode = str(data.get("mode") or "re_review")
            channel_id = str(data.get("channel_id") or config.TEAM_CHANNEL_ID)
            allowed_channels = {config.TEAM_CHANNEL_ID, *(t["channel_id"] for t in config.TEAMS)} - {""}
            if mode == "channel" and not channel_id:
                raise ValueError("channel mode requires a channel_id (TEAM_CHANNEL_ID not configured)")
            if channel_id and channel_id not in allowed_channels:
                raise ValueError("channel_id not in allow-list")
            if "/" not in repo:
                raise ValueError("repo must be owner/name")
            if mode not in ("re_review", "fresh", "channel"):
                raise ValueError("mode must be re_review, fresh, or channel")
            reviewers = [str(r) for r in reviewers if r]
            if not reviewers:
                raise ValueError("no reviewers to nudge")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        job, started = get_or_create_job(repo, number, "nudge")
        if started:
            threading.Thread(
                target=runners.run_nudge, args=(job, url, title, reviewers, mode, channel_id), daemon=True,
            ).start()
        self._send_json(202, {
            "started": started,
            "running": True,
            "number": number,
            "repo": repo,
            "kind": "nudge",
        })

    def _handle_create_dir_post(self):
        try:
            data = self._read_json_body()
            path = str(data["path"])
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        allowed = (clones.AGENT_CLONES_DIR, LOG_DIR)
        if path not in allowed:
            self._send_json(403, {"error": "path not allowed"})
            return
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_set_env_post(self):
        try:
            data = self._read_json_body()
            key = str(data["key"])
            value = str(data["value"]).strip()
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if key not in ("DEPLOY_TARGET",):
            self._send_json(403, {"error": "key not allowed"})
            return
        if not value:
            self._send_json(400, {"error": "value required"})
            return
        try:
            with sysstatus._env_lock:
                sysstatus.write_env_var(key, value)
                if key == "DEPLOY_TARGET":
                    config.DEPLOY_TARGET = value
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_settings_post(self):
        _allowed = {"DEPLOY_TARGET",
                    "CACHE_TTL", "HOST", "PORT", "EDITOR_CMD",
                    "JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN",
                    "JIRA_STATUS_FILTER", "CLEANUP_REPOS", "CLEANUP_AUTHOR_EMAIL",
                    "FRESH_REVIEWERS", "TEAM_CHANNEL_ID", "TEAMS", "SLACK_IDS"}
        try:
            data = self._read_json_body()
            settings = data.get("settings", {})
            if not isinstance(settings, dict):
                raise ValueError("settings must be a dict")
        except Exception as e:
            self._send_json(400, {"error": str(e)})
            return
        unknown = set(settings) - _allowed
        if unknown:
            self._send_json(403, {"error": f"unknown keys: {', '.join(sorted(unknown))}"})
            return
        with sysstatus._env_lock:
            for key, value in settings.items():
                value = str(value).strip()
                if key == "JIRA_SITE":
                    value = config._normalize_jira_site(value)
                # These keys may be written empty (clears them); every other key
                # keeps its current value when blank.
                if not value and key not in (
                    "JIRA_STATUS_FILTER", "CLEANUP_REPOS", "CLEANUP_AUTHOR_EMAIL",
                    "FRESH_REVIEWERS", "TEAM_CHANNEL_ID", "TEAMS", "SLACK_IDS"
                ):
                    continue
                try:
                    sysstatus.write_env_var(key, value)
                except Exception as e:
                    self._send_json(500, {"error": f"failed to write {key}: {e}"})
                    return
                if key == "DEPLOY_TARGET":
                    config.DEPLOY_TARGET = value
                elif key == "CACHE_TTL":
                    try:
                        config.CACHE_TTL = int(value)
                    except ValueError:
                        pass
                elif key == "EDITOR_CMD":
                    config.EDITOR_CMD = value
                elif key == "JIRA_SITE":
                    config.JIRA_SITE = value
                elif key == "JIRA_EMAIL":
                    config.JIRA_EMAIL = value
                elif key == "JIRA_API_TOKEN":
                    config.JIRA_API_TOKEN = value
                elif key == "JIRA_STATUS_FILTER":
                    config.JIRA_STATUS_FILTER = config._env_list("JIRA_STATUS_FILTER")
                elif key == "CLEANUP_REPOS":
                    config.CLEANUP_REPOS = config._env_list("CLEANUP_REPOS")
                elif key == "CLEANUP_AUTHOR_EMAIL":
                    config.CLEANUP_AUTHOR_EMAIL = value
                elif key == "FRESH_REVIEWERS":
                    config.FRESH_REVIEWERS = config._env_list("FRESH_REVIEWERS")
                elif key == "TEAM_CHANNEL_ID":
                    config.TEAM_CHANNEL_ID = value
                elif key == "TEAMS":
                    config.TEAMS = config._parse_teams()
                elif key == "SLACK_IDS":
                    config.SLACK_ID_MAP = config._parse_slack_ids()
        self._send_json(200, {"ok": True})

    def _handle_open_dir_post(self):
        try:
            data = self._read_json_body()
            path = str(data.get("path") or "")
        except Exception as e:
            self._send_json(400, {"error": str(e)})
            return
        if os.path.realpath(path) != os.path.realpath(_WORKFLOW_DIR):
            self._send_json(403, {"error": "path not allowed"})
            return
        try:
            sysstatus.open_in_editor(path)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_create_file_post(self):
        _defaults = {
            os.path.realpath(REVIEW_WORKFLOW):    workflows._DEFAULT_REVIEW_WORKFLOW,
            os.path.realpath(RE_REVIEW_WORKFLOW): workflows._DEFAULT_RE_REVIEW_WORKFLOW,
            os.path.realpath(ADDRESS_WORKFLOW):   workflows._DEFAULT_ADDRESS_WORKFLOW,
            os.path.realpath(FIX_PIPELINE_WORKFLOW):  workflows._DEFAULT_FIX_PIPELINE_WORKFLOW,
            os.path.realpath(REBASE_WORKFLOW):         workflows._DEFAULT_REBASE_WORKFLOW,
            os.path.realpath(deploy.DEPLOY_TARGETS_PATH): json.dumps(
                deploy._DEFAULT_DEPLOY_TARGETS, indent=2
            ) + "\n",
        }
        try:
            data = self._read_json_body()
            path = os.path.realpath(os.path.expanduser(str(data["path"])))
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        if path not in _defaults:
            self._send_json(403, {"error": "path not allowed"})
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(_defaults[path])
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_deploy_post(self):
        try:
            data = self._read_json_body()
            repo     = str(data["repo"])
            env      = str(data["env"])
            head_ref = str(data["head_ref"])
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        workflow_name = deploy.DEPLOY_TARGETS.get(repo, {}).get(env)
        if not workflow_name:
            self._send_json(400, {"error": f"No workflow configured for {repo} / {env}"})
            return
        result = subprocess.run(
            ["gh", "workflow", "run", workflow_name, "-R", repo, "--ref", head_ref],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip() or "workflow dispatch failed"
            self._send_json(500, {"error": err})
            return
        print(f"[deploy] dispatched '{workflow_name}' on {repo}@{head_ref} for {env}", flush=True)
        self._send_json(200, {"ok": True})

    def _handle_ticket_transition_post(self):
        if not jira.jira_configured():
            self._send_json(503, {"error": "Jira not configured"})
            return
        try:
            data = self._read_json_body()
            key = str(data["key"])
            transition_id = str(data["transitionId"])
            if not _JIRA_KEY_RE.match(key):
                raise ValueError("invalid issue key")
            if not re.match(r"^\d+$", transition_id):
                raise ValueError("invalid transition id")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        try:
            jira.jira_do_transition(key, transition_id)
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        print(f"[tickets] transitioned {key} via {transition_id}", flush=True)
        self._send_json(200, {"ok": True})

    def _handle_cleanup_delete_post(self):
        try:
            data = self._read_json_body()
            actions = data.get("actions") or []
            if not isinstance(actions, list):
                raise ValueError("actions must be a list")
        except Exception as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        allowed = {path for path, _label, _kind in clones.cleanup_repo_targets()}
        # Per-repo set of (kind, name, worktree_path) the scan actually surfaced.
        # A client may only delete candidates the server itself flagged — this
        # blocks crafted actions (e.g. deleting the default branch, or any branch
        # the scan never offered) and subsumes the default/current-branch guard.
        surfaced = {}

        def _surfaced_for(path):
            if path not in surfaced:
                try:
                    cands = cleanup.scan_repo(clones._git_runner, path)
                except Exception:
                    cands = []
                surfaced[path] = {
                    (c["kind"], c["name"], c.get("worktree_path") or "") for c in cands
                }
            return surfaced[path]

        results = []
        for action in actions:
            result = dict(action)
            repo_path = str(action.get("repo_path") or "")
            if repo_path not in allowed:
                result.update(ok=False, error="repo not allowed")
                results.append(result)
                continue
            triple = (action.get("kind"), action.get("name"),
                      str(action.get("worktree_path") or ""))
            if triple not in _surfaced_for(repo_path):
                result.update(ok=False, error="not a current cleanup candidate")
                results.append(result)
                continue
            ok, err = cleanup.delete_candidate(clones._git_runner, action)
            result.update(ok=ok, error=err)
            print(f"[cleanup] {action.get('kind')} {action.get('name')} "
                  f"in {repo_path} -> {'ok' if ok else err}", flush=True)
            results.append(result)
        self._send_json(200, {"results": results})


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
