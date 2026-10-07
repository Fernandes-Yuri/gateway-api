"""Router do chat conversacional (canal REST do app).

Extraido do monolito server.py na Fase A passo 4
(doc 11_PLANO_MODULARIZACAO_E_TTS.md). Dominio: /v1/chat/completions.

Pipeline: auth -> escopo cuidador -> shield anti-injection -> agendamento
por voz (escopo cuidador) -> roteador deterministico -> memoria/preferencias
-> follow-up -> enriquecimento PostgreSQL -> LLM 2-passos (classifica +
gera fala). Tudo best-effort com fallback gracioso.
"""
import asyncio
import json
import time
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api_identity import firebase_uid, require_owner, require_family_access
from core.prompt import SYSTEM_PROMPT
from orb_router import (
    route_chat, detect_all, recall, remember, resolve_followup,
    dedupe_fala, symptom_repeat, SYMPTOM_KINDS, ESCALATION_SUFFIX,
)
from security_defense import is_prompt_injection
from trace_llm import trace_llm
from voice_scheduling import quer_agendar, agendar_por_voz

router = APIRouter()


class Message(BaseModel):
    role: str
    content: str


from orb_semantics import validated_label, is_current_emergency, original_text, conversation_only, validate_envelope


class ChatCompletionRequest(BaseModel):
    nlu_context: Optional[dict] = None
    model: Optional[str] = "qwen-2.5-72b"
    messages: List[Message]
    temperature: Optional[float] = 0.1
    max_tokens: Optional[int] = 220
    userId: Optional[str] = None
    # Escopo de papel (D48): distingue o MODO CUIDADOR do autocuidado.
    # A mesma pessoa pode ser paciente no Android e cuidadora no PWA; o escopo
    # separa o contexto (e a memoria) dos dois papeis — ver doc PWA secao 6.5.
    acting_as: Optional[str] = None
    patient_id: Optional[str] = None


# Memoria curta de clarificacao do REST (Fase 4): userId -> (kind, ts).
_REST_MEMORY: Dict[str, tuple] = {}
# Ultima fala template do REST (anti-repeticao): userId -> fala.
_REST_LASTFALA: Dict[str, str] = {}
# Repeticoes de sintoma no REST (escalada anti-loop).
_REST_SYMPTOM: Dict[str, tuple] = {}


