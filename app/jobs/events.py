"""app/jobs/events.py — Claude stream-event formatting and result derivation."""

import re

from app import github

_RE_APPROVE = re.compile(r"gh\s+pr\s+review\b[^|;&]*--approve")
_RE_REVIEW_API = re.compile(r"gh\s+api\s+repos/[^\s]+/pulls/\d+/reviews\b")
_RE_COMMENTS_API = re.compile(r"gh\s+api\s+repos/[^\s]+/pulls/\d+/comments\b")


def format_event(ev):
    """Turn a stream-json event into a one-line human-readable string, or None to skip."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        model = ev.get("model") or "?"
        return f"Started session ({model})"
    if t == "assistant":
        for c in ev.get("message", {}).get("content", []):
            ct = c.get("type")
            if ct == "text":
                txt = (c.get("text") or "").strip()
                if txt:
                    return f"💬 {txt[:240]}"
            elif ct == "tool_use":
                name = c.get("name") or "?"
                inp = c.get("input") or {}
                if name == "Bash":
                    cmd = (inp.get("command") or "").splitlines()[0]
                    return f"$ {cmd[:240]}"
                if name == "Read":
                    return f"Read {inp.get('file_path', '?')}"
                if name == "Edit":
                    return f"Edit {inp.get('file_path', '?')}"
                if name == "Write":
                    return f"Write {inp.get('file_path', '?')}"
                if name == "Grep":
                    return f"Grep {inp.get('pattern', '?')}"
                if name == "Glob":
                    return f"Glob {inp.get('pattern', '?')}"
                if name == "Task":
                    desc = inp.get("description") or inp.get("subagent_type") or "?"
                    return f"→ subagent: {desc}"
                if name == "WebFetch":
                    return f"Fetch {inp.get('url', '?')}"
                return f"→ {name}"
    if t == "user":
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") == "tool_result" and c.get("is_error"):
                content = c.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        x.get("text", "") for x in content if x.get("type") == "text"
                    )
                return f"⚠️ tool error: {str(content)[:200]}"
    return None


def count_pending_comments(repo, number, me):
    """Sum comments across all of my pending reviews on this PR."""
    try:
        reviews = github.gh_json([
            "api", f"repos/{repo}/pulls/{number}/reviews", "--paginate",
        ]) or []
    except Exception:
        return 0
    total = 0
    for r in reviews:
        if r.get("state") != "PENDING":
            continue
        if (r.get("user") or {}).get("login") != me:
            continue
        rid = r.get("id")
        try:
            comments = github.gh_json([
                "api",
                f"repos/{repo}/pulls/{number}/reviews/{rid}/comments",
                "--paginate",
            ]) or []
        except Exception:
            continue
        total += len(comments)
    return total


def derive_result(events, repo, number, me):
    """Look at the tool_use stream + GH state to figure out what claude actually did."""
    approves = 0
    review_calls = 0
    comment_calls = 0
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") != "tool_use" or c.get("name") != "Bash":
                continue
            cmd = (c.get("input") or {}).get("command") or ""
            if _RE_APPROVE.search(cmd):
                approves += 1
            if _RE_REVIEW_API.search(cmd):
                review_calls += 1
            if _RE_COMMENTS_API.search(cmd):
                comment_calls += 1
    if approves > 0:
        return "approved"
    if review_calls > 0 or comment_calls > 0:
        n = count_pending_comments(repo, number, me)
        if n <= 0:
            # fallback: at least one comment per API call observed
            n = max(review_calls, comment_calls)
        return f"commented:{n}"
    return "no_action"


def count_slack_sends(events):
    """Count `slack_send_message` tool calls in a Claude stream (excluding drafts)."""
    sent = 0
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") != "tool_use":
                continue
            name = c.get("name") or ""
            if "slack_send_message" in name and "draft" not in name:
                sent += 1
    return sent


def derive_nudge_result(events, mode="re_review"):
    sent = count_slack_sends(events)
    if sent == 0:
        return {
            "sent": 0,
            "label": "Channel post failed" if mode == "channel" else "No DMs sent",
        }
    if mode == "channel":
        label = "Posted in team channel"
    else:
        label = f"DM'd {sent} reviewer{'s' if sent != 1 else ''}"
    return {"sent": sent, "label": label}
