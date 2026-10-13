"""Mensagens de cuidado no WhatsApp (PR4, passo 4.3).

Nesta entrega, lembrete significa uma MENSAGEM DE CUIDADO enviada após
confirmação, reaproveitando o contrato do app (care_notification_deliveries +
care_sender, de services/care_automations.py). Não é evento de agenda e não
haveria envio futuro: nenhum agendador persistente executa essa operação.
Agendamento fica para uma ampliação própria.

Contrato reutilizado (D62):
- _claim(conn, patient_id, caregiver_id, notification_type, dedupe_key):
  reserva atômica do envio; reenvio do mesmo lembrete encontra a chave e não
  duplica.
- _deliver_or_release(pool, sender, claim_id, phone, message, type): executa
  o envio e marca delivered_at, ou devolve o claim em falha.

Regras do plano (§4.3/§4.4):
- Destinatário e conteúdo identificados; faltam dados -> esclarecer.
- Texto e destinatário mostrados ANTES de pedir a confirmação.
- Envio só por canais/destinatários do vínculo de cuidado (nunca externos).
- Autorização revalidada no envio; falha de entrega nunca vira "enviado".
"""
import json
from datetime import datetime
from typing import Any, Optional

from services.care_automations import _claim, _deliver_or_release
from services.operations import OperationContext, OperationResult, validate_actor

NOTIFICATION_TYPE = "CARE_MESSAGE"
_METRIC_TYPE = "CARE_MESSAGE"


def _validate_content(text: str) -> Optional[str]:
    """Contúdo mínimo e coerente. None quando falta algo essencial."""
    corpo = (text or "").strip()
    if len(corpo) < 3:
        return None
    return corpo


async def recipient_of(conn: Any, context: OperationContext) -> OperationResult:
    """
    Destinatário da mensagem de cuidado: o próprio paciente (autocuidado) ou
    o cuidador do vínculo ativo do paciente-alvo (mensagem que o familiar
    recebe sobre quem ele acompanha).
    """
    if context.is_self_care:
        # Autocuidado: a mensagem vai para o próprio número do paciente.
        return OperationResult.success("Alvo: você mesmo.",
                                       data={"recipient_kind": "self"})
    # Familiar: revalida o vínculo antes de revelar o destinatário.
    denied = await validate_actor(conn, context, "care:read")
    if denied:
        return denied
    return OperationResult.success("Alvo: cuidador do vínculo.",
                                   data={"recipient_kind": "caregiver"})


async def prepare_care_message(conn: Any, db_pool: Any, *,
                               context: OperationContext,
                               text: str) -> OperationResult:
    """
    Monta a proposta da mensagem de cuidado: resolve destinatário, valida o
    conteúdo e devolve o texto + destinatário para confirmar ANTES do envio.
    Guarda corpo e destinatário na pendência (passo 4.2/5.2: o "sim" só
    confirma a proposta ativa correspondente).

    Não envia nada. O envio é step 4.4, após confirmação.
    """
    denied = await validate_actor(conn, context, "care:read")
    if denied:
        return denied

    corpo = _validate_content(text)
    if not corpo:
        return OperationResult.clarification(
            "Não entendi o que você quer enviar. Escreva a mensagem de "
            "cuidado, tipo 'lembra a mamãe de beber água'.")

    if context.is_self_care:
        destinatario = "você mesmo (este WhatsApp)"
    else:
        nome = await conn.fetchval(
            "SELECT full_name FROM profiles WHERE id=$1", context.patient_id)
        primeiro = (nome or "").split()[0] if nome else ""
        if not primeiro:
            return OperationResult.unavailable(
                "Não consegui identificar o destinatário agora. Tente novamente.")
        destinatario = f"o número de {primeiro} no vínculo de cuidado"

    from core.confirmation import create_pending_confirmation
    code = await create_pending_confirmation(
        user_id=context.patient_id,
        metric_type=_METRIC_TYPE,
        value_json={"body": corpo,
                    "recipient_kind": "self" if context.is_self_care else "caregiver",
                    "actor_id": context.actor_id},
        db_pool=db_pool,
    )
    if not code:
        return OperationResult.unavailable(
            "Não consegui preparar a confirmação da mensagem agora. Tente novamente.")

    proposta = (
        f"💌 *Mensagem de cuidado*\n\n"
        f"Para: *{destinatario}*\n"
        f"Texto: *{corpo}*\n\n"
        f"Posso enviar agora?")
    return OperationResult.confirmation(proposta, pending_op_id=code,
                                        data={"body": corpo, "code": code})


