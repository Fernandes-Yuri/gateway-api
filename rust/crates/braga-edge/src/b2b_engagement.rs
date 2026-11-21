//! Motor de engajamento B2B in-app em Rust (`braga-edge`):
//! - B2B-18: Disparo de lembretes e orientacoes de rotina pelo parceiro + Push FCM (082)
//!   com bloqueio Nao-SaMD de posologia, cotas anti-spam e zero escrita em `medications`.

use std::sync::Arc;
use std::time::Duration;

use axum::{
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::{PgPool, Postgres, Row, Transaction};
use tracing::{info, warn};
use uuid::Uuid;

use crate::b2b::{
    guard_program_module, json_error, parse_categories_json, parse_experience_config_json,
    record_audit, require_pool, require_service_token, ApprovalStatus, ProgramModule,
    ALLOWED_SHARED_CATEGORIES,
};
use crate::{authenticate_request, AppState};

pub const ALLOWED_REMINDER_TYPES: &[&str] = &["ROUTINE", "HYDRATION", "ACTIVITY", "PRE_CONSULT"];
pub const MAX_REMINDERS_PER_PATIENT_PER_DAY: i64 = 3;
pub const MAX_BATCH_REMINDERS_PER_PROGRAM_PER_DAY: i64 = 20;

const UNSAFE_POSOLOGY_TOKENS: &[&str] = &[
    "comprimido",
    "comprimidos",
    "capsula",
    "capsulas",
    "posologia",
    "dosagem",
    "dose",
    "doses",
    "prescreva",
    "prescrever",
    "prescrevo",
    "prescricao",
    "receitar",
    "medicamento",
    "medicamentos",
    "remedio",
    "remedios",
    "miligrama",
    "miligramas",
    "gota",
    "gotas",
    "antibiotico",
    "antibioticos",
    "analgesico",
    "analgesicos",
    "antiinflamatorio",
    "losartana",
    "metformina",
    "insulina",
    "omeprazol",
    "dipirona",
    "paracetamol",
    "amoxicilina",
    "ibuprofeno",
    "atenolol",
];

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateProgramReminderRequest {
    pub target_patient_id: Option<String>,
    pub target_category: Option<String>,
    pub reminder_type: Option<String>,
    pub title: String,
    pub body: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ListOwnerRemindersQuery {
    pub limit: Option<i64>,
    pub patient_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ListEnrollmentRemindersQuery {
    pub limit: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MuteProgramRequest {
    pub muted: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProgramReminderDto {
    pub reminder_id: String,
    pub program_id: String,
    pub target_patient_id: Option<String>,
    pub target_patient_name: Option<String>,
    pub target_category: Option<String>,
    pub reminder_type: String,
    pub title: String,
    pub body: String,
    pub created_at: String,
    pub acknowledged_count: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recipient_count: Option<i64>,
    pub acknowledged_at: Option<String>,
    pub acknowledged_by_uid: Option<String>,
}

/// Dobra acentos PT-BR para ASCII em minusculas, evitando bypass de guardrails Nao-SaMD.
pub(crate) fn fold_ascii_pt_br(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for ch in raw.chars() {
        let lower = ch.to_lowercase().next().unwrap_or(ch);
        let folded = match lower {
            'á' | 'à' | 'ã' | 'â' | 'ä' => 'a',
            'é' | 'è' | 'ê' | 'ë' => 'e',
            'í' | 'ì' | 'î' | 'ï' => 'i',
            'ó' | 'ò' | 'õ' | 'ô' | 'ö' => 'o',
            'ú' | 'ù' | 'û' | 'ü' => 'u',
            'ç' => 'c',
            other => other,
        };
        out.push(folded);
    }
    out
}

/// Remove URLs com esquema diferente de `https://` preservando o restante da orientacao.
pub(crate) fn sanitize_non_https_urls(raw: &str) -> String {
    let mut cleaned_words = Vec::new();
    for word in raw.split_whitespace() {
        let lower = word.to_ascii_lowercase();
        let unsafe_scheme = lower.starts_with("http://")
            || lower.starts_with("javascript:")
            || lower.starts_with("data:")
            || lower.starts_with("vbscript:")
            || lower.starts_with("file://");
        if unsafe_scheme {
            cleaned_words.push("[link removido]");
        } else {
            cleaned_words.push(word);
        }
    }
    cleaned_words.join(" ").trim().to_string()
}

/// Detecta termos posologicos/prescritivos (reutilizado no B2B-18 e no Output Guard B2B-20B).
pub(crate) fn contains_unsafe_posology_text(raw: &str) -> bool {
    let folded = fold_ascii_pt_br(raw);
    if folded.contains("anti inflamatorio") || folded.contains("anti-inflamatorio") {
        return true;
    }

    let tokens: Vec<String> = folded
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|s| !s.is_empty())
        .map(ToString::to_string)
        .collect();

    for (idx, tok) in tokens.iter().enumerate() {
        if UNSAFE_POSOLOGY_TOKENS.contains(&tok.as_str()) {
            return true;
        }
        // Formas coladas como "50mg" ou "25mcg"
        if (tok.ends_with("mg") || tok.ends_with("mcg"))
            && tok
                .trim_end_matches("mcg")
                .trim_end_matches("mg")
                .chars()
                .all(|c| c.is_ascii_digit())
            && tok.chars().next().is_some_and(|c| c.is_ascii_digit())
        {
            return true;
        }
        // Formas separadas como "50 mg" ou "25 mcg" (sem bloquear "300 ml" de hidratacao)
        if (tok == "mg" || tok == "mcg")
            && idx > 0
            && tokens[idx - 1].chars().all(|c| c.is_ascii_digit())
        {
            return true;
        }
    }
    false
}

pub fn validate_and_sanitize_reminder_payload(
    req: &CreateProgramReminderRequest,
) -> Result<NormalizedReminderInput, (StatusCode, &'static str)> {
    let r_type = req
        .reminder_type
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("ROUTINE")
        .to_ascii_uppercase();

    if r_type == "MEDICATION" {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "Orientação de medicação não é permitida neste canal.",
        ));
    }
    if !ALLOWED_REMINDER_TYPES.contains(&r_type.as_str()) {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "Tipo de lembrete inválido. Use ROUTINE, HYDRATION, ACTIVITY ou PRE_CONSULT.",
        ));
    }

    let target_category = match req
        .target_category
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        None => None,
        Some(cat) => {
            let lower = cat.to_ascii_lowercase();
            if !ALLOWED_SHARED_CATEGORIES.contains(&lower.as_str()) {
                return Err((
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "Categoria alvo inválida para filtragem de lembrete.",
                ));
            }
            Some(lower)
        }
    };

    let clean_title = sanitize_non_https_urls(&req.title);
    let clean_body = sanitize_non_https_urls(&req.body);
    if clean_title.is_empty() || clean_title.chars().count() > 140 {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "Título do lembrete deve ter entre 1 e 140 caracteres.",
        ));
    }
    if clean_body.is_empty() || clean_body.chars().count() > 500 {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "Mensagem do lembrete deve ter entre 1 e 500 caracteres.",
        ));
    }

    if contains_unsafe_posology_text(&clean_title) || contains_unsafe_posology_text(&clean_body) {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "Orientação de medicação não é permitida neste canal.",
        ));
    }

    Ok(NormalizedReminderInput {
        target_patient_id: req
            .target_patient_id
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(ToString::to_string),
        target_category,
        reminder_type: r_type,
        title: clean_title,
        body: clean_body,
    })
}

