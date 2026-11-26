"""GET /docs (README.md rendered as HTML) and the /health "docs" field.

Run: python3 -m unittest discover -s tests -t .   (needs pyserial importable)
"""

import http.client
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

os.environ.setdefault("ESP32_READY_TIMEOUT_S", "3")
import plugin_server as ps  # noqa: E402
from test_plugin import Harness  # noqa: E402

TOKEN = "0123456789abcdef-test-token"
CSP = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


def get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def first_h1(readme_path):
    with open(readme_path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^#\s+(.*?)\s*$", line)
            if m:
                return m.group(1)
    raise AssertionError("README.md has no level-1 heading")


class DocsTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.port = self.h.server.port

    def test_readme_path_is_repo_root(self):
        self.assertEqual(ps.README_PATH, os.path.join(ROOT, "README.md"))

    def test_docs_renders_readme(self):
        code, headers, body = get(self.port, "/docs")
        self.assertEqual(code, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["content-security-policy"], CSP)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["cache-control"], "no-store")
        page = body.decode("utf-8")
        self.assertTrue(page.startswith("<!DOCTYPE html>"))
        self.assertIn(">" + first_h1(ps.README_PATH) + "</h1>", page)
        self.assertIn("<title>esp32-sensor documentation</title>", page)
        self.assertNotIn("<script", page)

    def test_health_advertises_docs(self):
        code, _, body = get(self.port, "/health")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["docs"], "/docs")

    def test_missing_readme(self):
        missing = os.path.join(tempfile.mkdtemp(), "README.md")
        with mock.patch.object(ps, "README_PATH", missing):
            code, _, body = get(self.port, "/health")
            self.assertEqual(code, 200)
            self.assertNotIn("docs", json.loads(body))
            code, headers, body = get(self.port, "/docs")
            self.assertEqual(code, 404)
            self.assertEqual(headers["content-type"], "application/json")
            self.assertEqual(json.loads(body)["code"], "not_found")


class DocsTokenTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(token=TOKEN)
        self.addCleanup(self.h.close)
        self.port = self.h.server.port

    def test_docs_needs_token(self):
        for headers in ({}, {"X-Plugin-Token": ""}, {"X-Plugin-Token": "wrong"}):
            code, _, body = get(self.port, "/docs", headers)
            self.assertEqual(code, 401, headers)
            self.assertEqual(json.loads(body)["code"], "unauthorized")
        code, headers, _ = get(self.port, "/docs", {"X-Plugin-Token": TOKEN})
        self.assertEqual(code, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")

    def test_health_still_open_with_docs(self):
        code, _, body = get(self.port, "/health")
        self.assertEqual(code, 200)
        r = json.loads(body)
        self.assertEqual(r["docs"], "/docs")
        self.assertNotIn("host", r)


if __name__ == "__main__":
    unittest.main()
