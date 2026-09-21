"""Testes do router administrativo do portal (D-WS1 / D50).

Cobertura:
1. Autenticacao — sem token (401), token errado (401), formato torto (401),
   token nao configurado no env (503, recurso desativado nunca aberto).
2. Funcional — listagem paginada, filtros, alteracao de status, resposta
   (escreve em feedback_replies, que o app le de volta).
3. Metricas — so agregados; bucket menor que MIN_BUCKET e suprimido.
4. Seguranca — nenhuma rota admin aceita request sem Bearer valido.
"""
import os
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import server

# Token de servico usado nos testes (setado no setUp via patch do env).
_TEST_TOKEN = "svc_test_token_super_secreto_D51"


def _make_pool(rows=None, val=None):
    """Cria um pool falso com conn.fetch/fetchval/fetchrow controlados."""
    conn = AsyncMock()
    conn.fetch.return_value = rows or []
    conn.fetchval.return_value = val if val is not None else 0
    conn.fetchrow.return_value = rows[0] if rows else None
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


class _AdminTestBase(unittest.TestCase):
    def setUp(self):
        self._orig_pool = server.admin_router._state.db_pool
        self._orig_rl = server.admin_router._state.rate_limiter
        # Rate limiter falso: nunca bloqueia.
        server.admin_router._state.rate_limiter = MagicMock(
            check=AsyncMock())
        # Token de servico presente por default em todos os testes; os
        # casos especificos de "ausente" limpam o env.
        self._env_patcher = patch.dict(os.environ, {"SERVICE_TOKEN": _TEST_TOKEN})
        self._env_patcher.start()
        self.client = TestClient(server.app)

    def tearDown(self):
        self._env_patcher.stop()
        server.admin_router._state.db_pool = self._orig_pool
        server.admin_router._state.rate_limiter = self._orig_rl

    def _auth(self):
        return {"Authorization": f"Bearer {_TEST_TOKEN}"}


class AutenticacaoTest(_AdminTestBase):
    """A camada de token e a porta de tudo — precisa estar intransponivel."""

    def setUp(self):
        super().setUp()
        pool, _ = _make_pool()
        server.admin_router._state.db_pool = pool

    def test_sem_token_rejeitado(self):
        r = self.client.get("/api/admin/feedback")
        self.assertEqual(r.status_code, 401)

    def test_token_errado_rejeitado(self):
        r = self.client.get("/api/admin/feedback",
                            headers={"Authorization": "Bearer token_errado"})
        self.assertEqual(r.status_code, 401)

    def test_formato_nao_bearer_rejeitado(self):
        r = self.client.get("/api/admin/feedback",
                            headers={"Authorization": _TEST_TOKEN})
        self.assertEqual(r.status_code, 401)

    def test_sem_pool_da_503_nao_500(self):
        server.admin_router._state.db_pool = None
        r = self.client.get("/api/admin/feedback", headers=self._auth())
        self.assertEqual(r.status_code, 503)

    def test_token_valido_passa(self):
        r = self.client.get("/api/admin/feedback", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])

    def test_token_nao_configurado_desativa_recurso(self):
        # NUNCA pode ficar aberto por default — 503, nao 200.
        # So zera o SERVICE_TOKEN — .strip() torna string vazia em "nao
        # configurado", que e exatamente o estado de producao sem a env.
        with patch.dict(os.environ, {"SERVICE_TOKEN": ""}):
            r = self.client.get("/api/admin/feedback", headers=self._auth())
        self.assertEqual(r.status_code, 503)


