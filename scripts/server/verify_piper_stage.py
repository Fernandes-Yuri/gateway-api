"""Synthetic local synthesis; does not send audio to any user."""
import asyncio
import os
import tempfile
import time
import wave
from pathlib import Path
from piper_service import PiperService


async def main():
    service = PiperService(os.getenv('PIPER_BIN'),
                           os.getenv('PIPER_MODELS_DIR'))
    with tempfile.TemporaryDirectory(prefix='braga-piper-check-') as directory:
        output = Path(directory) / 'test.wav'
        start = time.monotonic()
        assert await service.synthesize('Olá! Eu sou o Braga. Este é um teste de voz.', 'faber', output)
        elapsed = time.monotonic() - start
        with wave.open(str(output), 'rb') as audio:
            duration = audio.getnframes() / audio.getframerate()
            assert duration > 0 and audio.getnchannels() >= 1
        assert await service.synthesize('Olá! Eu sou o Braga. Este é um teste de voz.', 'faber', output)
        assert not list(Path(directory).glob('.piper-*'))
        print(f'Piper real OK: {duration:.2f}s de áudio; síntese {elapsed:.2f}s; cache e limpeza OK.')


if __name__ == '__main__': asyncio.run(main())
