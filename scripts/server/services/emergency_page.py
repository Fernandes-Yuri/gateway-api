"""Página pública estática de socorro, sem dados pessoais nem ligação automática."""
from html import escape
from services.emergency_reply import SAMU_URL, UPA_URL


def emergency_page():
    return f'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opções de socorro - Braga Saúde</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f2f7f5;color:#173b33;font:20px/1.5 system-ui,sans-serif}}
main{{max-width:560px;margin:0 auto;padding:40px 24px}}.brand{{font-size:18px;font-weight:700}}
h1{{font-size:32px;line-height:1.2;margin:28px 0 16px}}p{{margin:16px 0}}
.actions{{display:grid;gap:16px;margin:28px 0}}
a{{display:block;padding:20px 16px;border-radius:12px;text-align:center;font-weight:700;text-decoration:none;color:white;background:#17634f}}
a.call{{background:#ac291d}}a:focus-visible{{outline:4px solid #142e28;outline-offset:4px}}
.help{{font-size:17px;color:#3b544b}}
</style></head><body><main>
<p class="brand">Braga Saúde</p><h1>Precisa de socorro?</h1>
<p><strong>Ligue para o SAMU 192 agora.</strong> Não espere uma resposta nesta conversa para pedir ajuda.</p>
<div class="actions">
<a class="call" href="{escape(SAMU_URL, quote=True)}">Ligar SAMU 192</a>
<a href="{escape(UPA_URL, quote=True)}" rel="noreferrer">Buscar UPA no Maps</a>
</div>
<p class="help">No celular, o primeiro botão abre o discador. Você confirma a ligação no aparelho. Se não abrir, disque 192.</p>
<p class="help">No computador, use um telefone para ligar 192. O Braga não faz ligações nem aciona o SAMU por você.</p>
</main></body></html>'''
