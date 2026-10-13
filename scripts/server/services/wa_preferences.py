"""Preferências compartilhadas entre app e WhatsApp (PR5).

O app já resolve preferências por LLM (assistant_preferences.py: interpret ->
propose/confirm). O WhatsApp precisa do mesmo RESULTADO sem reproduzir a
interpretação livre: aqui o reconhecimento é determinístico (passo 5.3), a
chave é sempre a conta (passo 5.1) e a confirmação só vale para a proposta
ativa correspondente (passo 5.2).

Escopo idêntico ao do app: nickname, response_length, cup_ml. Nada além.
"""
import json
import re
import unicodedata
import uuid
from typing import Any, Optional

from services.operations import OperationContext, OperationResult

# Passo 5.1: chaves permitidas — as mesmas do app.
PREFERENCE_KEYS = ("nickname", "response_length", "cup_ml")
RESPONSE_LENGTHS = ("short", "balanced", "detailed")

# Sigilo da pendência: o "sim" só confirma a proposta ativa (passo 5.2).
_METRIC_TYPE = "PREFERENCE"


def _norm(text: str) -> str:
    no_acc = unicodedata.normalize("NFD", text or "")
    no_acc = "".join(c for c in no_acc if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_acc.lower()).strip()


_LIST_RE = re.compile(
    r"\b(o que voce lembra|o que vc lembra|o que voce sabe de mim|"
    r"o que vc sabe de mim|quais (sao|sao as) (as )?preferencias|"
    r"minhas preferencias|o que voce guardou|o que vc guardou)\b")

_FORGET_ALL_RE = re.compile(
    r"\b(esquece (tudo|todas|as preferencias|o que sabe|o que voce sabe|"
    r"o que vc sabe))|\bapaga (tudo|todas (as )?preferencias)\b|"
    r"\bnao quero que (voce|vc) (lembre|guarde|saiba) (nada|de mim)\b")

_SET_NICKNAME_RE = re.compile(
    r"\b(me chama de|me chame de|pode me chamar de|gostaria de ser chamad[oa] de|"
    r"quero ser chamad[oa] de)\s+([A-Za-zÀ-ÿ'' -]{1,40})")

_SET_LENGTH_RE = re.compile(
    r"\b(respostas? (mais )?(curtas|longas|detalhadas)|"
    r"prefiro respostas? (mais )?(curtas|longas|detalhadas)|"
    r"responde (mais )?(curto|longo|detalhado)|"
    r"quero respostas? (curtas|longas|detalhadas|equilibradas))\b")

_FORGET_ONE_RE = re.compile(
    r"\b(esquece|apaga|remove)\b.{0,40}\b"
    r"(apelido|copo|respostas|extensao|tamanho)\b")

# Passo 5.3: consulta de UMA preferência ("qual meu apelido?"), além do
# "o que você lembra de mim?" que lista tudo. Padrões específicos para
# não confundir "qual copo devo usar?" com consulta de preferência.
_QUERY_ONE_RE = re.compile(
    r"\b(qual|o que e|qual e|me diz|me fala)\b.{0,20}\b"
    r"(apelido|tamanho do (meu )?copo|volume do (meu )?copo|"
    r"extensao das (minhas )?respostas)\b"
    r"|como voce (me )?(chama|chamaria)")

# Passo 5.3: volume habitual do copo ("meu copo é de 200 ml").
_SET_CUP_RE = re.compile(
    r"\b(meu copo (e|tem)?\s*(de|e)?|copo (de|com|e)|bebo (no|em|de)?\s*(um|copo)?\s*"
    r"(copo\s*(de|com)?|de)?)\s*(\d{2,4})\s*(ml|mililitros?)\b",
    re.IGNORECASE)


