"""Módulo de Circuit Breaker para resiliência de chamadas a provedores externos de IA (DT-063)."""
import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("braga.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenException(Exception):
    """Lançada quando o circuito está ABERTO para rejeitar a chamada imediatamente sem gastar socket/worker."""

    def __init__(self, message: str = "Circuit breaker is open", retry_after: int = 30):
        super().__init__(message)
        self.retry_after = retry_after


class CircuitBreaker:
    """Implementa o padrão Circuit Breaker para evitar esgotamento de conexões e workers sob falha de terceiros."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_time: float = 30.0,
        half_open_success_threshold: int = 1,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.half_open_success_threshold = half_open_success_threshold

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time >= self.recovery_time:
                return CircuitState.HALF_OPEN
        return self._state

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        current_state = self.state
        if current_state == CircuitState.OPEN:
            wait_remaining = int(max(1, self.recovery_time - (time.time() - self._last_failure_time)))
            logger.warning(
                "[CircuitBreaker:%s] Circuito ABERTO. Chamada externa rejeitada imediatamente (retry_after=%ss)",
                self.name,
                wait_remaining,
            )
            raise CircuitBreakerOpenException(
                f"Serviço de IA ({self.name}) temporariamente instável. Circuito aberto.",
                retry_after=wait_remaining,
            )

        if current_state == CircuitState.HALF_OPEN and self._state == CircuitState.OPEN:
            async with self._lock:
                self._state = CircuitState.HALF_OPEN

        try:
            result = await func(*args, **kwargs)
            await self._record_success()
            return result
        except Exception as exc:
            await self._record_failure(exc)
            raise

    async def _record_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.half_open_success_threshold:
                    self._state = CircuitState.CLOSED
                    self._consecutive_failures = 0
                    self._consecutive_successes = 0
                    logger.info("[CircuitBreaker:%s] Recuperação confirmada. Circuito FECHADO.", self.name)
            else:
                self._consecutive_failures = 0

    async def _record_failure(self, exc: Exception) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            logger.warning(
                "[CircuitBreaker:%s] Falha registrada (%s/%s): %s",
                self.name,
                self._consecutive_failures,
                self.failure_threshold,
                exc,
            )
            if self._consecutive_failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._consecutive_successes = 0
                logger.error(
                    "[CircuitBreaker:%s] Limite de falhas atingido! Circuito ABERTO por %ss",
                    self.name,
                    self.recovery_time,
                )


# Instâncias globais reutilizáveis para provedores externos de IA
groq_circuit_breaker = CircuitBreaker("groq", failure_threshold=3, recovery_time=30.0)
gemini_circuit_breaker = CircuitBreaker("gemini", failure_threshold=3, recovery_time=30.0)
