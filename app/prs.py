"""app/prs.py — PR-domain logic.

Extracted from server.py. Handles categorizing PRs, fetching and enriching
PR lists (both authored and review-requested), and check-run summarization.
"""

import concurrent.futures
import json
from app import config, github
from app.config import STATUS_ORDER, STATUS_LABELS, MY_STATUS_ORDER, MY_STATUS_LABELS


def determine_my_pr_status(pr, me):
    """Categorize one of my open PRs.

    Returns dict {status, status_label, active_commenters} or None if the
    PR should be excluded (drafts).
    """
    if pr.get("isDraft"):
        return None

    latest_reviews = (pr.get("latestReviews") or {}).get("nodes") or []
    threads = (pr.get("reviewThreads") or {}).get("nodes") or []
    comments = (pr.get("comments") or {}).get("nodes") or []
    review_decision = pr.get("reviewDecision")

    approvers = {
        (r.get("author") or {}).get("login")
        for r in latest_reviews
        if r.get("state") == "APPROVED" and config._is_human_author(r.get("author"))
    }
    approvers.discard(None)

    # Build a per-reviewer picture of their inline threads so we can tell
    # whether their CHANGES_REQUESTED is still actionable by me.
    reviewer_has_active_thread: dict = {}   # login → True if any thread is live
    reviewer_has_any_thread: dict = {}      # login → True if they left any thread
    for t in threads:
        cnodes = (t.get("comments") or {}).get("nodes") or []
        if not cnodes:
            continue
        author = cnodes[0].get("author") or {}
        if not config._is_human_author(author):
            continue
        login = author.get("login")
        if not login:
            continue
        reviewer_has_any_thread[login] = True
        # Whoever commented last decides whose court the ball is in. If I (the PR
        # author) replied last, I've already addressed it — even if the thread is
        # still open — so it shouldn't keep the PR in "has comments to address".
        last_nodes = (t.get("lastComment") or {}).get("nodes") or []
        last_author = (last_nodes[0].get("author") or {}).get("login") if last_nodes else None
        author_replied_last = last_author == me
        # A thread is "active" only if it is neither resolved nor outdated and I
        # haven't already replied. Outdated means the code changed under the
        # comment — addressed by new commits, so no longer something I need to fix.
        if not t.get("isResolved") and not t.get("isOutdated") and not author_replied_last:
            reviewer_has_active_thread[login] = True

    unresolved_inline_authors = {
        login for login, _ in reviewer_has_active_thread.items()
        if login not in approvers
    }

    review_body_authors = set()
    for r in latest_reviews:
        if r.get("state") != "CHANGES_REQUESTED":
            continue
        author = r.get("author") or {}
        if not config._is_human_author(author):
            continue
        login = author.get("login")
        if not login:
            continue
        # If the reviewer had inline threads but all are now resolved or
        # outdated, their changes-request has been addressed — the ball is
        # in their court, not mine. Exclude them from "active" so the PR
        # doesn't sit in "has comments to address" forever.
        if reviewer_has_any_thread.get(login) and not reviewer_has_active_thread.get(login):
            continue
        review_body_authors.add(login)
    review_body_authors -= approvers

    general_comment_authors = set()
    for c in comments:
        author = c.get("author") or {}
        if not config._is_human_author(author):
            continue
        login = author.get("login")
        if login and login != me and login not in approvers:
            general_comment_authors.add(login)

    active = (
        unresolved_inline_authors | review_body_authors | general_comment_authors
    )

    stale_reviewers = set()
    for r in latest_reviews:
        if r.get("state") not in ("CHANGES_REQUESTED", "COMMENTED"):
            continue
        author = r.get("author") or {}
        if not config._is_human_author(author):
            continue
        login = author.get("login")
        if login and login != me:
            stale_reviewers.add(login)

    if review_decision == "APPROVED" and not active:
        status = "approved"
    elif active:
        status = "has_comments"
    else:
        status = "not_reviewed_yet"

    any_human_review = any(config._is_human_author(r.get("author")) for r in latest_reviews)
    if stale_reviewers:
        nudge_mode = "re_review"
        nudge_targets = sorted(stale_reviewers)
    elif not any_human_review:
        nudge_mode = "fresh"
        nudge_targets = list(config.FRESH_REVIEWERS)
    else:
        nudge_mode = None
        nudge_targets = []

    return {
        "status": status,
        "status_label": MY_STATUS_LABELS[status],
        "active_commenters": sorted(active),
        "stale_reviewers": sorted(stale_reviewers),
        "nudge_mode": nudge_mode,
        "nudge_targets": nudge_targets,
    }


