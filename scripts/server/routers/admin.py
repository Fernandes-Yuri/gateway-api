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
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from security_defense import get_client_ip

router = APIRouter()


class _AppState:
    """Estado injetado pelo server.py via configure_state()."""

    db_pool = None
    rate_limiter = None


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
                       f.device_info, f.status, f.created_at,
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
        row = await conn.fetchrow(
            """INSERT INTO feedback_replies (feedback_id, user_id, author, body)
               VALUES ($1, $2, $3, $4) RETURNING id, created_at""",
            fid, feedback["user_id"], author, body)
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
    _auth=Depends(require_service_token),
    limit: int = Query(200, ge=1, le=500),
):
    """Lista IPs bloqueados pelo StrictRateLimiter (security_blocked_ips).

    Essa e a tabela autoritativa: o middleware do gateway bloqueia de
    verdade. A 'blocked_ips' do portal era uma copia divergente.
    """
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

# Hash bcrypt pre-calculado (nao corresponde a nenhuma conta). Usado so
# no caminho "usuario inexistente" para igualar o tempo de resposta ao
# caminho de sucesso (timing side-channel na enumeracao de e-mails).
# Precisa ser um hash VALIDO — senao checkpw estoura ValueError e vira
# 500. Gerado com: bcrypt.hashpw(b"dummy", bcrypt.gensalt(12)).
_DUMMY_BCRYPT_HASH = (
    b"$2b$12$nmsnfVue9dXQxV6g3ENMt.q1Mxd5WdBYCuVg4FtA6FvUtLdXrWDve"
)


class AdminLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/api/admin/login")
async def admin_login(
    req: AdminLoginRequest,
    _auth=Depends(require_service_token),
):
    import bcrypt

    pool = _require_pool()
    email = (req.email or "").strip().lower()
    if not email or not req.password:
        # Mesma mensagem do fail-closed abaixo — nao vaza qual faltou.
        raise HTTPException(401, "Credenciais inválidas.")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, email, password_hash, role, totp_enabled
               FROM admin_users WHERE email = $1 LIMIT 1""",
            email)

    if row is None:
        # DIAG (remover apos estabilizar): qual e-mail chegou?
        print(f"[ADMIN-LOGIN] usuario inexistente: {email!r}")
        # Roda um bcrypt dummy para o tempo de resposta ser igual ao do
        # caminho de sucesso (mitigacao de timing side-channel na
        # enumeracao de e-mails). O hash precisa ser valido, senao o
        # checkpw estoura ValueError e vira 500 — fail-closed silencioso.
        bcrypt.checkpw(b"dummy", _DUMMY_BCRYPT_HASH)
        raise HTTPException(401, "Credenciais inválidas.")

    try:
        password_ok = bcrypt.checkpw(
            req.password.encode("utf-8"),
            row["password_hash"].encode("utf-8"),
        )
    except (ValueError, TypeError):
        # Hash malformado: trata como credencial invalida, sem estourar 500
        # (um bug de dados nao vira um bypass nem um info-leak).
        password_ok = False

    # DIAG (remover apos estabilizar): senha longa + hash prefixo
    print(f"[ADMIN-LOGIN] usuario={row['email']!r} senha_len={len(req.password)} hash_prefix={row['password_hash'][:10]!r} ok={password_ok}")

    if not password_ok:
        raise HTTPException(401, "Credenciais inválidas.")

    return {
        "success": True,
        "user": {
            "id": row["id"],
            "email": row["email"],
            "role": row["role"] or "admin",
            "totp_enabled": bool(row["totp_enabled"]) if "totp_enabled" in row.keys() else False,
        },
    }

