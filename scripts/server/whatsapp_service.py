"""
Módulo de Integração com WhatsApp Business Cloud API - Braga Saúde
Gerencia o envio de alertas clínicos, notificações de medicamentos e mensagens conversacionais.
"""

import os
import sys
import json
import asyncio
from typing import Optional, Dict, Any
import httpx

# Credenciais Oficiais Meta WhatsApp Cloud API
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0")
WHATSAPP_API_URL = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"


def sanitize_phone_number(phone: str) -> str:
    """Remove caracteres especiais e garante DDI 55 para o Brasil se omitido."""
    cleaned = "".join(filter(str.isdigit, phone))
    if cleaned.startswith("0"):
        cleaned = cleaned[1:]
    # Se o usuário digitou DDD + número (ex: 11999998888 -> 11 dígitos), prefixar com 55
    if len(cleaned) in (10, 11) and not cleaned.startswith("55"):
        cleaned = "55" + cleaned
    return cleaned


async def send_whatsapp_template(
    to_number: str,
    template_name: str = "3p_direct_integration_test_template",
    language_code: str = "en_US"
) -> Dict[str, Any]:
    """
    Envia uma mensagem baseada em template aprovado pela Meta.
    Necessário para iniciar conversas fora da janela de 24h.
    """
    cleaned_number = sanitize_phone_number(to_number)
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": cleaned_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code}
        }
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
        try:
            res_json = response.json()
        except Exception:
            res_json = {"raw": response.text}

        print(f"[WhatsApp Template] Status: {response.status_code} | Resposta: {res_json}")
        return {
            "success": response.status_code in (200, 201),
            "status_code": response.status_code,
            "response": res_json
        }


async def send_whatsapp_text(
    to_number: str,
    text: str,
    preview_url: bool = False
) -> Dict[str, Any]:
    """
    Envia uma mensagem de texto simples.
    Válido dentro da janela de 24h aberta pelo usuário.
    """
    cleaned_number = sanitize_phone_number(to_number)
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": cleaned_number,
        "type": "text",
        "text": {
            "preview_url": preview_url,
            "body": text
        }
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
        try:
            res_json = response.json()
        except Exception:
            res_json = {"raw": response.text}

        print(f"[WhatsApp Text] Status: {response.status_code} | Resposta: {res_json}")
        return {
            "success": response.status_code in (200, 201),
            "status_code": response.status_code,
            "response": res_json
        }


async def mark_whatsapp_message_read(message_id: str) -> bool:
    """
    Envia confirmação de leitura (dois risquinhos azuis) para a Meta.
    Sem essa chamada, a mensagem nunca aparece como visualizada no WhatsApp do usuário.
    """
    if not message_id:
        return False
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
            print(f"[WhatsApp Mark Read] Msg ID: {message_id} | Status: {resp.status_code}")
            return resp.status_code in (200, 201)
    except Exception as e:
        print(f"[WhatsApp Mark Read Error] {e}")
        return False


async def send_whatsapp_reaction(to_number: str, message_id: str, emoji: str = "🎙️") -> bool:
    """Envia uma reação de emoji em uma mensagem do WhatsApp (ex: 🎙️ gravando áudio, ✍️ digitando)."""
    if not message_id:
        return False
    cleaned_number = sanitize_phone_number(to_number)
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": cleaned_number,
        "type": "reaction",
        "reaction": {
            "message_id": message_id,
            "emoji": emoji
        }
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
            return resp.status_code in (200, 201)
    except Exception as e:
        print(f"[WhatsApp Reaction Error] {e}")
        return False


