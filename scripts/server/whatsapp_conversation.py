"""Resposta pública do WhatsApp e identidade da conta com vínculo confirmado."""
import json
import re
import unicodedata

FALLBACK_REPLY = 'Não consegui preparar a resposta agora. Pode tentar novamente?'
_THINK_TAG = re.compile(r'<\s*(/?)\s*think\s*>', re.IGNORECASE)


def public_reply(text):
    """Remove blocos internos, inclusive aninhados ou cortados pelo limite de tokens."""
    if not isinstance(text, str):
        return ''
    parts, start, depth = [], 0, 0
    for match in _THINK_TAG.finditer(text):
        if match.group(1):
            if depth:
                depth -= 1
            else:
                # Um fechamento sem abertura pode ser um prefixo truncado.
                parts.clear()
            start = match.end()
        else:
            if not depth:
                parts.append(text[start:match.start()])
            depth += 1
            start = match.end()
    if depth:
        return ''
    parts.append(text[start:])
    return ''.join(parts).strip()


def parse_conversation_reply(content):
    """Higieniza antes de interpretar ações e novamente dentro do envelope JSON."""
    text = public_reply(content)
    action, params = 'CONVERSA', {}
    raw = text
    code = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
    if code:
        raw = code.group(1).strip()
    start, end = raw.find('{'), raw.rfind('}')
    if 0 <= start < end:
        raw = raw[start:end + 1]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            text = public_reply(parsed.get('fala') or parsed.get('message'))
            if text:
                action = parsed.get('acao', 'CONVERSA')
                params = parsed.get('parametros', {})
    except (ValueError, TypeError):
        pass
    if not text:
        return FALLBACK_REPLY, 'CONVERSA', {}
    return text, action, params


def identity_reply(question, *, verified_link, full_name):
    """Só revela o nome quando o próprio número tem vínculo confirmado."""
    normalized = ''.join(c for c in unicodedata.normalize('NFKD', question.casefold())
                         if not unicodedata.combining(c))
    normalized = re.sub(r'[^a-z0-9 ]', '', normalized)
    normalized = ' '.join(normalized.split())
    if normalized not in ('quem sou eu', 'qual meu nome', 'qual e meu nome',
                          'voce sabe meu nome', 'como eu me chamo'):
        return None
    if not verified_link:
        return 'Para confirmar sua identidade, vincule seu WhatsApp à sua conta pelo aplicativo Braga Saúde.'
    if isinstance(full_name, str) and full_name.strip():
        return f'Você é {full_name.split()[0]}. Seu WhatsApp está vinculado à sua conta do Braga Saúde.'
    return ('Seu WhatsApp está vinculado à sua conta do Braga Saúde, mas não encontrei '
            'seu nome no perfil. Você pode conferi-lo no aplicativo.')


async def retry_public_reply(client, url, headers, payload):
    """Uma tentativa adicional quando o provedor gerou somente raciocínio.

    Não reaproveita texto interno nem executa ações da tentativa incompleta.
    Uma falha no retry mantém o fallback seguro do parser.
    """
    retry_payload = {**payload, "max_tokens": 1000, "temperature": 0.2,
                     "messages": [dict(message) for message in payload["messages"]]}
    retry_payload["messages"][0]["content"] += (
        "\nForneça somente a resposta pública final em português, sem raciocínio interno. "
        "Não use JSON, tags think ou comandos de registro.")
    try:
        response = await client.post(url, headers=headers, json=retry_payload)
        if response.status_code == 200:
            content = response.json()["choices"][0]["message"]["content"]
            reply, _, _ = parse_conversation_reply(content)
            # Retry é conversa pura, sem executar ações propostas pelo modelo.
            return reply, 'CONVERSA', {}
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    except Exception:
        # Erro do provedor: o pipeline ainda pode enviar o fallback original.
        pass
    return FALLBACK_REPLY, 'CONVERSA', {}
