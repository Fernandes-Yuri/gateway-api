"""Aceite e exclusão exclusivos da captação; sem vínculo ou dados do aplicativo."""
import hashlib
import re
import unicodedata
from datetime import datetime, timezone

TERMS_VERSION = '2026-10-23h59'
PRIVACY_VERSION = '2026-10-23h59'
CONSENT_TEXT = (
    'Antes de iniciarmos seu atendimento, leia nossos Termos de Uso '
    '(https://bragasaude.online/termos.html) e a Política de Privacidade '
    '(https://bragasaude.online/privacidade.html).\n\n'
    'O histórico ativo e seus anexos ficam disponíveis por até 23h59 desde a abertura, '
    'sem prorrogação por novas mensagens. Depois, o acesso é bloqueado e o histórico entra em expurgo automático. '
    'Backups seguem ciclo próprio e as mensagens no seu WhatsApp não são apagadas por esse expurgo.\n\n'
    'Para prosseguir com o atendimento, confirme seu aceite tocando no botão abaixo:')


def phone_hash(phone):
    if not isinstance(phone,str) or not re.fullmatch(r'[0-9]{8,16}',phone):
        raise ValueError('Telefone inválido')
    return hashlib.sha256(('braga-whatsapp-intake:'+phone).encode()).hexdigest()


def norm(text):
    return ''.join(c for c in unicodedata.normalize('NFD',(text or '').casefold()) if not unicodedata.combining(c)).strip()


def deletion_request(text):
    return bool(re.search(r'^(?:quero |por favor[, ]*)?(?:apagar|apague|excluir|exclua|eliminar|elimine|esquecer|esqueca)\b',norm(text)))


async def consent_gate(pool,phone,*,text='',button_id='',timestamp=None,accept_message=None):
    if pool is None:
        return True,'Não consegui verificar a autorização agora. Tente novamente em instantes.',[]
    key=phone_hash(phone)
    async with pool.acquire() as conn,conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))','intake-consent:'+key)
        row=await conn.fetchrow('SELECT * FROM whatsapp_intake_consents WHERE phone_hash=$1 FOR UPDATE',key)
        if row and row['terms_version']==TERMS_VERSION and row['privacy_version']==PRIVACY_VERSION and row['accepted_at']:
            if timestamp and float(timestamp)<int(row['accepted_at'].timestamp()):
                return True,'',[]
            return False,'',[]
        pending=bool(row and row['expires_at']>datetime.now(timezone.utc) and row['terms_version']==TERMS_VERSION and row['privacy_version']==PRIVACY_VERSION)
        accept=norm(text) in ('li e aceito','aceito os termos e a politica de privacidade') or button_id == 'wa_accept'
        if pending and accept and (not timestamp or float(timestamp)>=int(row['presented_at'].timestamp())):
            await conn.execute('UPDATE whatsapp_intake_consents SET accepted_at=NOW() WHERE phone_hash=$1',key)
            return True,accept_message or 'Aceite registrado. Como posso ajudar no atendimento?',[]
        if norm(text) in ('nao aceito','nao concordo') or button_id == 'wa_decline':
            await conn.execute('DELETE FROM whatsapp_intake_consents WHERE phone_hash=$1',key)
            return True,'Tudo bem. Não farei a coleta sem seu aceite. A orientação de socorro continua disponível.',[]
        if not pending:
            await conn.execute('INSERT INTO whatsapp_intake_consents(phone_hash,terms_version,privacy_version) VALUES($1,$2,$3) '
                'ON CONFLICT(phone_hash) DO UPDATE SET terms_version=$2,privacy_version=$3,accepted_at=NULL,presented_at=NOW(),expires_at=NOW()+INTERVAL \'10 minutes\'',key,TERMS_VERSION,PRIVACY_VERSION)
        return True,CONSENT_TEXT,[{'id':'wa_accept','title':'Li e aceito'},{'id':'wa_decline','title':'Não aceito'}]


async def prepare_deletion(pool,phone):
    async with pool.acquire() as conn:
        await conn.execute('INSERT INTO whatsapp_intake_deletions(phone_hash) VALUES($1) ON CONFLICT(phone_hash) DO UPDATE SET expires_at=NOW()+INTERVAL \'5 minutes\'',phone_hash(phone))
    return ('Quer apagar os dados deste atendimento no WhatsApp? Responda SIM ou NÃO.',
            [{'id':'del_confirm','title':'Sim, apagar'},{'id':'del_cancel','title':'Não'}])


async def resolve_deletion(pool,phone,*,text='',button_id='',timestamp=None):
    from services.wa_intake_store import event
    key=phone_hash(phone)
    async with pool.acquire() as conn,conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))','intake-consent:'+key)
        pending=await conn.fetchrow('SELECT * FROM whatsapp_intake_deletions WHERE phone_hash=$1 AND expires_at>NOW() FOR UPDATE',key)
        if not pending:
            return None
        if timestamp and float(timestamp)<int(pending['expires_at'].timestamp()-300):
            return None
        if norm(text) in ('nao','cancelar') or button_id == 'del_cancel':
            await conn.execute('DELETE FROM whatsapp_intake_deletions WHERE phone_hash=$1',key)
            return 'Tudo bem, não apaguei seus dados.'
        if norm(text)!='sim' and button_id != 'del_confirm':
            return None
        rows=await conn.fetch('SELECT id FROM whatsapp_intake_conversations WHERE phone_hash=$1 ORDER BY id FOR UPDATE',key)
        for row in rows:
            await conn.execute('DELETE FROM whatsapp_intake_events WHERE conversation_id=$1',row['id'])
            await conn.execute('DELETE FROM whatsapp_intake_conversations WHERE id=$1',row['id'])
        await conn.execute('DELETE FROM whatsapp_intake_consents WHERE phone_hash=$1',key)
        await conn.execute('DELETE FROM whatsapp_intake_deletions WHERE phone_hash=$1',key)
        for row in rows:
            await event(conn,row['id'],'conversation.deleted')
        return {'deleted':True}
