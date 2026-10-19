"""Contratos compartilhados das operações de saúde (PR1, passo 1.3).

Toda operação (consulta, registro, lembrete, preferência) recebe um contexto de
execução e devolve um resultado estruturado. O contrato é canônico para o
WhatsApp e para a API REST: a mesma operação executada do app ou do WhatsApp
tem o mesmo comportamento, mudando apenas o canal.

Contexto:
  actor_id      - quem executa (conta autenticada / vínculo confirmado)
  patient_id    - de quem é o dado (alvo da operação)
  channel       - "whatsapp" | "app"
  message_id    - id da mensagem Meta (antiduplicidade, passo 1.4)
  pending_op_id - id da operação pendente que autoriza a gravação

actor_id != patient_id é a regra de ouro: autocuidado é quando são iguais;
familiar exige vínculo ativo, revalidado a cada operação (nunca cacheado entre
operações). Toda gravação exige confirmação vinculada à operação pendente.

Resultado estruturado — o modelo apenas interpreta a linguagem; é o serviço que
computa o status, nunca a conversa:
  success       - operação efetivada
  missing_data  - dados insuficientes no banco (sem registro) -> esclarecer
  clarification - ambiguidade na fala; é preciso perguntar ao usuário
  confirmation  - pendência criada; depende de confirmação do usuário
  denied        - sem autorização (vínculo ausente/revogado/sem permissão)
  unavailable   - banco ou serviço indisponível
"""
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

CHANNEL_APP = "app"

STATUS_SUCCESS = "success"
STATUS_MISSING_DATA = "missing_data"
STATUS_CLARIFICATION = "clarification"
STATUS_CONFIRMATION = "confirmation"
STATUS_DENIED = "denied"
STATUS_UNAVAILABLE = "unavailable"


class OperationContext(BaseModel):
    """Identidade de uma execução. Quem age, sobre quem, por onde."""

    actor_id: str
    patient_id: str
    channel: str = CHANNEL_APP
    message_id: Optional[str] = None
    pending_op_id: Optional[str] = None

    @property
    def is_self_care(self) -> bool:
        """Autocuidado: actor e patient são a mesma conta."""
        return self.actor_id == self.patient_id


class OperationResult(BaseModel):
    """Resultado estruturado de uma operação de serviço.

    message: texto pronto para o canal (WhatsApp) ou detalhe para a API.
    data: payload estruturado (registro encontrado, opções, etc.).
    pending_op_id: presente em status=confirmation (vínculo com a pendência).
    """

    status: str
    message: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    pending_op_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    # --- fábricas: o status nasce do serviço, nunca do modelo ---
    @classmethod
    def success(cls, message: str = "", data: Optional[dict] = None) -> "OperationResult":
        return cls(status=STATUS_SUCCESS, message=message or None, data=data)

    @classmethod
    def missing_data(cls, message: str, data: Optional[dict] = None) -> "OperationResult":
        return cls(status=STATUS_MISSING_DATA, message=message, data=data)

    @classmethod
    def clarification(cls, message: str, data: Optional[dict] = None) -> "OperationResult":
        return cls(status=STATUS_CLARIFICATION, message=message, data=data)

    @classmethod
    def confirmation(cls, message: str, pending_op_id: str,
                     data: Optional[dict] = None) -> "OperationResult":
        return cls(status=STATUS_CONFIRMATION, message=message,
                   pending_op_id=pending_op_id, data=data)

    @classmethod
    def denied(cls, message: str, data: Optional[dict] = None) -> "OperationResult":
        return cls(status=STATUS_DENIED, message=message, data=data)

    @classmethod
    def unavailable(cls, message: str, data: Optional[dict] = None) -> "OperationResult":
        return cls(status=STATUS_UNAVAILABLE, message=message, data=data)

    @property
    def is_success(self) -> bool:
        return self.status == STATUS_SUCCESS

    @property
    def is_confirmation(self) -> bool:
        return self.status == STATUS_CONFIRMATION


async def validate_actor(conn: Any, context: OperationContext,
                         permission: Optional[str] = None) -> Optional[OperationResult]:
    """
    Revalida a autorização do ator para o paciente-alvo, a cada operação.

    Reutiliza services.care_os.require_patient_permission (roles + permissões
    do family_bindings). Retorna None quando autorizado, ou OperationResult
    denied quando não. Autocuidado (actor == patient) sempre passa; familiar
    exige vínculo ACTIVE com a permissão exigida — vínculo revogado corta a
    operação mesmo que tenha sido selecionado antes.
    """
    if context.is_self_care:
        return None
    try:
        from services.care_os import require_patient_permission
        await require_patient_permission(conn, context.actor_id, context.patient_id,
                                         permission or "care:read")
        return None
    except Exception as err:
        detail = getattr(err, "detail", None) or str(err)
        if not isinstance(detail, str) or not detail.strip():
            detail = "O acompanhamento deste familiar não está autorizado."
        return OperationResult.denied(detail)
