"""Testes do vinculo WhatsApp<->conta por TOTP.

Rodar:  python -m pytest test_wa_link_totp.py -q   (ou unittest)
Nao depende de rede, Meta nem PostgreSQL: usa um pool falso e um send_text que
so grava o que foi dito.
"""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from wa_link_totp import (
    TOTP_STEP_SECONDS,
    current_totp_code,
    generate_totp_secret,
    is_six_digit_code,
    seconds_until_rotation,
    verify_totp_code,
)
from wa_link_service import (
    LINK_ATTEMPT_LIMIT,
    handle_link_attempt,
)


class FakeConn:
    """Simula uma conexao asyncpg: profiles + whatsapp_link_attempts em memoria."""

    def __init__(self, state):
        self.state = state

    async def execute(self, sql, *params):
        s = sql.strip().upper()
        if "INSERT INTO WHATSAPP_LINK_ATTEMPTS" in s:
            self.state["attempts"].append({
                "sender": params[0], "code": params[1],
                "user": params[2], "status": params[3],
            })
            return "INSERT 0 1"
        if "UPDATE PROFILES SET WHATSAPP_PHONE" in s:
            if "IS NULL" in s:
                # revoke: limpa vinculo do numero em outras contas
                for pid, prof in self.state["profiles"].items():
                    if prof.get("wa") == params[0] and pid != params[1]:
                        prof["wa"] = None
                return "UPDATE 0"
            # vincula
            self.state["profiles"][params[1]]["wa"] = params[0]
            return "UPDATE 1"
        if "SELECT COUNT" in s:
            n = sum(
                1 for a in self.state["attempts"]
                if a["sender"] == params[0] and a["status"] in ("WRONG_CODE", "BLOCKED")
            )
            row = MagicMock()
            row.__getitem__ = lambda self_, k: n if k == "n" else None
            return row
        if "SELECT ID, FULL_NAME FROM PROFILES WHERE REGEXP_REPLACE(WHATSAPP_PHONE" in s:
            for pid, prof in self.state["profiles"].items():
                if prof.get("wa") == params[0]:
                    row = MagicMock()
                    row.__getitem__ = lambda self_, k, _p=pid, _r=prof: (
                        _p if k == "id" else _r.get("name"))
                    return row
            return None
        if "SELECT ID, FULL_NAME, TOTP_SECRET FROM PROFILES" in s:
            rows = []
            for pid, prof in self.state["profiles"].items():
                if prof.get("totp"):
                    r = MagicMock()
                    r.__getitem__ = lambda self_, k, _p=pid, _r=prof: (
                        _p if k == "id" else _r.get("name") if k == "full_name"
                        else _r.get("totp"))
                    rows.append(r)
            return rows
        return None

    async def fetchrow(self, sql, *params):
        return await self.execute(sql, *params)

    async def fetch(self, sql, *params):
        return await self.execute(sql, *params)


class FakePool:
    def __init__(self, state):
        self.state = state

    def acquire(self):
        conn = FakeConn(self.state)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx


def _state_with_account(phone="5511987654321", name="Dona Maria"):
    secret = generate_totp_secret()
    return {
        "profiles": {"usuario-1": {"name": name, "totp": secret, "wa": None}},
        "attempts": [],
    }, secret


class TestTotpPrimitives(unittest.TestCase):

    def test_secret_is_base32_and_20_bytes(self):
        s = generate_totp_secret()
        self.assertEqual(len(s), 32)
        import base64
        self.assertEqual(len(base64.b32decode(s)), 20)

    def test_server_and_app_agree_on_current_code(self):
        s = generate_totp_secret()
        self.assertEqual(current_totp_code(s), current_totp_code(s))

    def test_verify_accepts_current_and_adjacent_steps(self):
        s = generate_totp_secret()
        now = time.time()
        self.assertTrue(verify_totp_code(s, current_totp_code(s, at=now), at=now))
        # D-WA1: tolera +/-1 janela (drift de relogio celular x servidor)
        old = current_totp_code(s, at=now - TOTP_STEP_SECONDS)
        self.assertTrue(verify_totp_code(s, old, at=now))
        nxt = current_totp_code(s, at=now + TOTP_STEP_SECONDS)
        self.assertTrue(verify_totp_code(s, nxt, at=now))
        # 2 janelas atras/adiante: ainda rejeitado
        self.assertFalse(verify_totp_code(s, current_totp_code(s, at=now - 2 * TOTP_STEP_SECONDS), at=now))
        self.assertFalse(verify_totp_code(s, current_totp_code(s, at=now + 2 * TOTP_STEP_SECONDS), at=now))
        self.assertFalse(verify_totp_code(s, "000000", at=now))

    def test_six_digit_helper(self):
        self.assertTrue(is_six_digit_code("123456"))
        self.assertFalse(is_six_digit_code("12345"))
        self.assertFalse(is_six_digit_code("1234567"))
        self.assertFalse(is_six_digit_code("abc123"))

    def test_seconds_until_rotation_bounded_by_window(self):
        self.assertTrue(0 < seconds_until_rotation() <= TOTP_STEP_SECONDS)


