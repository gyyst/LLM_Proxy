"""Workers 统一请求路由与响应观测。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gateway_runtime.http import HttpError, json_response
from gateway_runtime.management import Management
from gateway_runtime.proxy import WorkersProxy
from gateway_runtime.services import GatewayServices


class WorkersApplication:
    """明确分离同源控制面与公开数据面。"""

    def __init__(self, env: Any, root: Path, ctx: Any):
        self.env = env
        self.management = Management(env, root, ctx)
        self.services = GatewayServices(env, root, ctx)
        self.root = root

    async def handle(self, request: Any) -> Any:
        path = urlsplit(request.url).path
        method = str(request.method).upper()
        try:
            if method == "OPTIONS" and path.startswith("/v1/"):
                return json_response({}, 204, _cors())
            if path.startswith("/v1/"):
                return await self.data_plane(request, path)
            if path.startswith("/api/") or path in {
                "/",
                "/login",
                "/providers",
                "/users",
                "/api-keys",
                "/model-mappings",
                "/oauth",
                "/statistics",
                "/settings",
            }:
                return await self.management.handle(request, self.env)
            assets = getattr(self.env, "ASSETS", None)
            if assets is not None:
                return await assets.fetch(request)
            return json_response({"error": "Not found"}, 404)
        except HttpError as exc:
            return json_response(
                {"error": {"message": exc.message, "type": exc.code, "code": exc.code}}, exc.status, _cors()
            )
        except Exception as exc:
            print(f"Worker request failed: {type(exc).__name__}: {exc}")
            return json_response({"error": "Internal server error"}, 500)

    async def data_plane(self, request: Any, path: str) -> Any:
        if path == "/v1/models":
            return await self.management.handle(request, self.env)
        if str(request.method).upper() != "POST":
            raise HttpError("Method not allowed", 405, "method_not_allowed")
        routes = {
            "/v1/chat/completions": ("openai_chat", "chat"),
            "/v1/responses": ("openai_responses", "responses"),
            "/v1/responses/compact": ("openai_responses", "compact"),
            "/v1/messages": ("claude_chat", "messages"),
            "/v1/images/generations": ("openai_images", "images"),
            "/v1/images/edits": ("openai_images", "images"),
        }
        if path not in routes:
            raise HttpError("Not found", 404, "not_found")
        target, route = routes[path]
        if route == "images":
            from gateway_runtime.images import handle_image

            return await handle_image(request, self.env, self.services, path.endswith("/edits"))
        if route == "compact":
            snapshot = await self.services.snapshot()
            request_body = await request.json()
            if not any(account.get("model") == request_body.get("model") for account in snapshot["oauth"]):
                raise HttpError("Compaction requires an OAuth model", 400, "invalid_compact_request")
        result = await WorkersProxy(self.env, self.services).handle(
            request, target_format=target, route=route, body=locals().get("request_body")
        )
        return result.response


def _cors() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Authorization,Content-Type,X-API-Key,Accept",
        "Access-Control-Max-Age": "86400",
    }
