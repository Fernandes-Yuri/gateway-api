import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers.catalog import router
from orb_actions import ACTION_CATALOG
from orb_router import route_chat


class NutritionRetirementTest(unittest.TestCase):
    def test_old_routes_cannot_read_or_write_without_a_database(self):
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            for route in router.routes:
                path = route.path.replace("{food_name}", "arroz").replace("{slug}", "arroz")
                for method in route.methods:
                    with self.subTest(path=path, method=method):
                        response = client.request(method, path, content=b"invalid-body")
                        self.assertEqual(response.status_code, 410)
                        self.assertEqual(response.json()["code"], "nutrition_retired")
                        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("ABRIR_LISTA_COMPRAS", ACTION_CATALOG)

    def test_retirement_does_not_capture_medical_prescriptions_or_general_health_questions(self):
        self.assertIsNone(route_chat("quero revisar minhas receitas médicas"))
        self.assertIsNone(route_chat("quais hábitos você recomenda para a saúde?"))
        reply = route_chat("abre minha lista de compras")
        self.assertEqual(reply["acao"], "CONVERSA")
        self.assertIn("descontinuado", reply["fala"])
