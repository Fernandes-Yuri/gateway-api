from datetime import datetime

from pydantic import BaseModel, Field


class MedicalAccessGenerateRequest(BaseModel):
    purpose: str = Field(default="CONSULTATION", pattern=r"^CONSULTATION$")


class MedicalAccessGrant(BaseModel):
    access_token: str
    magic_link: str
    qr_code_payload: str
    expires_at: datetime
    expires_in_seconds: int = 7200
