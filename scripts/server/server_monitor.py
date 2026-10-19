#!/usr/bin/env python3
"""Monitoramento diário do servidor Braga Saúde.

Verifica:
- Serviço braga-gateway (FastAPI gateway)
- PostgreSQL
- Disco e memória
- Uptime do sistema

Envia relatório HTML por e-mail para os destinatários configurados.
"""

import os
import sys
import subprocess
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from typing import Dict, List, Tuple

# ==================== CONFIGURAÇÕES ====================

RECIPIENTS = [
    "workfjsyuri@gmail.com",
    "welisacristina@gmail.com",
]

# Ler variáveis de ambiente do arquivo de configuração
ENV_FILE = "/etc/braga-saude/gateway.env"
env_vars = {}
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env_vars[key] = value

GMAIL_SMTP_HOST = env_vars.get("GMAIL_SMTP_HOST", "smtp.gmail.com")
GMAIL_SMTP_PORT = int(env_vars.get("GMAIL_SMTP_PORT", "465"))
GMAIL_SMTP_USERNAME = env_vars.get("GMAIL_SMTP_USERNAME", "")
GMAIL_SMTP_APP_PASSWORD = env_vars.get("GMAIL_SMTP_APP_PASSWORD", "")
GMAIL_FROM_EMAIL = env_vars.get("GMAIL_FROM_EMAIL", GMAIL_SMTP_USERNAME)
GMAIL_FROM_NAME = env_vars.get("GMAIL_FROM_NAME", "Braga Saúde")

# ==================== UTILITÁRIOS ====================

