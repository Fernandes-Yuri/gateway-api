"""Router administrativo do portal webservice (D-WS1, D50).

Finalidade: expor para o portal Next.js (Render) APENAS leituras e
operacoes administrativas, sem que ele precise de credencial de banco.
O portal autentica com um token de serviço de escopo reduzido
(SERVICE_TOKEN do env) — nunca com ADMIN_API_TOKEN nem chaves Groq.

Seguranca (AUD-G01): atras do tuneel Cloudflare TODA requisicao chega
com client.host == "127.0.0.1", entao "chamada local" nunca pode ser
autenticacao. So o token constante-no-tempo vale.

Metricas (Frente 2): aggregate-only. Nenhuma linha de usuario cruza;
bucket com menos de MIN_BUCKET usuários e suprimido/fundido DENTRO do
SQL do gateway — o portal so recebe numeros que nao isolam ninguem.
"""
import asyncio
import ipaddress
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from security_defense import get_client_ip, sanitize_zero_trust_text
from portal_auth_security import auth_limit, master_step, consume_master
from auth_crypto import hash_password, verify_password, verify_missing_user
from column_crypto import encrypt_field

router = APIRouter()


class _AppState:
    """Estado injetado pelo server.py via configure_state()."""

    db_pool = None
    rate_limiter = None
    telemetry = None


_state = _AppState()


def configure_state(*, db_pool=None, rate_limiter=None):
    _state.db_pool = db_pool
    _state.rate_limiter = rate_limiter


# ---- Modelos -----------------------------------------------------------


class FeedbackStatusUpdate(BaseModel):
    status: str


class FeedbackReplyCreate(BaseModel):
    author: Optional[str] = "Suporte Braga Saúde"
    body: str


class BlockedIpCreate(BaseModel):
    ip: str
    reason: str = Field(min_length=1, max_length=500)
    minutes: int = Field(default=1440, ge=1, le=43200)


# ---- Autenticacao ------------------------------------------------------


def _service_token() -> str:
    """Token de serviço do env. String vazia se nao configurado."""
    return os.getenv("SERVICE_TOKEN", "").strip()


async def require_service_token(request: Request):
    """Dependencia FastAPI: exige Bearer <SERVICE_TOKEN>.

    - 503 se o token nao esta configurado (recurso desativado, nunca aberto).
    - 401 se ausente ou divergente. compare_digest e constante no tempo
      para nao vazar o token por timing side-channel.
    - Loga cada acesso (path + IP) para auditoria. O token em si nunca
      aparece no log.
    """
    token = _service_token()
    if not token:
        raise HTTPException(503, "Portal administrativo indisponível: SERVICE_TOKEN não configurado.")
    supplied = request.headers.get("Authorization", "")
    # So compara se o formato for exatamente "Bearer <token>".
    if not supplied.startswith("Bearer "):
        print(f"[ADMIN] Acesso negado (sem Bearer) {get_client_ip(request)} {request.url.path}")
        raise HTTPException(401, "Não autorizado.")
    candidate = supplied[len("Bearer "):]
    if not secrets.compare_digest(candidate, token):
        # Nao loga o candidato — so o fato da divergência.
        print(f"[ADMIN] Token invalido {get_client_ip(request)} {request.url.path}")
        raise HTTPException(401, "Não autorizado.")
    print(f"[ADMIN] OK {get_client_ip(request)} {request.url.path}")
    return "service"


# ---- Helpers de banco --------------------------------------------------


def _require_pool():
    """Pool ou 503. Rotas admin nao tem modo offline: ou tem banco ou nao."""
    if _state.db_pool is None:
        raise HTTPException(503, "Banco de dados indisponível.")
    return _state.db_pool


