from app.http import static_files


def test_serve_index_injects_config():
    out = static_files.serve_index('{"x":1}')
    assert b'window.PR_DASHBOARD_CONFIG = {"x":1};' in out
    assert b"__PR_DASHBOARD_CONFIG__" not in out


def test_serve_asset_js_content_type():
    body, ctype = static_files.serve_asset("app.js")
    assert ctype == "text/javascript; charset=utf-8"
    assert b"window.PR_DASHBOARD_CONFIG" in body


def test_serve_asset_blocks_traversal():
    import pytest
    with pytest.raises(FileNotFoundError):
        static_files.serve_asset("../server.py")
