"""Git branch / worktree cleanup: scanning and deletion.

All git access goes through an injected ``run(args, cwd)`` callable returning
``(returncode, stdout, stderr)``, so the scan/parse/delete logic is unit-testable
without a real repository. ``server.py`` supplies a subprocess-backed runner that
prepends ``git -C <cwd>``.
"""
import re


def _ok(run, args, cwd):
    """Run a read-only git query; return stdout on success, else ''."""
    code, out, _err = run(args, cwd)
    return out if code == 0 else ""


def default_branch(run, path):
    """Best-effort default branch name (no ``origin/`` prefix)."""
    out = _ok(run, ["symbolic-ref", "refs/remotes/origin/HEAD"], path).strip()
    if out:
        return out.rsplit("/", 1)[-1]
    branches = {
        line.strip()
        for line in _ok(run, ["branch", "-a", "--format=%(refname:short)"], path).splitlines()
    }
    for cand in ("develop", "main", "master"):
        if cand in branches or f"origin/{cand}" in branches:
            return cand
    return "main"


def current_branch(run, path):
    """Name of the checked-out branch in the main worktree ('' if detached)."""
    return _ok(run, ["branch", "--show-current"], path).strip()


def parse_gone(branch_vv_output):
    """Local branch names whose upstream is gone (from ``git branch -vv``)."""
    names = []
    for line in branch_vv_output.splitlines():
        if ": gone]" not in line:
            continue
        m = re.match(r"^[*+]?\s*(\S+)", line)
        if m:
            names.append(m.group(1))
    return names


def parse_worktrees(porcelain):
    """Parse ``git worktree list --porcelain`` into entry dicts.

    Each entry: {path, branch (short name or None), detached, bare, is_main}.
    The first entry is always the repo's main worktree.
    """
    entries = []
    cur = None
    for line in porcelain.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = None
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = {"path": val, "branch": None, "detached": False, "bare": False}
        elif cur is None:
            continue
        elif key == "branch":
            cur["branch"] = val.rsplit("/", 1)[-1] if val else None
        elif key == "detached":
            cur["detached"] = True
        elif key == "bare":
            cur["bare"] = True
    if cur:
        entries.append(cur)
    for i, e in enumerate(entries):
        e["is_main"] = i == 0
    return entries


def parse_merged(branch_merged_output, exclude):
    """Branch names from ``git branch --merged`` minus the excluded set."""
    names = []
    for line in branch_merged_output.splitlines():
        name = line.replace("*", "").strip()
        if name and name not in exclude:
            names.append(name)
    return names


def parse_remote_merged(output, default, remote="origin"):
    """Remote branch short names from ``git branch -r --merged``.

    Skips the symbolic ``HEAD ->`` line and the default branch.
    """
    names = []
    prefix = remote + "/"
    for line in output.splitlines():
        s = line.strip()
        if not s or "->" in s or not s.startswith(prefix):
            continue
        name = s[len(prefix):]
        if name in ("HEAD", default):
            continue
        names.append(name)
    return names


def branch_authors(run, path):
    """Map ref short-name -> tip-commit author email (lowercased), one git call.

    Covers local (refs/heads, key e.g. "feature") and remote-tracking
    (refs/remotes, key e.g. "origin/feature") branches.
    """
    out = _ok(run, [
        "for-each-ref", "--format=%(refname:short)%09%(authoremail)",
        "refs/heads", "refs/remotes",
    ], path)
    authors = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, _, email = line.partition("\t")
        authors[name.strip()] = email.strip().strip("<>").lower()
    return authors


def scan_repo(run, path, author_email=None):
    """Classify cleanup candidates for one git repo.

    Returns dicts: {kind, name, reason, mine, author, worktree_path?}. A branch
    checked out in a linked worktree is surfaced as a ``worktree`` candidate (not
    a local branch) since the worktree must be removed first.

    ``mine`` marks candidates whose tip-commit author is "me" — ``author_email``
    if given, else the repo's ``git config user.email``. When "me" can't be
    determined, every candidate is treated as mine (so a UI filter never hides
    everything).
    """
    default = default_branch(run, path)
    current = current_branch(run, path)
    exclude = {default, current, ""}

    me = (author_email or _ok(run, ["config", "user.email"], path).strip()).lower()
    authors = branch_authors(run, path)

    def annotate(lookup_name):
        author = authors.get(lookup_name, "")
        mine = True if not me else (author == me)
        return author, mine

    gone = set(parse_gone(_ok(run, ["branch", "-vv"], path)))
    merged = set(parse_merged(_ok(run, ["branch", "--merged", default], path), exclude))

    candidates = []

    # Worktrees first, so we can skip their branches in the local lists.
    wt_branches = {}
    for wt in parse_worktrees(_ok(run, ["worktree", "list", "--porcelain"], path)):
        if wt.get("is_main") or wt.get("bare"):
            continue
        branch = wt.get("branch")
        if branch and (branch in gone or branch in merged):
            author, mine = annotate(branch)
            candidates.append({
                "kind": "worktree",
                "name": branch,
                "worktree_path": wt["path"],
                "reason": "branch gone" if branch in gone else f"merged to {default}",
                "author": author,
                "mine": mine,
            })
            wt_branches[branch] = wt["path"]

    for branch in sorted(gone):
        if branch in exclude or branch in wt_branches:
            continue
        author, mine = annotate(branch)
        candidates.append({"kind": "local_gone", "name": branch,
                           "reason": "upstream gone", "author": author, "mine": mine})

    for branch in sorted(merged):
        if branch in exclude or branch in wt_branches or branch in gone:
            continue
        author, mine = annotate(branch)
        candidates.append({"kind": "local_merged", "name": branch,
                           "reason": f"merged to {default}", "author": author, "mine": mine})

    for branch in parse_remote_merged(
        _ok(run, ["branch", "-r", "--merged", f"origin/{default}"], path), default
    ):
        author, mine = annotate(f"origin/{branch}")
        candidates.append({"kind": "remote_merged", "name": branch,
                           "reason": f"merged to {default}", "author": author, "mine": mine})

    return candidates


def _safe_arg(value):
    """Reject values that could be smuggled to git as a flag (argv injection).

    Git refs/paths never legitimately start with '-', so refusing them closes the
    flag-smuggling hole (e.g. a branch named '--exec=...') with no false positives.
    """
    value = value or ""
    if value.startswith("-"):
        raise ValueError(f"refusing argument that looks like a flag: {value!r}")
    return value


def delete_candidate(run, action):
    """Execute one delete action. Returns (ok, error_or_None).

    Caller is responsible for authorizing the repo path and confirming the action
    was surfaced by a scan; this builds and runs the git command. Untrusted names
    are guarded with `--` (end of options) and a leading-dash rejection.
    """
    kind = action.get("kind")
    name = action.get("name") or ""
    path = action.get("repo_path") or ""
    force = bool(action.get("force"))

    try:
        if kind in ("local_gone", "local_merged"):
            args = ["branch", "-D" if force else "-d", "--", _safe_arg(name)]
        elif kind == "worktree":
            args = ["worktree", "remove"] + (["--force"] if force else [])
            args += ["--", _safe_arg(action.get("worktree_path"))]
        elif kind == "remote_merged":
            # `git push` refspec parsing does not honor `--`; the leading-dash
            # rejection in _safe_arg is the guard here.
            args = ["push", "origin", "--delete", _safe_arg(name)]
        else:
            return False, f"unknown kind: {kind}"
    except ValueError as e:
        return False, str(e)

    code, out, err = run(args, path)
    if code == 0:
        return True, None
    return False, (err or out or "git command failed").strip()