def _portal_identity(request: Request) -> str:
    """Identidade assinada pelo portal, vinculada à rota e válida por 60s.

    O portal só emite esta declaração depois de validar a sessão do usuário.
    Não confundir com Authorization: esse header contém SERVICE_TOKEN.
    """
    try:
        encoded, signature = request.headers.get("X-Admin-Identity", "").split(".")
        expected = hmac.new(_service_token().encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("assinatura")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        now = time.time()
        if payload["path"] != request.url.path or not now < payload["exp"] <= now + 60:
            raise ValueError("escopo ou validade")
        email = payload["email"]
        if not isinstance(email, str) or not email or len(email) > 254:
            raise ValueError("identidade")
        return email.strip().lower()
    except (ValueError, KeyError, TypeError):
        raise HTTPException(401, "Identidade administrativa inválida.")


def _row_to_feedback(row) -> dict:
    """Mapeia uma linha de feedbacks para o formato do portal."""
    return {
        "id": str(row["id"]),
        "userId": row["user_id"],
        "userEmail": row["user_email"],
        "userName": row["user_name"],
        "category": row["category"],
        "title": row["title"],
        "message": row["message"],
        "inputMethod": row["input_method"],
        "appVersion": row["app_version"],
        "deviceInfo": row["device_info"],
        "screenshotBase64": row.get("screenshot_base64"),
        # O gateway usa 'pending'; o portal usa 'novo'. Unificamos para o
        # vocabulario do portal (ver ALLOWED_STATUS).
        "status": _map_status_gateway_to_portal(row["status"]),
        "createdAt": row["created_at"].isoformat() if row["created_at"] else None,
        # Badge do portal: quantas respostas a equipe ja deu neste item.
        "repliesCount": int(row["replies_count"] or 0) if "replies_count" in row.keys() else 0,
    }


# Vocabulario de status: o gateway escreve 'pending' (init_schema.sql); o
# portal usa 'novo'/'investigando'/etc. Aceitamos os dois nas entradas e
# devolvemos no do portal na saida.
ALLOWED_STATUS = {
    "novo", "investigando", "resolvido_v1.2", "arquivado",
    "pending", "analyzed", "resolved", "archived",
}

_STATUS_GATEWAY_TO_PORTAL = {
    "pending": "novo",
    "analyzed": "investigando",
    "resolved": "resolvido_v1.2",
    "archived": "arquivado",
}


def _map_status_gateway_to_portal(status: Optional[str]) -> Optional[str]:
    if not status:
        return "novo"
    return _STATUS_GATEWAY_TO_PORTAL.get(status, status)


# ---- Rotas: feedback ---------------------------------------------------


@router.get("/api/admin/feedback")
async def list_feedback_admin(
    request: Request,
    _auth=Depends(require_service_token),
    page: int = Query(1, ge=1),
    limit: int = Query(15, ge=1, le=100),
    category: str = Query("all"),
    status: str = Query("all"),
    priority: str = Query("all"),
    search: str = Query(""),
):
    """Lista feedbacks do app para o painel — paginado e filtrado.

    Le a tabela 'feedbacks' (a do gateway, escrita por /api/sync/feedback).
    Inclui contagem de respostas por item (o portal mostra o badge) e os
    KPIs do cabecalho (total/pendentes/alta prioridade) — sempre agregados,
    nunca linhas individiais alem da pagina atual.
    """
    pool = _require_pool()
    client_ip = get_client_ip(request)
    if _state.rate_limiter is not None:
        await _state.rate_limiter.check(
            "admin_feedback_ip", client_ip, limit=30, window_seconds=60,
            ip=client_ip, pool=pool)

    conditions = []
    values = []
    idx = 1
    if category != "all":
        conditions.append(f"LOWER(category) = ${idx}")
        values.append(category.lower())
        idx += 1
    if status != "all":
        conditions.append("LOWER(status) = ANY($%d::text[])" % idx)
        # Aceita ambos os vocabularios na entrada.
        values.append([status, _map_status_portal_to_gateway(status)])
        idx += 1
    if priority == "high":
        # O gateway nao tem coluna de prioridade (D-WS3 pendente); a alta
        # prioridade do portal vinha de termos criticos no texto. Para nao
        # devolver sempre 'all', filtra pelos mesmos termos criticos que o
        # portal usava — logica espelhada, resultado equivalente.
        conditions.append(_critical_terms_condition(idx))
        values.append([f"%{t}%" for t in _CRITICAL_TERMS])
        idx += 1
    if search:
        # LIKE trata % e _ como curingas — escapa antes de envolver.
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").lower()
        conditions.append("("
                          "LOWER(message) LIKE $%d ESCAPE '\\' OR "
                          "LOWER(title) LIKE $%d ESCAPE '\\' OR "
                          "LOWER(user_name) LIKE $%d ESCAPE '\\' OR "
                          "LOWER(user_email) LIKE $%d ESCAPE '\\'" % (idx, idx, idx, idx))
        values.append(f"%{escaped}%")
        idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM feedbacks {where}", *values)
        total = int(total or 0)
        offset = (page - 1) * limit
        rows = await conn.fetch(
            f"""SELECT f.id, f.user_id, f.user_email, f.user_name, f.category,
                       f.title, f.message, f.input_method, f.app_version,
                       f.device_info, f.screenshot_base64, f.status, f.created_at,
                       (SELECT COUNT(*) FROM feedback_replies r WHERE r.feedback_id = f.id) AS replies_count
                FROM feedbacks f {where}
                ORDER BY f.created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}""",
            *values, limit, offset)
        # KPIs do cabecalho do painel: agregados puros sobre a mesma base.
        # ~* e case/accent-insensitive; o regex cobre os termos criticos.
        kpis = await conn.fetchrow(
            f"""SELECT
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE LOWER(status) IN ('pending','analyzed')) AS pendentes,
                   COUNT(*) FILTER (WHERE message ~* $1) AS alta_prioridade,
                   COUNT(*) FILTER (WHERE LOWER(category) IN ('sugestao','suggestion')) AS sugestoes,
                   COUNT(*) FILTER (WHERE LOWER(category) = 'bug') AS bugs,
                   COUNT(*) FILTER (WHERE LOWER(category) IN ('passos','steps')) AS passos,
                   COUNT(*) FILTER (WHERE LOWER(category) IN ('exames','exams')) AS exames,
                   COUNT(*) FILTER (WHERE LOWER(status) IN ('investigando','analyzed')) AS investigando,
                   COUNT(*) FILTER (WHERE LOWER(status) IN ('resolvido_v1.2','resolved')) AS resolvidos
                FROM feedbacks {where}""",
            _CRITICAL_REGEX, *values)

    items = [_row_to_feedback(r) for r in rows]
    # KPIs podem faltar em mock/teste; defensivo para nao quebrar a lista.
    k = kpis or {}
    return {
        "success": True,
        "data": items,
        "pagination": {
            "page": page, "limit": limit, "total": total,
            "totalPages": max(1, (total + limit - 1) // limit),
        },
        # KPIs agregados: o painel mostra no cabecalho da lista. Todos sao
        # contagens sobre a base filtrada — nunca linhas de usuario.
        "stats": {
            "total": int(k.get("total") or 0),
            "pending": int(k.get("pendentes") or 0),
            "highPriority": int(k.get("alta_prioridade") or 0),
            "suggestions": int(k.get("sugestoes") or 0),
            "bugs": int(k.get("bugs") or 0),
            "steps": int(k.get("passos") or 0),
            "exams": int(k.get("exames") or 0),
            "investigating": int(k.get("investigando") or 0),
            "resolved": int(k.get("resolvidos") or 0),
        },
    }


# Termos criticos — espelham o checkCriticalRisk do portal (CRITICAL_TERMS),
# sem acentos para casar com mensagem normalizada. Usados no filtro de
# prioridade ate a migration 014 criar is_high_priority (D-WS3).
_CRITICAL_TERMS = [
    "nao abre", "zerou", "travou", "trava", "perdi meus dados",
    "perdi dados", "emergencia", "socorro", "parou de funcionar",
    "parou", "erro grave", "tela preta", "tela branca", "nao funciona",
    "queda", "desmaiou",
]
# Regex unico, reusado no WHERE do filtro e no FILTER dos KPIs.
_CRITICAL_REGEX = "(" + "|".join(_CRITICAL_TERMS) + ")"


def _critical_terms_condition(idx: int) -> str:
    """Condicao para o filtro de prioridade (LIKE ANY sobre o LOWER)."""
    return "LOWER(message) LIKE ANY($%d::text[])" % idx


_STATUS_PORTAL_TO_GATEWAY = {
    "novo": "pending",
    "investigando": "analyzed",
    "resolvido_v1.2": "resolved",
    "arquivado": "archived",
}


def _map_status_portal_to_gateway(status: str) -> str:
    return _STATUS_PORTAL_TO_GATEWAY.get(status, status)


@router.patch("/api/admin/feedback/{feedback_id}/status")
async def update_feedback_status(
    feedback_id: str,
    update: FeedbackStatusUpdate,
    _auth=Depends(require_service_token),
):
    """Atualiza o status de um feedback (fluxo de triagem do painel)."""
    pool = _require_pool()
    status = update.status.strip().lower()
    if status not in ALLOWED_STATUS:
        raise HTTPException(422, f"Status inválido. Use: {', '.join(sorted(ALLOWED_STATUS))}")
    # Normaliza para o vocabulario do gateway antes de gravar.
    normalized = _map_status_portal_to_gateway(status)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE feedbacks SET status=$1 WHERE id=$2 RETURNING id",
            normalized, feedback_id)
    if not row:
        raise HTTPException(404, "Feedback não encontrado.")
    return {"success": True, "status": _map_status_gateway_to_portal(normalized)}


@router.post("/api/admin/feedback/{feedback_id}/reply")
async def reply_feedback_admin(
    feedback_id: str,
    reply: FeedbackReplyCreate,
    _auth=Depends(require_service_token),
):
    """Equipe responde um feedback pelo portal.

    Escreve em feedback_replies — a MESMA tabela que o app le via
    /api/feedback/replies. Com isso o ciclo fecha: portal responde ->
    SyncWorker puxa -> notificacao local no celular do usuario.
    """
    pool = _require_pool()
    body = (reply.body or "").strip()
    if not body:
        raise HTTPException(422, "A resposta não pode ser vazia.")
    if len(body) > 2000:
        raise HTTPException(422, "A resposta excede o limite de 2.000 caracteres.")
    author = (reply.author or "Suporte Braga Saúde").strip()[:100]

    import uuid
    try:
        fid = uuid.UUID(feedback_id)
    except ValueError:
        raise HTTPException(422, "Feedback inválido.")

    async with pool.acquire() as conn:
        feedback = await conn.fetchrow(
            "SELECT id, user_id FROM feedbacks WHERE id=$1", fid)
        if not feedback:
            raise HTTPException(404, "Feedback não encontrado.")
        encrypted_body = encrypt_field(body)
        row = await conn.fetchrow(
            """INSERT INTO feedback_replies (feedback_id, user_id, author, body)
               VALUES ($1, $2, $3, $4) RETURNING id, created_at""",
            fid, feedback["user_id"], author, encrypted_body)
    return {
        "success": True,
        "id": str(row["id"]),
        "createdAt": row["created_at"].isoformat(),
    }


# ---- Metricas (Frente 2): aggregate-only --------------------------------
#
# REGRA DE OURO (k-anonimato): nenhuma linha de usuario cruza a rede — so
# numeros. Qualquer bucket agregado com menos de MIN_BUCKET usuarios e
# suprimido DENTRO do SQL (retorna NULL), para que o portal nunca receba
# um total que isole um unico individuo. Proibido combinar saude +
# localidade + faixa etaria na mesma query. metadata de audit_logs nunca
# e selecionado.

MIN_BUCKET = 5


@router.get("/api/admin/metrics/active-users")
async def metrics_active_users(
    _auth=Depends(require_service_token),
    days: int = Query(7, ge=1, le=365),
):
    """Usuarios ativos: distintos com atividade em audit_logs na janela.

    audit_logs e escrito por /api/sync/audit-log a cada acao relevante do
    app — e a fonte mais fiel de "quem usou de verdade".
    """
    pool = _require_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """SELECT COUNT(DISTINCT user_id) FROM audit_logs
               WHERE created_at >= NOW() - ($1 || ' days')::INTERVAL""",
            str(days))
        # Total de cadastrados para a razao de adocao.
        total = await conn.fetchval("SELECT COUNT(*) FROM profiles")
    return {
        "success": True,
        "activeUsers": int(count or 0),
        "totalUsers": int(total or 0),
        "days": days,
    }