def run_cmd(cmd: str, timeout: int = 10) -> Tuple[str, bool]:
    """Executa comando e retorna (saída, sucesso)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip(), result.returncode == 0
    except subprocess.TimeoutExpired:
        return "TIMEOUT", False
    except Exception as e:
        return str(e), False


def check_service(name: str) -> Dict:
    """Verifica status de um serviço systemd."""
    output, ok = run_cmd(f"systemctl is-active {name}")
    enabled_out, _ = run_cmd(f"systemctl is-enabled {name}")
    return {
        "name": name,
        "status": output,
        "enabled": enabled_out == "enabled",
        "ok": output == "active",
    }


def check_postgres() -> Dict:
    """Verifica se o PostgreSQL está respondendo."""
    output, ok = run_cmd("docker exec bragasaude-postgres pg_isready -U bragasaude_user -d bragasaude")
    return {
        "name": "PostgreSQL",
        "status": "ready" if ok else output,
        "ok": ok,
    }


def check_disk() -> Dict:
    """Verifica uso de disco."""
    output, ok = run_cmd("df -h / | tail -1 | awk '{print $5}'")
    usage = output.replace("%", "").strip() if ok else "?"
    try:
        usage_ok = ok and usage != "?" and int(usage) < 90
    except ValueError:
        usage_ok = False
    return {
        "name": "Disco",
        "usage_percent": usage,
        "ok": usage_ok,
    }


def check_memory() -> Dict:
    """Verifica uso de memória."""
    output, ok = run_cmd("free -m | grep Mem | awk '{printf \"%.0f\", $3/$2 * 100}'")
    usage = output.strip() if ok else "?"
    try:
        usage_ok = ok and usage != "?" and int(usage) < 90
    except ValueError:
        usage_ok = False
    return {
        "name": "Memória",
        "usage_percent": usage,
        "ok": usage_ok,
    }


def check_uptime() -> str:
    """Retorna uptime do sistema."""
    output, _ = run_cmd("uptime -p")
    # Remover caracteres não-ASCII (como non-breaking space)
    return output.encode('ascii', 'ignore').decode('ascii')


def check_temperature() -> Dict:
    """Verifica temperatura da CPU."""
    # Tentar diferentes métodos
    output, ok = run_cmd("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null")
    if ok and output.isdigit():
        temp_c = int(output) / 1000
        return {
            "name": "Temperatura CPU",
            "value": f"{temp_c:.1f}°C",
            "ok": temp_c < 80,
            "warning": temp_c >= 70,
        }
    
    # Tentar sensors (lm-sensors)
    output, ok = run_cmd("sensors 2>/dev/null | grep 'Core 0' | awk '{print $3}' | tr -d '+°C'")
    if ok and output:
        try:
            temp_c = float(output)
            return {
                "name": "Temperatura CPU",
                "value": f"{temp_c:.1f}°C",
                "ok": temp_c < 80,
                "warning": temp_c >= 70,
            }
        except ValueError:
            pass
    
    return {
        "name": "Temperatura CPU",
        "value": "N/A",
        "ok": True,
        "warning": False,
    }


def check_cpu() -> Dict:
    """Verifica uso de CPU."""
    output, ok = run_cmd("top -bn1 | grep 'Cpu(s)' | awk '{print $2}'")
    if ok and output:
        try:
            usage = float(output)
            return {
                "name": "CPU",
                "usage_percent": f"{usage:.1f}",
                "ok": usage < 90,
            }
        except ValueError:
            pass
    return {
        "name": "CPU",
        "usage_percent": "?",
        "ok": True,
    }


def check_load_average() -> Dict:
    """Verifica load average do sistema."""
    output, ok = run_cmd("uptime | awk -F'load average:' '{print $2}'")
    if ok and output:
        parts = [p.strip() for p in output.split(",")]
        if len(parts) >= 3:
            return {
                "name": "Load Average",
                "1min": parts[0],
                "5min": parts[1],
                "15min": parts[2],
                "ok": True,
            }
    return {
        "name": "Load Average",
        "1min": "?",
        "5min": "?",
        "15min": "?",
        "ok": True,
    }


def check_network() -> Dict:
    """Verifica conectividade de rede."""
    output, ok = run_cmd("ping -c 1 -W 3 8.8.8.8 > /dev/null 2>&1 && echo OK || echo FAIL")
    dns_ok = run_cmd("nslookup google.com > /dev/null 2>&1 && echo OK || echo FAIL")[0]
    
    return {
        "name": "Rede",
        "internet": "OK" if "OK" in output else "FALHA",
        "dns": "OK" if "OK" in dns_ok else "FALHA",
        "ok": "OK" in output and "OK" in dns_ok,
    }


def check_docker() -> Dict:
    """Verifica status dos containers Docker."""
    output, ok = run_cmd("docker ps --format '{{.Names}}: {{.Status}}' 2>/dev/null")
    containers = []
    if ok and output:
        for line in output.strip().split("\n"):
            if line:
                containers.append(line)
    
    return {
        "name": "Docker",
        "containers": containers,
        "ok": len(containers) > 0,
    }


def check_top_processes() -> str:
    """Retorna top 5 processos por uso de memória."""
    output, _ = run_cmd("ps aux --sort=-%mem | head -6 | tail -5 | awk '{printf \"%-6s %-5s %-5s %s\\n\", $1, $3, $4, $11}'")
    return output if output else "N/A"


def check_swap() -> Dict:
    """Verifica uso de swap."""
    output, ok = run_cmd("free -m | grep Swap | awk '{if ($2 > 0) printf \"%.0f\", $3/$2 * 100; else print \"0\"}'")
    usage = output.strip() if ok else "?"
    try:
        usage_ok = usage == "?" or int(usage) < 50
    except ValueError:
        usage_ok = True
    return {
        "name": "Swap",
        "usage_percent": usage,
        "ok": usage_ok,
    }


def check_inodes() -> Dict:
    """Verifica uso de inodes."""
    output, ok = run_cmd("df -i / | tail -1 | awk '{print $5}'")
    usage = output.replace("%", "").strip() if ok else "?"
    try:
        usage_ok = usage == "?" or int(usage) < 80
    except ValueError:
        usage_ok = True
    return {
        "name": "Inodes",
        "usage_percent": usage,
        "ok": usage_ok,
    }


def check_local_gateway() -> Dict:
    """Verifica se o gateway responde localmente."""
    output, ok = run_cmd("curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8080/health")
    return {
        "name": "Gateway Local",
        "status": f"HTTP {output}" if ok else "não responde",
        "ok": ok and output == "200",
    }


def get_recent_logs(service: str, lines: int = 20) -> str:
    """Pega logs recentes de um serviço."""
    output, _ = run_cmd(f"journalctl -u {service} --since '24 hours ago' --no-pager -n {lines} --output=short-iso 2>/dev/null")
    return output[:1500] if output else "Sem logs disponíveis"


# ==================== GERAÇÃO DE RELATÓRIO ====================

def generate_report(checks: List[Dict], top_procs: str = "") -> Tuple[str, str, bool]:
    """Gera relatório HTML e texto plano. Retorna (html, plain, all_ok)."""
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    uptime = check_uptime()

    all_ok = all(c.get("ok", False) for c in checks)

    # Status icon
    def icon(ok):
        return "✅" if ok else "❌"

    def status_badge(ok):
        color = "#10B981" if ok else "#EF4444"
        text = "OK" if ok else "FALHA"
        return f'<span style="color:{color};font-weight:700">{text}</span>'

    # Extrair dados dos checks
    disk = next((c for c in checks if c.get("name") == "Disco"), None)
    mem = next((c for c in checks if c.get("name") == "Memória"), None)
    swap = next((c for c in checks if c.get("name") == "Swap"), None)
    inodes = next((c for c in checks if c.get("name") == "Inodes"), None)
    cpu = next((c for c in checks if c.get("name") == "CPU"), None)
    temp = next((c for c in checks if c.get("name") == "Temperatura CPU"), None)
    load = next((c for c in checks if c.get("name") == "Load Average"), None)
    net = next((c for c in checks if c.get("name") == "Rede"), None)
    docker = next((c for c in checks if c.get("name") == "Docker"), None)

    # Build HTML
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',-apple-system,sans-serif;background:#F0F4F8;margin:0;padding:24px;">
<div style="max-width:640px;margin:0 auto;background:#FFF;border-radius:16px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08);">

  <div style="background:linear-gradient(135deg,#1E3A5F 0%,#2563EB 100%);padding:32px 24px;text-align:center;color:#FFF;">
    <h1 style="margin:0;font-size:24px;">Braga Saude - Relatorio de Monitoramento</h1>
    <p style="margin:8px 0 0;font-size:14px;opacity:0.9;">{now}</p>
  </div>

  <div style="padding:24px;">
    <div style="background:{'#F0FDF4' if all_ok else '#FEF2F2'};border-radius:12px;padding:16px;margin-bottom:20px;text-align:center;">
      <span style="font-size:20px;">{'🟢' if all_ok else '🔴'}</span>
      <span style="font-size:16px;font-weight:700;color:{'#166534' if all_ok else '#991B1B'};">
        {'TODOS OS SERVICOS OPERACIONAIS' if all_ok else 'ALGUNS SERVICOS COM PROBLEMA'}
      </span>
    </div>

    <h2 style="font-size:16px;color:#1E3A5F;border-bottom:2px solid #E5E7EB;padding-bottom:8px;">Status dos Servicos</h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
"""

    for check in checks:
        name = check.get("name", check.get("service", "?"))
        ok = check.get("ok", False)
        status = check.get("status", "?")
        html += f"""      <tr style="border-bottom:1px solid #F1F5F9;">
        <td style="padding:10px 8px;font-weight:600;">{icon(ok)} {name}</td>
        <td style="padding:10px 8px;text-align:right;">{status_badge(ok)}</td>
      </tr>
"""

    # Túneis com URLs
    tunnel_info = [c for c in checks if "url" in c]
    if tunnel_info:
        html += """    </table>
    <h2 style="font-size:16px;color:#1E3A5F;border-bottom:2px solid #E5E7EB;padding-bottom:8px;margin-top:20px;">Tuneis Cloudflare</h2>
"""
        for t in tunnel_info:
            url = t.get("url", "N/A")
            html += f"""    <div style="background:#F8FAFC;border-radius:8px;padding:12px;margin-bottom:10px;font-size:13px;">
      <strong>{t.get('name', '?')}</strong><br>
      <span style="color:#64748B;word-break:break-all;">{url}</span>
    </div>
"""

    # Recursos do sistema
    html += """    <h2 style="font-size:16px;color:#1E3A5F;border-bottom:2px solid #E5E7EB;padding-bottom:8px;margin-top:20px;">Recursos do Sistema</h2>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
"""
    if disk:
        html += f"""      <tr style="border-bottom:1px solid #F1F5F9;">
        <td style="padding:10px 8px;">Disco</td>
        <td style="padding:10px 8px;text-align:right;">{disk['usage_percent']}% usado</td>
      </tr>
"""
    if mem:
        html += f"""      <tr style="border-bottom:1px solid #F1F5F9;">
        <td style="padding:10px 8px;">Memoria</td>
        <td style="padding:10px 8px;text-align:right;">{mem['usage_percent']}% usado</td>
      </tr>
"""
    if swap:
        html += f"""      <tr style="border-bottom:1px solid #F1F5F9;">
        <td style="padding:10px 8px;">Swap</td>
        <td style="padding:10px 8px;text-align:right;">{swap['usage_percent']}% usado</td>
      </tr>
"""
    if cpu:
        html += f"""      <tr style="border-bottom:1px solid #F1F5F9;">
        <td style="padding:10px 8px;">CPU</td>
        <td style="padding:10px 8px;text-align:right;">{cpu['usage_percent']}% uso</td>
      </tr>
"""
    if temp:
        temp_color = "#EF4444" if temp.get("warning") else ("#F59E0B" if not temp.get("ok") else "#10B981")
        html += f"""      <tr style="border-bottom:1px solid #F1F5F9;">
        <td style="padding:10px 8px;">Temperatura</td>
        <td style="padding:10px 8px;text-align:right;color:{temp_color};font-weight:700;">{temp['value']}</td>
      </tr>
"""
    if load:
        html += f"""      <tr style="border-bottom:1px solid #F1F5F9;">
        <td style="padding:10px 8px;">Load Average</td>
        <td style="padding:10px 8px;text-align:right;">{load['1min']} / {load['5min']} / {load['15min']}</td>
      </tr>
"""
    if inodes:
        html += f"""      <tr style="border-bottom:1px solid #F1F5F9;">
        <td style="padding:10px 8px;">Inodes</td>
        <td style="padding:10px 8px;text-align:right;">{inodes['usage_percent']}% usado</td>
      </tr>
"""

    html += f"""    </table>

    <div style="background:#F8FAFC;border-radius:8px;padding:12px;margin-top:20px;font-size:13px;color:#64748B;">
      <strong>Uptime:</strong> {uptime}
    </div>
"""

    # Rede
    if net:
        net_color = "#10B981" if net.get("ok") else "#EF4444"
        html += f"""    <div style="background:#F8FAFC;border-radius:8px;padding:12px;margin-top:10px;font-size:13px;">
      <strong>Rede:</strong> <span style="color:{net_color};">Internet {net['internet']} | DNS {net['dns']}</span>
    </div>
"""

    # Docker
    if docker and docker.get("containers"):
        html += """    <h2 style="font-size:16px;color:#1E3A5F;border-bottom:2px solid #E5E7EB;padding-bottom:8px;margin-top:20px;">Docker Containers</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
"""
        for container in docker["containers"]:
            html += f"""      <tr style="border-bottom:1px solid #F1F5F9;">
        <td style="padding:8px;">{container}</td>
      </tr>
"""
        html += "    </table>"

    # Top Processos
    if top_procs and top_procs != "N/A":
        html += """    <h2 style="font-size:16px;color:#1E3A5F;border-bottom:2px solid #E5E7EB;padding-bottom:8px;margin-top:20px;">Top Processos (Memoria)</h2>
    <pre style="background:#F8FAFC;border-radius:8px;padding:12px;font-size:11px;overflow-x:auto;margin:0;">"""
        html += top_procs
        html += "</pre>"

    html += """  </div>

  <div style="background:#F1F5F9;padding:16px;text-align:center;font-size:12px;color:#64748B;border-top:1px solid #E2E8F0;">
    Braga Saude - Monitoramento Automatizado
  </div>

</div>
</body>
</html>"""

    # Texto plano
    plain_lines = [
        "RELATORIO DE MONITORAMENTO - BRAGA SAUDE",
        f"Data: {now}",
        f"Uptime: {uptime}",
        "",
        "STATUS DOS SERVICOS:",
    ]
    for c in checks:
        name = c.get("name", "?")
        ok = c.get("ok", False)
        status = c.get("status", "?")
        plain_lines.append(f"  {'OK' if ok else 'FALHA'} - {name}: {status}")

    tunnel_info = [c for c in checks if "url" in c]
    if tunnel_info:
        plain_lines.append("")
        plain_lines.append("TUNEIS CLOUDFLARE:")
        for t in tunnel_info:
            plain_lines.append(f"  {t.get('name')}: {t.get('url', 'N/A')}")

    plain_lines.append("")
    plain_lines.append(f"Resumo: {'TODOS OK' if all_ok else 'ATENCAO: Alguns servicos com problema!'}")

    return html, "\n".join(plain_lines), all_ok