def _target_key(norm: str) -> Optional[str]:
    """Resolve a chave (nickname/cup_ml/response_length) da preferência citada.

    Tolerante a possessivos e variações ("tamanho do MEU copo",
    "como você ME chama", "extensão das MINHAS respostas").
    """
    if re.search(r"\bapelido\b|\bcomo voce (me )?chama\w*\b|"
                 r"\bnome que voce (me )?chama\w*\b", norm):
        return "nickname"
    if re.search(r"\bcopo\b|\btamanho do copo\b|\bvolume do copo\b", norm):
        return "cup_ml"
    if re.search(r"\bextensao das (minhas )?respostas\b|"
                 r"\brespostas (curtas|longas|detalhadas|equilibradas)\b", norm):
        return "response_length"
    return None


def _length_value(norm: str) -> Optional[str]:
    m = re.search(r"\b(curtas|curto)\b", norm)
    if m:
        return "short"
    m = re.search(r"\b(detalhadas|detalhado|longas|longo)\b", norm)
    if m:
        return "detailed"
    m = re.search(r"\b(equilibradas|equilibrada)\b", norm)
    if m:
        return "balanced"
    return None


def detect_preference_intent(text: str) -> Optional[dict]:
    """
    Intenção determinística de preferência (passo 5.3), sem LLM.

    Retorna None quando a fala não é de preferência (segue o fluxo normal).
    """
    norm = _norm(text)
    if not norm:
        return None

    if _LIST_RE.search(norm):
        return {"op": "list"}

    if _FORGET_ALL_RE.search(norm):
        return {"op": "forget", "key": "all"}

    if _FORGET_ONE_RE.search(norm):
        key = _target_key(norm)
        if key:
            return {"op": "forget", "key": key}

    # Passo 5.3: consulta de uma única preferência.
    if _QUERY_ONE_RE.search(norm):
        key = _target_key(norm)
        if key:
            return {"op": "list", "key": key}

    m = _SET_NICKNAME_RE.search(norm)
    if m:
        apelido = m.group(2).strip()
        if 1 <= len(apelido) <= 40 and all(c.isalpha() or c in " '-" for c in apelido):
            return {"op": "set", "key": "nickname", "value": apelido}

    if _SET_LENGTH_RE.search(norm):
        valor = _length_value(norm)
        if valor:
            return {"op": "set", "key": "response_length", "value": valor}

    m = _SET_CUP_RE.search(norm)
    if m:
        volume = int(m.group(9))
        if 50 <= volume <= 2000:
            return {"op": "set", "key": "cup_ml", "value": volume}

    return None


def _validate(key: str, value: Any) -> Optional[str]:
    """Valida como o app (validate_operation); devolve mensagem de erro."""
    if key not in PREFERENCE_KEYS and key != "all":
        return "Preferência inválida."
    if key == "all":
        return None
    if value is None:
        return None
    if key == "nickname":
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40:
            return "Apelido inválido."
        if not all(c.isalpha() or c in " '-" for c in value.strip()):
            return "Apelido inválido."
    elif key == "response_length" and value not in RESPONSE_LENGTHS:
        return "Extensão inválida."
    elif key == "cup_ml":
        if type(value) is not int or not 50 <= value <= 2000:
            return "Volume inválido."
    return None


def _describe(key: str, value: Any) -> str:
    if key == "nickname":
        return f"chamar você de {value}"
    if key == "cup_ml":
        return f"usar {value} ml como tamanho do seu copo habitual"
    return {"short": "dar respostas mais curtas",
            "balanced": "dar respostas de extensão equilibrada",
            "detailed": "dar respostas mais detalhadas"}[value]


# Passo 5.4: aplicação das preferências confirmadas no canal.
_BARE_CUP_RE = re.compile(
    r"\b(bebi|tomei|consumi|mais um|outro|um|dei)\b\s+(um|mais um|outro)?\s*"
    r"(copo|xicara|caneca)\b(\s+(de\s+)?(agua|liquido))?\b")


