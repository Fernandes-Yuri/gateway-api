"""Sanitizacao de texto: TTS e PII (LGPD).

Funcoes puras, sem dependencia do FastAPI nem do estado global.
Extraidas do monolito server.py (linhas 1835-1910) na Fase A passo 1
(doc 11_PLANO_MODULARIZACAO_E_TTS.md).
"""
import json
import re

# =============================================================
# SANITIZACAO DE TEXTO PARA SINTETESE VOCAL (TTS)
# =============================================================
EMOJI_REGEX = re.compile(
    r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]|[\u3030\u303d\u3297\u3299]"
)
STAGE_DIRECTIONS_REGEX = re.compile(r"\*.*?\*|\(.*?\)|\[.*?\]")


def clean_text_for_tts(text: str) -> str:
    """
    Higieniza o texto antes da sintese neural.
    Remove emojis, marcacoes de expressao teatral (*sorrindo*, *pausa*, [risos]),
    simbolos markdown e normaliza onomatopeias com letras repetidas que causam
    delirio e soletracao sequencial de caracteres no sintetizador.
    """
    if not text:
        return ""
    # Se o texto for um JSON serializado (ex: {"fala": "..."}), extrai apenas o campo da fala
    stripped = text.strip()
    if stripped.startswith("{") and ('"fala"' in stripped or "'fala'" in stripped):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and "fala" in parsed:
                text = parsed["fala"]
        except Exception:
            m_fala = re.search(r'["\']fala["\']\s*:\s*["\'](.*?)["\']', stripped)
            if m_fala:
                text = m_fala.group(1)
    # 1. Remove tags de link e URLs
    cleaned = re.sub(r'\[LINK:\s*.*?\]', '', text)
    cleaned = re.sub(r'https?://\S+|www\.\S+', '', cleaned)
    # 2. Remove marcacoes de acao/expressao teatral (*sorrindo*, (pensativa), [pausa])
    cleaned = STAGE_DIRECTIONS_REGEX.sub('', cleaned)
    # 3. Remove emojis (o TTS le por extenso, ex: "coracao verde")
    cleaned = EMOJI_REGEX.sub('', cleaned)
    # 4. Remove simbolos markdown residuais (*, _, ~, #, `, >, |)
    cleaned = re.sub(r'[*_~#`>|]', '', cleaned)
    # 5. Remove onomatopeias e letras triplicadas/repetidas que provocam soletracao
    cleaned = re.sub(r'(?i)\b(hum+|h+m+)\b', '', cleaned)
    cleaned = re.sub(r'(?i)\b(k{2,}|haha+|rsrs+)\b', '', cleaned)
    cleaned = re.sub(r'(.)\1{2,}', r'\1', cleaned)
    # 6. Normaliza espacos multiplos
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


# =============================================================
# SANITIZACAO LGPD & PIPELINE DE FINE-TUNING
# =============================================================
PHONE_REGEX = re.compile(r"(\+?55\s?)?(\(?0?\d{2}\)?\s?)?(9?\d{4}[-\s]?\d{4})")
CPF_REGEX = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
CEP_REGEX = re.compile(r"\b\d{5}-?\d{3}\b")
# Codigos de verificacao (OTP) de 5 a 8 digitos soltos: se cairem no dataset
# de fine-tuning, o modelo pode aprender a repeti-los (vazamento de credencial
# temporaria). Valores de saude (pressao, glicemia, peso) tem 2-3 digitos e
# nao sao afetados.
OTP_REGEX = re.compile(r"(?<![\d])(\d{5,8})(?![\d])")


def sanitize_pii(text: str, user_name: str = None) -> str:
    """
    Higieniza dados sensiveis (PII) em conformidade com a LGPD (Lei 13.709/2018).
    Substitui numeros de telefone, CPFs, e-mails, CEPs e nomes reais
    por tokens neutros de aprendizado para proteger a privacidade e evitar alucinacoes.
    """
    if not text:
        return ""
    sanitized = text
    if user_name and len(user_name) > 2:
        sanitized = re.sub(re.escape(user_name), "[USUARIO]", sanitized, flags=re.IGNORECASE)
    sanitized = CPF_REGEX.sub("[CPF]", sanitized)
    sanitized = EMAIL_REGEX.sub("[EMAIL]", sanitized)
    sanitized = CEP_REGEX.sub("[CEP]", sanitized)
    sanitized = PHONE_REGEX.sub("[TELEFONE]", sanitized)
    # Por ultimo: codigos OTP de 5-8 digitos que tenham sobrevivido as
    # regras acima (CEP/CPF ja viraram tokens e nao sao mais digitos).
    sanitized = OTP_REGEX.sub("[CODIGO]", sanitized)
    return sanitized
