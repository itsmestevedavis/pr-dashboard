"""HTTP request handler for the PR dashboard server."""

import json
import os
import queue
import re
import threading
from http.server import BaseHTTPRequestHandler
from typing import Callable, NamedTuple, Optional
from urllib.parse import urlparse, parse_qs

import cleanup
from app import config, workflows, jira, prs, deploy, branches, cleanup_scan, sysstatus, team, standup
from app.config import (
    HOST, PORT,
    _JIRA_KEY_RE,
    LOG_DIR, _WORKFLOW_DIR,
    REVIEW_WORKFLOW, RE_REVIEW_WORKFLOW,
    ADDRESS_WORKFLOW, FIX_PIPELINE_WORKFLOW, REBASE_WORKFLOW, NUDGE_WORKFLOW,
)
from app.jobs import job, runners, clones
from app.http import static_files, routes


# ---- UI-writable settings ----------------------------------------------------

class _Setting(NamedTuple):
    """One .env key the Settings/Status UI may write.

    `validate` runs before anything is persisted (raise ValueError to reject);
    `apply` updates the in-process config after the .env write (None = takes
    effect on restart); `allow_empty` keys may be saved blank to clear them —
    every other key keeps its current value when submitted blank.
    """
    apply: Optional[Callable[[str], None]]
    allow_empty: bool = False
    validate: Optional[Callable[[str], object]] = None


def _set(attr):
    return lambda value: setattr(config, attr, value)


def _reparse_list(attr):
    # Comma-list keys re-read os.environ (updated by write_env_var), exactly as
    # config.py parses them at startup.
    return lambda value: setattr(config, attr, config._env_list(attr))


def _apply_cache_ttl(value):
    config.CACHE_TTL = int(value)


def _apply_board_id(value):
    config.JIRA_BOARD_ID = value
    jira.clear_sprint_support_cache()  # a new board may support sprints — re-probe


_SETTINGS = {
    "DEPLOY_TARGET":        _Setting(_set("DEPLOY_TARGET"), allow_empty=True),  # empty hides Deploy buttons
    "DEV_BOX_URL":          _Setting(_set("DEV_BOX_URL"), allow_empty=True),
    "CACHE_TTL":            _Setting(_apply_cache_ttl, validate=int),
    "HOST":                 _Setting(None),  # server bind — restart to take effect
    "PORT":                 _Setting(None, validate=int),
    "EDITOR_CMD":           _Setting(_set("EDITOR_CMD"), allow_empty=True),  # empty = auto-detect
    "JIRA_SITE":            _Setting(_set("JIRA_SITE")),
    "JIRA_EMAIL":           _Setting(_set("JIRA_EMAIL")),
    "JIRA_API_TOKEN":       _Setting(_set("JIRA_API_TOKEN")),
    "JIRA_STATUS_FILTER":   _Setting(_reparse_list("JIRA_STATUS_FILTER"), allow_empty=True),
    "JIRA_TEAM":            _Setting(_reparse_list("JIRA_TEAM"), allow_empty=True),
    "JIRA_BOARD_ID":        _Setting(_apply_board_id, allow_empty=True),
    "CLEANUP_REPOS":        _Setting(_reparse_list("CLEANUP_REPOS"), allow_empty=True),
    "CLEANUP_AUTHOR_EMAIL": _Setting(_set("CLEANUP_AUTHOR_EMAIL"), allow_empty=True),
    "FRESH_REVIEWERS":      _Setting(_reparse_list("FRESH_REVIEWERS"), allow_empty=True),
    "TEAM_CHANNEL_ID":      _Setting(_set("TEAM_CHANNEL_ID"), allow_empty=True),
    "TEAMS":                _Setting(lambda v: setattr(config, "TEAMS", config._parse_teams()), allow_empty=True),
    "SLACK_IDS":            _Setting(lambda v: setattr(config, "SLACK_ID_MAP", config._parse_slack_ids()), allow_empty=True),
}


