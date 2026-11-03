"""Testes unitários do Relay Agêntico (/api/ai/agent-relay) — Fase 0."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from routers.agent_relay import (
    MAX_PAYLOAD_BYTES,
    AgentMessage,
    AgentRelayRequest,
    AgentRole,
    ToolDeclaration,
    ToolFunctionDef,
)


class TestAgentRelay(unittest.IsolatedAsyncioTestCase):
    def test_schema_serialization(self):
        """Valida que o schema Pydantic serializa e deserializa conforme contrato agêntico."""
        req = AgentRelayRequest(
            session_id="session-001",
            model="gemini-1.5-flash",
            messages=[
                AgentMessage(role=AgentRole.USER, content="Extraia o hemograma.")
            ],
            tools=[
                ToolDeclaration(
                    type="function",
                    function=ToolFunctionDef(
                        name="ocr_page",
                        description="Executa OCR local na página.",
                        parameters={
                            "type": "object",
                            "properties": {"page": {"type": "integer"}},
                            "required": ["page"],
                        },
                    ),
                )
            ],
        )

        raw_json = req.model_dump_json()
        deserialized = AgentRelayRequest.model_validate_json(raw_json)
        self.assertEqual(deserialized.session_id, "session-001")
        self.assertEqual(deserialized.messages[0].role, AgentRole.USER)
        self.assertEqual(deserialized.tools[0].function.name, "ocr_page")

    def test_max_payload_boundary(self):
        """Valida a constante de 256 KB para proteção de Zero File Egress."""
        self.assertEqual(MAX_PAYLOAD_BYTES, 256 * 1024)

    @patch("routers.agent_relay.GEMINI_API_KEY", "test-key-gemini")
    @patch("routers.agent_relay.httpx.AsyncClient")
    async def test_invoke_gemini_with_function_call(self, mock_client_cls):
        """Valida que o retorno functionCall do Gemini é convertido para tool_calls."""
        from routers.agent_relay import _invoke_gemini

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "ocr_page",
                                    "args": {"page": 1},
                                }
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 150,
                "candidatesTokenCount": 25,
                "totalTokenCount": 175,
            },
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        req = AgentRelayRequest(
            messages=[AgentMessage(role=AgentRole.USER, content="Leia a página 1.")],
            tools=[
                ToolDeclaration(
                    type="function",
                    function=ToolFunctionDef(
                        name="ocr_page",
                        description="OCR local",
                        parameters={"type": "object"},
                    ),
                )
            ],
        )

        res = await _invoke_gemini(req, "test-key-gemini")
        self.assertIsNotNone(res)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.provider, "gemini")
        self.assertIsNotNone(res.message.tool_calls)
        self.assertEqual(len(res.message.tool_calls), 1)
        self.assertEqual(res.message.tool_calls[0].function.name, "ocr_page")
        self.assertEqual(json.loads(res.message.tool_calls[0].function.arguments), {"page": 1})

    @patch("routers.agent_relay.GROQ_API_KEY", "test-key-groq")
    @patch("routers.agent_relay.httpx.AsyncClient")
    async def test_invoke_groq_fallback(self, mock_client_cls):
        """Valida chamada do Groq com fallback quando Gemini ausente."""
        from routers.agent_relay import _invoke_groq

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "search_anvisa",
                                    "arguments": '{"query": "losartana"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 15,
                "total_tokens": 95,
            },
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        req = AgentRelayRequest(
            messages=[AgentMessage(role=AgentRole.USER, content="Busque losartana.")],
        )

        res = await _invoke_groq(req, "test-key-groq")
        self.assertIsNotNone(res)
        self.assertEqual(res.provider, "groq")
        self.assertEqual(res.message.tool_calls[0].function.name, "search_anvisa")


if __name__ == "__main__":
    unittest.main()
