"""Router de autenticacao: OTP, reset de senha, boas-vindas e checagem de conta.

Extraido do monolito server.py na Fase A passo 4b
(doc 11_PLANO_MODULARIZACAO_E_TTS.md). Dominios: /api/auth/*.

Seguranca preservada: rate-limiter por IP/identifier (security_defense),
secrets para OTP (CSPRNG), transacoes Postgres, reCAPTCHA server-side,
alerta de seguranca ao dono da conta (doc 10 §4.5) e AUD-G13 (falha do
Firebase nunca e engolida com sucesso).
"""
import asyncio
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from api_identity import firebase_uid
from security_defense import get_client_ip

router = APIRouter()


class OtpSendRequest(BaseModel):
    identifier: str  # E-mail ou Telefone
    purpose: Optional[str] = "EMAIL_VERIFICATION"  # "EMAIL_VERIFICATION" ou "PASSWORD_RESET"
    channel: Optional[str] = "EMAIL"  # Somente e-mail


class OtpVerifyRequest(BaseModel):
    identifier: str
    code: str
    purpose: Optional[str] = "EMAIL_VERIFICATION"


class PasswordResetRequest(BaseModel):
    identifier: str
    resetToken: str
    newPassword: str


class WelcomeEmailRequest(BaseModel):
    email: str
    name: Optional[str] = ""


class CheckAccountRequest(BaseModel):
    identifier: str
    recaptchaToken: Optional[str] = None


async def _firebase_user_for_email(identifier: str):
    """Ausência é diferente de indisponibilidade; nunca altera uma conta na consulta."""
    if not _state.has_fcm or not _state.firebase_admin or not _state.firebase_admin._apps:
        raise HTTPException(503, "Serviço de autenticação indisponível. Tente novamente mais tarde.")
    from firebase_admin import auth as fb_auth
    try:
        return await asyncio.to_thread(fb_auth.get_user_by_email, identifier)
    except fb_auth.UserNotFoundError:
        return None
    except Exception:
        raise HTTPException(503, "Não foi possível consultar sua conta. Tente novamente mais tarde.")


