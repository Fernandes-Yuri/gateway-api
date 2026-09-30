"""Router de sintese vocal (TTS).

A sintese neural foi migrada 100% para On-Device no aplicativo Android (Sherpa-ONNX + VITS),
com fallback para o TextToSpeech do sistema operacional.
Estes endpoints permanecem para servir audios pre-gerados em cache ou retornar 410 Gone.
"""
import hashlib
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api_identity import firebase_uid
from config import AUDIO_CACHE_DIR
from core.text import clean_text_for_tts

router = APIRouter()


class VoiceRequest(BaseModel):
    text: str
    voice: Optional[str] = "pt-BR-FaberNeural"
    rate: Optional[str] = "-2%"
    volume: Optional[str] = "+20%"


@router.post("/synthesize_voice")
@router.get("/v1/audio/speech")
@router.post("/v1/audio/speech")
async def synthesize_voice(
    req: Optional[VoiceRequest] = None,
    text: Optional[str] = None,
    voice: Optional[str] = "faber",
    uid: str = Depends(firebase_uid),
):
    input_text = (req.text if req else text) or ""
    clean = clean_text_for_tts(input_text)
    if not clean:
        raise HTTPException(status_code=400, detail="Texto vazio ou inválido para síntese vocal")

    # Se existir em cache em disco, serve o arquivo
    hash_key = hashlib.md5(f"faber_{clean}".encode("utf-8")).hexdigest()
    audio_path = os.path.join(AUDIO_CACHE_DIR, f"{hash_key}.wav")
    if os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0:
        return FileResponse(audio_path, media_type="audio/wav")

    # Sintese em runtime na EC2 desativada (migrada para on-device)
    raise HTTPException(
        status_code=410,
        detail="Síntese vocal desativada no servidor: migrada para On-Device no aplicativo Android."
    )


def configure_state():
    """Chamado por server.py ao subir a aplicacao."""
    pass