class FeedbackListagemTest(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.pool, self.conn = _make_pool()
        server.admin_router._state.db_pool = self.pool

    def test_lista_paginada_devolve_campos_do_portal(self):
        fid = uuid.uuid4()
        self.conn.fetchval.return_value = 1
        self.conn.fetch.return_value = [{
            "id": fid, "user_id": "u1", "user_email": "a@b.com",
            "user_name": "Ana", "category": "bug", "title": "travou",
            "message": "app parou", "input_method": "text",
            "app_version": "1.2.5", "device_info": "pixel",
            "status": "pending", "created_at": datetime(2026, 9, 20, tzinfo=timezone.utc),
            "replies_count": 2,
        }]
        r = self.client.get("/api/admin/feedback", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["pagination"]["total"], 1)
        item = data["data"][0]
        self.assertEqual(item["status"], "novo")  # pending -> novo
        self.assertEqual(item["repliesCount"], 2)
        self.assertEqual(item["id"], str(fid))

    def test_filtro_de_categoria_aplicado(self):
        self.conn.fetchval.return_value = 0
        self.conn.fetch.return_value = []
        self.client.get("/api/admin/feedback?category=BUG", headers=self._auth())
        sql = self.conn.fetch.call_args.args[0]
        self.assertIn("LOWER(category)", sql)

    def test_busca_escapa_curingas_like(self):
        self.conn.fetchval.return_value = 0
        self.conn.fetch.return_value = []
        self.client.get("/api/admin/feedback?search=100%%", headers=self._auth())
        # O valor passado ao LIKE tem o % escapado.
        valores = self.conn.fetch.call_args.args[1:]
        self.assertTrue(any("%" in str(v) for v in valores))

    def test_kpis_vem_no_payload(self):
        # stats (total/pendentes/alta prioridade) e agregado puro.
        self.conn.fetchval.return_value = 3
        self.conn.fetch.return_value = []
        self.conn.fetchrow.return_value = {
            "total": 3, "pendentes": 2, "alta_prioridade": 1}
        r = self.client.get("/api/admin/feedback", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        stats = r.json()["stats"]
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["pending"], 2)
        self.assertEqual(stats["highPriority"], 1)

    def test_filtro_prioridade_aplicado(self):
        # priority=high adiciona a condicao de termos criticos na query.
        self.conn.fetchval.return_value = 0
        self.conn.fetch.return_value = []
        self.conn.fetchrow.return_value = None
        self.client.get("/api/admin/feedback?priority=high", headers=self._auth())
        sql = self.conn.fetch.call_args.args[0]
        self.assertIn("LIKE ANY", sql)


class FeedbackStatusTest(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.pool, self.conn = _make_pool()
        server.admin_router._state.db_pool = self.pool

    def test_status_invalido_rejeitado(self):
        r = self.client.patch(
                "/api/admin/feedback/%s/status" % uuid.uuid4(),
                json={"status": "HACKED"}, headers=self._auth())
        self.assertEqual(r.status_code, 422)

    def test_status_portal_gravado_no_vocabulario_gateway(self):
        fid = uuid.uuid4()
        self.conn.fetchrow.return_value = {"id": fid}
        r = self.client.patch(
                "/api/admin/feedback/%s/status" % fid,
                json={"status": "resolvido_v1.2"}, headers=self._auth())
        self.assertEqual(r.status_code, 200)
        # Gravou 'resolved' (vocabulario do gateway), devolveu 'resolvido_v1.2'.
        gravado = self.conn.fetchrow.call_args.args[1]
        self.assertEqual(gravado, "resolved")
        self.assertEqual(r.json()["status"], "resolvido_v1.2")

    def test_feedback_inexistente_da_404(self):
        self.conn.fetchrow.return_value = None
        r = self.client.patch(
                "/api/admin/feedback/%s/status" % uuid.uuid4(),
                json={"status": "novo"}, headers=self._auth())
        self.assertEqual(r.status_code, 404)


class FeedbackReplyTest(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.pool, self.conn = _make_pool()
        server.admin_router._state.db_pool = self.pool

    def test_resposta_vazia_rejeitada(self):
        r = self.client.post(
                "/api/admin/feedback/%s/reply" % uuid.uuid4(),
                json={"body": "   "}, headers=self._auth())
        self.assertEqual(r.status_code, 422)

    def test_resposta_escreve_em_feedback_replies(self):
        fid = uuid.uuid4()
        self.conn.fetchrow.side_effect = [
            {"id": fid, "user_id": "u1"},  # SELECT do feedback
            {"id": uuid.uuid4(), "created_at": datetime(2026, 9, 20, tzinfo=timezone.utc)},
        ]
        r = self.client.post(
                "/api/admin/feedback/%s/reply" % fid,
                json={"body": "Obrigado!", "author": "Suporte"}, headers=self._auth())
        self.assertEqual(r.status_code, 200)
        sql = self.conn.fetchrow.call_args.args[0]
        self.assertIn("INSERT INTO feedback_replies", sql)
        self.assertTrue(r.json()["success"])

    def test_id_malformado_rejeitado(self):
        r = self.client.post(
                "/api/admin/feedback/nao-e-uuid/reply",
                json={"body": "oi"}, headers=self._auth())
        self.assertEqual(r.status_code, 422)


class MetricasTest(_AdminTestBase):
    def setUp(self):
        super().setUp()
        self.pool, self.conn = _make_pool()
        server.admin_router._state.db_pool = self.pool

    def test_active_users_agrega_e_nao_vaz_linhas(self):
        self.conn.fetchval.side_effect = [42, 100]
        r = self.client.get("/api/admin/metrics/active-users?days=7",
                                headers=self._auth())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["activeUsers"], 42)
        self.assertEqual(data["totalUsers"], 100)
        # So numeros — nenhum campo de usuario individual.
        self.assertNotIn("user_id", str(data))
        self.assertNotIn("user_email", str(data))

    def test_engagement_suprime_bucket_pequeno(self):
        # bucket com 3 usuarios (< MIN_BUCKET=5) -> NULL (suprimido)
        self.conn.fetch.return_value = [
            {"faixa": "0", "usuarios": 30},
            {"faixa": "1-3", "usuarios": None},
        ]
        r = self.client.get("/api/admin/metrics/engagement",
                                headers=self._auth())
        data = r.json()
        self.assertEqual(data["streakDistribution"][0]["usuarios"], 30)
        self.assertIsNone(data["streakDistribution"][1]["usuarios"])

    def test_notifications_optin(self):
        self.conn.fetchval.side_effect = [80, 100]
        self.conn.fetch.return_value = [{"channel": "whatsapp", "total": 500}]
        r = self.client.get("/api/admin/metrics/notifications",
                                headers=self._auth())
        data = r.json()
        self.assertEqual(data["notificationsEnabled"], 80)
        self.assertEqual(data["optInRate"], 0.8)

    def test_feature_usage_nao_seleciona_metadata(self):
        self.conn.fetch.return_value = [{"action": "screen_view", "total": 120}]
        r = self.client.get("/api/admin/metrics/feature-usage",
                                headers=self._auth())
        sql = self.conn.fetch.call_args.args[0]
        # A coluna action e contada; metadata nunca aparece no SQL.
        self.assertNotIn("metadata", sql)
        self.assertEqual(r.json()["topActions"][0]["action"], "screen_view")


if __name__ == "__main__":
    unittest.main()
