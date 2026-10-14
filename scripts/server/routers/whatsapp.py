"""Router do WhatsApp Business Cloud API (canal de voz do Braga).

Extraido do monolito server.py na Fase A passo 7
(doc 11_PLANO_MODULARIZACAO_E_TTS.md) — o ultimo e mais invasivo.

Dominios: /api/whatsapp/{status, send, webhook (GET+POST), events}.

O que mora aqui:
- verify_whatsapp_webhook (desafio hub.mode/hub.challenge do painel Meta)
- receive_whatsapp_webhook (POST; assinatura HMAC obrigatoria, outbox
  antes do ack para nao perder mensagem se o processo cair)
- process_incoming_whatsapp_payload (pipeline completo: transcricao de
  audio -> reacao calorosa -> anti-injection -> vinculo TOTP -> portao de
  conta -> fast-lane de audio -> router deterministico Fase 4 -> Groq)
- handle_confirmation_callback (botes Confirmar/Editar/Cancelar)
- respostas rápidas em texto (FAST_LANE_INTENTS)

Seguranca preservada: verify_meta_signature no webhook (zero-trust),
match EXATO de telefone (AUD-G10), admin-only em /events (AUD-G09),
expira pendencias vencidas a cada callback, is_prompt_injection antes
do LLM e clean_text_for_tts antes de sintetizar.
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from api_identity import firebase_uid
from config import (
    GROQ_API_KEY_WHATSAPP,
    GROQ_API_URL,
    MODEL_ID,
    MODEL_WHATSAPP,
    PROJECT_ROOT,
)
from conversation_policy import build_conversation_system
from core.confirmation import (
    cancel_pending_confirmation,
    confirm_pending_confirmation,
    create_pending_confirmation,
    send_confirmation_buttons,
    validate_health_data,
)
# WhatsApp não alimenta o dataset de treinamento. O app mantém seu pipeline.
def log_fine_tuning_sample(*args, **kwargs):
    return None
from core.text import clean_text_for_tts
from orb_router import (
    ESCALATION_SUFFIX,
    QUERY_AMBIGUOUS_FALA,
    SYMPTOM_KINDS,
    dedupe_fala,
    detect_all,
    recall,
    remember,
    resolve_followup,
    symptom_repeat,
    whatsapp_next,
)
from security_defense import is_prompt_injection
from trace_llm import trace_llm
from whatsapp_conversation import FALLBACK_REPLY, identity_reply, parse_conversation_reply, retry_public_reply

router = APIRouter()


from services import wa_session_context as dialogue


async def _send_context_text(phone, text):
    result = await send_whatsapp_text(phone, text)
    if result is not False and (not isinstance(result, dict) or result.get('success') is not False):
        dialogue.record('assistant', text, phone_hash=channel_phone_hash(phone))
    return result


async def _send_context_confirmation(**kwargs):
    result = await send_confirmation_buttons(**kwargs)
    if result is not False and (not isinstance(result, dict) or result.get('success') is not False):
        from core.confirmation import format_confirmation_value
        metric = kwargs['metric_type']
        value, _icon = format_confirmation_value(metric, kwargs['values'])
        dialogue.record('assistant', f'Proposta de {metric}: {value}. Aguarda confirmação; ainda não é um registro confirmado.',
                        phone_hash=channel_phone_hash(kwargs['to_number']))
    return result


# =============================================================
# SERVICOS DEFENSIVOS (espelham o try/except do server.py)
# =============================================================
try:
    from whatsapp_service import (
        WHATSAPP_PHONE_NUMBER_ID,
        download_whatsapp_media,
        mark_whatsapp_message_read,
        send_whatsapp_interactive,
        send_whatsapp_emergency,
        send_whatsapp_reaction,
        send_whatsapp_template,
        send_whatsapp_text,
        transcribe_audio_whisper,
    )
    HAS_WHATSAPP = True
except ImportError:
    HAS_WHATSAPP = False
    WHATSAPP_PHONE_NUMBER_ID = None

    async def send_whatsapp_text(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    async def send_whatsapp_emergency(to_number, *, send_text=None):  # type: ignore
        from services.emergency_reply import emergency_reply
        return await (send_text or send_whatsapp_text)(to_number, emergency_reply())

    async def send_whatsapp_template(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    async def send_whatsapp_reaction(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    async def mark_whatsapp_message_read(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    async def download_whatsapp_media(*args, **kwargs):  # type: ignore
        return None

    async def send_whatsapp_interactive(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    def transcribe_audio_whisper(*args, **kwargs):  # type: ignore
        return ""

try:
    from wa_link_service import handle_link_attempt
    from wa_gate_service import gate_unregistered
    HAS_WA_LINK = True
    HAS_WA_GATE = True
except ImportError:
    HAS_WA_LINK = False
    HAS_WA_GATE = False

    async def handle_link_attempt(*args, **kwargs):  # type: ignore
        return False

    async def gate_unregistered(*args, **kwargs):  # type: ignore
        return False

try:
    from services.whatsapp_identity import (
        STATUS_OK,
        STATUS_UNAVAILABLE,
        STATUS_UNLINKED,
        context_from_identity,
        is_confirmed,
        resolve_linked_account,
    )
except ImportError:  # pragma: no cover - fallback defensivo do monolito
    STATUS_OK, STATUS_UNAVAILABLE, STATUS_UNLINKED = "ok", "unavailable", "unlinked"

    def is_confirmed(identity):  # type: ignore
        return bool(identity) and identity.get("status") == "ok"

    async def resolve_linked_account(conn, sender_phone):  # type: ignore
        return {"status": STATUS_UNLINKED, "user_id": None, "full_name": None}

    def context_from_identity(identity, **kwargs):  # type: ignore
        return None

try:
    from services.whatsapp_dedup import claim_message_receipt, claim_operation_key
except ImportError:  # pragma: no cover - fallback defensivo do monolito

    async def claim_message_receipt(conn, message_id, user_id=None):  # type: ignore
        return True

    async def claim_operation_key(conn, *, message_id, kind, actor_id):  # type: ignore
        return True

try:
    from services.health_queries import (
        blood_pressure_today,
        day_summary,
        last_blood_pressure,
        last_glucose,
        last_heart_rate,
        last_oxygen_saturation,
        last_weight,
        water_today,
    )
    from services.operations import OperationResult

    async def _ambiguous_query(conn, ctx):
        """Ambíguo: sem métrica específica, pergunta qual dado (não adivinha)."""
        return OperationResult.clarification(QUERY_AMBIGUOUS_FALA)

    # Passo 2.3: cada intent de consulta chama o serviço daquela métrica.
    _WA_QUERY_HANDLERS = {
        "query_bp": last_blood_pressure,
        "query_glucose": last_glucose,
        "query_hr": last_heart_rate,
        "query_spo2": last_oxygen_saturation,
        "query_weight": last_weight,
        "query_water": water_today,
        "query_summary": day_summary,
        "query_ambiguous": _ambiguous_query,
    }
except ImportError:  # pragma: no cover - fallback defensivo do monolito
    _WA_QUERY_HANDLERS = {}

try:
    from services.family_target import resolve_target_context as _wa_family_target_ctx
    _WA_FAMILY_RESOLVER = _wa_family_target_ctx
except ImportError:  # pragma: no cover - fallback defensivo do monolito
    _WA_FAMILY_RESOLVER = None

try:
    from webhook_security import verify_meta_signature
    HAS_WEBHOOK_SECURITY = True
except ImportError:
    HAS_WEBHOOK_SECURITY = False

    def verify_meta_signature(*args, **kwargs):  # type: ignore
        return False


# =============================================================
# MODELOS
# =============================================================
class WhatsAppSendRequest(BaseModel):
    phone: str
    message: Optional[str] = None
    templateName: Optional[str] = None
    languageCode: Optional[str] = None


# ---------------------------------------------------------------------------
# PAUSA TEMPORARIA DO CANAL WHATSAPP PARA OTP (18/09/2026)
# ---------------------------------------------------------------------------
# A Meta nao aprova os templates de autenticacao e a conta business ainda nao
# foi verificada, entao o codigo OTP por WhatsApp nunca chegava ao usuario.
# O app agora usa SOMENTE e-mail para OTP. O canal WhatsApp foi pausado aqui
# (nao removido): o codigo continua intacto para religar assim que a Meta
# liberar o template.
#
# Para religar: basta colocar WHATSAPP_OTP_PAUSED = False.
# ---------------------------------------------------------------------------
WHATSAPP_OTP_PAUSED = True


# =============================================================
# DIÁRIO DE EVENTOS + OUTBOX
# =============================================================
# O WHATSAPP_EVENT_LOG e so diagnostico e vive em memoria (limite 200).
# O outbox JSONL abaixo e a persistencia bruta: gravada ANTES do ack 200,
# de forma que uma queda do processo entre o recebimento e o processamento
# nao perde a mensagem (a Meta nao reenvia quem ela ackou).
WHATSAPP_EVENT_LOG: List[Dict[str, Any]] = []
WEBHOOK_OUTBOX_PATH = os.path.join(PROJECT_ROOT, "webhook_outbox.log")


# =============================================================
# TOM CALOROSO (reacao 💚)
# =============================================================
WARM_AFFEX_KEYWORDS = [
    "obrigad", "valeu", "gratidão", "grato", "grata", "deus te abençoe", "deus abençoe",
    "fica com deus", "boa noite", "bom dia", "durma bem", "querida", "linda", "anjo",
    "amor", "carinho", "adorei", "muito bom", "maravilha", "abraço", "beijo", "beijos",
    "maravilhosa", "fofa", "coração"
]


def is_warm_and_affectionate(text: str) -> bool:
    """Detecta se a mensagem possui tom caloroso, afetuoso ou de agradecimento."""
    if not text:
        return False
    lower = text.lower()
    return any(k in lower for k in WARM_AFFEX_KEYWORDS)


# =============================================================
# RESPOSTAS RÁPIDAS EM TEXTO
# =============================================================
# Saudações e dúvidas frequentes não precisam de uma chamada ao modelo.
FAST_LANE_INTENTS = {
    "ask_audio": {
        "text": "Por enquanto, minhas respostas no WhatsApp são somente por texto. Como posso te ajudar hoje?",
        "keywords": [
            "manda audio", "manda áudio", "mandar audio", "mandar áudio",
            "responde em audio", "responde em áudio", "falar por audio", "falar por áudio",
            "consegue mandar audio", "consegue me mandar audio", "consegue me mandar áudio",
            "voce manda audio", "você manda áudio", "manda por audio", "manda por áudio",
            "pode falar por audio", "pode falar por áudio", "responde audio", "responde áudio"
        ]
    },
    "greeting_morning": {
        "text": "Bom dia! Tudo bem com você? Como posso te ajudar a cuidar da sua saúde e bem-estar hoje?",
        "keywords": ["bom dia", "otimo dia", "ótimo dia", "excelente dia", "um bom dia"]
    },
    "greeting_afternoon": {
        "text": "Boa tarde! Tudo bem por aí? Me conta, como posso te ajudar com a sua saúde hoje?",
        "keywords": ["boa tarde", "otima tarde", "ótima tarde", "uma boa tarde"]
    },
    "greeting_evening": {
        "text": "Boa noite! Espero que o seu dia tenha sido tranquilo. Como posso te ajudar hoje?",
        "keywords": ["boa noite", "otima noite", "ótima noite", "uma boa noite", "durma bem"]
    },
    "greeting_general": {
        "text": "Olá! Tudo ótimo por aqui. É uma alegria falar com você! Me conta, como posso te ajudar hoje?",
        "keywords": ["olá", "ola", "oi braga", "tudo bem", "como vai", "fala braga", "e ai braga", "e aí braga", "oi tudo bem", "ola tudo bem"]
    },
    "gratitude": {
        "text": "Imagina, por nada! Estou sempre aqui para cuidar de você e da sua família com todo carinho. Se precisar de qualquer coisa, é só me chamar!",
        "keywords": ["obrigado", "obrigada", "valeu", "gratidão", "muito obrigado", "muito obrigada", "agradeço"]
    },
    "app_info": {
        "text": "O aplicativo Braga Saúde ainda está em fase de desenvolvimento e com carinho muito em breve estará disponível na Play Store! Por enquanto, você já pode acessar para conhecer os Termos de Uso e a Política de Privacidade. O app vai te ajudar a acompanhar sua hidratação, pressão arterial e rotina, com o objetivo principal de gerar um relatório em PDF bem organizado para você levar ao médico, tornando a avaliação e as decisões clínicas muito mais assertivas. Vou deixar o link logo abaixo para você poder conferir com calma!",
        "link_text": "https://bragasaude.online/",
        "keywords": ["onde baixo", "como baixo", "link do app", "link do aplicativo", "baixar o app", "onde encontro o app", "mandar o link", "como baixar", "link para baixar", "onde baixar", "baixar aplicativo"]
    }
}

CLINICAL_BYPASS_KEYWORDS = [
    "dor", "pressao", "pressão", "glicemia", "falta de ar", "remedio", "remédio",
    "sintoma", "tontura", "febre", "enjoo", "enjôo", "vomito", "vômito", "peito",
    "sangue", "insulina", "samu", "192", "posto", "ubs", "upa", "hospital",
    "infarto", "avc", "queda", "machuquei", "ajuda medica", "ajuda médica", "receita"
]

def match_fast_lane_intent(text: str) -> Optional[str]:
    """Retorna o intent engatilhado para respostas ultra-rápidas se não houver termos clínicos."""
    if not text:
        return None
    lower = text.lower().strip()
    words = lower.split()
    if len(words) > 18:
        return None
    if any(ck in lower for ck in CLINICAL_BYPASS_KEYWORDS):
        return None

    # Verifica intents por ordem de prioridade
    for intent_key in ("ask_audio", "app_info", "gratitude", "greeting_morning", "greeting_afternoon", "greeting_evening", "greeting_general"):
        for kw in FAST_LANE_INTENTS[intent_key]["keywords"]:
            if kw in lower:
                return intent_key
    return None


# =============================================================
# MEMÓRIA CURTA + MAPA MÉTRICA->AÇÃO
# =============================================================
# Memoria curta de clarificacao do WhatsApp (Fase 4): telefone -> kind.
_WA_OPERATION_MEMORY: Dict[str, tuple] = {}


def _remember_operation(user_id, text, result, kind):
    if result.status == "clarification":
        _WA_OPERATION_MEMORY[user_id] = (time.monotonic(), text, result.data or {}, kind)
    else:
        _WA_OPERATION_MEMORY.pop(user_id, None)


async def _send_family_clarification(phone, result):
    key = "".join(filter(str.isdigit, phone or "")) or phone
    message = result.message
    alternative = "Para continuar com segurança, escolha pelo nome ou número. " + message
    await _send_context_text(phone, dedupe_fala(_WA_LASTFALA, key, message, alternative))


def _operation_followup(user_id, text):
    pending = _WA_OPERATION_MEMORY.pop(user_id, None)
    if not pending or time.monotonic() - pending[0] > 300:
        return text
    from services.family_target import _norm
    from services.medication_lookup import detect_hour
    answer = _norm(text)
    candidates = pending[2].get("candidates", [])
    option = re.fullmatch(r"(?:opcao\s*)?(\d+)", answer)
    if option and 1 <= int(option[1]) <= len(candidates):
        selected = candidates[int(option[1]) - 1].get("full_name")
        if selected:
            return pending[1] + " da " + selected
    names = [candidate.get("full_name") or candidate.get("name") or "" for candidate in candidates]
    matches = any(answer and (answer == _norm(name) or answer in _norm(name).split()) for name in names)
    if pending[3] == "query_ambiguous" and len(answer.split()) <= 4:
        metric = detect_all("qual " + text).get("kind", "")
        if metric.startswith("query_") and metric != "query_ambiguous":
            return pending[1] + " " + text
    if matches or (pending[3] == "med_take" and detect_hour(answer)):
        return pending[1] + " " + ("da " if pending[3] != "med_take" else "") + text
    return text


_WA_MEMORY: Dict[str, tuple] = {}
# Ultima fala template do WhatsApp (anti-repeticao): telefone -> fala.
_WA_LASTFALA: Dict[str, str] = {}
# Repeticoes de sintoma no WhatsApp (escalada anti-loop).
_WA_SYMPTOM: Dict[str, tuple] = {}

# Metrica -> acao do fluxo de confirmacao (inverso do metric_type_map).
_WA_METRIC_TO_ACTION = {
    "PRESSAO": "REGISTRAR_PRESSAO",
    "GLICEMIA": "REGISTRAR_GLICEMIA",
    "AGUA": "REGISTRAR_AGUA",
    "PESO": "REGISTRAR_PESO",
    "BATIMENTOS": "REGISTRAR_BATIMENTOS",
    "OXIGENACAO": "REGISTRAR_OXIGENACAO",
}


# =============================================================
# RESPOSTA DIRETA DO ROUTER
# =============================================================
async def _wa_send_router_text(sender_phone: str, msg_type: str, fala: str,
                               user_text: str, user_name) -> None:
    """Resposta direta do router no WhatsApp (áudio se a entrada foi áudio)."""
    try:
        log_fine_tuning_sample(
            user_prompt=user_text,
            assistant_reply=fala,
            source="whatsapp_voice_router" if msg_type in ("audio", "voice") else "whatsapp_router",
            system_prompt="ORB router Fase 4 (template, sem LLM)",
            user_name=user_name,
            action="ROUTER",
            action_params={}
        )
    except Exception:
        pass
    await _send_context_text(sender_phone, fala)


async def handle_wa_health_confirmation(*, sender_phone: str, user_id: str, user_name,
                                        msg_type: str, db, action_name: str,
                                        action_params: dict, reply_text: str,
                                        user_text: str, system_label: str,
                                        context=None) -> bool:
    """Fluxo de confirmação com botões (extraído do caminho LLM, Fase 4).

    Retorna True quando resolveu (valor inválido avisado ou botões
    enviados); False quando o chamador deve seguir o fluxo normal.

    context (passo 1.3/1.5): OperationContext do canal; carrega actor_id,
    patient_id, channel e message_id. No autocuidado do WhatsApp, actor e
    patient são a mesma conta; a pendência é sempre do paciente.
    """
    health_actions = ["REGISTRAR_PRESSAO", "REGISTRAR_GLICEMIA", "REGISTRAR_AGUA",
                      "REGISTRAR_PESO", "REGISTRAR_BATIMENTOS", "REGISTRAR_OXIGENACAO"]
    if action_name not in health_actions or not user_id:
        return False
    # O registro é feito no contexto de uma operação; sem contexto (caminho
    # legado), trata-se de autocuidado da própria conta.
    patient_id = getattr(context, "patient_id", None) or user_id
    actor_id = getattr(context, "actor_id", None) or user_id
    metric_type_map = {
        "REGISTRAR_PRESSAO": "PRESSAO",
        "REGISTRAR_GLICEMIA": "GLICEMIA",
        "REGISTRAR_AGUA": "AGUA",
        "REGISTRAR_PESO": "PESO",
        "REGISTRAR_BATIMENTOS": "BATIMENTOS",
        "REGISTRAR_OXIGENACAO": "OXIGENACAO"
    }
    metric_type = metric_type_map.get(action_name)
    if not metric_type:
        return False
    is_valid, error_msg, validated_params = validate_health_data(metric_type, action_params)
    if not is_valid:
        await _send_context_text(sender_phone,
            f"⚠️ *Valor inválido:*\n{error_msg}\n\n"
            f"Por favor, envie novamente o valor correto."
        )
        return True
    # Chave de operação persistida (passo 1.4): amarra a pendência à mensagem
    # que a originou — um reenvio daquela mensagem não cria segunda pendência.
    _op_message_id = getattr(context, "message_id", None)
    _op_kind = f"pending:{metric_type}"
    if db and actor_id:
        try:
            async with db.acquire() as _conn:
                _claimed = await claim_operation_key(
                    _conn, message_id=_op_message_id, kind=_op_kind, actor_id=actor_id)
            if not _claimed:
                print(f"[WhatsApp Confirmation] Operação já pendente: {_op_kind} {_op_message_id}")
                await _send_context_text(
                    sender_phone,
                    "Você já me enviou este valor. Estou aguardando a confirmação nos botões. 💚")
                return True
        except Exception as _op_err:
            print(f"[WhatsApp Confirmation] Aviso ao reservar chave de operação: {_op_err}")
    confirmation_code = await create_pending_confirmation(
        user_id=patient_id,
        metric_type=metric_type,
        value_json=validated_params,
        db_pool=db
    )
    if confirmation_code:
        await mark_channel_pending(db, confirmation_code, sender_phone, actor_id)
        await _send_context_confirmation(
            to_number=sender_phone,
            metric_type=metric_type,
            values=validated_params,
            confirmation_code=confirmation_code,
            user_name=user_name
        )
        print(f"[WhatsApp Confirmation] Botões enviados: {confirmation_code} para {metric_type}")
        log_fine_tuning_sample(
            user_prompt=user_text,
            assistant_reply=reply_text,
            source="whatsapp_voice" if msg_type in ("audio", "voice") else "whatsapp",
            system_prompt=system_label,
            user_name=user_name,
            action=action_name,
            action_params={**action_params, "pending_confirmation": True, "confirmation_code": confirmation_code}
        )
        return True
    print("[WhatsApp Confirmation] Erro ao criar pendência, enviando resposta normal")
    return False


async def handle_wa_medication_take(*, sender_phone: str, user_id: str, user_name,
                                    msg_type: str, db, user_text: str,
                                    context=None) -> bool:
    """Tomada de medicação no WhatsApp (PR3, passo 3.3).

    Identifica o remédio cadastrado, resolve o horário programado
    (scheduled_for) e cria a pendência guardando medicamento e horário. A
    tomada em si só acontece no botão Confirmar (passo 3.4), na mesma
    transação do consumo da pendência — nada é gravado pela metade, e
    Cancelar/Editar não gera tomada.

    Retorna True quando respondeu (botões enviados ou pedido de
    esclarecimento do serviço); False caso contrário, e o pipeline segue.
    """
    if not user_id:
        return False
    try:
        from services.medication_take import prepare_medication_take
        from services.operations import OperationContext
    except ImportError:  # pragma: no cover - fallback defensivo do monolito
        print("[WhatsApp Med Take] Serviço de tomada indisponível")
        return False

    take_ctx = context or OperationContext(
        actor_id=user_id, patient_id=user_id, channel="whatsapp")
    if _WA_FAMILY_RESOLVER is not None:
        async with db.acquire() as conn:
            take_ctx, error = await _WA_FAMILY_RESOLVER(conn, take_ctx, user_text)
        if error:
            _remember_operation(user_id, user_text, error, "med_take")
            await _send_context_text(sender_phone, error.message)
            return True
    _op_message_id = getattr(take_ctx, "message_id", None)

    # Chave de operação persistida (passo 1.4): mesma mensagem não cria duas
    # pendências de tomada. Best-effort, como no fluxo de saúde.
    try:
        async with db.acquire() as _conn:
            _claimed = await claim_operation_key(
                _conn, message_id=_op_message_id, kind="pending:MED_TOMADA",
                actor_id=take_ctx.actor_id)
        if not _claimed:
            print(f"[WhatsApp Med Take] Tomada já pendente: {_op_message_id}")
            await _send_context_text(
                sender_phone,
                "Você já me enviou essa tomada. Estou aguardando a confirmação nos botões. 💚")
            return True
    except Exception as _op_err:
        print(f"[WhatsApp Med Take] Aviso ao reservar chave de operação: {_op_err}")

    take_result = None
    try:
        async with db.acquire() as conn:
            take_result = await prepare_medication_take(
                conn, db, context=take_ctx, text=user_text)
    except Exception as take_err:
        print(f"[WhatsApp Med Take] Falha ao preparar tomada: {take_err}")
        return False
    if take_result is None or not take_result.message:
        return False
    _remember_operation(user_id, user_text, take_result, "med_take")

    if take_result.is_confirmation and take_result.pending_op_id:
        await mark_channel_pending(db, take_result.pending_op_id, sender_phone, take_ctx.actor_id)
        await _send_context_text(sender_phone, take_result.message)
        await _send_context_confirmation(
            to_number=sender_phone,
            metric_type="MED_TOMADA",
            values=take_result.data or {},
            confirmation_code=take_result.pending_op_id,
            user_name=user_name,
        )
        print(f"[WhatsApp Med Take] Botões enviados: {take_result.pending_op_id}")
    else:
        # clarification (vários remédios ou horário ambíguo), missing_data
        # (nenhum remédio cadastrado) ou indisponibilidade: texto do serviço.
        await _send_context_text(sender_phone, take_result.message)

    # Log sem dado clínico (nome do remédio e horário não entram): registra
    # a intenção e o resultado estruturado, como no fluxo de consultas.
    try:
        log_fine_tuning_sample(
            user_prompt=user_text,
            assistant_reply=take_result.message,
            source="whatsapp_voice" if msg_type in ("audio", "voice") else "whatsapp",
            system_prompt="medication_take (serviço, sem LLM)",
            user_name=user_name,
            action="TOMADA_MEDICAMENTO",
            action_params={"result": take_result.status,
                           "confirmation_code": take_result.pending_op_id}
        )
    except Exception:
        pass
    return True


async def handle_wa_care_message(*, sender_phone: str, user_id: str, user_name,
                                 msg_type: str, db, user_text: str,
                                 context=None) -> bool:
    """Mensagem de cuidado no WhatsApp (PR4, passo 4.3).

    "Me lembra de beber água" vira uma proposta com destinatário e texto,
    exigindo confirmação antes do envio. Não promete agenda nem envio futuro:
    nesta entrega, lembrete é mensagem enviada após o clique (passo 4.4).

    Retorna True quando respondeu (proposta com botões ou esclarecimento do
    serviço); False caso contrário, e o pipeline segue.
    """
    from services.care_intent import is_care_request, is_reminder_request, REMINDER_APP_REPLY
    if is_reminder_request(user_text):
        await _send_context_text(sender_phone, REMINDER_APP_REPLY)
        return True
    if not is_care_request(user_text):
        return False
    if not user_id:
        return False
    try:
        from services.care_messages import prepare_care_message
        from services.operations import OperationContext
    except ImportError:  # pragma: no cover - fallback defensivo do monolito
        print("[WhatsApp Care] Serviço de mensagem de cuidado indisponível")
        return False

    care_ctx = context or OperationContext(
        actor_id=user_id, patient_id=user_id, channel="whatsapp")

    # Familiar mencionado? O alvo é o paciente do vínculo (passo 4.1).
    if _WA_FAMILY_RESOLVER is not None:
        async with db.acquire() as conn:
            care_ctx, _fam_err = await _WA_FAMILY_RESOLVER(conn, care_ctx, user_text)
        if _fam_err is not None and _fam_err.message:
            _remember_operation(user_id, user_text, _fam_err, "reminder")
            await _send_family_clarification(sender_phone, _fam_err)
            return True

    care_result = None
    try:
        async with db.acquire() as conn:
            care_result = await prepare_care_message(
                conn, db, context=care_ctx, text=user_text)
    except Exception as care_err:
        print(f"[WhatsApp Care] Falha ao preparar mensagem: {care_err}")
        return False
    if care_result is None or not care_result.message:
        return False

    if care_result.is_confirmation and care_result.pending_op_id:
        await mark_channel_pending(db, care_result.pending_op_id, sender_phone, care_ctx.actor_id)
        await _send_context_text(sender_phone, care_result.message)
        await _send_context_confirmation(
            to_number=sender_phone,
            metric_type="CARE_MESSAGE",
            values={"body": (care_result.data or {}).get("body", "")},
            confirmation_code=care_result.pending_op_id,
            user_name=user_name,
        )
        print(f"[WhatsApp Care] Botões enviados: {care_result.pending_op_id}")
    else:
        # clarification (conteúdo insuficiente) / denied / unavailable.
        await _send_context_text(sender_phone, care_result.message)

    # Log sem o conteúdo da mensagem pessoal (LGPD): só a intenção e o
    # resultado, como nos demais fluxos de serviço.
    try:
        log_fine_tuning_sample(
            user_prompt=user_text,
            assistant_reply=care_result.message,
            source="whatsapp_voice" if msg_type in ("audio", "voice") else "whatsapp",
            system_prompt="care_messages (serviço, sem LLM)",
            user_name=user_name,
            action="MENSAGEM_CUIDADO",
            action_params={"result": care_result.status,
                           "confirmation_code": care_result.pending_op_id}
        )
    except Exception:
        pass
    return True


async def handle_wa_preference(*, sender_phone: str, user_id: str, user_name,
                               msg_type: str, db, user_text: str,
                               context=None) -> bool:
    """Preferências persistentes pertencem ao app, não a este assistente."""
    from services.wa_preferences import detect_preference_intent
    if not detect_preference_intent(user_text):
        return False
    from services.wa_onboarding import preference_reply
    await _send_context_text(sender_phone, preference_reply(user_text, sender_phone))
    return True


# =============================================================
# PIPELINE DE MENSAGEM ENTRANTE
# =============================================================
async def _process_authorized_whatsapp_payload(payload: Dict[str, Any]):
    """Processa mensagens recebidas no WhatsApp e responde usando a IA Qwen 2.5 72B."""
    if not HAS_WHATSAPP:
        return
    db_pool = _state.db_pool
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                val = change.get("value", {})
                messages = val.get("messages", [])
                for msg in messages:
                    msg_id = msg.get("id")
                    msg_type = msg.get("type")
                    sender_phone = msg.get("from")
                    user_text = ""

                    # Marca a mensagem como lida (dois risquinhos azuis no WhatsApp)
                    if msg_id:
                        asyncio.create_task(mark_whatsapp_message_read(msg_id))

                    if msg_type == "text":
                        user_text = msg.get("text", {}).get("body", "").strip()
                    elif msg_type in ("audio", "voice"):
                        audio_info = msg.get("audio") or msg.get("voice") or {}
                        media_id = audio_info.get("id")
                        if media_id:
                            print("[WhatsApp Audio] Processando áudio após aceite")
                            audio_bytes = await download_whatsapp_media(media_id)
                            if audio_bytes:
                                user_text = await transcribe_audio_whisper(audio_bytes, GROQ_API_KEY_WHATSAPP) or ""
                                print("[WhatsApp Audio] Transcrição concluída")
                        if not user_text:
                            await _send_context_text(
                                sender_phone,
                                "Olá! Recebi o seu áudio, mas não consegui escutar nitidamente. Pode me mandar de novo ou escrever por texto? 💚"
                            )
                            continue

                    if not sender_phone or not user_text:
                        continue

                    # Antiduplicidade (passo 1.4): a Meta reenvia mensagens.
                    # Registra o recebimento no banco; reenvio = mesma
                    # mensagem = não reprocessa (nova pendência, novo registro).
                    # user_id fica em branco aqui: a identidade é resolvida
                    # depois, no passo 2 do pipeline (vínculo confirmado).
                    if db_pool and msg_id:
                        try:
                            async with db_pool.acquire() as _conn:
                                _seen = await claim_message_receipt(_conn, msg_id)
                                await _conn.execute("UPDATE whatsapp_message_receipts SET phone_hash=$2 WHERE message_id=$1", msg_id, channel_phone_hash(sender_phone))
                            if not _seen:
                                print(f"[WhatsApp Dedup] Reenvio ignorado: {msg_id}")
                                continue
                        except Exception as _dedup_err:
                            print(f"[WhatsApp Dedup] Aviso: {msg_id}: {_dedup_err}")

                    wa_tid = trace_llm('whatsapp', 'request', user_text=user_text,
                                       msg_type=msg_type, phone_hash=channel_phone_hash(sender_phone))

                    print(f"[WhatsApp Webhook] Mensagem autorizada ({msg_type})")

                    # Reação humanizada e sutil: se a mensagem tiver tom carinhoso/amoroso/agradecimento, reage com coração verde 💚
                    if msg_id and is_warm_and_affectionate(user_text):
                        asyncio.create_task(send_whatsapp_reaction(sender_phone, msg_id, "💚"))

                    # 1. Blindagem contra Prompt Injection ou Tarefas Exaustivas (ex: contar até 100)
                    if is_prompt_injection(user_text):
                        refusal_text = (
                            "Não compartilho instruções internas nem altero minhas regras por mensagem. "
                            "Posso ajudar com sua saúde e rotina no Braga Saúde. 💚"
                        )
                        await _send_context_text(sender_phone, refusal_text)
                        continue

                    # 2. Identificação do Usuário no PostgreSQL
                    # Só vale o vínculo confirmado por TOTP (whatsapp_phone).
                    # O fallback legado por phone / emergency_contact_phone foi
                    # removido do canal de saúde no passo 1.2: telefone de
                    # contato e de emergência não autorizam uma conta clínica,
                    # e um número sem vínculo nunca é identidade.
                    # Ambiguidade (mais de uma conta no mesmo número) interrompe
                    # o fluxo e pede regularização — a identidade não vem da
                    # interpretação do modelo.
                    identity = {"status": STATUS_UNLINKED, "user_id": None, "full_name": None}
                    if db_pool:
                        try:
                            async with db_pool.acquire() as conn:
                                identity = await resolve_linked_account(conn, sender_phone)
                        except Exception as db_err:
                            print(f"[WhatsApp DB Lookup Error] {db_err}")
                            identity = {"status": STATUS_UNAVAILABLE, "user_id": None, "full_name": None}
                    verified_link = identity.get("status") == STATUS_OK
                    user_id = identity.get("user_id") if verified_link else None
                    user_name = first_name(identity.get("full_name")) if verified_link else None
                    if verified_link:
                        dialogue.scope((channel_phone_hash(sender_phone), str(user_id)))
                        dialogue.record('user', user_text)


                    # Passo 1.5: a identidade resolvida vira o contexto de
                    # operação (actor/patient/channel/message_id) que os
                    # serviços extraídos consomem. Sem vínculo -> None.
                    wa_ctx = context_from_identity(identity, channel="whatsapp", message_id=msg_id)

                    # 2.5. VÍNCULO TOTP (arquitetura temporária, sem template).
                    # Roda ANTES do portão de conta: é assim que um número sem conta
                    # conquista a dele. Se consumir, já respondeu e paramos por aqui.
                    if HAS_WA_LINK:
                        try:
                            _consumed = await handle_link_attempt(
                                sender_phone=sender_phone,
                                text=user_text,
                                db_pool=db_pool,
                                send_text=_send_context_text,
                            )
                            if _consumed:
                                continue
                        except Exception as _link_err:
                            print(f"[WhatsApp Link Error] {_link_err}")

                    # 2.6. PORTÃO DE CONTA VINCULADA: sem conta -> institucional,
                    # MAS a triagem de emergência (SAMU) permanece ativa antes dele.
                    # Ordem: vinculo -> emergência -> portão -> fluxo normal.
                    if HAS_WA_GATE:
                        try:
                            _blocked = await gate_unregistered(
                                user_text=user_text,
                                identity=identity,
                                send_text=_send_context_text,
                                sender_phone=sender_phone,
                                send_emergency=lambda phone: send_whatsapp_emergency(phone, send_text=send_whatsapp_text),
                            )
                            if _blocked:
                                continue
                        except Exception as _gate_err:
                            print(f"[WhatsApp Gate Error] {_gate_err}")

                    # Identidade vem do vínculo confirmado, não da memória do modelo.
                    identity_text = identity_reply(
                        user_text, verified_link=verified_link, full_name=user_name
                    )
                    if identity_text:
                        await _send_context_text(sender_phone, identity_text)
                        continue

                    if user_id:
                        user_text = _operation_followup(user_id, user_text)

                    wa_kind = detect_all(user_text).get("kind")
                    if wa_kind == "emergency":
                        await send_whatsapp_emergency(sender_phone, send_text=send_whatsapp_text)
                        continue
                    if wa_kind in {"date", "time"}:
                        from services.calendar_reply import calendar_reply
                        zone = None
                        if user_id and db_pool:
                            try:
                                async with db_pool.acquire() as conn:
                                    zone = await conn.fetchval("SELECT timezone FROM profiles WHERE id=$1", user_id)
                            except Exception:
                                pass
                        key = "".join(filter(str.isdigit, sender_phone or "")) or sender_phone
                        reply = calendar_reply(user_text, wa_kind, zone)
                        alternative = calendar_reply(user_text, wa_kind, zone, alternate=True) if wa_kind == "time" else None
                        await _send_context_text(sender_phone, dedupe_fala(_WA_LASTFALA, key, reply, alternative))
                        continue

                    if user_id and wa_kind == "family_list":
                        from services.family_target import active_relations_reply
                        async with db_pool.acquire() as conn:
                            result = await active_relations_reply(conn, wa_ctx or context_from_identity(identity, message_id=msg_id))
                        await _send_context_text(sender_phone, result.message)
                        continue

                    # 2.7. Respostas rápidas em texto
                    # (somente após portão de conta: anônimo já respondeu antes)
                    fast_intent = (match_fast_lane_intent(user_text)
                                   if wa_kind != "out_of_scope" and wa_kind != "model_identity"
                                   else None)
                    if fast_intent:
                        reply_text = FAST_LANE_INTENTS[fast_intent]["text"]

                        print(f"[WhatsApp Fast-Lane] Intent '{fast_intent}'")

                        # Registra no dataset de fine-tuning (higienizado por LGPD)
                        log_fine_tuning_sample(
                            user_prompt=user_text,
                            assistant_reply=reply_text,
                            source="whatsapp_voice_fastlane" if msg_type in ("audio", "voice") else "whatsapp_fastlane",
                            system_prompt="Resposta rápida padronizada Braga Saúde (Voz Antonio)",
                            user_name=user_name,
                            action="FAST_LANE",
                            action_params={"intent": fast_intent}
                        )

                        await _send_context_text(sender_phone, reply_text)
                        if "link_text" in FAST_LANE_INTENTS[fast_intent]:
                            await _send_context_text(sender_phone, FAST_LANE_INTENTS[fast_intent]["link_text"])
                        continue

                    # 2.8. Consultas de saúde (PR2, passo 2.3): o serviço
                    # responde ANTES da conversa aberta — sem LLM.
                    # PR4 (4.1/4.2): se a fala menciona outro pessoa, o alvo
                    # é o familiar do vínculo ativo (actor != patient), com
                    # permissão revalidada no próprio serviço.
                    if user_id and (wa_kind in _WA_QUERY_HANDLERS or wa_kind == "query_medication"):
                        _q_ctx = wa_ctx or context_from_identity(identity, message_id=msg_id)
                        _q_result = None
                        try:
                            async with db_pool.acquire() as _q_conn:
                                if _WA_FAMILY_RESOLVER is not None and _q_ctx is not None:
                                    _q_ctx, _fam_err = await _WA_FAMILY_RESOLVER(
                                        _q_conn, _q_ctx, user_text)
                                    if _fam_err is not None and _fam_err.message:
                                        _remember_operation(user_id, user_text, _fam_err, wa_kind)
                                        await _send_family_clarification(sender_phone, _fam_err)
                                        continue
                                if _q_ctx is not None:
                                    if wa_kind == "query_medication":
                                        from services.medication_queries import medication_take_history
                                        _q_result = await medication_take_history(
                                            _q_conn, _q_ctx, user_text)
                                    elif wa_kind == "query_bp" and re.search(r"\bhoje\b", user_text.lower()):
                                        _q_result = await blood_pressure_today(_q_conn, _q_ctx)
                                    else:
                                        _q_result = await _WA_QUERY_HANDLERS[wa_kind](_q_conn, _q_ctx)
                        except Exception as _q_err:
                            print(f"[WhatsApp Queries] Falha na consulta {wa_kind}: {_q_err}")
                            _q_result = None
                        if _q_result is not None and _q_result.message:
                            # Log sem o valor de saúde (LGPD): registra a
                            # intenção, não o dado clínico.
                            try:
                                log_fine_tuning_sample(
                                    user_prompt=user_text,
                                    assistant_reply="Consulta respondida pelo serviço de saúde.",
                                    source="whatsapp_query",
                                    system_prompt="health_queries (serviço, sem LLM)",
                                    user_name=user_name, action="CONSULTA",
                                    action_params={"kind": wa_kind, "result": _q_result.status,
                                                   "self_care": _q_ctx is not None and _q_ctx.is_self_care})
                            except Exception:
                                pass
                            _remember_operation(user_id, user_text, _q_result, wa_kind)
                            await _send_context_text(sender_phone, _q_result.message)
                            continue

                        await _send_context_text(sender_phone,
                            "Não consegui consultar seus registros agora. Tente novamente em instantes.")
                        continue

                    # 2.8b. Tomada de medicação (PR3, passo 3.3): identifica o
                    # remédio cadastrado e o horário programado, cria a
                    # pendência e envia os botões. A tomada só é registrada no
                    # Confirmar (passo 3.4), na mesma transação da pendência.
                    if user_id and wa_kind == "med_take":
                        _t_handled = await handle_wa_medication_take(
                            sender_phone=sender_phone, user_id=user_id,
                            user_name=user_name, msg_type=msg_type, db=db_pool,
                            user_text=user_text, context=wa_ctx,
                        )
                        if _t_handled:
                            continue

                    # 2.8c. Lembrete como mensagem de cuidado (PR4, passo
                    # 4.3): o "me lembra" vira proposta de mensagem com
                    # destinatário e texto, confirmada antes do envio. Não é
                    # evento de agenda: nada é agendado, só enviado após o
                    # clique (passo 4.4).
                    if user_id and wa_kind == "reminder":
                        _c_handled = await handle_wa_care_message(
                            sender_phone=sender_phone, user_id=user_id,
                            user_name=user_name, msg_type=msg_type, db=db_pool,
                            user_text=user_text, context=wa_ctx,
                        )
                        if _c_handled:
                            continue

                    # 2.8d. Preferências da conta (PR5, passos 5.1/5.3): o
                    # serviço reconhece, propõe e grava na mesma conta do
                    # app. A confirmação só vale para a proposta ativa (5.2).
                    if user_id:
                        _p_handled = await handle_wa_preference(
                            sender_phone=sender_phone, user_id=user_id,
                            user_name=user_name, msg_type=msg_type, db=db_pool,
                            user_text=user_text, context=wa_ctx,
                        )
                        if _p_handled:
                            continue

                    # 2.9. Camada 0 (Fase 4): router determinístico antes do LLM.
                    # Vitais com número vão direto aos botões; fofoca/social vai
                    # de template; só conversa aberta chega ao Groq.
                    wa_mem_key = "".join(filter(str.isdigit, sender_phone or "")) or sender_phone
                    wa_follow = resolve_followup(user_text, recall(_WA_MEMORY, wa_mem_key), channel="wa")
                    if wa_follow is not None:
                        _WA_MEMORY.pop(wa_mem_key, None)
                        wa_follow["fala"] = dedupe_fala(_WA_LASTFALA, wa_mem_key, wa_follow["fala"])
                        trace_llm('whatsapp', 'followup_hit', wa_tid, fala=wa_follow["fala"])
                        await _wa_send_router_text(sender_phone, msg_type, wa_follow["fala"], user_text, user_name)
                        continue
                    # Registros de terceiros precisam de executor próprio; nunca
                    # gravar na conta do interlocutor como se fossem autocuidado.
                    if wa_kind in ("bp", "glucose", "hydration", "hr", "spo2", "weight") and wa_ctx:
                        async with db_pool.acquire() as conn:
                            target, error = await _WA_FAMILY_RESOLVER(conn, wa_ctx, user_text)
                        if error or (target and not target.is_self_care):
                            await _send_context_text(sender_phone, error.message if error else
                                "Para registrar essa medição do familiar, use o aplicativo. "
                                "Aqui posso consultar os dados dele e registrar medicamentos com confirmação.")
                            continue
                    wa_route = whatsapp_next(user_text, bool(user_id), model_name=MODEL_WHATSAPP)
                    trace_llm('whatsapp', 'router_hit', wa_tid, route={k: (str(v)[:200]) for k, v in wa_route.items()})
                    if wa_route["type"] == "text":
                        if wa_kind == "app":
                            from services.wa_onboarding import linked_download_reply
                            wa_route["fala"] = linked_download_reply(sender_phone)
                        if wa_kind == "nutrition":
                            remember(_WA_MEMORY, wa_mem_key, "nutrition")
                        if wa_kind in SYMPTOM_KINDS:
                            if symptom_repeat(_WA_SYMPTOM, wa_mem_key, wa_kind) >= 2:
                                wa_route["fala"] += ESCALATION_SUFFIX
                        wa_route["fala"] = dedupe_fala(_WA_LASTFALA, wa_mem_key, wa_route["fala"])
                        await _wa_send_router_text(sender_phone, msg_type, wa_route["fala"], user_text, user_name)
                        continue
                    if wa_route["type"] == "vitals" and user_id:
                        _wa_action = _WA_METRIC_TO_ACTION.get(wa_route["metric"])
                        _wa_handled = await handle_wa_health_confirmation(
                            sender_phone=sender_phone, user_id=user_id, user_name=user_name,
                            msg_type=msg_type, db=db_pool, action_name=_wa_action,
                            action_params=wa_route["values"], reply_text=wa_route["fala"],
                            user_text=user_text, system_label="ORB router Fase 4 (vitals, sem LLM)",
                            context=wa_ctx
                        )
                        if _wa_handled:
                            continue
                        await _wa_send_router_text(
                            sender_phone, msg_type,
                            "Tive um problema técnico para preparar sua confirmação. Tente de novo em instantes.",
                            user_text, user_name)
                        continue

                    # Política compartilhada; dados do usuário não entram no
                    # system prompt. Passo 5.4: as preferências confirmadas
                    # da conta do INTERLOCUTOR são carregadas como o app faz
                    # (context_prompt), no mesmo conteúdo persistido.
                    _wa_prefs = {}
                    conv_prompt = build_conversation_system(
                        "whatsapp", preferences=_wa_prefs,
                        self_care=wa_ctx.is_self_care if wa_ctx else True, account_name=user_name)

                    # 4. Resposta com IA Qwen (Groq)
                    headers = {
                        "Authorization": f"Bearer {GROQ_API_KEY_WHATSAPP}",
                        "Content-Type": "application/json"
                    }
                    messages_payload = dialogue.messages(conv_prompt, user_text)
                    data = {
                        "model": MODEL_WHATSAPP,
                        "messages": messages_payload,
                        # A extensão é definida pela política, não cortando a geração.
                        "max_tokens": 500,
                        "temperature": 0.6
                    }
                    trace_llm('whatsapp', 'llm_request', wa_tid, model=MODEL_ID,
                              max_tokens=data["max_tokens"], temperature=0.6, messages=messages_payload,
                              context_messages=len(messages_payload) - 1)

                    async with _state.sem_wa:
                        async with httpx.AsyncClient(timeout=20.0) as client:
                            resp = await client.post(GROQ_API_URL, headers=headers, json=data)
                        if resp.status_code == 200:
                            r_json = resp.json()
                            choice = r_json["choices"][0]
                            reply_text, action_name, action_params = parse_conversation_reply(
                                choice["message"]["content"]
                            )
                            if reply_text == FALLBACK_REPLY or choice.get("finish_reason") == "length":
                                # Somente metadados: não expõe texto interno ou dados clínicos.
                                print("[WhatsApp LLM] Resposta vazia ou incompleta; tentando novamente uma vez")
                                async with httpx.AsyncClient(timeout=20.0) as client:
                                    reply_text, action_name, action_params = await retry_public_reply(
                                        client, GROQ_API_URL, headers, data)
                            trace_llm('whatsapp', 'llm_response', wa_tid, reply_text=reply_text,
                                      action=action_name, params=action_params)

                            # FLUXO DE CONFIRMAÇÃO (função compartilhada com o router, Fase 4)
                            _handled = await handle_wa_health_confirmation(
                                sender_phone=sender_phone, user_id=user_id, user_name=user_name,
                                msg_type=msg_type, db=db_pool, action_name=action_name,
                                action_params=action_params, reply_text=reply_text,
                                user_text=user_text, system_label=conv_prompt,
                                context=wa_ctx
                            )
                            if _handled:
                                return  # Não envia resposta de texto - aguarda confirmação

                            # Registra no dataset de fine-tuning (higienizado por LGPD)
                            log_fine_tuning_sample(
                                user_prompt=user_text,
                                assistant_reply=reply_text,
                                source="whatsapp_voice" if msg_type in ("audio", "voice") else "whatsapp",
                                system_prompt=conv_prompt,
                                user_name=user_name,
                                action=action_name,
                                action_params=action_params
                            )

                            # Extrai links da resposta usando Regex
                            links_to_send = re.findall(r'\[LINK:\s*(.*?)\]', reply_text)
                            # Higieniza profundamente o texto falado para a IA não soletrar expressões/caracteres
                            spoken_text = clean_text_for_tts(reply_text)

                            # Envia resposta por texto (com links formatados se houver)
                            if links_to_send:
                                await _send_context_text(sender_phone, f"{spoken_text}\n\n{links_to_send[0]}")
                            else:
                                await _send_context_text(sender_phone, spoken_text)
                        else:
                            await _send_context_text(
                                sender_phone,
                                "Olá! Recebi sua mensagem com carinho, mas meu sistema está processando muitas informações agora. Já já volto a te responder!"
                            )
    except Exception as e:
        print(f"[WhatsApp Webhook Error] Falha ao processar mensagem recebida: {e}")


# =============================================================
# CALLBACK DE CONFIRMAÇÃO (botões)
# =============================================================
async def _handle_authorized_confirmation_callback(button_id: str, sender_phone: str):
    """Processa cliques nos botões de confirmação."""
    db_pool = _state.db_pool
    try:
        # Extrair ação e código do button_id (formato: action_code)
        parts = button_id.split("_", 1)
        if len(parts) != 2 or not parts[1] or parts[0].lower() not in {"confirm", "edit", "cancel"}:
            await _send_context_text(sender_phone, "Não reconheci esta confirmação. Envie novamente o pedido.")
            return

        action, confirmation_code = parts
        action = action.lower()

        # Buscar pool de conexão
        if not db_pool:
            print("[Callback Error] db_pool não disponível")
            await _send_context_text(sender_phone, "Não consegui verificar a confirmação agora. Tente novamente em instantes.")
            return

        # AUD-G(M): a função expire_pending_confirmations() existia no banco
        # (migration 004:63-71) mas NINGUÉM a chamava — confirmações vencidas
        # ficavam com status='pending' para sempre, poluindo o índice parcial
        # idx_pending_conf_expires e o relatório v_confirmation_report.
        # Expira aqui (cada callback é o momento natural de limpeza).
        try:
            async with db_pool.acquire() as _c:
                await _c.execute("SELECT expire_pending_confirmations()")
        except Exception as _e:
            print(f"[Callback] Aviso: não expirou pendências vencidas: {_e}")

        # Buscar usuário pelo telefone: somente vínculo confirmado (TOTP).
        # Passo 1.2: fallback por phone / emergency_contact_phone removido —
        # confirmar um registro de saúde exige a conta vinculada a este número.
        user_id = None
        async with db_pool.acquire() as conn:
            _cb_identity = await resolve_linked_account(conn, sender_phone)
            if is_confirmed(_cb_identity):
                user_id = _cb_identity["user_id"]

        if not user_id:
            print("[Callback] Sem vínculo confirmado")
            await _send_context_text(
                sender_phone,
                "Não encontrei uma conta vinculada a este WhatsApp. "
                "Vincule seu número no aplicativo Braga Saúde ('Vincular WhatsApp') "
                "para confirmar registros."
            )
            return

        # O estado pertence ao ator resolvido. Repetição/expiração tem retorno
        # explícito e não executa uma ação nova.
        async with db_pool.acquire() as conn:
            proposal = await conn.fetchrow(
                """SELECT metric_type, status, expires_at > NOW() AS active
                   FROM pending_confirmations WHERE confirmation_code=$1 AND user_id=$2""",
                confirmation_code, user_id)
        if not proposal:
            await _send_context_text(sender_phone, "Não encontrei esta proposta na sua conta. Envie novamente o pedido.")
            return
        if proposal['status'] != 'pending' or not proposal['active']:
            labels = {'confirmed': 'já foi confirmada', 'cancelled': 'já foi cancelada', 'expired': 'expirou'}
            label = labels.get(proposal['status'], 'expirou')
            await _send_context_text(sender_phone, f"Esta proposta {label}. Nenhuma nova ação foi executada.")
            return

        # Processar ação
        if action == "confirm":
            # Passo 3.4: a tomada de medicamento tem fluxo próprio — o serviço
            # compartilhado grava na mesma transação do consumo da pendência e
            # devolve o resultado real (registrada / já registrada / erro).
            # Não passa pelo caminho de vitais: destino e efeitos são outros.
            _mt_metric = proposal['metric_type']
            if _mt_metric == "MED_TOMADA":
                try:
                    from services.medication_take import confirm_pending_medication_take
                    _mt_result = await confirm_pending_medication_take(
                        db_pool, confirmation_code=confirmation_code, actor_id=user_id)
                    await _send_context_text(
                        sender_phone,
                        _mt_result.message or "Não foi possível registrar a tomada agora.")
                except ImportError:  # pragma: no cover
                    await _send_context_text(
                        sender_phone,
                        "Não foi possível registrar a tomada agora. Tente novamente.")
                except Exception as _mt_err:
                    print(f"[Callback] Falha na tomada: {_mt_err}")
                    await _send_context_text(
                        sender_phone,
                        "Não foi possível registrar a tomada agora. Tente novamente em instantes.")
                return

            # Passo 4.4: mensagem de cuidado enviada após confirmação, pelo
            # contrato do app. O resultado real da entrega vira texto —
            # falha nunca é anunciada como "enviado".
            if _mt_metric == "CARE_MESSAGE":
                try:
                    from services.care_messages import confirm_pending_care_message
                    await _send_context_text(sender_phone, "Envio da mensagem de cuidado em andamento. Vou informar o resultado aqui.")
                    _cm_result = await confirm_pending_care_message(
                        db_pool, _state.send_care_message,
                        confirmation_code=confirmation_code, actor_id=user_id)
                    await _send_context_text(
                        sender_phone,
                        _cm_result.message or "Não foi possível enviar a mensagem agora.")
                except ImportError:  # pragma: no cover
                    await _send_context_text(
                        sender_phone,
                        "Não foi possível enviar a mensagem agora. Tente novamente.")
                except Exception as _cm_err:
                    print(f"[Callback] Falha na mensagem de cuidado: {_cm_err}")
                    await _send_context_text(
                        sender_phone,
                        "Não foi possível enviar a mensagem agora. Tente novamente em instantes.")
                return

            # PR5, passo 5.2: confirma a proposta de preferência ativa e grava
            # na mesma conta do app. Reenvio do botão não duplica.
            if _mt_metric == "PREFERENCE":
                await cancel_pending_confirmation(confirmation_code, user_id, db_pool)
                await _send_context_text(sender_phone,
                    "Esta proposta de preferência foi encerrada. Não guardo apelidos nem preferências neste assistente WhatsApp.")
                return

            success = await confirm_pending_confirmation(confirmation_code, user_id, db_pool)
            if success:
                # Buscar dados da confirmação para a mensagem final
                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow("""
                        SELECT metric_type, value_json, confirmed_at
                        FROM pending_confirmations
                        WHERE confirmation_code = $1
                    """, confirmation_code)

                    if row:
                        metric_type = row['metric_type']
                        values = json.loads(row['value_json'])

                        from core.confirmation import format_confirmation_value
                        from services.health_queries import patient_timezone, zone_of
                        from datetime import timezone
                        icon, value_display = format_confirmation_value(metric_type, values)
                        zone = zone_of(await patient_timezone(conn, user_id))
                        confirmed_at = row['confirmed_at'] or datetime.now(timezone.utc)
                        if confirmed_at.tzinfo is None:
                            confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
                        local_time = confirmed_at.astimezone(zone).strftime('%d/%m/%Y %H:%M')

                        await _send_context_text(sender_phone,
                            f"✅ *Registro confirmado por você!*\n\n"
                            f"📋 Código: `{confirmation_code}`\n"
                            f"{icon} {metric_type}: {value_display}\n"
                            f"🕐 Data: {local_time}\n\n"
                            f"Este registro foi autenticado por você e pode ser consultado no aplicativo."
                        )
            else:
                async with db_pool.acquire() as conn:
                    state = await conn.fetchval(
                        "SELECT status FROM pending_confirmations WHERE confirmation_code=$1 AND user_id=$2",
                        confirmation_code, user_id)
                if state == 'pending':
                    message = "Não consegui gravar o registro agora. A proposta continua pendente; tente confirmar novamente."
                else:
                    message = "Esta proposta já foi processada ou expirou. Consulte seus registros antes de enviar novamente."
                await _send_context_text(sender_phone, message)

        elif action == "edit":
            # Buscar tipo de métrica para orientar o usuário
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT metric_type FROM pending_confirmations
                    WHERE confirmation_code = $1 AND user_id = $2 AND status = 'pending'
                """, confirmation_code, user_id)

                if row:
                    metric_type = row['metric_type']
                    if not await cancel_pending_confirmation(confirmation_code, user_id, db_pool):
                        await _send_context_text(sender_phone, "Esta proposta já foi processada ou expirou. Envie novamente o pedido.")
                        return
                    await conn.execute("DELETE FROM assistant_preference_pending WHERE user_id=$1 AND proposal_id IN (SELECT COALESCE(value_json->>'proposal_id', confirmation_code) FROM pending_confirmations WHERE user_id=$1 AND confirmation_code=$2)", user_id, confirmation_code)

                    if metric_type == "PRESSAO":
                        await _send_context_text(sender_phone,
                            "✏️ *Edição de Pressão*\n\n"
                            "Por favor, envie novamente o valor correto.\n"
                            "Exemplo: *130 por 85*"
                        )
                    elif metric_type == "GLICEMIA":
                        await _send_context_text(sender_phone,
                            "✏️ *Edição de Glicose*\n\n"
                            "Por favor, envie novamente o valor correto.\n"
                            "Exemplo: *glicose 120*"
                        )
                    elif metric_type == "AGUA":
                        await _send_context_text(sender_phone,
                            "✏️ *Edição de Água*\n\n"
                            "Por favor, envie novamente a quantidade correta.\n"
                            "Exemplo: *bebi 300ml de água*"
                        )
                    elif metric_type == "PESO":
                        await _send_context_text(sender_phone,
                            "✏️ *Edição de Peso*\n\n"
                            "Por favor, envie novamente o valor correto.\n"
                            "Exemplo: *meu peso 75kg*"
                        )
                    elif metric_type == "MED_TOMADA":
                        # Passo 3.3: editar a tomada cancela a pendência —
                        # nenhum registro é feito. O usuário reenvia remédio e
                        # horário; o serviço re-identifica do cadastro.
                        await _send_context_text(sender_phone,
                            "✏️ *Edição de Tomada*\n\n"
                            "A tomada anterior foi descartada (nada foi registrado).\n"
                            "Envie novamente o remédio e o horário correto.\n"
                            "Exemplo: *tomei o remédio das 20h*"
                        )
                    else:
                        await _send_context_text(sender_phone,
                            "✏️ A proposta anterior foi cancelada. Envie novamente o pedido com os dados corretos.")
                else:
                    await _send_context_text(sender_phone,
                        "Esta proposta já foi processada ou expirou. Envie novamente o pedido.")

        elif action == "cancel":
            success = await cancel_pending_confirmation(confirmation_code, user_id, db_pool)
            if success:
                async with db_pool.acquire() as conn:
                    await conn.execute("DELETE FROM assistant_preference_pending WHERE user_id=$1 AND proposal_id IN (SELECT COALESCE(value_json->>'proposal_id', confirmation_code) FROM pending_confirmations WHERE user_id=$1 AND confirmation_code=$2)", user_id, confirmation_code)
                await _send_context_text(sender_phone,
                    "❌ *Registro cancelado.*\n\n"
                    "Seus dados não foram salvos.\n"
                    "Se precisar, pode enviar novamente a qualquer momento."
                )
            else:
                await _send_context_text(sender_phone,
                    "Não foi possível cancelar. O registro pode já ter sido processado."
                )

        else:
            print(f"[Callback] Ação desconhecida: {action}")

    except Exception as e:
        print(f"[Callback Error] Falha ao processar callback: {e}")
        await _send_context_text(sender_phone,
            "Desculpe, tive um problema ao processar sua solicitação.\n"
            "Por favor, tente novamente."
        )