# ==================== ENVIO DE E-MAIL ====================

def send_email(to_emails: List[str], subject: str, html: str, plain: str) -> bool:
    """Envia e-mail para múltiplos destinatários."""
    if not GMAIL_SMTP_USERNAME or not GMAIL_SMTP_APP_PASSWORD:
        print("[Monitor Error] Gmail SMTP não configurado")
        return False

    # Apenas sanitizar a senha (que pode ter caracteres estranhos do env file)
    clean_password = ''.join(c for c in GMAIL_SMTP_APP_PASSWORD if ord(c) < 128)

    try:
        msg = EmailMessage()
        msg["From"] = f"{GMAIL_FROM_NAME} <{GMAIL_FROM_EMAIL}>"
        msg["To"] = ", ".join(to_emails)
        msg["Subject"] = subject
        msg.set_content(plain, charset='utf-8')
        msg.add_alternative(html, subtype="html", charset='utf-8')

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, context=context, timeout=30) as smtp:
            smtp.login(GMAIL_SMTP_USERNAME, clean_password)
            smtp.send_message(msg)

        print(f"[Monitor] E-mail enviado para: {', '.join(to_emails)}")
        return True
    except Exception as e:
        print(f"[Monitor Error] Falha ao enviar e-mail: {e}")
        return False