@router.get("/api/admin/metrics/engagement")
async def metrics_engagement(_auth=Depends(require_service_token)):
    """Engajamento: distribuicao de streak e level em faixas.

    Faixas amplas (0, 1-3, 4-6, 7+; level 1, 2-3, 4-5, 6+). Buckets com
    menos de MIN_BUCKET usuarios voltam como NULL — o portal mostra
    'poucos dados' em vez de um numero que isola alguem.
    """
    pool = _require_pool()
    async with pool.acquire() as conn:
        streak = await conn.fetch(
            """SELECT
                 CASE
                   WHEN current_streak = 0 THEN '0'
                   WHEN current_streak BETWEEN 1 AND 3 THEN '1-3'
                   WHEN current_streak BETWEEN 4 AND 6 THEN '4-6'
                   ELSE '7+'
                 END AS faixa,
                 CASE WHEN COUNT(*) >= %d THEN COUNT(*) ELSE NULL END AS usuarios
               FROM profiles GROUP BY faixa ORDER BY faixa""" % MIN_BUCKET)
        level = await conn.fetch(
            """SELECT
                 CASE
                   WHEN current_level = 1 THEN '1'
                   WHEN current_level BETWEEN 2 AND 3 THEN '2-3'
                   WHEN current_level BETWEEN 4 AND 5 THEN '4-5'
                   ELSE '6+'
                 END AS faixa,
                 CASE WHEN COUNT(*) >= %d THEN COUNT(*) ELSE NULL END AS usuarios
               FROM profiles GROUP BY faixa ORDER BY faixa""" % MIN_BUCKET)
    return {
        "success": True,
        "streakDistribution": [{"faixa": r["faixa"], "usuarios": r["usuarios"]} for r in streak],
        "levelDistribution": [{"faixa": r["faixa"], "usuarios": r["usuarios"]} for r in level],
    }


@router.get("/api/admin/metrics/notifications")
async def metrics_notifications(_auth=Depends(require_service_token)):
    """Uso de notificacoes: opt-in % e volume por canal.

    agent_actions.carrega o canal de cada acao executada ('app', 'whatsapp'
    ou 'fcm'). O opt-in vem de profiles.notifications_enabled.
    """
    pool = _require_pool()
    async with pool.acquire() as conn:
        opted = await conn.fetchval(
            "SELECT COUNT(*) FROM profiles WHERE notifications_enabled = TRUE")
        total = await conn.fetchval("SELECT COUNT(*) FROM profiles")
        channels = await conn.fetch(
            """SELECT channel,
                      CASE WHEN COUNT(*) >= %d THEN COUNT(*) ELSE NULL END AS total
               FROM agent_actions GROUP BY channel ORDER BY total DESC NULLS LAST"""
            % MIN_BUCKET)
    return {
        "success": True,
        "notificationsEnabled": int(opted or 0),
        "totalUsers": int(total or 0),
        "optInRate": round((opted or 0) / total, 4) if total else 0.0,
        "actionsByChannel": [{"channel": r["channel"], "total": r["total"]} for r in channels],
    }


@router.get("/api/admin/metrics/feature-usage")
async def metrics_feature_usage(
    _auth=Depends(require_service_token),
    limit: int = Query(15, ge=1, le=50),
    days: int = Query(30, ge=1, le=365),
):
    """Features mais usadas: top audit_logs.action por contagem.

    Seleciona so a coluna action — metadata (que pode carregar conteudo
    digitado pelo usuario) NUNCA e tocado por esta rota.
    """
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT action,
                      CASE WHEN COUNT(*) >= %d THEN COUNT(*) ELSE NULL END AS total
               FROM audit_logs
               WHERE created_at >= NOW() - ($1 || ' days')::INTERVAL
               GROUP BY action ORDER BY total DESC NULLS LAST LIMIT $2"""
            % MIN_BUCKET,
            str(days), limit)
    return {
        "success": True,
        "days": days,
        "topActions": [{"action": r["action"], "total": r["total"]} for r in rows],
    }


@router.get("/api/admin/blocked-ips")
async def list_blocked_ips_admin(
    request: Request,
    _auth=Depends(require_service_token),
    limit: int = Query(200, ge=1, le=500),
):
    """Lista IPs bloqueados pelo StrictRateLimiter (security_blocked_ips).

    Essa e a tabela autoritativa: o middleware do gateway bloqueia de
    verdade. A 'blocked_ips' do portal era uma copia divergente.
    """
    await _moderation_operator(request)
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ip, reason, violations, blocked_until, created_at, updated_at
               FROM security_blocked_ips
               ORDER BY COALESCE(blocked_until, NOW()) DESC
               LIMIT $1""",
            limit)
    return {
        "success": True,
        "data": [{
            "ip": r["ip"],
            "reason": r["reason"],
            "violations": r["violations"],
            "blockedUntil": r["blocked_until"].isoformat() if r["blocked_until"] else None,
            "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
        } for r in rows],
    }


@router.post("/blocked-ips", include_in_schema=False)
@router.post("/api/admin/blocked-ips")
async def create_blocked_ip(
    body: BlockedIpCreate,
    request: Request,
    _auth=Depends(require_service_token),
):
    """Cria ou atualiza um bloqueio de IP manual na tabela security_blocked_ips.

    O prazo padrão é de 24h; o portal pode informar até 30 dias. O campo violations e
    setado para 3 para garantir que o middleware nao desbloqueie
    automaticamente antes do prazo.
    """
    email = await _moderation_operator(request)
    body.reason = _moderation_reason(body.reason, request)
    try:
        body.ip = str(ipaddress.ip_address(body.ip))
    except ValueError:
        raise HTTPException(422, "IP inválido.")
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _moderation_audit(conn, email, request, 'block-ip', body.reason, ip=body.ip)
            await conn.execute(
            """
            INSERT INTO security_blocked_ips (ip, reason, violations, blocked_until, created_at, updated_at)
            VALUES ($1, $2, 3, NOW() + ($3 * INTERVAL '1 minute'), NOW(), NOW())
            ON CONFLICT (ip) DO UPDATE SET
                reason = EXCLUDED.reason,
                violations = 3,
                blocked_until = EXCLUDED.blocked_until,
                updated_at = NOW();
            """,
            body.ip,
            body.reason,
            body.minutes,
        )
    if _state.rate_limiter is not None:
        await _state.rate_limiter.set_manual_block(body.ip, True, seconds=body.minutes * 60)
    print(f"[ADMIN] IP bloqueado: {body.ip}")
    return {"success": True, "ip": body.ip, "reason": body.reason}


