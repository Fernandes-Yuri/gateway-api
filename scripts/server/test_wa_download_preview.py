"""Texto da Cloud API solicita prévia do download, sem contato com Meta."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import whatsapp_service as transport
from services.wa_download_links import DOWNLOAD_URL, has_download_link


class LinkTests(unittest.TestCase):
    def test_download_addresses_and_punctuation(self):
        for link in (DOWNLOAD_URL, 'https://bragasaude.online',
                     'https://bragasaude.online/app-download.html',
                     'https://api.bragasaude.online/api/app/download',
                     'https://www.bragasaude.online/?utm_source=wa'):
            with self.subTest(link=link):
                self.assertTrue(has_download_link(f'Baixe aqui: {link}.'))
        for link in ('https://bragasaude.online/termos.html',
                     'https://bragasaude.online/privacidade.html',
                     'https://api.bragasaude.online/socorro',
                     'https://bragasaude.online.evil.invalid/',
                     'https://example.invalid/app-download.html'):
            with self.subTest(link=link):
                self.assertFalse(has_download_link(link))


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_enables_preview_for_download_only_and_preserves_body(self):
        client = MagicMock()
        client.post = AsyncMock(return_value=MagicMock(status_code=200))
        factory = MagicMock()
        factory.return_value.__aenter__.return_value = client
        with patch.object(transport.httpx, 'AsyncClient', factory):
            for text, expected in ((f'Baixe para Android: {DOWNLOAD_URL}', True),
                                   ('https://bragasaude.online/app-download.html', True),
                                   ('https://api.bragasaude.online/api/app/download', True),
                                   ('Consulte https://bragasaude.online/privacidade.html', False),
                                   ('Sua pressão está registrada.', False)):
                await transport.send_whatsapp_text('5500000000000', text)
                sent = client.post.await_args.kwargs['json']['text']
                self.assertIs(sent['preview_url'], expected)
                self.assertEqual(sent['body'], text)
            await transport.send_whatsapp_text('5500000000000', 'https://example.invalid', preview_url=True)
            self.assertTrue(client.post.await_args.kwargs['json']['text']['preview_url'])
