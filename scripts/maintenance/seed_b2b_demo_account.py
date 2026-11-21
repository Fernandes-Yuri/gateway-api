#!/usr/bin/env python3
"""B2B-31: seed manual das contas reais; nunca importar no startup.

Recebe DATABASE_URL e FIREBASE_SERVICE_ACCOUNT_PATH do runtime da EC2.
--check faz somente preflight; --reset limpa apenas o programa TEST-B2B1.
Não cria nem modifica identidades Firebase, senhas ou perfis dos pacientes.
"""

import argparse
import asyncio
import json
import os
import sys
from uuid import NAMESPACE_URL, uuid5

OWNER_EMAIL = "testeb2b@teste.com"
PATIENT_EMAILS = ("testebraga1@proton.me", "testebraga2@proton.me")
PROJECT_ID = "braga-saude"
INVITE_CODE = "TEST-B2B1"
LOCK_ID = 310031
METRICS = ["steps", "hydration", "medications", "authorized_records"]
FEATURES = (
    "assistant_chat_enabled", "direct_partner_chat_enabled", "ai_receptionist_enabled",
    "family_link_enabled", "partner_reminders_enabled", "appointments_enabled",
    "pre_consultation_pdf_enabled",
)
EXPERIENCE = {
    "schema_version": 1,
    "template_key": "CLINICA_GERAL",
    "welcome_headline": "Horizonte Cuida",
    "welcome_message": "Espaço de teste para acompanhar sua rotina e conversar com a equipe.",
    "enabled_metrics": METRICS,
    "features": dict.fromkeys(FEATURES, True),
    "direct_chat_policy": {
        "service_hours_label": "Seg a Sex, 08h às 18h",
        "response_sla_label": "Resposta em até 1 dia útil",
        "allow_off_hours_queue": True,
    },
    "ai_receptionist_policy": {
        "persona_name": "Recepção Horizonte",
        "activation_mode": "ALWAYS_FIRST",
        "greeting_message": "Olá! Posso ajudar com horários e agendamentos deste programa de teste.",
        "partner_faq_knowledge": (
            "Este é um programa de teste, sem atendimento clínico real. "
            "Horário: segunda a sexta, das 08h às 18h. "
            "Agendamento: solicite pela aba Agenda; a equipe confirma o horário. "
            "Modalidades: presencial ou remota. "
            "Para falar com a equipe, peça atendimento humano no chat. "
            "A recepção virtual responde apenas dúvidas administrativas."
        ),
        "auto_escalate_to_human_on_unknown": True,
    },
    "appointments_policy": {
        "allowed_modalities": ["IN_PERSON", "REMOTE"],
        "booking_mode": "REQUEST_CONFIRMATION",
        "availabilities_summary": "Segunda a sexta, 08h às 18h (horários de teste)",
        "external_booking_url": None,
    },
}


class SeedError(RuntimeError):
    """Erro operacional seguro para exibição, sem resposta bruta de provedores."""


def resolve_accounts():
    """Consulta exclusivamente as três identidades existentes no projeto esperado."""
    import firebase_admin
    from firebase_admin import auth, credentials

    try:
        app = firebase_admin.get_app()
    except ValueError:
        path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH")
        if not path:
            raise SeedError("FIREBASE_SERVICE_ACCOUNT_PATH ausente no runtime.") from None
        app = firebase_admin.initialize_app(credentials.Certificate(path))
    if app.project_id != PROJECT_ID or os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
        raise SeedError("Projeto Firebase incorreto ou emulador ativo.")
    accounts = {}
    for email in (OWNER_EMAIL, *PATIENT_EMAILS):
        try:
            user = auth.get_user_by_email(email, app=app)
        except auth.UserNotFoundError:
            raise SeedError(f"Conta existente não encontrada: {email}.") from None
        if user.disabled or (user.email or "").lower() != email or not user.uid:
            raise SeedError(f"Identidade indisponível ou divergente: {email}.")
        accounts[email] = user.uid
    if len(set(accounts.values())) != 3:
        raise SeedError("As três identidades devem ser distintas.")
    return accounts


def record_id(program_id, patient_id, kind):
    return uuid5(NAMESPACE_URL, f"braga/b2b-31/{program_id}/{patient_id}/{kind}")