def _question(intent: dict) -> str:
    key = intent.get("key")
    if key == "all":
        return ("Quer apagar todas as preferências que guardei sobre você? "
                "Isso não apaga conversas nem registros de saúde.")
    if intent.get("value") is None:
        nomes = {"nickname": "apelido", "response_length": "extensão das respostas",
                 "cup_ml": "tamanho do copo"}
        return f"Quer apagar a preferência de {nomes[key]}?"
    return (f"Posso guardar na sua conta a preferência de "
            f"{_describe(key, intent['value'])} para as próximas conversas?")


async def read_preferences(conn: Any, user_id: str) -> dict:
    """Preferências confirmadas da conta (chave = conta, nunca o telefone)."""
    try:
        value = await conn.fetchval(
            "SELECT preferences FROM assistant_preferences WHERE user_id=$1", user_id)
    except Exception as err:
        print(f"[WA Preferences] Falha ao ler preferências: {err}")
        return {}
    if value is None:
        return {}
    return json.loads(value) if isinstance(value, str) else dict(value)


# Passo 5.4: limite de "um copo" raso — sem outro número na fala, o copo
# habitual da preferência é a única fonte do volume.
_HAS_NUMBER_RE = re.compile(r"\d")


async def resolve_cup_ml(conn: Any, context: OperationContext,
                         text: str) -> Optional[int]:
    """
    Passo 5.4: volume de "um copo" no autocuidado.

    Só vale no autocuidado (actor == patient): a preferência de copo é do
    interlocutor e nunca preenche o registro de outra pessoa. Se a fala
    traz volume explícito ("300 ml"), ela prevalece sobre a preferência.

    Devolve None quando não é autocuidado, não há preferência de copo ou a
    fala não é de "um copo" (o pipeline segue com o volume declarado).
    """
    if not context or not context.is_self_care or not text:
        return None
    norm = _norm(text)
    if _HAS_NUMBER_RE.search(norm):
        return None
    if not _BARE_CUP_RE.search(norm):
        return None
    try:
        prefs = await read_preferences(conn, context.actor_id)
    except Exception as err:
        print(f"[WA Preferences] Falha ao ler copo habitual: {err}")
        return None
    copo = prefs.get("cup_ml")
    if isinstance(copo, int) and 50 <= copo <= 2000:
        return copo
    return None


