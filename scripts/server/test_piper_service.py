import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from piper_service import PiperService

WAV = b'RIFF' + b'\x00' * 4 + b'WAVE' + b'\x00' * 40


class PiperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.binary = self.root / 'piper'
        self.binary.touch()
        (self.root / 'pt_BR-faber-medium.onnx').touch()
        (self.root / 'pt_BR-faber-medium.onnx.json').touch()
        self.output = self.root / 'output.wav'

    def service(self, spawn, timeout=1):
        return PiperService(self.binary, self.root, spawn=spawn, timeout=timeout)

    async def test_atomic_publication_and_duplicate_request(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def spawn(*args, **kwargs):
            target = Path(args[args.index('--output_file') + 1])
            proc = AsyncMock(returncode=None)
            async def communicate(**_):
                target.write_bytes(b'RIFF')
                started.set()
                await release.wait()
                target.write_bytes(WAV)
                proc.returncode = 0
            proc.communicate.side_effect = communicate
            return proc
        factory = AsyncMock(side_effect=spawn)
        service = self.service(factory)
        first = asyncio.create_task(service.synthesize('Olá', 'faber', self.output))
        await started.wait()
        second = asyncio.create_task(service.synthesize('Olá', 'faber', self.output))
        self.assertFalse(self.output.exists())
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [True, True])
        factory.assert_awaited_once()
        self.assertEqual(self.output.read_bytes(), WAV)
        self.assertEqual(list(self.root.glob('.piper-*')), [])

    async def test_cancellation_reaps_child_and_removes_partial_audio(self):
        started = asyncio.Event()
        proc = AsyncMock(returncode=None)
        def kill(): proc.returncode = -9
        proc.kill = kill
        async def communicate(**_):
            started.set()
            await asyncio.Event().wait()
        proc.communicate.side_effect = communicate
        service = self.service(AsyncMock(return_value=proc))
        task = asyncio.create_task(service.synthesize('Olá', 'faber', self.output))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        proc.wait.assert_awaited_once()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.piper-*')), [])

    async def test_timeout_reaps_child(self):
        proc = AsyncMock(returncode=None)
        def kill(): proc.returncode = -9
        proc.kill = kill
        async def communicate(**_): await asyncio.Event().wait()
        proc.communicate.side_effect = communicate
        result = await self.service(AsyncMock(return_value=proc), timeout=.01).synthesize('Olá', 'faber', self.output)
        self.assertFalse(result)
        proc.wait.assert_awaited_once()

    async def test_missing_config_does_not_spawn(self):
        (self.root / 'pt_BR-faber-medium.onnx.json').unlink()
        spawn = AsyncMock()
        self.assertFalse(await self.service(spawn).synthesize('Olá', 'faber', self.output))
        spawn.assert_not_awaited()

    async def test_invalid_output_is_not_published(self):
        proc = AsyncMock(returncode=0)
        self.assertFalse(await self.service(AsyncMock(return_value=proc)).synthesize('Olá', 'faber', self.output))
        self.assertFalse(self.output.exists())


if __name__ == '__main__': unittest.main()
