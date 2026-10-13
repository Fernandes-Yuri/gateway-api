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
from typing import Any, Optional, Tuple

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
    r"\b(esquece|apaga|remove)\b\s+(o\s+)?(apelido|tamanho do copo|volume do copo|"
    r"extensao das respostas|respostas curtas)\b")


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
        alvo = re.search(r"\b(apelido|tamanho do copo|volume do copo|"
                         r"extensao das respostas|respostas curtas)\b", norm)
        key = "nickname" if alvo and alvo.group(1) == "apelido" else (
            "cup_ml" if alvo and "copo" in alvo.group(1) else "response_length")
        return {"op": "forget", "key": key}

    m = _SET_NICKNAME_RE.search(norm)
    if m:
        apelido = m.group(2).strip()
        if 1 <= len(apelido) <= 40 and all(c.isalpha() or c in " '-" for c in apelido):
            return {"op": "set", "key": "nickname", "value": apelido}

    if _SET_LENGTH_RE.search(norm):
        valor = _length_value(norm)
        if valor:
            return {"op": "set", "key": "response_length", "value": valor}

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
    if pending and _is_affirmative(norm):
        return await _confirm(conn, context.actor_id, pending)

    if pending and _is_negative(norm):
        await _cancel_pending(conn, context.actor_id, pending)
        return OperationResult.success(
            "Tudo bem, não alterei suas preferências.")

    intent = detect_preference_intent(text)
    if not intent:
        return None

    if intent["op"] == "list":
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

    value_json = {"op": intent["op"], "key": intent["key"],
                  "value": intent.get("value")}
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
                   (user_id, proposal_id, operation, question)
               VALUES ($1, $2, $3::jsonb, $4)
               ON CONFLICT (user_id) DO UPDATE SET
                   proposal_id=EXCLUDED.proposal_id,
                   operation=EXCLUDED.operation,
                   question=EXCLUDED.question,
                   expires_at=NOW() + INTERVAL '5 minutes'""",
            context.actor_id, code, json.dumps(value_json), _question(intent),
        )
    except Exception as err:
        print(f"[WA Preferences] Aviso: pendência do app não gravada: {err}")

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


async def _confirm(conn: Any, user_id: str, pending: Any) -> OperationResult:
    """Passo 5.2/5.3: consome a proposta ativa e grava na conta."""
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """DELETE FROM assistant_preference_pending
                   WHERE user_id=$1 AND proposal_id=$2 AND expires_at > NOW()
                   RETURNING operation""",
                user_id, pending["proposal_id"])
            if not row:
                return OperationResult.missing_data(
                    "Essa proposta já foi resolvida ou expirou. "
                    "Diga novamente o que quer guardar ou apagar.")
            change = json.loads(row["operation"]) if isinstance(row["operation"], str) \
                else dict(row["operation"])
            await conn.execute(
                "INSERT INTO assistant_preferences(user_id) VALUES($1) "
                "ON CONFLICT DO NOTHING", user_id)
            prefs = json.loads(await conn.fetchval(
                "SELECT preferences FROM assistant_preferences "
                "WHERE user_id=$1 FOR UPDATE", user_id) or "{}")
            if change.get("key") == "all":
                prefs = {}
            elif change.get("value") is None:
                prefs.pop(change["key"], None)
            else:
                prefs[change["key"]] = change["value"]
            await conn.execute(
                "UPDATE assistant_preferences SET preferences=$2::jsonb, "
                "updated_at=NOW() WHERE user_id=$1",
                user_id, json.dumps(prefs))
    except Exception as err:
        print(f"[WA Preferences] Falha ao confirmar preferência: {err}")
        return OperationResult.unavailable(
            "Não consegui confirmar a alteração agora. Nada foi gravado — "
            "tente novamente em instantes.")

    if change.get("value") is None:
        if change.get("key") == "all":
            return OperationResult.success(
                "Pronto, apaguei todas as preferências. Sua conta e seus "
                "registros de saúde continuam intactos.")
        return OperationResult.success(
            "Pronto, apaguei a preferência solicitada.")
    return OperationResult.success(
        f"Combinado, guardei a preferência de "
        f"{_describe(change['key'], change['value'])}.")


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

        # A pendência do app (assistant_preference_pending) precisa estar
        # ativa e casar com o código desta proposta — vínculo da confirmação.
        app_pending = await conn.fetchrow(
            """SELECT proposal_id FROM assistant_preference_pending
               WHERE user_id=$1 AND proposal_id=$2 AND expires_at > NOW()""",
            actor_id, confirmation_code)

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
                        actor_id, confirmation_code)
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
