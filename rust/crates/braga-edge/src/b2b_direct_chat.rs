//! Motor de Chat Direto Assíncrono B2B em Rust (`braga-edge`) — B2B-20.
//!
//! Opera exclusivamente sobre `program_direct_threads` e `program_direct_messages` (migracao 092),
//! mantendo isolamento total do `braga-chat` Go (`family_messages` / `is_go_chat_route`),
//! aplicando rate limit de 10 msgs/min, Zero File Egress, controle `allow_off_hours_queue`,
//! marcacao explicita `read_at` e Push FCM zero-PHI via `PARTNER_DIRECT_MESSAGE`.

use std::sync::Arc;

use axum::{
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use chrono::{DateTime, Datelike, FixedOffset, Timelike, Utc, Weekday};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::{PgPool, Postgres, Row, Transaction};
use uuid::Uuid;

use crate::b2b::{
    guard_program_module, json_error, parse_experience_config_json, record_audit, require_pool,
    ApprovalStatus, DirectChatPolicyDto, ProgramModule,
};
use crate::b2b_engagement::sanitize_non_https_urls;
use crate::{authenticate_request, AppState};

pub const MAX_DIRECT_MESSAGES_PER_MINUTE: i64 = 10;
pub const MAX_DIRECT_MESSAGE_CHARS: usize = 1000;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ListDirectMessagesQuery {
    pub cursor: Option<String>,
    pub limit: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SendDirectMessageRequest {
    pub body: String,
    #[serde(default)]
    pub has_attachment: bool,
    pub attachment_url: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DirectThreadSummaryDto {
    pub program_id: String,
    pub patient_user_id: String,
    pub handling_mode: String,
    pub taken_over_by: Option<String>,
    pub handoff_note: Option<String>,
    pub updated_at: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DirectChatMessageDto {
    pub message_id: String,
    pub program_id: String,
    pub patient_user_id: String,
    pub sender_user_id: String,
    pub sender_role: String,
    pub body: String,
    pub created_at: String,
    pub read_at: Option<String>,
}

pub(crate) fn parse_direct_cursor_and_limit(
    raw_cursor: Option<&str>,
    raw_limit: Option<i64>,
) -> (Option<DateTime<Utc>>, Option<Uuid>, i64) {
    let limit = raw_limit.unwrap_or(30).clamp(1, 50);
    let Some(clean) = raw_cursor.map(str::trim).filter(|s| !s.is_empty()) else {
        return (None, None, limit);
    };
    if let Some((ts_part, id_part)) = clean.split_once('|') {
        let dt = DateTime::parse_from_rfc3339(ts_part.trim())
            .ok()
            .map(|d| d.with_timezone(&Utc));
        let msg_id = Uuid::parse_str(id_part.trim()).ok();
        return (dt, msg_id, limit);
    }
    let dt = DateTime::parse_from_rfc3339(clean)
        .ok()
        .map(|d| d.with_timezone(&Utc));
    (dt, None, limit)
}

pub(crate) fn validate_direct_message_payload(
    req: &SendDirectMessageRequest,
) -> Result<String, (StatusCode, &'static str)> {
    if req.has_attachment
        || req
            .attachment_url
            .as_deref()
            .is_some_and(|u| !u.trim().is_empty())
    {
        return Err((
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "Anexos e arquivos não são permitidos no chat direto (política Zero File Egress).",
        ));
    }

    let lower_raw = req.body.to_ascii_lowercase();
    if lower_raw.contains("data:image/")
        || lower_raw.contains("data:application/")
        || lower_raw.contains("data:text/html")
    {
        return Err((
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "Conteúdo embutido de mídia/arquivo não é permitido neste canal de texto.",
        ));
    }

    let cleaned = sanitize_non_https_urls(&req.body);
    let count = cleaned.chars().count();
    if count == 0 || count > MAX_DIRECT_MESSAGE_CHARS {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "A mensagem deve conter entre 1 e 1000 caracteres.",
        ));
    }

    Ok(cleaned)
}

/// Avalia se o instante UTC informado cai fora da janela de atendimento configurada (horario de Brasilia UTC-3).
pub(crate) fn evaluate_is_off_hours(policy: &DirectChatPolicyDto, now_utc: DateTime<Utc>) -> bool {
    let Some(brt_offset) = FixedOffset::west_opt(3 * 3600) else {
        return false;
    };
    let local_brt = now_utc.with_timezone(&brt_offset);
    let label_lower = policy.service_hours_label.to_ascii_lowercase();

    let weekdays_only = label_lower.contains("seg a sex");
    if weekdays_only && matches!(local_brt.weekday(), Weekday::Sat | Weekday::Sun) {
        return true;
    }

    let mut extracted_hours: Vec<u32> = Vec::new();
    let mut digits = String::new();
    for ch in label_lower.chars() {
        if ch.is_ascii_digit() {
            digits.push(ch);
        } else {
            if !digits.is_empty() && (ch == 'h' || ch == ':') {
                if let Ok(h) = digits.parse::<u32>() {
                    if h <= 23 {
                        extracted_hours.push(h);
                    }
                }
            }
            digits.clear();
        }
    }

    if extracted_hours.len() >= 2 {
        let start_h = extracted_hours[0];
        let end_h = extracted_hours[1];
        let current_hour = local_brt.hour();
        if start_h < end_h {
            return current_hour < start_h || current_hour >= end_h;
        }
    }
    false
}

pub(crate) fn map_direct_message_row(row: &sqlx::postgres::PgRow) -> DirectChatMessageDto {
    let created_at: DateTime<Utc> = row.get("created_at");
    let read_at: Option<DateTime<Utc>> = row.try_get("read_at").ok().flatten();
    DirectChatMessageDto {
        message_id: row.get::<String, _>("message_id"),
        program_id: row.get::<String, _>("program_id"),
        patient_user_id: row.get::<String, _>("patient_user_id"),
        sender_user_id: row.get::<String, _>("sender_user_id"),
        sender_role: row.get("sender_role"),
        body: row.get("body"),
        created_at: created_at.to_rfc3339(),
        read_at: read_at.map(|dt| dt.to_rfc3339()),
    }
}

pub(crate) async fn emit_direct_message_notification(
    tx: &mut Transaction<'_, Postgres>,
    recipient_uid: &str,
    patient_uid: &str,
    program_id: Uuid,
    message_id: Uuid,
    actor_uid: &str,
) {
    let event_key = format!("b2b_dm:{message_id}:{recipient_uid}");
    let group_key = format!("b2b_dm_thread:{program_id}:{patient_uid}");
    let _ = sqlx::query(
        "SELECT notification_emit($1, $2, $3, 'PARTNER_DIRECT_MESSAGE', 'CARE', 'b2b_direct_thread', $4, $5, $6, NOW(), NOW() + interval '14 days', '{}'::jsonb, true)",
    )
    .bind(recipient_uid)
    .bind(&event_key)
    .bind(&group_key)
    .bind(program_id.to_string())
    .bind(patient_uid)
    .bind(actor_uid)
    .execute(&mut **tx)
    .await;
}

pub(crate) async fn load_thread_summary(
    pool: &PgPool,
    program_id: Uuid,
    patient_uid: &str,
) -> DirectThreadSummaryDto {
    if let Ok(Some(row)) = sqlx::query(
        "SELECT handling_mode, taken_over_by, handoff_note, updated_at \
           FROM program_direct_threads \
          WHERE program_id = $1 AND patient_user_id = $2",
    )
    .bind(program_id)
    .bind(patient_uid)
    .fetch_optional(pool)
    .await
    {
        let updated: DateTime<Utc> = row.get("updated_at");
        return DirectThreadSummaryDto {
            program_id: program_id.to_string(),
            patient_user_id: patient_uid.to_string(),
            handling_mode: row.get("handling_mode"),
            taken_over_by: row
                .try_get::<Option<String>, _>("taken_over_by")
                .ok()
                .flatten(),
            handoff_note: row
                .try_get::<Option<String>, _>("handoff_note")
                .ok()
                .flatten(),
            updated_at: updated.to_rfc3339(),
        };
    }

    DirectThreadSummaryDto {
        program_id: program_id.to_string(),
        patient_user_id: patient_uid.to_string(),
        handling_mode: "AI_ACTIVE".to_string(),
        taken_over_by: None,
        handoff_note: None,
        updated_at: Utc::now().to_rfc3339(),
    }
}

async fn query_direct_messages_page(
    pool: &PgPool,
    program_id: Uuid,
    patient_uid: &str,
    query: &ListDirectMessagesQuery,
) -> Result<(Vec<DirectChatMessageDto>, Option<String>, bool), sqlx::Error> {
    let (cursor_dt, cursor_id, limit) =
        parse_direct_cursor_and_limit(query.cursor.as_deref(), query.limit);
    let fetch_limit = limit + 1;

    let rows = sqlx::query(include_str!("../sql/direct_messages_page.sql"))
        .bind(program_id)
        .bind(patient_uid)
        .bind(cursor_dt)
        .bind(cursor_id)
        .bind(fetch_limit)
        .fetch_all(pool)
        .await?;

    let has_more = rows.len() > limit as usize;
    let slice_len = if has_more { limit as usize } else { rows.len() };
    let mut messages = Vec::with_capacity(slice_len);
    let mut next_cursor: Option<String> = None;

    for row in rows.into_iter().take(slice_len) {
        let dto = map_direct_message_row(&row);
        if has_more {
            next_cursor = Some(format!("{}|{}", dto.created_at, dto.message_id));
        }
        messages.push(dto);
    }

    Ok((messages, next_cursor, has_more))
}

pub(crate) async fn fetch_enrolled_program_for_chat(
    pool: &PgPool,
    program_id: Uuid,
    patient_uid: &str,
) -> Result<
    (
        String,
        String,
        String,
        crate::b2b::ProgramExperienceConfigDto,
    ),
    (StatusCode, &'static str),
> {
    let row = sqlx::query(
        "SELECT pr.owner_user_id, pr.program_name, pr.company_name, pr.approval_status, \
                pr.closed_at, pr.template_key, pr.experience_config, e.status AS enrollment_status \
           FROM partner_programs pr \
           LEFT JOIN program_enrollments e \
             ON e.program_id = pr.id AND e.patient_user_id = $2 \
          WHERE pr.id = $1",
    )
    .bind(program_id)
    .bind(patient_uid)
    .fetch_optional(pool)
    .await
    .map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao consultar programa.",
        )
    })?
    // Programa de outro tenant e programa inexistente respondem o mesmo 403:
    // nao ha oraculo de existencia de UUID de programa.
    .ok_or((
        StatusCode::FORBIDDEN,
        "Vínculo ativo necessário para acessar o chat direto.",
    ))?;

    let enroll_status: Option<String> = row.try_get("enrollment_status").ok().flatten();
    if enroll_status.as_deref() != Some("ACTIVE") {
        return Err((
            StatusCode::FORBIDDEN,
            "Vínculo ativo necessário para acessar o chat direto.",
        ));
    }

    let owner_uid: String = row.get("owner_user_id");
    let program_name: String = row.get("program_name");
    let company_name: String = row.get("company_name");
    let raw_status: String = row.get("approval_status");
    let approval = ApprovalStatus::parse(&raw_status).unwrap_or(ApprovalStatus::Draft);
    let closed_at: Option<DateTime<Utc>> = row.try_get("closed_at").ok().flatten();
    let tpl_raw: Option<String> = row.try_get("template_key").ok().flatten();
    let cfg_raw: Option<Value> = row.try_get("experience_config").ok().flatten();
    let (_, config) = parse_experience_config_json(tpl_raw.as_deref(), cfg_raw.as_ref());

    guard_program_module(
        approval,
        closed_at.is_some(),
        &config,
        ProgramModule::DirectPartnerChat,
    )?;
    Ok((owner_uid, program_name, company_name, config))
}