@router.delete("/blocked-ips/{ip}", include_in_schema=False)
@router.delete("/api/admin/blocked-ips/{ip}")
async def delete_blocked_ip(
    request: Request,
    _auth=Depends(require_service_token),
    ip: str = None,
):
    """Remove um IP da tabela security_blocked_ips, desbloqueando-o imediatamente."""
    email = await _moderation_operator(request)
    try:
        ip = str(ipaddress.ip_address(ip))
    except ValueError:
        raise HTTPException(422, "IP inválido.")
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _moderation_audit(conn, email, request, 'unblock-ip', 'Desbloqueio manual confirmado', ip=ip)
            await conn.execute("DELETE FROM security_blocked_ips WHERE ip = $1", ip)
    if _state.rate_limiter is not None:
        await _state.rate_limiter.set_manual_block(ip, False)
    print(f"[ADMIN] IP desbloqueado: {ip}")
    return {"success": True, "ip": ip}


@router.get("/api/admin/blocked-ips/check")
async def check_blocked_ip(request: Request, ip: str, _auth=Depends(require_service_token)):
    # Consulta somente booleana também usada pela defesa do portal antes do login.
    # O portal exige sessão/role na tela; mutações e listagem exigem operador aqui.
    try:
        ip = str(ipaddress.ip_address(ip))
    except ValueError:
        raise HTTPException(422, "IP inválido.")
    async with _require_pool().acquire() as conn:
        blocked = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM security_blocked_ips WHERE ip=$1 AND blocked_until > NOW())", ip)
    return {"blocked": bool(blocked)}


# ---- Proxy de autenticacao (login) -------------------------------------
#
# Contexto (doc 12): o portal roda no Render, FORA da VPC, e o RDS e
# privado — o pool do Next.js nao consegue conectar (500 no login). O
# gateway, na EC2, e quem tem conectividade. Este proxy recebe as
# credenciais que o portal ja validou contra o Turnstile, verifica no
# RDS e devolve o VEREDTO. O token de sessao (HMAC com
# ADMIN_SESSION_SECRET) continua sendo cunhado SO no portal — a secret
# nao cruza a rede, e o gateway nunca aprende um token valido.
#
# Seguranca:
#  - compare_digest no bcrypt (nao ha timing leak do hash)
#  - resposta identica para "usuario inexistente" e "senha errada"
#    (fail-closed, sem vazamento de existencia)
#  - nao devolve o hash, segredos TOTP nem codigos de recuperacao

# A mitigacao de timing side-channel para "usuario inexistente" e feita
# por auth_crypto.verify_missing_user (Argon2id dummy pre-computado),
# que gasta o mesmo tempo criptografico de um login real.
# (Antigo _DUMMY_BCRYPT_HASH removido na migracao SPEC-10.)

class AdminLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/api/admin/login")
async def admin_login(
    req: AdminLoginRequest,
    _auth=Depends(require_service_token),
):
    pool = _require_pool()
    email = (req.email or "").strip().lower()
    if not email or not req.password:
        # Mesma mensagem do fail-closed abaixo — nao vaza qual faltou.
        raise HTTPException(401, "Credenciais inválidas.")

    await auth_limit(pool, "admin_login", email, 10, 300)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, email, password_hash, role, totp_enabled, session_version
               FROM admin_users WHERE email = $1 LIMIT 1""",
            email)

    if row is None:
        # Mitigacao de timing side-channel: gasta o mesmo tempo de Argon2id
        # de um login real para nao vazar existencia do e-mail.
        await asyncio.to_thread(verify_missing_user, req.password)
        raise HTTPException(401, "Credenciais inválidas.")

    stored_hash = row["password_hash"] or ""
    password_ok, needs_rehash = await asyncio.to_thread(
        verify_password, req.password, stored_hash
    )

    if not password_ok:
        raise HTTPException(401, "Credenciais inválidas.")

    # Lazy re-hashing (SPEC-10 §5): hash Bcrypt legado valido e convertido
    # para Argon2id + Pepper de forma transparente no primeiro login bem
    # sucedido, sem exigir redefinicao de senha do usuario.
    if needs_rehash:
        new_hash = await asyncio.to_thread(hash_password, req.password)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE admin_users SET password_hash = $1 WHERE id = $2",
                new_hash, row["id"],
            )

    return {
        "success": True,
        "user": {
            "id": row["id"],
            "email": row["email"],
            "role": row["role"] or "admin",
            "totp_enabled": bool(row["totp_enabled"]) if "totp_enabled" in row.keys() else False,
            "session_version": row["session_version"],
        },
    }


class AdminRegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    master_totp_code: str


@router.post("/api/admin/register")
async def admin_register(
    req: AdminRegisterRequest,
    _auth=Depends(require_service_token),
):
    """Cadastra um admin novo (proxy — doc 12).

    O codigo TOTP MASTER (ADMIN_TOTP_SECRET) e validado AQUI no gateway:
    e a prova de que quem chama tem autorizacao de criar admins. Sem isso,
    qualquer um com o SERVICE_TOKEN criaria contas. O hash Argon2id +
    Pepper e gerado nesta ponta (SPEC-10) — o portal nunca envia
    texto-plano que precise ser confiavel, e o hash nunca cruza de volta.
    """
    pool = _require_pool()
    email = (req.email or "").strip().lower()
    name = (req.name or "").strip()
    password = req.password or ""

    # 1. Validacoes de formato (espelham o register original)
    if len(name) < 2:
        raise HTTPException(400, "O nome deve ter no mínimo 2 caracteres.")
    if not email or "@" not in email:
        raise HTTPException(400, "Informe um endereço de e-mail válido.")
    if len(password) < 8 or len(password.encode("utf-8")) > 72:
        raise HTTPException(400, "A senha deve ter no mínimo 8 caracteres.")

    # Mesmo segredo mestre e limite compartilhado com a recuperação.
    await auth_limit(pool, "master", "global", 5, 60)
    step, fingerprint = master_step(req.master_totp_code)

    # 3. Duplicidade
    async with pool.acquire() as conn, conn.transaction():
        existing = await conn.fetchval(
            "SELECT id FROM admin_users WHERE email = $1 LIMIT 1", email)
        if existing:
            raise HTTPException(409, "Já existe um usuário cadastrado com este e-mail.")

        await consume_master(conn, step, fingerprint)

        # 4. Hash Argon2id + Pepper (SPEC-10). Novas contas nascem direto
        #    no padrao moderno, sem depender de lazy migration.
        password_hash = await asyncio.to_thread(hash_password, password)
        user_id = f"usr_{__import__('uuid').uuid4()}"
        await conn.execute(
            """INSERT INTO admin_users (id, email, password_hash, name, role, created_at, last_login_at)
               VALUES ($1, $2, $3, $4, 'admin', NOW(), NOW())""",
            user_id, email, password_hash, name)
        # O TOTP mestre autoriza também a frente editorial, sem allowlist de e-mail.
        await conn.execute(
            "INSERT INTO editorial_members(email,role) VALUES($1,'publisher') ON CONFLICT(email) DO NOTHING",
            email)

    return {
        "success": True,
        "user": {"id": user_id, "email": email, "name": name, "role": "admin"},
    }


# ---- TOTP (conta do admin) -----------------------------------------------
#
# Contexto (D-WS4): o portal nao acessa o RDS direto. Todas as operacoes
# TOTP passam pelo gateway, que valida SERVICE_TOKEN e consulta o banco.
# Recovery codes usam bcrypt (iguais ao login-step2 original).
#
# Seguranca:
#  - Timing-safe comparison em todas as verificacoes
#  - Fail-closed: credenciais invalidas retornam 401 identico
#  - TOTP usa 30s window (RFC 6238), mesmo padrao do admin register


def _totp_generate_secret() -> str:
    """Gera segredo Base32 de 32 chars (mesmo padrao do webservice)."""
    import secrets as _s
    raw = _s.token_bytes(20)
    return base64.b32encode(raw).decode("ascii").replace("=", "")


def _totp_verify(secret_b32: str, code: str, step_seconds: int = 30) -> bool:
    """Verifica TOTP com janela +/-1 (tolerancia de relogio descompassado)."""
    try:
        clean = secret_b32.strip().replace(" ", "").rstrip("=").upper()
        key = base64.b32decode(clean + "=" * (-len(clean) % 8), casefold=True)
        submitted = code.strip()
        if len(submitted) != 6 or not submitted.isascii() or not submitted.isdigit():
            return False
        now = int(time.time())
        for counter in (now // step_seconds - 1, now // step_seconds, now // step_seconds + 1):
            msg = struct.pack(">Q", counter)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            offset = h[-1] & 0x0F
            binary = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
            if hmac.compare_digest(f"{binary % 1000000:06d}", submitted):
                return True
        return False
    except Exception:
        return False


# Modelos de request

class LoginStep2Request(BaseModel):
    email: str
    password: str
    totpCode: Optional[str] = None
    recoveryCode: Optional[str] = None


class TotpEnrollRequest(BaseModel):
    email: str


class TotpDisableRequest(BaseModel):
    email: str
    totpCode: str


@router.post("/api/admin/totp/confirm")
async def admin_totp_confirm(
    req: TotpDisableRequest, request: Request, _auth=Depends(require_service_token),
):
    import asyncio
    import bcrypt

    email = _portal_identity(request)
    if email != req.email.strip().lower():
        raise HTTPException(403, "Identidade divergente.")
    pool = _require_pool()
    await auth_limit(pool, "totp_confirm", email, 5, 300)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, totp_enabled, totp_secret FROM admin_users WHERE email = $1 FOR UPDATE", email)
            if not row or row["totp_enabled"] or not row["totp_secret"]:
                raise HTTPException(409, "Nenhum vínculo pendente. Gere um novo QR code.")
            if not _totp_verify(row["totp_secret"], req.totpCode):
                raise HTTPException(401, "Código inválido ou expirado. Verifique o horário do celular.")
            codes = ["-".join(secrets.token_hex(2).upper() for _ in range(3)) for _ in range(8)]
            hashes = [await asyncio.to_thread(bcrypt.hashpw, c.encode(), bcrypt.gensalt(10)) for c in codes]
            await conn.execute(
                "UPDATE admin_users SET totp_enabled = TRUE, totp_recovery_codes = $2 WHERE id = $1",
                row["id"], [h.decode() for h in hashes])
    return {"success": True, "recoveryCodes": codes}


@router.post("/api/admin/login-step2")
async def admin_login_step2(
    req: LoginStep2Request,
    _auth=Depends(require_service_token),
):
    """Segundo passo do login admin: valida senha + TOTP (ou recovery code).

    O token de sessao (HMAC/ADMIN_SESSION_SECRET) e gerado no portal —
    este endpoint apenas valida as credenciais e devolve o usuario.
    Fail-closed: resposta identica para email inexistente e senha errada.
    """
    import bcrypt as _bcrypt  # apenas para comparacao de recovery codes

    pool = _require_pool()
    email = (req.email or "").strip().lower()
    password = req.password or ""

    if not email or not password:
        raise HTTPException(401, "Credenciais inválidas.")

    await auth_limit(pool, "admin_login_step2", email, 10, 300)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, email, password_hash, name, role,
                      totp_enabled, totp_secret, totp_recovery_codes, session_version
               FROM admin_users WHERE email = $1 LIMIT 1""",
            email)

    if row is None:
        # Mitigacao de timing side-channel (mesmo do login simples).
        await asyncio.to_thread(verify_missing_user, password)
        raise HTTPException(401, "Credenciais inválidas.")

    stored_hash = row["password_hash"] or ""
    password_ok, needs_rehash = await asyncio.to_thread(
        verify_password, password, stored_hash
    )

    if not password_ok:
        raise HTTPException(401, "Credenciais inválidas.")

    # Lazy re-hashing (SPEC-10 §5): hash Bcrypt legado valido convertido
    # para Argon2id + Pepper no primeiro login bem sucedido.
    if needs_rehash:
        new_hash = await asyncio.to_thread(hash_password, password)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE admin_users SET password_hash = $1 WHERE id = $2",
                new_hash, row["id"],
            )
    # Segundo fator: exige TOTP ativo
    if not row["totp_enabled"] or not row["totp_secret"]:
        raise HTTPException(400, "Autenticação em dois fatores não está ativa nesta conta.")

    # Aceita TOTP de 6 digitos OU recovery code
    totp_code = (req.totpCode or "").replace(" ", "").replace("-", "")
    recovery_raw = (req.recoveryCode or "").strip().upper()

    auth_ok = False
    if totp_code:
        # TOTP padrao: 6 digitos, janela de 30s
        if len(totp_code) == 6 and totp_code.isdigit():
            auth_ok = _totp_verify(row["totp_secret"], totp_code)
    elif recovery_raw:
        # Recovery code: bcrypt comparison (uso unico)
        codes_raw: list = row["totp_recovery_codes"] or []
        for i, hashed in enumerate(codes_raw):
            try:
                if _bcrypt.checkpw(recovery_raw.encode("utf-8"), hashed.encode("utf-8")):
                    # Consome o codigo: remove da lista
                    original_codes = list(codes_raw)
                    codes_raw = [c for j, c in enumerate(codes_raw) if j != i]
                    auth_ok = True
                    break
            except Exception:
                continue
        if auth_ok:
            # Persiste a remocao do recovery code
            async with pool.acquire() as conn:
                consumed = await conn.fetchval(
                    """UPDATE admin_users SET totp_recovery_codes = $2
                       WHERE id = $1 AND totp_recovery_codes = $3 RETURNING id""",
                    row["id"], codes_raw, original_codes)
                auth_ok = consumed is not None

    if not auth_ok:
        raise HTTPException(401, "Código inválido ou expirado.")

    # Atualiza last_login_at
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE admin_users SET last_login_at = NOW() WHERE id = $1",
            row["id"])

    return {
        "success": True,
        "user": {
            "id": row["id"],
            "email": row["email"],
            "name": row["name"],
            "session_version": row["session_version"],
            "role": row["role"] or "admin",
        },
    }