pub struct NormalizedReminderInput {
    pub target_patient_id: Option<String>,
    pub target_category: Option<String>,
    pub reminder_type: String,
    pub title: String,
    pub body: String,
}

async fn emit_partner_reminder_notification(
    tx: &mut Transaction<'_, Postgres>,
    recipient_uid: &str,
    patient_uid: &str,
    reminder_id: Uuid,
    actor_uid: &str,
) {
    let event_key = format!("b2b_reminder:{reminder_id}:{recipient_uid}");
    let group_key = format!("b2b_reminder:{reminder_id}");
    let _ = sqlx::query(
        "SELECT notification_emit($1, $2, $3, 'PARTNER_REMINDER', 'CARE', 'b2b_reminder', $4, $5, $6, NOW(), NOW() + interval '30 days', '{}'::jsonb, true)",
    )
    .bind(recipient_uid)
    .bind(&event_key)
    .bind(&group_key)
    .bind(reminder_id.to_string())
    .bind(patient_uid)
    .bind(actor_uid)
    .execute(&mut **tx)
    .await;
}

/// Dispara lembrete individual ou em lote pelo empresário (`POST /api/b2b/program/me/reminders`).
pub async fn create_program_reminder_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<CreateProgramReminderRequest>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "POST", "/api/b2b/program/me/reminders").await
        {
            Ok(id) => id,
            Err(resp) => return resp,
        };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let input = match validate_and_sanitize_reminder_payload(&req) {
        Ok(v) => v,
        Err((code, msg)) => return json_error(code, msg),
    };

    let prog_row = match sqlx::query(
        "SELECT id, approval_status, closed_at, template_key, experience_config \
           FROM partner_programs \
          WHERE owner_user_id = $1 AND closed_at IS NULL",
    )
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(r)) => r,
        Ok(None) => {
            return json_error(
                StatusCode::FORBIDDEN,
                "Nenhum programa parceiro ativo encontrado.",
            )
        }
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao carregar programa.",
            )
        }
    };

    let program_id: Uuid = prog_row.get("id");
    let raw_status: String = prog_row.get("approval_status");
    let status = ApprovalStatus::parse(&raw_status).unwrap_or(ApprovalStatus::Draft);
    let closed_at: Option<DateTime<Utc>> = prog_row.try_get("closed_at").ok().flatten();
    let tpl_raw: Option<String> = prog_row.try_get("template_key").ok().flatten();
    let cfg_raw: Option<Value> = prog_row.try_get("experience_config").ok().flatten();
    let (_, config) = parse_experience_config_json(tpl_raw.as_deref(), cfg_raw.as_ref());

    if let Err((code, msg)) = guard_program_module(
        status,
        closed_at.is_some(),
        &config,
        ProgramModule::PartnerReminders,
    ) {
        return json_error(code, msg);
    }

    let mut tx = match pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao iniciar transação.",
            )
        }
    };

    let mut push_targets: Vec<(String, bool, bool)> = Vec::new();
    let mut target_patient_name: Option<String> = None;

    if let Some(ref target_pid) = input.target_patient_id {
        let enroll_row = match sqlx::query(
            "SELECT e.status, e.shared_categories, e.muted_at, \
                    COALESCE(e.family_share_enabled, false) AS family_share_enabled, \
                    pr.full_name \
               FROM program_enrollments e \
               LEFT JOIN profiles pr ON pr.id = e.patient_user_id \
              WHERE e.program_id = $1 AND e.patient_user_id = $2 \
              FOR UPDATE",
        )
        .bind(program_id)
        .bind(target_pid)
        .fetch_optional(&mut *tx)
        .await
        {
            Ok(Some(r)) => r,
            Ok(None) => {
                return json_error(
                    StatusCode::FORBIDDEN,
                    "Participante não pertence a este programa.",
                )
            }
            Err(_) => {
                return json_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Erro ao validar participante.",
                )
            }
        };

        let enroll_status: String = enroll_row.get("status");
        if enroll_status != "ACTIVE" {
            return json_error(
                StatusCode::FORBIDDEN,
                "Participante sem vínculo ativo neste programa.",
            );
        }
        if let Some(ref required_cat) = input.target_category {
            let shared_val: Value = enroll_row
                .try_get("shared_categories")
                .unwrap_or_else(|_| json!([]));
            let shared = parse_categories_json(&shared_val);
            if !shared.iter().any(|c| c == required_cat) {
                return json_error(
                    StatusCode::CONFLICT,
                    "O participante ainda não autorizou esta categoria neste programa.",
                );
            }
        }
        let sent_today: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) \
               FROM program_reminders \
              WHERE program_id = $1 \
                AND target_patient_id = $2 \
                AND created_at >= CURRENT_DATE",
        )
        .bind(program_id)
        .bind(target_pid)
        .fetch_one(&mut *tx)
        .await
        .unwrap_or(0);
        if sent_today >= MAX_REMINDERS_PER_PATIENT_PER_DAY {
            return json_error(
                StatusCode::TOO_MANY_REQUESTS,
                "Limite diário de 3 lembretes individuais para este participante atingido.",
            );
        }

        let muted_at: Option<DateTime<Utc>> = enroll_row.try_get("muted_at").ok().flatten();
        let fam_share: bool = enroll_row.try_get("family_share_enabled").unwrap_or(false);
        target_patient_name = enroll_row
            .try_get::<Option<String>, _>("full_name")
            .ok()
            .flatten()
            .filter(|s| !s.trim().is_empty());
        push_targets.push((target_pid.clone(), muted_at.is_none(), fam_share));
    } else {
        let batch_today: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) \
               FROM program_reminders \
              WHERE program_id = $1 \
                AND target_patient_id IS NULL \
                AND created_at >= CURRENT_DATE",
        )
        .bind(program_id)
        .fetch_one(&mut *tx)
        .await
        .unwrap_or(0);
        if batch_today >= MAX_BATCH_REMINDERS_PER_PROGRAM_PER_DAY {
            return json_error(
                StatusCode::TOO_MANY_REQUESTS,
                "Limite diário de 20 disparos em lote para este programa atingido.",
            );
        }

        let filter_cat_json = input.target_category.as_ref().map(|c| json!([c]));
        let rows = match sqlx::query(
            "SELECT patient_user_id, muted_at, \
                    COALESCE(family_share_enabled, false) AS family_share_enabled \
               FROM program_enrollments \
              WHERE program_id = $1 \
                AND status = 'ACTIVE' \
                AND ($2::jsonb IS NULL OR shared_categories @> $2::jsonb)",
        )
        .bind(program_id)
        .bind(filter_cat_json)
        .fetch_all(&mut *tx)
        .await
        {
            Ok(r) => r,
            Err(_) => {
                return json_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Erro ao consultar carteira.",
                )
            }
        };

        if rows.is_empty() {
            return json_error(
                StatusCode::CONFLICT,
                "Nenhum participante ativo elegível para receber este lembrete.",
            );
        }
        for row in rows {
            let pid: String = row.get("patient_user_id");
            let muted_at: Option<DateTime<Utc>> = row.try_get("muted_at").ok().flatten();
            let fam_share: bool = row.try_get("family_share_enabled").unwrap_or(false);
            push_targets.push((pid, muted_at.is_none(), fam_share));
        }
    }

    let inserted = match sqlx::query(
        "INSERT INTO program_reminders \
            (program_id, target_patient_id, target_category, reminder_type, title, body, created_by) \
         VALUES ($1, $2, $3, $4, $5, $6, $7) \
         RETURNING id, created_at",
    )
    .bind(program_id)
    .bind(&input.target_patient_id)
    .bind(&input.target_category)
    .bind(&input.reminder_type)
    .bind(&input.title)
    .bind(&input.body)
    .bind(&identity.uid)
    .fetch_one(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao salvar lembrete do programa.",
            )
        }
    };

    let reminder_id: Uuid = inserted.get("id");
    let created_at: DateTime<Utc> = inserted.get("created_at");

    for (patient_uid, can_push, fam_share) in &push_targets {
        if *can_push {
            emit_partner_reminder_notification(
                &mut tx,
                patient_uid,
                patient_uid,
                reminder_id,
                &identity.uid,
            )
            .await;

            if config.features.family_link_enabled && *fam_share {
                if let Ok(caregivers) = sqlx::query_scalar::<_, String>(
                    "SELECT caregiver_id \
                       FROM family_bindings \
                      WHERE patient_id = $1 AND status = 'ACTIVE'",
                )
                .bind(patient_uid)
                .fetch_all(&mut *tx)
                .await
                {
                    for caregiver_uid in caregivers {
                        emit_partner_reminder_notification(
                            &mut tx,
                            &caregiver_uid,
                            patient_uid,
                            reminder_id,
                            &identity.uid,
                        )
                        .await;
                    }
                }
            }
        }
    }

    let audit_action = if input.target_patient_id.is_some() {
        "PARTNER_REMINDER_SENT"
    } else {
        "PARTNER_REMINDER_BATCH_SENT"
    };
    let _ = record_audit(
        &mut *tx,
        Some(program_id),
        &identity.uid,
        input.target_patient_id.as_deref().unwrap_or(""),
        audit_action,
        json!({
            "reminderId": reminder_id.to_string(),
            "reminderType": input.reminder_type,
            "targetCategory": input.target_category,
            "recipientCount": push_targets.len()
        }),
    )
    .await;

    if tx.commit().await.is_err() {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar disparo.",
        );
    }

    let dto = ProgramReminderDto {
        reminder_id: reminder_id.to_string(),
        program_id: program_id.to_string(),
        target_patient_id: input.target_patient_id,
        target_patient_name,
        target_category: input.target_category,
        reminder_type: input.reminder_type,
        title: input.title,
        body: input.body,
        created_at: created_at.to_rfc3339(),
        acknowledged_count: 0,
        recipient_count: Some(push_targets.len() as i64),
        acknowledged_at: None,
        acknowledged_by_uid: None,
    };

    (
        StatusCode::CREATED,
        Json(json!({
            "reminder": dto,
            "recipientCount": push_targets.len()
        })),
    )
        .into_response()
}