# ==================== MAIN ====================

def main():
    print(f"[Monitor] Iniciando monitoramento - {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")

    # Executar todas as verificações
    checks = []

    # Serviços
    checks.append({"name": "Serviço Braga Gateway", **check_service("braga-gateway")})
    checks.append({"name": "Gateway Local (8080)", **check_local_gateway()})

    # Túneis

    # Banco de dados
    checks.append(check_postgres())

    # APIs externas

    # Recursos do sistema
    checks.append(check_disk())
    checks.append(check_memory())
    checks.append(check_swap())
    checks.append(check_inodes())
    checks.append(check_cpu())
    checks.append(check_temperature())
    checks.append(check_load_average())
    checks.append(check_network())
    checks.append(check_docker())

    # Processos top
    top_procs = check_top_processes()

    # Gerar relatório
    html, plain, all_ok = generate_report(checks, top_procs)

    # Definir assunto
    status_emoji = "OK" if all_ok else "FALHA"
    subject = f"[{status_emoji}] Relatório Diário Braga Saúde - {datetime.now().strftime('%d/%m/%Y')}"

    # Enviar e-mail
    send_email(RECIPIENTS, subject, html, plain)

    # Salvar relatório localmente (backup)
    # AUD-G(B): hardcoded /home/yurifjs/... — na EC2 o makedirs criava uma
    # árvore inexistente (ou falhava) e poluía o log. Agora env-configurável,
    # fallback relativo ao script.
    report_dir = os.environ.get("MONITOR_REPORT_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "reports"
    )
    os.makedirs(report_dir, exist_ok=True)
    report_file = os.path.join(report_dir, f"report_{datetime.now().strftime('%Y%m%d')}.html")
    with open(report_file, "w") as f:
        f.write(html)
    print(f"[Monitor] Relatório salvo em: {report_file}")

    # Resumo no console
    print(f"\n{'='*50}")
    print(f"RESUMO: {'TODOS OK' if all_ok else 'ALGUNS SERVIÇOS COM PROBLEMA'}")
    print(f"{'='*50}")
    for c in checks:
        status = "✅" if c.get("ok") else "❌"
        print(f"  {status} {c.get('name', '?')}: {c.get('status', '?')}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