@router.post("/api/admin/totp/enroll")
async def admin_totp_enroll(
    req: TotpEnrollRequest,
    request: Request,
    _auth=Depends(require_service_token),
):
    """Gera segredo TOTP pendente para a conta (ativa apos confirmacao).

    Somente contas sem 2FA ativo podem gerar outro segredo pendente.
    Um vínculo ativo exige desativação autorizada antes da substituição.
    """

    pool = _require_pool()
    email = _portal_identity(request)
    if email != req.email.strip().lower():
        raise HTTPException(403, "Identidade divergente.")
    await auth_limit(pool, "totp_enroll", email, 5, 300)
    if not email:
        raise HTTPException(400, "E-mail obrigatorio.")

    secret = _totp_generate_secret()
    # URI otpauth para o QR code (issuer fixo BragaSaude)
    issuer_escaped = "BragaSaude"
    from urllib.parse import quote
    account_escaped = quote(email, safe="")
    otpauth_uri = (
        f"otpauth://totp/{issuer_escaped}:{account_escaped}"
        f"?secret={secret}&issuer={issuer_escaped}"
        f"&algorithm=SHA1&digits=6&period=30"
    )

    async with pool.acquire() as conn:
        changed = await conn.fetchval(
            """UPDATE admin_users
               SET totp_secret = $1, totp_recovery_codes = '{}'::text[]
               WHERE email = $2 AND totp_enabled = FALSE RETURNING id""",
            secret, email)
        if not changed:
            raise HTTPException(409, "Conta inexistente ou 2FA já ativo. Desative com o código atual antes de vincular novamente.")

    # O segredo só é entregue à própria conta durante o vínculo.
    return {
        "success": True,
        "otpauthUri": otpauth_uri,
        # Permite configuração manual do autenticador além do QR code.
        "secret": secret,
    }


@router.get("/api/admin/totp/status")
async def admin_totp_status(
    request: Request,
    _auth=Depends(require_service_token),
):
    """Retorna se TOTP esta ativo e quantos recovery codes restam.

    Authorization autentica o serviço; X-Admin-Identity contém a
    identidade assinada da sessão validada pelo portal, com escopo e expiração.
    """
    # Valida a assinatura e o escopo da identidade, separadamente do token de serviço.
    email = _portal_identity(request)

    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT totp_enabled,
                      cardinality(COALESCE(totp_recovery_codes, '{}'::text[])) AS recovery_left
               FROM admin_users WHERE email = $1 LIMIT 1""",
            email)
    if not row:
        raise HTTPException(404, "Conta não encontrada.")

    return {
        "success": True,
        "enabled": bool(row["totp_enabled"]),
        "recoveryLeft": int(row["recovery_left"] or 0),
    }


@router.post("/api/admin/totp/disable")
async def admin_totp_disable(
    req: TotpDisableRequest,
    request: Request,
    _auth=Depends(require_service_token),
):
    """Desabilita TOTP da conta — exige codigo atual para confirmar.

    Limpa segredo e zera recovery codes (precisa gerar novos se reativar).
    """
    pool = _require_pool()
    email = _portal_identity(request)
    if email != req.email.strip().lower():
        raise HTTPException(403, "Identidade divergente.")
    await auth_limit(pool, "totp_disable", email, 5, 300)
    totp_code = (req.totpCode or "").replace(" ", "").replace("-", "")

    if not totp_code or len(totp_code) != 6 or not totp_code.isdigit():
        raise HTTPException(400, "O código deve ter 6 dígitos numéricos.")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, totp_enabled, totp_secret FROM admin_users WHERE email = $1 LIMIT 1",
            email)
    if not row:
        raise HTTPException(404, "Conta não encontrada.")

    if not row["totp_enabled"] or not row["totp_secret"]:
        raise HTTPException(400, "Autenticação em dois fatores não está ativa nesta conta.")

    if not _totp_verify(row["totp_secret"], totp_code):
        raise HTTPException(401, "Código do autenticador inválido ou expirado.")

    async with pool.acquire() as conn:
        changed = await conn.fetchval(
            """UPDATE admin_users
               SET totp_enabled = FALSE, totp_secret = NULL,
                   totp_recovery_codes = '{}'::text[]
               WHERE id = $1 AND totp_secret = $2 AND totp_enabled = TRUE RETURNING id""",
            row["id"], row["totp_secret"])
        if not changed:
            raise HTTPException(409, "O vínculo mudou. Atualize a página.")

    return {"success": True, "message": "Autenticação em dois fatores desativada."}


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=72)
    master_totp_code: str = Field(min_length=6, max_length=6)


@router.post("/api/admin/password-reset")
async def admin_password_reset(req: PasswordResetRequest, _auth=Depends(require_service_token)):
    pool = _require_pool()
    # Global: trocar o e-mail ou o IP não aumenta as tentativas sobre o segredo mestre.
    await auth_limit(pool, "master", "global", 5, 60)
    step, fingerprint = master_step(req.master_totp_code)
    if len(req.password.encode("utf-8")) > 72:
        raise HTTPException(400, "A senha excede 72 bytes.")
    # Hash Argon2id + Pepper (SPEC-10) — redefinicao gera hash moderno.
    hashed = await asyncio.to_thread(hash_password, req.password)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await consume_master(conn, step, fingerprint)
            # Preserva o segundo fator da conta. A versao revoga sessoes existentes.
            await conn.execute(
                """UPDATE admin_users SET password_hash = $2, session_version = session_version + 1
                   WHERE email = $1""", req.email.strip().lower(), hashed)
    return {"success": True, "message": "Se a conta existir, a senha foi alterada. Entre novamente."}




class SessionCheckRequest(BaseModel):
    email: str = Field(max_length=254)
    version: int = Field(ge=0)


@router.post("/api/admin/session-check")
async def admin_session_check(req: SessionCheckRequest, request: Request, _auth=Depends(require_service_token)):
    email = _portal_identity(request)
    if email != req.email:
        raise HTTPException(403, "Identidade divergente.")
    async with _require_pool().acquire() as conn:
        version = await conn.fetchval("SELECT session_version FROM admin_users WHERE email = $1", email)
    return {"valid": version is not None and version == req.version}


class ChallengeRequest(BaseModel):
    id: str = Field(min_length=32, max_length=64)
    binding: str = Field(min_length=64, max_length=64)
    purpose: str = Field(pattern="^(login|register|reset)$")
    answer: int = Field(ge=0, le=260)


@router.post("/api/admin/challenge/create")
async def admin_challenge_create(req: ChallengeRequest, _auth=Depends(require_service_token)):
    pool = _require_pool()
    await auth_limit(pool, "challenge_issue", req.binding, 15, 300)
    await auth_limit(pool, "challenge_capacity", "global", 300, 60)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM admin_challenges WHERE expires_at < NOW()")
        await conn.execute(
            """INSERT INTO admin_challenges (id, binding, purpose, answer, expires_at)
               VALUES ($1, $2, $3, $4, NOW() + interval '2 minutes')""",
            req.id, req.binding, req.purpose, req.answer)
    return {"success": True}


@router.post("/api/admin/challenge/check")
async def admin_challenge_check(req: ChallengeRequest, _auth=Depends(require_service_token)):
    """Avalia a posicao solta pelo operador no momento do release.

    O DELETE atomico consome o desafio em QUALQUER tentativa: soltar fora do
    alvo invalida a linha e o cliente gera outro automaticamente. Acerto com
    precisao >= 98.2% (erro <= 4px em 260) devolve a linha marcada como
    resolvida para o submit final. Nao ha idade minima: o corte de 4px ja e a
    barreira, e exigir espera punia o operador rapido (sonda em producao:
    desafio correto em 337ms era rejeitado) sem impedir script nenhum.
    """
    pool = _require_pool()
    await auth_limit(pool, "challenge_check", req.binding, 15, 300)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """DELETE FROM admin_challenges WHERE id = $1 AND binding = $2 AND purpose = $3
               AND expires_at > NOW()
               RETURNING answer""",
            req.id, req.binding, req.purpose)
    if row is None:
        return {"success": False}
    precision = (1 - abs(row["answer"] - req.answer) / 260) * 100
    if precision < 98.2:
        return {"success": False}
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO admin_challenges (id, binding, purpose, answer, expires_at, solved)
               VALUES ($1, $2, $3, $4, NOW() + interval '2 minutes', TRUE)""",
            req.id, req.binding, req.purpose, row["answer"])
    return {"success": True, "precision": round(precision, 1)}


