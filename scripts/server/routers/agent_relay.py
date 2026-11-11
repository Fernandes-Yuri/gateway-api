"""Router do Relay Agêntico (/api/ai/agent-relay).

Provê Function Calling unificado e agnóstico de modelo para o Mini Harness
Agêntico no Android. Protege segredos organizacionais (Gemini/Groq/OpenAI) e
enforça limite rígido de tamanho (256 KB) para garantir Zero File Egress.
"""

import json
import logging
import os
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api_identity import firebase_uid
from config import (
    GEMINI_API_KEY,
    GROQ_API_KEY,
    GROQ_API_URL,
)
from services.circuit_breaker import (
    gemini_circuit_breaker,
    groq_circuit_breaker,
)

logger = logging.getLogger("agent_relay")

router = APIRouter(prefix="/api/ai", tags=["Agent Relay"])

MAX_PAYLOAD_BYTES = 256 * 1024  # 256 KB: garante que apenas texto/JSON trafeguem


# =============================================================================
# Modelos Pydantic (Espelho dos contratos Rust de braga-contracts)
# =============================================================================

class AgentRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCallFunction(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction


class AgentMessage(BaseModel):
    role: AgentRole
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class ToolFunctionDef(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any]


class ToolDeclaration(BaseModel):
    type: str = "function"
    function: ToolFunctionDef


class AgentRelayRequest(BaseModel):
    session_id: Optional[str] = None
    model: Optional[str] = None
    messages: List[AgentMessage]
    tools: Optional[List[ToolDeclaration]] = None
    temperature: Optional[float] = 0.0
    max_tokens: Optional[int] = 1024


class AgentUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AgentRelayResponse(BaseModel):
    status: str = "ok"
    message: AgentMessage
    finish_reason: Optional[str] = None
    usage: Optional[AgentUsage] = None
    provider: Optional[str] = None


# =============================================================================
# Adaptadores de Provedor (Groq / Gemini)
# =============================================================================

async def _invoke_groq(req: AgentRelayRequest, token: str) -> Optional[AgentRelayResponse]:
    """Encaminha para a API Groq no formato padrão OpenAI Function Calling."""
    model_name = req.model or os.getenv("GROQ_MODEL_AGENT", "llama-3.3-70b-versatile")
    url = GROQ_API_URL
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Serializa mensagens para o formato OpenAI
    openai_messages = []
    for m in req.messages:
        item: Dict[str, Any] = {"role": m.role.value}
        if m.content is not None:
            item["content"] = m.content
        if m.tool_calls:
            item["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in m.tool_calls
            ]
        if m.tool_call_id:
            item["tool_call_id"] = m.tool_call_id
        if m.name:
            item["name"] = m.name
        openai_messages.append(item)

    payload: Dict[str, Any] = {
        "model": model_name,
        "messages": openai_messages,
        "temperature": req.temperature or 0.0,
        "max_tokens": req.max_tokens or 1024,
    }

    if req.tools:
        payload["tools"] = [
            {
                "type": t.type,
                "function": {
                    "name": t.function.name,
                    "description": t.function.description,
                    "parameters": t.function.parameters,
                },
            }
            for t in req.tools
        ]

    async def _post_groq_relay():
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code >= 500 or r.status_code == 429:
                r.raise_for_status()
            return r

    try:
        resp = await groq_circuit_breaker.call(_post_groq_relay)
        if resp.status_code != 200:
            logger.warning("[agent_relay] Falha na chamada Groq (%s): %s", resp.status_code, resp.text[:200])
            return None

            data = resp.json()
            choice = data["choices"][0]
            choice_msg = choice["message"]

            tool_calls = None
            if "tool_calls" in choice_msg and choice_msg["tool_calls"]:
                tool_calls = [
                    ToolCall(
                        id=tc.get("id") or str(uuid.uuid4()),
                        type=tc.get("type", "function"),
                        function=ToolCallFunction(
                            name=tc["function"]["name"],
                            arguments=tc["function"]["arguments"],
                        ),
                    )
                    for tc in choice_msg["tool_calls"]
                ]

            usage_data = data.get("usage", {})
            usage = AgentUsage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            )

            msg = AgentMessage(
                role=AgentRole.ASSISTANT,
                content=choice_msg.get("content"),
                tool_calls=tool_calls,
            )

            return AgentRelayResponse(
                status="ok",
                message=msg,
                finish_reason=choice.get("finish_reason"),
                usage=usage,
                provider="groq",
            )
    except Exception as exc:
        logger.error("[agent_relay] Erro ao comunicar com Groq: %s", exc)
        return None


