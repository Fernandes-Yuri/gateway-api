"""ORB transport. Dependencies are injected so importing this module starts no services."""
import asyncio
import json
import logging
import time
from typing import Literal

import httpx
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from api_identity import firebase_uid
from orb_router import (recall as _recall, remember as _remember,
                        resolve_followup as _resolve_followup,
                        detect_all as _detect_all, dedupe_fala as _dedupe,
                        symptom_repeat as _symptom_repeat,
                        SYMPTOM_KINDS as _SYMPTOM_KINDS,
                        ESCALATION_SUFFIX as _ESCALATION_SUFFIX)
from trace_llm import trace_llm as _trace

ALLOWED_ACTIONS = {
    'REGISTRAR_PRESSAO', 'REGISTRAR_GLICEMIA', 'REGISTRAR_AGUA',
    'CONSULTAR_METRICAS', 'CONVERSA', 'EMERGENCIA',
    'REGISTRAR_BATIMENTOS', 'REGISTRAR_OXIGENACAO', 'LEMBRETES', 'EXAMES',
}

# tid por request (liga erros ao trace da conversa). Preenchido em generate().
_TID_BY_REQ: dict = {}
# Memória curta de clarificação por usuário (Fase 4): uid -> (kind, ts).
_CHAT_MEMORY: dict = {}
# Última fala template por usuário (anti-repetição): uid -> fala.
_CHAT_LASTFALA: dict = {}
# Repetições de sintoma por usuário (escalada anti-loop): uid -> (kind, ts, n).
_CHAT_SYMPTOM: dict = {}


class OrbMessage(BaseModel):
    role: Literal['user', 'assistant', 'system']
    content: str = Field(max_length=16000)


class OrbRequest(BaseModel):
    type: Literal['chat']
    id: str = Field(min_length=1, max_length=128)
    messages: list[OrbMessage] = Field(min_length=1, max_length=100)
    userId: str | None = None
    temperature: float = Field(default=0.65, ge=0, le=2)
    max_tokens: int = Field(default=350, ge=1, le=1024)
    # D50: escopo cuidador — o Android manda para agendar consulta por voz
    acting_as: str | None = None
    patient_id: str | None = None


from contextlib import asynccontextmanager


@asynccontextmanager
async def _use(semaphore):
    """Guarda de concorrência opcional (None = sem limite)."""
    if semaphore is None:
        yield
        return
    async with semaphore:
        yield


async def stream_content(client, url, token, payload, usage=None):
    """Read SSE deltas; an interrupted or truncated generation is never a success."""
    async with client.stream('POST', url, json={**payload, 'stream': True},
                             headers={'Authorization': f'Bearer {token}'}) as response:
        response.raise_for_status()
        finished = False
        async for line in response.aiter_lines():
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                if not finished:
                    raise ValueError('Missing successful finish reason')
                return
            chunk = json.loads(data)
            if usage is not None and chunk.get('usage'):
                usage.update(chunk['usage'])
            if 'error' in chunk:
                raise ValueError('Provider error')
            for choice in chunk.get('choices', [])[:1]:
                reason = choice.get('finish_reason')
                if reason and reason != 'stop':
                    raise ValueError('Incomplete generation')
                finished = finished or reason == 'stop'
                content = choice.get('delta', {}).get('content')
                if content:
                    yield content
        if not finished:
            raise ValueError('Interrupted stream')


async def _classify_label(client, url, token, model, prompt):
    """Passo 1 (Fase 3): 1 token de intenção. Falha segura = CONVERSA."""
    from orb_llm import build_classify_payload, parse_label
    try:
        resp = await client.post(url, json=build_classify_payload(model, prompt),
                                 headers={'Authorization': f'Bearer {token}'})
        resp.raise_for_status()
        data = resp.json()
        return parse_label(data['choices'][0]['message']['content'])
    except asyncio.CancelledError:
        raise
    except Exception:
        return 'CONVERSA'


