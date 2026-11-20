"""Provider 模型测试的请求构造与指标计算。"""

from __future__ import annotations

import json
from typing import Any

from src.proxy_core.usage import create_usage_meta, merge_usage_meta
from src.utils.request_metrics import output_tps

_MODEL_TEST_PROMPT = "\n\n".join(
    ["Copy the following block exactly once and output nothing else:"]
    + [f"{index:02d}: the quick brown fox jumps over the lazy dog" for index in range(1, 10)]
)


def build_model_test_request(source_format: str, model: str) -> dict[str, Any]:
    """按上游协议构造有足够输出量的非流式测试请求。"""
    if source_format == "openai_responses":
        return {
            "model": model,
            "input": _MODEL_TEST_PROMPT,
            "max_output_tokens": 128,
            "stream": False,
        }
    if source_format == "claude_chat":
        return {
            "model": model,
            "messages": [{"role": "user", "content": _MODEL_TEST_PROMPT}],
            "max_tokens": 128,
            "stream": False,
        }
    return {
        "model": model,
        "messages": [{"role": "user", "content": _MODEL_TEST_PROMPT}],
        "max_tokens": 128,
        "stream": False,
    }


def model_test_result(
    *,
    model: str,
    source_format: str,
    payload: Any,
    duration_ms: float,
) -> dict[str, Any]:
    """从成功响应计算模型可用性、响应耗时和输出 TPS。"""
    if not isinstance(payload, dict):
        return model_test_failure(model, "Upstream returned a non-object JSON response")
    error = _extract_error(payload)
    if error:
        return model_test_failure(model, error)

    meta = create_usage_meta()
    merge_usage_meta(meta, payload, source_format)
    response_model = _response_model(payload)
    completion_tokens = int(meta.get("completion_tokens") or 0)
    tps, _reason = output_tps(
        completion_tokens,
        duration_ms,
        usage_status=str(meta.get("usage_status") or "unknown"),
        outcome="success",
    )
    return {
        "requested_model": model,
        "available": True,
        "first_token_latency_ms": round(duration_ms, 2),
        "tps": tps,
        "response_model": response_model,
        "error": None,
    }


def model_test_failure(model: str, error: str, *, response_model: str | None = None) -> dict[str, Any]:
    """构造统一的失败结果。"""
    return {
        "requested_model": model,
        "available": False,
        "first_token_latency_ms": None,
        "tps": None,
        "response_model": response_model,
        "error": error,
    }


def _response_model(payload: dict[str, Any]) -> str | None:
    """兼容顶层、Responses 与 Claude 包装结构中的模型名。"""
    for candidate in (
        payload.get("model"),
        (payload.get("response") or {}).get("model") if isinstance(payload.get("response"), dict) else None,
        (payload.get("message") or {}).get("model") if isinstance(payload.get("message"), dict) else None,
    ):
        model = str(candidate or "").strip()
        if model:
            return model
    return None


def _extract_error(payload: dict[str, Any]) -> str | None:
    error_value = payload.get("error")
    if isinstance(error_value, str) and error_value.strip():
        return error_value.strip()
    if isinstance(error_value, dict):
        message = error_value.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return json.dumps(error_value, ensure_ascii=False)
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None
