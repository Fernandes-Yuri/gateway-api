"""Doses canônicas e lotes v1; chamador controla transação e commit.

REST e adaptadores de canal usam estas funções, nunca HTTP interno.
Os locks de medicamentos ordenados serializam a baixa individual e coletiva.
"""
import hashlib
import json
from datetime import date, datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from services.care_os import require_patient_permission

DEFAULT_ZONE = 'America/Sao_Paulo'


def fail(status, code, message, *, items=None, retryable=False):
    raise HTTPException(status, {'code': code, 'message': message,
                                 'retryable': retryable, 'items': items or []})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def utc_text(instant):
    return instant.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def occurrence_id(patient_id, medication_id, instant):
    canonical_medication = str(UUID(str(medication_id)))
    return str(uuid5(NAMESPACE_URL, f'{patient_id}/{canonical_medication}/{utc_text(instant)}'))


async def patient_profile(conn, patient_id, *, lock=False):
    profile = await conn.fetchrow('SELECT full_name,timezone FROM profiles WHERE id=$1' + (' FOR NO KEY UPDATE' if lock else ''), patient_id)
    if not profile:
        fail(404, 'PATIENT_NOT_FOUND', 'Paciente não encontrado.')
    name = profile.get('timezone')
    source = 'profile'
    try:
        zone = ZoneInfo(name) if name else ZoneInfo(DEFAULT_ZONE)
        if not name:
            source = 'fallback'
    except (ZoneInfoNotFoundError, ValueError):
        zone, source = ZoneInfo(DEFAULT_ZONE), 'fallback'
    return profile, zone, source


async def authorize(conn, actor_id, patient_id, permission):
    try:
        await require_patient_permission(conn, actor_id, patient_id, permission)
    except HTTPException as exc:
        fail(exc.status_code, 'PERMISSION_DENIED', str(exc.detail))
    # Bloqueia a revogação concorrente até o commit, depois revalida.
    if actor_id != patient_id:
        await conn.fetch('SELECT id FROM family_bindings WHERE patient_id=$1 AND caregiver_id=$2 FOR SHARE', patient_id, actor_id)
        await require_patient_permission(conn, actor_id, patient_id, permission)


async def reserve_occurrence(conn, *, patient_id, medication_id, instant, zone):
    oid = occurrence_id(patient_id, medication_id, instant)
    local = instant.astimezone(zone)
    await conn.execute('''INSERT INTO medication_dose_occurrences
        (id,patient_id,medication_id,scheduled_at,local_date,local_time,timezone)
        VALUES($1::uuid,$2,$3::uuid,$4,$5,$6,$7) ON CONFLICT DO NOTHING''',
        oid, patient_id, str(medication_id), instant, local.date(), local.strftime('%H:%M'), zone.key)
    return oid


async def occurrence_log(conn, patient_id, medication_id, instant):
    return await conn.fetchrow('''SELECT l.id,l.units_taken,l.taken_at,o.historical_conflict
        FROM medication_dose_occurrences o JOIN medication_logs l ON l.id=o.log_id
        WHERE o.patient_id=$1 AND o.medication_id=$2::uuid AND o.scheduled_at=$3''',
        patient_id, str(medication_id), instant)


def instant_for(day, local_time, zone):
    naive = datetime.fromisoformat(f'{day.isoformat()}T{local_time}:00')
    candidates = {naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
                  for fold in (0, 1)
                  if naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive}
    # Transições ambíguas/inexistentes exigem revisão, não escolha silenciosa.
    if len(candidates) != 1:
        fail(409, 'REVIEW_REQUIRED', 'Horário local ambíguo ou inexistente no fuso do paciente.')
    return candidates.pop()