def install_orb_routes(app, *, pool_provider, system_prompt, model, url, token,
                       injection_check, log_sample, authenticate=firebase_uid,
                       client_factory=httpx.AsyncClient, first_timeout=35.0,
                       chunk_timeout=45.0, heartbeat_interval=10.0, batch_interval=0.05,
                       enable_router=True, semaphore=None, memory_handler=None):
    async def generate(send, uid, req):
        started = time.monotonic()
        prompt = next((m.content for m in reversed(req.messages) if m.role == 'user'), '')
        if not prompt.strip():
            await send(dict(type='error', id=req.id, code='INVALID_REQUEST', message='Mensagem vazia.'))
            return
        tid = _trace('chat-ws', 'request', user_text=prompt, req_id=req.id,
                     temperature=req.temperature, max_tokens=req.max_tokens)
        _TID_BY_REQ[req.id] = tid
        if any(injection_check(m.content) for m in req.messages if m.role == 'user'):
            # AUD-G01-adjacente: limpa a chave de rastreio antes do early-return,
            # senão o dict global _TID_BY_REQ cresce sem limite (DoS de RAM).
            _TID_BY_REQ.pop(req.id, None)
            from server import get_friendly_safety_response
            user_msg = next((m.content for m in reversed(req.messages) if m.role == 'user'), "")
            friendly = get_friendly_safety_response(user_msg)
            friendly_json = json.dumps(friendly, ensure_ascii=False)
            await send(dict(type='content', id=req.id, delta=friendly_json))
            await send(dict(type='content', id=req.id, delta=''))
            return
    
        # Camada 0 (Fase 2): roteador determinístico antes de qualquer LLM.
        # Saudação, valor de saúde, tópico clínico e fofoca cotidiana
        # nunca chegam ao modelo — resposta instantânea por template.
        # D50: no escopo CUIDADOR, a intencao de agendar consulta e tratada
        # ANTES do roteador — grava na agenda do paciente (vinculo validado).
        if getattr(req, 'acting_as', None) == 'caregiver' and getattr(req, 'patient_id', None):
            from voice_scheduling import quer_agendar as _quer_agendar
            if _quer_agendar(prompt):
                from orb_llm import compose_envelope
                try:
                    from server import db_pool as _pool
                except Exception:
                    _pool = None
                if _pool is not None:
                    try:
                        async with _pool.acquire() as _conn:
                            from voice_scheduling import agendar_por_voz as _agendar
                            envelope = await _agendar(_conn, uid, req.patient_id, prompt)
                    except Exception as exc:  # fail-closed: nunca finge sucesso
                        logging.warning('Agendamento por voz falhou: %s', exc)
                        envelope = compose_envelope(
                            'Não consegui agendar agora. Tente novamente em instantes.',
                            'AGENDAR_FALHA', {})
                    _envelope_json = json.dumps(envelope, ensure_ascii=False)
                    await send(dict(type='content', id=req.id, delta=_envelope_json))
                    await send(dict(type='content', id=req.id, delta=''))
                    try:
                        await asyncio.to_thread(log_sample, user_prompt=prompt,
                                                assistant_reply=_envelope_json,
                                                source='app_chat_scheduler',
                                                system_prompt='voice_scheduling',
                                                action=envelope.get('acao', 'AGENDAR_FALHA'),
                                                action_params=envelope.get('parametros', {}))
                    except Exception:
                        logging.warning('ORB dataset logging unavailable')
                    metrics = dict(time_to_first_chunk_ms=round((time.monotonic() - started) * 1000),
                                   total_generation_ms=round((time.monotonic() - started) * 1000),
                                   chunks=1, tokens=None, router=True)
                    await send(dict(type='done', id=req.id, fullContent=_envelope_json,
                                    action=envelope.get('acao', 'AGENDAR_FALHA'),
                                    parameters=envelope.get('parametros', {}), metrics=metrics))
                    _TID_BY_REQ.pop(req.id, None)
                    return
        from orb_router import route_chat as _route_chat
        routed = _route_chat(prompt) if enable_router else None
        preferences = {}
        if memory_handler and (routed is None or routed.get('acao') == 'CONVERSA'):
            memory_reply, preferences = await memory_handler(uid, prompt, req.messages)
            if memory_reply:
                from orb_llm import compose_envelope
                content = compose_envelope(memory_reply, 'CONVERSA', {})
                await send(dict(type='done', id=req.id, fullContent=content,
                                action='CONVERSA', parameters={}))
                _TID_BY_REQ.pop(req.id, None)
                return
        if routed is not None:
            _det_kind = _detect_all(prompt).get('kind')
            if _det_kind == 'nutrition':
                _remember(_CHAT_MEMORY, uid, 'nutrition')
            if _det_kind in _SYMPTOM_KINDS:
                if _symptom_repeat(_CHAT_SYMPTOM, uid, _det_kind) >= 2:
                    routed['fala'] += _ESCALATION_SUFFIX
            if routed['acao'] == 'CONVERSA':
                routed['fala'] = _dedupe(_CHAT_LASTFALA, uid, routed['fala'])
            _trace('chat-ws', 'router_hit', tid, action=routed['acao'], fala=routed['fala'])
            routed_json = json.dumps(routed, ensure_ascii=False)
            await send(dict(type='content', id=req.id, delta=routed_json))
            await send(dict(type='content', id=req.id, delta=''))
            try:
                await asyncio.to_thread(log_sample, user_prompt=prompt, assistant_reply=routed_json,
                                        source='app_chat_router', system_prompt=system_prompt,
                                        action=routed['acao'], action_params=routed['parametros'])
            except Exception:
                logging.warning('ORB dataset logging unavailable')
            metrics = dict(time_to_first_chunk_ms=round((time.monotonic() - started) * 1000),
                           total_generation_ms=round((time.monotonic() - started) * 1000),
                           chunks=1, tokens=None, router=True)
            logging.info('ORB router hit: %s', routed['acao'])
            await send(dict(type='done', id=req.id, fullContent=routed_json,
                            action=routed['acao'], parameters=routed['parametros'], metrics=metrics))
            _TID_BY_REQ.pop(req.id, None)
            return
        # Follow-up de clarificação (Fase 4): "já comi" / "põe na lista"
        # após template de nutrição — responde sem LLM e consome a memória.
        _follow = _resolve_followup(prompt, _recall(_CHAT_MEMORY, uid), channel='chat')
        if _follow is not None:
            _CHAT_MEMORY.pop(uid, None)
            _follow['fala'] = _dedupe(_CHAT_LASTFALA, uid, _follow['fala'])
            _trace('chat-ws', 'followup_hit', tid, fala=_follow['fala'])
            _follow_json = json.dumps(_follow, ensure_ascii=False)
            await send(dict(type='content', id=req.id, delta=_follow_json))
            await send(dict(type='content', id=req.id, delta=''))
            try:
                await asyncio.to_thread(log_sample, user_prompt=prompt, assistant_reply=_follow_json,
                                        source='app_chat_followup', system_prompt=system_prompt,
                                        action=_follow['acao'], action_params=_follow['parametros'])
            except Exception:
                logging.warning('ORB dataset logging unavailable')
            _fu_metrics = dict(time_to_first_chunk_ms=round((time.monotonic() - started) * 1000),
                               total_generation_ms=round((time.monotonic() - started) * 1000),
                               chunks=1, tokens=None, router=True, followup=True)
            logging.info('ORB followup hit')
            await send(dict(type='done', id=req.id, fullContent=_follow_json,
                            action=_follow['acao'], parameters=_follow['parametros'], metrics=_fu_metrics))
            _TID_BY_REQ.pop(req.id, None)
            return
        await send(dict(type='typing', id=req.id))
        row = None
        pool = pool_provider()
        if pool:
            try:
                async def load_context():
                    async with pool.acquire() as conn:
                        return await conn.fetchrow(
                            'SELECT systolic_pressure, diastolic_pressure, glucose_level FROM vital_signs '
                            'WHERE user_id=$1 ORDER BY measured_at DESC LIMIT 1', uid)
                row = await asyncio.wait_for(load_context(), timeout=2)
            except Exception:
                logging.warning('ORB context unavailable')
        from orb_llm import (SPEAK_SYSTEM, build_speak_payload, compose_envelope,
                             metricas_fala, FIXED_FALAS, build_classify_payload,
                             assistant_fala as _assistant_fala)
        history = [dict(role=m.role, content=f'<fala_usuario>{m.content}</fala_usuario>'
                        if m.role == 'user' else _assistant_fala(m.content))
                   for m in req.messages[-20:] if m.role in ('user', 'assistant')]
        full, buffer = '', ''
        count, pending_count = 0, 0
        first_ms = None
        usage = {}
        system_used = SPEAK_SYSTEM
        async with client_factory(timeout=httpx.Timeout(chunk_timeout, connect=first_timeout)) as client, \
                _use(semaphore):
            # Passo 1: classifica (barato, sem stream). Teto próprio via
            # wait_for — não depende do timeout do transporte.
            _trace('chat-ws', 'classify_request', tid, payload=build_classify_payload(model, prompt))
            _t0 = time.monotonic()
            try:
                label = await asyncio.wait_for(
                    _classify_label(client, url, token, model, prompt),
                    timeout=first_timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError('Classification stalled') from exc
            _trace('chat-ws', 'classify_response', tid, label=label,
                   latency_ms=round((time.monotonic() - _t0) * 1000))
            if label == 'CONSULTAR_METRICAS':
                full = compose_envelope(metricas_fala(row), 'CONSULTAR_METRICAS', {})
                system_used = 'metricas-template'
            elif label in FIXED_FALAS:
                full = compose_envelope(FIXED_FALAS[label], label, {})
                system_used = 'fixed-template'
            else:
                # Passo 2: só CONVERSA gera fala livre (texto puro, sem JSON).
                _capped = min(req.max_tokens, 350)
                payload = build_speak_payload(model, history, req.temperature, _capped, preferences)
                _trace('chat-ws', 'speak_request', tid, payload=payload)
                stream = stream_content(client, url, token, payload, usage)
                next_delta = asyncio.create_task(anext(stream))
                deadline = time.monotonic() + first_timeout
                flush_at = deadline
                try:
                    while True:
                        now = time.monotonic()
                        wait = max(0, min(deadline, flush_at if buffer else deadline) - now)
                        ready, _ = await asyncio.wait({next_delta}, timeout=wait)
                        if next_delta in ready:
                            try:
                                delta = next_delta.result()
                            except StopAsyncIteration:
                                break
                            full += delta
                            if len(full) > 64000:
                                raise ValueError('Response too large')
                            count += 1
                            now = time.monotonic()
                            deadline = now + chunk_timeout
                            if first_ms is None:
                                first_ms = round((now - started) * 1000)
                                await send(dict(type='chunk', id=req.id, content=delta))
                            else:
                                if not buffer:
                                    flush_at = now + batch_interval
                                buffer += delta
                                pending_count += 1
                            next_delta = asyncio.create_task(anext(stream))
                        now = time.monotonic()
                        if buffer and (pending_count >= 3 or now >= flush_at):
                            await send(dict(type='chunk', id=req.id, content=buffer))
                            buffer, pending_count = '', 0
                        if now >= deadline:
                            raise TimeoutError('Generation stalled')
                    if buffer:
                        await send(dict(type='chunk', id=req.id, content=buffer))
                finally:
                    next_delta.cancel()
                    await asyncio.gather(next_delta, return_exceptions=True)
                    await stream.aclose()
                fala = full.strip()
                if not fala or any(tok in fala for tok in ("{", "}", "```")):
                    # O passo de fala devolve texto puro. Estrutura vazada
                    # do provedor (JSON/código) nunca vira resposta.
                    _trace('chat-ws', 'speak_invalid', tid, reason='structure_leak',
                           full_text=full[:500])
                    raise ValueError('Invalid speech')
                full = compose_envelope(fala, 'CONVERSA', {})
        try:
            parsed = json.loads(full)
        except Exception:
            _trace('chat-ws', 'speak_invalid', tid, reason='unparseable', full_text=full[:500])
            raise ValueError('Invalid structured response')
        if (not isinstance(parsed, dict) or not isinstance(parsed.get('fala'), str)
                or not parsed['fala'].strip() or parsed.get('acao') not in ALLOWED_ACTIONS
                or not isinstance(parsed.get('parametros'), dict)):
            _trace('chat-ws', 'speak_invalid', tid, reason='schema', full_text=full[:500])
            raise ValueError('Invalid structured response')
        try:
            await asyncio.to_thread(log_sample, user_prompt=prompt, assistant_reply=full,
                                    source='app_chat', system_prompt=system_used,
                                    action=parsed['acao'], action_params=parsed['parametros'])
        except Exception:
            logging.warning('ORB dataset logging unavailable')
        metrics = dict(time_to_first_chunk_ms=first_ms,
                       total_generation_ms=round((time.monotonic() - started) * 1000),
                       chunks=count, tokens=usage.get('completion_tokens'))
        _trace('chat-ws', 'speak_response', tid, full_text=full, metrics=metrics)
        logging.info('ORB generation metrics: %s', metrics)
        await send(dict(type='done', id=req.id, fullContent=full,
                        action=parsed['acao'], parameters=parsed['parametros'], metrics=metrics))
        _TID_BY_REQ.pop(req.id, None)

    @app.websocket('/ws/orb')
    async def orb(ws: WebSocket):
        try:
            uid = await authenticate(ws)
        except HTTPException:
            await ws.close(code=1008)
            return
        await ws.accept()
        lock = asyncio.Lock()
        last_sent = time.monotonic()
        last_alive = time.monotonic()
        active_id, work = None, None

        async def send(data):
            nonlocal last_sent
            async with lock:
                await ws.send_json(data)
                if data['type'] != 'heartbeat':
                    last_sent = time.monotonic()

        async def heartbeat():
            last_heartbeat = time.monotonic()
            while True:
                await asyncio.sleep(max(.001, max(last_sent, last_heartbeat) + heartbeat_interval - time.monotonic()))
                if time.monotonic() - last_alive >= heartbeat_interval * 3:
                    await ws.close(code=4000, reason='Heartbeat timeout')
                    return
                if time.monotonic() - last_sent >= heartbeat_interval:
                    await send(dict(type='heartbeat', id=active_id))
                    last_heartbeat = time.monotonic()

        async def run(req):
            try:
                await generate(send, uid, req)
            except TimeoutError:
                _trace('chat-ws', 'error', _TID_BY_REQ.pop(req.id, None),
                       code='TIMEOUT', req_id=req.id)
                await send(dict(type='error', id=req.id, code='TIMEOUT', message='A resposta demorou demais. Tente novamente.'))
            except (httpx.HTTPError, ValueError) as _e:
                _detail = str(_e)[:200] if isinstance(_e, httpx.HTTPError) else type(_e).__name__
                _trace('chat-ws', 'error', _TID_BY_REQ.pop(req.id, None),
                       code='STREAM_FAILED', detail=_detail, req_id=req.id)
                await send(dict(type='error', id=req.id, code='STREAM_FAILED', message='Não foi possível concluir a resposta.'))
            except (WebSocketDisconnect, OSError):
                _TID_BY_REQ.pop(req.id, None)
            except asyncio.CancelledError:
                _TID_BY_REQ.pop(req.id, None)
                raise
            except Exception:
                _trace('chat-ws', 'error', _TID_BY_REQ.pop(req.id, None),
                       code='STREAM_FAILED', req_id=req.id)
                logging.exception('ORB generation failed')
                await send(dict(type='error', id=req.id, code='STREAM_FAILED', message='Não foi possível concluir a resposta.'))

        beat = asyncio.create_task(heartbeat())
        try:
            while True:
                raw = await ws.receive_text()
                last_alive = time.monotonic()
                req_id = None
                try:
                    if len(raw) > 64000:
                        raise ValueError('Request too large')
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError('Expected object')
                    if isinstance(data.get('id'), str):
                        req_id = data['id'][:128]
                    if data.get('type') == 'pong':
                        continue
                    if data.get('type') == 'ping':
                        await send(dict(type='pong', id=req_id))
                        continue
                    if data.get('type') == 'cancel':
                        if work and active_id == req_id:
                            work.cancel()
                            await asyncio.gather(work, return_exceptions=True)
                            work, active_id = None, None
                        await send(dict(type='cancelled', id=req_id))
                        continue
                    if data.get('type') == 'receipt':
                        # Recibo de executor do agente B1: valida, audita, responde closing.
                        try:
                            from orb_actions import ACTION_CATALOG, closing_fala
                            r_action = data.get('action')
                            r_params = data.get('params') or {}
                            r_key = data.get('idempotencyKey') or ''
                            if r_action not in ACTION_CATALOG or not isinstance(r_params, dict) or not r_key:
                                raise ValueError('Invalid receipt')
                            closing = closing_fala(r_action, r_params)
                            status = 'ok'
                            pool = pool_provider()
                            if pool is not None:
                                try:
                                    async with pool.acquire() as conn:
                                        row = await conn.fetchrow("""
                                            INSERT INTO agent_actions (user_id, channel, action, params, idempotency_key, status, result)
                                            VALUES ($1, 'chat', $2, $3, $4, 'confirmed', '{}')
                                            ON CONFLICT (user_id, idempotency_key) DO NOTHING
                                            RETURNING id;""",
                                            uid, r_action, json.dumps(r_params), r_key)
                                        status = 'ok' if row else 'duplicate'
                                except Exception:
                                    logging.warning('ORB receipt logging unavailable')
                            await send(dict(type='receipt_ack', id=req_id, closing=closing, status=status))
                        except (ValueError, ValidationError):
                            await send(dict(type='error', id=req_id, code='INVALID_REQUEST', message='Recibo inválido.'))
                        continue
                    req = OrbRequest.model_validate(data)
                except (ValueError, ValidationError):
                    await send(dict(type='error', id=req_id, code='INVALID_REQUEST', message='Pedido inválido ou muito longo (limite: 16.000 caracteres por mensagem).'))
                    continue
                if work and not work.done():
                    await send(dict(type='error', id=req.id, code='BUSY', message='Aguarde ou cancele a resposta atual.'))
                    continue
                if req.userId and req.userId != uid:
                    await send(dict(type='error', id=req.id, code='FORBIDDEN', message='Conta não autorizada.'))
                    continue
                try:
                    if await authenticate(ws) != uid:
                        raise HTTPException(401)
                except HTTPException:
                    await send(dict(type='error', id=req.id, code='AUTH_EXPIRED', message='Atualize sua sessão.'))
                    await ws.close(code=4001)
                    return
                active_id = req.id
                work = asyncio.create_task(run(req))
        except (WebSocketDisconnect, OSError):
            pass
        finally:
            beat.cancel()
            if work:
                work.cancel()
            await asyncio.gather(beat, *([work] if work else []), return_exceptions=True)