async def handle_preference(conn: Any, db_pool: Any, *,
                            context: OperationContext,
                            text: str) -> Optional[OperationResult]:
    """
    Passos 5.1/5.3: reconhece a intenção e executa ou propõe.

    - list             -> success com as preferências reais da conta.
    - set/forget       -> proposal (pendencia) — gravação só após confirmar.
    - Pendência ativa e resposta afirmativa -> confirm (passo 5.2).
    - Devolve None quando a fala não é de preferência (fluxo normal segue).
    """
    if not context or not context.actor_id:
        return None
    norm = _norm(text)
    intent = detect_preference_intent(text)
    prefs = await read_preferences(conn, context.actor_id)

    # Passo 5.2: pendência ativa da CONTA; só ela autoriza a gravação.
    pending = None
    try:
        pending = await conn.fetchrow(
            """SELECT proposal_id, operation, question FROM assistant_preference_pending
               WHERE user_id=$1 AND expires_at > NOW()""",
            context.actor_id)
    except Exception as err:
        print(f"[WA Preferences] Aviso: pendência não lida: {err}")

    # Confirmação: "sim" genérico não basta; a fala precisa ser afirmativa E
    # a pendência precisa estar ativa e pertencer a esta conta.
    if pending and not intent and _is_affirmative(norm):
        code = await conn.fetchval(
            """SELECT confirmation_code FROM pending_confirmations
               WHERE user_id=$1 AND metric_type='PREFERENCE' AND status='pending'
                 AND expires_at > NOW() AND value_json->>'proposal_id'=$2
               ORDER BY created_at DESC LIMIT 1""", context.actor_id, pending["proposal_id"])
        if code:
            return await confirm_pending_preference(
                db_pool, confirmation_code=code, actor_id=context.actor_id)
        return None

    if pending and not intent and _is_negative(norm):
        await _cancel_pending(conn, context.actor_id, pending)
        return OperationResult.success(
            "Tudo bem, não alterei suas preferências.")

    if not intent:
        return None

    if intent["op"] == "list":
        chave = intent.get("key")
        if chave:
            if chave not in prefs:
                return OperationResult.success(
                    {"nickname": "Não guardei apelido seu — me diga "
                                 "'me chama de ...' se quiser.",
                     "response_length": "Não guardei preferência de extensão "
                                        "das respostas.",
                     "cup_ml": "Não guardei o tamanho do seu copo habitual."}[chave])
            return OperationResult.success(
                "Guardei isto: " + _describe(chave, prefs[chave]) + ".")
        if not prefs:
            return OperationResult.success(
                "Ainda não tenho preferências suas guardadas.")
        return OperationResult.success(
            "Guardei estas preferências: "
            + "; ".join(_describe(k, v) for k, v in prefs.items()) + ".")

    # set / forget: cria proposta e pendência; nada é gravado ainda.
    erro = _validate(intent["key"], intent.get("value"))
    if erro:
        return OperationResult.clarification(
            f"Não consegui entender essa preferência. {erro} Tente novamente, "
            f"tipo 'me chama de Ana' ou 'prefiro respostas curtas'.")

    proposal_id = str(uuid.uuid4())
    value_json = {"op": "propose", "key": intent["key"],
                  "value": intent.get("value"), "proposal_id": proposal_id}
    from core.confirmation import create_pending_confirmation
    code = await create_pending_confirmation(
        user_id=context.actor_id,
        metric_type=_METRIC_TYPE,
        value_json=value_json,
        db_pool=db_pool,
    )
    if not code:
        return OperationResult.unavailable(
            "Não consegui preparar a confirmação da preferência agora. "
            "Tente novamente.")

    # Passo 5.2: a pendência do app guarda a proposta; a do WhatsApp
    # (pending_confirmations) amarra a confirmação ao clique nesta proposta.
    try:
        await conn.execute(
            """INSERT INTO assistant_preference_pending
                   (user_id, proposal_id, operation, question, expires_at)
               VALUES ($1, $2, $3::jsonb, $4, NOW() + INTERVAL '5 minutes')
               ON CONFLICT (user_id) DO UPDATE SET
                   proposal_id=EXCLUDED.proposal_id,
                   operation=EXCLUDED.operation,
                   question=EXCLUDED.question,
                   expires_at=NOW() + INTERVAL '5 minutes'""",
            context.actor_id, proposal_id, json.dumps(value_json), _question(intent),
        )
    except Exception as err:
        print(f"[WA Preferences] Pendência não preparada: {err}")
        return OperationResult.unavailable(
            "Não consegui preparar a alteração. Nada foi gravado; tente novamente.")

    return OperationResult.confirmation(
        _question(intent), pending_op_id=code, data=value_json)


def _is_affirmative(norm: str) -> bool:
    return bool(re.match(r"^(sim|pode sim|claro|com certeza|confirmo|"
                         r"isso mesmo|exato|perfeito|pode ser|ta bom|"
                         r"tudo bem|aham|yes)\b", norm))


def _is_negative(norm: str) -> bool:
    return bool(re.match(r"^(nao|não|nao quero|cancela|cancelar|"
                         r"deixa|esquece|nao e isso)\b", norm))


async def _cancel_pending(conn: Any, user_id: str, pending: Any) -> None:
    try:
        await conn.execute(
            "DELETE FROM assistant_preference_pending "
            "WHERE user_id=$1 AND proposal_id=$2",
            user_id, pending["proposal_id"])
    except Exception as err:
        print(f"[WA Preferences] Aviso: pendência não cancelada: {err}")