@router.post("/api/auth/otp/send")
async def send_auth_otp(request: Request, req: OtpSendRequest, background_tasks: BackgroundTasks):
    """
    Gera um codigo OTP de 6 digitos numericos, armazena no PostgreSQL
    com expiracao de 15 minutos e despacha por e-mail.
    """
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível")

    identifier = req.identifier.strip().lower()
    channel = (req.channel or "").strip().upper()
    purpose = (req.purpose or "").strip().upper()
    if purpose not in {"REGISTRATION", "PASSWORD_RESET", "EMAIL_VERIFICATION"}:
        raise HTTPException(400, "Finalidade de código inválida.")

    if not identifier:
        raise HTTPException(status_code=400, detail="Identificador não informado")

    if channel != "EMAIL":
        raise HTTPException(status_code=400, detail="Canal inválido. Utilize EMAIL.")

    # Impede custos e acesso ao banco antes de gerar/enviar um novo OTP.
    client_ip = get_client_ip(request)
    await _state.rate_limiter.check("otp_send_identifier", identifier, limit=3, window_seconds=15 * 60, ip=client_ip, pool=db_pool)
    await _state.rate_limiter.check("otp_send_ip", client_ip, limit=3, window_seconds=15 * 60, ip=client_ip, pool=db_pool)

    if channel == "EMAIL" and (not _state.has_email or not _state.is_email_configured()):
        raise HTTPException(
            status_code=503,
            detail="Serviço de e-mail não configurado. Configure o Gmail SMTP no servidor.",
        )

    if purpose in {"REGISTRATION", "PASSWORD_RESET"}:
        user = await _firebase_user_for_email(identifier)
        if purpose == "REGISTRATION":
            async with db_pool.acquire() as conn:
                profile_exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM profiles WHERE lower(id)=$1 OR lower(phone)=$1)", identifier)
            if user is not None or profile_exists:
                raise HTTPException(409, "Este e-mail já possui cadastro. Faça login ou recupere sua senha.")
        elif user is None:
            raise HTTPException(404, "Não encontramos uma conta com este e-mail. Confira o endereço ou crie seu cadastro.")

    # Gera codigo aleatorio de 6 digitos. secrets (CSPRNG) e obrigatorio:
    # random.randint usa um PRNG nao criptografico e o codigo e previsivel.
    code = f"{secrets.randbelow(900000) + 100000}"
    expires_at = datetime.utcnow() + timedelta(minutes=15)

    # Para e-mail, confirma que o Gmail aceitou a mensagem antes de registrar o
    # OTP. Assim o app nao recebe uma falsa confirmacao em caso de erro SMTP.
    if channel == "EMAIL":
        delivery = await _state.send_security_code_email(identifier, code, purpose)
        if not delivery.get("success"):
            raise HTTPException(status_code=502, detail="Não foi possível enviar o código por e-mail. Tente novamente mais tarde.")

    # Invalida codigos anteriores ativos para este mesmo identificador e finalidade
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE auth_verification_codes SET is_used = TRUE WHERE identifier = $1 AND purpose = $2 AND is_used = FALSE",
            identifier, purpose
        )
        await conn.execute(
            """
            INSERT INTO auth_verification_codes (identifier, code, purpose, channel, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            identifier, code, purpose, channel, expires_at
        )

    return {
        "status": "code_dispatched",
        "channel": channel,
        "purpose": purpose,
        "expiresInSeconds": 900
    }


@router.post("/api/auth/otp/verify")
async def verify_auth_otp(request: Request, req: OtpVerifyRequest):
    """
    Valida o codigo OTP de 6 digitos digitado pelo usuario.
    Possui trava anti-brute force de no maximo 5 tentativas.
    Para 'PASSWORD_RESET', gera um resetToken seguro valido por 10 minutos.
    """
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível")

    identifier = req.identifier.strip().lower()
    code = req.code.strip()
    purpose = req.purpose.upper()

    if not identifier:
        raise HTTPException(status_code=400, detail="Identificador não informado")

    # Bloqueia rajadas de tentativa antes de consultar ou atualizar o PostgreSQL.
    client_ip = get_client_ip(request)
    await _state.rate_limiter.check("otp_verify_identifier", identifier, limit=5, window_seconds=5 * 60, ip=client_ip, pool=db_pool)
    await _state.rate_limiter.check("otp_verify_ip", client_ip, limit=5, window_seconds=5 * 60, ip=client_ip, pool=db_pool)

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, code, attempts, expires_at, is_used
                FROM auth_verification_codes
                WHERE identifier = $1 AND purpose = $2 AND is_used = FALSE
                ORDER BY created_at DESC
                LIMIT 1
                """,
                identifier, purpose
            )

            if not row:
                raise HTTPException(status_code=404, detail="Código não encontrado ou expirado. Solicite um novo código.")

            record_id = row["id"]
            stored_code = row["code"]
            attempts = row["attempts"]
            expires_at = row["expires_at"]

            # 1. Verifica limite de tentativas
            if attempts >= 5:
                await conn.execute("UPDATE auth_verification_codes SET is_used = TRUE WHERE id = $1", record_id)
                raise HTTPException(status_code=429, detail="Limite de 5 tentativas excedido. Solicite um novo código por segurança.")

            # 2. Verifica expiracao
            if datetime.utcnow().replace(tzinfo=expires_at.tzinfo) > expires_at:
                await conn.execute("UPDATE auth_verification_codes SET is_used = TRUE WHERE id = $1", record_id)
                raise HTTPException(status_code=410, detail="Código expirado (validade de 15 minutos ultrapassada).")

            # 3. Validacao do codigo
            if code != stored_code:
                await conn.execute("UPDATE auth_verification_codes SET attempts = attempts + 1 WHERE id = $1", record_id)
                remaining = 4 - attempts
                raise HTTPException(
                    status_code=400,
                    detail=f"Código incorreto. Você ainda tem {remaining} tentativa(s)."
                )

            # Codigo Correto!
            reset_token = secrets.token_urlsafe(32) if purpose == "PASSWORD_RESET" else None
            await conn.execute(
                "UPDATE auth_verification_codes SET is_used = TRUE, reset_token = $1 WHERE id = $2",
                reset_token, record_id
            )

            # Se for verificacao de e-mail, atualiza data no perfil correspondente
            if purpose == "EMAIL_VERIFICATION":
                await conn.execute(
                    "UPDATE profiles SET updated_at = NOW() WHERE phone = $1 OR emergency_contact_phone = $1 OR id = $1",
                    identifier
                )

            return {
                "status": "verified",
                "success": True,
                "purpose": purpose,
                "resetToken": reset_token
            }