/// Lista lembretes emitidos pelo empresario com contagem agregada de leitura (`GET /api/b2b/program/me/reminders`).
pub async fn list_owner_reminders_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<ListOwnerRemindersQuery>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        "/api/b2b/program/me/reminders",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let program_id: Option<Uuid> = match sqlx::query_scalar(
        "SELECT id FROM partner_programs WHERE owner_user_id = $1 AND closed_at IS NULL",
    )
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(id) => id,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao buscar programa.",
            )
        }
    };
    let Some(program_id) = program_id else {
        return json_error(
            StatusCode::FORBIDDEN,
            "Nenhum programa parceiro encontrado.",
        );
    };

    let limit = query.limit.unwrap_or(20).clamp(1, 50);
    let filter_patient = query
        .patient_id
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty());

    let rows = match sqlx::query(
        "SELECT r.id::text AS reminder_id, \
                r.program_id::text AS program_id, \
                r.target_patient_id, \
                r.target_category, \
                r.reminder_type, \
                r.title, \
                r.body, \
                r.created_at, \
                CASE WHEN r.target_patient_id IS NOT NULL THEN 1 ELSE ( \
                    SELECT (log.metadata->>'recipientCount')::bigint FROM program_audit_logs log \
                    WHERE log.program_id = r.program_id AND log.action = 'PARTNER_REMINDER_BATCH_SENT' \
                      AND log.metadata->>'reminderId' = r.id::text \
                    ORDER BY log.id DESC LIMIT 1) END AS recipient_count, \
                COALESCE(COUNT(rc.patient_user_id), 0) AS acknowledged_count \
           FROM program_reminders r \
           LEFT JOIN program_reminder_receipts rc ON rc.reminder_id = r.id \
          WHERE r.program_id = $1 \
            AND ($2::text IS NULL OR r.target_patient_id IS NULL OR r.target_patient_id = $2) \
          GROUP BY r.id \
          ORDER BY r.created_at DESC \
          LIMIT $3",
    )
    .bind(program_id)
    .bind(filter_patient)
    .bind(limit)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao listar lembretes enviados.",
            )
        }
    };

    let reminders: Vec<ProgramReminderDto> = rows
        .into_iter()
        .map(|row| {
            let created_at: DateTime<Utc> = row.get("created_at");
            ProgramReminderDto {
                reminder_id: row.get("reminder_id"),
                program_id: row.get("program_id"),
                target_patient_id: row.try_get("target_patient_id").ok().flatten(),
                target_patient_name: None,
                target_category: row.try_get("target_category").ok().flatten(),
                reminder_type: row.get("reminder_type"),
                title: row.get("title"),
                body: row.get("body"),
                created_at: created_at.to_rfc3339(),
                recipient_count: row.get("recipient_count"),
                acknowledged_count: row.try_get("acknowledged_count").unwrap_or(0),
                acknowledged_at: None,
                acknowledged_by_uid: None,
            }
        })
        .collect();

    (StatusCode::OK, Json(json!({ "reminders": reminders }))).into_response()
}