# =============================================================
# ROTAS
# =============================================================
@router.get("/api/whatsapp/status")
def whatsapp_status():
    return {
        "status": "online" if HAS_WHATSAPP else "module_missing",
        "official_number": "+55 (11) 96780-8252",
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID if HAS_WHATSAPP else None,
        "active": HAS_WHATSAPP,
        # OTP por WhatsApp esta pausado (Meta nao aprova template); o gateway
        # so despacha OTP por e-mail ate a Meta liberar a conta business.
        "otp_channel_paused": WHATSAPP_OTP_PAUSED
    }


@router.post("/api/whatsapp/send")
async def send_whatsapp_direct(req: WhatsAppSendRequest, uid: str = Depends(firebase_uid)):
    if not HAS_WHATSAPP:
        raise HTTPException(status_code=503, detail="Módulo WhatsApp não disponível no servidor")
    if req.templateName:
        res = await send_whatsapp_template(req.phone, req.templateName, req.languageCode or "en_US")
    else:
        res = await _send_context_text(req.phone, req.message or "Olá! Mensagem do ecossistema Braga Saúde.")
    return res


@router.get("/api/whatsapp/webhook")
def verify_whatsapp_webhook(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token")
):
    """Validação inicial exigida pelo painel da Meta para ativar o Webhook."""
    VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
    if not VERIFY_TOKEN:
        raise HTTPException(status_code=500, detail="WHATSAPP_VERIFY_TOKEN não configurado")
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        print("[WhatsApp Webhook] Desafio de verificação validado com sucesso!")
        return PlainTextResponse(content=hub_challenge or "")
    raise HTTPException(status_code=403, detail="Token de verificação inválido")