@router.post("/api/auth/password/reset")
async def reset_password(req: PasswordResetRequest, request: Request):
    """
    Efetua a troca da senha do usuario apos comprovacao do resetToken obtido via OTP.
    Atualiza no Firebase Auth (se ativo) ou na persistencia de contas.
    """
    db_pool = _state.db_pool
    if not db_pool:
        raise HTTPException(status_code=503, detail="Banco de dados não disponível")

    client_ip = get_client_ip(request)
    await _state.rate_limiter.check("password_reset_ip", client_ip, limit=3, window_seconds=15 * 60, ip=client_ip, pool=db_pool)

    identifier = req.identifier.strip().lower()
    reset_token = req.resetToken.strip()
    new_password = req.newPassword

    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="A nova senha deve possuir no mínimo 6 caracteres.")

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, created_at, expires_at
                FROM auth_verification_codes
                WHERE identifier = $1 AND reset_token = $2 AND purpose = 'PASSWORD_RESET'
                  AND expires_at > NOW() AND reset_token IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                identifier, reset_token
            )

            if not row:
                raise HTTPException(status_code=403, detail="Token de redefinição inválido, expirado ou não autorizado.")

            user_record = await _firebase_user_for_email(identifier)
            if user_record is None:
                raise HTTPException(404, "A conta deste e-mail não existe mais. Confira o endereço ou crie seu cadastro.")

            # Invalida o token apos o uso
            await conn.execute("UPDATE auth_verification_codes SET reset_token = NULL WHERE id = $1", row["id"])

    try:
        from firebase_admin import auth as fb_auth
        await asyncio.to_thread(fb_auth.update_user, user_record.uid, password=new_password)
    except fb_auth.UserNotFoundError:
        raise HTTPException(404, "A conta deste e-mail não existe mais. Crie seu cadastro para continuar.")
    except Exception:
        raise HTTPException(503, "Não foi possível atualizar a senha. Solicite um novo código e tente novamente.")
    firebase_updated = True

    # doc 10 §4.5: alerta de seguranca ao dono da conta. Se a troca partiu de
    # um OTP legitimo o e-mail e so um recibo; se partiu de um invasor, o dono
    # descobre aqui. Best-effort: nunca bloqueia o sucesso da troca.
    if _state.has_email:
        try:
            await _state.send_security_alert_email(
                identifier,
                'Sua senha foi alterada',
                'Sua senha do Braga Saúde foi redefinida agora há pouco. Se você não reconhece esta alteração, sua conta pode estar sendo usada por outra pessoa.',
            )
        except Exception as e:
            print(f"[Auth Service] Falha no e-mail de alerta de segurança: {e}")

    return {
        "status": "password_updated",
        "success": True,
        "firebaseUpdated": firebase_updated,
        "message": "Senha redefinida com sucesso! Você já pode entrar com sua nova senha."
    }


@router.post("/api/auth/welcome-email")
async def send_user_welcome_email(
    req: WelcomeEmailRequest,
    background_tasks: BackgroundTasks,
    request: Request,
):
    """
    Dispara e-mail de boas-vindas acolhedor com a marca Braga Saude.

    O destino e o nome vindo do cliente sao ignorados: sem isso, qualquer um
    enviava spam ilimitado para enderecos arbitrarios.
    """
    # UID autenticado obrigatorio; destinatario derivado do proprio token.
    uid = await firebase_uid(request)
    email = ""
    name = ""
    if _state.has_email and _state.has_fcm and _state.firebase_admin._apps:
        try:
            from firebase_admin import auth as fb_auth
            fb_user = fb_auth.get_user(uid)
            email = (fb_user.email or "").strip().lower()
            name = (fb_user.display_name or "").strip()
        except Exception:
            raise HTTPException(status_code=400, detail="Não foi possível obter o perfil autenticado.")
    if not email:
        raise HTTPException(status_code=400, detail="Conta autenticada não possui e-mail.")

    if _state.has_email:
        background_tasks.add_task(_state.send_welcome_email, email, name)
        return {"status": "welcome_email_queued", "email": email}
    return {"status": "email_service_unavailable"}