pub(crate) async fn fetch_owner_program_and_participant_for_chat(
    pool: &PgPool,
    owner_uid: &str,
    patient_uid: &str,
) -> Result<
    (Uuid, String, String, crate::b2b::ProgramExperienceConfigDto),
    (StatusCode, &'static str),
> {
    let row = sqlx::query(
        "SELECT pr.id, pr.program_name, pr.company_name, pr.approval_status, pr.closed_at, \
                pr.template_key, pr.experience_config \
           FROM partner_programs pr \
          WHERE pr.owner_user_id = $1 AND pr.closed_at IS NULL",
    )
    .bind(owner_uid)
    .fetch_optional(pool)
    .await
    .map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao consultar programa.",
        )
    })?
    .ok_or((
        StatusCode::FORBIDDEN,
        "Nenhum programa parceiro ativo encontrado.",
    ))?;

    let program_id: Uuid = row.get("id");
    let program_name: String = row.get("program_name");
    let company_name: String = row.get("company_name");
    let raw_status: String = row.get("approval_status");
    let approval = ApprovalStatus::parse(&raw_status).unwrap_or(ApprovalStatus::Draft);
    let closed_at: Option<DateTime<Utc>> = row.try_get("closed_at").ok().flatten();
    let tpl_raw: Option<String> = row.try_get("template_key").ok().flatten();
    let cfg_raw: Option<Value> = row.try_get("experience_config").ok().flatten();
    let (_, config) = parse_experience_config_json(tpl_raw.as_deref(), cfg_raw.as_ref());

    guard_program_module(
        approval,
        closed_at.is_some(),
        &config,
        ProgramModule::DirectPartnerChat,
    )?;

    let active_enroll: Option<String> = sqlx::query_scalar(
        "SELECT status FROM program_enrollments \
          WHERE program_id = $1 AND patient_user_id = $2",
    )
    .bind(program_id)
    .bind(patient_uid)
    .fetch_optional(pool)
    .await
    .map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao validar participante.",
        )
    })?;

    if active_enroll.as_deref() != Some("ACTIVE") {
        return Err((
            StatusCode::FORBIDDEN,
            "Participante não possui vínculo ativo neste programa.",
        ));
    }

    Ok((program_id, program_name, company_name, config))
}

