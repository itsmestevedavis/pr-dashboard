"""app/workflows.py — default workflow templates and workflow-file loader.

Holds the five _DEFAULT_*_WORKFLOW markdown strings and _load_workflow().
Workflow file *paths* live in app/config.py; this module only reads them.
"""


_DEFAULT_REVIEW_WORKFLOW = """\
## Review steps

1. Read the PR title, description, and metadata:
   `gh pr view {number} --repo {repo}`

2. Get the list of changed files and their individual patches (do NOT use `gh pr diff` — it produces one giant file that is too large):
   `gh api repos/{repo}/pulls/{number}/files --paginate`
   This returns JSON. Each entry has: filename, patch, additions, deletions, status.
   Review each file's patch field one at a time.

3. Review the changes for bugs, logic errors, missing edge cases, and style issues.

4. Post inline comments on specific lines where needed:
   `gh api repos/{repo}/pulls/{number}/comments --method POST -f body="..." -f commit_id="<sha from step 1>" -f path="<filename>" -F line=<line number>`

5. Submit your final review:
   - If the code is good: `gh pr review {number} --repo {repo} --approve --body "..."`
   - If changes are needed: `gh pr review {number} --repo {repo} --request-changes --body "..."`
   - If you only want to comment: `gh pr review {number} --repo {repo} --comment --body "..."`

Do not use `gh pr diff`. Do not ask the user any questions. Complete the review autonomously.
"""

_DEFAULT_ADDRESS_WORKFLOW = """\
## Address steps

For each open review thread on this PR:

1. Decide: apply the fix in code, or reply explaining why the change is not appropriate.

2. If fixing:
   - Edit the relevant file.
   - Commit with a clear message.

3. If replying without a code change:
   - `gh api repos/{repo}/pulls/{number}/comments/<comment_id>/replies --method POST -f body="..."`
   - Or: `gh pr comment {number} --repo {repo} --body "..."`

After all threads are addressed:

4. Push: `git push origin {local_branch}:{head_ref}`

5. Re-request review from original reviewers:
   `gh api repos/{repo}/pulls/{number}/requested_reviewers --method POST -f "reviewers[]=<login>"`

Do not ask questions. Do not open new PRs. Only modify files referenced in the comments.
"""

_DEFAULT_FIX_PIPELINE_WORKFLOW = """\
## Fix failing pipeline steps

1. Find the failing checks on this PR:
   `gh pr checks {number} --repo {repo}`

2. Identify the failing workflow run ID from the output and fetch its logs:
   `gh run view <run-id> --repo {repo} --log-failed`

   If the run ID is not obvious from the checks output, list recent runs:
   `gh run list --repo {repo} --branch {head_ref} --limit 5`

3. Read the error output carefully. Identify the root cause (failing test,
   type error, lint violation, build error, etc.).

4. Open the relevant source files and fix the issue. Make the smallest
   change that makes the check pass — do not refactor unrelated code.

5. Commit the fix with a clear message explaining what was broken and why.

6. Push: `git push origin {local_branch}:{head_ref}`

Do not ask questions. Do not open new PRs. Do not modify files unrelated to the failure.
"""

_DEFAULT_REBASE_WORKFLOW = """\
## Rebase steps

1. Fetch the latest from origin:
   `git fetch origin`

2. Rebase onto the base branch:
   `git rebase origin/{base_ref}`

3. If there are conflicts, resolve them:
   - For each conflicted file, open it and resolve the conflict markers.
   - Prefer the intent of this branch's changes — do not silently discard them.
   - Stage resolved files: `git add <file>`
   - Continue: `git rebase --continue`
   - Repeat until no conflicts remain.

4. Push the rebased branch:
   `git push origin {local_branch}:{head_ref} --force-with-lease`

Do not ask questions. Do not open new PRs. Do not squash commits unless explicitly required.
"""

_DEFAULT_RE_REVIEW_WORKFLOW = """\
## Re-review steps

You are re-reviewing this PR. Your job is NOT to do a fresh review — focus exclusively on
whether your previous comments have been addressed, and check what changed since your last review.

1. Fetch your previous review comments to understand what you originally flagged:
   `gh api repos/{repo}/pulls/{number}/comments --paginate`
   Filter for comments where `user.login` is your GitHub login.
   Also check review-level feedback:
   `gh api repos/{repo}/pulls/{number}/reviews --paginate`

2. Find your most recent review's commit SHA from the reviews list (field: `commit_id`).
   Compare what changed since then:
   `gh api "repos/{repo}/pulls/{number}/files" --paginate`
   This gives the full diff. Focus only on files you previously commented on.

3. For each of your original threads:
   - If `resolved: true` — the author addressed it. No action needed.
   - If the relevant code has changed in a way that addresses your concern — approve the thread's intent,
     even if the thread is still technically open.
   - If your concern is unaddressed — note it in your final review body.

4. Submit your verdict:
   - All concerns resolved: `gh pr review {number} --repo {repo} --approve --body "..."`
   - Concerns remain: `gh pr review {number} --repo {repo} --request-changes --body "..."`
   - Progress acknowledged, minor notes: `gh pr review {number} --repo {repo} --comment --body "..."`

Do not leave new inline comments on code unrelated to your original feedback.
Do not re-review files you never commented on in your original review.
Do not ask questions. Complete the re-review autonomously.
"""


def _load_workflow(path):
    """Read a workflow .md file. Raises FileNotFoundError if missing."""
    with open(path) as f:
        return f.read().strip()
