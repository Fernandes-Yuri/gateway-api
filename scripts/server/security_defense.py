"""
Braga Saude - Security Defense System
Implementa Zero-Trust, Deteccao de Prompt Injection, Sanitizacao de Entrada,
e StrictRateLimiter com Bloqueio de IP por 24 horas (apos 3 violacoes).
"""
import re
import time
import asyncio
from collections import deque
from typing import Dict, Deque, Optional, Tuple
from fastapi import HTTPException, Request

# =============================================================
# DETECCAO DE PROMPT INJECTION E PADROES MALICIOSOS
# =============================================================
INJECTION_PATTERNS = [
    re.compile(r"(?i)(ignore|esqueça|desconsidere|ignora|forget|bypass)\s+(todas\s+as\s+|tudo|os\s+|as\s+|all\s+)?(instruções|regras|comandos|prompts|limites|instructions|rules)"),
    re.compile(r"(?i)(modo\s+desenvolvedor|developer\s+mode|dan\s+mode|jailbreak|root\s+access|system\s+override|admin\s+override)"),
    re.compile(r"(?i)(aja\s+como|haja\s+como|finja\s+ser|você\s+agora\s+é|act\s+as|pretend\s+to\s+be|roleplay\s+as)\s+(um\s+|uma\s+)?(médico|doutor|cardiologista|especialista|hacker|administrador|unfiltered|god\s+mode)"),
    re.compile(r"(?i)(qual\s+é\s+o\s+seu\s+prompt|mostre\s+o\s+seu\s+prompt|repita\s+as\s+instruções|system\s+prompt|reveal\s+your\s+prompt|output\s+your\s+initial\s+instructions)"),
    re.compile(r"(?i)(receite\s+um\s+remédio|qual\s+remédio\s+tomar|prescreva\s+|qual\s+dosagem\s+de\s+|me\s+dê\s+uma\s+receita)"),
    re.compile(r"(?i)(cont[ea](r)?\s+(de\s+\d+\s+)?(at[ée]|a)\s+\d+|cont[ea](r)?\s+at[ée]\s+\d+|escreva\s+um\s+livro|escreva\s+um\s+poema\s+gigante|repita\s+isso\s+100|gere\s+um\s+texto\s+de\s+[0-9]+\s+linhas)"),
    re.compile(r"(?i)(<\|im_start\|>|<\|im_end\|>|<\|system\|>|\[SYSTEM\]|\[INST\]|\[/INST\]|<<SYS>>|system_prompt)"),
]

def is_prompt_injection(text: str) -> bool:
    """Verifica se o texto submetido contem padroes de injecao de prompt."""
    if not text or not isinstance(text, str):
        return False
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False

