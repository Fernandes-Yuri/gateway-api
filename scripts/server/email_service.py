"""Envio de e-mails transacionais do Braga Saúde via Gmail SMTP.

As credenciais são lidas exclusivamente das variáveis de ambiente. Nunca use a
senha normal da conta Google: crie uma senha de app, com a verificação em duas
etapas habilitada, e guarde-a fora do repositório.
"""

import os
import asyncio
import html
import smtplib
import ssl
from email.message import EmailMessage
from typing import Dict, Any

GMAIL_SMTP_HOST = os.getenv("GMAIL_SMTP_HOST", "smtp.gmail.com")
GMAIL_SMTP_PORT = int(os.getenv("GMAIL_SMTP_PORT", "465"))
GMAIL_SMTP_USERNAME = os.getenv("GMAIL_SMTP_USERNAME", "").strip()
GMAIL_SMTP_APP_PASSWORD = os.getenv("GMAIL_SMTP_APP_PASSWORD", "")
GMAIL_FROM_EMAIL = os.getenv("GMAIL_FROM_EMAIL", GMAIL_SMTP_USERNAME).strip()
GMAIL_FROM_NAME = os.getenv("GMAIL_FROM_NAME", "Braga Saúde")


def is_email_configured() -> bool:
    """Indica se há credenciais suficientes para enviar e-mail pelo Gmail."""
    return bool(GMAIL_SMTP_USERNAME and GMAIL_SMTP_APP_PASSWORD and GMAIL_FROM_EMAIL)


# ==================== TEMPLATES HTML ====================

def _otp_html(code: str) -> str:
    """Template de verificação de e-mail — azul confiança."""
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Confirmação de E-mail - Braga Saúde</title>
</head>
<body style="font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Roboto,sans-serif; background:#F0F4F8; margin:0; padding:24px;">
  <div style="max-width:520px; margin:0 auto; background:#FFF; border-radius:16px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,0.08);">
    <div style="background:linear-gradient(135deg,#2563EB 0%,#1D4ED8 100%); padding:32px 24px; text-align:center; color:#FFF;">
      <h1 style="margin:0; font-size:26px; font-weight:700;">Braga Saúde</h1>
      <p style="margin:6px 0 0 0; font-size:14px; opacity:0.9;">Confirmação de Cadastro</p>
    </div>
    <div style="padding:32px 28px;">
      <p style="font-size:16px; line-height:1.6; color:#334155; margin-bottom:24px;">
        Olá!<br><br>
        <strong>Seja muito bem-vindo ao Braga Saúde!</strong> 🎉<br><br>
        Para ativar sua conta e proteger seus dados, utilize o código de verificação abaixo:
      </p>
      <div style="background:#EFF6FF; border:2px dashed #2563EB; border-radius:12px; padding:24px; text-align:center; margin:28px 0;">
        <div style="font-size:12px; font-weight:700; color:#2563EB; text-transform:uppercase; letter-spacing:1.5px; margin-bottom:8px;">Seu Código de Verificação</div>
        <div style="font-size:42px; font-weight:800; color:#1D4ED8; letter-spacing:10px; font-family:'Courier New',monospace; margin:0;">""" + code + """</div>
      </div>
      <div style="background:#F8FAFC; border-radius:8px; padding:14px 16px; font-size:14px; color:#64748B; line-height:1.6; border-left:4px solid #2563EB;">
        ⏱️ <strong>Validade:</strong> Este código expira em <strong>15 minutos</strong>.<br>
        🔒 <strong>Segurança:</strong> Se você não solicitou este cadastro, ignore este e-mail.
      </div>
    </div>
    <div style="background:#F1F5F9; padding:20px; text-align:center; font-size:12px; color:#64748B; border-top:1px solid #E2E8F0;">
      Braga Saúde • Cuidando de quem sempre cuidou de você 💚
    </div>
  </div>