async def preflight(conn, accounts, *, reset=False):
    """Bloqueia colisão de convite/dono e ausência de perfis antes de escrever."""
    owner = accounts[OWNER_EMAIL]
    program = await conn.fetchrow(
        "SELECT id, owner_user_id, invite_code, closed_at FROM partner_programs "
        "WHERE invite_code=$1 FOR UPDATE", INVITE_CODE,
    )
    if program and program["owner_user_id"] != owner:
        raise SeedError("TEST-B2B1 pertence a outro titular; nenhuma alteração realizada.")
    owned = await conn.fetchrow(
        "SELECT id, invite_code, closed_at FROM partner_programs WHERE owner_user_id=$1 FOR UPDATE", owner,
    )
    if owned and owned["invite_code"] not in (None, INVITE_CODE):
        raise SeedError("Titular possui outro programa; não será convertido automaticamente.")
    if not reset and owned and owned["closed_at"] is not None:
        raise SeedError("Programa encerrado; não será reaberto automaticamente.")
    if not reset:
        for email in PATIENT_EMAILS:
            profile = await conn.fetchrow("SELECT to_jsonb(p) AS data FROM profiles p WHERE id=$1", accounts[email])
            if not profile:
                raise SeedError(f"Perfil PostgreSQL ausente: {email}; abra o app primeiro.")
            data = json.loads(profile["data"])
            if data.get("paused_at") or data.get("account_mode") == "BUSINESS_ONLY":
                raise SeedError(f"Perfil de paciente indisponível: {email}.")
    return program or owned


async def reset_program(conn, program_id):
    # Mensagens não possuem FK para threads; recibos têm CASCADE por reminder_id.
    counts = {}
    for table in (
        "program_direct_messages", "program_direct_threads", "program_appointments",
        "program_reminders", "program_enrollments",
    ):
        result = await conn.execute(f"DELETE FROM {table} WHERE program_id=$1", program_id)
        counts[table] = int(result.split()[-1])
    return counts


async def supplement_metrics(conn, patient_id):
    """Complemento mínimo marcado como demo; não sobrescreve medições existentes.

    Apenas passos e hidratação, se cada série estiver vazia nos últimos 7 dias.
    Não inventa pressão, glicemia, peso/altura nem medicamentos. Biometrics e
    sinais clínicos existentes continuam disponíveis no drill-down autorizado.
    """
    counts = {"daily_metrics": 0, "vital_signs": 0}
    recent_steps = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM daily_metrics WHERE user_id=$1 AND date>=CURRENT_DATE-7)", patient_id,
    )
    if not recent_steps:
        for days, steps in ((3, 3200), (2, 4100), (1, 3800)):
            result = await conn.execute(
                "INSERT INTO daily_metrics(id,user_id,date,steps) VALUES($1,$2,CURRENT_DATE-$3::int,$4) "
                "ON CONFLICT DO NOTHING",
                record_id(INVITE_CODE, patient_id, f"demo-steps-{days}"), patient_id, days, steps,
            )
            counts["daily_metrics"] += int(result.split()[-1])
    # Mesmo lock utilizado pelo sync de hidratação: serializa o teste de ausência.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,8042))", patient_id)
    recent_water = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM vital_signs WHERE user_id=$1 AND deleted_at IS NULL "
        "AND hydration_ml>0 AND measured_at>=now()-interval '7 days')", patient_id,
    )
    if not recent_water:
        for days in (3, 2, 1):
            result = await conn.execute(
                "INSERT INTO vital_signs(id,user_id,hydration_ml,measured_at,confirmed_via) "
                "VALUES($1,$2,250,now()-make_interval(days=>$3),'b2b_demo') ON CONFLICT DO NOTHING",
                record_id(INVITE_CODE, patient_id, f"demo-water-{days}"), patient_id, days,
            )
            counts["vital_signs"] += int(result.split()[-1])
    return counts


