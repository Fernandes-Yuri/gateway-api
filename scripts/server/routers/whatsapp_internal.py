"""API v1 da central futura; identidade Firebase mais autorização no servidor."""
import asyncio
import json
import os
import re
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response

from services import wa_intake_auth as sessions
from security_defense import get_client_ip
from schemas.whatsapp_intake import (EnrollmentRequest, RefreshRequest, SessionResponse, Capabilities, CommandResult, ControlRequest, Conversation, ConversationList, DeviceRequest, EventPage, MessagePage,
                                     QueuedMessage, SeenRequest, TextRequest)
from services import wa_intake_store as store
from services import wa_intake_transport as transport

router = APIRouter(prefix='/api/internal/whatsapp', tags=['whatsapp-internal-v1'])


class State:
    db_pool = None


_state = State()


def pool():
    if _state.db_pool is None:
        raise HTTPException(503, 'Banco de dados indisponível.')
    return _state.db_pool


async def check_operator(uid):
    async with pool().acquire() as conn:
        if not await store.authorized(conn, uid):
            raise HTTPException(403, 'Acesso exclusivo aos operadores autorizados.')
    return uid


async def operator(request: Request):
    return await sessions.authenticate(pool(),request.headers.get('authorization',''))


@router.post('/auth/enroll',response_model=SessionResponse,status_code=201)
async def enroll(request: Request,body: EnrollmentRequest,response: Response):
    response.headers['Cache-Control']='no-store'
    return await sessions.enroll(pool(),body,get_client_ip(request))


@router.post('/auth/refresh',response_model=SessionResponse)
async def refresh(request: Request,body: RefreshRequest,response: Response):
    response.headers['Cache-Control']='no-store'
    return await sessions.refresh_session(pool(),body,get_client_ip(request))


@router.post('/auth/logout',status_code=204)
async def logout(uid=Depends(operator)):
    await sessions.logout(pool(),uid)
    return Response(status_code=204)


@router.get('/capabilities', response_model=Capabilities)
async def capabilities(uid=Depends(operator)):
    from services.wa_intake_worker import enabled
    import shutil
    return {'api_version': '1', 'intake_enabled': enabled(), 'audio_validation_available': bool(shutil.which(os.getenv('WHATSAPP_FFPROBE_BIN', 'ffprobe'))),
            'audio_max_bytes': transport.MAX_MEDIA, 'audio_max_seconds': 600,
            'audio_mime_types': ['audio/ogg', 'audio/mpeg', 'audio/mp4', 'audio/aac', 'audio/amr'],
            'image_max_bytes': transport.MAX_IMAGE, 'realtime': 'websocket', 'operator_uid': uid}


@router.get('/conversations', response_model=ConversationList)
async def conversations(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),
                        mode: str | None = Query(None, pattern='^(bot|human|waiting_human|closed)$'), uid=Depends(operator)):
    async with pool().acquire() as conn, conn.transaction(isolation='repeatable_read', readonly=True):
        rows = await conn.fetch(
            "SELECT c.*,(SELECT jsonb_build_object('id',id,'status',status,'reason',reason,'epoch',epoch,'attempts',attempts) "
            "FROM whatsapp_intake_handoffs WHERE conversation_id=c.id ORDER BY created_at DESC LIMIT 1) handoff,"
            "(SELECT count(*) FROM whatsapp_intake_messages m WHERE m.conversation_id=c.id AND m.origin='contact' "
            "AND m.id>COALESCE((SELECT message_id FROM whatsapp_intake_reads WHERE conversation_id=c.id AND uid=$4),0)) unread_count "
            "FROM whatsapp_intake_conversations c WHERE ($1::text IS NULL OR mode=$1) ORDER BY updated_at DESC,id DESC LIMIT $2 OFFSET $3",
            mode, limit + 1, offset, uid)
        cursor = await conn.fetchval('SELECT COALESCE(max(id),0) FROM whatsapp_intake_events')
    items = [{**store.public_conversation(r), 'unread_count': r['unread_count']} for r in rows[:limit]]
    return {'items': items, 'next_offset': offset + len(items), 'has_more': len(rows) > limit, 'event_cursor': cursor}


