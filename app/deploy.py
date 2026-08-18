"""app/deploy.py — deploy-targets loader, preview-branch push, deployed-runs query."""

import json
import os
import re
import concurrent.futures

from app import config, github
from app.config import _WORKFLOW_DIR

DEPLOY_TARGETS_PATH = os.path.join(_WORKFLOW_DIR, "deploy_targets.json")

# Workflow names per repo per environment. Keys are "owner/repo"; values map
# env slug to the GitHub Actions workflow whose runs the Deployed tab tracks.
# Deploys themselves are push-driven (see push_preview), not dispatched.
_DEFAULT_DEPLOY_TARGETS = {
    "Cognota/next-gen": {
        "dev-box": "Deploy preview branch to dev box",
    },
}


def _load_deploy_targets():
    if not os.path.isfile(DEPLOY_TARGETS_PATH):
        return {}
    with open(DEPLOY_TARGETS_PATH) as f:
        return json.load(f)


DEPLOY_TARGETS = _load_deploy_targets()

# A branch prefix like "feat/", "fix/", "itsmestevedavis/" — stripped only when
# what follows is a ticket key, so the preview name keeps its Jira link.
_TYPE_PREFIX_RE = re.compile(r"^[\w.-]+/(?=[A-Z][A-Z0-9]*-\d)")


def preview_branch(head_ref: str) -> str:
    """Derive the preview branch name that deploys head_ref to the dev box.

    The deploy workflow triggers on any pushed branch whose name contains
    "preview"; keeping the ticket key keeps the deploy commits Jira-linked.
    feat/NG-261-x -> preview/NG-261-x; NG-9-y -> preview/NG-9-y.
    """
    if head_ref.startswith("preview/"):
        return head_ref
    return "preview/" + _TYPE_PREFIX_RE.sub("", head_ref, count=1)


def push_preview(repo: str, head_ref: str) -> dict:
    """Point the preview branch for head_ref at its current SHA on origin.

    Force-updates the ref if it exists, creates it otherwise — either way the
    push event triggers the "Deploy preview branch to dev box" workflow, which
    deploys to the pushing user's box and cancels any in-flight deploy for it.
    Returns {branch, sha}; raises RuntimeError with gh's error output on failure.
    """
    try:
        ref = github.gh_json(["api", f"repos/{repo}/git/ref/heads/{head_ref}"], timeout=30)
    except RuntimeError as e:
        raise RuntimeError(f"Could not resolve {head_ref} on {repo}: {e}")
    sha = ref["object"]["sha"]

    branch = preview_branch(head_ref)
    try:
        github.gh_run(["api", "-X", "PATCH", f"repos/{repo}/git/refs/heads/{branch}",
                       "-f", f"sha={sha}", "-F", "force=true"], timeout=30)
    except RuntimeError:
        # PATCH fails when the ref doesn't exist yet — create it instead.
        try:
            github.gh_run(["api", "-X", "POST", f"repos/{repo}/git/refs",
                           "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}"], timeout=30)
        except RuntimeError as e:
            raise RuntimeError(f"Could not push {branch} on {repo}: {e}")
    return {"branch": branch, "sha": sha}


def get_deployed() -> dict:
    """Return the latest workflow run per repo for the configured DEPLOY_TARGET.

    If DEPLOY_TARGET is set (e.g. "dev-box"), only that environment's workflow is
    queried for each repo. If not set, all configured environments are queried.
    The workflow name comes from DEPLOY_TARGETS[repo][env] — the value in
    deploy_targets.json — and is passed to ``gh run list -w`` which accepts
    either a workflow filename or its display name.
    """
    target_env = config.DEPLOY_TARGET

    combos = [
        (repo, env, wf)
        for repo, envs in DEPLOY_TARGETS.items()
        for env, wf in envs.items()
        if not target_env or env == target_env
    ]

    if not combos:
        return {"environments": {}, "target_env": target_env}

    def fetch_one(combo: tuple) -> tuple:
        repo, env, workflow_name = combo
        try:
            out = github.gh_run(
                [
                    "run", "list",
                    "-R", repo,
                    "-w", workflow_name,
                    "--limit", "1",
                    "--json", "status,conclusion,headBranch,createdAt,displayTitle",
                ],
                timeout=15,
            )
            runs = json.loads(out) if out.strip() else []
            if not runs:
                return repo, env, {"repo": repo, "env": env, "error": "no runs found"}
            run = runs[0]
            return repo, env, {
                "repo": repo,
                "env": env,
                "branch": run.get("headBranch", ""),
                "status": run.get("status", ""),
                "conclusion": run.get("conclusion", ""),
                "createdAt": run.get("createdAt", ""),
                "displayTitle": run.get("displayTitle", ""),
            }
        except Exception as e:
            return repo, env, {"repo": repo, "env": env, "error": str(e)}

    by_env: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for repo, env, data in pool.map(fetch_one, combos):
            by_env.setdefault(env, []).append(data)

    return {"environments": by_env, "target_env": target_env}
