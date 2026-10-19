"""Socorro público acessível sem importar o atendimento WhatsApp."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import emergency
from services.emergency_page import emergency_page


class PageTests(unittest.TestCase):
    def test_public_page_has_real_links_in_samu_first_order_and_no_auto_dial(self):
        app = FastAPI()
        app.include_router(emergency.router)
        with TestClient(app) as client:
            response = client.get('/socorro')
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('lang="pt-BR"', html)
        self.assertIn('href="tel:192"', html)
        self.assertIn('Buscar UPA no Maps', html)
        self.assertLess(html.index('href="tel:192"'), html.index('href="https://www.google.com/maps'))
        self.assertNotIn('<script', html)
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertEqual(html, emergency_page())