struct InsertDirectMessageParams<'a> {
    program_id: Uuid,
    patient_uid: &'a str,
    sender_uid: &'a str,
    sender_role: &'a str,
    recipient_uid: &'a str,
    initial_thread_mode: &'a str,
    body: String,
}

async fn is_program_ai_paused(pool: &PgPool, program_id: Uuid) -> bool {
    sqlx::query_scalar::<_, bool>(
        "SELECT COALESCE(ai_paused, false) FROM partner_programs WHERE id = $1",
    )
    .bind(program_id)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten()
    .unwrap_or(false)
}

async fn insert_direct_message_tx(
    pool: &PgPool,
    params: InsertDirectMessageParams<'_>,
) -> Result<DirectChatMessageDto, (StatusCode, &'static str)> {
    let mut tx = pool.begin().await.map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao iniciar transação de mensagem.",
        )
    })?;

    // Garante existencia da thread e serializa rajadas simultaneas do mesmo participante.
    let next_mode_sql = if params.sender_role == "PARTNER" {
        "CASE WHEN program_direct_threads.handling_mode = 'WAITING_HUMAN' THEN 'HUMAN_ACTIVE' ELSE program_direct_threads.handling_mode END"
    } else {
        "program_direct_threads.handling_mode"
    };
    let upsert_sql = format!(
        "INSERT INTO program_direct_threads (program_id, patient_user_id, handling_mode) \
         VALUES ($1, $2, $3) \
         ON CONFLICT (program_id, patient_user_id) DO UPDATE \
            SET handling_mode = {next_mode_sql}, updated_at = NOW()"
    );
    sqlx::query(&upsert_sql)
        .bind(params.program_id)
        .bind(params.patient_uid)
        .bind(params.initial_thread_mode)
        .execute(&mut *tx)
        .await
        .map_err(|_| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao preparar conversa.",
            )
        })?;

    let recent_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM program_direct_messages \
          WHERE program_id = $1 \
            AND sender_user_id = $2 \
            AND created_at > NOW() - interval '60 seconds'",
    )
    .bind(params.program_id)
    .bind(params.sender_uid)
    .fetch_one(&mut *tx)
    .await
    .unwrap_or(0);

    if recent_count >= MAX_DIRECT_MESSAGES_PER_MINUTE {
        return Err((
            StatusCode::TOO_MANY_REQUESTS,
            "Limite de 10 mensagens por minuto atingido neste canal.",
        ));
    }

    let inserted = sqlx::query(
        "INSERT INTO program_direct_messages \
            (program_id, patient_user_id, sender_user_id, sender_role, body) \
         VALUES ($1, $2, $3, $4, $5) \
         RETURNING id, id::text AS message_id, program_id::text AS program_id, \
                   patient_user_id, sender_user_id, sender_role, body, created_at, read_at",
    )
    .bind(params.program_id)
    .bind(params.patient_uid)
    .bind(params.sender_uid)
    .bind(params.sender_role)
    .bind(&params.body)
    .fetch_one(&mut *tx)
    .await
    .map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao gravar mensagem.",
        )
    })?;

    let msg_uuid: Uuid = inserted.get("id");
    emit_direct_message_notification(
        &mut tx,
        params.recipient_uid,
        params.patient_uid,
        params.program_id,
        msg_uuid,
        params.sender_uid,
    )
    .await;

    let _ = record_audit(
        &mut *tx,
        Some(params.program_id),
        params.sender_uid,
        params.patient_uid,
        "PARTNER_DIRECT_MESSAGE_SENT",
        json!({
            "messageId": msg_uuid.to_string(),
            "senderRole": params.sender_role,
        }),
    )
    .await;

    tx.commit().await.map_err(|_| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar envio da mensagem.",
        )
    })?;

    Ok(map_direct_message_row(&inserted))
}

