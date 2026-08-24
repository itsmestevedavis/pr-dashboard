"""Same-origin reverse proxy for external dashboards embedded as iframes.

The reliability dashboards are plain-http hosts with no TLS listener.
Firefox's HTTPS-First upgrades cross-site http: iframe navigations to
https:, which then hangs — a white frame. Serving them same-origin under
/embed/<name>/ sidesteps the upgrade; this module fetches the http
backend on the browser's behalf (same machine, so VPN access matches).
"""

import urllib.error
import urllib.request
from typing import Optional, Tuple
from urllib.parse import urlparse

# name -> backend origin (scheme + host, no trailing slash). The frontend's
# EMBED_URLS in static/app.js points its iframes at /embed/<name>/….
EMBED_BACKENDS = {
    "reliability-stg": "http://reliability.stg.internal.cognota.com",
    "reliability-prod": "http://reliability.prod.cognota.com",
}

_TIMEOUT_S = 15
_MAX_BODY_BYTES = 64 * 1024 * 1024


def resolve(path: str, referer: Optional[str]) -> Optional[Tuple[str, str]]:
    """Map a request path to (backend_name, upstream_path), or None.

    /embed/<name>/<rest> maps directly. Any other path maps via the Referer
    of the embedding page — the dashboards fetch a few absolute paths (e.g.
    /sources/...) that land outside the /embed/ prefix. Because of that
    fallback, callers must try every real route first and use this last.
    """
    if path == "/embed" or path.startswith("/embed/"):
        rest = path[len("/embed/"):] if path.startswith("/embed/") else ""
        name, _, subpath = rest.partition("/")
        return (name, "/" + subpath) if name in EMBED_BACKENDS else None
    ref_path = urlparse(referer or "").path
    if ref_path.startswith("/embed/"):
        name = ref_path[len("/embed/"):].partition("/")[0]
        if name in EMBED_BACKENDS:
            return (name, path)
    return None


def fetch(name: str, upstream_path: str, query: str) -> Tuple[int, str, bytes]:
    """GET the upstream resource; returns (status, content_type, body).

    4xx/5xx upstream responses are passed through rather than raised, so the
    embedded page sees its own error semantics. Network failures propagate.
    """
    url = EMBED_BACKENDS[name] + upstream_path + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": "pr-dashboard-embed-proxy"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return (
                resp.status,
                resp.headers.get("Content-Type", "application/octet-stream"),
                resp.read(_MAX_BODY_BYTES),
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            e.headers.get("Content-Type", "text/plain"),
            e.read(_MAX_BODY_BYTES),
        )