async def confirm_pending_care_message(db_pool: Any, sender: Any, *,
                                       confirmation_code: str,
                                       actor_id: str) -> OperationResult:
    """
    Passo 4.4: consome a pendência da mensagem de cuidado e envia pelo
    contrato do app, numa transação só. Revalida a autorização no envio e
    só confirma ao usuário após a entrega real — falha nunca vira "enviado".
    """
    if not db_pool or not sender:
        return OperationResult.unavailable(
            "O serviço de envio não está disponível agora. Tente novamente.")
    if not confirmation_code or not actor_id:
        return OperationResult.missing_data(
            "Não consegui identificar esta confirmação. Escreva novamente a mensagem.")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, value_json FROM pending_confirmations
               WHERE confirmation_code = $1
                 AND user_id = $2
                 AND status = 'pending'
                 AND expires_at > NOW()""",
            confirmation_code, actor_id,
        )
        if not row:
            return OperationResult.missing_data(
                "Esta confirmação já foi usada ou está expirada. Se a mensagem "
                "não foi enviada, escreva novamente o que você quer mandar.")
        value = json.loads(row["value_json"])
        pending_id = row["id"]
        recipient_kind = value.get("recipient_kind") or "self"
        patient_id = value.get("patient_id") or actor_id

        # Contexto efetivo do envio: ator autenticado + paciente-alvo da
        # pendência (autocuidado ou familiar).
        send_ctx = OperationContext(actor_id=actor_id, patient_id=patient_id,
                                    channel="whatsapp")

        outcome: Optional[OperationResult] = None
        try:
            async with conn.transaction():
                consumed = await conn.execute(
                    """UPDATE pending_confirmations
                       SET status = 'confirmed', confirmed_at = NOW()
                       WHERE id = $1 AND status = 'pending'""",
                    pending_id,
                )
                if consumed != "UPDATE 1":
                    return OperationResult.missing_data(
                        "Esta mensagem já foi enviada por outra via. "
                        "Nada foi duplicado.")

                if recipient_kind == "self":
                    phone = await conn.fetchval(
                        "SELECT whatsapp_phone FROM profiles WHERE id=$1", actor_id)
                else:
                    # Destinatário do vínculo de cuidado, nunca um contato
                    # arbitrário informado na fala.
                    phone = await conn.fetchval(
                        """SELECT c.whatsapp_phone FROM family_bindings b
                           JOIN profiles c ON c.id = b.caregiver_id
                           WHERE b.patient_id = $1 AND b.status = 'ACTIVE'
                             AND c.whatsapp_phone IS NOT NULL
                           ORDER BY b.updated_at DESC LIMIT 1""",
                        patient_id,
                    )
                if not phone:
                    raise _NoRecipient(
                        "Não encontrei um número de WhatsApp vinculado para "
                        "enviar essa mensagem. O vínculo precisa estar ativo "
                        "no aplicativo.")

                # Revalida a autorização NO envio (passo 4.4): vínculo
                # revogado entre a proposta e o clique corta o envio.
                denied = await validate_actor(conn, send_ctx, "care:read")
                if denied:
                    return denied

                claim_id = await _claim(
                    conn, patient_id, actor_id, NOTIFICATION_TYPE,
                    f"care:{confirmation_code}",
                )
                if not claim_id:
                    return OperationResult.missing_data(
                        "Essa mensagem de cuidado já foi enviada. "
                        "Nada foi duplicado.")

                await conn.execute(
                    """INSERT INTO confirmation_logs
                           (user_id, pending_conf_id, confirmation_code,
                            action, values_submitted, values_confirmed,
                            channel, confirmed_by_user, was_edited)
                       VALUES ($1, $2, $3, $4, $5, $6, 'whatsapp', TRUE, FALSE)""",
                    actor_id, pending_id, confirmation_code, "ENVIAR_CARE_MESSAGE",
                    json.dumps(value), json.dumps({"phone_tail": str(phone)[-4:]}),
                )
        except _NoRecipient as err:
            return OperationResult.missing_data(str(err))
        except Exception as err:
            print(f"[Care Messages] Falha ao enviar mensagem de cuidado: {err}")
            return OperationResult.unavailable(
                "Agora não consegui enviar a mensagem. Tente novamente em instantes.")

    # I/O de rede fora da transação, como no contrato do app (D62).
    delivered = await _deliver_or_release(
        db_pool, sender, claim_id, phone, value.get("body") or "", NOTIFICATION_TYPE,
    )
    if not delivered:
        return OperationResult.unavailable(
            "Não consegui entregar a mensagem agora. Ela ficou registrada "
            "para nova tentativa; só diga que foi enviada quando a entrega "
            "for confirmada.")
    return OperationResult.success(
        "💌 Mensagem de cuidado entregue!",
        data={"phone_tail": str(phone)[-4:],
              "delivered_at": datetime.utcnow().isoformat()})


class _NoRecipient(Exception):
    """Sem destinatário válido no vínculo — não é falha do envio em si."""
