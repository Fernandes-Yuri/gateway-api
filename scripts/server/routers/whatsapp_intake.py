"""Webhook público exclusivamente para captação Yuri, sem endpoints do canal de saúde retirado."""
import hashlib
import hmac
import json
import os

from fastapi import APIRouter,HTTPException,Query,Request
from fastapi.responses import JSONResponse,PlainTextResponse
from services.wa_intake_worker import enabled
from services.wa_intake_store import ingest

router=APIRouter()


class State:
    db_pool=None


_state=State()


def require_enabled():
    if not enabled():
        raise HTTPException(404,'Not Found')


def verify_meta_signature(signature,body):
    secret=os.getenv('WHATSAPP_APP_SECRET','')
    if not secret or not signature or not signature.startswith('sha256='):
        return False
    expected='sha256='+hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature,expected)


@router.get('/api/whatsapp/webhook',include_in_schema=False)
async def verify(hub_mode:str|None=Query(None,alias='hub.mode'),hub_challenge:str|None=Query(None,alias='hub.challenge'),hub_verify_token:str|None=Query(None,alias='hub.verify_token')):
    require_enabled()
    token=os.getenv('WHATSAPP_VERIFY_TOKEN','')
    if not token:
        raise HTTPException(503,'Verificação do canal indisponível.')
    if hub_mode!='subscribe' or not hub_verify_token or not hmac.compare_digest(hub_verify_token,token):
        raise HTTPException(403,'Verificação inválida.')
    return PlainTextResponse(hub_challenge or '')


@router.post('/api/whatsapp/webhook',include_in_schema=False)
async def receive(request:Request):
    require_enabled()
    chunks,size=[],0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>1024*1024:
            return JSONResponse({'status':'payload_too_large'},status_code=413)
        chunks.append(chunk)
    raw=b''.join(chunks)
    if not verify_meta_signature(request.headers.get('x-hub-signature-256'),raw):
        return JSONResponse({'status':'unauthorized'},status_code=401)
    receiver=os.getenv('WHATSAPP_PHONE_NUMBER_ID','')
    if not _state.db_pool or not receiver:
        return JSONResponse({'status':'unavailable'},status_code=503)
    try:
        body=json.loads(raw)
        if not isinstance(body,dict) or body.get('object')!='whatsapp_business_account':
            raise ValueError()
        await ingest(_state.db_pool,body,receiver)
    except HTTPException:
        raise
    except (ValueError,TypeError,AttributeError):
        return JSONResponse({'status':'invalid_payload'},status_code=400)
    except Exception:
        return JSONResponse({'status':'unavailable'},status_code=503)
    return {'status':'received'}
