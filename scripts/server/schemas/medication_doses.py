"""Contrato v1 de doses e lotes. IDs de intenção devem sobreviver à reconexão."""
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


class DoseSlot(StrictRequest):
    local_time: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    dose_units: int | None = Field(default=None, ge=1, le=20, strict=True)
    quantity_confirmed: bool = False

    @model_validator(mode='after')
    def confirmed_quantity(self):
        if self.quantity_confirmed and self.dose_units is None:
            raise ValueError('Quantidade confirmada exige dose_units explícita.')
        return self


class DoseScheduleUpdate(StrictRequest):
    expected_revision: int = Field(ge=1, strict=True)
    slots: list[DoseSlot] = Field(min_length=1, max_length=12)

    @field_validator('slots')
    @classmethod
    def unique_times(cls, slots):
        if len({s.local_time for s in slots}) != len(slots):
            raise ValueError('Horários duplicados.')
        return slots


class DoseScheduleResponse(BaseModel):
    medication_batch_contract: Literal[1] = 1
    medication_id: UUID
    schedule_revision: int
    slots: list[DoseSlot]


class DoseBatchPrepare(StrictRequest):
    client_batch_id: UUID
    group_id: UUID
    expected_schedule_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    occurrence_ids: list[UUID] = Field(min_length=1, max_length=200)
    channel: Literal['app']

    @field_validator('occurrence_ids')
    @classmethod
    def unique_occurrences(cls, values):
        if len(set(values)) != len(values):
            raise ValueError('Ocorrências duplicadas.')
        return values


class DoseBatchConfirm(StrictRequest):
    confirmation_id: UUID
    snapshot_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    selected_occurrence_ids: list[UUID] = Field(min_length=1, max_length=200)
    declared_taken_at: datetime | None = None

    @field_validator('selected_occurrence_ids')
    @classmethod
    def unique_selection(cls, values):
        return DoseBatchPrepare.unique_occurrences(values)

    @field_validator('declared_taken_at')
    @classmethod
    def aware_declared_time(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('declared_taken_at deve conter fuso.')
        return value


class DoseItem(BaseModel):
    occurrence_id: UUID
    medication_id: UUID
    name: str
    dose_units: int | None
    instruction_text: str | None
    schedule_revision: int
    scheduled_at: datetime
    state: Literal['pending', 'recorded', 'blocked']
    reason_code: str | None = None
    status: Literal['recorded', 'already_recorded', 'excluded', 'blocked'] | None = None
    log_id: UUID | None = None
    current_units: int | None = None
    stock_decremented: bool | None = None
    recorded_units: int | None = None


class DoseGroup(BaseModel):
    group_id: UUID
    local_time: str
    scheduled_at: datetime
    schedule_digest: str
    counts: dict[str, int]
    can_confirm_all: bool
    items: list[DoseItem]


class DoseGroupsResponse(BaseModel):
    medication_batch_contract: Literal[1] = 1
    patient_id: str
    patient_name: str
    local_date: date
    timezone: str
    timezone_source: Literal['profile', 'fallback']
    generated_at: datetime
    groups: list[DoseGroup]
    review_items: list[dict] = Field(default_factory=list)


class DoseBatchResponse(BaseModel):
    medication_batch_contract: Literal[1] = 1
    batch_id: UUID
    patient_id: str
    actor_id: str
    channel: Literal['app']
    state: Literal['prepared', 'confirmed', 'partially_confirmed', 'failed', 'cancelled', 'expired']
    prepared_at: datetime
    expires_at: datetime
    snapshot_digest: str
    timezone: str
    local_date: date
    local_time: str
    items: list[DoseItem]
    counts: dict[str, int]
    requires_action: bool


class DoseErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    items: list[dict] = Field(default_factory=list)


class DoseError(BaseModel):
    detail: DoseErrorDetail
