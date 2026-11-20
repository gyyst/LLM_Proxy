"""复用现有转换器与请求构建逻辑的 Workers 数据面。"""

from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway_runtime.http import (
    DEFAULT_USER_AGENT,
    HttpError,
    close_response,
    compact_error_detail,
    iter_response_bytes,
    json_response,
    open_upstream,
    request_headers,
    response_headers,
    streaming_response,
)
from src.executors.contracts import OpenedUpstreamResponse
from src.external import LLMProvider
from src.hooks import HookAbortError
from src.proxy_core import (
    DownstreamChunk,
    StreamEvent,
    decode_stream_events,
    encode_downstream_chunk,
    is_terminal_chunk,
    resolve_stream_format,
    should_emit_terminal_chunk,
)
from src.proxy_core.errors import build_stream_error_chunks
from src.proxy_core.usage import create_usage_meta, merge_usage_meta, public_usage_meta
from src.services.outbound_privacy import OutboundPrivacyService
from src.services.upstream_request_builder import build_upstream_request
from src.translators import build_default_translator_registry
from src.translators.stream_aggregator import StreamAggregationError, aggregate_stream_to_native_response
from src.utils.http_headers import merge_http_headers


@dataclass
class GatewayResult:
    """向下游返回 Workers Response 与观测结果。"""

    response: Any
    entry: dict[str, Any]