pub(crate) fn map_enrollment_reminder_row(row: &sqlx::postgres::PgRow) -> ProgramReminderDto {
    let created_at: DateTime<Utc> = row.get("created_at");
    let ack_at: Option<DateTime<Utc>> = row.try_get("acknowledged_at").ok().flatten();
    ProgramReminderDto {
        reminder_id: row.get("reminder_id"),
        program_id: row.get("program_id"),
        target_patient_id: row.try_get("target_patient_id").ok().flatten(),
        target_patient_name: None,
        target_category: row.try_get("target_category").ok().flatten(),
        reminder_type: row.get("reminder_type"),
        title: row.get("title"),
        body: row.get("body"),
        created_at: created_at.to_rfc3339(),
        recipient_count: None,
        acknowledged_count: if ack_at.is_some() { 1 } else { 0 },
        acknowledged_at: ack_at.map(|d| d.to_rfc3339()),
        acknowledged_by_uid: row.try_get("acknowledged_by_uid").ok().flatten(),
    }
}

/// Participante lista os lembretes recebidos do programa ativo (`GET /api/b2b/enrollments/:program_id/reminders`).
pub async fn list_enrollment_reminders_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id): Path<String>,
    Query(query): Query<ListEnrollmentRemindersQuery>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        "/api/b2b/enrollments/:program_id/reminders",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let prog_uuid = match Uuid::parse_str(program_id.trim()) {
        Ok(u) => u,
        Err(_) => {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Identificador do programa inválido.",
            )
        }
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let ctx_row = match sqlx::query(
        "SELECT pr.program_name, pr.primary_color, pr.approval_status, pr.closed_at, \
                pr.template_key, pr.experience_config, \
                e.status AS enroll_status, e.consented_at, e.shared_categories, e.muted_at \
           FROM program_enrollments e \
           JOIN partner_programs pr ON pr.id = e.program_id \
          WHERE e.program_id = $1 AND e.patient_user_id = $2",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(r)) => r,
        Ok(None) => {
            return json_error(
                StatusCode::FORBIDDEN,
                "Você não possui vínculo neste programa.",
            )
        }
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao validar programa.",
            )
        }
    };

    let enroll_status: String = ctx_row.get("enroll_status");
    if enroll_status != "ACTIVE" {
        return json_error(StatusCode::FORBIDDEN, "Vínculo encerrado ou inativo.");
    }

    let raw_status: String = ctx_row.get("approval_status");
    let status = ApprovalStatus::parse(&raw_status).unwrap_or(ApprovalStatus::Draft);
    let closed_at: Option<DateTime<Utc>> = ctx_row.try_get("closed_at").ok().flatten();
    let tpl_raw: Option<String> = ctx_row.try_get("template_key").ok().flatten();
    let cfg_raw: Option<Value> = ctx_row.try_get("experience_config").ok().flatten();
    let (_, config) = parse_experience_config_json(tpl_raw.as_deref(), cfg_raw.as_ref());

    if let Err((code, msg)) = guard_program_module(
        status,
        closed_at.is_some(),
        &config,
        ProgramModule::PartnerReminders,
    ) {
        return json_error(code, msg);
    }

    let limit = query.limit.unwrap_or(20).clamp(1, 50);
    let consented_at: DateTime<Utc> = ctx_row.get("consented_at");
    let shared_categories_json: Value = ctx_row
        .try_get("shared_categories")
        .unwrap_or_else(|_| json!([]));
    let muted_at: Option<DateTime<Utc>> = ctx_row.try_get("muted_at").ok().flatten();
    let program_name: String = ctx_row.get("program_name");
    let primary_color: String = ctx_row.get("primary_color");

    let rows = match sqlx::query(
        "SELECT r.id::text AS reminder_id, \
                r.program_id::text AS program_id, \
                r.target_patient_id, \
                r.target_category, \
                r.reminder_type, \
                r.title, \
                r.body, \
                r.created_at, \
                rc.acknowledged_at, \
                rc.acknowledged_by_uid \
           FROM program_reminders r \
           LEFT JOIN program_reminder_receipts rc \
             ON rc.reminder_id = r.id AND rc.patient_user_id = $2 \
          WHERE r.program_id = $1 \
            AND r.expires_at > NOW() \
            AND r.created_at >= $3 \
            AND (r.target_patient_id IS NULL OR r.target_patient_id = $2) \
            AND (r.target_category IS NULL OR $4::jsonb @> jsonb_build_array(r.target_category)) \
          ORDER BY r.created_at DESC \
          LIMIT $5",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .bind(consented_at)
    .bind(shared_categories_json)
    .bind(limit)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao listar avisos do programa.",
            )
        }
    };

    let reminders: Vec<ProgramReminderDto> = rows.iter().map(map_enrollment_reminder_row).collect();

    (
        StatusCode::OK,
        Json(json!({
            "programId": prog_uuid.to_string(),
            "programName": program_name,
            "primaryColor": primary_color,
            "muted": muted_at.is_some(),
            "reminders": reminders
        })),
    )
        .into_response()
}

