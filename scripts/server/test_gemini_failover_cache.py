"""Testes da arquitetura Gemini Primário + Groq Fallback com Cache Global, Sonda Calma e Alerta Sentinela."""
import asyncio
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import httpx

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from services import circuit_breaker as cb_mod  # noqa: E402
from services.circuit_breaker import (  # noqa: E402
    CircuitBreaker,
    CircuitBreakerOpenException,
    CircuitState,
    gemini_circuit_breaker,
    post_openai_chat_with_failover,
)


class _DummyClient:
    """Cliente async simulado com comportamento distinto para Gemini vs Groq."""

    def __init__(self, gemini_handler, groq_handler):
        self.gemini_handler = gemini_handler
        self.groq_handler = groq_handler
        self.gemini_calls = 0
        self.groq_calls = 0

    async def post(self, url, **kwargs):
        if "generativelanguage.googleapis.com" in url:
            self.gemini_calls += 1
            return await self.gemini_handler(url, **kwargs)
        self.groq_calls += 1
        return await self.groq_handler(url, **kwargs)


class TestGeminiFailoverCacheAndSentinel(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gemini_circuit_breaker.reset()
        self._orig_probe_interval = gemini_circuit_breaker.probe_interval
        gemini_circuit_breaker.probe_interval = 300.0

    def tearDown(self):
        gemini_circuit_breaker.probe_interval = self._orig_probe_interval
        gemini_circuit_breaker.reset()

    async def test_gemini_timeout_1_6s_triggers_immediate_groq_fallback(self):
        """Se o Gemini demorar mais que 1.6s, corta a espera e retorna Groq em < 2.0s abrindo o cache."""

        async def slow_gemini(_url, **_kwargs):
            await asyncio.sleep(3.0)
            return httpx.Response(200, json={"choices": [{"message": {"content": "tarde-demais"}}]})

        async def fast_groq(_url, **_kwargs):
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok-via-groq"}}]})

        client = _DummyClient(slow_gemini, fast_groq)
        payload = {"model": "qwen/qwen3.8-27b", "messages": [{"role": "user", "content": "oi"}]}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}), patch.object(
            cb_mod, "_send_sentinel_telegram_alert", return_value=True
        ):
            t0 = time.monotonic()
            resp = await post_openai_chat_with_failover(
                client,
                groq_url="https://api.groq.com/openai/v1/chat/completions",
                groq_token="fake-groq-token",
                payload=payload,
            )
            elapsed = time.monotonic() - t0

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["choices"][0]["message"]["content"], "ok-via-groq")
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 2.2)
        self.assertEqual(client.gemini_calls, 1)
        self.assertEqual(client.groq_calls, 1)
        self.assertTrue(gemini_circuit_breaker.is_open)

    async def test_global_cache_on_first_failure_routes_subsequent_users_in_sub_ms(self):
        """1ª falha no Gemini abre o cache global; usuários 2, 3 e 4 vão direto para o Groq."""

        async def broken_gemini(_url, **_kwargs):
            return httpx.Response(429, text="Quota exhausted")

        async def fast_groq(_url, **_kwargs):
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok-via-groq"}}]})

        client = _DummyClient(broken_gemini, fast_groq)
        payload = {"model": "qwen/qwen3.8-27b", "messages": [{"role": "user", "content": "olá"}]}

        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}), patch.object(
            cb_mod, "_send_sentinel_telegram_alert", return_value=True
        ):
            # Usuário #1: tenta Gemini, toma 429, abre cache global e cai no Groq
            r1 = await post_openai_chat_with_failover(
                client,
                groq_url="https://api.groq.com/openai/v1/chat/completions",
                groq_token="fake-groq-token",
                payload=payload,
            )
            self.assertEqual(r1.json()["choices"][0]["message"]["content"], "ok-via-groq")
            self.assertTrue(gemini_circuit_breaker.is_open)

            # Usuários #2, #3 e #4: zero chamadas ao Gemini, desvio instantâneo para Groq
            t_start = time.monotonic()
            for _ in range(3):
                r_next = await post_openai_chat_with_failover(
                    client,
                    groq_url="https://api.groq.com/openai/v1/chat/completions",
                    groq_token="fake-groq-token",
                    payload=payload,
                )
                self.assertEqual(r_next.json()["choices"][0]["message"]["content"], "ok-via-groq")
            subsequent_elapsed = time.monotonic() - t_start

        self.assertEqual(client.gemini_calls, 1)
        self.assertEqual(client.groq_calls, 4)
        self.assertLess(subsequent_elapsed, 0.05)

    async def test_calm_background_probe_keeps_open_for_users_until_probe_succeeds(self):
        """Com background_probe=True, usuários não disparam HALF_OPEN; apenas a sonda calma reabre."""
        probe_results = [False, True]
        probe_calls = 0

        async def calm_probe():
            nonlocal probe_calls
            probe_calls += 1
            if probe_results:
                return probe_results.pop(0)
            return True

        cb = CircuitBreaker(
            "gemini-probe-test",
            failure_threshold=1,
            recovery_time=0.04,
            background_probe=True,
            probe_interval=0.10,
            probe_func=calm_probe,
            alert_on_open=False,
        )
        self.addCleanup(cb.reset)

        async def fail_once():
            raise RuntimeError("Gemini outage")

        with self.assertRaises(RuntimeError):
            await cb.call(fail_once)

        # Mesmo após recovery_time=0.04s, usuário continua vendo OPEN (sem virar cobaia)
        await asyncio.sleep(0.06)
        self.assertEqual(cb.state, CircuitState.OPEN)
        with self.assertRaises(CircuitBreakerOpenException):
            await cb.call(fail_once)

        # Aguarda sonda 1 (False) + sonda 2 (True) fecharem o circuito em background
        for _ in range(25):
            if cb.state == CircuitState.CLOSED:
                break
            await asyncio.sleep(0.03)

        self.assertGreaterEqual(probe_calls, 2)
        self.assertEqual(cb.state, CircuitState.CLOSED)

    async def test_sentinel_telegram_alert_sent_on_open_and_recovery_with_cooldown(self):
        """Envia alerta no Telegram ao abrir (OPEN), respeita cooldown e notifica na recuperação (CLOSED)."""
        sent_messages = []

        class _MockTelegramClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, json=None, **_kwargs):
                sent_messages.append((url, json))
                return httpx.Response(200, json={"ok": True})

        probe_attempts = 0

        async def probe_ok_once():
            nonlocal probe_attempts
            probe_attempts += 1
            return probe_attempts == 1

        cb = CircuitBreaker(
            "gemini",
            failure_threshold=1,
            recovery_time=0.08,
            background_probe=True,
            probe_interval=0.08,
            probe_func=probe_ok_once,
            alert_on_open=True,
            alert_cooldown=60.0,
        )
        self.addCleanup(cb.reset)

        async def fail_gemini():
            raise TimeoutError("Gemini > 1.6s")

        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "bot-token", "TELEGRAM_CHAT_ID": "999999"},
        ), patch("httpx.AsyncClient", side_effect=lambda **kw: _MockTelegramClient(**kw)):
            with self.assertRaises(TimeoutError):
                await cb.call(fail_gemini)

            await asyncio.sleep(0.03)
            self.assertEqual(len(sent_messages), 1)
            open_url, open_payload = sent_messages[0]
            self.assertEqual(open_url, "https://api.telegram.org/botbot-token/sendMessage")
            self.assertEqual(open_payload["chat_id"], "999999")
            self.assertIn("ALERTA CRÍTICO", open_payload["text"])
            self.assertIn("GROQ", open_payload["text"])

            # Aguarda sonda calma recuperar e notificar recuperação
            for _ in range(20):
                if cb.state == CircuitState.CLOSED and len(sent_messages) >= 2:
                    break
                await asyncio.sleep(0.02)

            self.assertEqual(cb.state, CircuitState.CLOSED)
            self.assertEqual(len(sent_messages), 2)
            self.assertIn("RECUPERAÇÃO", sent_messages[1][1]["text"])

            # Nova falha imediata dentro do cooldown de 60s abre o breaker sem spammar Telegram
            with self.assertRaises(TimeoutError):
                await cb.call(fail_gemini)
            await asyncio.sleep(0.03)

        self.assertTrue(cb.is_open)
        open_alerts = [m for _u, m in sent_messages if "ALERTA CRÍTICO" in m["text"]]
        self.assertEqual(len(open_alerts), 1)


if __name__ == "__main__":
    unittest.main()
