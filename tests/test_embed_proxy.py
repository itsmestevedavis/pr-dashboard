"""Route resolution for the /embed/ reverse proxy (app/http/embed_proxy.py)."""

import unittest

from app.http import embed_proxy


class ResolveTest(unittest.TestCase):
    def test_direct_embed_path_maps_to_backend_root(self):
        self.assertEqual(
            embed_proxy.resolve("/embed/reliability-stg/", None),
            ("reliability-stg", "/"),
        )

    def test_direct_embed_subpath(self):
        self.assertEqual(
            embed_proxy.resolve("/embed/reliability-prod/index.html", None),
            ("reliability-prod", "/index.html"),
        )

    def test_unknown_embed_name_is_none(self):
        self.assertIsNone(embed_proxy.resolve("/embed/nope/index.html", None))

    def test_missing_trailing_slash_maps_to_root(self):
        # /embed/<name> (no slash) still resolves; the page's relative links
        # resolve correctly because the iframe src always includes the slash.
        self.assertEqual(
            embed_proxy.resolve("/embed/reliability-stg", None),
            ("reliability-stg", "/"),
        )

    def test_absolute_path_via_referer(self):
        # The embedded dashboards fetch a few absolute paths (e.g. /sources/…);
        # those requests carry the embedding page's URL as the Referer.
        referer = "http://127.0.0.1:8765/embed/reliability-stg/"
        self.assertEqual(
            embed_proxy.resolve("/sources/pipeline/index.json", referer),
            ("reliability-stg", "/sources/pipeline/index.json"),
        )

    def test_absolute_path_without_referer_is_none(self):
        self.assertIsNone(embed_proxy.resolve("/sources/pipeline/index.json", None))

    def test_absolute_path_with_non_embed_referer_is_none(self):
        referer = "http://127.0.0.1:8765/?tab=mine"
        self.assertIsNone(embed_proxy.resolve("/sources/pipeline/index.json", referer))

    def test_referer_with_unknown_name_is_none(self):
        referer = "http://127.0.0.1:8765/embed/nope/"
        self.assertIsNone(embed_proxy.resolve("/sources/x.json", referer))


if __name__ == "__main__":
    unittest.main()