/// Participante confirma leitura do lembrete (`POST /api/b2b/enrollments/:program_id/reminders/:reminder_id/ack`).
pub async fn acknowledge_enrollment_reminder_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((program_id, reminder_id)): Path<(String, String)>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/enrollments/:program_id/reminders/:reminder_id/ack",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let prog_uuid = match Uuid::parse_str(program_id.trim()) {
        Ok(u) => u,
        Err(_) => {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Identificador do programa inválido.",
            )
        }
    };
    let rem_uuid = match Uuid::parse_str(reminder_id.trim()) {
        Ok(u) => u,
        Err(_) => {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Identificador do lembrete inválido.",
            )
        }
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let ctx_row = match sqlx::query(
        "SELECT pr.approval_status, pr.closed_at, pr.template_key, pr.experience_config, e.status \
           FROM program_enrollments e \
           JOIN partner_programs pr ON pr.id = e.program_id \
          WHERE e.program_id = $1 AND e.patient_user_id = $2",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(r)) => r,
        Ok(None) => {
            return json_error(StatusCode::FORBIDDEN, "Vínculo inexistente neste programa.")
        }
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao validar vínculo.",
            )
        }
    };

    let enroll_status: String = ctx_row.get("status");
    if enroll_status != "ACTIVE" {
        return json_error(StatusCode::FORBIDDEN, "Vínculo encerrado ou inativo.");
    }
    let raw_status: String = ctx_row.get("approval_status");
    let status = ApprovalStatus::parse(&raw_status).unwrap_or(ApprovalStatus::Draft);
    let closed_at: Option<DateTime<Utc>> = ctx_row.try_get("closed_at").ok().flatten();
    let tpl_raw: Option<String> = ctx_row.try_get("template_key").ok().flatten();
    let cfg_raw: Option<Value> = ctx_row.try_get("experience_config").ok().flatten();
    let (_, config) = parse_experience_config_json(tpl_raw.as_deref(), cfg_raw.as_ref());

    if let Err((code, msg)) = guard_program_module(
        status,
        closed_at.is_some(),
        &config,
        ProgramModule::PartnerReminders,
    ) {
        return json_error(code, msg);
    }

    let valid_rem = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) \
           FROM program_reminders \
          WHERE id = $1 AND program_id = $2 \
            AND (target_patient_id IS NULL OR target_patient_id = $3)",
    )
    .bind(rem_uuid)
    .bind(prog_uuid)
    .bind(&identity.uid)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    if valid_rem == 0 {
        return json_error(
            StatusCode::NOT_FOUND,
            "Lembrete não encontrado para este participante.",
        );
    }

    let ack_at = match sqlx::query_scalar::<_, DateTime<Utc>>(
        "INSERT INTO program_reminder_receipts (reminder_id, patient_user_id, acknowledged_by_uid) \
         VALUES ($1, $2, $2) \
         ON CONFLICT (reminder_id, patient_user_id) DO UPDATE \
            SET acknowledged_by_uid = COALESCE(program_reminder_receipts.acknowledged_by_uid, EXCLUDED.acknowledged_by_uid) \
         RETURNING acknowledged_at",
    )
    .bind(rem_uuid)
    .bind(&identity.uid)
    .fetch_one(pool)
    .await
    {
        Ok(ts) => ts,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao registrar confirmação de leitura.",
            )
        }
    };

    let _ = sqlx::query(
        "UPDATE notification_sources ns \
            SET read_at = COALESCE(ns.read_at, NOW()), active = false \
           FROM notification_items ni \
          WHERE ns.item_id = ni.id \
            AND ni.recipient_id = $1 \
            AND ns.resource_type = 'b2b_reminder' \
            AND ns.resource_id = $2",
    )
    .bind(&identity.uid)
    .bind(rem_uuid.to_string())
    .execute(pool)
    .await;

    (
        StatusCode::OK,
        Json(json!({
            "reminderId": rem_uuid.to_string(),
            "acknowledgedAt": ack_at.to_rfc3339()
        })),
    )
        .into_response()
}

