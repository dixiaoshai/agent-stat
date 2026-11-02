import json
import re
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_stats.server import make_server


class StubStore:
    def query(self, filters):
        if filters.get("start") == "bad":
            raise ValueError("无效时间")
        return {"filters": filters, "summary": {"token_total": None}}

    def detail(self, identity, filters=None):
        return {"session": {"id": identity}, "turns": []} if identity == "known" else None

    def import_paths(self, paths, instance):
        return {"paths": paths, "instance": instance}


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(StubStore(), ["synthetic"], 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, data=None, headers=None):
        request = Request(self.base + path, data=data, headers=headers or {})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.headers, response.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_filters_and_missing(self):
        status, _, body = self.request("/api/overview?model=synthetic&start=2026-01-01T00%3A00%3A00Z")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["filters"]["model"], "synthetic")
        self.assertIsNone(json.loads(body)["summary"]["token_total"])
        self.assertEqual(self.request("/api/session?id=missing")[0], 404)
        self.assertEqual(self.request("/api/overview?start=bad")[0], 400)

    def test_no_remote_origin_or_host(self):
        self.assertEqual(self.request("/api/overview", headers={"Host": "evil.invalid"})[0], 403)
        self.assertEqual(self.request("/api/overview", headers={"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(self.request("/api/refresh", b"{}", {"Origin": "https://evil.invalid", "Content-Type": "application/json"})[0], 403)

    def test_refresh_is_fixed_source_and_json_only(self):
        status, _, body = self.request("/api/refresh", b'{"paths":["/not-allowed"]}', {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["paths"], ["synthetic"])
        self.assertEqual(self.request("/api/refresh", b"{}")[0], 415)
        self.assertEqual(self.request("/api/refresh", b"bad", {"Content-Type": "application/json"})[0], 400)

    def test_static_and_traversal(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        assets = re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', body)
        self.assertTrue(assets, 'React production assets must be built before testing')
        for asset in assets:
            asset_status, asset_headers, asset_body = self.request(asset.decode())
            self.assertEqual(asset_status, 200)
            self.assertTrue(asset_body)
            self.assertIn('text/', asset_headers['Content-Type'])
        self.assertEqual(self.request("/../README.md")[0], 404)
        self.assertEqual(self.request("/assets/../../README.md")[0], 404)
        self.assertEqual(self.request("/assets/missing.js")[0], 404)


if __name__ == "__main__":
    unittest.main()