</body>
</html>"""


def _reset_html(code: str) -> str:
    """Template de recuperação de senha — âmbar alerta."""
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Recuperação de Senha - Braga Saúde</title>
</head>
<body style="font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Roboto,sans-serif; background:#FFFBEB; margin:0; padding:24px;">
  <div style="max-width:520px; margin:0 auto; background:#FFF; border-radius:16px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,0.08);">
    <div style="background:linear-gradient(135deg,#F59E0B 0%,#D97706 100%); padding:32px 24px; text-align:center; color:#FFF;">
      <h1 style="margin:0; font-size:26px; font-weight:700;">Braga Saúde</h1>
      <p style="margin:6px 0 0 0; font-size:14px; opacity:0.9;">Recuperação de Acesso</p>
    </div>
    <div style="padding:32px 28px;">
      <p style="font-size:16px; line-height:1.6; color:#334155; margin-bottom:24px;">
        Olá!<br><br>
        Recebemos uma solicitação para <strong>redefinir a senha</strong> da sua conta no Braga Saúde.<br><br>
        Utilize o código de segurança abaixo para criar uma nova senha:
      </p>
      <div style="background:#FFFBEB; border:2px dashed #F59E0B; border-radius:12px; padding:24px; text-align:center; margin:28px 0;">
        <div style="font-size:12px; font-weight:700; color:#D97706; text-transform:uppercase; letter-spacing:1.5px; margin-bottom:8px;">Código de Recuperação</div>
        <div style="font-size:42px; font-weight:800; color:#B45309; letter-spacing:10px; font-family:'Courier New',monospace; margin:0;">""" + code + """</div>
      </div>
      <div style="background:#FEF3C7; border-radius:8px; padding:14px 16px; font-size:14px; color:#92400E; line-height:1.6; border-left:4px solid #F59E0B;">
        ⏱️ <strong>Validade:</strong> Este código expira em <strong>15 minutos</strong>.<br>
        🔒 <strong>Segurança:</strong> Se você não solicitou a redefinição, <strong>não se preocupe</strong> — sua senha atual continua segura. Apenas ignore este e-mail.
      </div>
    </div>
    <div style="background:#F1F5F9; padding:20px; text-align:center; font-size:12px; color:#64748B; border-top:1px solid #E2E8F0;">
      Braga Saúde • Cuidando de quem sempre cuidou de você 💚
    </div>
  </div>
</body>
</html>"""


def _welcome_html(user_name: str) -> str:
    """Template de boas-vindas — verde esmeralda da marca."""
    # AUD-G05: display_name vem do Firebase Auth (controlado pelo próprio usuário);
    # escapar antes de concatenar no HTML (stored injection).
    display_name = html.escape(user_name.split()[0]) if user_name else "parceiro(a)"
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Bem-vindo ao Braga Saúde!</title>
</head>
<body style="font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Roboto,sans-serif; background:#F0FDF4; margin:0; padding:24px;">
  <div style="max-width:520px; margin:0 auto; background:#FFF; border-radius:16px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,0.08);">
    <div style="background:linear-gradient(135deg,#00A884 0%,#008f70 100%); padding:40px 24px; text-align:center; color:#FFF;">
      <div style="font-size:48px; margin-bottom:12px;">💚</div>
      <h1 style="margin:0; font-size:26px; font-weight:700;">Bem-vindo(a), """ + display_name + """!</h1>
      <p style="margin:8px 0 0 0; font-size:14px; opacity:0.9;">Sua jornada de autocuidado começa agora</p>
    </div>
    <div style="padding:32px 28px;">
      <p style="font-size:16px; line-height:1.7; color:#334155; margin-bottom:24px;">
        É com muito carinho que damos as boas-vindas ao <strong>Braga Saúde</strong>! 🎉<br><br>
        Agora você faz parte de uma comunidade que cuida de quem sempre cuidou de você.
      </p>

      <div style="background:#F0FDF4; border-radius:12px; padding:20px; margin:20px 0;">
        <p style="font-size:15px; font-weight:600; color:#00A884; margin:0 0 12px 0;">📋 O que você pode fazer agora:</p>
        <div style="font-size:14px; color:#334155; line-height:2;">
          ✅ <strong>Preencher seu perfil</strong> — dados básicos para personalizar sua experiência<br>
          ✅ <strong>Registrar sinais vitais</strong> — pressão, glicose, peso e mais<br>
          ✅ <strong>Configurar lembretes</strong> — hidratação e medicações<br>
          ✅ <strong>Conectar com a família</strong> — compartilhe sua saúde com quem você ama
        </div>
      </div>

      <div style="background:#F8FAFC; border-radius:8px; padding:14px 16px; font-size:14px; color:#64748B; line-height:1.6; border-left:4px solid #00A884; margin-top:24px;">
        💡 <strong>Dica:</strong> Seus dados ficam sempre seguros e sincronizados. Mesmo que você troque de celular, basta fazer login com a mesma conta para recuperar tudo.
      </div>
    </div>
    <div style="background:#F1F5F9; padding:20px; text-align:center; font-size:12px; color:#64748B; border-top:1px solid #E2E8F0;">
      Braga Saúde • Cuidando de quem sempre cuidou de você 💚
    </div>
  </div>
