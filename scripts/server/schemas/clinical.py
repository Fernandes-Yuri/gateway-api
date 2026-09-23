from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SymptomCheckInCreate(BaseModel):
    patient_id: Optional[str] = Field(default=None, max_length=128)
    reported_at: datetime
    symptoms_text: str = Field(min_length=1, max_length=1000)
    sleep_quality: Optional[int] = Field(default=None, ge=1, le=5)
    disposition: Optional[int] = Field(default=None, ge=1, le=5)
    input_method: str = Field(default="VOICE", pattern=r"^(VOICE|TEXT)$")


class ClinicalCorrelation(BaseModel):
    event_at: datetime
    event_type: str
    observation: str
    evidence: list[str]
    window_hours: int = 24
    interpretation: str = "TEMPORAL_ASSOCIATION_NOT_DIAGNOSIS"