async def process_whatsapp_webhook_payload(body: Dict[str, Any]):
    """Preserva a ordem de mensagens e confirmações dentro do lote da Meta."""
    for entry in body.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {})
            for msg in value.get('messages', []):
                interactive = msg.get('interactive') or {}
                reply_key = interactive.get('type')
                if msg.get('type') == 'interactive' and reply_key in {'button_reply', 'button'}:
                    button_id = (interactive.get(reply_key) or {}).get('id')
                    sender_phone = msg.get('from')
                    if button_id and sender_phone:
                        await handle_confirmation_callback(button_id=button_id, sender_phone=sender_phone, timestamp=msg.get('timestamp'))
                        continue
                single = {'entry': [{**entry, 'changes': [{**change, 'value': {**value, 'messages': [msg]}}]}]}
                await process_incoming_whatsapp_payload(single)


@router.post("/api/whatsapp/webhook")
async def receive_whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """Endpoint receptor de eventos e mensagens da Meta Cloud API."""
    # Zero-trust: sem a assinatura HMAC do App Secret, qualquer um forja
    # webhooks e se passa por qualquer remetente (confirmacoes de registros
    # de saude inclusas). payload bruto e necessario para o HMAC.
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256") or request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(signature, raw_body):
        print("[WhatsApp Webhook] Assinatura invalida ou ausente — requisicao rejeitada.")
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    try:
        body = json.loads(raw_body.decode("utf-8"))
        from services.wa_channel_storage import event_metadata, append_event
        event = {"ts": datetime.utcnow().isoformat(), "payload": event_metadata(body)}
        try:
            append_event(WEBHOOK_OUTBOX_PATH, event)
        except Exception:
            print("[WhatsApp Webhook] Falha ao gravar metadados do recebimento")
        WHATSAPP_EVENT_LOG.append(event)
        if len(WHATSAPP_EVENT_LOG) > 200:
            WHATSAPP_EVENT_LOG.pop(0)

    except Exception:
        return JSONResponse({"status": "invalid_payload"}, status_code=400)

    background_tasks.add_task(process_whatsapp_webhook_payload, body)
    return JSONResponse({"status": "received"}, status_code=200)