async def groups(conn, *, actor_id, patient_id, day=None):
    await authorize(conn, actor_id, patient_id, 'care:read')
    profile, zone, source = await patient_profile(conn, patient_id)
    day = day or datetime.now(zone).date()
    meds = await conn.fetch('SELECT * FROM medications WHERE user_id=$1 ORDER BY id', patient_id)
    slots = await conn.fetch('''SELECT s.* FROM medication_dose_schedules s JOIN medications m ON m.id=s.medication_id
                               WHERE m.user_id=$1 AND s.active ORDER BY s.local_time,s.medication_id''', patient_id)
    by_id = {str(m['id']): m for m in meds}
    grouped = {}
    review_items = []
    invalid_medications = set()
    for med in meds:
        times = (med.get('schedule_times') or med.get('schedule_time') or '').split(',')
        try:
            valid = all(datetime.strptime(t.strip(), '%H:%M').strftime('%H:%M') == t.strip() for t in times)
        except ValueError:
            valid = False
        if not valid:
            invalid_medications.add(str(med['id']))
            review_items.append({'medication_id': str(med['id']), 'name': med['name'], 'reason_code': 'schedule_invalid'})
    for slot in slots:
        med = by_id[str(slot['medication_id'])]
        local_time = slot['local_time']
        instant = instant_for(day, local_time, zone)
        existing = await occurrence_log(conn, patient_id, med['id'], instant)
        reason = 'schedule_invalid' if str(med['id']) in invalid_medications else None
        if not slot['quantity_confirmed'] or slot['dose_units'] is None:
            reason = reason or 'quantity_required'
        # Explicit integer stock units only; liquids/volumes remain individual.
        form = (med.get('pharmaceutical_form') or '').lower()
        if form and not any(x in form for x in ('comprimid', 'capsul', 'cápsul', 'tablet')):
            reason = 'unsupported_unit'
        if slot['dose_units'] and (med.get('current_units') or 0) < slot['dose_units']:
            reason = reason or 'insufficient_stock'
        instruction = med.get('meal_context')
        if instruction and instruction not in {'JEJUM', 'APOS_CAFE', 'ANTES_ALMOCO', 'APOS_ALMOCO', 'ANTES_JANTAR', 'APOS_JANTAR', 'AO_DEITAR'}:
            reason = 'needs_review'
        if existing and existing['historical_conflict']:
            reason = 'needs_review'
        # Textos antigos sem fuso não podem liberar uma segunda tomada.
        historical = await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM medication_dose_migration_issues i
            JOIN medication_logs l ON l.id=i.log_id WHERE l.medication_id=$1 AND
            (left(l.scheduled_for,16)=$2 OR (l.scheduled_for IS NULL AND
            (l.taken_at AT TIME ZONE $3)::date=$4)))''', med['id'], f'{day}T{local_time}', zone.key, day)
        if historical:
            reason = 'needs_review'
        item = {'occurrence_id': occurrence_id(patient_id, med['id'], instant),
                'medication_id': str(med['id']), 'name': med['name'],
                'dose_units': slot['dose_units'], 'instruction_text': med.get('meal_context'),
                'schedule_revision': med['schedule_revision'], 'scheduled_at': utc_text(instant),
                'state': 'blocked' if reason == 'needs_review' else ('recorded' if existing else ('blocked' if reason else 'pending')),
                'reason_code': reason if not existing or reason == 'needs_review' else None}
        grouped.setdefault(local_time, []).append(item)
    result = []
    for local_time, items in sorted(grouped.items()):
        signature = {'patient_id': patient_id, 'timezone': zone.key, 'local_date': str(day),
                     'local_time': local_time, 'items': [{k: v for k, v in i.items() if k not in ('state', 'reason_code')} for i in items]}
        counts = {key: sum(i['state'] == key for i in items) for key in ('pending', 'recorded', 'blocked')}
        counts['total'] = len(items)
        result.append({'group_id': str(uuid5(NAMESPACE_URL, f'group/{patient_id}/{day}/{local_time}/{zone.key}')),
                       'local_time': local_time, 'scheduled_at': items[0]['scheduled_at'],
                       'schedule_digest': digest(signature), 'counts': counts,
                       'can_confirm_all': counts['blocked'] == 0 and counts['pending'] > 0, 'items': items})
    return {'medication_batch_contract': 1, 'patient_id': patient_id,
            'patient_name': profile['full_name'], 'timezone': zone.key, 'timezone_source': source,
            'local_date': str(day), 'generated_at': utc_text(datetime.now(timezone.utc)), 'groups': result, 'review_items': review_items}


async def read_schedule(conn, *, actor_id, patient_id, medication_id):
    await authorize(conn, actor_id, patient_id, 'medication:write')
    med = await conn.fetchrow('SELECT id,schedule_revision FROM medications WHERE id=$1::uuid AND user_id=$2', str(medication_id), patient_id)
    if not med:
        fail(404, 'MEDICATION_NOT_FOUND', 'Medicamento não encontrado.')
    slots = await conn.fetch('SELECT local_time,dose_units,quantity_confirmed FROM medication_dose_schedules WHERE medication_id=$1::uuid AND active ORDER BY local_time', str(medication_id))
    return {'medication_batch_contract': 1, 'medication_id': str(med['id']),
            'schedule_revision': med['schedule_revision'], 'slots': [dict(row) for row in slots]}


async def update_schedule(conn, *, actor_id, patient_id, medication_id, payload):
    await authorize(conn, actor_id, patient_id, 'medication:write')
    med = await conn.fetchrow('SELECT * FROM medications WHERE id=$1::uuid AND user_id=$2 FOR UPDATE', str(medication_id), patient_id)
    if not med:
        fail(404, 'MEDICATION_NOT_FOUND', 'Medicamento não encontrado.')
    if med['schedule_revision'] != payload.expected_revision:
        fail(409, 'SCHEDULE_CHANGED', 'O cadastro mudou. Consulte a programação atual.')
    slots = sorted(payload.slots, key=lambda s: s.local_time)
    revision = med['schedule_revision'] + 1
    # Trigger pode incrementar a mesma revisão se os horários mudaram.
    await conn.execute('''UPDATE medications SET schedule_time=$2,schedule_times=$3,
        schedule_revision=$4,updated_at=now() WHERE id=$1::uuid''', str(medication_id), slots[0].local_time,
        ','.join(s.local_time for s in slots), revision)
    for slot in slots:
        await conn.execute('''INSERT INTO medication_dose_schedules(medication_id,local_time,dose_units,quantity_confirmed,active)
            VALUES($1::uuid,$2,$3,$4,true) ON CONFLICT(medication_id,local_time) DO UPDATE
            SET dose_units=EXCLUDED.dose_units,quantity_confirmed=EXCLUDED.quantity_confirmed,active=true''',
            str(medication_id), slot.local_time, slot.dose_units, slot.quantity_confirmed)
    await conn.execute('''INSERT INTO care_audit_trail(patient_id,actor_id,actor_name,action_type,details)
        SELECT $1::varchar,$2::varchar,COALESCE(full_name,'Usuário autorizado'),'DOSE_SCHEDULE_UPDATED',$3::jsonb FROM profiles WHERE id=$2::varchar''',
        patient_id, actor_id, json.dumps({'medication_id': str(medication_id), 'schedule_revision': revision,
                                        'slots': [s.model_dump() for s in slots]}))
    return {'medication_batch_contract': 1, 'medication_id': str(medication_id), 'schedule_revision': revision,
            'slots': [s.model_dump() for s in slots]}


def batch_response(row):
    if row['result']:
        return decoded(row['result'])
    snap = decoded(row['snapshot'])
    return {'medication_batch_contract': 1, 'batch_id': str(row['id']), 'patient_id': row['patient_id'],
            'actor_id': row['actor_id'], 'channel': row['channel'], 'state': row['state'],
            'prepared_at': utc_text(row['prepared_at']), 'expires_at': utc_text(row['expires_at']),
            'snapshot_digest': row['snapshot_digest'], 'timezone': snap['timezone'],
            'local_date': snap['local_date'], 'local_time': snap['local_time'], 'items': snap['items'],
            'counts': {}, 'requires_action': row['state'] == 'expired'}


async def get_batch(conn, *, actor_id, batch_id, lock=True):
    row = await conn.fetchrow('SELECT * FROM medication_dose_batches WHERE id=$1::uuid AND actor_id=$2' + (' FOR UPDATE' if lock else ''), str(batch_id), actor_id)
    if not row:
        fail(404, 'BATCH_NOT_FOUND', 'Proposta não encontrada.')
    await authorize(conn, actor_id, row['patient_id'], 'medication:take')
    if row['state'] == 'prepared' and row['expires_at'] <= datetime.now(timezone.utc):
        await conn.execute("UPDATE medication_dose_batches SET state='expired' WHERE id=$1 AND state='prepared'", row['id'])
        row = dict(row); row['state'] = 'expired'
    return row


async def prepare_batch(conn, *, actor_id, patient_id, payload):
    await authorize(conn, actor_id, patient_id, 'medication:take')
    # Serializar preparação com mesmo ID antes de consultar a proposta.
    await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', f'{actor_id}/{payload.client_batch_id}')
    request_signature = payload.model_dump(mode='json')
    request_signature['patient_id'] = patient_id
    request_signature['occurrence_ids'] = sorted(request_signature['occurrence_ids'])
    request_digest = digest(request_signature)
    previous = await conn.fetchrow('SELECT * FROM medication_dose_batches WHERE actor_id=$1 AND client_batch_id=$2', actor_id, payload.client_batch_id)
    if previous:
        if previous['request_digest'] != request_digest:
            fail(409, 'IDEMPOTENCY_CONFLICT', 'Identificador reutilizado com conteúdo diferente.')
        return batch_response(await get_batch(conn, actor_id=actor_id, batch_id=previous['id'], lock=True)), False
    # group_id opaco: buscar ocorrência escolhida determinística sem aceitar data arbitrária.
    # Cliente consulta hoje ou outra data; occurrence pode não estar materializada.
    # Materializamos os IDs na consulta REST antes de retornar (materialize_groups).
    first = await conn.fetchrow('SELECT scheduled_at FROM medication_dose_occurrences WHERE id=$1 AND patient_id=$2', payload.occurrence_ids[0], patient_id)
    if not first:
        fail(422, 'INVALID_SELECTION', 'Consulte as doses antes de preparar a proposta.')
    _, zone, _ = await patient_profile(conn, patient_id, lock=True)
    await conn.fetch('SELECT id FROM medications WHERE user_id=$1 ORDER BY id FOR UPDATE', patient_id)
    data = await groups(conn, actor_id=actor_id, patient_id=patient_id, day=first['scheduled_at'].astimezone(zone).date())
    group = next((g for g in data['groups'] if g['group_id'] == str(payload.group_id)), None)
    if group is None or group['schedule_digest'] != payload.expected_schedule_digest:
        fail(409, 'SCHEDULE_CHANGED', 'O grupo mudou. Consulte novamente.')
    ids = {str(i) for i in payload.occurrence_ids}
    items = [i for i in group['items'] if i['occurrence_id'] in ids]
    if len(items) != len(ids):
        fail(422, 'INVALID_SELECTION', 'Seleção fora do grupo apresentado.')
    blocked = [i for i in items if i['state'] == 'blocked' or i['dose_units'] is None]
    if blocked:
        fail(409, 'REVIEW_REQUIRED', 'Revise os itens antes de preparar a proposta.', items=blocked)
    snapshot = {'patient_id': patient_id, 'actor_id': actor_id, 'timezone': data['timezone'],
                'local_date': data['local_date'], 'local_time': group['local_time'], 'items': items}
    row = await conn.fetchrow('''INSERT INTO medication_dose_batches
        (patient_id,actor_id,client_batch_id,channel,group_id,request_digest,schedule_digest,snapshot_digest,snapshot)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb) RETURNING *''', patient_id, actor_id,
        payload.client_batch_id, payload.channel, payload.group_id, request_digest,
        group['schedule_digest'], digest(snapshot), json.dumps(snapshot))
    for item in items:
        await conn.execute('INSERT INTO medication_dose_batch_items(batch_id,occurrence_id,snapshot) VALUES($1,$2::uuid,$3::jsonb)', row['id'], item['occurrence_id'], json.dumps(item))
    return batch_response(row), True


async def materialize_groups(conn, *, actor_id, patient_id, day=None):
    data = await groups(conn, actor_id=actor_id, patient_id=patient_id, day=day)
    zone = ZoneInfo(data['timezone'])
    for group in data['groups']:
        for item in group['items']:
            await reserve_occurrence(conn, patient_id=patient_id, medication_id=item['medication_id'],
                                     instant=datetime.fromisoformat(item['scheduled_at']), zone=zone)
    return data


async def confirm_batch(conn, *, actor_id, batch_id, payload):
    row = await get_batch(conn, actor_id=actor_id, batch_id=batch_id, lock=True)
    confirmation = payload.model_dump(mode='json')
    confirmation['selected_occurrence_ids'] = sorted(confirmation['selected_occurrence_ids'])
    if confirmation['declared_taken_at']:
        confirmation['declared_taken_at'] = utc_text(payload.declared_taken_at)
    confirmation_digest = digest(confirmation)
    if row['result']:
        if row['confirmation_digest'] != confirmation_digest:
            fail(409, 'IDEMPOTENCY_CONFLICT', 'A confirmação concluída não pode mudar.')
        return batch_response(row)
    if row['state'] == 'expired':
        fail(409, 'PROPOSAL_EXPIRED', 'A proposta expirou. Apresente uma nova proposta.')
    if row['state'] != 'prepared':
        fail(409, 'BATCH_ALREADY_RESOLVED', 'Proposta encerrada.')
    if payload.snapshot_digest != row['snapshot_digest']:
        fail(409, 'SNAPSHOT_MISMATCH', 'A proposta confirmada não corresponde à apresentada.')
    if payload.declared_taken_at and payload.declared_taken_at > datetime.now(timezone.utc):
        fail(422, 'INVALID_DECLARED_TIME', 'O horário declarado não pode ser futuro.')
    snap = decoded(row['snapshot'])
    selected = {str(i) for i in payload.selected_occurrence_ids}
    if not selected or not selected <= {i['occurrence_id'] for i in snap['items']}:
        fail(422, 'INVALID_SELECTION', 'A seleção deve pertencer à proposta.')
    await patient_profile(conn, row['patient_id'], lock=True)
    # Ordem global preservada entre lotes de diferentes horários/cuidadores.
    await conn.fetch('SELECT id FROM medications WHERE user_id=$1 ORDER BY id FOR UPDATE', row['patient_id'])
    data = await groups(conn, actor_id=actor_id, patient_id=row['patient_id'], day=date.fromisoformat(snap['local_date']))
    current = next((g for g in data['groups'] if g['group_id'] == str(row['group_id'])), None)
    if current is None or current['schedule_digest'] != row['schedule_digest'] or data['timezone'] != snap['timezone']:
        fail(409, 'SCHEDULE_CHANGED', 'O cadastro mudou. Apresente nova proposta antes de confirmar.')
    from services.medication_take import take_medication
    items = []
    for item in snap['items']:
        result = dict(item)
        if item['occurrence_id'] not in selected:
            result['status'] = 'excluded'
        else:
            try:
                async with conn.transaction():  # savepoint só para conflitos de negócio
                    outcome = await take_medication(conn, medication_id=item['medication_id'], actor_id=actor_id,
                        scheduled_for=datetime.fromisoformat(item['scheduled_at']), units_taken=item['dose_units'],
                        idempotency_key=f"batch:{row['id']}:{item['medication_id']}",
                        declared_taken_at=payload.declared_taken_at, channel=row['channel'])
                    result.update({k: v for k, v in outcome.items() if k != 'patient_id'})
                    if outcome.get('recorded_units') not in (None, item['dose_units']):
                        result.update(status='blocked', reason_code='ALREADY_RECORDED_DIFFERENT_QUANTITY')
                    elif outcome.get('historical_conflict'):
                        result.update(status='blocked', reason_code='HISTORICAL_CONFLICT')
                    result.pop('historical_conflict', None)
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                code = exc.detail.get('code') if isinstance(exc.detail, dict) else 'INSUFFICIENT_STOCK'
                result.update(status='blocked', reason_code=code)
        items.append(result)
    counts = {s: sum(i['status'] == s for i in items) for s in ('recorded', 'already_recorded', 'excluded', 'blocked')}
    resolved = counts['recorded'] + counts['already_recorded']
    state = 'confirmed' if counts['blocked'] == 0 else ('partially_confirmed' if resolved else 'failed')
    response = batch_response(row)
    response.update(state=state, items=items, counts=counts, requires_action=counts['blocked'] > 0)
    await conn.execute('''UPDATE medication_dose_batches SET state=$2,confirmation_id=$3,
        confirmation_digest=$4,result=$5::jsonb WHERE id=$1''', row['id'], state,
        payload.confirmation_id, confirmation_digest, json.dumps(response))
    for item in items:
        await conn.execute('UPDATE medication_dose_batch_items SET selected=$3,result=$4::jsonb WHERE batch_id=$1 AND occurrence_id=$2::uuid',
            row['id'], item['occurrence_id'], item['occurrence_id'] in selected, json.dumps(item))
    await conn.execute('''INSERT INTO care_audit_trail(patient_id,actor_id,actor_name,action_type,details)
        SELECT $1::varchar,$2::varchar,COALESCE(full_name,'Usuário autorizado'),'MEDICATION_DOSE_BATCH_CONFIRMED',$3::jsonb FROM profiles WHERE id=$2::varchar''',
        row['patient_id'], actor_id, json.dumps({'batch_id': str(row['id']), 'counts': counts, 'channel': row['channel']}))
    return response


async def cancel_batch(conn, *, actor_id, batch_id):
    row = await get_batch(conn, actor_id=actor_id, batch_id=batch_id, lock=True)
    if row['state'] != 'prepared':
        fail(409, 'BATCH_ALREADY_RESOLVED', 'Só propostas preparadas podem ser canceladas.')
    await conn.execute("UPDATE medication_dose_batches SET state='cancelled' WHERE id=$1", row['id'])
    row = dict(row); row['state'] = 'cancelled'
    return batch_response(row)
