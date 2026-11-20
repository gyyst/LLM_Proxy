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

_http_stub = types.ModuleType("gateway_runtime.http")
_http_stub.HttpError = type("HttpError", (Exception,), {})
_http_stub.close_response = lambda response: None
_http_stub.iter_response_bytes = lambda response: iter(())
_http_stub.json_response = lambda payload, status=200: (payload, status)
_http_stub.open_upstream = lambda *args, **kwargs: None
_http_stub.request_headers = lambda request: {}
_http_stub.response_headers = lambda response: {}
_http_stub.streaming_response = lambda *args, **kwargs: None

_package = sys.modules.setdefault("gateway_runtime", types.ModuleType("gateway_runtime"))
_package.__path__ = [str(ROOT / "workers" / "gateway_runtime")]
sys.modules["gateway_runtime.http"] = _http_stub

proxy = importlib.import_module("gateway_runtime.proxy")

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


if __name__ == "__main__":
    unittest.main()
