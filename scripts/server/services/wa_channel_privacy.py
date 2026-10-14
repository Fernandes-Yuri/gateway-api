"""Aceite versionado e exclusão do canal WhatsApp, preservando o aplicativo."""
import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone

from services.whatsapp_identity import normalize_phone_digits

TERMS_URL = 'https://bragasaude.online/termos.html'
PRIVACY_URL = 'https://bragasaude.online/privacidade.html'
TERMS_VERSION = '2026-09'
PRIVACY_VERSION = '2026-09'
CONSENT_TEXT = (
    'Antes de usar o assistente WhatsApp, leia os documentos do Braga Saúde:\n\n'
    f'Termos de Uso: {TERMS_URL}\nPolítica de Privacidade: {PRIVACY_URL}\n\n'
    'Você leu e aceita os documentos para usar este canal? '
    'Toque em “Li e aceito” ou responda “Li e aceito”. Sem aceite, não consulto nem registro seus dados aqui. '
    'A orientação de socorro continua disponível.')
DELETE_TEXT = (
    'Quer apagar os dados ativos deste canal e desvincular seu número do Braga Saúde? '
    'Sua conta, configurações e histórico de saúde no aplicativo serão preservados. '
    'As mensagens no seu WhatsApp precisam ser apagadas por você. Responda SIM ou NÃO.')
DELETED_TEXT = (
    'Apaguei os dados ativos deste canal e desvinculei seu número. '
    'Sua conta e histórico no aplicativo foram preservados. '
    'Sugiro que você também apague esta conversa no seu WhatsApp. '
    'Para voltar a usar o assistente, será necessário aceitar os documentos novamente e vincular pelo app.')
UNAVAILABLE_TEXT = 'Não consegui verificar a autorização deste canal agora. Tente novamente em instantes.'


def phone_hash(phone):
    digits = normalize_phone_digits(phone)
    if not digits:
        raise ValueError('Número ausente')
    return hashlib.sha256(('braga-whatsapp-channel:' + digits).encode()).hexdigest()


def norm(text):
    return ''.join(c for c in unicodedata.normalize('NFD', (text or '').casefold())
                   if not unicodedata.combining(c)).strip()


def deletion_request(text):
    text = norm(text)
    return bool(re.search(r'^(?:quero |por favor[, ]*)?(?:apagar|apague|excluir|exclua|eliminar|elimine|esquecer|esqueca|esquece|desvincular|desvincule)\b', text))


def _valid_consent(row):
    return bool(row and row['accepted_at'] and row['terms_version'] == TERMS_VERSION
                and row['privacy_version'] == PRIVACY_VERSION)


async def consent_gate(pool, phone, *, text='', button_id='', timestamp=None):
    """Retorna (bloqueado, mensagem, botões). Nunca aceita um SIM solto."""
    if pool is None:
        return True, UNAVAILABLE_TEXT, []
    key = phone_hash(phone)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))', key)
        row = await conn.fetchrow('SELECT * FROM whatsapp_channel_consents WHERE phone_hash=$1 FOR UPDATE', key)
        if _valid_consent(row):
            # Reentregas anteriores ao aceite não voltam a executar operações.
            if timestamp and float(timestamp) < int(row['accepted_at'].timestamp()):
                return True, '', []
            return False, '', []
        now = datetime.now(timezone.utc)
        pending = bool(row and row['expires_at'] > now and row['terms_version'] == TERMS_VERSION
                       and row['privacy_version'] == PRIVACY_VERSION)
        accept = norm(text) in ('li e aceito', 'aceito os termos e a politica de privacidade')
        decline = norm(text) in ('nao aceito', 'nao concordo')
        if pending:
            challenge = str(row['challenge'])
            accept |= button_id == 'wa_accept_' + challenge
            decline |= button_id == 'wa_decline_' + challenge
            if timestamp and float(timestamp) < int(row['presented_at'].timestamp()):
                accept = decline = False
        if pending and accept:
            await conn.execute('UPDATE whatsapp_channel_consents SET accepted_at=NOW() WHERE phone_hash=$1', key)
            return True, 'Aceite registrado. Se ainda não vinculou este número, vincule pelo aplicativo Braga Saúde. Depois envie novamente seu pedido.', []
        if decline:
            await conn.execute('DELETE FROM whatsapp_channel_consents WHERE phone_hash=$1', key)
            return True, 'Tudo bem. O atendimento deste canal fica bloqueado sem o aceite. Você pode continuar usando o aplicativo. A orientação de socorro permanece disponível.', []
        if not pending:
            challenge = str(uuid.uuid4())
            await conn.execute(
                'INSERT INTO whatsapp_channel_consents(phone_hash,terms_version,privacy_version,challenge) '
                'VALUES($1,$2,$3,$4::uuid) ON CONFLICT(phone_hash) DO UPDATE SET '
                'terms_version=$2,privacy_version=$3,challenge=$4::uuid,accepted_at=NULL,presented_at=NOW(),'
                "expires_at=NOW()+INTERVAL '10 minutes'", key, TERMS_VERSION, PRIVACY_VERSION, challenge)
        else:
            challenge = str(row['challenge'])
        return True, CONSENT_TEXT, [
            {'id': 'wa_accept_' + challenge, 'title': 'Li e aceito'},
            {'id': 'wa_decline_' + challenge, 'title': 'Não aceito'}]