@router.get("/api/whatsapp/events")
def whatsapp_events(limit: int = Query(10, ge=1, le=50), uid: str = Depends(firebase_uid)):
    """Retorna os últimos eventos brutos recebidos pelo webhook do WhatsApp para diagnóstico.

    Payloads conteem telefones e corpos de mensagens (PII) DE TODOS OS USUARIOS:
    qualquer conta autenticada podia ler as mensagens dos outros. AUD-G09:
    agora exige flag de administrador no perfil.
    """
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(503, "Banco de dados não disponível")
    try:
        role = None
        with db_pool.acquire() as _conn:
            role = _conn.fetchval("SELECT user_role FROM profiles WHERE id = $1", uid)
    except Exception:
        pass
    if role != "ADMIN":
        raise HTTPException(403, "Acesso restrito a administradores.")
    return {
        "total_eventos_recebidos": len(WHATSAPP_EVENT_LOG),
        "eventos": list(reversed(WHATSAPP_EVENT_LOG[-limit:]))
    }


# =============================================================
# ESTADO (preenchido por server.py no startup)
# =============================================================
class _AppState:
    """Porta das dependencias do monolito: pool Postgres e
    semaforo de concorrencia do Groq (SEM_WA)."""

    db_pool = None
    sem_wa = None
    # Passo 4.4: sender do contrato de cuidado do app (care_automations),
    # injetado pelo server.py. Usado só no envio após confirmação.
    send_care_message = None