class WorkersProxy:
    """保持首字节提交边界的异步 HTTP + 同步协议核适配。"""

    def __init__(self, env: Any, state: Any):
        self.env = env
        self.state = state
        self.translators = build_default_translator_registry()
        self.privacy = OutboundPrivacyService()
        self.root = Path(__file__).resolve().parents[1]
        self.started = time.time()
        self.logger = logging.getLogger("workers")

    async def handle(
        self, request: Any, *, target_format: str, route: str, body: dict[str, Any] | None = None
    ) -> GatewayResult:
        started = time.time()
        body = body if body is not None else await _json_body(request)
        requested_model = str(body.get("model") or "").strip()
        if not requested_model:
            raise HttpError("Missing required field: model", 400, "missing_model")
        if route == "compact":
            body["stream"] = False
        config = await self.state.resolve_route(requested_model, request, body, target_format)
        entry_snapshot = config.snapshot
        config.request_id = uuid.uuid4().hex
        await self.state.authorize(config, config.request_id)
        request_id = config.request_id
        entry = {
            "request_id": request_id,
            "created_at": started,
            "client_ip": config.client_ip,
            "api_key_id": config.key_id,
            "request_model": requested_model,
            "provider": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "status": 502,
            "snapshot": entry_snapshot,
        }
        try:
            response, final_entry = await self._proxy(request, body, config, target_format, route, entry)
            if final_entry.pop("_streaming", False):
                return GatewayResult(response, final_entry)
            final_entry["duration_ms"] = int((time.time() - started) * 1000)
            await self.state.record_llm(self.env, final_entry, body, final_entry.get("downstream_output", body))
            await self.state.finish(config, request_id, final_entry)
            return GatewayResult(response, final_entry)
        except HttpError as exc:
            entry.update(status=exc.status, outcome="rejected", error=exc.message)
            entry["duration_ms"] = int((time.time() - started) * 1000)
            await self.state.finish(config, request_id, entry)
            return GatewayResult(_error(target_format, exc.message, exc.status, exc.code), entry)
        except Exception as exc:
            entry.update(outcome="error", error=str(exc)[:1000])
            entry["duration_ms"] = int((time.time() - started) * 1000)
            await self.state.finish(config, request_id, entry)
            self.logger.exception("Workers proxy failed: %s", exc)
            return GatewayResult(_error(target_format, "Upstream request failed", 502), entry)

    async def _proxy(
        self, request: Any, body: dict[str, Any], config: Any, target: str, route: str, entry: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        original = copy.deepcopy(body)
        last_status = 502
        last_error = "No upstream provider available"
        for target_attempt, provider in enumerate(config.providers):
            translator = self.translators.get(provider.source_format, target)
            upstream_model = resolve_upstream_model(config, provider.name)
            upstream_body = copy.deepcopy(original)
            upstream_body["model"] = upstream_model
            privacy_context = None
            if provider.safe_desensitization_enabled or config.force_privacy:
                result = self.privacy.sanitize_request_body(upstream_body)
                upstream_body = result.body
                privacy_context = result.context
            entry["provider"] = provider.name
            for attempt in range(provider.max_retries):
                auth = await self.state.acquire_auth(provider) if provider.auth_group else None
                if provider.auth_group and auth is None:
                    last_error = "Authentication group has no available entry"
                    break
                built = build_upstream_request(
                    root_path=self.root,
                    logger=self.logger,
                    provider=provider,
                    request_model=config.request_model,
                    upstream_model=upstream_model,
                    provider_target_format=target,
                    request_data=upstream_body,
                    request_headers=self._headers(request, provider, auth),
                    translator=translator,
                    attempt=attempt,
                    previous_status_code=None,
                    previous_error_type=None,
                    auth_group_name=provider.auth_group,
                    auth_entry_id=auth.get("id") if auth else None,
                    force_upstream_stream=provider.force_upstream_stream and not original.get("stream", False),
                )
                if route == "compact":
                    built.translated_body["stream"] = False
                response = None
                try:
                    response = await open_upstream(provider.api, built.headers, built.translated_body, self.env)
                    status = int(response.status)
                    last_status = status
                    if status in {408, 409, 425, 429, 500, 502, 503, 504} and attempt < provider.max_retries - 1:
                        await self.state.finish_auth(provider, auth, status, response.headers.get("Retry-After"))
                        close_response(response)
                        continue
                    if status >= 400 or 300 <= status < 400:
                        await self.state.finish_auth(provider, auth, status, response.headers.get("Retry-After"))
                        await self.state.fail_target(config, provider, status)
                        error = await response.text()
                        entry.update(
                            status=status if status < 500 else 502, outcome="upstream_error", error=error[:1000]
                        )
                        last_error = _error_message(error)
                        last_status = status
                        if target_attempt < len(config.providers) - 1:
                            break
                        return _error(target, last_error, entry["status"]), entry
                    stream = bool(built.translated_body.get("stream", False))
                    opened = _opened(response, stream)
                    if stream and original.get("stream", False):
                        final = self._stream_result(
                            response,
                            opened,
                            provider,
                            translator,
                            built,
                            target,
                            original,
                            entry,
                            privacy_context,
                            config=config,
                            request_id=config.request_id,
                            started=self.started,
                            auth_finish=lambda finish_status=200: self.state.finish_auth(provider, auth, finish_status),
                        )
                        if final is not None:
                            return final
                        await self.state.finish_auth(provider, auth, 502)
                        close_response(response)
                        continue
                    result = await self._nonstream_result(
                        response, opened, provider, translator, built, target, original, entry, privacy_context
                    )
                    await self.state.finish_auth(provider, auth, status)
                    return result
                except (HttpError, HookAbortError):
                    close_response(response) if response is not None else None
                    raise
                except Exception as exc:
                    close_response(response) if response is not None else None
                    await self.state.finish_auth(provider, auth, 0)
                    last_error = str(exc)
                    self.logger.warning(
                        "Upstream attempt failed: provider=%s attempt=%s error=%s", provider.name, attempt + 1, exc
                    )
            await self.state.fail_target(config, provider, last_status)
        entry.update(
            outcome="upstream_error", error=last_error[:1000], status=last_status if last_status < 500 else 502
        )
        return _error(target, last_error, entry["status"]), entry

    def _headers(self, request: Any, provider: LLMProvider, auth: dict[str, Any] | None) -> dict[str, str]:
        headers = request_headers(request)
        if provider.safe_desensitization_enabled:
            headers = self.privacy.sanitize_request_headers(headers)
        headers = merge_http_headers(headers, {"Content-Type": "application/json"})
        if not any(name.lower() == "user-agent" for name in headers):
            headers = merge_http_headers(headers, {"User-Agent": DEFAULT_USER_AGENT})
        if provider.extra_headers:
            headers = merge_http_headers(headers, dict(provider.extra_headers))
        if auth:
            return merge_http_headers(headers, auth.get("headers") or {})
        if provider.api_key:
            headers = merge_http_headers(headers, {"Authorization": f"Bearer {provider.api_key}"})
        return headers

    async def _nonstream_result(
        self,
        response: Any,
        opened: OpenedUpstreamResponse,
        provider: LLMProvider,
        translator: Any,
        built: Any,
        target: str,
        original: dict[str, Any],
        entry: dict[str, Any],
        privacy: Any,
    ) -> tuple[Any, dict[str, Any]]:
        meta = create_usage_meta()
        try:
            if opened.is_stream:

                def events():
                    for event in decode_stream_events(iter_response_bytes(response), opened.stream_format):
                        _usage(meta, event.payload, translator.source_format)
                        yield event

                payload = aggregate_stream_to_native_response(
                    source_format=translator.source_format, model_name=built.request_ctx.upstream_model, events=events()
                )
            else:
                payload = json.loads(await response.text())
                _usage(meta, payload, translator.source_format)
            translated = translator.translate_nonstream_response(
                built.request_ctx.upstream_model, original, built.translated_body, payload
            )
            guarded = provider.apply_response_guard(built.request_ctx, translated)
            translated = translated if guarded is None else guarded
            if privacy:
                translated = privacy.restore_payload(translated)
            _usage(meta, translated, target)
            entry.update(status=200, outcome="success", response_model=meta.get("response_model"))
            entry.update({key: meta.get(key, 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")})
            entry["downstream_output"] = translated
            return json_response(translated), entry
        except (StreamAggregationError, json.JSONDecodeError) as exc:
            raise StreamAggregationError.from_message(
                f"Invalid upstream response: {exc}", error_type="upstream_stream_processing_error"
            ) from exc
        finally:
            close_response(response)

    def _stream_result(
        self,
        response: Any,
        opened: OpenedUpstreamResponse,
        provider: LLMProvider,
        translator: Any,
        built: Any,
        target: str,
        original: dict[str, Any],
        entry: dict[str, Any],
        privacy: Any,
        config: Any,
        request_id: str,
        started: float,
        auth_finish: Any = None,
    ) -> tuple[Any, dict[str, Any]] | None:
        meta = create_usage_meta()
        output_parts: list[bytes] = []
        state: dict[str, Any] = {}
        terminal = False
        usage = public_usage_meta(meta)

        def generate():
            nonlocal terminal, usage
            started = False
            restorer = _Restorer(privacy)
            try:
                for event in decode_stream_events(iter_response_bytes(response), opened.stream_format):
                    if event.kind == "json" and isinstance(event.payload, dict):
                        _usage(meta, event.payload, translator.source_format)
                    chunks = translator.translate_stream_event(
                        built.request_ctx.upstream_model, original, built.translated_body, event, state
                    )
                    for chunk in _capture_chunks(
                        _encode_chunks(chunks, provider, built.request_ctx, target, restorer), output_parts
                    ):
                        started = True
                        yield chunk
                    terminal = terminal or any(is_terminal_chunk(chunk, target) for chunk in chunks)
                if not terminal:
                    chunks = translator.translate_stream_event(
                        built.request_ctx.upstream_model,
                        original,
                        built.translated_body,
                        StreamEvent(kind="done", payload="[DONE]"),
                        state,
                    )
                    for chunk in _capture_chunks(
                        _encode_chunks(chunks, provider, built.request_ctx, target, restorer), output_parts
                    ):
                        started = True
                        yield chunk
                    terminal = terminal or any(is_terminal_chunk(chunk, target) for chunk in chunks)
                if should_emit_terminal_chunk(target) and not terminal:
                    started = True
                    yield encode_downstream_chunk(DownstreamChunk(kind="done"), target)
                    terminal = True
                entry.update(status=200, outcome="success")
            except GeneratorExit:
                entry.update(outcome="client_cancelled", status=499)
                if auth_finish is not None:
                    from pyodide.ffi import run_sync

                    run_sync(auth_finish(200))
                raise
            except Exception as exc:
                if not started:
                    raise
                from pyodide.ffi import run_sync

                if terminal:
                    if auth_finish is not None:
                        run_sync(auth_finish(200))
                    return
                entry.update(outcome="upstream_error", error=str(exc)[:1000])
                if auth_finish is not None:
                    run_sync(auth_finish(502))
                for chunk in build_stream_error_chunks(
                    downstream_target_format=target,
                    message="Upstream stream interrupted",
                    error_type="upstream_stream_error",
                    response_id=f"stream_{provider.name}",
                    response_model=built.request_ctx.upstream_model,
                ):
                    yield encode_downstream_chunk(chunk, target)
                from pyodide.ffi import run_sync

                run_sync(self.state.fail_target(config, provider, 502))
            finally:
                close_response(response)
                usage = public_usage_meta(meta)
                entry.update({key: usage.get(key, 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")})
                if usage.get("response_model"):
                    entry["response_model"] = usage["response_model"]
                entry["downstream_output"] = b"".join(output_parts).decode("utf-8", errors="replace")

        async def finish() -> None:
            entry["duration_ms"] = int((time.time() - started) * 1000)
            if auth_finish is not None and entry.get("outcome") == "success":
                await auth_finish()
            await self.state.record_llm(self.env, entry, original, entry.get("downstream_output"))
            await self.state.finish(config, request_id, entry)

        result = streaming_response(
            generate(), headers=response_headers(response.headers, stream=True), on_close=finish
        )
        entry["_streaming"] = True
        return result, entry


def _is_streaming_response(response: Any) -> bool:
    """保留工具函数以兼容已有诊断，不再用于主结算分支。"""
    return False


def _opened(response: Any, stream: bool) -> OpenedUpstreamResponse:
    content_type = response.headers.get("Content-Type") or ""
    return OpenedUpstreamResponse(
        response=response,
        status_code=int(response.status),
        content_type=content_type.lower(),
        is_stream=stream,
        stream_format=resolve_stream_format(None, content_type, "http") if stream else "nonstream",
    )


def _encode_chunks(chunks: list[DownstreamChunk], provider: LLMProvider, ctx: Any, target: str, restorer: Any):
    for chunk in chunks:
        if chunk.kind != "done":
            guarded = provider.apply_response_guard(ctx, chunk.payload)
            if guarded is not None:
                chunk = DownstreamChunk(
                    kind="json" if isinstance(guarded, (dict, list)) else "text", payload=guarded, event=chunk.event
                )
        for restored in restorer.restore(chunk):
            yield encode_downstream_chunk(restored, target)


def _capture_chunks(chunks: Any, output_parts: list[bytes]):
    for chunk in chunks:
        output_parts.append(chunk)
        yield chunk


class _Restorer:
    def __init__(self, context: Any):
        self.context = context

    def restore(self, chunk: DownstreamChunk) -> list[DownstreamChunk]:
        if not self.context or not self.context.enabled or chunk.kind == "done":
            return [chunk]
        return [
            DownstreamChunk(kind=chunk.kind, payload=self.context.restore_payload(chunk.payload), event=chunk.event)
        ]


def _usage(meta: dict[str, Any], payload: Any, source: str) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("model"):
        meta["response_model"] = payload["model"]
    merge_usage_meta(meta, payload, source)


async def _json_body(request: Any) -> dict[str, Any]:
    if int(request.headers.get("Content-Length") or 0) > 16 * 1024 * 1024:
        raise HttpError("Request body is too large", 413, "request_too_large")
    try:
        value = await request.json()
    except Exception as exc:
        raise HttpError("Request body must be valid JSON", 400, "invalid_json") from exc
    if not isinstance(value, dict):
        raise HttpError("Request body must be a JSON object", 400, "invalid_json")
    return dict(value)


def resolve_upstream_model(config: Any, provider_name: str) -> str:
    """解析 Provider 实际使用的上游模型名；映射路由使用命中的目标模型。"""
    routed_model = config.targets_by_provider.get(provider_name) or config.request_model
    return requested_model_without_prefix(routed_model, provider_name)


def requested_model_without_prefix(model: str, provider: str) -> str:
    prefix = f"{provider}/"
    return model[len(prefix) :] if model.startswith(prefix) else model


def _error(target: str, message: str, status: int = 502, code: str = "upstream_error") -> Any:
    if target == "claude_chat":
        return json_response({"type": "error", "error": {"type": code, "message": message}}, status)
    return json_response({"error": {"message": message, "type": code, "code": code, "param": None}}, status)


def _error_message(raw: str) -> str:
    text = raw.strip()
    if "<html" in text.lower():
        return f"Upstream error page: {compact_error_detail(text)}"
    try:
        body = json.loads(text)
        error = body.get("error", body) if isinstance(body, dict) else body
        return str(error.get("message") if isinstance(error, dict) else error)[:1000]
    except (ValueError, AttributeError):
        return text[:1000] or "Upstream request failed"
