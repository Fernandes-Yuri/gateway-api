"""Módulo de Circuit Breaker para resiliência de chamadas a provedores externos de IA (DT-063).

Inclui suporte a Fail-Fast global na 1ª falha, sonda calma em segundo plano
e alerta assíncrono via Sentinela Bot (Telegram) quando o provedor primário
entra em fallback ou recupera.
"""
import asyncio
import html
import logging
import os
import time
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("braga.circuit_breaker")

try:
    import config as _config
except ImportError:  # pragma: no cover - fallback defensivo em testes isolados
    _config = None  # type: ignore[assignment]


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenException(Exception):
    """Lançada quando o circuito está ABERTO para rejeitar a chamada imediatamente sem gastar socket/worker."""

    def __init__(self, message: str = "Circuit breaker is open", retry_after: int = 30):
        super().__init__(message)
        self.retry_after = retry_after


def _cfg_str(attr: str, env_name: str, default: str = "") -> str:
    val = getattr(_config, attr, None) if _config is not None else None
    if val is not None and str(val).strip():
        return str(val).strip()
    env_val = os.getenv(env_name, "")
    return env_val.strip() if env_val else default


def _cfg_float(attr: str, env_name: str, default: float) -> float:
    env_val = os.getenv(env_name)
    if env_val is not None and env_val.strip():
        try:
            return float(env_val)
        except ValueError:
            pass
    val = getattr(_config, attr, None) if _config is not None else None
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    return default


async def _send_sentinel_telegram_alert(message: str) -> bool:
    """Envia alerta assíncrono e não-bloqueante para o Telegram do Sentinela Braga."""
    token = _cfg_str("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    chat_id = _cfg_str("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.debug("[SentinelaAlert] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes; alerta apenas em log.")
        return False

    try:
        import httpx

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            logger.warning("[SentinelaAlert] Telegram retornou HTTP %s: %s", resp.status_code, resp.text[:160])
    except Exception as exc:
        logger.warning("[SentinelaAlert] Falha ao enviar alerta Telegram: %s", exc)
    return False


async def _default_gemini_probe() -> bool:
    """Sonda leve (1 token, timeout 1.6s) para verificar se a API do Gemini recuperou."""
    api_key = _cfg_str("GEMINI_API_KEY", "GEMINI_API_KEY")
    if not api_key:
        return False

    model_chat = _cfg_str("GEMINI_MODEL_CHAT", "GEMINI_MODEL_CHAT", "gemini-3.8-flash")
    fast_timeout = _cfg_float("GEMINI_FAST_TIMEOUT", "GEMINI_FAST_TIMEOUT", 1.6)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_chat}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 1, "temperature": 0.0},
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    try:
        import httpx

        async def _do_ping():
            async with httpx.AsyncClient(timeout=fast_timeout) as client:
                return await client.post(url, json=payload, headers=headers)

        resp = await asyncio.wait_for(_do_ping(), timeout=fast_timeout)
        return resp.status_code == 200
    except Exception as exc:
        logger.debug("[GeminiProbe] Sonda falhou: %s", exc)
        return False