_state = _AppState()


def configure_state(*, db_pool=None, sem_wa=None, send_care_message=None):
    """Chamado por server.py ao subir a aplicacao."""
    _state.db_pool = db_pool
    _state.sem_wa = sem_wa
    _state.send_care_message = send_care_message


@router.get("/socorro", include_in_schema=False)
async def emergency_options_page():
    """Acesso público: não depende de login, vínculo, banco ou modelo."""
    from fastapi.responses import HTMLResponse
    from services.emergency_page import emergency_page
    return HTMLResponse(emergency_page(), headers={
        "Cache-Control": "public, max-age=300",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })


from contextlib import asynccontextmanager
from contextvars import ContextVar
_channel_locks = ContextVar('wa_channel_locks', default=frozenset())
from services.display_names import first_name
from services.wa_channel_privacy import phone_hash as channel_phone_hash, mark_pending as mark_channel_pending


@asynccontextmanager
async def _channel_serialized(phone):
    # Every conversation action and deletion share this lock. Business helpers
    # use their own connections/transactions, so use a separate key namespace.
    pool = _state.db_pool
    if not pool:
        yield
        return
    key = "pipeline:" + channel_phone_hash(phone)
    held = (key, asyncio.current_task())
    if held in _channel_locks.get():
        yield
        return
    loop = asyncio.get_running_loop()
    if getattr(_state, 'privacy_pool', None) is not pool or getattr(_state, 'privacy_loop', None) is not loop:
        getter = getattr(pool, 'get_max_size', None)
        size = getter() if callable(getter) else 3
        size = size if type(size) is int else 3
        # Reserve pool capacity for nested business transactions.
        _state.privacy_sem = asyncio.Semaphore(max(1, size // 3))
        _state.privacy_pool, _state.privacy_loop = pool, loop
    async with _state.privacy_sem:
        async with pool.acquire() as conn:
            await conn.execute('SELECT pg_advisory_lock(hashtextextended($1, 0))', key)
            token = _channel_locks.set(_channel_locks.get() | {held})
            try:
                yield
            finally:
                _channel_locks.reset(token)
                await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1, 0))', key)