/// Participante silencia ou reativa alertas Push de um programa sem sair (`POST /api/b2b/enrollments/:program_id/mute`).
pub async fn set_enrollment_mute_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id): Path<String>,
    Json(req): Json<MuteProgramRequest>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/enrollments/:program_id/mute",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let prog_uuid = match Uuid::parse_str(program_id.trim()) {
        Ok(u) => u,
        Err(_) => {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Identificador do programa inválido.",
            )
        }
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let res = match sqlx::query(
        "UPDATE program_enrollments \
            SET muted_at = CASE WHEN $3 THEN COALESCE(muted_at, NOW()) ELSE NULL END, \
                updated_at = NOW() \
          WHERE program_id = $1 AND patient_user_id = $2 AND status = 'ACTIVE'",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .bind(req.muted)
    .execute(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao atualizar preferência de notificações do programa.",
            )
        }
    };
    if res.rows_affected() == 0 {
        return json_error(
            StatusCode::FORBIDDEN,
            "Vínculo ativo não encontrado neste programa.",
        );
    }

    let action = if req.muted {
        "PROGRAM_MUTED"
    } else {
        "PROGRAM_UNMUTED"
    };
    let _ = record_audit(
        pool,
        Some(prog_uuid),
        &identity.uid,
        &identity.uid,
        action,
        json!({ "muted": req.muted }),
    )
    .await;

    (
        StatusCode::OK,
        Json(json!({
            "programId": prog_uuid.to_string(),
            "muted": req.muted
        })),
    )
        .into_response()
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ReportProgramAbuseRequest {
    pub reason: String,
    pub details: Option<String>,
    #[serde(alias = "mute_also")]
    pub mute_also: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AdminAbuseReportsQuery {
    pub status: Option<String>,
    pub limit: Option<i64>,
}

pub(crate) fn validate_abuse_report_payload(
    req: &ReportProgramAbuseRequest,
) -> Result<(String, Option<String>), (StatusCode, &'static str)> {
    let reason = req.reason.trim().to_string();
    if reason.is_empty() || reason.chars().count() > 64 {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "Informe o motivo da denúncia (até 64 caracteres).",
        ));
    }
    let clean_details = req
        .details
        .as_deref()
        .map(sanitize_non_https_urls)
        .filter(|s| !s.is_empty());
    if clean_details
        .as_ref()
        .is_some_and(|d| d.chars().count() > 500)
    {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "A descrição da denúncia deve ter no máximo 500 caracteres.",
        ));
    }
    Ok((reason, clean_details))
}

