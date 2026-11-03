"""Router de síntese vocal legado (aposentado).

A síntese vocal no servidor foi completamente desativada.
O aplicativo Android utiliza exclusivamente o TextToSpeech nativo do sistema operacional.
Estes endpoints retornam HTTP 410 Gone imediatamente sem leitura de disco ou processamento.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class VoiceRequest(BaseModel):
    text: Optional[str] = None
    voice: Optional[str] = None
    rate: Optional[str] = None
    volume: Optional[str] = None


@router.post("/synthesize_voice")
@router.get("/v1/audio/speech")
@router.post("/v1/audio/speech")
async def synthesize_voice(
    req: Optional[VoiceRequest] = None,
    text: Optional[str] = None,
    voice: Optional[str] = None,
):
    raise HTTPException(
        status_code=410,
        detail="Síntese vocal desativada no servidor: migrada para TextToSpeech nativo no aplicativo Android.",
    )


def configure_state():
    """Chamado por server.py ao subir a aplicação."""
    pass
