"""app/sysstatus.py — system status checks, env-var writer, and editor opener."""

import os
import re
import shutil
import subprocess
import threading

from app import config, deploy
from app.config import (
    LOG_DIR,
    REVIEW_WORKFLOW, RE_REVIEW_WORKFLOW,
    ADDRESS_WORKFLOW, FIX_PIPELINE_WORKFLOW, REBASE_WORKFLOW,
)
from app.jobs import clones

# Reentrant so a handler can hold it across both write_env_var and the global update.
_env_lock = threading.RLock()

# Path to the .env file (mirrors config._ENV_PATH).
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")


def write_env_var(key, value):
    """Update or append key=value in the .env file and os.environ.

    Holds _env_lock so concurrent settings saves can't interleave their
    read-modify-write and corrupt the file.
    """
    with _env_lock:
        try:
            with open(_ENV_PATH) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        updated = False
        new_lines = []
        for line in lines:
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                new_lines.append(f"{key}={value}\n")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"{key}={value}\n")
        with open(_ENV_PATH, "w") as f:
            f.writelines(new_lines)
        os.environ[key] = value


def get_status():
    """Return a list of status checks for the app configuration."""
    checks = []

    def check(name, description, ok, excerpt="", fix=None):
        checks.append({"name": name, "description": description, "ok": ok, "excerpt": excerpt, "fix": fix})

    def separator():
        checks.append({"separator": True})

    def prompt_excerpt(prompt):
        if not prompt or not prompt.strip():
            return "Not set or empty."
        text = prompt.strip()
        return text[:500] + ("\n… (truncated)" if len(text) > 500 else "")

    def dir_excerpt(path):
        if not os.path.isdir(path):
            return f"Path: {path}\nStatus: does not exist (created automatically on first use)"
        try:
            entries = sorted(os.listdir(path))
            contents = ", ".join(entries[:20]) + ("…" if len(entries) > 20 else "") if entries else "(empty)"
            return f"Path: {path}\nStatus: exists\nContents: {contents}"
        except Exception as e:
            return f"Path: {path}\nError reading directory: {e}"

    def workflow_excerpt(path):
        if not os.path.isfile(path):
            return f"File not found: {path}\nClick 'Create file' to generate it with sensible defaults."
        try:
            with open(path) as f:
                text = f.read().strip()
            return text[:500] + ("\n… (truncated)" if len(text) > 500 else "")
        except Exception as e:
            return f"Error reading {path}: {e}"

    for wf_path, wf_name, wf_label in [
        (REVIEW_WORKFLOW,       "review_workflow.md",       "Review workflow instructions"),
        (RE_REVIEW_WORKFLOW,    "re_review_workflow.md",    "Re-review workflow instructions"),
        (ADDRESS_WORKFLOW,      "address_workflow.md",      "Address workflow instructions"),
        (FIX_PIPELINE_WORKFLOW, "fix_pipeline_workflow.md", "Fix pipeline workflow instructions"),
        (REBASE_WORKFLOW,       "rebase_workflow.md",       "Rebase workflow instructions"),
        (deploy.DEPLOY_TARGETS_PATH,   "deploy_targets.json",
         "Deploy targets (repo → env → workflow name)"),
    ]:
        ok = os.path.isfile(wf_path)
        check(
            wf_name, wf_label, ok,
            workflow_excerpt(wf_path),
            fix={"action": "create_file", "path": wf_path} if not ok else None,
        )

    separator()

    check("AGENT_CLONES_DIR", "Agent clones directory",
          os.path.isdir(clones.AGENT_CLONES_DIR),
          dir_excerpt(clones.AGENT_CLONES_DIR),
          fix={"action": "create_dir", "path": clones.AGENT_CLONES_DIR})

    check("LOG_DIR", "Log directory",
          os.path.isdir(LOG_DIR),
          dir_excerpt(LOG_DIR),
          fix={"action": "create_dir", "path": LOG_DIR})

    claude_path = shutil.which("claude")
    check("claude", "claude CLI on PATH",
          claude_path is not None,
          f"Found at: {claude_path}" if claude_path else "Not found on PATH")

    gh_result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    gh_output = (gh_result.stdout + gh_result.stderr).strip()
    check("gh", "gh CLI authenticated",
          gh_result.returncode == 0,
          gh_output[:500] if gh_output else "No output from gh auth status")

    check("DEPLOY_TARGET", "Default deploy environment (.env)",
          bool(config.DEPLOY_TARGET),
          f"Target: {config.DEPLOY_TARGET}" if config.DEPLOY_TARGET else "Not set — add DEPLOY_TARGET=csi-3 to .env to show Deploy buttons",
          fix={"action": "set_env", "key": "DEPLOY_TARGET", "placeholder": "csi-3"})

    from app import jira
    check("JIRA", "Jira credentials for the Tickets tab (.env)",
          jira.jira_configured(),
          f"Configured: {config.JIRA_EMAIL} @ {config.JIRA_SITE}" if jira.jira_configured()
          else "Not set — add JIRA_SITE, JIRA_EMAIL, and JIRA_API_TOKEN on the Settings tab")

    return checks


def open_in_editor(path: str) -> None:
    """Open *path* in the configured or auto-detected editor.

    If EDITOR_CMD is set, that command is used directly (e.g. "cursor", "code",
    "subl", "open -a TextEdit"). Otherwise: tries ``code``, then falls back to
    the OS default (``open`` on macOS, ``xdg-open`` on Linux).
    """
    import platform
    import shlex
    if config.EDITOR_CMD:
        cmd = shlex.split(config.EDITOR_CMD) + [path]
        subprocess.Popen(cmd)
        return
    if shutil.which("code"):
        subprocess.Popen(["code", path])
        return
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", path])
    elif system == "Linux":
        subprocess.Popen(["xdg-open", path])
    else:
        raise RuntimeError(f"No editor found for platform {system!r}")