/// Participante lista historico cursor-paginado do Chat Direto (`GET /api/b2b/enrollments/:program_id/messages`).
pub async fn list_enrollment_direct_messages_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id_raw): Path<String>,
    Query(query): Query<ListDirectMessagesQuery>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        "/api/b2b/enrollments/messages",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let Ok(program_id) = Uuid::parse_str(program_id_raw.trim()) else {
        return json_error(
            StatusCode::BAD_REQUEST,
            "Identificador de programa inválido.",
        );
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let (_, program_name, company_name, config) =
        match fetch_enrolled_program_for_chat(pool, program_id, &identity.uid).await {
            Ok(v) => v,
            Err((code, msg)) => return json_error(code, msg),
        };

    let (messages, next_cursor, has_more) =
        match query_direct_messages_page(pool, program_id, &identity.uid, &query).await {
            Ok(v) => v,
            Err(_) => {
                return json_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Erro ao carregar mensagens do programa.",
                )
            }
        };

    let thread = load_thread_summary(pool, program_id, &identity.uid).await;
    let is_off_hours_now = evaluate_is_off_hours(&config.direct_chat_policy, Utc::now());
    let ai_paused = is_program_ai_paused(pool, program_id).await;

    (
        StatusCode::OK,
        Json(json!({
            "programId": program_id.to_string(),
            "programName": program_name,
            "companyName": company_name,
            "assistantChatEnabled": config.features.assistant_chat_enabled,
            "aiReceptionistEnabled": config.features.ai_receptionist_enabled,
            "aiPaused": ai_paused,
            "directChatPolicy": config.direct_chat_policy,
            "aiReceptionistPolicy": config.ai_receptionist_policy,
            "isOffHoursNow": is_off_hours_now,
            "thread": thread,
            "messages": messages,
            "nextCursor": next_cursor,
            "hasMore": has_more
        })),
    )
        .into_response()
}