@router.post("/api/admin/challenge/verify")
async def admin_challenge_verify(req: ChallengeRequest, _auth=Depends(require_service_token)):
    async with _require_pool().acquire() as conn:
        # DELETE é atômico e consome inclusive tentativas erradas em qualquer réplica.
        row = await conn.fetchrow(
            """DELETE FROM admin_challenges WHERE id = $1 AND binding = $2 AND purpose = $3
               RETURNING solved, expires_at > NOW() AS fresh""",
            req.id, req.binding, req.purpose)
    # O desafio so vale se o check o marcou resolvido (precisao >= 98.2%) e
    # a linha ainda esta fresca.
    return {"success": bool(row and row["fresh"] and row["solved"])}



class DeleteAccountRequest(BaseModel):
    userId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


async def _moderation_operator(request):
    email = _portal_identity(request)
    pool = _require_pool()
    await auth_limit(pool, 'moderation', email, 30, 60)
    async with pool.acquire() as conn:
        role = await conn.fetchval('SELECT role FROM admin_users WHERE email=$1', email)
    if role not in ('admin', 'superadmin'):
        raise HTTPException(403, 'Permissão administrativa necessária.')
    return email


def _moderation_reason(reason, request):
    clean = sanitize_zero_trust_text(reason, max_length=500, field_name='motivo',
                                    ip=get_client_ip(request), rate_limiter=_state.rate_limiter,
                                    pool=_state.db_pool).strip()
    if not clean:
        raise HTTPException(422, 'Motivo obrigatório.')
    return clean


async def _moderation_audit(conn, email, request, action, reason, *, uid=None, ip=None):
    await conn.execute(
        'INSERT INTO moderation_audit_log(actor_email,ip,target_uid,target_ip,action,reason) VALUES ($1,$2,$3,$4,$5,$6)',
        email, get_client_ip(request), uid, ip, action, reason)


class UserRestrictionCreate(DeleteAccountRequest):
    level: str = Field(pattern=r'^(read-only|no-sync|suspended)$')
    reason: str = Field(min_length=1, max_length=500)
    minutes: Optional[int] = Field(default=None, ge=1, le=43200)


@router.get('/api/admin/social-reports')
async def list_social_reports(request: Request,
                              status: Literal['pending', 'dismissed', 'removed'] = 'pending',
                              limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                              _auth=Depends(require_service_token)):
    await _moderation_operator(request)
    async with _require_pool().acquire() as conn:
        rows = await conn.fetch('''SELECT r.*, sp.title, sp.description, sp.created_at AS post_created_at,
            sp.is_visible, p.full_name AS author_name, p.community_nickname AS author_nickname
            FROM social_reports r JOIN social_posts sp ON sp.id=r.post_id
            JOIN profiles p ON p.id=r.author_id
            WHERE r.status=$1 ORDER BY r.created_at DESC,r.id DESC LIMIT $2 OFFSET $3''',
            status, limit + 1, offset)
    return {'success': True, 'data': [dict(r) for r in rows[:limit]],
            'has_more': len(rows) > limit, 'limit': limit, 'offset': offset}


class SocialReportAction(BaseModel):
    action: Literal['dismiss', 'remove_post']


@router.post('/api/admin/social-reports/{report_id}/action')
async def act_on_social_report(report_id: uuid.UUID, body: SocialReportAction, request: Request,
                               _auth=Depends(require_service_token)):
    email = await _moderation_operator(request)
    result_status = 'removed' if body.action == 'remove_post' else 'dismissed'
    async with _require_pool().acquire() as conn:
        async with conn.transaction():
            post_id = await conn.fetchval('SELECT post_id FROM social_reports WHERE id=$1', report_id)
            if post_id is None:
                raise HTTPException(404, 'Denúncia não encontrada.')
            # Mesmo lock primeiro para todas as denúncias do post: decisões concorrentes
            # não se sobrescrevem e a remoção resolve a fila inteira em uma transação.
            await conn.fetchval('SELECT id FROM social_posts WHERE id=$1 FOR UPDATE', post_id)
            report = await conn.fetchrow('SELECT author_id,status FROM social_reports WHERE id=$1 FOR UPDATE', report_id)
            if not report:
                raise HTTPException(404, 'Denúncia não encontrada.')
            if report['status'] != 'pending':
                if report['status'] == result_status:
                    return {'success': True, 'status': result_status}
                raise HTTPException(409, 'Denúncia já revisada. Atualize a lista.')
            if body.action == 'remove_post':
                await conn.execute('UPDATE social_posts SET is_visible=FALSE WHERE id=$1', post_id)
                await conn.execute("""UPDATE social_reports SET status='removed',reviewed_at=NOW(),reviewed_by=$2
                    WHERE post_id=$1 AND status='pending'""", post_id, email)
            else:
                await conn.execute("""UPDATE social_reports SET status='dismissed',reviewed_at=NOW(),reviewed_by=$2
                    WHERE id=$1""", report_id, email)
            await _moderation_audit(conn, email, request, 'social-' + body.action,
                                    f'Denúncia {report_id}; publicação {post_id}', uid=report['author_id'])
    return {'success': True, 'status': result_status}


@router.get('/api/admin/security-activity')
async def security_activity(request: Request, groupBy: str = Query('ip', pattern='^(ip|uid)$'),
                            minutes: int = Query(60, ge=15, le=1440),
                            limit: int = Query(100, ge=1, le=200),
                            _auth=Depends(require_service_token)):
    await _moderation_operator(request)
    column = 'ip' if groupBy == 'ip' else 'user_id'
    from security_telemetry import SIGNALS
    async with _require_pool().acquire() as conn:
        rows = await conn.fetch(f'''WITH recent AS (
            SELECT *, greatest(0,1-extract(epoch FROM (NOW()-occurred_at))/900.0)*weight AS risk
            FROM security_request_events WHERE occurred_at > NOW()-($1::integer*INTERVAL '1 minute')
        ), minute_counts AS (
            SELECT {column} AS target,date_trunc('minute',occurred_at) AS minute,COUNT(*) AS requests
            FROM recent GROUP BY {column},date_trunc('minute',occurred_at)
        ) SELECT {column} AS target,COUNT(*) AS requests,
            COUNT(*) FILTER(WHERE status>=400 AND status<500) AS refused,
            MIN(occurred_at) AS first_seen,MAX(occurred_at) AS last_seen,
            SUM(risk)::double precision AS score,
            array_remove(array_agg(DISTINCT ip),NULL) AS ips,
            array_remove(array_agg(DISTINCT user_id),NULL) AS uids,
            array_remove(array_agg(DISTINCT country),NULL) AS countries,
            array_remove(array_agg(DISTINCT region),NULL) AS regions,
            array_agg(DISTINCT signal) FILTER(WHERE weight>0) AS signals,
            (SELECT MAX(requests) FROM minute_counts m WHERE m.target=recent.{column}) AS peak_per_minute
            FROM recent WHERE {column} IS NOT NULL AND {column}<>'unknown'
            GROUP BY {column} HAVING SUM(weight)>0
            ORDER BY score DESC,last_seen DESC LIMIT $2''', minutes, limit)
    data = []
    for row in rows:
        item = dict(row)
        item['level'] = 'high' if item['score'] >= 60 else 'attention' if item['score'] >= 20 else 'normal'
        item['reasons'] = [SIGNALS[s][0] for s in (item.pop('signals') or []) if s in SIGNALS]
        for key in ('ips', 'uids', 'countries', 'regions'):
            item[key] = item[key][:10]
        data.append(item)
    return {'success': True, 'data': data, 'minutes': minutes,
            'riskWindowMinutes': 15, 'thresholds': {'attention': 20, 'high': 60},
            'telemetry': _state.telemetry.health() if _state.telemetry else None}


