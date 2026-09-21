"""Router de sintese vocal (TTS) — voz do assistente.

Extraido do monolito server.py na Fase A passo 3
(doc 11_PLANO_MODULARIZACAO_E_TTS.md). Dominios: /synthesize_voice,
/v1/audio/speech (GET+POST).

Engine: Piper TTS (pt_BR-faber-medium) em processo na EC2. A decisao do
fundador (20/09/2026) mantem o Piper como voz unica; a frente de
substituicao por voz mais humana fica arquivada (doc 11 §4).
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
    # TTS anonimo com texto ilimitado = exaustao de CPU/disco (DoS).
    input_text = (req.text if req else text) or ""
    input_voice = (req.voice if req else voice) or "faber"
    lower_voice = input_voice.lower()
    if "cadu" in lower_voice:
        chosen_voice = "cadu"
    elif "edresson" in lower_voice:
        chosen_voice = "edresson"
    else:
        chosen_voice = "faber"

    clean = clean_text_for_tts(input_text)
    if not clean:
        raise HTTPException(status_code=400, detail="Texto vazio ou inválido para síntese vocal")
    # Limite de tamanho por sintese.
    if len(clean) > 3000:
        raise HTTPException(status_code=413, detail="Texto excede o limite de 3000 caracteres.")

    hash_key = hashlib.md5(f"{chosen_voice}_{clean}".encode("utf-8")).hexdigest()
    audio_path = os.path.join(AUDIO_CACHE_DIR, f"{hash_key}.wav")

    piper = _state.piper_service
    if piper and piper.has_audio(audio_path):
        return FileResponse(audio_path, media_type="audio/wav")

    ok = await piper.synthesize(clean, chosen_voice, audio_path) if piper else False
    if ok:
        return FileResponse(audio_path, media_type="audio/wav")
    raise HTTPException(status_code=500, detail="Erro ao sintetizar áudio via Piper TTS local")


class _AppState:
    """Porta do PiperService (estado global do server). Preenchida por
    server.py no startup."""

    piper_service = None


_state = _AppState()


def configure_state(*, piper_service=None):
    """Chamado por server.py ao subir a aplicacao."""
    _state.piper_service = piper_service