/// Participante envia mensagem ao chat direto do programa (`POST /api/b2b/enrollments/:program_id/messages`).
pub async fn send_enrollment_direct_message_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id_raw): Path<String>,
    Json(req): Json<SendDirectMessageRequest>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "POST", "/api/b2b/enrollments/messages").await
        {
            Ok(id) => id,
            Err(resp) => return resp,
        };
    let Ok(program_id) = Uuid::parse_str(program_id_raw.trim()) else {
        return json_error(
            StatusCode::BAD_REQUEST,
            "Identificador de programa inválido.",
        );
    };
    let body = match validate_direct_message_payload(&req) {
        Ok(b) => b,
        Err((code, msg)) => return json_error(code, msg),
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let (owner_uid, program_name, company_name, config) =
        match fetch_enrolled_program_for_chat(pool, program_id, &identity.uid).await {
            Ok(v) => v,
            Err((code, msg)) => return json_error(code, msg),
        };

    let is_off_hours = evaluate_is_off_hours(&config.direct_chat_policy, Utc::now());
    if is_off_hours && !config.direct_chat_policy.allow_off_hours_queue {
        return json_error(
            StatusCode::CONFLICT,
            "O canal está fora do horário de atendimento e não aceita fila no momento.",
        );
    }

    let initial_mode = if !config.features.ai_receptionist_enabled
        || config.ai_receptionist_policy.activation_mode == "MANUAL_ONLY"
    {
        "HUMAN_ACTIVE"
    } else {
        "AI_ACTIVE"
    };

    let dto = match insert_direct_message_tx(
        pool,
        InsertDirectMessageParams {
            program_id,
            patient_uid: &identity.uid,
            sender_uid: &identity.uid,
            sender_role: "PATIENT",
            recipient_uid: &owner_uid,
            initial_thread_mode: initial_mode,
            body,
        },
    )
    .await
    {
        Ok(d) => d,
        Err((code, msg)) => return json_error(code, msg),
    };

    let ai_turn_req = crate::b2b_ai_receptionist::AiTurnRequest {
        program_id,
        owner_uid: &owner_uid,
        program_name: &program_name,
        company_name: &company_name,
        patient_uid: &identity.uid,
        config: &config,
        is_off_hours_now: is_off_hours,
    };
    let ai_outcome =
        crate::b2b_ai_receptionist::maybe_generate_and_persist_ai_reply(pool, &ai_turn_req).await;
    let thread = load_thread_summary(pool, program_id, &identity.uid).await;

    (
        StatusCode::CREATED,
        Json(json!({
            "message": dto,
            "sentOffHours": is_off_hours,
            "aiReply": ai_outcome.ai_reply,
            "aiFallbackReason": ai_outcome.fallback_reason,
            "thread": thread
        })),
    )
        .into_response()
}