ORB_CHAT_PROMPT = SYSTEM_PROMPT + (
    " REGRAS CRÍTICAS DE AÇÃO NO CHAT: "
    "1. O chat é ORIENTADOR e EDUCADOR — NUNCA registra nada. PROIBIDO usar 'REGISTRAR_PRESSAO', 'REGISTRAR_GLICEMIA', 'REGISTRAR_AGUA', 'REGISTRAR_BATIMENTOS', 'REGISTRAR_OXIGENACAO', 'BUSCAR_ALIMENTO', 'ABRIR_LISTA_COMPRAS' ou 'CONSULTAR_METRICAS'. "
    "O usuário faz as próprias anotações nas telas do aplicativo; o assistente de voz atua como agente e ajuda a preencher. "
    "2. Ações permitidas no chat: 'CONVERSA' (parametros {}) para tudo; 'EMERGENCIA' só em sintomas graves; 'LEMBRETES' e 'EXAMES' só quando o usuário pedir explicitamente remédios ou exames. "
    "3. NUNCA invente números de saúde nem afirme que salvou ou abriu tela sem confirmação. "
    "4. ANTI-FANTASMA DE DIETA: frases cotidianas sem menção explícita a comida ('tô na correria', 'vou descer ali rapidinho', 'bom dia') são CONVERSA — NUNCA interprete como refeição, NUNCA palpite o que a pessoa comeu, NUNCA dê dica de dieta não solicitada. Só fale de alimento se o usuário citar comida explicitamente ou pedir orientação nutricional. "
)


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    db_pool = _state.db_pool
    # Auth obrigatoria: antes, sem userId e sem header Authorization, o LLM
    # processava anonimamente (abuso de custo, sem rate limit real).
    if not req.userId and not request.headers.get("authorization"):
        raise HTTPException(status_code=401, detail="Autenticação necessária.")
    authenticated_uid = None
    if req.userId:
        authenticated_uid = await firebase_uid(request)
        require_owner(authenticated_uid, req.userId)
    elif request.headers.get('authorization'):
        authenticated_uid = await firebase_uid(request)

    # Escopo de papel (D48): se veio acting_as=caregiver, valida o vinculo
    # familiar no banco ANTES de processar. Sem isso, qualquer conta poderia
    # perguntar sobre qualquer paciente.
    if req.acting_as == "caregiver" and req.patient_id and authenticated_uid:
        if db_pool:
            async with db_pool.acquire() as conn:
                await require_family_access(conn, authenticated_uid, req.patient_id)

    try:
        user_prompt = ""
        for m in req.messages:
            if m.role == "user":
                user_prompt = m.content
        user_prompt = original_text(user_prompt)
        tid = trace_llm('chat-rest', 'request', user_text=user_prompt,
                        temperature=req.temperature, max_tokens=req.max_tokens)

        if any(is_prompt_injection(m.content) for m in req.messages if m.role == "user"):
            user_msg = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
            friendly_safety = _state.get_friendly_safety_response(user_msg)
            blocked_reply = {
                "fala": friendly_safety["fala"],
                "acao": "CONVERSA",
                "parametros": {}
            }
            return {
                "id": "chatcmpl-shield-blocked",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(blocked_reply, ensure_ascii=False)
                    },
                    "finish_reason": "stop"
                }]
            }

        # Camada 0 (Fase 2): roteador deterministico antes de qualquer LLM.
        # D50: no escopo CUIDADOR, a intencao de agendar consulta e tratada
        # ANTES do roteador — grava na agenda do paciente (vinculo validado).
        if req.acting_as == "caregiver" and req.patient_id and quer_agendar(user_prompt) and not is_current_emergency(user_prompt) and not conversation_only(user_prompt, req.nlu_context) and db_pool:
            try:
                async with db_pool.acquire() as _conn:
                    envelope = await agendar_por_voz(_conn, authenticated_uid, req.patient_id, user_prompt)
            except Exception as exc:
                trace_llm('chat-rest', 'scheduler_error', tid, error=str(exc))
                envelope = {"fala": "Não consegui agendar agora. Tente novamente em instantes.",
                            "acao": "AGENDAR_FALHA", "parametros": {}}
            trace_llm('chat-rest', 'scheduler_hit', tid, action=envelope.get("acao"),
                      fala=envelope.get("fala"))
            return {
                "id": "chatcmpl-scheduler",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant",
                                "content": json.dumps(envelope, ensure_ascii=False)},
                    "finish_reason": "stop",
                }],
            }

        routed = route_chat(user_prompt, req.nlu_context)
        preferences = {}
        # D48: no escopo cuidador, a memoria e preferencias sao indexadas por
        # uid+patient_id — nunca so uid — para nao misturar os dois papeis
        # da mesma pessoa (paciente no Android, cuidadora no PWA).
        scope_key = authenticated_uid
        if req.acting_as == "caregiver" and req.patient_id:
            scope_key = f"{authenticated_uid}@caregiver:{req.patient_id}"
        if scope_key and (routed is None or routed.get('acao') == 'CONVERSA'):
            memory_reply, preferences = await _state.assistant_memory(scope_key, user_prompt, req.messages)
            if memory_reply:
                return {'id': 'chatcmpl-memory', 'object': 'chat.completion', 'choices': [{
                    'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
                    'content': json.dumps({'fala': memory_reply, 'acao': 'CONVERSA', 'parametros': {}}, ensure_ascii=False)}}]}
        if routed is not None:
            _rest_kind = detect_all(user_prompt).get("kind")
            if scope_key and _rest_kind == "nutrition":
                remember(_REST_MEMORY, scope_key, "nutrition")
            if scope_key and _rest_kind in SYMPTOM_KINDS:
                if symptom_repeat(_REST_SYMPTOM, scope_key, _rest_kind) >= 2:
                    routed["fala"] += ESCALATION_SUFFIX
            if routed["acao"] == "CONVERSA" and scope_key:
                routed["fala"] = dedupe_fala(_REST_LASTFALA, scope_key, routed["fala"])
            trace_llm('chat-rest', 'router_hit', tid, action=routed["acao"], fala=routed["fala"])
            content = json.dumps(routed, ensure_ascii=False)
            await asyncio.to_thread(_state.log_fine_tuning_sample,
                user_prompt=user_prompt,
                assistant_reply=content,
                source="app_chat_router",
                system_prompt=ORB_CHAT_PROMPT,
                action=routed["acao"],
                action_params=routed["parametros"]
            )
            return {
                "id": "chatcmpl-router",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content
                    },
                    "finish_reason": "stop"
                }]
            }

        # Follow-up de clarificação (Fase 4): consome a memória e responde sem LLM.
        if req.userId:
            _rest_fu = resolve_followup(user_prompt, recall(_REST_MEMORY, req.userId), channel="chat")
            if _rest_fu is not None:
                _REST_MEMORY.pop(req.userId, None)
                _rest_fu["fala"] = dedupe_fala(_REST_LASTFALA, req.userId, _rest_fu["fala"])
                trace_llm('chat-rest', 'followup_hit', tid, fala=_rest_fu["fala"])
                _fu_content = json.dumps(_rest_fu, ensure_ascii=False)
                await asyncio.to_thread(_state.log_fine_tuning_sample,
                    user_prompt=user_prompt,
                    assistant_reply=_fu_content,
                    source="app_chat_followup",
                    system_prompt=ORB_CHAT_PROMPT,
                    action=_rest_fu["acao"],
                    action_params=_rest_fu["parametros"]
                )
                return {
                    "id": "chatcmpl-followup",
                    "object": "chat.completion",
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": _fu_content
                        },
                        "finish_reason": "stop"
                    }]
                }

        # Enriquecimento com Dados Reais do PostgreSQL (se userId presente)
        last_v = None
        if req.userId and db_pool:
            try:
                async with db_pool.acquire() as conn:
                    vitals = await conn.fetch("SELECT systolic_pressure, diastolic_pressure, glucose_level, measured_at FROM vital_signs WHERE user_id = $1 ORDER BY measured_at DESC LIMIT 3", req.userId)
                    if vitals:
                        last_v = vitals[0]
            except Exception:
                pass

        from orb_llm import (SPEAK_SYSTEM, build_classify_payload, build_speak_payload,
                             compose_envelope, metricas_fala, parse_label, FIXED_FALAS,
                             assistant_fala)
        headers = {"Authorization": f"Bearer {_state.groq_api_key}", "Content-Type": "application/json"}
        history = [
            {"role": m.role, "content": f"<fala_usuario>{m.content}</fala_usuario>" if m.role == "user" else assistant_fala(m.content)}
            for m in req.messages[-8:] if m.role in ("user", "assistant")
        ]
        async with _state.sem_rest:
            async with httpx.AsyncClient(timeout=15) as client:
                # Passo 1: classifica (barato, sem stream).
                _class_payload = build_classify_payload(_state.model_rest, user_prompt)
                trace_llm('chat-rest', 'classify_request', tid, payload=_class_payload)
                _ct0 = time.monotonic()
                class_resp = await client.post(_state.groq_api_url, json=_class_payload, headers=headers)
                if class_resp.status_code != 200:
                    raise HTTPException(status_code=class_resp.status_code, detail=class_resp.text)
                try:
                    label = parse_label(class_resp.json()["choices"][0]["message"]["content"])
                except Exception:
                    label = "CONVERSA"
                label = validated_label(user_prompt, label, req.nlu_context)
                trace_llm('chat-rest', 'classify_response', tid, label=label,
                          raw=class_resp.json()["choices"][0]["message"]["content"][:200],
                          latency_ms=round((time.monotonic() - _ct0) * 1000))
                # Passo 2: só CONVERSA gera fala livre; o resto é template.
                if label == "CONSULTAR_METRICAS":
                    content = compose_envelope(metricas_fala(last_v), "CONSULTAR_METRICAS", {})
                    action_name, action_params, system_used = "CONSULTAR_METRICAS", {}, "metricas-template"
                    trace_llm('chat-rest', 'speak_response', tid, template=True, content=content)
                elif label in FIXED_FALAS:
                    content = compose_envelope(FIXED_FALAS[label], label, {})
                    action_name, action_params, system_used = label, {}, "fixed-template"
                    trace_llm('chat-rest', 'speak_response', tid, template=True, content=content)
                else:
                    speak_payload = build_speak_payload(_state.model_rest, history, req.temperature or 0.1, min(req.max_tokens or 220, 220), preferences)
                    trace_llm('chat-rest', 'speak_request', tid, payload=speak_payload)
                    speak_resp = await client.post(_state.groq_api_url, json=speak_payload, headers=headers)
                    if speak_resp.status_code != 200:
                        raise HTTPException(status_code=speak_resp.status_code, detail=speak_resp.text)
                    fala = (speak_resp.json()["choices"][0]["message"]["content"] or "").strip()
                    if not fala:
                        raise HTTPException(status_code=502, detail="Resposta vazia do provedor")
                    trace_llm('chat-rest', 'speak_response', tid, fala=fala)
                    safe = validate_envelope(user_prompt, {"fala": fala, "acao": "CONVERSA", "parametros": {}}, req.nlu_context)
                    content = compose_envelope(safe["fala"], "CONVERSA", {})
                    action_name, action_params, system_used = "CONVERSA", {}, SPEAK_SYSTEM

        await asyncio.to_thread(_state.log_fine_tuning_sample,
            user_prompt=user_prompt,
            assistant_reply=content,
            source="app_chat",
            system_prompt=system_used,
            action=action_name,
            action_params=action_params
        )

        return {
            "id": "chatcmpl-twostep",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "finish_reason": "stop"
            }]
        }
    except HTTPException:
        raise
    except Exception as e:
        # Log interno completo; o cliente so recebe mensagem generica para
        # nao vazar internals (strings de conexao, caminhos, stack).
        print(f"[Internal Error] {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Erro interno do servidor. Tente novamente.")


class _AppState:
    """Porta das dependencias do monolito (db_pool, Groq, callbacks de
    memoria/seguranca/dataset). Preenchida por server.py no startup."""

    db_pool = None
    groq_api_key = ""
    groq_api_url = ""
    model_rest = None
    sem_rest = None
    assistant_memory = None
    get_friendly_safety_response = None
    log_fine_tuning_sample = None


_state = _AppState()


def configure_state(*, db_pool=None, groq_api_key="", groq_api_url="",
                    model_rest=None, sem_rest=None, assistant_memory=None,
                    get_friendly_safety_response=None, log_fine_tuning_sample=None):
    """Chamado por server.py ao subir a aplicacao."""
    _state.db_pool = db_pool
    _state.groq_api_key = groq_api_key
    _state.groq_api_url = groq_api_url
    _state.model_rest = model_rest
    _state.sem_rest = sem_rest
    _state.assistant_memory = assistant_memory
    _state.get_friendly_safety_response = get_friendly_safety_response
    _state.log_fine_tuning_sample = log_fine_tuning_sample
