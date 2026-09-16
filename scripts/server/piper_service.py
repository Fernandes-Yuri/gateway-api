"""Piper adapter: no FastAPI, account data or conversation rules."""
import asyncio
import os
import tempfile
from pathlib import Path


VOICES = {
    'faber': 'pt_BR-faber-medium.onnx',
    'cadu': 'pt_BR-cadu-medium.onnx',
    'edresson': 'pt_BR-edresson-low.onnx',
}


class PiperService:
    def __init__(self, binary, models_dir, *, concurrency=1, timeout=15, spawn=None):
        self.binary = str(binary)
        self.models_dir = Path(models_dir)
        self.timeout = timeout
        self._slots = asyncio.Semaphore(concurrency)
        self._spawn = spawn or asyncio.create_subprocess_exec

    @staticmethod
    def has_audio(path):
        try:
            with open(path, 'rb') as audio:
                header = audio.read(12)
            return header[:4] == b'RIFF' and header[8:12] == b'WAVE' and os.path.getsize(path) > 44
        except OSError:
            return False

    async def synthesize(self, text, voice, output_path):
        key = voice.lower().replace('piper:', '').replace('piper-', '').strip()
        model = self.models_dir / VOICES.get(key, VOICES['faber'])
        config = Path(str(model) + '.json')
        if not text.strip() or not Path(self.binary).is_file() or not model.is_file() or not config.is_file():
            return False
        output = Path(output_path)
        async with self._slots:
            # Repeat the cache check under the lock so identical concurrent requests
            # use the completed result instead of starting a second synthesis.
            if self.has_audio(output):
                return True
            proc = None
            temporary = None
            try:
                fd, temporary = tempfile.mkstemp(prefix='.piper-', suffix='.wav', dir=output.parent)
                os.close(fd)
                proc = await self._spawn(
                    self.binary, '--model', str(model), '--config', str(config), '--output_file', temporary,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(proc.communicate(input=text.encode('utf-8')), timeout=self.timeout)
                if proc.returncode != 0 or not self.has_audio(temporary):
                    return False
                os.replace(temporary, output)
                return True
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError):
                return False
            finally:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.shield(proc.wait())
                if temporary:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