/// Participante marca como lidas as respostas do parceiro/IA (`POST /api/b2b/enrollments/:program_id/messages/read`).
pub async fn mark_enrollment_direct_messages_read_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id_raw): Path<String>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/enrollments/messages/read",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let Ok(program_id) = Uuid::parse_str(program_id_raw.trim()) else {
        return json_error(
            StatusCode::BAD_REQUEST,
            "Identificador de programa inválido.",
        );
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    if let Err((code, msg)) = fetch_enrolled_program_for_chat(pool, program_id, &identity.uid).await
    {
        return json_error(code, msg);
    }

    let updated = sqlx::query(
        "UPDATE program_direct_messages \
            SET read_at = NOW() \
          WHERE program_id = $1 \
            AND patient_user_id = $2 \
            AND sender_role IN ('PARTNER', 'AI_RECEPTIONIST') \
            AND read_at IS NULL",
    )
    .bind(program_id)
    .bind(&identity.uid)
    .execute(pool)
    .await
    .map(|r| r.rows_affected())
    .unwrap_or(0);

    (
        StatusCode::OK,
        Json(json!({
            "programId": program_id.to_string(),
            "markedReadCount": updated
        })),
    )
        .into_response()
}

/// Responsavel lista historico cursor-paginado de conversa com participante (`GET /api/b2b/program/me/participants/:patient_id/messages`).
pub async fn list_owner_direct_messages_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(patient_id_raw): Path<String>,
    Query(query): Query<ListDirectMessagesQuery>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        "/api/b2b/program/me/participants/messages",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let patient_uid = patient_id_raw.trim();
    if patient_uid.is_empty() {
        return json_error(StatusCode::BAD_REQUEST, "Participante inválido.");
    }
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let (program_id, program_name, company_name, config) =
        match fetch_owner_program_and_participant_for_chat(pool, &identity.uid, patient_uid).await {
            Ok(v) => v,
            Err((code, msg)) => return json_error(code, msg),
        };

    let (messages, next_cursor, has_more) =
        match query_direct_messages_page(pool, program_id, patient_uid, &query).await {
            Ok(v) => v,
            Err(_) => {
                return json_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Erro ao carregar conversa com participante.",
                )
            }
        };

    let thread = load_thread_summary(pool, program_id, patient_uid).await;
    let ai_paused = is_program_ai_paused(pool, program_id).await;

    (
        StatusCode::OK,
        Json(json!({
            "programId": program_id.to_string(),
            "programName": program_name,
            "companyName": company_name,
            "aiReceptionistEnabled": config.features.ai_receptionist_enabled,
            "aiPaused": ai_paused,
            "directChatPolicy": config.direct_chat_policy,
            "aiReceptionistPolicy": config.ai_receptionist_policy,
            "thread": thread,
            "messages": messages,
            "nextCursor": next_cursor,
            "hasMore": has_more
        })),
    )
        .into_response()
}

/// Responsavel envia resposta direta ao participante (`POST /api/b2b/program/me/participants/:patient_id/messages`).
pub async fn send_owner_direct_message_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(patient_id_raw): Path<String>,
    Json(req): Json<SendDirectMessageRequest>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/program/me/participants/messages",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let patient_uid = patient_id_raw.trim();
    if patient_uid.is_empty() {
        return json_error(StatusCode::BAD_REQUEST, "Participante inválido.");
    }
    let body = match validate_direct_message_payload(&req) {
        Ok(b) => b,
        Err((code, msg)) => return json_error(code, msg),
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let (program_id, _, _, _) = match fetch_owner_program_and_participant_for_chat(
        pool,
        &identity.uid,
        patient_uid,
    )
    .await
    {
        Ok(v) => v,
        Err((code, msg)) => return json_error(code, msg),
    };

    match insert_direct_message_tx(
        pool,
        InsertDirectMessageParams {
            program_id,
            patient_uid,
            sender_uid: &identity.uid,
            sender_role: "PARTNER",
            recipient_uid: patient_uid,
            initial_thread_mode: "HUMAN_ACTIVE",
            body,
        },
    )
    .await
    {
        Ok(dto) => (StatusCode::CREATED, Json(json!({ "message": dto }))).into_response(),
        Err((code, msg)) => json_error(code, msg),
    }
}