async def seed_participant(conn, program_id, owner, patient, *, full, family_share):
    # O seed representa o aceite do app novo (migration 094): sem a versao
    # corrente o resumo do responsavel nao libera medications/authorized_records
    # para as contas de demonstracao.
    await conn.execute(
        "INSERT INTO program_enrollments(program_id,patient_user_id,shared_categories,"
        "family_share_enabled,consent_version) "
        "VALUES($1,$2,$3::jsonb,$4,'program-routine-v1') ON CONFLICT(program_id,patient_user_id) DO UPDATE SET "
        "status='ACTIVE',shared_categories=EXCLUDED.shared_categories,"
        "family_share_enabled=EXCLUDED.family_share_enabled,"
        "consent_version=EXCLUDED.consent_version,"
        "revoked_at=NULL,updated_at=now()",
        program_id, patient, json.dumps(METRICS if full else METRICS[:2]), family_share,
    )
    await conn.execute(
        "INSERT INTO program_appointments(id,program_id,patient_user_id,modality,requested_slot_text,"
        "scheduled_at,patient_note,status) VALUES($1,$2,$3,$4,$5,"
        "CASE WHEN $6='CONFIRMED' THEN now()+interval '7 days' ELSE NULL END,$7,$6) "
        "ON CONFLICT(id) DO NOTHING",
        record_id(program_id, patient, "appointment"), program_id, patient,
        "IN_PERSON" if full else "REMOTE", "Próxima semana, período da manhã (teste)",
        "PENDING" if full else "CONFIRMED", "Agendamento demonstrativo B2B-31.",
    )
    reminder_id = record_id(program_id, patient, "reminder")
    await conn.execute(
        "INSERT INTO program_reminders(id,program_id,target_patient_id,reminder_type,title,body,created_by) "
        "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(id) DO NOTHING",
        reminder_id, program_id, patient, "ROUTINE" if full else "HYDRATION",
        "Rotina do programa de teste" if full else "Registro de hidratação (teste)",
        "Lembrete demonstrativo para validar a leitura no aplicativo.", owner,
    )
    # Escopo confirmado: ambos lidos. Neste contrato recibo equivale a leitura.
    await conn.execute(
        "INSERT INTO program_reminder_receipts(reminder_id,patient_user_id,acknowledged_by_uid) "
        "VALUES($1,$2,$2) ON CONFLICT DO NOTHING", reminder_id, patient,
    )
    inserted = await conn.fetchval(
        "INSERT INTO program_direct_threads(program_id,patient_user_id,handling_mode,handoff_note,ai_turns_today) "
        "VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING RETURNING true",
        program_id, patient, "WAITING_HUMAN" if full else "AI_ACTIVE",
        "Solicitação demonstrativa de atendimento humano." if full else None, 0 if full else 1,
    )
    # Reruns preservam conversas já utilizadas nos aparelhos, inclusive após retenção.
    if inserted:
        messages = [("PATIENT", patient, "Olá! Quero falar com a equipe (teste)." if full
                     else "Olá! Qual é o horário de atendimento deste programa de teste?")]
        if not full:
            messages.append(("AI_RECEPTIONIST", owner,
                             "Olá! O horário de teste é de segunda a sexta, das 08h às 18h. "
                             "Você pode solicitar um horário pela Agenda."))
        for index, (role, sender, body) in enumerate(messages):
            await conn.execute(
                "INSERT INTO program_direct_messages(id,program_id,patient_user_id,sender_user_id,"
                "sender_role,body,created_at) VALUES($1,$2,$3,$4,$5,$6,"
                "now()-interval '2 minutes'+make_interval(secs=>$7)) ON CONFLICT DO NOTHING",
                record_id(program_id, patient, f"message-{index}"), program_id, patient,
                sender, role, body, float(index),
            )