@router.post("/api/auth/check-account")
async def check_account_status(request: Request, req: CheckAccountRequest):
    """
    Verifica se a conta ja existe e qual o metodo de login oficial (Google vs Direto).
    """
    db_pool = _state.db_pool
    identifier = req.identifier.strip().lower()
    if not identifier:
        raise HTTPException(status_code=400, detail="Identificador não informado")

    client_ip = get_client_ip(request)
    await _state.rate_limiter.check("check_account_ip", client_ip, limit=5, window_seconds=5 * 60, ip=client_ip, pool=db_pool)

    # Validacao server-side do Google reCAPTCHA (zero-trust): o token gerado
    # no front so e confiavel apos o Google confirma-lo aqui. Sem isso, a
    # verificacao do cliente e burlavel.
    captcha = _state.verify_recaptcha(req.recaptchaToken, expected_action="login", remote_ip=client_ip)
    if not captcha["valid"]:
        if captcha["reason"] == "disabled":
            # Secret nao configurado: nao derruba o fluxo em dev, mas registra.
            print("[reCAPTCHA] Secret ausente — verificacao pulada (RECAPTCHA_SECRET).")
        elif captcha["reason"] in ("missing_token", "rejected", "low_score", "action_mismatch"):
            raise HTTPException(status_code=403, detail="Verificação de segurança falhou. Tente novamente.")
        # verify_unavailable: o Google esta fora; nao aprova, mas nao trava o usuario legitimo.
        print(f"[reCAPTCHA] Indisponível ({captcha['reason']}) — solicitacao sem verificacao.")

    profile_exists = False
    profile_id = None

    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, full_name, phone, emergency_contact_phone
                FROM profiles
                WHERE id = $1 OR phone = $1 OR emergency_contact_phone = $1
                ORDER BY CASE WHEN id = $1 THEN 0 WHEN phone = $1 THEN 1 ELSE 2 END
                LIMIT 1
                """,
                identifier
            )
            if row:
                profile_exists = True
                profile_id = row["id"]

    fb_user = await _firebase_user_for_email(identifier) if "@" in identifier else None
    is_google_user = bool(fb_user and any(p.provider_id == "google.com" for p in fb_user.provider_data))
    if fb_user and not profile_id:
        profile_id = fb_user.uid

    # Se descobriu UID no Firebase mas ainda nao achou perfil, busca pelo UID no banco
    if fb_user and not profile_exists and db_pool:
        async with db_pool.acquire() as conn:
            p_row = await conn.fetchrow("SELECT id FROM profiles WHERE id = $1", fb_user.uid)
            if p_row:
                profile_exists = True
                profile_id = fb_user.uid

    # Um cadastro Firebase sem perfil pode estar concluindo o onboarding.
    # Consultar disponibilidade nunca autoriza excluir ou recriar essa identidade.
    # Cenario 2: Usuario com conta Google ativa
    if is_google_user:
        return {
            "exists": True,
            "provider": "GOOGLE",
            "canCreateDirect": False,
            "message": "Você já possui uma conta criada com o Google. Para sua segurança, faça login tocando em 'Continuar com o Google'."
        }

    # Cenario 3: Conta direta ja cadastrada e ativa
    if profile_exists or fb_user is not None:
        return {
            "exists": True,
            "provider": "DIRECT",
            "canCreateDirect": False,
            "message": "Esta conta já está cadastrada. Por favor, faça login com seu e-mail e senha."
        }

    # Cenario 4: Conta nova / Nao cadastrada
    return {
        "exists": False,
        "provider": None,
        "canCreateDirect": True,
        "message": "Conta disponível para cadastro."
    }


class _AppState:
    """Porta das dependencias do monolito (db_pool, rate_limiter, serviços de
    e-mail/Firebase). Preenchida por server.py no startup."""

    db_pool = None
    rate_limiter = None
    has_email = False
    has_fcm = False
    firebase_admin = None
    is_email_configured = None
    send_security_code_email = None
    send_security_alert_email = None
    send_welcome_email = None
    verify_recaptcha = None


_state = _AppState()


def configure_state(*, db_pool=None, rate_limiter=None,
                    has_email=False, has_fcm=False,
                    firebase_admin=None, is_email_configured=None,
                    send_security_code_email=None,
                    send_security_alert_email=None, send_welcome_email=None,
                    verify_recaptcha=None):
    """Chamado por server.py ao subir a aplicacao."""
    _state.db_pool = db_pool
    _state.rate_limiter = rate_limiter
    _state.has_email = has_email
    _state.has_fcm = has_fcm
    _state.firebase_admin = firebase_admin
    _state.is_email_configured = is_email_configured
    _state.send_security_code_email = send_security_code_email
    _state.send_security_alert_email = send_security_alert_email
    _state.send_welcome_email = send_welcome_email
    _state.verify_recaptcha = verify_recaptcha
