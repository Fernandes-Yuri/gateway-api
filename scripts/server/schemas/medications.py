from datetime import date, datetime, time
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def sanitize_anti_null(v: Any) -> Any:
    """Sanitiza strings nulas/vazias ('null', 'none', 'undefined', '') para None."""
    if v is None:
        return None
    if isinstance(v, str):
        cleaned = v.strip()
        if cleaned.lower() in ("", "null", "none", "undefined"):
            return None
        return cleaned
    return v


class PrescriptionCreate(BaseModel):
    image_url: Optional[str] = Field(default=None, max_length=500)
    issued_on: date
    validity_days: int = Field(ge=1, le=365)
    prescriber_name: str = Field(min_length=2, max_length=150)
    prescriber_crm: str = Field(min_length=4, max_length=32)

    @field_validator("image_url", mode="before")
    @classmethod
    def sanitize_optional_fields(cls, v):
        return sanitize_anti_null(v)


class MedicationCreate(BaseModel):
    ean_barcode: Optional[str] = Field(default=None, pattern=r"^\d{13}$")
    anvisa_registration_number: Optional[str] = Field(default=None, max_length=32)
    name: str = Field(min_length=2, max_length=255)
    active_principle: Optional[str] = Field(default=None, max_length=150)
    manufacturer: Optional[str] = Field(default=None, max_length=100)
    dosage_mg: Optional[float] = Field(default=None, gt=0, le=100000)
    pharmaceutical_form: Optional[str] = Field(default=None, max_length=100)
    frequency_interval_hours: Optional[int] = Field(default=None, ge=1, le=48)
    treatment_duration_days: Optional[int] = Field(default=None, ge=1, le=365)
    meal_context: Optional[str] = Field(default=None)
    schedule_times: list[time] = Field(min_length=1, max_length=12)
    total_units: int = Field(ge=0, le=10000)
    alert_threshold_days: int = Field(default=5, ge=1, le=30)
    photo_reference_url: Optional[str] = Field(default=None, max_length=500)
    confirmed_with_prescription: bool
    prescription: Optional[PrescriptionCreate] = None

    @field_validator(
        "ean_barcode",
        "anvisa_registration_number",
        "active_principle",
        "manufacturer",
        "dosage_mg",
        "pharmaceutical_form",
        "frequency_interval_hours",
        "treatment_duration_days",
        "meal_context",
        "photo_reference_url",
        mode="before",
    )
    @classmethod
    def sanitize_optional_fields(cls, v):
        return sanitize_anti_null(v)

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
    anvisa_registration_number: Optional[str] = None
    source_name: str
    source_url: Optional[str] = None
    source_checked_at: datetime

    @field_validator(
        "active_principle",
        "concentration",
        "pharmaceutical_form",
        "manufacturer",
        "anvisa_registration_number",
        "source_url",
        mode="before",
    )
    @classmethod
    def sanitize_optional_fields(cls, v):
        return sanitize_anti_null(v)


class BleTelemetryRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=3, max_length=128)
    device_type: str = Field(pattern=r"^(BLOOD_PRESSURE|GLUCOSE)$")
    measured_at: datetime
    systolic_pressure: Optional[int] = Field(default=None, ge=40, le=300)
    diastolic_pressure: Optional[int] = Field(default=None, ge=20, le=200)
    glucose_level: Optional[int] = Field(default=None, ge=20, le=1000)
    protocol: str = Field(default="GATT", pattern=r"^GATT$")


class AnalyzedMedicationItem(BaseModel):
    name: str = Field(default="")
    name_divergent: bool = False
    dosage: str = Field(default="")
    dosage_divergent: bool = False
    dosage_mg: Optional[float] = None
    frequency: str = Field(default="")
    frequency_divergent: bool = False
    frequency_interval_hours: Optional[int] = Field(default=None, ge=1, le=48)
    daily_doses_count: int = Field(default=1, ge=1, le=12)
    treatment_duration_days: Optional[int] = Field(default=None, ge=1, le=365)
    meal_context: Optional[str] = Field(default=None)
    suggested_times: list[str] = Field(default_factory=list)
    ean_barcode: Optional[str] = None
    anvisa_registration_number: Optional[str] = Field(default=None, max_length=32)
    active_principle: Optional[str] = None
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_human_fill: bool = False
    divergence_reason: Optional[str] = None
    source_pages: list[int] = Field(default_factory=list)

    @field_validator(
        "dosage_mg",
        "frequency_interval_hours",
        "treatment_duration_days",
        "meal_context",
        "ean_barcode",
        "anvisa_registration_number",
        "active_principle",
        "divergence_reason",
        mode="before",
    )
    @classmethod
    def sanitize_optional_fields(cls, v):
        return sanitize_anti_null(v)


class PrescriptionAnalysisResponse(BaseModel):
    status: str = "success"
    prescription_image_url: Optional[str] = None
    medications: list[AnalyzedMedicationItem] = Field(default_factory=list)
    raw_ocr_snippet: Optional[str] = None
    total_pages_analyzed: int = 1
    duplicates_merged_count: int = 0

    @field_validator("prescription_image_url", "raw_ocr_snippet", mode="before")
    @classmethod
    def sanitize_optional_fields(cls, v):
        return sanitize_anti_null(v)


class MedicationBatchCreate(BaseModel):
    """Remessa de múltiplos medicamentos para gravação atômica."""
    medications: list[MedicationCreate] = Field(min_length=1, max_length=20)


class MedicationBatchItemResult(BaseModel):
    id: str
    name: str
    confirmed_with_prescription: bool = True


class MedicationBatchResponse(BaseModel):
    status: str = "success"
    patient_id: str
    created_count: int
    medications: list[MedicationBatchItemResult]


