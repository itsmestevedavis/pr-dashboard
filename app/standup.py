"""app/standup.py — Team-tab standup summary.

Builds a short "what to say at standup" summary from the team's sprint tickets
(app.team.team_overview) and the user's own open PRs (app.prs.list_my_prs) by
prompting the local `claude` CLI, then caches the result to disk so the Team tab
can serve it instantly. Generation is button-only: `force=False` never calls
`claude` (it only reads the cache), `force=True` regenerates.

All Jira/config values are read at call time via `config.<NAME>` so live settings
changes are picked up without a restart (same contract as app/team.py).
"""

import datetime
import json
import os
import subprocess

from app import config, team, prs

# Cache of the last generated summary, alongside jira_team.json under the shared
# config dir. Shape: {generated_at, team, me}.
_CACHE_PATH = os.path.join(os.path.expanduser("~/.config/pr-dashboard"), "standup.json")

# `claude -p` is a few-second call for two sentences; cap it so a wedged CLI can't
# tie up the request thread indefinitely.
_CLAUDE_TIMEOUT = 120  # seconds


def _now_iso():
    """Current UTC time as an ISO-8601 string (for the 'generated N ago' label)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_cache():
    """Load the cached summary dict; None if missing/corrupt."""
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_cache(data):
    """Persist the summary atomically, creating the config dir if needed.

    Write-to-temp + os.replace so concurrent POSTs (e.g. two browser tabs) can't
    interleave and leave a half-written standup.json — the reader always sees a
    whole file or the previous one.
    """
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    tmp = f"{_CACHE_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _CACHE_PATH)


def _result(configured=True, cached=False, generated_at=None, team_line="", me_line="", error=None):
    """Build the standard endpoint payload (single shape for every path)."""
    return {
        "configured": configured,
        "cached": cached,
        "generated_at": generated_at,
        "team": team_line,
        "me": me_line,
        "error": error,
    }


def _safe_my_prs():
    """My open PRs, best-effort. A GitHub hiccup shouldn't sink the whole summary —
    the personal line is *enriched* by PRs, not dependent on them. Log and degrade."""
    try:
        return prs.list_my_prs()
    except Exception as e:
        print(f"[standup] WARNING: couldn't fetch my PRs, continuing without them: {e}", flush=True)
        return []


def _build_prompt(overview, my_prs, my_email):
    """Assemble the full prompt: the STANDUP_PROMPT instructions + a data block.

    Pure function (no I/O), so it's unit-testable without a subprocess. The data
    block lists the sprint goal, each teammate's tickets (marking "(me)"), and my
    open PRs with their review/CI state.
    """
    me = (my_email or "").lower()
    lines = []

    sprint = overview.get("sprint")
    if sprint:
        goal = sprint.get("goal") or "(no goal set)"
        lines.append(f"Active sprint: {sprint.get('name')} — Goal: {goal}")
    else:
        lines.append("No active sprint.")

    lines.append("")
    lines.append("Team members and their tickets this sprint:")
    for p in overview.get("people", []):
        name = p.get("displayName") or p.get("email") or "Unknown"
        tag = " (me)" if (p.get("email") or "").lower() == me else ""
        tickets = p.get("tickets") or []
        if not tickets:
            lines.append(f"- {name}{tag}: no tickets in the sprint")
            continue
        lines.append(f"- {name}{tag}:")
        for t in tickets:
            lines.append(
                f"    - {t.get('key')} [{t.get('status_label') or ''}] {t.get('summary') or ''}"
            )

    lines.append("")
    lines.append("My open pull requests:")
    if my_prs:
        for pr in my_prs:
            bits = [pr.get("title") or "(untitled)"]
            if pr.get("repository"):
                bits.append(f"in {pr['repository']}")
            state = pr.get("status_label") or pr.get("review_decision")
            if state:
                bits.append(f"— {state}")
            if pr.get("check_state"):
                bits.append(f"checks: {pr['check_state']}")
            if pr.get("behind_by"):
                bits.append(f"behind base by {pr['behind_by']}")
            lines.append("- " + " ".join(str(b) for b in bits))
    else:
        lines.append("- (none)")

    return config.STANDUP_PROMPT + "\n\n" + "\n".join(lines)


def _run_claude(prompt):
    """Run `claude -p` synchronously and return its result text. Raises on failure.

    Safe to block here: the server is a ThreadingHTTPServer, so this ties up only
    the current request thread. `--output-format json` gives a single envelope.

    Security: the prompt embeds untrusted text — teammates' Jira ticket summaries
    and PR titles — so this is a prompt-injection surface. Unlike the review/fix
    jobs in runners.py, generating standup text needs NO tools, so we run with
    `--tools ""` (all tools disabled) and WITHOUT bypassPermissions. A malicious
    ticket can then at worst produce junk text (escaped on render), never a tool
    call. Do not add bypassPermissions here to "match" the other jobs.
    """
    proc = subprocess.run(
        ["claude", "-p", prompt,
         "--tools", "",
         "--output-format", "json"],
        capture_output=True, text=True, timeout=_CLAUDE_TIMEOUT,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"claude exited {proc.returncode}: {err[:200]}")
    try:
        envelope = json.loads(proc.stdout)
    except ValueError as e:
        raise RuntimeError(f"claude returned non-JSON output: {e}")
    text = envelope.get("result")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("claude returned no result text")
    return text


def _extract_json_obj(text):
    """Parse a JSON object out of claude's result — directly, or the first {...}
    slice (handles a stray code fence or prose the model added). None on failure."""
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    return None


def _parse_result(text):
    """Turn claude's result text into {'team', 'me'}. Falls back to the raw text as
    the team line (empty me) if it isn't the JSON we asked for, so the user still
    gets something rather than an error."""
    obj = _extract_json_obj(text)
    if obj is not None and ("team" in obj or "me" in obj):
        return {
            "team": str(obj.get("team") or "").strip(),
            "me": str(obj.get("me") or "").strip(),
        }
    return {"team": text.strip(), "me": ""}


def standup_summary(force=False):
    """Return the standup summary payload (see _result for the shape).

    force=False (GET / tab load): return the cached summary, or an empty
    "not generated yet" result. Never calls claude.
    force=True (POST / button): regenerate from live Jira + PR data, cache it,
    and return it. Returns a not-configured result (no claude call) when the Team
    tab isn't configured, and an error result (uncached) when generation fails.
    """
    if not force:
        cached = _load_cache()
        if cached:
            return _result(cached=True, generated_at=cached.get("generated_at"),
                           team_line=cached.get("team") or "", me_line=cached.get("me") or "")
        return _result()  # configured=True, empty — "click Generate"

    if not team.team_configured():
        return _result(configured=False)

    overview = team.team_overview()
    prompt = _build_prompt(overview, _safe_my_prs(), config.JIRA_EMAIL)

    try:
        raw = _run_claude(prompt)
    except Exception as e:
        print(f"[standup] generation failed: {e}", flush=True)
        return _result(error=str(e))

    parsed = _parse_result(raw)
    generated_at = _now_iso()
    _save_cache({"generated_at": generated_at, "team": parsed["team"], "me": parsed["me"]})
    return _result(generated_at=generated_at, team_line=parsed["team"], me_line=parsed["me"])
