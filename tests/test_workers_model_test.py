"""Workers 模型测试指标计算。"""

from __future__ import annotations

import unittest

from workers.gateway_runtime.model_test import build_model_test_request, model_test_result


class WorkersModelTestMetricsTests(unittest.TestCase):
    def test_builds_native_nonstream_requests_with_enough_output_room(self):
        chat = build_model_test_request("openai_chat", "chat-model")
        responses = build_model_test_request("openai_responses", "response-model")
        claude = build_model_test_request("claude_chat", "claude-model")

        self.assertEqual(chat["max_tokens"], 128)
        self.assertFalse(chat["stream"])
        self.assertEqual(responses["max_output_tokens"], 128)
        self.assertFalse(responses["stream"])
        self.assertEqual(claude["max_tokens"], 128)
        self.assertFalse(claude["stream"])

    def test_chat_usage_produces_output_tps(self):
        result = model_test_result(
            model="demo",
            source_format="openai_chat",
            payload={
                "model": "demo-upstream",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 40, "total_tokens": 50},
            },
            duration_ms=2000,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["tps"], 20)
        self.assertEqual(result["response_model"], "demo-upstream")

    def test_responses_nested_usage_and_model(self):
        result = model_test_result(
            model="demo",
            source_format="openai_responses",
            payload={
                "type": "response.completed",
                "response": {
                    "model": "demo-upstream",
                    "usage": {"input_tokens": 10, "output_tokens": 12, "total_tokens": 22},
                },
            },
            duration_ms=3000,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["tps"], 4)
        self.assertEqual(result["response_model"], "demo-upstream")

    def test_claude_usage_produces_tps(self):
        result = model_test_result(
            model="demo",
            source_format="claude_chat",
            payload={
                "model": "claude-upstream",
                "usage": {"input_tokens": 8, "output_tokens": 30},
            },
            duration_ms=1500,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["tps"], 20)
        self.assertEqual(result["response_model"], "claude-upstream")

    def test_missing_usage_keeps_tps_empty(self):
        result = model_test_result(
            model="demo",
            source_format="openai_chat",
            payload={"model": "demo-upstream", "choices": []},
            duration_ms=1000,
        )

        self.assertTrue(result["available"])
        self.assertIsNone(result["tps"])

    def test_error_payload_is_reported(self):
        result = model_test_result(
            model="demo",
            source_format="openai_chat",
            payload={"error": {"message": "model unavailable"}},
            duration_ms=1000,
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "model unavailable")


if __name__ == "__main__":
    unittest.main()
