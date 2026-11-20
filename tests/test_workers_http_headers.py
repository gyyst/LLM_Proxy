"""Workers 上游请求头过滤的单元测试。"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "workers"))

sys.modules.setdefault("js", types.ModuleType("js"))
_pyodide = sys.modules.setdefault("pyodide", types.ModuleType("pyodide"))
_ffi = types.ModuleType("pyodide.ffi")
_ffi.create_proxy = lambda value: value
_ffi.run_sync = lambda value: value
_ffi.to_js = lambda value: value
_pyodide.ffi = _ffi
sys.modules.setdefault("pyodide.ffi", _ffi)

_package = sys.modules.setdefault("gateway_runtime", types.ModuleType("gateway_runtime"))
_package.__path__ = [str(ROOT / "workers" / "gateway_runtime")]

_saved_workers = sys.modules.get("workers")
_workers_stub = types.ModuleType("workers")
_workers_stub.Response = type("Response", (), {})
_workers_stub.fetch = lambda *args, **kwargs: None
sys.modules["workers"] = _workers_stub
try:
    http = importlib.import_module("gateway_runtime.http")
finally:
    if _saved_workers is None:
        sys.modules.pop("workers", None)
    else:
        sys.modules["workers"] = _saved_workers


class RequestHeaderTests(unittest.TestCase):
    def test_cloudflare_and_forwarding_headers_are_not_forwarded(self):
        request = types.SimpleNamespace(
            headers={
                "CF-Connecting-IP": "203.0.113.9",
                "cf-ray": "0000000000000000-HKG",
                "cdn-loop": "cloudflare",
                "x-forwarded-for": "198.51.100.2",
                "user-agent": "test-client",
                "content-type": "application/json",
            }
        )

        headers = http.request_headers(request)

        self.assertEqual(headers, {"user-agent": "test-client", "content-type": "application/json"})

    def test_compact_error_detail_extracts_html_title(self):
        html = (
            "<!doctype html><html><head>"
            "<title>Access denied | api.example.com used Cloudflare to restrict access</title>"
            "</head></html>"
        )

        self.assertEqual(
            http.compact_error_detail(html),
            "Access denied | api.example.com used Cloudflare to restrict access",
        )

    def test_compact_error_detail_keeps_plain_text(self):
        self.assertEqual(http.compact_error_detail("error code: 1015"), "error code: 1015")

    def test_credentials_and_hop_by_hop_headers_are_removed(self):
        request = types.SimpleNamespace(
            headers={
                "Authorization": "Bearer downstream",
                "cookie": "session=abc",
                "connection": "keep-alive",
                "accept": "application/json",
            }
        )

        headers = http.request_headers(request)

        self.assertEqual(headers, {"accept": "application/json"})


if __name__ == "__main__":
    unittest.main()
