from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class CareAuditEvent(BaseModel):
    id: str
    patient_id: str
    actor_id: str
    actor_name: str
    action_type: str
    occurred_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class DailyBulletin(BaseModel):
    patient_id: str
    day: date
    medications_taken: int
    medications_expected: int
    latest_blood_pressure: Optional[str] = None
    hydration_ml: int = 0
    privacy_scope: str = "CONSOLIDATED_SAFETY_ONLY"
