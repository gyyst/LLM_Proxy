"""各下游协议共用的流内错误编码。"""

from __future__ import annotations

from .contracts import DownstreamChunk


def build_stream_error_chunks(
    *,
    downstream_target_format: str,
    message: str,
    error_type: str,
    response_id: str,
    response_model: str,
) -> list[DownstreamChunk]:
    """生成协议对应的流内错误，不依赖 Flask 或 requests。"""
    normalized_target_format = str(downstream_target_format or "").strip().lower()
    if normalized_target_format == "claude_chat":
        return [
            DownstreamChunk(
                kind="json",
                event="error",
                payload={"type": "error", "error": {"type": error_type, "message": message}},
            )
        ]
    if normalized_target_format == "openai_responses":
        return [
            DownstreamChunk(
                kind="json",
                event="response.failed",
                payload={
                    "type": "response.failed",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "status": "failed",
                        "error": {"message": message, "type": error_type, "code": error_type},
                        "model": response_model,
                    },
                },
            )
        ]
    return [
        DownstreamChunk(
            kind="json",
            payload={"error": {"message": message, "type": error_type, "param": None, "code": error_type}},
        ),
        DownstreamChunk(kind="done"),
    ]