class TestLinkAttempt(unittest.TestCase):

    def _run(self, state, text, phone="5511987654321"):
        sent = []

        async def fake_send(dest, msg):
            sent.append((dest, msg))

        pool = FakePool(state)
        consumed = asyncio.run(
            handle_link_attempt(phone, text, pool, fake_send)
        )
        return consumed, sent

    def test_correct_code_links_account(self):
        state, secret = _state_with_account()
        code = current_totp_code(secret)
        consumed, sent = self._run(state, code)
        self.assertTrue(consumed)
        self.assertEqual(state["profiles"]["usuario-1"]["wa"], "5511987654321")
        self.assertIn("Tudo certo", sent[0][1])
        self.assertEqual(state["attempts"][-1]["status"], "SUCCESS")

    def test_wrong_code_replies_and_logs(self):
        state, _ = _state_with_account()
        consumed, sent = self._run(state, "000000")
        self.assertTrue(consumed)
        self.assertIn("não é válido", sent[0][1])
        self.assertEqual(state["attempts"][-1]["status"], "WRONG_CODE")
        self.assertIsNone(state["profiles"]["usuario-1"]["wa"])

    def test_message_must_say_to_go_back_to_app(self):
        state, _ = _state_with_account()
        _, sent = self._run(state, "000000")
        self.assertIn("Volte no aplicativo", sent[0][1])

    def test_far_expired_code_is_rejected(self):
        state, secret = _state_with_account()
        old = current_totp_code(secret, at=time.time() - 2 * TOTP_STEP_SECONDS)
        consumed, sent = self._run(state, old)
        self.assertTrue(consumed)
        self.assertIn("não é válido", sent[0][1])

    def test_vincular_prefix_is_recognized(self):
        state, secret = _state_with_account()
        code = current_totp_code(secret)
        consumed, _ = self._run(state, f"Vincular Braga Saúde {code}")
        self.assertTrue(consumed)
        self.assertEqual(state["profiles"]["usuario-1"]["wa"], "5511987654321")

    def test_cap_blocks_after_limit(self):
        state, _ = _state_with_account()
        for _ in range(LINK_ATTEMPT_LIMIT):
            self._run(state, "000000")
        consumed, sent = self._run(state, "000000")
        self.assertTrue(consumed)
        self.assertIn("segurança", sent[0][1])
        self.assertEqual(state["attempts"][-1]["status"], "BLOCKED")

    def test_already_linked_account_reassures(self):
        state, _ = _state_with_account()
        state["profiles"]["usuario-1"]["wa"] = "5511987654321"
        consumed, sent = self._run(state, "000000")
        self.assertTrue(consumed)
        self.assertIn("já está vinculado", sent[0][1])

    def test_number_conflict_is_reported(self):
        # O numero ja consta vinculado a outra conta: o servico responde com
        # reasseguracao em vez de roubar o vinculo silenciosamente.
        state, _ = _state_with_account()
        state["profiles"]["outra"] = {"name": "Seu João", "totp": None, "wa": "5511987654321"}
        secret = state["profiles"]["usuario-1"]["totp"]
        consumed, sent = self._run(state, current_totp_code(secret))
        self.assertTrue(consumed)
        self.assertIn("já está vinculado", sent[0][1])
        # numero nao foi movido: continua com a conta original
        self.assertEqual(state["profiles"]["outra"]["wa"], "5511987654321")
        self.assertIsNone(state["profiles"]["usuario-1"]["wa"])

    def test_normal_message_not_consumed(self):
        state, _ = _state_with_account()
        consumed, sent = self._run(state, "Bom dia! Tudo bem?")
        self.assertFalse(consumed)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