MY_PRS_GRAPHQL = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 50) {
    nodes {
      ... on PullRequest {
        number
        title
        url
        isDraft
        updatedAt
        headRefName
        baseRefName
        mergeStateStatus
        author { login __typename }
        repository {
          nameWithOwner
          viewerDefaultMergeMethod
        }
        reviewDecision
        latestReviews(first: 50) {
          nodes {
            author { login __typename }
            state
            submittedAt
          }
        }
        reviewThreads(first: 50) {
          nodes {
            isResolved
            isOutdated
            comments(first: 1) {
              nodes { author { login __typename } }
            }
            lastComment: comments(last: 1) {
              nodes { author { login __typename } }
            }
          }
        }
        comments(last: 50) {
          nodes {
            author { login __typename }
            createdAt
          }
        }
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
                contexts(first: 100) {
                  totalCount
                  nodes {
                    __typename
                    ... on CheckRun { name conclusion status detailsUrl }
                    ... on StatusContext { context state targetUrl }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


_CHECK_PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_CHECK_FAILED = {
    "FAILURE", "ERROR", "TIMED_OUT", "CANCELLED",
    "ACTION_REQUIRED", "STARTUP_FAILURE",
}


def summarize_checks(rollup: dict) -> dict:
    """Bucket a statusCheckRollup's contexts into passed/pending/failed.

    Normalizes both node types: CheckRun (GitHub Actions etc.) uses
    status/conclusion, while the legacy StatusContext uses a single state.
    Returns {passed: int, pending: [{name,url}], failed: [{name,url}], truncated: bool}.
    Greens are counted (not named); pending/failed are named with a details URL.
    """
    contexts = (rollup.get("contexts") or {})
    nodes = contexts.get("nodes") or []
    passed = 0
    pending: list = []
    failed: list = []
    for node in nodes:
        if node.get("__typename") == "CheckRun":
            name = node.get("name") or "check"
            url = node.get("detailsUrl") or ""
            # An incomplete CheckRun has no conclusion yet -> pending.
            verdict = node.get("conclusion") if node.get("status") == "COMPLETED" else None
        else:  # StatusContext (legacy commit status)
            name = node.get("context") or "check"
            url = node.get("targetUrl") or ""
            verdict = node.get("state")
        verdict = (verdict or "").upper()
        if verdict in _CHECK_PASSED:
            passed += 1
        elif verdict in _CHECK_FAILED:
            failed.append({"name": name, "url": url})
        else:
            pending.append({"name": name, "url": url})
    total = contexts.get("totalCount") or len(nodes)
    return {
        "passed": passed,
        "pending": pending,
        "failed": failed,
        "truncated": total > len(nodes),
    }


def pr_behind_count(repo: str, base: str, head: str) -> int:
    """Commits the PR's head branch is behind its base, via the compare API.

    GitHub's `mergeStateStatus` only reports BEHIND when an up-to-date-branch
    protection rule makes being behind the *governing* merge blocker. When a
    behind branch is also blocked by reviews/checks the status collapses to
    BLOCKED and the "behind" signal is hidden — so it can't tell us whether a
    branch can be rebased. The REST compare endpoint always exposes the real
    divergence. Returns 0 on missing refs or any error (never blocks the UI).
    """
    if not (repo and base and head):
        return 0
    try:
        out = github.gh_run([
            "api", f"repos/{repo}/compare/{base}...{head}?per_page=1",
            "--jq", ".behind_by",
        ]).strip()
    except Exception as e:
        print(f"[warn] compare failed for {repo} {base}...{head}: {e}", flush=True)
        return 0
    return int(out) if out.isdigit() else 0


def list_my_prs():
    """Return my open PRs across all repos, enriched with status."""
    me = github.get_my_login()
    q = f"is:pr is:open author:{me} archived:false"
    out = github.gh_run([
        "api", "graphql",
        "-f", f"query={MY_PRS_GRAPHQL}",
        "-f", f"q={q}",
    ])
    payload = json.loads(out) if out.strip() else {}
    nodes = (
        ((payload.get("data") or {}).get("search") or {}).get("nodes")
        or []
    )

    out_list = []
    for pr in nodes:
        if not pr:
            continue
        status = determine_my_pr_status(pr, me)
        if status is None:
            continue
        repo = (pr.get("repository") or {}).get("nameWithOwner") or ""
        commit_nodes = ((pr.get("commits") or {}).get("nodes") or [])
        rollup = (
            ((commit_nodes[0].get("commit") or {}).get("statusCheckRollup") or {})
            if commit_nodes else {}
        )
        out_list.append({
            "number": pr.get("number"),
            "title": pr.get("title") or "",
            "url": pr.get("url") or "",
            "updatedAt": pr.get("updatedAt") or "",
            "headRefName": pr.get("headRefName") or "",
            "baseRefName": pr.get("baseRefName") or "",
            "repository": repo,
            "defaultMergeMethod": (
                (pr.get("repository") or {}).get("viewerDefaultMergeMethod")
                or "MERGE"
            ),
            "review_decision": pr.get("reviewDecision") or "",
            "check_state": rollup.get("state") or "",
            "checks": summarize_checks(rollup),
            "merge_state_status": pr.get("mergeStateStatus") or "",
            **status,
        })

    # GitHub's mergeStateStatus hides "behind" behind higher-priority block
    # reasons: a PR that is behind *and* blocked by reviews/checks reports
    # BLOCKED, not BEHIND. Ask the compare API per PR for the real divergence
    # so the rebase signal fires whenever the branch can be rebased. These are
    # independent blocking `gh` round trips, so fan out (mirrors list_prs).
    def with_behind(pr):
        return {**pr, "behind_by": pr_behind_count(
            pr["repository"], pr["baseRefName"], pr["headRefName"])}

    if out_list:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(out_list))
        ) as pool:
            out_list = list(pool.map(with_behind, out_list))

    out_list.sort(key=lambda p: p["updatedAt"])
    out_list.sort(key=lambda p: MY_STATUS_ORDER[p["status"]])
    return out_list


def author_reply_count(repo, number, me, author_login, since_iso, fresh):
    """Replies from the PR author to my review comments since my last review."""
    if not author_login or not since_iso:
        return 0
    comments = github.fetch_review_comments(repo, number, fresh=fresh)
    my_ids = {
        c["id"]
        for c in comments
        if (c.get("user") or {}).get("login") == me
    }
    count = 0
    for c in comments:
        if (c.get("user") or {}).get("login") != author_login:
            continue
        in_reply = c.get("in_reply_to_id")
        if in_reply is None or in_reply not in my_ids:
            continue
        if (c.get("created_at") or "") <= since_iso:
            continue
        count += 1
    return count


def determine_status(repo, number, detail, me, fresh):
    """Apply the spec's status rules. Return dict or None to exclude."""
    reviews = detail.get("reviews") or []
    review_requests = detail.get("reviewRequests") or []
    commits = detail.get("commits") or []
    pr_author = (detail.get("author") or {}).get("login")

    my_reviews = sorted(
        (r for r in reviews
         if (r.get("author") or {}).get("login") == me
         and r.get("submittedAt")),
        key=lambda r: r["submittedAt"],
    )
    last_my_review = my_reviews[-1] if my_reviews else None
    last_my_review_at = last_my_review.get("submittedAt") if last_my_review else None

    commit_dates = [c.get("committedDate") for c in commits if c.get("committedDate")]
    last_commit_date = max(commit_dates) if commit_dates else None

    me_in_requests = any(
        (u or {}).get("login") == me for u in review_requests
    )
    re_requested = bool(last_my_review) and me_in_requests

    has_new_commits = bool(
        last_my_review and last_commit_date and last_commit_date > last_my_review_at
    )
    new_commits_count = (
        sum(
            1 for c in commits
            if (c.get("committedDate") or "") > (last_my_review_at or "")
        )
        if has_new_commits else 0
    )

    replies = 0
    if last_my_review:
        replies = author_reply_count(
            repo, number, me, pr_author, last_my_review_at, fresh,
        )

    # Exclude: I've reviewed, nothing new since.
    if last_my_review and not has_new_commits and not re_requested and replies == 0:
        return None

    # Priority: re_requested > new_commits > author_replied > untouched
    if re_requested:
        status = "re_requested"
        detail_str = "Re-requested after your last review"
    elif has_new_commits:
        status = "new_commits"
        noun = "commit" if new_commits_count == 1 else "commits"
        detail_str = f"{new_commits_count} new {noun} since your review"
    elif replies > 0:
        status = "author_replied"
        noun = "reply" if replies == 1 else "replies"
        detail_str = f"{replies} {noun} to your comments"
    elif last_my_review is None:
        # Skip PRs that another reviewer has already given a verdict on — they
        # don't need a pile-on from me unless I'm explicitly re-requested.
        latest_reviews = detail.get("latestReviews") or []
        other_verdicts = [
            r for r in latest_reviews
            if (r.get("author") or {}).get("login") not in (me, None, pr_author)
            and config._is_human_author(r.get("author"))
            and r.get("state") in ("APPROVED", "CHANGES_REQUESTED")
        ]
        if other_verdicts:
            return None
        status = "untouched"
        detail_str = None
    else:
        return None

    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "status_detail": detail_str,
    }


def list_prs(fresh=False):
    me = github.get_my_login()
    results = github.gh_json([
        "search", "prs",
        "--review-requested=@me",
        "--state=open",
        "--json", "number,title,author,repository,updatedAt,url",
    ]) or []
    candidates = []
    for pr in results:
        repo = (pr.get("repository") or {}).get("nameWithOwner")
        number = pr.get("number")
        if not repo or number is None:
            continue
        candidates.append({
            "number": number,
            "title": pr.get("title") or "",
            "author": (pr.get("author") or {}).get("login") or "",
            "repository": repo,
            "updatedAt": pr.get("updatedAt") or "",
            "url": pr.get("url") or "",
        })

    # Enrich each candidate in parallel: fetch_detail + determine_status both make
    # blocking `gh` calls, so a sequential loop is O(N) round trips. The detail cache
    # (_detail_cache/_cache_lock) is thread-safe, so fan out across a small pool.
    def enrich(pr):
        try:
            detail = github.fetch_detail(pr["repository"], pr["number"], fresh=fresh)
        except Exception as e:
            print(f"[warn] detail fetch failed for {pr['repository']}#{pr['number']}: {e}", flush=True)
            return None
        status = determine_status(pr["repository"], pr["number"], detail, me, fresh)
        if status is None:
            return None
        return {**pr, **status}

    enriched = []
    if candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
            for result in pool.map(enrich, candidates):
                if result is not None:
                    enriched.append(result)

    enriched.sort(key=lambda p: p["updatedAt"])
    enriched.sort(key=lambda p: STATUS_ORDER[p["status"]])
    return enriched