</body>
</html>"""


# ==================== SERVIÇO DE ENVIO ====================

def _send_gmail_sync(to_email: str, subject: str, html_content: str, text_content: str) -> bool:
    """Envia e-mail por SMTP seguro do Gmail de forma síncrona."""
    if not is_email_configured():
        print("[Email Service Error] Gmail SMTP não configurado. Defina GMAIL_SMTP_USERNAME, GMAIL_SMTP_APP_PASSWORD e GMAIL_FROM_EMAIL.")
        return False

    try:
        message = EmailMessage()
        message["From"] = f"{GMAIL_FROM_NAME} <{GMAIL_FROM_EMAIL}>"
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(text_content)
        message.add_alternative(html_content, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, context=context, timeout=20) as smtp:
            smtp.login(GMAIL_SMTP_USERNAME, GMAIL_SMTP_APP_PASSWORD)
            smtp.send_message(message)
        print(f"[Email Service] E-mail aceito pelo Gmail SMTP para {to_email}")
        return True
    except Exception as e:
        print(f"[Email Service Error] Falha ao enviar e-mail pelo Gmail SMTP para {to_email}: {e}")
        return False


async def send_otp_email(to_email: str, code: str) -> Dict[str, Any]:
    """Envia e-mail de verificação de cadastro (OTP)."""
    # AUD-G(M): o OTP ia no ASSUNTO — clientes de e-mail mostram o assunto na
    # tela de bloqueio, na notificação do app e na prévia da caixa; o código
    # ficava legível sem abrir a mensagem (e assuntos são indexados/pesquisados).
    # Agora o código só existe no CORPO.
    subject = "Seu código de verificação - Braga Saúde"
    plain_text = f"Seu código de verificação do Braga Saúde é: {code}. Válido por 15 minutos."
    html = _otp_html(code)

    success = await asyncio.to_thread(_send_gmail_sync, to_email, subject, html, plain_text)
    return {"success": success, "channel": "EMAIL", "to": to_email, "purpose": "EMAIL_VERIFICATION"}


async def send_password_reset_email(to_email: str, code: str) -> Dict[str, Any]:
    """Envia e-mail de recuperação de senha (OTP)."""
    # AUD-G(M): idem — OTP fora do assunto, só no corpo.
    subject = "Recupere sua senha - Braga Saúde"
    plain_text = f"Seu código de recuperação de senha do Braga Saúde é: {code}. Válido por 15 minutos."
    html = _reset_html(code)

    success = await asyncio.to_thread(_send_gmail_sync, to_email, subject, html, plain_text)
    return {"success": success, "channel": "EMAIL", "to": to_email, "purpose": "PASSWORD_RESET"}


async def send_welcome_email(to_email: str, user_name: str = "") -> Dict[str, Any]:
    """Envia e-mail de boas-vindas para novo usuário."""
    subject = "Bem-vindo ao Braga Saúde! 💚"
    plain_text = f"Olá{', ' + user_name if user_name else ''}! Seja bem-vindo ao Braga Saúde. Sua jornada de autocuidado começa agora."
    html = _welcome_html(user_name)

    success = await asyncio.to_thread(_send_gmail_sync, to_email, subject, html, plain_text)
    return {"success": success, "channel": "EMAIL", "to": to_email, "purpose": "WELCOME"}


async def send_security_alert_email(
    to_email: str, title: str, detail: str, user_name: str = ""
) -> Dict[str, Any]:
    """Alerta de segurança ao próprio usuário (doc 10 §4.5).

    Usado após uma troca de senha bem-sucedida: se não foi o dono da conta, ele
    descobre pelo e-mail. Sem emojis no título (manual de UX).
    """
    subject = f"Aviso de segurança: {title} - Braga Saúde"
    plain_text = f"Olá{', ' + user_name if user_name else ''}! {title}. {detail}"
    html = _security_alert_html(title, detail, user_name)

    success = await asyncio.to_thread(_send_gmail_sync, to_email, subject, html, plain_text)
    return {"success": success, "channel": "EMAIL", "to": to_email, "purpose": "SECURITY_ALERT"}


def _security_alert_html(title: str, detail: str, user_name: str) -> str:
    """Template de alerta de segurança — mesmo visual da marca, sem emojis."""
    # AUD-G05: display_name vem do Firebase Auth; title/detail também são escapados
    # (hoje são constantes do servidor, mas a defesa é por camadas).
    display_name = html.escape(user_name.split()[0]) if user_name else "parceiro(a)"
    title = html.escape(title)
    detail = html.escape(detail)
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aviso de segurança - Braga Saúde</title>
</head>
<body style="font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Roboto,sans-serif; background:#F8FAFC; margin:0; padding:24px;">
  <div style="max-width:520px; margin:0 auto; background:#FFF; border-radius:16px; overflow:hidden; box-shadow:0 4px 20px rgba(0,0,0,0.08);">
    <div style="background:linear-gradient(135deg,#0F766E 0%,#0d5c56 100%); padding:40px 24px; text-align:center; color:#FFF;">
      <h1 style="margin:0; font-size:24px; font-weight:700;">Aviso de segurança</h1>
      <p style="margin:8px 0 0 0; font-size:14px; opacity:0.9;">Olá, """ + display_name + """</p>
    </div>
    <div style="padding:32px 28px;">
      <p style="font-size:16px; line-height:1.7; color:#334155; margin-bottom:16px;">
        <strong>""" + title + """</strong>
      </p>
      <p style="font-size:15px; line-height:1.7; color:#475569; margin-bottom:24px;">
        """ + detail + """
      </p>
      <div style="background:#FEF2F2; border-radius:8px; padding:14px 16px; font-size:14px; color:#7F1D1D; line-height:1.6; border-left:4px solid #DC2626;">
        <strong>Não foi você?</strong> Entre em contato imediatamente com nosso suporte e altere sua senha novamente.
      </div>
    </div>
    <div style="background:#F1F5F9; padding:20px; text-align:center; font-size:12px; color:#64748B; border-top:1px solid #E2E8F0;">
      Esta é uma mensagem automática do Braga Saúde. Não responda a este e-mail.
    </div>
  </div>
</body>
</html>"""


# ==================== INTERFACE LEGADA (compatibilidade com server.py) ====================

async def send_security_code_email(to_email: str, code: str, purpose: str = "EMAIL_VERIFICATION") -> Dict[str, Any]:
    """Interface legada — redireciona para o método correto baseado no purpose."""
    if purpose == "PASSWORD_RESET":
        return await send_password_reset_email(to_email, code)
    return await send_otp_email(to_email, code)