def sanitize_zero_trust_text(
    text: Optional[str],
    max_length: int = 500,
    field_name: str = "texto",
    ip: Optional[str] = None,
    rate_limiter = None,
    pool = None,
) -> str:
    """Valida e sanitiza entradas de texto sob politica Zero-Trust.
    
    1. Rejeita tentativas de prompt injection e penaliza o IP com 2 violacoes.
    2. Remove tags HTML/XML, scripts e manipuladores de eventos (anti-XSS).
    3. Limita o comprimento do campo com seguranca.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        return str(text)[:max_length]
    
    # 1. Checagem contra Prompt Injection
    if is_prompt_injection(text):
        print(f"[SECURITY ZERO-TRUST ALERT] Prompt Injection detectado no campo '{field_name}'! IP: {ip}")
        if ip and rate_limiter:
            try:
                # Dispara penalidade de 2 violacoes para o IP infrator
                asyncio.create_task(
                    rate_limiter.record_violation(
                        ip,
                        f"Prompt Injection detectado no campo '{field_name}'",
                        pool=pool,
                        severity=2,
                    )
                )
            except Exception as e:
                print(f"[SECURITY ZERO-TRUST] Erro ao gravar violacao: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Conteudo do campo '{field_name}' rejeitado pelas diretrizes de seguranca.",
        )

    # 2. Sanitizacao XSS e caracteres de controle
    clean = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text.strip())
    clean = re.sub(r'(?i)<[^>]*>', '', clean)  # Remove tags HTML/XML
    clean = re.sub(r'(?i)javascript:\s*', '', clean)
    clean = re.sub(r'(?i)vbscript:\s*', '', clean)
    clean = re.sub(r'(?i)data:\s*text/html', '', clean)

    # 3. Truncagem
    return clean[:max_length]

# =============================================================
# DETECCAO DE IP REAL DO CLIENTE
# =============================================================
def get_client_ip(request: Request) -> str:
    """Obtem o endereco IP real do cliente inspecionando proxies confiaveis.

    Apenas o Cloudflare Connecting IP e confiavel, pois o deploy roda atrás
    do Cloudflare Tunnel. Os demais headers (x-real-ip, x-forwarded-for) sao
    forjaveis pelo cliente: se forem lidos cegamente, um atacante zera todos
    os limites de rate limit enviando um header diferente a cada requisicao.
    """
    # Prioridade 1: Cloudflare Connecting IP (unico confiavel neste deploy)
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip and _is_valid_ip(cf_ip.strip()):
        return cf_ip.strip()
    # Prioridade 2: IP da conexao direta (ultimo hop confiavel)
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _is_valid_ip(value: str) -> bool:
    """Validacao minima de formato para evitar header-IPs absurdos."""
    import ipaddress
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False

# =============================================================
# STRICT RATE LIMITER COM BLOQUEIO DE 24 HORAS
# =============================================================
class StrictRateLimiter:
    """Limitador rigoroso com bloqueio de 24h para violacoes repetidas (>= 3).
    
    Persiste bloqueios ativos no PostgreSQL (security_blocked_ips) para que
    reinicializacoes do servico nao desfacam o bloqueio de 24 horas.
    """

    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = {}
        self._violations: Dict[str, Deque[float]] = {}  # ip -> timestamps de violacoes em 24h
        self._blocked_ips: Dict[str, float] = {}       # ip -> timestamp monotonic de liberacao
        self._lock = asyncio.Lock()

    async def set_manual_block(self, ip: str, blocked: bool) -> None:
        async with self._lock:
            if blocked:
                self._blocked_ips[ip] = time.monotonic() + 86400
            else:
                self._blocked_ips.pop(ip, None)
                self._violations.pop(ip, None)

    async def load_blocked_ips_from_db(self, pool) -> None:
        """Carrega bloqueios ativos do PostgreSQL na inicializacao."""
        if not pool:
            return
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT ip, EXTRACT(EPOCH FROM (blocked_until - NOW())) as remaining_sec
                    FROM security_blocked_ips
                    WHERE blocked_until > NOW();
                    """
                )
                now = time.monotonic()
                async with self._lock:
                    for r in rows:
                        ip = r["ip"]
                        rem = float(r["remaining_sec"] or 0)
                        if rem > 0:
                            self._blocked_ips[ip] = now + rem
                            print(f"[SECURITY FIREWALL] IP {ip} restaurado sob bloqueio de 24h ({int(rem)}s restantes)")
        except Exception as e:
            print(f"[SECURITY FIREWALL] Aviso: Falha ao carregar IPs bloqueados do banco: {e}")

    def is_ip_blocked(self, ip: str) -> Tuple[bool, int]:
        """Verifica se o IP esta sob bloqueio de 24h ativo. Retorna (bloqueado, segundos_restantes)."""
        if not ip or ip in ("127.0.0.1", "::1", "localhost", "unknown"):
            return False, 0
        now = time.monotonic()
        blocked_until = self._blocked_ips.get(ip)
        if blocked_until is not None:
            if now < blocked_until:
                remaining = max(1, int(blocked_until - now))
                return True, remaining
            else:
                # Expirou o bloqueio
                self._blocked_ips.pop(ip, None)
                self._violations.pop(ip, None)
        return False, 0

    async def record_violation(self, ip: str, reason: str, pool=None, severity: int = 1) -> bool:
        """Registra infracao de seguranca. Se acumular 3 infracoes em 24h, bloqueia o IP por 24h."""
        if not ip or ip in ("127.0.0.1", "::1", "localhost", "unknown"):
            return False
        now = time.monotonic()
        cutoff_24h = now - 86400.0  # 24 horas

        async with self._lock:
            v_list = self._violations.setdefault(ip, deque())
            # Limpa infracoes com mais de 24 horas
            while v_list and v_list[0] <= cutoff_24h:
                v_list.popleft()

            # Adiciona a infracao (severidade 1 = 1 violacao; severidade 2 = 2 violacoes)
            for _ in range(severity):
                v_list.append(now)

            count = len(v_list)
            print(f"[SECURITY FIREWALL] Violacao registrada para IP {ip}: {reason} (Total em 24h: {count}/3)")

            if count >= 3:
                # Bloqueio imediato por 24h (86.400 segundos)
                self._blocked_ips[ip] = now + 86400.0
                print(f"[SECURITY ALERT] >>> IP {ip} BLOQUEADO POR 24 HORAS <<< Motivo: {reason} (Violacoes: {count})")
                
                # Persiste no PostgreSQL
                if pool:
                    try:
                        async with pool.acquire() as conn:
                            await conn.execute(
                                """
                                INSERT INTO security_blocked_ips (ip, reason, violations, blocked_until, updated_at)
                                VALUES ($1, $2, $3, NOW() + INTERVAL '24 hours', NOW())
                                ON CONFLICT (ip) DO UPDATE SET
                                    reason = EXCLUDED.reason,
                                    violations = security_blocked_ips.violations + EXCLUDED.violations,
                                    blocked_until = NOW() + INTERVAL '24 hours',
                                    updated_at = NOW();
                                """,
                                ip, reason, count
                            )
                    except Exception as err:
                        print(f"[SECURITY FIREWALL] Falha ao persistir bloqueio no banco: {err}")

                # Dispara alerta em background para o WhatsApp do Administrador
                try:
                    from whatsapp_service import send_security_incident_alert_whatsapp
                    asyncio.create_task(
                        send_security_incident_alert_whatsapp(ip, reason, count)
                    )
                except Exception as alert_err:
                    print(f"[SECURITY FIREWALL] Falha ao agendar alerta de WhatsApp: {alert_err}")

                return True
        return False

    async def check(
        self,
        scope: str,
        key: str,
        limit: int,
        window_seconds: int,
        ip: Optional[str] = None,
        pool=None,
    ) -> None:
        """Checa rate limit com janela deslizante. Se excedido, registra violacao e bloqueia por 24h no 3o excesso."""
        now = time.monotonic()
        target_ip = ip or (key if (scope.endswith("_ip") or scope.startswith("ip_")) else None)

        # 1. Verifica se ja esta bloqueado
        if target_ip:
            blocked, remaining = self.is_ip_blocked(target_ip)
            if blocked:
                raise HTTPException(
                    status_code=403,
                    detail="Acesso bloqueado por 24 horas para este endereco IP devido a excesso de tentativas e violacoes de seguranca.",
                    headers={
                        "Retry-After": str(remaining),
                        "X-RateLimit-Blocked": "true",
                        "X-Blocked-Until": str(remaining),
                    },
                )

        bucket_key = f"{scope}:{key}"
        async with self._lock:
            hits = self._hits.setdefault(bucket_key, deque())
            cutoff = now - window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= limit:
                retry_after = max(1, int(window_seconds - (now - hits[0])) + 1)
                hits_overflow = True
            else:
                hits.append(now)
                hits_overflow = False

        if hits_overflow:
            if target_ip:
                is_now_blocked = await self.record_violation(
                    target_ip,
                    f"Rate limit excedido em {scope}: {limit} requisicoes em {window_seconds}s",
                    pool=pool,
                    severity=1,
                )
                if is_now_blocked:
                    raise HTTPException(
                        status_code=403,
                        detail="Limite de seguranca violado 3 vezes. Seu endereco IP foi bloqueado por 24 horas.",
                        headers={"Retry-After": "86400", "X-RateLimit-Blocked": "true"},
                    )
            raise HTTPException(
                status_code=429,
                detail="Muitas tentativas. Aguarde alguns minutos antes de tentar novamente.",
                headers={"Retry-After": str(retry_after)},
            )