class CircuitBreaker:
    """Implementa o padrão Circuit Breaker com suporte a sonda calma em segundo plano e alerta."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_time: float = 30.0,
        half_open_success_threshold: int = 1,
        *,
        background_probe: bool = False,
        probe_interval: float = 300.0,
        probe_func: Optional[Callable[[], Awaitable[bool]]] = None,
        alert_on_open: bool = False,
        alert_cooldown: float = 300.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.half_open_success_threshold = half_open_success_threshold
        self.background_probe = background_probe
        self.probe_interval = probe_interval
        self.probe_func = probe_func
        self.alert_on_open = alert_on_open
        self.alert_cooldown = alert_cooldown

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_failure_time: float = 0.0
        self._last_error: str = ""
        self._last_alert_time: float = 0.0
        self._probe_task: Optional[asyncio.Task[Any]] = None
        self._alert_tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if self.background_probe:
                return CircuitState.OPEN
            if time.time() - self._last_failure_time >= self.recovery_time:
                return CircuitState.HALF_OPEN
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    @property
    def last_error(self) -> str:
        return self._last_error

    def reset(self) -> None:
        """Restaura estado inicial (CLOSED) e cancela eventual sonda em segundo plano."""
        if self._probe_task is not None and not self._probe_task.done():
            self._probe_task.cancel()
        self._probe_task = None
        for task in list(self._alert_tasks):
            if not task.done():
                task.cancel()
        self._alert_tasks.clear()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_failure_time = 0.0
        self._last_error = ""
        self._last_alert_time = 0.0

    def _spawn_background_task(self, coro: Awaitable[Any]) -> Optional[asyncio.Task[Any]]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(coro)  # type: ignore[arg-type]
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)
        return task

    def _schedule_sentinel_open_alert(self, reason: str) -> None:
        safe_name = html.escape(self.name.upper())
        safe_reason = html.escape(reason or "Erro desconhecido")
        probe_min = round(self.probe_interval / 60, 1)
        msg = (
            f"🚨 <b>ALERTA CRÍTICO — Sentinela Braga</b>\n"
            f"Provedor primário <b>{safe_name}</b> falhou e foi isolado em cache global.\n"
            f"<b>Motivo:</b> <code>{safe_reason}</code>\n"
            f"<b>Ação automática:</b> Tráfego roteado 100% para <b>GROQ</b> (fallback rápido).\n"
            f"<b>Sonda calma:</b> Verificação em segundo plano a cada {probe_min} min ({int(self.probe_interval)}s)."
        )
        self._spawn_background_task(_send_sentinel_telegram_alert(msg))

    def _ensure_background_probe(self) -> None:
        if not self.background_probe or self.probe_func is None:
            return
        if self._probe_task is not None and not self._probe_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._probe_task = loop.create_task(self._background_probe_loop())

    async def _background_probe_loop(self) -> None:
        current = asyncio.current_task()
        logger.info(
            "[CircuitBreaker:%s] Sonda calma em background iniciada (intervalo=%.1fs).",
            self.name,
            self.probe_interval,
        )
        try:
            while self._state == CircuitState.OPEN:
                await asyncio.sleep(self.probe_interval)
                if self._state != CircuitState.OPEN:
                    break
                ok = False
                if self.probe_func is not None:
                    try:
                        ok = bool(await self.probe_func())
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("[CircuitBreaker:%s] Sonda calma falhou: %s", self.name, exc)
                        ok = False
                if ok:
                    async with self._lock:
                        self._state = CircuitState.CLOSED
                        self._consecutive_failures = 0
                        self._consecutive_successes = 0
                        self._last_error = ""
                    logger.info(
                        "[CircuitBreaker:%s] Sonda calma obteve sucesso. Circuito FECHADO e primário reativado.",
                        self.name,
                    )
                    if self.alert_on_open:
                        safe_name = html.escape(self.name.upper())
                        msg = (
                            f"✅ <b>RECUPERAÇÃO — Sentinela Braga</b>\n"
                            f"Provedor <b>{safe_name}</b> respondeu com sucesso na sonda calma.\n"
                            f"Tráfego principal restaurado para <b>{safe_name}</b>."
                        )
                        await _send_sentinel_telegram_alert(msg)
                    break
                logger.warning(
                    "[CircuitBreaker:%s] Sonda calma sem resposta positiva. Mantendo fallback por %.1fs.",
                    self.name,
                    self.probe_interval,
                )
        except asyncio.CancelledError:
            logger.debug("[CircuitBreaker:%s] Sonda calma cancelada.", self.name)
        finally:
            if self._probe_task is current:
                self._probe_task = None

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        current_state = self.state
        if current_state == CircuitState.OPEN:
            window = self.probe_interval if self.background_probe else self.recovery_time
            wait_remaining = int(max(1, window - (time.time() - self._last_failure_time)))
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
                    self._last_error = ""
                    logger.info("[CircuitBreaker:%s] Recuperação confirmada. Circuito FECHADO.", self.name)
            else:
                self._consecutive_failures = 0

    async def _record_failure(self, exc: BaseException) -> None:
        should_alert = False
        start_probe = False
        error_summary = f"{type(exc).__name__}: {str(exc)[:180]}".strip(": ")
        async with self._lock:
            was_open = self._state == CircuitState.OPEN
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            self._last_error = error_summary
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
                window = self.probe_interval if self.background_probe else self.recovery_time
                logger.error(
                    "[CircuitBreaker:%s] Limite de falhas atingido! Circuito ABERTO por %ss",
                    self.name,
                    window,
                )
                if not was_open and self.alert_on_open:
                    now = time.monotonic()
                    if self._last_alert_time == 0.0 or (now - self._last_alert_time) >= self.alert_cooldown:
                        self._last_alert_time = now
                        should_alert = True
                if self.background_probe:
                    start_probe = True

        if should_alert:
            self._schedule_sentinel_open_alert(error_summary)
        if start_probe:
            self._ensure_background_probe()


def sanitize_openai_payload_for_gemini(
    payload: dict[str, Any],
    gemini_model: str,
    *,
    stream: bool = False,
) -> dict[str, Any]:
    """Adapta payload no formato OpenAI para o endpoint compatível do Gemini."""
    cleaned = {
        k: v
        for k, v in payload.items()
        if k not in ("stream_options", "reasoning_effort")
    }
    cleaned["model"] = gemini_model
    if stream:
        cleaned["stream"] = True
    else:
        cleaned.pop("stream", None)
    return cleaned


async def post_openai_chat_with_failover(
    client: Any,
    *,
    groq_url: str,
    groq_token: str,
    payload: dict[str, Any],
    gemini_timeout: Optional[float] = None,
) -> Any:
    """Executa chamada OpenAI-compatível tentando Gemini primeiro (corte 1.6s + cache global) e Groq no fallback."""
    gemini_key = _cfg_str("GEMINI_API_KEY", "GEMINI_API_KEY")
    if gemini_key and not gemini_circuit_breaker.is_open:
        gemini_url = _cfg_str(
            "GEMINI_OPENAI_URL",
            "GEMINI_OPENAI_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        )
        gemini_model = _cfg_str("GEMINI_MODEL_CHAT", "GEMINI_MODEL_CHAT", "gemini-3.8-flash")
        timeout = (
            float(gemini_timeout)
            if gemini_timeout is not None
            else _cfg_float("GEMINI_FAST_TIMEOUT", "GEMINI_FAST_TIMEOUT", 1.6)
        )
        gemini_payload = sanitize_openai_payload_for_gemini(payload, gemini_model, stream=False)
        gemini_headers = {
            "Authorization": f"Bearer {gemini_key}",
            "Content-Type": "application/json",
        }

        async def _post_gemini() -> Any:
            resp = await asyncio.wait_for(
                client.post(gemini_url, json=gemini_payload, headers=gemini_headers),
                timeout=timeout,
            )
            status = getattr(resp, "status_code", 200)
            if status != 200:
                detail = getattr(resp, "text", "")[:160]
                raise RuntimeError(f"Gemini HTTP {status}: {detail}")
            return resp

        try:
            return await gemini_circuit_breaker.call(_post_gemini)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}".strip(": ")
            logger.warning(
                "[AI Failover] Gemini falhou (%s). Fallback imediato para Groq.",
                reason,
            )

    groq_headers = {
        "Authorization": f"Bearer {groq_token}",
        "Content-Type": "application/json",
    }
    return await client.post(groq_url, json=payload, headers=groq_headers)


# Instâncias globais reutilizáveis para provedores externos de IA
_DEFAULT_PROBE_INTERVAL = _cfg_float("GEMINI_PROBE_INTERVAL_S", "GEMINI_PROBE_INTERVAL_S", 300.0)
groq_circuit_breaker = CircuitBreaker("groq", failure_threshold=3, recovery_time=30.0)
gemini_circuit_breaker = CircuitBreaker(
    "gemini",
    failure_threshold=1,
    recovery_time=_DEFAULT_PROBE_INTERVAL,
    background_probe=True,
    probe_interval=_DEFAULT_PROBE_INTERVAL,
    probe_func=_default_gemini_probe,
    alert_on_open=True,
)
