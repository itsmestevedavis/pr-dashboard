"""app/jobs/clones.py — agent-clone management for worktree-based jobs."""

import os
import subprocess
import threading

from app import config

AGENT_CLONES_DIR = os.path.expanduser("~/.cache/pr-tools/clones")
_repo_locks = {}
_repo_locks_lock = threading.Lock()


def repo_flat(repo_full):
    """owner/repo -> owner_repo."""
    return repo_full.replace("/", "_")


def agent_clone_path(repo_full):
    """Path to the dedicated agent clone for a repo."""
    return os.path.join(AGENT_CLONES_DIR, repo_flat(repo_full))


def get_repo_lock(repo_full):
    """Per-repo mutex so two agent jobs against the same repo serialize."""
    with _repo_locks_lock:
        lock = _repo_locks.get(repo_full)
        if lock is None:
            lock = threading.Lock()
            _repo_locks[repo_full] = lock
        return lock


def prepare_agent_clone(repo_full, head_ref):
    """Ensure ~/.cache/pr-tools/clones/<repo_flat> has the PR head checked out.

    Clones if missing, then fetches origin/<head_ref>, discards local state,
    and force-checks out the branch. Returns (clone_path, branch_name).

    Raises subprocess.CalledProcessError on git failure.
    """
    path = agent_clone_path(repo_full)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(AGENT_CLONES_DIR, exist_ok=True)
        # `gh repo clone` handles auth via the user's gh session.
        subprocess.run(
            ["gh", "repo", "clone", repo_full, path],
            check=True, capture_output=True, text=True,
        )
    subprocess.run(
        ["git", "-C", path, "fetch", "origin", head_ref],
        check=True, capture_output=True, text=True,
    )
    # Discard any leftover state from a prior run before switching branch.
    subprocess.run(
        ["git", "-C", path, "reset", "--hard"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", path, "clean", "-fd"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", path, "checkout", "-B", head_ref, f"origin/{head_ref}"],
        check=True, capture_output=True, text=True,
    )
    return path, head_ref


def _git_runner(args, cwd):
    """Runner injected into cleanup.py: run `git -C cwd <args>` -> (code, out, err)."""
    proc = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True
    )
    return proc.returncode, proc.stdout, proc.stderr


def cleanup_repo_targets():
    """(path, label, kind) tuples to scan: configured repos + each agent clone."""
    targets = []
    for raw in config.CLEANUP_REPOS:
        targets.append((os.path.abspath(os.path.expanduser(raw)), raw, "configured"))
    if os.path.isdir(AGENT_CLONES_DIR):
        for name in sorted(os.listdir(AGENT_CLONES_DIR)):
            full = os.path.join(AGENT_CLONES_DIR, name)
            if os.path.isdir(os.path.join(full, ".git")):
                targets.append((full, name, "clone"))
    return targets
