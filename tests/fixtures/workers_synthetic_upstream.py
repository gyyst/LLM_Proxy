"""用于部署验证的合成上游，不调用真实模型服务。"""

import json
import time

from workers import Response, WorkerEntrypoint


def event(payload):
    return f"data: {json.dumps(payload)}\n\n"


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await handle_upstream(request)


class SyntheticUpstream(WorkerEntrypoint):
    async def fetch(self, request):
        return await handle_upstream(request)


def usage(source="chat"):
    if source == "claude":
        return {"input_tokens": 2, "output_tokens": 5}
    return {"input_tokens": 2, "output_tokens": 5, "total_tokens": 7}


def chat_response(model, stream):
    text = "Hello from synthetic upstream"
    if not stream:
        return Response.json(
            {
                "id": "chat-demo",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 5, "total_tokens": 7},
            }
        )
    first = {
        "id": "chat-demo",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
    }
    last = {
        "id": "chat-demo",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 5, "total_tokens": 7},
    }
    return Response(event(first) + event(last) + "data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"})


def responses_response(model, stream):
    text = "Hello from synthetic upstream"
    output = [
        {"id": "msg-demo", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    ]
    if not stream:
        return Response.json(
            {"id": "resp-demo", "object": "response", "model": model, "output": output, "usage": usage()}
        )
    delta = {
        "type": "response.output_text.delta",
        "delta": text,
        "item_id": "msg-demo",
        "output_index": 0,
        "content_index": 0,
    }
    done = {
        "type": "response.completed",
        "response": {
            "id": "resp-demo",
            "object": "response",
            "model": model,
            "output": output,
            "usage": usage(),
        },
    }
    return Response(event(delta) + event(done), headers={"Content-Type": "text/event-stream"})


def claude_response(model, stream):
    text = "Hello from synthetic upstream"
    if not stream:
        return Response.json(
            {
                "id": "msg-demo",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": usage("claude"),
            }
        )
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg-demo", "model": model, "usage": {"input_tokens": 2, "output_tokens": 0}},
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    return Response("".join(event(item) for item in events), headers={"Content-Type": "text/event-stream"})


async def handle_upstream(request):
    body = await request.json()
    model = body.get("model", "demo")
    stream = bool(body.get("stream"))
    if any(tool.get("type") == "image_generation" for tool in body.get("tools") or []):
        item = {
            "type": "image_generation_call",
            "result": "c3ludGhldGlj",
            "revised_prompt": "synthetic",
            "output_format": "png",
        }
        if stream:
            partial = {
                "type": "response.image_generation_call.partial_image",
                "partial_image_b64": "c3ludGhldGlj",
                "partial_image_index": 0,
                "output_format": "png",
            }
            done = {"type": "response.output_item.done", "output_index": 0, "item": item}
            completed = {
                "type": "response.completed",
                "response": {
                    "id": "resp-image",
                    "object": "response",
                    "created_at": int(time.time()),
                    "model": model,
                    "output": [item],
                    "usage": usage(),
                },
            }
            return Response(
                event(partial) + event(done) + event(completed), headers={"Content-Type": "text/event-stream"}
            )
        return Response.json(
            {
                "id": "resp-image",
                "object": "response",
                "model": model,
                "output": [item],
                "usage": usage(),
            }
        )
    if body.get("messages"):
        return chat_response(model, stream)
    if body.get("input") is not None:
        return responses_response(model, stream)
    return claude_response(model, stream)
