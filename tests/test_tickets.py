import json
import unittest
import sys
import os
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


class JiraRequestTest(unittest.TestCase):
    @mock.patch.object(server, "JIRA_API_TOKEN", "tok")
    @mock.patch.object(server, "JIRA_EMAIL", "me@example.com")
    @mock.patch.object(server, "JIRA_SITE", "ex.atlassian.net")
    @mock.patch("server.urllib.request.urlopen")
    def test_get_sets_auth_and_parses_json(self, urlopen):
        resp = mock.MagicMock()
        resp.read.return_value = b'{"ok": true}'
        resp.__enter__.return_value = resp
        urlopen.return_value = resp

        out = server.jira_request("GET", "/rest/api/3/myself")

        self.assertEqual(out, {"ok": True})
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://ex.atlassian.net/rest/api/3/myself")
        self.assertTrue(req.get_header("Authorization").startswith("Basic "))


if __name__ == "__main__":
    unittest.main()