/// Responsavel marca mensagens recebidas do participante como lidas (`POST /api/b2b/program/me/participants/:patient_id/messages/read`).
pub async fn mark_owner_direct_messages_read_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(patient_id_raw): Path<String>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/program/me/participants/messages/read",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let patient_uid = patient_id_raw.trim();
    if patient_uid.is_empty() {
        return json_error(StatusCode::BAD_REQUEST, "Participante inválido.");
    }
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let (program_id, _, _, _) = match fetch_owner_program_and_participant_for_chat(
        pool,
        &identity.uid,
        patient_uid,
    )
    .await
    {
        Ok(v) => v,
        Err((code, msg)) => return json_error(code, msg),
    };

    let updated = sqlx::query(
        "UPDATE program_direct_messages \
            SET read_at = NOW() \
          WHERE program_id = $1 \
            AND patient_user_id = $2 \
            AND sender_role = 'PATIENT' \
            AND read_at IS NULL",
    )
    .bind(program_id)
    .bind(patient_uid)
    .execute(pool)
    .await
    .map(|r| r.rows_affected())
    .unwrap_or(0);

    (
        StatusCode::OK,
        Json(json!({
            "programId": program_id.to_string(),
            "patientUserId": patient_uid,
            "markedReadCount": updated
        })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn direct_message_blocks_attachments_and_data_uris_and_sanitizes_http() {
        let with_attach = SendDirectMessageRequest {
            body: "Segue meu exame".to_string(),
            has_attachment: true,
            attachment_url: None,
        };
        assert_eq!(
            validate_direct_message_payload(&with_attach).unwrap_err().0,
            StatusCode::UNSUPPORTED_MEDIA_TYPE
        );

        let with_data_uri = SendDirectMessageRequest {
            body: "Veja data:image/png;base64,AAAA".to_string(),
            has_attachment: false,
            attachment_url: None,
        };
        assert_eq!(
            validate_direct_message_payload(&with_data_uri)
                .unwrap_err()
                .0,
            StatusCode::UNSUPPORTED_MEDIA_TYPE
        );

        let clean = SendDirectMessageRequest {
            body: "Bom dia, link http://inseguro.test mas https://bragasaude.online ok".to_string(),
            has_attachment: false,
            attachment_url: None,
        };
        let sanitized = validate_direct_message_payload(&clean).expect("Texto deve passar");
        assert!(sanitized.contains("[link removido]"));
        assert!(sanitized.contains("https://bragasaude.online"));
    }

    #[test]
    fn off_hours_evaluator_respects_brt_weekday_window() {
        let policy = DirectChatPolicyDto {
            service_hours_label: "Seg a Sex, 08h às 18h".to_string(),
            response_sla_label: "Resposta em até 1 dia útil".to_string(),
            allow_off_hours_queue: false,
        };
        // Quinta 14:00 BRT = 17:00 UTC -> dentro do expediente
        let in_hours = Utc.with_ymd_and_hms(2026, 11, 19, 17, 0, 0).unwrap();
        assert!(!evaluate_is_off_hours(&policy, in_hours));
        // Quinta 21:00 BRT = 2026-11-20 00:00 UTC -> fora do expediente
        let after_hours = Utc.with_ymd_and_hms(2026, 11, 20, 0, 0, 0).unwrap();
        assert!(evaluate_is_off_hours(&policy, after_hours));
    }

    #[test]
    fn direct_cursor_parses_composite_and_clamps_limit() {
        let id = Uuid::new_v4();
        let raw = format!("2026-11-19T19:00:00Z|{id}");
        let (dt, parsed_id, lim) = parse_direct_cursor_and_limit(Some(&raw), Some(200));
        assert!(dt.is_some());
        assert_eq!(parsed_id, Some(id));
        assert_eq!(lim, 50);
    }
}