async def _invoke_gemini(req: AgentRelayRequest, api_key: str) -> Optional[AgentRelayResponse]:
    """Encaminha para a API Google Gemini 1.5 Flash adaptando o schema Function Calling."""
    model_name = req.model or os.getenv("GEMINI_MODEL_AGENT", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"

    # Adapta mensagens para Gemini contents
    contents = []
    system_instruction = None

    for m in req.messages:
        if m.role == AgentRole.SYSTEM:
            if m.content:
                system_instruction = {"parts": [{"text": m.content}]}
            continue

        if m.role == AgentRole.USER:
            contents.append({
                "role": "user",
                "parts": [{"text": m.content or ""}],
            })
        elif m.role == AgentRole.ASSISTANT:
            parts: List[Dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            if m.tool_calls:
                for tc in m.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except Exception:
                        args = {"raw_args": tc.function.arguments}
                    parts.append({
                        "functionCall": {
                            "name": tc.function.name,
                            "args": args,
                        }
                    })
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif m.role == AgentRole.TOOL:
            try:
                resp_obj = json.loads(m.content or "{}")
            except Exception:
                resp_obj = {"result": m.content or ""}
            contents.append({
                "role": "function",
                "parts": [
                    {
                        "functionResponse": {
                            "name": m.name or "unknown_tool",
                            "response": resp_obj,
                        }
                    }
                ],
            })

    payload: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": req.temperature or 0.0,
            "maxOutputTokens": req.max_tokens or 1024,
        },
    }

    if system_instruction:
        payload["systemInstruction"] = system_instruction

    if req.tools:
        declarations = []
        for t in req.tools:
            declarations.append({
                "name": t.function.name,
                "description": t.function.description,
                "parameters": t.function.parameters,
            })
        payload["tools"] = [{"function_declarations": declarations}]

    async def _post_gemini_relay():
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
            if r.status_code >= 500 or r.status_code == 429:
                r.raise_for_status()
            return r

    try:
        resp = await gemini_circuit_breaker.call(_post_gemini_relay)
        if resp.status_code != 200:
            logger.warning("[agent_relay] Falha na chamada Gemini (%s): %s", resp.status_code, resp.text[:200])
            return None

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None

            cand = candidates[0]
            content_obj = cand.get("content", {})
            parts = content_obj.get("parts", [])

            text_content = None
            tool_calls: List[ToolCall] = []

            for part in parts:
                if "text" in part:
                    text_content = (text_content or "") + part["text"]
                if "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append(
                        ToolCall(
                            id=f"call_{uuid.uuid4().hex[:8]}",
                            type="function",
                            function=ToolCallFunction(
                                name=fc.get("name", ""),
                                arguments=json.dumps(fc.get("args", {})),
                            ),
                        )
                    )

            meta = data.get("usageMetadata", {})
            usage = AgentUsage(
                prompt_tokens=meta.get("promptTokenCount", 0),
                completion_tokens=meta.get("candidatesTokenCount", 0),
                total_tokens=meta.get("totalTokenCount", 0),
            )

            msg = AgentMessage(
                role=AgentRole.ASSISTANT,
                content=text_content,
                tool_calls=tool_calls if tool_calls else None,
            )

            return AgentRelayResponse(
                status="ok",
                message=msg,
                finish_reason=cand.get("finishReason"),
                usage=usage,
                provider="gemini",
            )
    except Exception as exc:
        logger.error("[agent_relay] Erro ao comunicar com Gemini: %s", exc)
        return None


# =============================================================================
# Endpoint Oficial do Relay
# =============================================================================

@router.post("/agent-relay", response_model=AgentRelayResponse)
async def agent_relay(
    request: Request,
    body: AgentRelayRequest,
    uid: str = Depends(firebase_uid),
):
    """Encaminha requisição agêntica autenticada para o provedor de IA configurado."""
    # Enforça limite estrito de payload (impede que arquivos binários transitem pelo relay)
    raw_body = await request.body()
    if len(raw_body) > MAX_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Payload excede o limite máximo permitido de {MAX_PAYLOAD_BYTES // 1024} KB.",
        )

    if not body.messages:
        raise HTTPException(status_code=400, detail="A lista de mensagens não pode estar vazia.")

    # 1. Tenta Gemini 1.5 Flash primeiro
    if GEMINI_API_KEY:
        gemini_resp = await _invoke_gemini(body, GEMINI_API_KEY)
        if gemini_resp is not None:
            return gemini_resp

    # 2. Fallback resiliente para Groq
    token = GROQ_API_KEY
    if token:
        groq_resp = await _invoke_groq(body, token)
        if groq_resp is not None:
            return groq_resp

    # 3. Caso nenhum provedor esteja acessível em produção
    logger.error("[agent_relay] Nenhum provedor de IA disponível para responder à requisição do usuário %s", uid)
    raise HTTPException(
        status_code=503,
        detail="Serviço de inteligência artificial temporariamente indisponível. Tente novamente em instantes.",
    )