async def upload_whatsapp_media(file_bytes: bytes, mime_type: str = "audio/mpeg", filename: str = "audio.mp3") -> Optional[str]:
    """Faz upload de mídia binária para a Meta e retorna o media_id."""
    upload_url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    files = {"file": (filename, file_bytes, mime_type)}
    data = {"messaging_product": "whatsapp", "type": mime_type}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(upload_url, headers=headers, data=data, files=files)
            if resp.status_code in (200, 201):
                m_id = resp.json().get("id")
                print(f"[WhatsApp Media Upload OK] ID: {m_id}")
                return m_id
            print(f"[WhatsApp Media Upload Fail] Status: {resp.status_code} | {resp.text}")
            return None
    except Exception as e:
        print(f"[WhatsApp Media Upload Exception] {e}")
        return None


async def send_whatsapp_audio(
    to_number: str,
    audio_bytes: Optional[bytes] = None,
    media_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Envia uma mensagem de áudio (nota de voz do Braga) para o WhatsApp.
    Permite envio ultra-rápido (<300ms) reutilizando um media_id pré-carregado.
    Se o media_id não for fornecido ou tiver expirado, faz o upload dos audio_bytes.
    """
    cleaned_number = sanitize_phone_number(to_number)
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    used_media_id = media_id
    if not used_media_id:
        if not audio_bytes:
            return {"success": False, "error": "nenhum_audio_fornecido"}
        used_media_id = await upload_whatsapp_media(audio_bytes, mime_type="audio/mpeg", filename="resposta_braga.mp3")
        if not used_media_id:
            return {"success": False, "error": "upload_failed"}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": cleaned_number,
        "type": "audio",
        "audio": {"id": used_media_id}
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
            # Se o media_id expirou (ex: erro 400 da Meta) e temos os bytes, tenta upload imediato e reenvio
            if resp.status_code not in (200, 201) and media_id and audio_bytes:
                print(f"[WhatsApp Audio] media_id {media_id} rejeitado pela Meta (HTTP {resp.status_code}). Renovando mídia...")
                new_media_id = await upload_whatsapp_media(audio_bytes, mime_type="audio/mpeg", filename="resposta_braga.mp3")
                if new_media_id:
                    payload["audio"]["id"] = new_media_id
                    used_media_id = new_media_id
                    resp = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)

            r_json = resp.json() if resp.status_code in (200, 201) else {"raw": resp.text}
            print(f"[WhatsApp Send Audio] Status: {resp.status_code} | Media ID: {used_media_id} | {r_json}")
            return {
                "success": resp.status_code in (200, 201),
                "media_id": used_media_id,
                "response": r_json
            }
    except Exception as e:
        print(f"[WhatsApp Send Audio Exception] {e}")
        return {"success": False, "error": str(e)}


async def send_clinical_alert_whatsapp(
    to_number: str,
    patient_name: str,
    alert_type: str,
    metric_value: str
) -> Dict[str, Any]:
    """Dispara um alerta de saúde prioritário para o cuidador/familiar via WhatsApp."""
    msg = (
        f"🚨 *ALERTA BRAGA SAÚDE*\n\n"
        f"Atenção: Foi registrado um valor crítico para *{patient_name}*:\n"
        f"• *Parâmetro:* {alert_type}\n"
        f"• *Valor:* {metric_value}\n\n"
        f"Por favor, verifique como o familiar está se sentindo e, se necessário, consulte a equipe de saúde de referência."
    )
    # Tenta enviar texto direto
    result = await send_whatsapp_text(to_number, msg)
    return result


async def send_security_code_whatsapp(
    to_number: str,
    code: str,
    purpose: str = "EMAIL_VERIFICATION"
) -> Dict[str, Any]:
    """Envia código OTP formatado de 6 dígitos via WhatsApp."""
    is_recovery = purpose == "PASSWORD_RESET"
    title = "Recuperação de Senha" if is_recovery else "Confirmação de Conta"
    msg = (
        f"🔐 *Braga Saúde - {title}*\n\n"
        f"Olá! O seu código de segurança é:\n\n"
        f"👉 *{code}*\n\n"
        f"⏱️ Válido por *15 minutos*.\n"
        f"🔒 Por segurança, nunca compartilhe este código com ninguém. Se você não solicitou, ignore esta mensagem."
    )
    return await send_whatsapp_text(to_number, msg)


async def download_whatsapp_media(media_id: str) -> Optional[bytes]:
    """Obtém a URL temporária da mídia na Meta e faz o download dos bytes (ex: áudio OGG Opus)."""
    media_meta_url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            meta_resp = await client.get(media_meta_url, headers=headers)
            if meta_resp.status_code != 200:
                print(f"[WhatsApp Media Meta Error] {meta_resp.status_code} {meta_resp.text}")
                return None
            download_url = meta_resp.json().get("url")
            if not download_url:
                return None

            # Download dos bytes reais da mídia
            media_resp = await client.get(download_url, headers=headers)
            if media_resp.status_code == 200:
                return media_resp.content
            else:
                print(f"[WhatsApp Media Download Error] {media_resp.status_code}")
                return None
    except Exception as e:
        print(f"[WhatsApp Media Exception] {e}")
        return None


async def transcribe_audio_whisper(audio_bytes: bytes, groq_api_key: str) -> Optional[str]:
    """Transcreve áudio OGG/WAV/MP3 para texto em português usando OpenAI Whisper via Groq API (ultra-rápido)."""
    if not groq_api_key:
        print("[Whisper Error] Chave da Groq não configurada")
        return None
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {groq_api_key}"
    }
    files = {
        "file": ("audio.ogg", audio_bytes, "audio/ogg")
    }
    data = {
        "model": "whisper-large-v3-turbo",
        "language": "pt"
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers, files=files, data=data)
            if resp.status_code == 200:
                result = resp.json()
                text = result.get("text", "").strip()
                if text:
                    print(f"[Whisper Groq Transcrição] '{text}'")
                    return text
            else:
                print(f"[Whisper Groq Error {resp.status_code}] {resp.text}")
    except Exception as e:
        print(f"[Whisper Groq Exception] {e}")
    return None


async def send_whatsapp_interactive(
    to_number: str,
    body_text: str,
    buttons: list,
    header_text: Optional[str] = None,
    footer_text: Optional[str] = None
) -> Dict[str, Any]:
    """
    Envia mensagem interativa com botões (máximo 3).
    
    Args:
        to_number: Número do destinatário
        body_text: Texto principal da mensagem
        buttons: Lista de dicts com 'id' e 'title' (máximo 3 botões)
        header_text: Texto do cabeçalho (opcional)
        footer_text: Texto do rodapé (opcional)
    
    Returns:
        Dict com resultado do envio
    """
    cleaned_number = sanitize_phone_number(to_number)
    
    # Validar número de botões (máximo 3)
    if len(buttons) > 3:
        return {"success": False, "error": "max_3_buttons"}
    
    # Construir botões
    action_buttons = []
    for btn in buttons:
        action_buttons.append({
            "type": "reply",
            "reply": {
                "id": btn["id"],
                "title": btn["title"][:20]  # Meta limita a 20 caracteres
            }
        })
    
    # Construir payload
    interactive = {
        "type": "button",
        "body": {
            "text": body_text
        },
        "action": {
            "buttons": action_buttons
        }
    }
    
    # Adicionar header se fornecido
    if header_text:
        interactive["header"] = {
            "type": "text",
            "text": header_text
        }
    
    # Adicionar footer se fornecido
    if footer_text:
        interactive["footer"] = {
            "text": footer_text
        }
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": cleaned_number,
        "type": "interactive",
        "interactive": interactive
    }
    
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
            r_json = resp.json() if resp.status_code in (200, 201) else {"raw": resp.text}
            print(f"[WhatsApp Interactive] Status: {resp.status_code} | Buttons: {len(buttons)} | {r_json}")
            return {
                "success": resp.status_code in (200, 201),
                "response": r_json
            }
    except Exception as e:
        print(f"[WhatsApp Interactive Error] {e}")
        return {"success": False, "error": str(e)}


async def send_whatsapp_list(
    to_number: str,
    title: str,
    body_text: str,
    button_text: str,
    sections: list
) -> Dict[str, Any]:
    """
    Envia mensagem com lista de opções.
    
    Args:
        to_number: Número do destinatário
        title: Título da lista
        body_text: Texto descritivo
        button_text: Texto do botão que abre a lista
        sections: Lista de seções com opções
    """
    cleaned_number = sanitize_phone_number(to_number)
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": cleaned_number,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": body_text
            },
            "action": {
                "button": button_text[:20],
                "sections": sections
            }
        }
    }
    
    if title:
        payload["interactive"]["header"] = {
            "type": "text",
            "text": title
        }
    
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(WHATSAPP_API_URL, headers=headers, json=payload)
            r_json = resp.json() if resp.status_code in (200, 201) else {"raw": resp.text}
            print(f"[WhatsApp List] Status: {resp.status_code} | {r_json}")
            return {
                "success": resp.status_code in (200, 201),
                "response": r_json
            }
    except Exception as e:
        print(f"[WhatsApp List Error] {e}")
        return {"success": False, "error": str(e)}


# Número oficial que recebe os pedidos de código (plano B, sem template).
WHATSAPP_BUSINESS_NUMBER = os.getenv("WHATSAPP_BUSINESS_NUMBER", "5511967808252")

OTP_REQUEST_RE = None

def _otp_request_re():
    global OTP_REQUEST_RE
    if OTP_REQUEST_RE is None:
        import re as _re
        OTP_REQUEST_RE = _re.compile(
            r"(quero meu codigo|meu codigo|codigo de verificacao|manda(r)? (o |meu )?codigo|"
            r"envia(r)? (o |meu )?codigo|preciso do codigo|code|otp)",
            _re.IGNORECASE,
        )
    return OTP_REQUEST_RE


def normalize_digits(text: str) -> str:
    """Só dígitos (para comparar telefones digitados com os da Meta)."""
    return "".join(filter(str.isdigit, text or ""))


def phones_match(a: str, b: str) -> bool:
    """Igualdade por sufixo: cobre +55/DDD/máscara dos dois lados."""
    da, db = normalize_digits(a), normalize_digits(b)
    if not da or not db:
        return False
    if da == db:
        return True
    tail = min(len(da), len(db), 11)
    return da[-tail:] == db[-tail:] and tail >= 8


def is_otp_request(text: str) -> bool:
    """Pedido explícito de código (plano B). Fora disso, fluxo normal."""
    if not text or len(text) > 60:
        return False
    import unicodedata as _ud
    _norm = "".join(
        c for c in _ud.normalize("NFD", text.lower()) if _ud.category(c) != "Mn")
    return bool(_otp_request_re().search(_norm))


def build_wa_link(prefilled: str = "Quero meu código Braga Saúde") -> str:
    """Link wa.me que abre a conversa (e a janela de 24h) com texto pronto."""
    from urllib.parse import quote as _quote
    biz = normalize_digits(WHATSAPP_BUSINESS_NUMBER) or "5511967808252"
    return f"https://wa.me/{biz}?text={_quote(prefilled)}"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python whatsapp_service.py <NUMERO_DESTINO> [TIPO: template|text] [MENSAGEM]")
        sys.exit(1)

    target_phone = sys.argv[1]
    msg_type = sys.argv[2] if len(sys.argv) > 2 else "template"
    custom_text = sys.argv[3] if len(sys.argv) > 3 else "Olá! Teste de integração do ecossistema Braga Saúde."

    if msg_type == "template":
        print(f">>> Disparando template de teste para: {target_phone}...")
        res = asyncio.run(send_whatsapp_template(target_phone))
    else:
        print(f">>> Disparando mensagem de texto para: {target_phone}...")
        res = asyncio.run(send_whatsapp_text(target_phone, custom_text))

    print(json.dumps(res, indent=2, ensure_ascii=False))