/// Denuncia UGC de programa parceiro (conformidade Google Play) com silenciamento opcional imediato (`POST /api/b2b/enrollments/:program_id/report`).
pub async fn report_program_abuse_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id): Path<String>,
    Json(req): Json<ReportProgramAbuseRequest>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/enrollments/:program_id/report",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let prog_uuid = match Uuid::parse_str(program_id.trim()) {
        Ok(u) => u,
        Err(_) => {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Identificador do programa inválido.",
            )
        }
    };
    let (reason, details) = match validate_abuse_report_payload(&req) {
        Ok(v) => v,
        Err((code, msg)) => return json_error(code, msg),
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let enroll_row = match sqlx::query(
        "SELECT status FROM program_enrollments WHERE program_id = $1 AND patient_user_id = $2",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(r)) => r,
        Ok(None) => {
            return json_error(
                StatusCode::FORBIDDEN,
                "Apenas participantes vinculados podem reportar este programa.",
            )
        }
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao validar vínculo.",
            )
        }
    };

    let mut tx = match pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao abrir transação.",
            )
        }
    };

    let report_id: Uuid = match sqlx::query_scalar(
        "INSERT INTO program_abuse_reports (program_id, reporter_user_id, reason, details) \
         VALUES ($1, $2, $3, $4) \
         RETURNING id",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .bind(&reason)
    .bind(&details)
    .fetch_one(&mut *tx)
    .await
    {
        Ok(id) => id,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao registrar denúncia.",
            )
        }
    };

    let should_mute = req.mute_also.unwrap_or(true);
    let enroll_status: String = enroll_row.get("status");
    let mut is_muted = false;
    if should_mute && enroll_status == "ACTIVE" {
        let _ = sqlx::query(
            "UPDATE program_enrollments \
                SET muted_at = COALESCE(muted_at, NOW()), updated_at = NOW() \
              WHERE program_id = $1 AND patient_user_id = $2 AND status = 'ACTIVE'",
        )
        .bind(prog_uuid)
        .bind(&identity.uid)
        .execute(&mut *tx)
        .await;
        is_muted = true;
    }

    let _ = record_audit(
        &mut *tx,
        Some(prog_uuid),
        &identity.uid,
        &identity.uid,
        "PROGRAM_ABUSE_REPORTED",
        json!({
            "reportId": report_id.to_string(),
            "reason": reason,
            "muted": is_muted
        }),
    )
    .await;

    if tx.commit().await.is_err() {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar denúncia.",
        );
    }

    (
        StatusCode::CREATED,
        Json(json!({
            "reportId": report_id.to_string(),
            "programId": prog_uuid.to_string(),
            "muted": is_muted
        })),
    )
        .into_response()
}

/// Lista denuncias UGC B2B para equipe de moderacao (`GET /api/admin/b2b/abuse-reports`).
pub async fn admin_list_abuse_reports_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<AdminAbuseReportsQuery>,
) -> Response {
    if let Err((code, msg)) = require_service_token(&headers) {
        return json_error(code, msg);
    }
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let status_filter = query
        .status
        .as_deref()
        .map(str::trim)
        .filter(|s| matches!(*s, "OPEN" | "REVIEWED" | "DISMISSED"))
        .map(ToString::to_string);
    let limit = query.limit.unwrap_or(20).clamp(1, 50);

    let rows = match sqlx::query(
        "SELECT r.id::text AS report_id, r.program_id::text AS program_id, \
                pr.program_name, pr.company_name, r.reporter_user_id, r.reason, \
                r.details, r.status, r.created_at \
           FROM program_abuse_reports r \
           JOIN partner_programs pr ON pr.id = r.program_id \
          WHERE ($1::text IS NULL OR r.status = $1) \
          ORDER BY r.created_at DESC \
          LIMIT $2",
    )
    .bind(status_filter)
    .bind(limit)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao listar denúncias.",
            )
        }
    };

    let reports: Vec<Value> = rows
        .into_iter()
        .map(|row| {
            let created_at: DateTime<Utc> = row.get("created_at");
            json!({
                "reportId": row.get::<String, _>("report_id"),
                "programId": row.get::<String, _>("program_id"),
                "programName": row.get::<String, _>("program_name"),
                "companyName": row.get::<String, _>("company_name"),
                "reporterUserId": row.get::<String, _>("reporter_user_id"),
                "reason": row.get::<String, _>("reason"),
                "details": row.try_get::<Option<String>, _>("details").ok().flatten(),
                "status": row.get::<String, _>("status"),
                "createdAt": created_at.to_rfc3339()
            })
        })
        .collect();

    (StatusCode::OK, Json(json!({ "reports": reports }))).into_response()
}

#[derive(Debug, Default)]
pub struct B2BCleanupSummary {
    pub reminders_purged: u64,
    pub direct_messages_purged: u64,
    pub cnpj_cache_purged: u64,
}