@router.get('/api/admin/security-events')
async def security_events(request: Request, ip: Optional[str] = None, userId: Optional[str] = None,
                          minutes: int = Query(60, ge=15, le=1440),
                          limit: int = Query(100, ge=1, le=200),
                          _auth=Depends(require_service_token)):
    await _moderation_operator(request)
    if userId is not None:
        _validate_uid(userId)
    if ip is not None:
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            raise HTTPException(422, 'IP inválido.')
    from security_telemetry import SIGNALS
    async with _require_pool().acquire() as conn:
        rows = await conn.fetch('''SELECT request_id,occurred_at,ip,user_id,country,region,
            route,method,status,duration_ms,signal,app_version,ray_id,connection_ip,source
            FROM security_request_events
            WHERE occurred_at>NOW()-($1::integer*INTERVAL '1 minute')
                AND ($2::text IS NULL OR ip=$2) AND ($3::text IS NULL OR user_id=$3)
            ORDER BY occurred_at DESC,id DESC LIMIT $4''', minutes, ip, userId, limit)
    data = []
    for row in rows:
        item = dict(row)
        item['reason'] = SIGNALS.get(item['signal'], ('Evento recebido', 0))[0]
        data.append(item)
    return {'success': True, 'data': data}


def _validate_uid(uid):
    import re
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', uid):
        raise HTTPException(422, 'Use o UID da conta.')
    return uid


@router.get('/api/admin/user-restrictions')
async def get_user_restriction(request: Request, userId: str, _auth=Depends(require_service_token)):
    await _moderation_operator(request)
    _validate_uid(userId)
    async with _require_pool().acquire() as conn:
        row = await conn.fetchrow('SELECT * FROM user_restrictions WHERE user_id=$1 AND (expires_at IS NULL OR expires_at>NOW())', userId)
    return {'success': True, 'data': dict(row) if row else None}


@router.post('/api/admin/user-restrictions')
async def restrict_user(body: UserRestrictionCreate, request: Request, _auth=Depends(require_service_token)):
    email = await _moderation_operator(request)
    reason = _moderation_reason(body.reason, request)
    async with _require_pool().acquire() as conn:
        async with conn.transaction():
            version = await conn.fetchval('UPDATE profiles SET session_version=session_version+1,session_revoked_at=NOW() WHERE id=$1 RETURNING session_version', body.userId)
            if version is None:
                raise HTTPException(404, 'Conta não encontrada.')
            await conn.execute('''INSERT INTO user_restrictions(user_id,level,reason,expires_at,created_by)
                VALUES ($1,$2,$3,NOW()+($4::integer*INTERVAL '1 minute'),$5)
                ON CONFLICT(user_id) DO UPDATE SET level=EXCLUDED.level,reason=EXCLUDED.reason,
                expires_at=EXCLUDED.expires_at,created_by=EXCLUDED.created_by,updated_at=NOW()''',
                body.userId, body.level, reason, body.minutes, email)
            await _moderation_audit(conn, email, request, body.level, reason, uid=body.userId)
    return {'success': True, 'session_version': version}


@router.delete('/api/admin/user-restrictions/{userId}')
async def unrestrict_user(userId: str, request: Request, _auth=Depends(require_service_token)):
    email = await _moderation_operator(request)
    _validate_uid(userId)
    async with _require_pool().acquire() as conn:
        async with conn.transaction():
            await conn.fetchval('SELECT id FROM profiles WHERE id=$1 FOR UPDATE', userId)
            await conn.execute('DELETE FROM user_restrictions WHERE user_id=$1', userId)
            await _moderation_audit(conn, email, request, 'unrestrict', 'Remoção manual confirmada', uid=userId)
    return {'success': True}


@router.get('/api/admin/moderation-history')
async def moderation_history(request: Request, userId: Optional[str] = None, ip: Optional[str] = None,
                             _auth=Depends(require_service_token)):
    await _moderation_operator(request)
    if (userId is None) == (ip is None):
        raise HTTPException(422, 'Informe um UID ou um IP.')
    if userId is not None:
        _validate_uid(userId)
    else:
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            raise HTTPException(422, 'IP inválido.')
    async with _require_pool().acquire() as conn:
        rows = await conn.fetch('''SELECT actor_email,ip,target_uid,target_ip,action,reason,created_at
            FROM moderation_audit_log WHERE target_uid=$1 OR target_ip=$2 ORDER BY created_at DESC LIMIT 200''', userId, ip)
        deletions = await conn.fetch('SELECT admin_email,ip_address,deleted_at,tables_affected FROM user_deletion_logs WHERE user_id=$1 ORDER BY deleted_at DESC LIMIT 100', userId) if userId else []
        care = await conn.fetch('SELECT actor_id,actor_name,action_type,occurred_at FROM care_audit_trail WHERE patient_id=$1 OR actor_id=$1 ORDER BY occurred_at DESC LIMIT 100', userId) if userId else []
    return {'success': True, 'data': [dict(r) for r in rows], 'deletions': [dict(r) for r in deletions], 'care': [dict(r) for r in care]}


@router.get('/api/admin/user-moderation')
async def user_moderation_detail(request: Request, userId: str, _auth=Depends(require_service_token)):
    await _moderation_operator(request)
    _validate_uid(userId)
    async with _require_pool().acquire() as conn:
        profile = await conn.fetchrow('SELECT id,full_name,created_at,session_version FROM profiles WHERE id=$1', userId)
        if not profile:
            raise HTTPException(404, 'Conta não encontrada.')
        family = await conn.fetch("SELECT id,patient_id,caregiver_id,status FROM family_bindings WHERE (patient_id=$1 OR caregiver_id=$1) AND status='ACTIVE'", userId)
        devices = await conn.fetch('SELECT platform,role,is_active,last_seen_at FROM device_push_tokens WHERE user_id=$1 LIMIT 100', userId)
        counts = await conn.fetchrow('SELECT (SELECT COUNT(*) FROM feedbacks WHERE user_id=$1) AS feedbacks', userId)
        restriction = await conn.fetchrow('SELECT * FROM user_restrictions WHERE user_id=$1 AND (expires_at IS NULL OR expires_at>NOW())', userId)
    return {'success': True, 'profile': dict(profile), 'family': [dict(r) for r in family],
            'devices': [dict(r) for r in devices], 'counts': dict(counts),
            'restriction': dict(restriction) if restriction else None,
            'lastLogin': None, 'recentIps': [], 'unavailable': ['Último login e IPs de acesso não são registrados por UID no esquema atual.']}


@router.post("/api/admin/delete-user-data")
async def delete_user_data(body: DeleteAccountRequest, request: Request, _auth=Depends(require_service_token)):
    email = await _moderation_operator(request)
    pool = _require_pool()
    await auth_limit(pool, 'moderation_delete', email, 5, 60)
    from services.account_deletion import delete_account
    return await delete_account(pool, body.userId, actor=email, ip=get_client_ip(request))