async def prepare_deletion(pool, phone, actor_id=None):
    challenge = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO whatsapp_channel_deletions(phone_hash,actor_id,challenge) VALUES($1,$2,$3::uuid) '
            'ON CONFLICT(phone_hash) DO UPDATE SET actor_id=$2,challenge=$3::uuid,'
            "expires_at=NOW()+INTERVAL '5 minutes'", phone_hash(phone), actor_id, challenge)
    return DELETE_TEXT, [{'id': 'wa_delete_yes_' + challenge, 'title': 'SIM'},
                         {'id': 'wa_delete_no_' + challenge, 'title': 'NÃO'}]


async def resolve_deletion(pool, phone, *, text='', button_id='', timestamp=None):
    """None = sem confirmação pertinente; resultado só após a transação."""
    key = phone_hash(phone)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))', key)
        row = await conn.fetchrow(
            'SELECT * FROM whatsapp_channel_deletions WHERE phone_hash=$1 AND expires_at>NOW() FOR UPDATE', key)
        if not row:
            if button_id.startswith('wa_delete_'):
                return 'Essa confirmação de exclusão expirou ou já foi resolvida. Faça um novo pedido.'
            return None
        if timestamp and float(timestamp) < int(row['expires_at'].timestamp() - 300):
            return None
        challenge = str(row['challenge'])
        yes = norm(text) == 'sim' or button_id == 'wa_delete_yes_' + challenge
        no = norm(text) in ('nao', 'cancelar') or button_id == 'wa_delete_no_' + challenge
        if not yes and not no:
            return None
        if no:
            await conn.execute('DELETE FROM whatsapp_channel_deletions WHERE phone_hash=$1', key)
            return 'Tudo bem. Não apaguei seus dados nem desvinculei seu número.'
        actor = row['actor_id']
        # A posse do vínculo é revalidada para não apagar dados de outra conta.
        if not actor:
            linked = await conn.fetchrow(
                "SELECT id FROM profiles WHERE regexp_replace(whatsapp_phone, '\\D', '', 'g')=$1 FOR UPDATE",
                normalize_phone_digits(phone))
            if linked:
                return 'O vínculo mudou desde o pedido. Faça uma nova solicitação de exclusão.'
        if actor:
            current = await conn.fetchrow(
                "SELECT id FROM profiles WHERE id=$1 AND regexp_replace(whatsapp_phone, '\\D', '', 'g')=$2 FOR UPDATE",
                actor, normalize_phone_digits(phone))
            if not current:
                return 'O vínculo mudou desde o pedido. Faça uma nova solicitação de exclusão.'
            await conn.execute(
                "DELETE FROM assistant_preference_pending a WHERE a.user_id=$1 AND EXISTS "
                "(SELECT 1 FROM pending_confirmations p WHERE p.user_id=$1 AND p.metric_type='PREFERENCE' "
                "AND p.value_json->>'proposal_id'=a.proposal_id AND p.source_channel='whatsapp')", actor)
            await conn.execute(
                "DELETE FROM confirmation_logs WHERE channel='whatsapp' AND (user_id=$1 OR "
                "pending_conf_id IN (SELECT id FROM pending_confirmations WHERE channel_actor_id=$1))", actor)
            await conn.execute(
                "DELETE FROM pending_confirmations WHERE source_channel='whatsapp' AND "
                '(channel_phone_hash=$1 OR channel_actor_id=$2)', key, actor)
            await conn.execute('DELETE FROM whatsapp_operation_keys WHERE actor_id=$1', actor)
            await conn.execute('UPDATE profiles SET whatsapp_phone=NULL,whatsapp_linked_at=NULL WHERE id=$1', actor)
        await conn.execute('DELETE FROM whatsapp_message_receipts WHERE phone_hash=$1', key)
        await conn.execute("DELETE FROM whatsapp_link_attempts WHERE regexp_replace(sender_phone, '\\D', '', 'g')=$1", normalize_phone_digits(phone))
        await conn.execute('DELETE FROM whatsapp_channel_deletions WHERE phone_hash=$1', key)
        await conn.execute('DELETE FROM whatsapp_channel_consents WHERE phone_hash=$1', key)
        await conn.execute('INSERT INTO whatsapp_channel_cleanup(phone_hash) VALUES($1) ON CONFLICT DO NOTHING', key)
    return {'deleted': True, 'actor_id': actor, 'message': DELETED_TEXT}


async def mark_pending(pool, code, phone, actor_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE pending_confirmations SET source_channel='whatsapp',channel_actor_id=$2,channel_phone_hash=$3 "
            'WHERE confirmation_code=$1', code, actor_id, phone_hash(phone))