/// Expurga lotes limitados do buffer temporario B2B (protege o RDS db.t4g.micro sem locks longos).
pub async fn cleanup_expired_b2b_buffers(pool: &PgPool) -> Result<B2BCleanupSummary, sqlx::Error> {
    let rem = sqlx::query(
        "DELETE FROM program_reminders \
          WHERE id IN (SELECT id FROM program_reminders WHERE expires_at < NOW() LIMIT 500)",
    )
    .execute(pool)
    .await?
    .rows_affected();

    let msg = sqlx::query(include_str!("../sql/purge_direct_messages.sql"))
        .execute(pool)
        .await?
        .rows_affected();

    let cache = sqlx::query(
        "DELETE FROM cnpj_lookup_cache \
          WHERE cnpj_digits IN ( \
              SELECT cnpj_digits FROM cnpj_lookup_cache \
               WHERE expires_at < NOW() \
               LIMIT 500 \
          )",
    )
    .execute(pool)
    .await?
    .rows_affected();

    Ok(B2BCleanupSummary {
        reminders_purged: rem,
        direct_messages_purged: msg,
        cnpj_cache_purged: cache,
    })
}

/// Tarefa leve em Tokio executando a limpeza de buffers expirados a cada 6 horas.
pub async fn spawn_rds_buffer_cleanup_loop(pool: PgPool) {
    let mut ticker = tokio::time::interval(Duration::from_secs(6 * 3600));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    // Evita disputar conexoes no primeiro segundo de boot do braga-edge.
    ticker.tick().await;
    loop {
        ticker.tick().await;
        match cleanup_expired_b2b_buffers(&pool).await {
            Ok(summary) => {
                if summary.reminders_purged > 0
                    || summary.direct_messages_purged > 0
                    || summary.cnpj_cache_purged > 0
                {
                    info!(
                        reminders = summary.reminders_purged,
                        direct_messages = summary.direct_messages_purged,
                        cnpj_cache = summary.cnpj_cache_purged,
                        "Limpeza periódica de buffers B2B concluída"
                    );
                }
            }
            Err(err) => {
                warn!("Falha na limpeza periódica de buffers B2B: {err}");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reminder_guardrail_blocks_posology_and_allows_hydration() {
        let unsafe_req = CreateProgramReminderRequest {
            target_patient_id: None,
            target_category: None,
            reminder_type: Some("ROUTINE".to_string()),
            title: "Tomar comprimido".to_string(),
            body: "Tomar 50 mg de losartana agora".to_string(),
        };
        let err = validate_and_sanitize_reminder_payload(&unsafe_req)
            .err()
            .expect("Posologia precisa retornar 422");
        assert_eq!(err.0, StatusCode::UNPROCESSABLE_ENTITY);

        let med_type_req = CreateProgramReminderRequest {
            target_patient_id: None,
            target_category: None,
            reminder_type: Some("MEDICATION".to_string()),
            title: "Horario".to_string(),
            body: "Confira o app".to_string(),
        };
        let err_type = validate_and_sanitize_reminder_payload(&med_type_req)
            .err()
            .expect("Tipo MEDICATION precisa retornar 422");
        assert_eq!(err_type.0, StatusCode::UNPROCESSABLE_ENTITY);

        // Hidratacao com "300 ml" e link https passa intacto; http/javascript sao removidos
        let safe_req = CreateProgramReminderRequest {
            target_patient_id: Some("patient-1".to_string()),
            target_category: Some("hydration".to_string()),
            reminder_type: Some("HYDRATION".to_string()),
            title: "Meta da tarde".to_string(),
            body: "Beba 300 ml de agua e acesse https://bragasaude.online ou http://insecure.local"
                .to_string(),
        };
        let ok = validate_and_sanitize_reminder_payload(&safe_req).expect("Hidratacao valida");
        assert!(ok.body.contains("https://bragasaude.online"));
        assert!(!ok.body.contains("http://insecure.local"));
        assert!(ok.body.contains("[link removido]"));
    }

    #[test]
    fn fold_accent_prevents_posology_bypass() {
        assert!(contains_unsafe_posology_text(
            "Aumentar dósagem da cápsula (25mcg)"
        ));
        assert!(contains_unsafe_posology_text(
            "Solicitar nova prescrição médica"
        ));
        assert!(!contains_unsafe_posology_text(
            "Lembrete: beber 400 ml de água e registrar passos antes do retorno."
        ));
    }

    #[test]
    fn abuse_report_validation_rejects_empty_and_sanitizes_details() {
        let empty = ReportProgramAbuseRequest {
            reason: "   ".to_string(),
            details: None,
            mute_also: Some(true),
        };
        assert_eq!(
            validate_abuse_report_payload(&empty).err().unwrap().0,
            StatusCode::UNPROCESSABLE_ENTITY
        );

        let valid = ReportProgramAbuseRequest {
            reason: "CONTEUDO_INADEQUADO".to_string(),
            details: Some("Enviou link http://spam.local".to_string()),
            mute_also: Some(true),
        };
        let (reason, details) = validate_abuse_report_payload(&valid).expect("Report valido");
        assert_eq!(reason, "CONTEUDO_INADEQUADO");
        assert!(details.unwrap().contains("[link removido]"));
    }
}
