"""Workers 数据面模型路由的单元测试，不依赖 Cloudflare 运行环境。"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from dataclasses import dataclass, field
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
    proxy = importlib.import_module("gateway_runtime.proxy")
finally:
    if _saved_workers is None:
        sys.modules.pop("workers", None)
    else:
        sys.modules["workers"] = _saved_workers

from src.external import LLMProvider  # noqa: E402


@dataclass
class _Config:
    request_model: str
    targets_by_provider: dict[str, str] = field(default_factory=dict)


class UpstreamModelTests(unittest.TestCase):
    def test_mapping_route_uses_selected_target_model(self):
        config = _Config("home", {"huawei": "huawei/hy4-flash"})

        self.assertEqual(proxy.resolve_upstream_model(config, "huawei"), "hy4-flash")

    def test_direct_route_uses_request_model_without_provider_prefix(self):
        config = _Config("huawei/glm-5.3-flash", {"huawei": "huawei/glm-5.3-flash"})

        self.assertEqual(proxy.resolve_upstream_model(config, "huawei"), "glm-5.3-flash")

    def test_oauth_route_keeps_bare_model_name(self):
        config = _Config("gpt-5-codex", {"oauth-account": "gpt-5-codex"})

        self.assertEqual(proxy.resolve_upstream_model(config, "oauth-account"), "gpt-5-codex")

    def test_missing_provider_route_falls_back_to_request_model(self):
        config = _Config("huawei/glm-5.3-flash")

        self.assertEqual(proxy.resolve_upstream_model(config, "huawei"), "glm-5.3-flash")


class ErrorMessageTests(unittest.TestCase):
    def test_cloudflare_error_page_is_reduced_to_title(self):
        html = (
            "<!doctype html><html><head>"
            "<title>Access denied | api.example.com used Cloudflare to restrict access</title>"
            "</head><body>blocked</body></html>"
        )

        self.assertEqual(
            proxy._error_message(html),
            "Upstream error page: Access denied | api.example.com used Cloudflare to restrict access",
        )

    def test_plain_error_text_is_preserved(self):
        self.assertEqual(proxy._error_message("error code: 1015"), "error code: 1015")


class RequestHeaderTests(unittest.TestCase):
    def test_downstream_content_type_is_replaced_case_insensitively(self):
        workers_proxy = proxy.WorkersProxy(None, None)
        provider = LLMProvider(name="demo", api="https://example.com/v1/chat/completions", api_key="test-key")
        original = proxy.request_headers
        proxy.request_headers = lambda request: {"content-type": "text/plain", "x-test": "1"}
        try:
            headers = workers_proxy._headers(None, provider, None)
        finally:
            proxy.request_headers = original

        self.assertEqual([name for name in headers if name.lower() == "content-type"], ["Content-Type"])
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(headers["User-Agent"], "LLM-Proxy/3.0")

    def test_client_user_agent_is_preserved(self):
        workers_proxy = proxy.WorkersProxy(None, None)
        provider = LLMProvider(name="demo", api="https://example.com/v1/chat/completions", api_key="test-key")
        original = proxy.request_headers
        proxy.request_headers = lambda request: {"user-agent": "client/1.0"}
        try:
            headers = workers_proxy._headers(None, provider, None)
        finally:
            proxy.request_headers = original

        self.assertEqual(headers["user-agent"], "client/1.0")
        self.assertEqual([name for name in headers if name.lower() == "user-agent"], ["user-agent"])


if __name__ == "__main__":
    unittest.main()
