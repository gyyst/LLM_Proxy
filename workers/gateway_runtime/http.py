"""Python 数据模型的 Workers HTTP 适配。

该模块集中处理 JS FFI 边界，业务服务只接收 Python 数据与字节流。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import js
from pyodide.ffi import create_proxy, run_sync, to_js

from workers import Response, fetch

DEFAULT_USER_AGENT = "LLM-Proxy/3.0"


class HttpError(Exception):
    """带 HTTP 状态的业务错误。"""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def json_response(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Any:
    """创建无缓存 JSON 响应。"""
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        status=status,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            **(headers or {}),
        },
    )


async def read_json(request: Any, *, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    """读取有界 JSON 请求体，禁止把不受控大对象缓存在 isolate 中。"""
    declared = int(request.headers.get("Content-Length") or 0)
    if declared > max_bytes:
        raise HttpError("Request body is too large", 413, "request_too_large")
    body = await request.arrayBuffer()
    raw = bytes(body.to_bytes())
    if len(raw) > max_bytes:
        raise HttpError("Request body is too large", 413, "request_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpError("Request body must be valid JSON", 400, "invalid_json") from exc
    if not isinstance(payload, dict):
        raise HttpError("Request body must be a JSON object", 400, "invalid_json")
    return payload


def request_headers(request: Any) -> dict[str, str]:
    """复制 headers，移除下游认证、Cloudflare 内部字段与 hop-by-hop 字段。"""
    excluded = {
        "authorization",
        "x-api-key",
        "cookie",
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "cdn-loop",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-proto",
        "x-real-ip",
        "forwarded",
        "true-client-ip",
    }
    return {
        str(key): str(value)
        for key, value in request.headers.items()
        if str(key).lower() not in excluded and not str(key).lower().startswith("cf-")
    }


def compact_error_detail(raw: str, limit: int = 300) -> str:
    """把上游错误正文压缩为适合展示的短文本。"""
    text = raw.strip()
    if "<html" in text.lower():
        title = _html_title(text)
        return (title or "Upstream returned an HTML error page")[:limit]
    return text[:limit]


def _html_title(html: str) -> str:
    lowered = html.lower()
    start = lowered.find("<title>")
    end = lowered.find("</title>", start + 7) if start >= 0 else -1
    if end < 0:
        return ""
    return " ".join(html[start + 7 : end].split())


def response_headers(headers: Any, *, stream: bool = False) -> dict[str, str]:
    """仅保留端到端响应头，并明确下游流媒体类型。"""
    excluded = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length", "content-type"}
    result = {str(key): str(value) for key, value in headers.items() if str(key).lower() not in excluded}
    if stream:
        result["Content-Type"] = "text/event-stream; charset=utf-8"
        result["Cache-Control"] = "no-cache"
    elif headers.get("Content-Type"):
        result["Content-Type"] = str(headers.get("Content-Type"))
    return result


async def open_upstream(
    url: str, headers: dict[str, str], payload: dict[str, Any] | None, env: Any = None, method: str = "POST"
) -> Any:
    """通过 Workers fetch 发出上游请求。"""
    del env
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    return await fetch(url, method=method, headers=headers, body=body)


async def get_upstream(url: str, headers: dict[str, str], env: Any = None) -> Any:
    """GET 上游。"""
    del env
    return await fetch(url, method="GET", headers=headers)


def iter_response_bytes(response: Any) -> Iterator[bytes]:
    """在同步转换器与异步 JS ReadableStream 之间桥接。"""
    body = getattr(response, "js_response", response).body
    if body is None:
        raw = run_sync(response.arrayBuffer())
        yield bytes(raw.to_bytes())
        return
    reader = body.getReader()
    try:
        while True:
            result = run_sync(reader.read())
            if result.done:
                break
            if result.value is not None:
                yield bytes(result.value.to_bytes())
    finally:
        if not getattr(result, "done", True):
            run_sync(reader.cancel())
        else:
            reader.releaseLock()


def close_response(response: Any) -> None:
    """主动取消未读完的上游响应。"""
    js_response = getattr(response, "js_response", response)
    if js_response.body is not None and not js_response.bodyUsed:
        run_sync(js_response.body.cancel())


def streaming_response(
    chunks: Iterator[bytes],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    on_close: Any = None,
) -> Any:
    """以按需拉取方式创建流，并预取首个非空下游字节。"""
    iterator = iter(chunks)
    first = None
    for chunk in iterator:
        if chunk:
            first = chunk
            break

    def body() -> Iterator[bytes]:
        if first is not None:
            yield first
        yield from iterator

    stream = body()
    closed = False

    def cleanup() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        close = getattr(stream, "close", None)
        if close:
            close()
        if on_close is not None:
            result = on_close()
            if hasattr(result, "__await__"):
                run_sync(result)

    @create_proxy
    async def pull(controller: Any) -> None:
        try:
            chunk = next(stream, None)
            if chunk is None:
                controller.close()
                cleanup()
            elif chunk:
                controller.enqueue(to_js(chunk))
        except Exception as exc:
            cleanup()
            controller.error(str(exc))

    @create_proxy
    async def cancel(_reason: Any = None) -> None:
        cleanup()

    return Response(js.ReadableStream.new(pull=pull, cancel=cancel), status=status, headers=headers)


async def collect_stream(chunks: AsyncIterator[bytes]) -> bytes:
    """只用于有界控制面响应。"""
    return b"".join([part async for part in chunks])
