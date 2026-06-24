"""Job-POST routing table and request-extraction helpers.

`_JOB_POST_ROUTES` maps URL paths to (job_kind, runner_fn, extract_fn) tuples.
The dispatcher in Handler._dispatch_job_post handles the common
number/repo parse, job creation, thread spawn, and 202 envelope;
`extract_fn` returns the extra positional args each runner needs (and may
raise to reject the request).
"""

from app.jobs import runners


def _req_ref(data, key):
    """Pull a required, non-empty ref string from a request body."""
    val = str(data[key])
    if not val:
        raise ValueError(f"{key} required")
    return val


def _extract_merge(d):
    return (str(d.get("defaultMergeMethod") or "MERGE"),)


# path -> (job kind, runner fn, extract-extra-args fn or None).
_JOB_POST_ROUTES = {
    "/api/review":       ("review",       runners.run_review,       None),
    "/api/re-review":    ("re_review",    runners.run_re_review,    None),
    "/api/merge":        ("merge",        runners.run_merge,        _extract_merge),
    "/api/address":      ("address",      runners.run_address,      lambda d: (_req_ref(d, "headRefName"),)),
    "/api/fix-pipeline": ("fix_pipeline", runners.run_fix_pipeline, lambda d: (_req_ref(d, "headRefName"),)),
    "/api/rebase":       ("rebase",       runners.run_rebase,       lambda d: (_req_ref(d, "headRefName"), _req_ref(d, "baseRefName"))),
}