async def confirm_pending_preference(db_pool: Any, *,
                                     confirmation_code: str,
                                     actor_id: str) -> OperationResult:
    """
    Passo 5.2 (callback do botão): consome a pendência do WhatsApp e grava,
    na mesma transação. Reenvio do botão encontra a pendência já consumida
    e não duplica.
    """
    if not db_pool:
        return OperationResult.unavailable(
            "Não consegui acessar o banco agora. Tente novamente.")
    if not confirmation_code or not actor_id:
        return OperationResult.missing_data(
            "Não consegui identificar esta confirmação.")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, value_json FROM pending_confirmations
               WHERE confirmation_code=$1 AND user_id=$2
                 AND status='pending' AND expires_at > NOW()""",
            confirmation_code, actor_id)
        if not row:
            return OperationResult.missing_data(
                "Esta confirmação já foi usada ou está expirada. "
                "Diga novamente o que quer guardar ou apagar.")
        value = json.loads(row["value_json"])
        pending_id = row["id"]
        proposal_id = value.get("proposal_id") or confirmation_code

        # A pendência do app (assistant_preference_pending) precisa estar
        # ativa e casar com o código desta proposta — vínculo da confirmação.
        app_pending = await conn.fetchrow(
            """SELECT proposal_id FROM assistant_preference_pending
               WHERE user_id=$1 AND proposal_id=$2 AND expires_at > NOW()""",
            actor_id, proposal_id)

        if not app_pending:
            return OperationResult.missing_data(
                "Essa proposta foi substituída ou expirou. Confirme a proposta mais recente.")
        try:
            async with conn.transaction():
                consumed = await conn.execute(
                    """UPDATE pending_confirmations
                       SET status='confirmed', confirmed_at=NOW()
                       WHERE id=$1 AND status='pending'""",
                    pending_id)
                if consumed != "UPDATE 1":
                    return OperationResult.missing_data(
                        "Esta preferência já foi confirmada por outra via. "
                        "Nada foi duplicado.")

                if app_pending:
                    app_row = await conn.fetchrow(
                        """DELETE FROM assistant_preference_pending
                           WHERE user_id=$1 AND proposal_id=$2
                             AND expires_at > NOW()
                           RETURNING operation""",
                        actor_id, proposal_id)
                    if not app_row:
                        raise ValueError("Proposta ativa não está mais disponível")
                    if app_row:
                        change = json.loads(app_row["operation"]) \
                            if isinstance(app_row["operation"], str) \
                            else dict(app_row["operation"])
                        await conn.execute(
                            "INSERT INTO assistant_preferences(user_id) "
                            "VALUES($1) ON CONFLICT DO NOTHING", actor_id)
                        prefs = json.loads(await conn.fetchval(
                            "SELECT preferences FROM assistant_preferences "
                            "WHERE user_id=$1 FOR UPDATE", actor_id) or "{}")
                        if change.get("key") == "all":
                            prefs = {}
                        elif change.get("value") is None:
                            prefs.pop(change.get("key"), None)
                        else:
                            prefs[change["key"]] = change["value"]
                        await conn.execute(
                            "UPDATE assistant_preferences SET preferences=$2::jsonb, "
                            "updated_at=NOW() WHERE user_id=$1",
                            actor_id, json.dumps(prefs))

                await conn.execute(
                    """INSERT INTO confirmation_logs
                           (user_id, pending_conf_id, confirmation_code,
                            action, values_submitted, values_confirmed,
                            channel, confirmed_by_user, was_edited)
                       VALUES ($1, $2, $3, $4, $5, $6,
                               'whatsapp', TRUE, FALSE)""",
                    actor_id, pending_id, confirmation_code,
                    "REGISTRAR_PREFERENCE",
                    json.dumps(value), json.dumps(value))
        except Exception as err:
            print(f"[WA Preferences] Falha no callback de preferência: {err}")
            return OperationResult.unavailable(
                "Não consegui confirmar a alteração agora. Nada foi gravado.")

    if value.get("value") is None:
        return OperationResult.success(
            "Pronto, apaguei a preferência solicitada."
            if value.get("key") != "all"
            else "Pronto, apaguei todas as preferências.")
    return OperationResult.success(
        f"Combinado, guardei a preferência de "
        f"{_describe(value['key'], value['value'])}.")