async def _send_channel_prompt(phone, text, buttons):
    if buttons:
        result = await send_whatsapp_interactive(phone, text, buttons)
        if isinstance(result, dict) and result.get('success') is True:
            return
    if text:
        await _send_context_text(phone, text)


async def _channel_privacy_guard(phone, *, text='', button_id='', timestamp=None):
    """True = consumed. No media, clinical service, model or callback first."""
    from services.wa_channel_privacy import (
        consent_gate, deletion_request, prepare_deletion, resolve_deletion,
        DELETED_TEXT, UNAVAILABLE_TEXT)
    # Public rescue is the sole care exception to the consent gate.
    if text and detect_all(text).get('kind') == 'emergency':
        await send_whatsapp_emergency(phone, send_text=send_whatsapp_text)
        return True
    pool = _state.db_pool
    if not pool:
        await _send_context_text(phone, UNAVAILABLE_TEXT)
        return True
    cleanup_started = False
    try:
        from services.wa_channel_storage import finish_channel_cleanup
        async with pool.acquire() as conn:
            pending_cleanup = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM whatsapp_channel_cleanup WHERE phone_hash=$1)', channel_phone_hash(phone))
        if pending_cleanup is True:
            cleanup_started = True
            await finish_channel_cleanup(pool, channel_phone_hash(phone), outbox_path=WEBHOOK_OUTBOX_PATH)
            await _send_context_text(phone, DELETED_TEXT)
            return True
        result = await resolve_deletion(pool, phone, text=text, button_id=button_id, timestamp=timestamp)
        if isinstance(result, dict) and result.get('deleted'):
            cleanup_started = True
            dialogue.clear(channel_phone_hash(phone))
            from services.wa_onboarding import clear as clear_onboarding
            clear_onboarding(phone)
            _WA_OPERATION_MEMORY.pop(result.get('actor_id'), None)
            for memory in (_WA_MEMORY, _WA_LASTFALA, _WA_SYMPTOM):
                memory.pop(''.join(filter(str.isdigit, phone)), None)
            filtered = []
            for event in WHATSAPP_EVENT_LOG:
                items = [item for item in (event.get('payload') or {}).get('events', [])
                         if item.get('phone_hash') != channel_phone_hash(phone)]
                if items:
                    filtered.append({**event, 'payload': {'events': items, 'content_redacted': True}})
            WHATSAPP_EVENT_LOG[:] = filtered
            await finish_channel_cleanup(pool, channel_phone_hash(phone), outbox_path=WEBHOOK_OUTBOX_PATH)
            await _send_context_text(phone, result['message'])
            return True
        if result is not None:
            await _send_context_text(phone, result)
            return True
        if deletion_request(text):
            async with pool.acquire() as conn:
                identity = await resolve_linked_account(conn, phone)
            if identity.get('status') in (STATUS_UNAVAILABLE, 'ambiguous'):
                await _send_context_text(phone, UNAVAILABLE_TEXT)
                return True
            message, buttons = await prepare_deletion(pool, phone, identity.get('user_id'))
            await _send_channel_prompt(phone, message, buttons)
            return True
        blocked, message, buttons = await consent_gate(pool, phone, text=text,
            button_id=button_id, timestamp=timestamp)
        if blocked:
            dialogue.clear(channel_phone_hash(phone))
            await _send_channel_prompt(phone, message, buttons)
        return blocked
    except Exception:
        print('[WhatsApp Privacy] Operação indisponível')
        message = ('Seu número foi desvinculado. A limpeza dos dados do canal ainda está em andamento. '
                   'Sua conta e histórico no app foram preservados.' if cleanup_started else UNAVAILABLE_TEXT)
        await _send_context_text(phone, message)
        return True


