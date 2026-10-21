"""Contratos v1 da captação e da futura central interna."""
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ControlRequest(StrictModel):
    epoch: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


class TextRequest(ControlRequest):
    text: str = Field(min_length=1, max_length=4000)

    @field_validator('text')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Mensagem vazia')
        return value.strip()


class SeenRequest(StrictModel):
    message_id: int = Field(ge=0)


class DeviceRequest(StrictModel):
    device_id: UUID
    token: str = Field(min_length=16, max_length=4096, repr=False)


class FieldUpdate(StrictModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    value: str | float | None
    status: Literal['provided', 'unknown', 'declined']
    evidence: str = Field(min_length=1, max_length=300)

    @model_validator(mode='after')
    def value_matches_status(self):
        if (self.status == 'provided') != (self.value is not None):
            raise ValueError('Valor incompatível com o estado')
        return self


FIELD_NAMES = (
    'contact.full_name', 'contact.gender', 'care_for', 'relationship',
    'patient.full_name', 'patient.age_years', 'patient.gender', 'payment.kind',
    'payment.insurer', 'payment.plan', 'clinical.chief_complaint', 'clinical.onset',
    'clinical.impact', 'clinical.additional_notes',
)


class ModelReply(StrictModel):
    reply: str = Field(min_length=1, max_length=2000)
    updates: dict[str, FieldUpdate] = Field(default_factory=dict, max_length=16)
    intent: Literal['continue', 'propose_summary', 'confirm_summary', 'correct_summary',
                    'human_requested', 'emergency', 'new_patient'] = 'continue'

    @model_validator(mode='after')
    def known_fields(self):
        for key, update in self.updates.items():
            if key not in FIELD_NAMES:
                raise ValueError('Campo desconhecido')
            if update.status != 'provided':
                continue
            value = update.value
            if key == 'patient.age_years':
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 130:
                    raise ValueError('Idade inválida')
            elif not isinstance(value, str) or not value.strip() or len(value) > {
                'contact.full_name':180,'patient.full_name':180,'contact.gender':80,'patient.gender':80,
                'clinical.chief_complaint':500,'clinical.impact':300,'clinical.additional_notes':700,
            }.get(key,120):
                raise ValueError('Texto inválido')
            elif key == 'care_for' and value not in ('self', 'other'):
                raise ValueError('Titularidade inválida')
            elif key == 'payment.kind' and value not in ('private', 'insurance'):
                raise ValueError('Pagamento inválido')
        return self


class Page(StrictModel):
    items: list[dict[str, Any]]
    next_cursor: int
    has_more: bool


class StoredField(StrictModel):
    value: str | float | None
    status: Literal['provided', 'unknown', 'declined']
    evidence: str
    message_id: int
    derived_from: str | None = None


class HandoffInfo(StrictModel):
    id: UUID
    epoch: int
    reason: str
    status: Literal['pending', 'push_accepted', 'seen', 'claimed']
    attempts: int


class Capabilities(StrictModel):
    api_version: Literal['1']
    intake_enabled: bool
    audio_validation_available: bool
    audio_max_bytes: int
    audio_max_seconds: int
    audio_mime_types: list[str]
    image_max_bytes: int
    realtime: Literal['websocket']
    operator_uid: str


class Conversation(StrictModel):
    id: UUID
    mode: Literal['bot', 'waiting_human', 'human', 'closed']
    epoch: int
    operator_uid: str | None
    intake_status: str
    fields: dict[str, StoredField]
    summary: str | None
    consented: bool
    last_inbound_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    handoff: HandoffInfo | None = None


class CommandResult(StrictModel):
    conversation_id: UUID
    epoch: int
    mode: str


class QueuedMessage(StrictModel):
    message_id: int
    delivery: str


class ConversationItem(Conversation):
    unread_count: int


class ConversationList(StrictModel):
    items: list[ConversationItem]
    next_offset: int
    has_more: bool
    event_cursor: int


class Message(StrictModel):
    id: int
    conversation_id: UUID
    origin: Literal['contact', 'bot', 'human', 'system']
    kind: Literal['text', 'audio', 'image', 'unsupported']
    body: str
    delivery: Literal['received', 'queued', 'sending', 'sent', 'delivered', 'read', 'failed', 'uncertain', 'cancelled']
    operator_uid: str | None
    transcript: str | None
    transcript_status: Literal['none', 'pending', 'completed', 'failed']
    error_code: str | None
    created_at: datetime
    media_id: UUID | None = None
    media_status: Literal['pending', 'ready', 'failed'] | None = None
    media_mime_type: str | None = None


class MessagePage(StrictModel):
    items: list[Message]
    next_cursor: int
    has_more: bool


class Event(StrictModel):
    id: int
    conversation_id: UUID
    kind: str
    data: dict[str, Any]
    created_at: datetime


class EventPage(StrictModel):
    items: list[Event]
    next_cursor: int
    has_more: bool


class EnrollmentRequest(StrictModel):
    device_id: UUID
    display_name: str = Field(default='Yuri',min_length=2,max_length=80)
    master_totp_code: str = Field(min_length=6,max_length=6,pattern=r'^[0-9]{6}$',repr=False)


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=32,max_length=128,repr=False)


class SessionResponse(StrictModel):
    access_token: str
    refresh_token: str
    token_type: Literal['Bearer']
    expires_in: int
    refresh_expires_at: datetime
    operator_uid: str
