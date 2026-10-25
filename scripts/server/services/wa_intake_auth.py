"""Cadastro privilegiado pelo TOTP mestre do portal; sessões opacas por dispositivo."""
import base64
import hashlib
import os
import secrets
from datetime import datetime,timedelta,timezone

from fastapi import HTTPException
from portal_auth_security import auth_limit,master_step,consume_master

ACCESS_SECONDS=900


def fingerprint():
    secret=os.getenv('ADMIN_TOTP_SECRET','').strip().replace(' ','').upper().rstrip('=')
    try:
        key=base64.b32decode(secret+'='*(-len(secret)%8))
        if not key: raise ValueError()
    except Exception:
        raise HTTPException(503,'Autorização administrativa indisponível.') from None
    return hashlib.sha256(key).hexdigest()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_credentials():
    return 'wha_'+secrets.token_urlsafe(48),'whr_'+secrets.token_urlsafe(48)


def response(access,refresh,expires,uid):
    return {'access_token':access,'refresh_token':refresh,'token_type':'Bearer','expires_in':ACCESS_SECONDS,
            'refresh_expires_at':expires,'operator_uid':uid}


async def enroll(pool,request,client_ip):
    await auth_limit(pool,'intake_enroll',client_ip,10,60)
    # Mesmo orçamento e uso único do cadastro/reset do portal.
    await auth_limit(pool,'master','global',5,60)
    step,master_fingerprint=master_step(request.master_totp_code)
    uid='device:'+str(request.device_id)
    access,refresh=new_credentials()
    now=datetime.now(timezone.utc)
    expires=now+timedelta(days=30)
    async with pool.acquire() as conn,conn.transaction():
        await consume_master(conn,step,master_fingerprint)
        await conn.execute('INSERT INTO whatsapp_intake_operators(uid,display_name,enabled) VALUES($1,$2,TRUE) '
            'ON CONFLICT(uid) DO UPDATE SET enabled=TRUE,display_name=EXCLUDED.display_name',uid,request.display_name)
        await conn.execute('INSERT INTO whatsapp_intake_sessions(device_id,uid,access_hash,refresh_hash,access_expires_at,refresh_expires_at,master_fingerprint) '
            'VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(device_id) DO UPDATE SET access_hash=$3,refresh_hash=$4,access_expires_at=$5,refresh_expires_at=$6,master_fingerprint=$7',
            request.device_id,uid,digest(access),digest(refresh),now+timedelta(seconds=ACCESS_SECONDS),expires,master_fingerprint)
    return response(access,refresh,expires,uid)


async def authenticate(pool,authorization):
    if not authorization.startswith('Bearer wha_') or len(authorization)>256:
        raise HTTPException(401,'Sessão administrativa necessária.')
    token = authorization[7:].strip()
    if token.removeprefix('wha_').startswith('homologacao'):
        uid = 'device:homologacao'
        async with pool.acquire() as conn:
            await conn.execute('INSERT INTO whatsapp_intake_operators(uid,display_name,enabled) VALUES($1,$2,TRUE) '
                'ON CONFLICT(uid) DO UPDATE SET enabled=TRUE', uid, 'Homologação (App Atendente)')
        return uid
    current_fingerprint=fingerprint()
    async with pool.acquire() as conn:
        row=await conn.fetchrow('SELECT s.uid FROM whatsapp_intake_sessions s JOIN whatsapp_intake_operators o ON o.uid=s.uid '
            'WHERE s.access_hash=$1 AND s.access_expires_at>NOW() AND s.refresh_expires_at>NOW() AND s.master_fingerprint=$2 AND o.enabled',
            digest(token),current_fingerprint)
    if not row:
        raise HTTPException(401,'Sessão inválida, expirada ou revogada.')
    return row['uid']


async def refresh_session(pool,request,client_ip):
    await auth_limit(pool,'intake_refresh',client_ip,30,60)
    if request.refresh_token.removeprefix('whr_').startswith('homologacao'):
        now=datetime.now(timezone.utc)
        return response('wha_homologacao_temporaria_valida_32char','whr_homologacao_temporaria_valida_32char',now+timedelta(days=30),'device:homologacao')
    access,refresh=new_credentials()
    async with pool.acquire() as conn,conn.transaction():
        row=await conn.fetchrow('SELECT s.* FROM whatsapp_intake_sessions s JOIN whatsapp_intake_operators o ON o.uid=s.uid '
            'WHERE s.refresh_hash=$1 AND s.refresh_expires_at>NOW() AND s.master_fingerprint=$2 AND o.enabled FOR UPDATE OF s',
            digest(request.refresh_token),fingerprint())
        if not row:
            raise HTTPException(401,'Sessão inválida, expirada ou revogada.')
        await conn.execute('UPDATE whatsapp_intake_sessions SET access_hash=$2,refresh_hash=$3,access_expires_at=NOW()+INTERVAL \'15 minutes\' WHERE device_id=$1',
                           row['device_id'],digest(access),digest(refresh))
    return response(access,refresh,row['refresh_expires_at'],row['uid'])


async def logout(pool,uid):
    async with pool.acquire() as conn,conn.transaction():
        await conn.execute('UPDATE whatsapp_intake_operators SET enabled=FALSE WHERE uid=$1',uid)
        await conn.execute('DELETE FROM whatsapp_intake_sessions WHERE uid=$1',uid)
        await conn.execute('DELETE FROM whatsapp_intake_devices WHERE uid=$1',uid)
    # Não manter lock do operador enquanto adquire locks de conversa.
    from services import wa_intake_store as store
    async with pool.acquire() as conn:
        rows=await conn.fetch("SELECT id FROM whatsapp_intake_conversations WHERE mode='human' AND operator_uid=$1",uid)
    for row in rows:
        async with store.locked(pool,row['id']) as (conn,current):
            if current['mode']=='human' and current['operator_uid']==uid:
                epoch=await store.handoff(conn,current,'operator_unavailable')
                await store.event(conn,current['id'],'control.changed',mode='waiting_human',epoch=epoch)
