"""OAuth 与 Images 专用数据面适配。"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

from gateway_runtime.http import (
    HttpError,
    iter_response_bytes,
    json_response,
    open_upstream,
    response_headers,
    streaming_response,
)
from src.proxy_core import decode_stream_events


async def _image_body(request: Any, edits: bool) -> dict[str, Any]:
    if int(request.headers.get("Content-Length") or 0) > 25 * 1024 * 1024:
        raise HttpError("Request body is too large", 413, "request_too_large")
    if not edits:
        value = await request.json()
        if not isinstance(value, dict):
            raise HttpError("Request body must be a JSON object", 400, "invalid_json")
        return dict(value)
    form = await request.form_data()
    body = {key: str(value) for key, value in form.items() if not hasattr(value, "filename")}
    files = [value for value in form.values() if hasattr(value, "filename")]
    images = []
    for file in files:
        raw = await file.bytes()
        content_type = file.content_type or "application/octet-stream"
        images.append(f"data:{content_type};base64,{base64.b64encode(raw).decode()}")
    if images:
        body["image"] = images
    return body


async def handle_image(request: Any, env: Any, state: Any, edits: bool) -> Any:
    """执行图片准入、OAuth 工具调用和观测记录。"""
    body = await _image_body(request, edits)
    model = str(body.get("model") or "").strip()
    if not model:
        raise HttpError("Missing required field: model", 400, "missing_model")
    config = await state.resolve_route(model, request, body, "openai_responses")
    snapshot = config.snapshot
    account = next(
        (item for item in snapshot["oauth"] if item.get("id") == config.providers[0].name),
        None,
    )
    if account is None:
        raise HttpError("Images requires an OAuth image model", 400, "unsupported_image_model")
    request_id = uuid.uuid4().hex
    await state.authorize(config, request_id)
    entry = {
        "request_id": request_id,
        "created_at": time.time(),
        "client_ip": config.client_ip,
        "api_key_id": config.key_id,
        "request_model": model,
        "provider": account["id"],
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "status": 502,
        "snapshot": snapshot,
    }
    stream_requested = bool(body.get("stream"))

    async def finish(status: int = 200) -> None:
        entry.update(status=status, outcome="success" if status < 400 else "upstream_error")
        entry["duration_ms"] = int((time.time() - entry["created_at"]) * 1000)
        await state.finish(config, request_id, entry)

    try:
        response = await proxy_oauth_image(
            request, env, state, account, body, on_close=finish if stream_requested else None
        )
        if not stream_requested:
            entry.update(status=response.status, outcome="success" if response.status < 400 else "upstream_error")
            await finish(response.status)
        return response
    except Exception:
        await finish(502)
        raise


async def proxy_oauth_image(
    request: Any,
    env: Any,
    state: Any,
    account: dict[str, Any],
    body: dict[str, Any] | None = None,
    on_close: Any = None,
) -> Any:
    """将 Images 请求转换为 OAuth Responses 图片工具调用。"""
    body = body if body is not None else await request.json()
    credentials = await state.oauth_credentials(account)
    content = [{"type": "input_text", "text": body.get("prompt") or ""}]
    for image in body.get("image") or []:
        if isinstance(image, str):
            content.append({"type": "input_image", "image_url": image})
    tool: dict[str, Any] = {
        "type": "image_generation",
        "model": account["model"],
        "action": "edit" if body.get("image") else "generate",
        "size": body.get("size", "auto"),
        "quality": body.get("quality", "auto"),
        "output_format": body.get("output_format", "png"),
        "background": body.get("background", "auto"),
    }
    if body.get("partial_images") is not None:
        try:
            tool["partial_images"] = int(body["partial_images"])
        except (TypeError, ValueError):
            pass
    payload = {
        "model": account.get("main_model") or account["model"],
        "instructions": body.get("prompt") or "Generate an image",
        "input": [{"role": "user", "content": content}],
        "tools": [tool],
        "stream": bool(body.get("stream")),
    }
    headers = {"Authorization": f"Bearer {credentials['access_token']}", "Content-Type": "application/json"}
    if account.get("account_id"):
        headers["ChatGPT-Account-ID"] = account["account_id"]
    response = await open_upstream(account["api"], headers, payload, env)
    if response.status != 200:
        return response
    if not body.get("stream"):
        data = await response.json()
        source = data.get("response", data)
        results = []
        for item in source.get("output") or []:
            if item.get("type") == "image_generation_call" and item.get("result"):
                results.append({"b64_json": item["result"], "revised_prompt": item.get("revised_prompt")})
        return json_response({"created": int(source.get("created_at") or time.time()), "data": results})
    response_format = "url" if str(body.get("response_format") or "").lower() == "url" else "b64_json"

    def generate():
        image_items: list[dict[str, Any]] = []
        completed = False
        try:
            for event in decode_stream_events(iter_response_bytes(response), "sse_json"):
                if event.kind != "json" or not isinstance(event.payload, dict):
                    continue
                payload = event.payload
                event_type = str(payload.get("type") or event.event or "").strip()
                if event_type == "response.image_generation_call.partial_image":
                    partial = str(payload.get("partial_image_b64") or "")
                    if partial:
                        yield _image_frame(
                            "image_generation.partial_image",
                            {
                                "type": "image_generation.partial_image",
                                "partial_image_index": int(payload.get("partial_image_index") or 0),
                                **(
                                    {"url": _data_url(partial, payload.get("output_format"))}
                                    if response_format == "url"
                                    else {"b64_json": partial}
                                ),
                            },
                        )
                elif event_type == "response.output_item.done":
                    item = payload.get("item")
                    if isinstance(item, dict) and item.get("type") == "image_generation_call":
                        image_items.append(item)
                elif event_type in {"response.completed", "response.done"}:
                    completed = True
                    source = payload.get("response") if isinstance(payload.get("response"), dict) else payload
                    items = [item for item in source.get("output") or [] if isinstance(item, dict)]
                    if not items:
                        items = image_items
                    usage = source.get("tool_usage") or source.get("usage")
                    for item in items:
                        if item.get("type") != "image_generation_call" or not item.get("result"):
                            continue
                        result = str(item["result"])
                        data = {"type": "image_generation.completed"}
                        if usage:
                            data["usage"] = usage
                        data.update(
                            {"url": _data_url(result, item.get("output_format"))}
                            if response_format == "url"
                            else {"b64_json": result}
                        )
                        yield _image_frame("image_generation.completed", data)
                elif event_type in {"response.failed", "response.cancelled", "error"}:
                    yield _image_frame(
                        "image_generation.error",
                        {
                            "type": "image_generation.error",
                            "error": {"message": "Image generation failed", "type": "upstream_error"},
                        },
                    )
                    completed = True
            if not completed:
                yield _image_frame(
                    "image_generation.error",
                    {
                        "type": "image_generation.error",
                        "error": {"message": "Image stream ended unexpectedly", "type": "upstream_stream_error"},
                    },
                )
        finally:
            from gateway_runtime.http import close_response

            close_response(response)

    return streaming_response(
        generate(),
        headers=response_headers(response.headers, stream=True),
        on_close=lambda: on_close(200) if on_close else None,
    )


def _image_frame(event_name: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _data_url(data: str, output_format: Any) -> str:
    normalized = str(output_format or "").lower()
    mime = "image/jpeg" if normalized in {"jpg", "jpeg"} else "image/webp" if normalized == "webp" else "image/png"
    return f"data:{mime};base64,{data}"
