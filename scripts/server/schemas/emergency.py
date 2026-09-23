from datetime import datetime

from pydantic import BaseModel


class EmergencyTokenGrant(BaseModel):
    emergency_token: str
    rescue_link: str
    expires_at: datetime
