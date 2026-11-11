"""Testes unitários para o Circuit Breaker de IA (DT-063)."""
import asyncio
import time
import unittest
from services.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenException,
    CircuitState,
)


class TestCircuitBreaker(unittest.IsolatedAsyncioTestCase):
    async def test_successful_calls_remain_closed(self):
        cb = CircuitBreaker("test-service", failure_threshold=3, recovery_time=1.0)
        async def dummy_ok():
            return "ok"

        res = await cb.call(dummy_ok)
        self.assertEqual(res, "ok")
        self.assertEqual(cb.state, CircuitState.CLOSED)

    async def test_circuit_opens_after_failures_threshold(self):
        cb = CircuitBreaker("test-service", failure_threshold=3, recovery_time=0.5)
        call_count = 0

        async def failing():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("API failure")

        for _ in range(3):
            with self.assertRaises(RuntimeError):
                await cb.call(failing)

        self.assertEqual(call_count, 3)
        self.assertEqual(cb.state, CircuitState.OPEN)

        # Chamada seguinte deve ser rejeitada imediatamente sem chamar `failing`
        with self.assertRaises(CircuitBreakerOpenException):
            await cb.call(failing)

        self.assertEqual(call_count, 3)  # Não chamou novamente

    async def test_circuit_recovers_to_closed_after_half_open(self):
        cb = CircuitBreaker("test-service", failure_threshold=2, recovery_time=0.2)

        async def failing():
            raise RuntimeError("Temporary error")

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await cb.call(failing)

        self.assertEqual(cb.state, CircuitState.OPEN)

        # Aguarda tempo de recuperação
        await asyncio.sleep(0.25)
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # Chamada bem-sucedida fecha o circuito
        async def ok_func():
            return "recovered"

        res = await cb.call(ok_func)
        self.assertEqual(res, "recovered")
        self.assertEqual(cb.state, CircuitState.CLOSED)