async def run(conn, accounts, *, reset=False, check=False):
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='5s'")
        await conn.execute("SET LOCAL statement_timeout='30s'")
        await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_ID)
        existing = await preflight(conn, accounts, reset=reset)
        if check:
            return {"mode": "check", "accounts_resolved": 3, "patient_profiles": 2,
                    "program_exists": existing is not None}
        owner = accounts[OWNER_EMAIL]
        if reset:
            # Nunca usa apenas owner: o reset precisa do convite exato TEST-B2B1.
            if not existing or existing["invite_code"] != INVITE_CODE:
                return {"mode": "reset", "program_exists": False, "deleted": {}}
            counts = await reset_program(conn, existing["id"])
            await audit(conn, existing["id"], owner, "DEMO_RESET", counts)
            return {"mode": "reset", "program_exists": True, "deleted": counts}
        await conn.execute(
            "INSERT INTO profiles(id,full_name,user_role,account_mode) VALUES($1,'Horizonte Teste',NULL,"
            "'BUSINESS_ONLY') ON CONFLICT(id) DO UPDATE SET full_name=EXCLUDED.full_name,account_mode='BUSINESS_ONLY',"
            "user_role=NULL,caregiver_mode=NULL,basic_profile_complete=false,self_care_complete=false,"
            "self_care_setup_pending=false,updated_at=now()", owner,
        )
        program_id = await conn.fetchval(
            "INSERT INTO partner_programs(owner_user_id,program_name,company_name,primary_color,logo_url,"
            "approval_status,invite_code,seat_limit,template_key,experience_config,review_note,reviewed_at) "
            "VALUES($1,'Horizonte Cuida','Horizonte','#00695C',"
            "NULL,'APPROVED',$2,50,'CLINICA_GERAL',$3::jsonb,"
            "NULL,now()) "
            "ON CONFLICT(owner_user_id) DO UPDATE SET program_name=EXCLUDED.program_name,"
            "company_name=EXCLUDED.company_name,primary_color=EXCLUDED.primary_color,"
            "logo_url=CASE WHEN partner_programs.logo_url='https://bragasaude.online/images/logo.png' "
            "THEN NULL ELSE partner_programs.logo_url END,approval_status='APPROVED',"
            "invite_code=EXCLUDED.invite_code,seat_limit=50,template_key='CLINICA_GERAL',"
            "experience_config=EXCLUDED.experience_config,invite_paused=false,ai_paused=false,"
            "review_note=EXCLUDED.review_note,"
            "reviewed_at=now(),updated_at=now() RETURNING id",
            owner, INVITE_CODE, json.dumps(EXPERIENCE, ensure_ascii=False),
        )
        # Cidade/UF do programa demo: só grava quando a coluna existe (compatível
        # com schemas de teste que não aplicam a migration 091b).
        has_city_column = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'partner_programs' "
            "AND column_name = 'city')"
        )
        if has_city_column:
            await conn.execute(
                "UPDATE partner_programs SET city = 'São Paulo', state = 'SP', updated_at = now() WHERE id = $1",
                program_id,
            )
        first, second = (accounts[email] for email in PATIENT_EMAILS)
        family_share = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM family_bindings WHERE status='ACTIVE' "
            "AND patient_id=$1 AND caregiver_id=$2)", first, second,
        )
        await seed_participant(conn, program_id, owner, first, full=True, family_share=family_share)
        await seed_participant(conn, program_id, owner, second, full=False, family_share=False)
        metrics = await supplement_metrics(conn, first)
        summary = {"mode": "seed", "program_id": str(program_id), "participants": 2,
                   "family_share_enabled": family_share, "metrics_added": metrics}
        await audit(conn, program_id, owner, "DEMO_SEEDED", summary)
        return summary


async def audit(conn, program_id, owner, action, metadata):
    await conn.execute(
        "INSERT INTO program_audit_logs(program_id,actor_user_id,action,metadata) VALUES($1,$2,$3,$4::jsonb)",
        program_id, owner, action, json.dumps(metadata),
    )


async def execute(args):
    import asyncpg

    if not os.environ.get("DATABASE_URL"):
        raise SeedError("DATABASE_URL ausente no runtime.")
    accounts = resolve_accounts()
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=15, command_timeout=30)
    try:
        return await run(conn, accounts, reset=args.reset, check=args.check)
    finally:
        await conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reset", action="store_true", help="Remove somente registros B2B de TEST-B2B1; não recria.")
    mode.add_argument("--check", action="store_true", help="Valida identidades e perfis sem gravar dados.")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(asyncio.run(execute(args)), ensure_ascii=False))
        return 0
    except SeedError as exc:
        print(str(exc), file=sys.stderr)
    except Exception as exc:
        # Nunca imprime DSN, respostas Firebase, SQL com dados ou traceback.
        print(f"B2B-31 falhou ({type(exc).__name__}); transação revertida se iniciada.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