async def process_incoming_whatsapp_payload(payload):
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value') or {}
            for message in value.get('messages', []):
                phone = message.get('from')
                if not phone:
                    continue
                async with _channel_serialized(phone):
                    text = (message.get('text') or {}).get('body', '') if message.get('type') == 'text' else ''
                    if await _channel_privacy_guard(phone, text=text, timestamp=message.get('timestamp')):
                        continue
                    single = {'entry': [{**entry, 'changes': [{**change, 'value': {**value, 'messages': [message]}}]}]}
                    token = dialogue.scope(None)
                    try:
                        await _process_authorized_whatsapp_payload(single)
                    finally:
                        dialogue.reset(token)


async def handle_confirmation_callback(button_id, sender_phone, timestamp=None):
    async with _channel_serialized(sender_phone):
        if await _channel_privacy_guard(sender_phone, button_id=button_id, timestamp=timestamp):
            return
        token = dialogue.scope(None)
        try:
            async with _state.db_pool.acquire() as conn:
                identity = await resolve_linked_account(conn, sender_phone)
            if identity.get('status') == STATUS_OK:
                dialogue.scope((channel_phone_hash(sender_phone), str(identity['user_id'])))
                action = button_id.split('_', 1)[0]
                if action in ('confirm', 'edit', 'cancel'):
                    dialogue.record('user', f'Ação no cartão: {action}.')
            await _handle_authorized_confirmation_callback(button_id, sender_phone)
        finally:
            dialogue.reset(token)
