from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class PrescriptionCreate(BaseModel):
    image_url: Optional[str] = Field(default=None, max_length=500)
    issued_on: date
    validity_days: int = Field(ge=1, le=365)
    prescriber_name: str = Field(min_length=2, max_length=150)
    prescriber_crm: str = Field(min_length=4, max_length=32)


class MedicationCreate(BaseModel):
    ean_barcode: Optional[str] = Field(default=None, pattern=r"^\d{13}$")
    name: str = Field(min_length=2, max_length=255)
    active_principle: Optional[str] = Field(default=None, max_length=150)
    manufacturer: Optional[str] = Field(default=None, max_length=100)
    dosage_mg: Optional[float] = Field(default=None, gt=0, le=100000)
    pharmaceutical_form: Optional[str] = Field(default=None, max_length=100)
    schedule_times: list[time] = Field(min_length=1, max_length=12)
    total_units: int = Field(ge=0, le=10000)
    alert_threshold_days: int = Field(default=5, ge=1, le=30)
    photo_reference_url: Optional[str] = Field(default=None, max_length=500)
    confirmed_with_prescription: bool
    prescription: Optional[PrescriptionCreate] = None

    @field_validator("confirmed_with_prescription")
    @classmethod
    def prescription_must_be_confirmed(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("A conferência humana com a receita é obrigatória.")
        return value


class MedicationTakeRequest(BaseModel):
    scheduled_for: datetime
    units_taken: int = Field(default=1, ge=1, le=20)
    idempotency_key: str = Field(min_length=8, max_length=100)


class MedicationRestockRequest(BaseModel):
    """Reposição autoritativa de uma nova caixa/lote.

    ``new_total_units`` substitui o saldo atual pelo total conferido na caixa;
    a chave torna reenvios offline seguros entre Android, PWA e gateway.
    """
    new_total_units: int = Field(ge=1, le=10000)
    idempotency_key: str = Field(min_length=8, max_length=100)


class BarcodeMedication(BaseModel):
    ean_barcode: str
    name: str
    active_principle: Optional[str] = None
    concentration: Optional[str] = None
    pharmaceutical_form: Optional[str] = None
    manufacturer: Optional[str] = None
    farmacia_popular_eligible: Optional[bool] = None
    source_name: str
    source_url: Optional[str] = None
    source_checked_at: datetime


class BleTelemetryRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=3, max_length=128)
    device_type: str = Field(pattern=r"^(BLOOD_PRESSURE|GLUCOSE)$")
    measured_at: datetime
    systolic_pressure: Optional[int] = Field(default=None, ge=40, le=300)
    diastolic_pressure: Optional[int] = Field(default=None, ge=20, le=200)
    glucose_level: Optional[int] = Field(default=None, ge=20, le=1000)
    protocol: str = Field(default="GATT", pattern=r"^GATT$")