def _write_setting(key, value):
    """Persist one settings key to .env and apply it in-process.

    Raises ValueError for an invalid value (nothing written) and propagates
    write errors. Callers hold sysstatus._env_lock.
    """
    spec = _SETTINGS[key]
    if key == "JIRA_SITE":
        value = config._normalize_jira_site(value)
    if value and spec.validate:
        try:
            spec.validate(value)
        except ValueError:
            raise ValueError(f"invalid value for {key}: {value!r}")
    sysstatus.write_env_var(key, value)
    if spec.apply:
        spec.apply(value)


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
                "dev_box_url": config.DEV_BOX_URL,
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
                "jira_team": ",".join(config.JIRA_TEAM),
                "jira_board_id": config.JIRA_BOARD_ID,
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
        if parsed.path == "/api/team":
            try:
                self._send_json(200, team.team_overview())
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if parsed.path == "/api/team/standup":
            # Cached summary only — never generates (that's the POST). Safe/fast on load.
            try:
                self._send_json(200, standup.standup_summary(force=False))
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
        with job._jobs_lock:
            j = job._jobs.get((repo, number, kind))
        if not j:
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
        q = j.subscribe()
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
            j.unsubscribe(q)

    def do_POST(self):
        parsed = urlparse(self.path)
        route = routes._JOB_POST_ROUTES.get(parsed.path)
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
        if parsed.path == "/api/team/resolve":
            self._handle_team_resolve_post()
            return
        if parsed.path == "/api/team/standup":
            self._handle_standup_post()
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
        """Shared handler for all job-spawning POST routes (see routes._JOB_POST_ROUTES).

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
        j, started = job.get_or_create_job(repo, number, kind)
        if started:
            threading.Thread(
                target=runner, args=(j, *extra), daemon=True,
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
        with job._jobs_lock:
            j = job._jobs.get((repo, number, kind))
        if not j or j.status != "running":
            self._send_json(404, {"error": "no running job"})
            return
        j.stop()
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
        j, started = job.get_or_create_job(repo, number, "nudge")
        if started:
            threading.Thread(
                target=runners.run_nudge, args=(j, url, title, reviewers, mode, channel_id), daemon=True,
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
        if key not in _SETTINGS:
            self._send_json(403, {"error": "key not allowed"})
            return
        if not value:
            self._send_json(400, {"error": "value required"})
            return
        try:
            with sysstatus._env_lock:
                _write_setting(key, value)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        self._send_json(200, {"ok": True})

    def _handle_settings_post(self):
        try:
            data = self._read_json_body()
            settings = data.get("settings", {})
            if not isinstance(settings, dict):
                raise ValueError("settings must be a dict")
        except Exception as e:
            self._send_json(400, {"error": str(e)})
            return
        unknown = set(settings) - set(_SETTINGS)
        if unknown:
            self._send_json(403, {"error": f"unknown keys: {', '.join(sorted(unknown))}"})
            return
        with sysstatus._env_lock:
            for key, value in settings.items():
                value = str(value).strip()
                # Blank clears allow_empty keys; every other key keeps its
                # current value when submitted blank.
                if not value and not _SETTINGS[key].allow_empty:
                    continue
                try:
                    _write_setting(key, value)
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                except Exception as e:
                    self._send_json(500, {"error": f"failed to write {key}: {e}"})
                    return
        self._send_json(200, {"ok": True})

    def _handle_team_resolve_post(self):
        """Preview which configured emails resolve to Jira accounts (Settings button).

        Accepts {emails: "a@x,b@y"} or {emails: [...]}, resolves them (updating the
        cache), and returns each member's status so the UI can flag ⚠ not-found.
        """
        if not jira.jira_configured():
            self._send_json(503, {"error": "Jira not configured"})
            return
        try:
            data = self._read_json_body()
            raw = data.get("emails", "")
        except Exception as e:
            self._send_json(400, {"error": str(e)})
            return
        if isinstance(raw, str):
            emails = [s.strip() for s in raw.split(",") if s.strip()]
        elif isinstance(raw, list):
            emails = [str(s).strip() for s in raw if str(s).strip()]
        else:
            self._send_json(400, {"error": "emails must be a string or list"})
            return
        try:
            self._send_json(200, {"members": team.resolve_members(emails)})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_standup_post(self):
        """Regenerate the Team-tab standup summary (the Generate/Refresh button).

        Blocks on `claude -p` (up to ~2 min), which is fine on ThreadingHTTPServer —
        it ties up only this request thread. standup_summary handles the
        not-configured / generation-error cases in its payload.
        """
        try:
            self._send_json(200, standup.standup_summary(force=True))
        except Exception as e:
            self._send_json(500, {"error": str(e)})

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
            os.path.realpath(NUDGE_WORKFLOW):          workflows._DEFAULT_NUDGE_WORKFLOW,
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
        if env not in deploy.DEPLOY_TARGETS.get(repo, {}):
            self._send_json(400, {"error": f"{repo} has no '{env}' deploy target configured"})
            return
        try:
            result = deploy.push_preview(repo, head_ref)
        except RuntimeError as e:
            self._send_json(502, {"error": str(e)})
            return
        print(f"[deploy] pushed {result['branch']} @ {result['sha'][:9]} on {repo} for {env}",
              flush=True)
        self._send_json(200, {"ok": True, "branch": result["branch"]})

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
