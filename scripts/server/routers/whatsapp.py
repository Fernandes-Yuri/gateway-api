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
- fast-lane de audio engatilhado (FAST_LANE_*) + init_fast_lane_audio_cache
- synthesize_with_piper (ponte para o PiperService; todos os chamadores
  sao deste canal)

Seguranca preservada: verify_meta_signature no webhook (zero-trust),
match EXATO de telefone (AUD-G10), admin-only em /events (AUD-G09),
expira pendencias vencidas a cada callback, is_prompt_injection antes
do LLM e clean_text_for_tts antes de sintetizar.
"""
import asyncio
import hashlib
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
    AUDIO_CACHE_DIR,
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
from core.dataset import log_fine_tuning_sample
from core.text import clean_text_for_tts
from orb_router import (
    ESCALATION_SUFFIX,
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

router = APIRouter()


# =============================================================
# SERVICOS DEFENSIVOS (espelham o try/except do server.py)
# =============================================================
try:
    from whatsapp_service import (
        WHATSAPP_PHONE_NUMBER_ID,
        download_whatsapp_media,
        mark_whatsapp_message_read,
        send_whatsapp_audio,
        send_whatsapp_interactive,
        send_whatsapp_reaction,
        send_whatsapp_template,
        send_whatsapp_text,
        transcribe_audio_whisper,
        upload_whatsapp_media,
    )
    HAS_WHATSAPP = True
except ImportError:
    HAS_WHATSAPP = False
    WHATSAPP_PHONE_NUMBER_ID = None

    async def send_whatsapp_text(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    async def send_whatsapp_template(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    async def send_whatsapp_audio(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    async def send_whatsapp_reaction(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    async def mark_whatsapp_message_read(*args, **kwargs):  # type: ignore
        return {"success": False, "error": "whatsapp_service ausente"}

    async def download_whatsapp_media(*args, **kwargs):  # type: ignore
        return None

    async def upload_whatsapp_media(*args, **kwargs):  # type: ignore
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
# SÍNTESE DE VOZ (Piper) — todos os chamadores sao deste canal
# =============================================================
async def synthesize_with_piper(text: str, voice_name: str, output_path: str) -> bool:
    """Ponte para o PiperService. O endpoint HTTP /v1/audio/speech +
    /synthesize_voice vive em routers/audio.py (Fase A passo 3)."""
    if _state.piper_service is None:
        return False
    return await _state.piper_service.synthesize(text, voice_name, output_path)


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
# ÁUDIOS ENGATILHADOS (FAST-LANE PRE-CACHED RESPONSES)
# =============================================================
# Respostas padrao de voz humanizada pre-renderizadas (cache rapido).
# Garante resposta de audio em menos de 1.5s para saudacoes e duvidas
# frequentes, liberando o modelo Qwen 2.5 72B para focar nas respostas
# clinicas complexas.
FAST_LANE_AUDIO_INTENTS = {
    "ask_audio": {
        "text": "Com certeza! Eu adoro conversar por áudio. Você pode me mandar áudios sempre que preferir, e eu te respondo por áudio também! Como posso te ajudar hoje?",
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
        "link_text": "https://braga-saude.web.app/",
        "keywords": ["onde baixo", "como baixo", "link do app", "link do aplicativo", "baixar o app", "onde encontro o app", "mandar o link", "como baixar", "link para baixar", "onde baixar", "baixar aplicativo"]
    }
}

CLINICAL_BYPASS_KEYWORDS = [
    "dor", "pressao", "pressão", "glicemia", "falta de ar", "remedio", "remédio",
    "sintoma", "tontura", "febre", "enjoo", "enjôo", "vomito", "vômito", "peito",
    "sangue", "insulina", "samu", "192", "posto", "ubs", "upa", "hospital",
    "infarto", "avc", "queda", "machuquei", "ajuda medica", "ajuda médica", "receita"
]

FAST_LANE_CACHE: Dict[str, Dict[str, Any]] = {}


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
        for kw in FAST_LANE_AUDIO_INTENTS[intent_key]["keywords"]:
            if kw in lower:
                return intent_key
    return None


async def init_fast_lane_audio_cache():
    """Pré-sintetiza os áudios engatilhados e pré-carrega em memória e na Meta."""
    print("[Fast-Lane Audio] Inicializando cache de áudios rápidos...")
    for intent_key, data in FAST_LANE_AUDIO_INTENTS.items():
        audio_path = os.path.join(AUDIO_CACHE_DIR, f"fastlane_{intent_key}.wav")
        file_bytes = None
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            try:
                with open(audio_path, "rb") as f:
                    file_bytes = f.read()
            except Exception as e:
                print(f"[Fast-Lane Audio] Erro lendo cache em disco de {intent_key}: {e}")

        if not file_bytes:
            try:
                await synthesize_with_piper(data["text"], "faber", audio_path)
                with open(audio_path, "rb") as f:
                    file_bytes = f.read()
                print(f"[Fast-Lane Audio] Áudio gerado e salvo: {audio_path} ({len(file_bytes)} bytes)")
            except Exception as e:
                print(f"[Fast-Lane Audio] Erro ao sintetizar áudio de {intent_key}: {e}")

        if file_bytes:
            FAST_LANE_CACHE[intent_key] = {
                "text": data["text"],
                "bytes": file_bytes,
                "media_id": None
            }
            if HAS_WHATSAPP:
                try:
                    m_id = await upload_whatsapp_media(file_bytes, mime_type="audio/mpeg", filename=f"fastlane_{intent_key}.mp3")
                    # NOTE: os bytes sao WAV (Piper gera RIFF/WAVE). A extensao
                    # .mp3 no nome enviado a Meta e intencional por enquanto:
                    # audio/wav nao esta na lista oficial de tipos suportados
                    # para upload. Ver doc 06 (divida rebaixada a parcial).
                    if m_id:
                        FAST_LANE_CACHE[intent_key]["media_id"] = m_id
                        print(f"[Fast-Lane Audio] Meta media_id pronto para {intent_key}: {m_id}")
                except Exception as m_err:
                    print(f"[Fast-Lane Audio] Pré-upload Meta falhou para {intent_key}: {m_err}")


# =============================================================
# MEMÓRIA CURTA + MAPA MÉTRICA->AÇÃO
# =============================================================
# Memoria curta de clarificacao do WhatsApp (Fase 4): telefone -> kind.
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
    if msg_type in ("audio", "voice"):
        try:
            spoken = clean_text_for_tts(fala)
            wav_temp = f"/tmp/wa_router_{int(time.time()*1000)}.wav"
            if await synthesize_with_piper(spoken, "faber", wav_temp) and os.path.exists(wav_temp):
                with open(wav_temp, "rb") as f:
                    audio_bytes = f.read()
                try:
                    os.remove(wav_temp)
                except Exception:
                    pass
                if audio_bytes:
                    a_res = await send_whatsapp_audio(sender_phone, audio_bytes=bytes(audio_bytes))
                    if a_res.get("success"):
                        return
        except Exception as tts_err:
            print(f"[WhatsApp Router Audio Error] {tts_err}")
    await send_whatsapp_text(sender_phone, fala)


async def handle_wa_health_confirmation(*, sender_phone: str, user_id: str, user_name,
                                        msg_type: str, db, action_name: str,
                                        action_params: dict, reply_text: str,
                                        user_text: str, system_label: str) -> bool:
    """Fluxo de confirmação com botões (extraído do caminho LLM, Fase 4).

    Retorna True quando resolveu (valor inválido avisado ou botões
    enviados); False quando o chamador deve seguir o fluxo normal.
    """
    health_actions = ["REGISTRAR_PRESSAO", "REGISTRAR_GLICEMIA", "REGISTRAR_AGUA",
                      "REGISTRAR_PESO", "REGISTRAR_BATIMENTOS", "REGISTRAR_OXIGENACAO"]
    if action_name not in health_actions or not user_id:
        return False
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
        await send_whatsapp_text(sender_phone,
            f"⚠️ *Valor inválido:*\n{error_msg}\n\n"
            f"Por favor, envie novamente o valor correto."
        )
        return True
    confirmation_code = await create_pending_confirmation(
        user_id=user_id,
        metric_type=metric_type,
        value_json=validated_params,
        db_pool=db
    )
    if confirmation_code:
        await send_confirmation_buttons(
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


# =============================================================
# PIPELINE DE MENSAGEM ENTRANTE
# =============================================================
async def process_incoming_whatsapp_payload(payload: Dict[str, Any]):
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
                            print(f"[WhatsApp Audio] Processando áudio ID {media_id} de {sender_phone}...")
                            audio_bytes = await download_whatsapp_media(media_id)
                            if audio_bytes:
                                user_text = await transcribe_audio_whisper(audio_bytes, GROQ_API_KEY_WHATSAPP) or ""
                                print(f"[WhatsApp Audio] Transcrição concluída: '{user_text}'")
                        if not user_text:
                            await send_whatsapp_text(
                                sender_phone,
                                "Olá! Recebi o seu áudio, mas não consegui escutar nitidamente. Pode me mandar de novo ou escrever por texto? 💚"
                            )
                            continue

                    if not sender_phone or not user_text:
                        continue

                    wa_tid = trace_llm('whatsapp', 'request', user_text=user_text,
                                       msg_type=msg_type, phone_tail=(sender_phone or '')[-4:])

                    print(f"[WhatsApp Webhook] Mensagem recebida de {sender_phone} ({msg_type}): {user_text}")

                    # Reação humanizada e sutil: se a mensagem tiver tom carinhoso/amoroso/agradecimento, reage com coração verde 💚
                    if msg_id and is_warm_and_affectionate(user_text):
                        asyncio.create_task(send_whatsapp_reaction(sender_phone, msg_id, "💚"))

                    # 1. Blindagem contra Prompt Injection ou Tarefas Exaustivas (ex: contar até 100)
                    if is_prompt_injection(user_text):
                        refusal_text = (
                            "Ih, contar até aí vai demorar uma eternidade! 😅 "
                            "Meu foco por aqui é cuidar com todo carinho da sua saúde e bem-estar. "
                            "Me conta, como você está se sentindo hoje? 💚"
                        )
                        if msg_type in ("audio", "voice"):
                            audio_sent = False
                            try:
                                # Unificado no Piper (decisao 20/09/2026, doc 11 §3.1):
                                # edge_tts nunca foi importado neste arquivo -> o NameError
                                # era engolido aqui e a recusa caia sempre em texto.
                                spoken_refusal = clean_text_for_tts(refusal_text)
                                refusal_path = os.path.join(AUDIO_CACHE_DIR, f"refusal_{hashlib.md5(spoken_refusal.encode('utf-8')).hexdigest()}.wav")
                                if await synthesize_with_piper(spoken_refusal, "faber", refusal_path) and os.path.exists(refusal_path):
                                    with open(refusal_path, "rb") as f:
                                        audio_buf = bytearray(f.read())
                                    if audio_buf:
                                        a_res = await send_whatsapp_audio(sender_phone, audio_bytes=bytes(audio_buf))
                                        if a_res.get("success"):
                                            audio_sent = True
                            except Exception as e:
                                print(f"[Refusal Audio Error] {e}")
                            if not audio_sent:
                                await send_whatsapp_text(sender_phone, refusal_text)
                        else:
                            await send_whatsapp_text(sender_phone, refusal_text)
                        continue

                    # 2. Identificação do Usuário no PostgreSQL
                    # Primária: whatsapp_phone (vínculo verificado por TOTP, new).
                    # Fallback (transição): busca legada por número completo,
                    # mantida até as contas existentes migrarem para o vínculo novo.
                    #
                    # AUD-G10: o fallback comparava os ÚLTIMOS 8 dígitos contra
                    # phone/emergency_contact_phone com LIKE '%sufixo%' — o que
                    # casava QUALQUER aparição do sufixo em QUALQUER posição. Dois
                    # números diferentes que coincidem nos 8 finais (ou até
                    # continham o trecho) apontavam para o mesmo paciente, e um
                    # estranho podia se passar por usuário cadastrado. Agora a
                    # associação legada exige o número COMPLETO (mesma
                    # quantidade de dígitos), e logs avisam que o caminho
                    # ainda está em uso para que a migração seja concluída.
                    user_id = None
                    user_name = None
                    if db_pool:
                        try:
                            _wa_digits = "".join(filter(str.isdigit, sender_phone or ""))
                            async with db_pool.acquire() as conn:
                                p_row = await conn.fetchrow(
                                    "SELECT id, full_name, user_role FROM profiles "
                                    "WHERE regexp_replace(whatsapp_phone, '\\D', '', 'g') = $1",
                                    _wa_digits
                                )
                                if not p_row and _wa_digits:
                                    # AUD-G10: match EXATO do número completo
                                    # (igualdade de dígitos), nunca substring.
                                    p_row = await conn.fetchrow(
                                        "SELECT id, full_name, user_role FROM profiles "
                                        "WHERE regexp_replace(phone, '\\D', '', 'g') = $1",
                                        _wa_digits
                                    )
                                    if not p_row:
                                        p_row = await conn.fetchrow(
                                            "SELECT id, full_name, user_role FROM profiles "
                                            "WHERE regexp_replace(emergency_contact_phone, '\\D', '', 'g') = $1",
                                            _wa_digits
                                        )
                                    if p_row:
                                        print(
                                            f"[AUD-G10] Conta {p_row['id']} acessada pelo fallback "
                                            f"legado (phone completo) — número sem vínculo TOTP. "
                                            f"Migrar para whatsapp_link."
                                        )
                                if p_row:
                                    user_id = p_row["id"]
                                    user_name = p_row["full_name"]
                        except Exception as db_err:
                            print(f"[WhatsApp DB Lookup Error] {db_err}")

                    # 2.5. VÍNCULO TOTP (arquitetura temporária, sem template).
                    # Roda ANTES do portão de conta: é assim que um número sem conta
                    # conquista a dele. Se consumir, já respondeu e paramos por aqui.
                    if HAS_WA_LINK:
                        try:
                            _consumed = await handle_link_attempt(
                                sender_phone=sender_phone,
                                text=user_text,
                                db_pool=db_pool,
                                send_text=send_whatsapp_text,
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
                                user_id=user_id,
                                send_text=send_whatsapp_text,
                                sender_phone=sender_phone,
                            )
                            if _blocked:
                                continue
                        except Exception as _gate_err:
                            print(f"[WhatsApp Gate Error] {_gate_err}")

                    # 2.7. Fast-Lane de Resposta Rápida com Áudios Engatilhados
                    # (somente após portão de conta: anônimo já respondeu antes)
                    fast_intent = match_fast_lane_intent(user_text)
                    if fast_intent:
                        cached = FAST_LANE_CACHE.get(fast_intent)
                        if not cached or not cached.get("bytes"):
                            data = FAST_LANE_AUDIO_INTENTS[fast_intent]
                            text_hash = hashlib.md5(data["text"].encode("utf-8")).hexdigest()[:8]
                            audio_path = os.path.join(AUDIO_CACHE_DIR, f"fastlane_{fast_intent}_{text_hash}.mp3")
                            f_bytes = None
                            if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                                with open(audio_path, "rb") as f:
                                    f_bytes = f.read()
                            if not f_bytes:
                                try:
                                    # Unificado no Piper (decisao 20/09/2026, doc 11 §3.1):
                                    # antes usava edge_tts aqui, mas edge_tts nunca foi
                                    # importado neste arquivo -> NameError silencioso.
                                    # O Piper ja e a engine do fast-lane de startup
                                    # (init_fast_lane_audio_cache) e da voz em producao.
                                    ok = await synthesize_with_piper(data["text"], "faber", audio_path)
                                    if ok:
                                        with open(audio_path, "rb") as f:
                                            f_bytes = f.read()
                                except Exception as e:
                                    print(f"[Fast-Lane On-Demand Error] {e}")
                            cached = {"text": data["text"], "bytes": f_bytes, "media_id": None}
                            FAST_LANE_CACHE[fast_intent] = cached

                        reply_text = cached["text"]
                        audio_bytes = cached.get("bytes")
                        media_id = cached.get("media_id")

                        print(f"[WhatsApp Fast-Lane] Intent '{fast_intent}' engatilhado para {sender_phone}!")

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

                        if msg_type in ("audio", "voice"):
                            audio_res = await send_whatsapp_audio(
                                sender_phone,
                                audio_bytes=audio_bytes,
                                media_id=media_id
                            )
                            if audio_res.get("success"):
                                if audio_res.get("media_id"):
                                    cached["media_id"] = audio_res["media_id"]
                                # Send link if it exists
                                if "link_text" in FAST_LANE_AUDIO_INTENTS[fast_intent]:
                                    await send_whatsapp_text(sender_phone, FAST_LANE_AUDIO_INTENTS[fast_intent]["link_text"])
                                continue
                            # Fallback para texto se áudio falhar
                            await send_whatsapp_text(sender_phone, reply_text)
                            if "link_text" in FAST_LANE_AUDIO_INTENTS[fast_intent]:
                                await send_whatsapp_text(sender_phone, FAST_LANE_AUDIO_INTENTS[fast_intent]["link_text"])
                            continue
                        else:
                            await send_whatsapp_text(sender_phone, reply_text)
                            if "link_text" in FAST_LANE_AUDIO_INTENTS[fast_intent]:
                                await send_whatsapp_text(sender_phone, FAST_LANE_AUDIO_INTENTS[fast_intent]["link_text"])
                            continue

                    # 2.8. Camada 0 (Fase 4): router determinístico antes do LLM.
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
                    wa_kind = detect_all(user_text).get("kind")
                    wa_route = whatsapp_next(user_text, bool(user_id))
                    trace_llm('whatsapp', 'router_hit', wa_tid, route={k: (str(v)[:200]) for k, v in wa_route.items()})
                    if wa_route["type"] == "text":
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
                            user_text=user_text, system_label="ORB router Fase 4 (vitals, sem LLM)"
                        )
                        if _wa_handled:
                            continue
                        await _wa_send_router_text(
                            sender_phone, msg_type,
                            "Tive um problema técnico para preparar sua confirmação. Tente de novo em instantes.",
                            user_text, user_name)
                        continue

                    # Política compartilhada; dados do usuário não entram no system prompt.
                    conv_prompt = build_conversation_system("whatsapp")

                    # 4. Resposta com IA Qwen (Groq)
                    headers = {
                        "Authorization": f"Bearer {GROQ_API_KEY_WHATSAPP}",
                        "Content-Type": "application/json"
                    }
                    messages_payload = [
                        {"role": "system", "content": conv_prompt},
                        {"role": "user", "content": user_text}
                    ]
                    data = {
                        "model": MODEL_WHATSAPP,
                        "messages": messages_payload,
                        "max_tokens": 500,
                        "temperature": 0.6
                    }
                    trace_llm('whatsapp', 'llm_request', wa_tid, model=MODEL_ID,
                              max_tokens=500, temperature=0.6, messages=messages_payload)

                    async with _state.sem_wa:
                        async with httpx.AsyncClient(timeout=20.0) as client:
                            resp = await client.post(GROQ_API_URL, headers=headers, json=data)
                        if resp.status_code == 200:
                            r_json = resp.json()
                            reply_text = r_json["choices"][0]["message"]["content"]
                            action_name = "CONVERSA"
                            action_params = {}
                            try:
                                raw_c = reply_text.strip()
                                if "```" in raw_c:
                                    m_code = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw_c)
                                    if m_code:
                                        raw_c = m_code.group(1).strip()
                                s_idx = raw_c.find('{')
                                e_idx = raw_c.rfind('}')
                                if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
                                    raw_c = raw_c[s_idx:e_idx+1]
                                parsed = json.loads(raw_c)
                                reply_text = parsed.get("fala") or parsed.get("message") or reply_text
                                action_name = parsed.get("acao", "CONVERSA")
                                action_params = parsed.get("parametros", {})
                            except Exception:
                                pass
                            trace_llm('whatsapp', 'llm_response', wa_tid, reply_text=reply_text,
                                      action=action_name, params=action_params)

                            # FLUXO DE CONFIRMAÇÃO (função compartilhada com o router, Fase 4)
                            _handled = await handle_wa_health_confirmation(
                                sender_phone=sender_phone, user_id=user_id, user_name=user_name,
                                msg_type=msg_type, db=db_pool, action_name=action_name,
                                action_params=action_params, reply_text=reply_text,
                                user_text=user_text, system_label=conv_prompt
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

                            # REGRA CLÍNICA & ACESSIBILIDADE: Toda mensagem de áudio é respondida em ÁUDIO!
                            if msg_type in ("audio", "voice"):
                                audio_sent = False
                                try:
                                    print("[WhatsApp TTS] Sintetizando resposta de voz do Braga (Piper faber)...")
                                    wav_temp = f"/tmp/wa_reply_{int(time.time()*1000)}.wav"
                                    ok = await synthesize_with_piper(spoken_text, "faber", wav_temp)
                                    audio_buffer = bytearray()
                                    if ok and os.path.exists(wav_temp):
                                        with open(wav_temp, "rb") as f:
                                            audio_buffer = bytearray(f.read())
                                        try:
                                            os.remove(wav_temp)
                                        except Exception:
                                            pass

                                    if audio_buffer:
                                        print(f"[WhatsApp Audio Upload] Enviando áudio de {len(audio_buffer)} bytes...")
                                        audio_res = await send_whatsapp_audio(sender_phone, audio_bytes=bytes(audio_buffer))
                                        if audio_res.get("success"):
                                            audio_sent = True
                                            print("[WhatsApp Audio] Áudio do Braga entregue com sucesso!")
                                            for link in links_to_send:
                                                await send_whatsapp_text(sender_phone, link)
                                except Exception as tts_err:
                                    print(f"[WhatsApp Audio Synthesize/Send Error] {tts_err}")

                                # Se o áudio falhar por qualquer motivo, garante resposta por texto
                                if not audio_sent:
                                    if links_to_send:
                                        await send_whatsapp_text(sender_phone, f"{spoken_text}\n\n{links_to_send[0]}")
                                    else:
                                        await send_whatsapp_text(sender_phone, spoken_text)
                            else:
                                if links_to_send:
                                    await send_whatsapp_text(sender_phone, f"{spoken_text}\n\n{links_to_send[0]}")
                                else:
                                    await send_whatsapp_text(sender_phone, spoken_text)
                        else:
                            await send_whatsapp_text(
                                sender_phone,
                                "Olá! Recebi sua mensagem com carinho, mas meu sistema está processando muitas informações agora. Já já volto a te responder!"
                            )
    except Exception as e:
        print(f"[WhatsApp Webhook Error] Falha ao processar mensagem recebida: {e}")


# =============================================================
# CALLBACK DE CONFIRMAÇÃO (botões)
# =============================================================
async def handle_confirmation_callback(button_id: str, sender_phone: str):
    """Processa cliques nos botões de confirmação."""
    db_pool = _state.db_pool
    try:
        # Extrair ação e código do button_id (formato: action_code)
        parts = button_id.split("_", 1)
        if len(parts) != 2:
            return

        action, confirmation_code = parts
        action = action.lower()

        # Buscar pool de conexão
        if not db_pool:
            print("[Callback Error] db_pool não disponível")
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

        # Buscar usuário pelo telefone
        # AUD-G10: era LIKE '%' || $1 com os últimos 8 dígitos — correspondência
        # parcial que podia casar a conta errada. Agora exige o número completo.
        user_id = None
        _cb_digits = "".join(filter(str.isdigit, sender_phone or ""))
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id FROM profiles
                WHERE regexp_replace(whatsapp_phone, '\\D', '', 'g') = $1
                   OR regexp_replace(phone, '\\D', '', 'g') = $1
                LIMIT 1
            """, _cb_digits)
            if not row:
                row = await conn.fetchrow("""
                    SELECT id FROM profiles
                    WHERE regexp_replace(emergency_contact_phone, '\\D', '', 'g') = $1
                    LIMIT 1
                """, _cb_digits)
            if row:
                user_id = row['id']

        if not user_id:
            print(f"[Callback] Usuário não encontrado para telefone: {sender_phone}")
            await send_whatsapp_text(sender_phone, "Usuário não encontrado. Verifique seu cadastro.")
            return

        # Processar ação
        if action == "confirm":
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

                        # Formatar mensagem de sucesso
                        if metric_type == "PRESSAO":
                            value_display = f"{values.get('sistolica')} x {values.get('diastolica')} mmHg"
                        elif metric_type == "GLICEMIA":
                            value_display = f"{values.get('glicemia')} mg/dL"
                        elif metric_type == "AGUA":
                            value_display = f"{values.get('quantidade_ml')} ml"
                        elif metric_type == "PESO":
                            value_display = f"{values.get('peso_kg')} kg"
                        else:
                            value_display = str(values)

                        await send_whatsapp_text(sender_phone,
                            f"✅ *Registro confirmado por você!*\n\n"
                            f"📋 Código: `{confirmation_code}`\n"
                            f"🩺 {metric_type}: {value_display}\n"
                            f"🕐 Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                            f"Este registro foi autenticado por você e pode ser consultado no aplicativo."
                        )
            else:
                await send_whatsapp_text(sender_phone,
                    "❌ Não foi possível confirmar. O registro pode ter expirado.\n"
                    "Por favor, envie os dados novamente."
                )

        elif action == "edit":
            # Buscar tipo de métrica para orientar o usuário
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT metric_type FROM pending_confirmations
                    WHERE confirmation_code = $1 AND status = 'pending'
                """, confirmation_code)

                if row:
                    metric_type = row['metric_type']
                    await cancel_pending_confirmation(confirmation_code, user_id, db_pool)

                    if metric_type == "PRESSAO":
                        await send_whatsapp_text(sender_phone,
                            "✏️ *Edição de Pressão*\n\n"
                            "Por favor, envie novamente o valor correto.\n"
                            "Exemplo: *130 por 85*"
                        )
                    elif metric_type == "GLICEMIA":
                        await send_whatsapp_text(sender_phone,
                            "✏️ *Edição de Glicose*\n\n"
                            "Por favor, envie novamente o valor correto.\n"
                            "Exemplo: *glicose 120*"
                        )
                    elif metric_type == "AGUA":
                        await send_whatsapp_text(sender_phone,
                            "✏️ *Edição de Água*\n\n"
                            "Por favor, envie novamente a quantidade correta.\n"
                            "Exemplo: *bebi 300ml de água*"
                        )
                    elif metric_type == "PESO":
                        await send_whatsapp_text(sender_phone,
                            "✏️ *Edição de Peso*\n\n"
                            "Por favor, envie novamente o valor correto.\n"
                            "Exemplo: *meu peso 75kg*"
                        )

        elif action == "cancel":
            success = await cancel_pending_confirmation(confirmation_code, user_id, db_pool)
            if success:
                await send_whatsapp_text(sender_phone,
                    "❌ *Registro cancelado.*\n\n"
                    "Seus dados não foram salvos.\n"
                    "Se precisar, pode enviar novamente a qualquer momento."
                )
            else:
                await send_whatsapp_text(sender_phone,
                    "Não foi possível cancelar. O registro pode já ter sido processado."
                )

        else:
            print(f"[Callback] Ação desconhecida: {action}")

    except Exception as e:
        print(f"[Callback Error] Falha ao processar callback: {e}")
        await send_whatsapp_text(sender_phone,
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
        res = await send_whatsapp_text(req.phone, req.message or "Olá! Mensagem do ecossistema Braga Saúde.")
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
        # AUD-G(M): persiste o evento BRUTO no outbox ANTES de ackar. Se o
        # processo cair entre este ponto e o retorno 200, a mensagem se perde
        # (a Meta não reenvia quem ackou). Falha aqui é só warning — melhor
        # processar sem registro que perder a mensagem inteira.
        try:
            with open(WEBHOOK_OUTBOX_PATH, "a", encoding="utf-8") as _f:
                _f.write(json.dumps({
                    "ts": datetime.utcnow().isoformat(),
                    "payload": body,
                }, ensure_ascii=False) + "\n")
        except Exception as _e:
            print(f"[WhatsApp Webhook] Aviso: não gravou no outbox: {_e}")

        WHATSAPP_EVENT_LOG.append({
            "timestamp": datetime.utcnow().isoformat(),
            "payload": body
        })
        if len(WHATSAPP_EVENT_LOG) > 200:
            WHATSAPP_EVENT_LOG.pop(0)

        # Detectar cliques em botões interativos (confirmação)
        if "entry" in body:
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    for msg in messages:
                        if msg.get("type") == "interactive":
                            interactive = msg.get("interactive", {})
                            if interactive.get("type") == "button":
                                button_id = interactive.get("button", {}).get("id", "")
                                sender_phone = msg.get("from", "")
                                if button_id and sender_phone:
                                    background_tasks.add_task(
                                        handle_confirmation_callback,
                                        button_id=button_id,
                                        sender_phone=sender_phone
                                    )
                                    return JSONResponse({"status": "callback_received"}, status_code=200)
    except Exception:
        return JSONResponse({"status": "invalid_payload"}, status_code=400)

    background_tasks.add_task(process_incoming_whatsapp_payload, body)
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
    """Porta das dependencias do monolito: pool Postgres, servico Piper e
    semaforo de concorrencia do Groq (SEM_WA)."""

    db_pool = None
    piper_service = None
    sem_wa = None


_state = _AppState()


def configure_state(*, db_pool=None, piper_service=None, sem_wa=None):
    """Chamado por server.py ao subir a aplicacao."""
    _state.db_pool = db_pool
    _state.piper_service = piper_service
    _state.sem_wa = sem_wa