@router.get('/conversations/{conversation_id}', response_model=Conversation)
async def conversation(conversation_id: UUID, uid=Depends(operator)):
    async with pool().acquire() as conn:
        row = await conn.fetchrow("SELECT c.*,(SELECT jsonb_build_object('id',id,'status',status,'reason',reason,'epoch',epoch,'attempts',attempts) FROM whatsapp_intake_handoffs WHERE conversation_id=c.id ORDER BY created_at DESC LIMIT 1) handoff FROM whatsapp_intake_conversations c WHERE id=$1", conversation_id)
    if not row:
        raise HTTPException(404, 'Atendimento não encontrado.')
    return store.public_conversation(row)


@router.get('/conversations/{conversation_id}/messages', response_model=MessagePage)
async def messages(conversation_id: UUID, cursor: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), uid=Depends(operator)):
    async with pool().acquire() as conn:
        if not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM whatsapp_intake_conversations WHERE id=$1)', conversation_id):
            raise HTTPException(404, 'Atendimento não encontrado.')
        rows = await conn.fetch(
            'SELECT m.*,a.id media_id,a.status media_status,a.mime_type media_mime_type FROM whatsapp_intake_messages m '
            'LEFT JOIN whatsapp_intake_media a ON a.message_id=m.id WHERE m.conversation_id=$1 AND m.id>$2 ORDER BY m.id LIMIT $3',
            conversation_id, cursor, limit + 1)
    items = [store.public_message(r) for r in rows[:limit]]
    return {'items': items, 'next_cursor': items[-1]['id'] if items else cursor, 'has_more': len(rows) > limit}


@router.post('/conversations/{conversation_id}/claim', response_model=CommandResult)
async def claim(conversation_id: UUID, request: ControlRequest, uid=Depends(operator)):
    return await store.control(pool(), conversation_id, uid, 'claim', request)


@router.post('/conversations/{conversation_id}/release', response_model=CommandResult)
async def release(conversation_id: UUID, request: ControlRequest, uid=Depends(operator)):
    return await store.control(pool(), conversation_id, uid, 'release', request)


@router.post('/conversations/{conversation_id}/close', response_model=CommandResult)
async def close(conversation_id: UUID, request: ControlRequest, uid=Depends(operator)):
    return await store.control(pool(), conversation_id, uid, 'close', request)


@router.post('/conversations/{conversation_id}/messages', status_code=202, response_model=QueuedMessage)
async def send_text(conversation_id: UUID, request: TextRequest, uid=Depends(operator)):
    return await store.human_message(pool(), conversation_id, uid, request)


@router.post('/conversations/{conversation_id}/audio', status_code=202, response_model=QueuedMessage)
async def send_audio(conversation_id: UUID, epoch: int = Form(ge=0), idempotency_key: str = Form(min_length=8, max_length=128),
                     file: UploadFile = File(), uid=Depends(operator)):
    from pydantic import ValidationError
    try:
        request = ControlRequest(epoch=epoch, idempotency_key=idempotency_key)
    except ValidationError:
        raise HTTPException(422, 'Controle inválido.') from None
    content = await file.read(transport.MAX_MEDIA + 1)
    mime = (file.content_type or '').split(';')[0].lower()
    try:
        await transport.validate_media(content, mime, 'audio')
    except transport.TransportFailure as error:
        raise HTTPException(503 if error.code == 'ffprobe_required' else 422, error.code) from None
    finally:
        await file.close()
    return await store.human_message(pool(), conversation_id, uid, request, content=content, mime=mime)


@router.get('/media/{media_id}')
async def media(media_id: UUID, request: Request, uid=Depends(operator)):
    async with pool().acquire() as conn:
        row = await conn.fetchrow('SELECT content,mime_type,status FROM whatsapp_intake_media WHERE id=$1', media_id)
    if not row:
        raise HTTPException(404, 'Anexo não encontrado.')
    if row['status'] != 'ready':
        raise HTTPException(409, 'Anexo ainda não está disponível.')
    content = bytes(row['content'])
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
               'Content-Disposition': 'inline', 'Accept-Ranges': 'bytes'}
    byte_range = request.headers.get('range')
    if byte_range:
        match = re.fullmatch(r'bytes=(\d{0,18})-(\d{0,18})', byte_range)
        if not match or not any(match.groups()):
            return Response(status_code=416,headers={**headers,'Content-Range':f'bytes */{len(content)}'})
        first,last = match.groups()
        start = int(first) if first else max(0,len(content)-int(last))
        end = min(int(last),len(content)-1) if first and last else len(content)-1
        if start> end or start>=len(content):
            return Response(status_code=416,headers={**headers,'Content-Range':f'bytes */{len(content)}'})
        headers['Content-Range'] = f'bytes {start}-{end}/{len(content)}'
        return Response(content=content[start:end+1],status_code=206,media_type=row['mime_type'],headers=headers)
    return Response(content=content,media_type=row['mime_type'],headers=headers)


