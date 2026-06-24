"""Serve the dashboard's static assets from disk."""
import os

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "static")
_STATIC_DIR = os.path.abspath(_STATIC_DIR)

_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}


def serve_index(config_json: str) -> bytes:
    """Read static/index.html and inject the per-request config JSON."""
    with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    return html.replace("__PR_DASHBOARD_CONFIG__", config_json).encode("utf-8")


def serve_asset(rel_path: str) -> tuple[bytes, str]:
    """Return (bytes, content_type) for a file under static/. Blocks traversal."""
    safe = os.path.normpath(rel_path).lstrip("/")
    full = os.path.abspath(os.path.join(_STATIC_DIR, safe))
    if not full.startswith(_STATIC_DIR + os.sep):
        raise FileNotFoundError(rel_path)
    with open(full, "rb") as f:
        body = f.read()
    ext = os.path.splitext(full)[1]
    return body, _CONTENT_TYPES.get(ext, "application/octet-stream")