@router.post('/conversations/{conversation_id}/seen', status_code=204)
async def seen(conversation_id: UUID, request: SeenRequest, uid=Depends(operator)):
    async with store.locked(pool(), conversation_id) as (conn, _):
        if request.message_id and not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM whatsapp_intake_messages WHERE id=$1 AND conversation_id=$2)', request.message_id, conversation_id):
            raise HTTPException(422, 'Mensagem não pertence ao atendimento.')
        await conn.execute('INSERT INTO whatsapp_intake_reads VALUES($1,$2,$3) ON CONFLICT(conversation_id,uid) DO UPDATE SET message_id=GREATEST(whatsapp_intake_reads.message_id,EXCLUDED.message_id)', conversation_id, uid, request.message_id)
        await conn.execute("UPDATE whatsapp_intake_handoffs SET status='seen' WHERE conversation_id=$1 AND status IN ('pending','push_accepted')", conversation_id)
    return Response(status_code=204)


@router.post('/devices', status_code=204)
async def register_device(request: DeviceRequest, uid=Depends(operator)):
    async with pool().acquire() as conn, conn.transaction():
        if not await store.authorized(conn, uid):
            raise HTTPException(403, 'Acesso revogado.')
        existing = await conn.fetchrow('SELECT uid FROM whatsapp_intake_devices WHERE id=$1 OR token=$2 FOR UPDATE', request.device_id, request.token)
        if existing and existing['uid'] != uid:
            raise HTTPException(409, 'Dispositivo já registrado.')
        await conn.execute('DELETE FROM whatsapp_intake_devices WHERE token=$1 AND id<>$2', request.token, request.device_id)
        await conn.execute('INSERT INTO whatsapp_intake_devices(id,uid,token) VALUES($1,$2,$3) ON CONFLICT(id) DO UPDATE SET token=EXCLUDED.token,updated_at=NOW()', request.device_id, uid, request.token)
    return Response(status_code=204)


@router.delete('/devices/{device_id}', status_code=204)
async def delete_device(device_id: UUID, uid=Depends(operator)):
    async with pool().acquire() as conn:
        await conn.execute('DELETE FROM whatsapp_intake_devices WHERE id=$1 AND uid=$2', device_id, uid)
    return Response(status_code=204)


async def event_page(cursor, limit=100):
    async with pool().acquire() as conn:
        oldest = await conn.fetchval('SELECT min(id) FROM whatsapp_intake_events')
        if cursor and oldest and cursor < oldest - 1:
            raise HTTPException(410, 'Cursor expirado. Sincronize a lista de atendimentos.')
        rows = await conn.fetch('SELECT * FROM whatsapp_intake_events WHERE id>$1 ORDER BY id LIMIT $2', cursor, limit + 1)
    items = [{**dict(row), 'data': store.decoded(row['data'])} for row in rows[:limit]]
    return {'items': items, 'next_cursor': items[-1]['id'] if items else cursor, 'has_more': len(rows) > limit}


@router.get('/events', response_model=EventPage)
async def events(cursor: int = Query(0, ge=0), uid=Depends(operator)):
    return await event_page(cursor)


@router.websocket('/events')
async def stream(websocket: WebSocket):
    # Header Authorization do OkHttp. Não aceitar token na URL.
    try:
        authorization=websocket.headers.get('authorization','')
        await sessions.authenticate(pool(),authorization)
        cursor=int(websocket.query_params.get('cursor','0'))
        if cursor<0: raise ValueError()
    except (HTTPException,ValueError):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            await sessions.authenticate(pool(),authorization)
            page = await event_page(cursor)
            await websocket.send_text(json.dumps(jsonable_encoder(page)))
            cursor = page['next_cursor']
            await asyncio.sleep(0.2 if page['has_more'] else 1)
    except WebSocketDisconnect:
        return
    except HTTPException as error:
        await websocket.close(code=1008, reason='resync_required' if error.status_code == 410 else 'authorization_required')
