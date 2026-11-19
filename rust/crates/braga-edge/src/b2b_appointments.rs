//! Motor de Agendamento Configurável B2B In-App em Rust (`braga-edge`) — B2B-19.
//!
//! Opera exclusivamente sobre `program_appointments` (migracao 092), sem tocar na tabela
//! pessoal/familiar `consultations`. Garante trava de 3 pendentes por participante,
//! serializacao transacional contra dupla confirmacao na janela de 30 minutos (`409`)
//! e emissao de notificacao Push FCM (`082` `PARTNER_APPOINTMENT` na categoria `AGENDA`).

use std::sync::Arc;

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
use uuid::Uuid;

use crate::b2b::{
    guard_program_module, json_error, parse_experience_config_json, record_audit, require_pool,
    AppointmentsPolicyDto, ApprovalStatus, ProgramModule, ALLOWED_APPOINTMENT_MODALITIES,
};
use crate::b2b_engagement::sanitize_non_https_urls;
use crate::{authenticate_request, AppState};

pub const MAX_PENDING_APPOINTMENTS_PER_PROGRAM: i64 = 3;
pub const CONFLICT_WINDOW_SECONDS: f64 = 1800.0;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ListAppointmentsQuery {
    pub status: Option<String>,
    pub patient_id: Option<String>,
    pub limit: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateAppointmentRequest {
    pub modality: String,
    pub requested_slot_text: String,
    pub patient_note: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateOwnerAppointmentRequest {
    pub status: String,
    pub scheduled_at: Option<String>,
    pub partner_note: Option<String>,
    #[serde(default)]
    pub allow_overlap: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CancelAppointmentRequest {
    pub reason: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProgramAppointmentDto {
    pub appointment_id: String,
    pub program_id: String,
    pub patient_user_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub patient_name: Option<String>,
    pub modality: String,
    pub requested_slot_text: String,
    pub scheduled_at: Option<String>,
    pub patient_note: Option<String>,
    pub partner_note: Option<String>,
    pub status: String,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug)]
pub(crate) struct NormalizedAppointmentInput {
    pub modality: String,
    pub requested_slot_text: String,
    pub patient_note: Option<String>,
}

pub(crate) fn validate_create_appointment_input(
    req: &CreateAppointmentRequest,
    policy: &AppointmentsPolicyDto,
) -> Result<NormalizedAppointmentInput, (StatusCode, &'static str)> {
    if policy
        .booking_mode
        .eq_ignore_ascii_case("EXTERNAL_LINK_ONLY")
    {
        return Err((
            StatusCode::CONFLICT,
            "Este programa realiza agendamentos apenas pelo link externo configurado.",
        ));
    }

    let modality = req.modality.trim().to_ascii_uppercase();
    if !ALLOWED_APPOINTMENT_MODALITIES.contains(&modality.as_str())
        || !policy
            .allowed_modalities
            .iter()
            .any(|m| m.eq_ignore_ascii_case(&modality))
    {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "Modalidade de agendamento não habilitada para este programa.",
        ));
    }

    let slot_text = sanitize_non_https_urls(&req.requested_slot_text);
    let slot_len = slot_text.chars().count();
    if !(2..=160).contains(&slot_len) {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "Informe a janela de horário desejada (entre 2 e 160 caracteres).",
        ));
    }

    let note = req
        .patient_note
        .as_deref()
        .map(sanitize_non_https_urls)
        .filter(|s| !s.is_empty());
    if note.as_ref().is_some_and(|n| n.chars().count() > 400) {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "A observação do agendamento deve ter no máximo 400 caracteres.",
        ));
    }

    Ok(NormalizedAppointmentInput {
        modality,
        requested_slot_text: slot_text,
        patient_note: note,
    })
}

pub(crate) fn normalize_appointment_status_filter(
    raw: Option<&str>,
) -> Result<Option<String>, (StatusCode, &'static str)> {
    match raw.map(str::trim).filter(|s| !s.is_empty()) {
        None => Ok(None),
        Some(v) => {
            let upper = v.to_ascii_uppercase();
            match upper.as_str() {
                "PENDING" | "CONFIRMED" | "RESCHEDULE_REQUESTED" | "CANCELLED" | "COMPLETED" => {
                    Ok(Some(upper))
                }
                _ => Err((StatusCode::BAD_REQUEST, "Filtro de status inválido.")),
            }
        }
    }
}

pub(crate) fn can_owner_transition_status(current: &str, target: &str) -> bool {
    matches!(
        (current, target),
        ("PENDING", "CONFIRMED")
            | ("PENDING", "RESCHEDULE_REQUESTED")
            | ("PENDING", "CANCELLED")
            | ("RESCHEDULE_REQUESTED", "CONFIRMED")
            | ("RESCHEDULE_REQUESTED", "RESCHEDULE_REQUESTED")
            | ("RESCHEDULE_REQUESTED", "CANCELLED")
            | ("CONFIRMED", "RESCHEDULE_REQUESTED")
            | ("CONFIRMED", "CANCELLED")
            | ("CONFIRMED", "COMPLETED")
    )
}

pub(crate) fn parse_future_utc_timestamp(
    raw: Option<&str>,
    now: DateTime<Utc>,
) -> Result<DateTime<Utc>, (StatusCode, &'static str)> {
    let text = raw.map(str::trim).filter(|s| !s.is_empty()).ok_or((
        StatusCode::UNPROCESSABLE_ENTITY,
        "Informe data e horário confirmados em ISO-8601 UTC.",
    ))?;
    let parsed = DateTime::parse_from_rfc3339(text)
        .map(|dt| dt.with_timezone(&Utc))
        .map_err(|_| {
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Formato de data inválido; utilize ISO-8601 (RFC3339).",
            )
        })?;
    if parsed <= now {
        return Err((
            StatusCode::UNPROCESSABLE_ENTITY,
            "A data confirmada do agendamento precisa estar no futuro.",
        ));
    }
    Ok(parsed)
}

pub(crate) fn map_appointment_row(
    row: &sqlx::postgres::PgRow,
    include_patient_name: bool,
) -> ProgramAppointmentDto {
    let created_at: DateTime<Utc> = row.get("created_at");
    let updated_at: DateTime<Utc> = row.get("updated_at");
    let scheduled_at: Option<DateTime<Utc>> = row.try_get("scheduled_at").ok().flatten();
    ProgramAppointmentDto {
        appointment_id: row.get::<String, _>("appointment_id"),
        program_id: row.get::<String, _>("program_id"),
        patient_user_id: row.get::<String, _>("patient_user_id"),
        patient_name: if include_patient_name {
            row.try_get::<Option<String>, _>("patient_name")
                .ok()
                .flatten()
        } else {
            None
        },
        modality: row.get("modality"),
        requested_slot_text: row.get("requested_slot_text"),
        scheduled_at: scheduled_at.map(|dt| dt.to_rfc3339()),
        patient_note: row
            .try_get::<Option<String>, _>("patient_note")
            .ok()
            .flatten(),
        partner_note: row
            .try_get::<Option<String>, _>("partner_note")
            .ok()
            .flatten(),
        status: row.get("status"),
        created_at: created_at.to_rfc3339(),
        updated_at: updated_at.to_rfc3339(),
    }
}

async fn emit_partner_appointment_notification(
    tx: &mut Transaction<'_, Postgres>,
    recipient_uid: &str,
    patient_uid: &str,
    appointment_id: Uuid,
    actor_uid: &str,
    status: &str,
    suffix: &str,
) {
    let event_key = format!("b2b_appt:{appointment_id}:{suffix}:{recipient_uid}");
    let group_key = format!("b2b_appt:{appointment_id}");
    let payload = json!({ "status": status });
    let _ = sqlx::query(
        "SELECT notification_emit($1, $2, $3, 'PARTNER_APPOINTMENT', 'AGENDA', 'b2b_appointment', $4, $5, $6, NOW(), NOW() + interval '30 days', $7::jsonb, true)",
    )
    .bind(recipient_uid)
    .bind(&event_key)
    .bind(&group_key)
    .bind(appointment_id.to_string())
    .bind(patient_uid)
    .bind(actor_uid)
    .bind(payload)
    .execute(&mut **tx)
    .await;
}

struct AppointmentFanoutCtx<'a> {
    program_id: Uuid,
    patient_uid: &'a str,
    appointment_id: Uuid,
    actor_uid: &'a str,
    status: &'a str,
    suffix: &'a str,
    family_link_enabled: bool,
}

async fn emit_patient_and_family_appointment_notifications(
    tx: &mut Transaction<'_, Postgres>,
    ctx: &AppointmentFanoutCtx<'_>,
) {
    emit_partner_appointment_notification(
        tx,
        ctx.patient_uid,
        ctx.patient_uid,
        ctx.appointment_id,
        ctx.actor_uid,
        ctx.status,
        ctx.suffix,
    )
    .await;

    if !ctx.family_link_enabled {
        return;
    }

    if let Ok(caregivers) = sqlx::query_scalar::<_, String>(
        "SELECT fb.caregiver_id \
           FROM program_enrollments e \
           JOIN family_bindings fb \
             ON fb.patient_id = e.patient_user_id \
            AND fb.status = 'ACTIVE' \
          WHERE e.program_id = $1 \
            AND e.patient_user_id = $2 \
            AND e.status = 'ACTIVE' \
            AND COALESCE(e.family_share_enabled, false) = true",
    )
    .bind(ctx.program_id)
    .bind(ctx.patient_uid)
    .fetch_all(&mut **tx)
    .await
    {
        for caregiver_uid in caregivers {
            emit_partner_appointment_notification(
                tx,
                &caregiver_uid,
                ctx.patient_uid,
                ctx.appointment_id,
                ctx.actor_uid,
                ctx.status,
                ctx.suffix,
            )
            .await;
        }
    }
}

async fn fetch_active_program_for_owner(
    pool: &PgPool,
    owner_uid: &str,
) -> Result<(Uuid, crate::b2b::ProgramExperienceConfigDto, String), (StatusCode, &'static str)> {
    let row = sqlx::query(
        "SELECT id, program_name, approval_status, closed_at, template_key, experience_config \
           FROM partner_programs \
          WHERE owner_user_id = $1 AND closed_at IS NULL",
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
        ProgramModule::Appointments,
    )?;
    Ok((program_id, config, program_name))
}

async fn fetch_active_enrolled_program(
    pool: &PgPool,
    program_id: Uuid,
    patient_uid: &str,
) -> Result<(String, String, crate::b2b::ProgramExperienceConfigDto), (StatusCode, &'static str)> {
    let row = sqlx::query(
        "SELECT pr.owner_user_id, pr.program_name, pr.approval_status, pr.closed_at, \
                pr.template_key, pr.experience_config, e.status AS enrollment_status \
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
    .ok_or((StatusCode::NOT_FOUND, "Programa parceiro não encontrado."))?;

    let enroll_status: Option<String> = row.try_get("enrollment_status").ok().flatten();
    if enroll_status.as_deref() != Some("ACTIVE") {
        return Err((
            StatusCode::FORBIDDEN,
            "Vínculo ativo necessário para acessar agendamentos deste programa.",
        ));
    }

    let owner_uid: String = row.get("owner_user_id");
    let program_name: String = row.get("program_name");
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
        ProgramModule::Appointments,
    )?;
    Ok((owner_uid, program_name, config))
}

/// Lista agendamentos do programa para o responsavel (`GET /api/b2b/program/me/appointments`).
pub async fn list_owner_appointments_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<ListAppointmentsQuery>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "GET", "/api/b2b/program/me/appointments")
            .await
        {
            Ok(id) => id,
            Err(resp) => return resp,
        };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let (program_id, config, program_name) =
        match fetch_active_program_for_owner(pool, &identity.uid).await {
            Ok(v) => v,
            Err((code, msg)) => return json_error(code, msg),
        };
    let status_filter = match normalize_appointment_status_filter(query.status.as_deref()) {
        Ok(s) => s,
        Err((code, msg)) => return json_error(code, msg),
    };
    let patient_filter = query
        .patient_id
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(ToString::to_string);
    let limit = query.limit.unwrap_or(50).clamp(1, 50);

    let rows = match sqlx::query(
        "SELECT a.id::text AS appointment_id, a.program_id::text AS program_id, \
                a.patient_user_id, COALESCE(NULLIF(BTRIM(pf.full_name), ''), 'Participante') AS patient_name, \
                a.modality, a.requested_slot_text, a.scheduled_at, a.patient_note, \
                a.partner_note, a.status, a.created_at, a.updated_at \
           FROM program_appointments a \
           LEFT JOIN profiles pf ON pf.id = a.patient_user_id \
          WHERE a.program_id = $1 \
            AND ($2::text IS NULL OR a.status = $2) \
            AND ($3::text IS NULL OR a.patient_user_id = $3) \
          ORDER BY CASE a.status \
                     WHEN 'PENDING' THEN 0 \
                     WHEN 'RESCHEDULE_REQUESTED' THEN 1 \
                     WHEN 'CONFIRMED' THEN 2 \
                     ELSE 3 \
                   END, \
                   COALESCE(a.scheduled_at, a.updated_at) ASC, \
                   a.updated_at DESC \
          LIMIT $4",
    )
    .bind(program_id)
    .bind(status_filter)
    .bind(patient_filter)
    .bind(limit)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao listar agendamentos do programa.",
            )
        }
    };

    let appointments: Vec<ProgramAppointmentDto> = rows
        .iter()
        .map(|row| map_appointment_row(row, true))
        .collect();

    (
        StatusCode::OK,
        Json(json!({
            "programId": program_id.to_string(),
            "programName": program_name,
            "appointmentsPolicy": config.appointments_policy,
            "appointments": appointments
        })),
    )
        .into_response()
}

/// Atualiza estado de um agendamento pelo responsavel (`PATCH /api/b2b/program/me/appointments/:appointment_id`).
pub async fn update_owner_appointment_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(appointment_id_raw): Path<String>,
    Json(req): Json<UpdateOwnerAppointmentRequest>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "PATCH",
        "/api/b2b/program/me/appointments",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let Ok(appt_uuid) = Uuid::parse_str(appointment_id_raw.trim()) else {
        return json_error(
            StatusCode::BAD_REQUEST,
            "Identificador de agendamento inválido.",
        );
    };

    let target_status = req.status.trim().to_ascii_uppercase();
    if !matches!(
        target_status.as_str(),
        "CONFIRMED" | "RESCHEDULE_REQUESTED" | "CANCELLED" | "COMPLETED"
    ) {
        return json_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "Status de destino inválido para decisão de agenda.",
        );
    }

    let partner_note = req
        .partner_note
        .as_deref()
        .map(sanitize_non_https_urls)
        .filter(|s| !s.is_empty());
    if partner_note
        .as_ref()
        .is_some_and(|n| n.chars().count() > 400)
    {
        return json_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "A observação do parceiro deve ter no máximo 400 caracteres.",
        );
    }

    let parsed_scheduled_at = if target_status == "CONFIRMED" {
        match parse_future_utc_timestamp(req.scheduled_at.as_deref(), Utc::now()) {
            Ok(dt) => Some(dt),
            Err((code, msg)) => return json_error(code, msg),
        }
    } else if let Some(raw_dt) = req.scheduled_at.as_deref().filter(|s| !s.trim().is_empty()) {
        match parse_future_utc_timestamp(Some(raw_dt), Utc::now()) {
            Ok(dt) => Some(dt),
            Err((code, msg)) => return json_error(code, msg),
        }
    } else {
        None
    };

    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let (program_id, config, _) = match fetch_active_program_for_owner(pool, &identity.uid).await {
        Ok(v) => v,
        Err((code, msg)) => return json_error(code, msg),
    };

    let mut tx = match pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao iniciar transação.",
            )
        }
    };

    // Serializa confirmacoes no mesmo programa para impedir dupla marcacao concorrente no mesmo horario.
    if sqlx::query_scalar::<_, Uuid>("SELECT id FROM partner_programs WHERE id = $1 FOR UPDATE")
        .bind(program_id)
        .fetch_optional(&mut *tx)
        .await
        .is_err()
    {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao bloquear agenda do programa.",
        );
    }

    let existing = match sqlx::query(
        "SELECT id::text AS appointment_id, program_id::text AS program_id, patient_user_id, \
                modality, requested_slot_text, scheduled_at, patient_note, partner_note, \
                status, created_at, updated_at \
           FROM program_appointments \
          WHERE id = $1 AND program_id = $2 \
          FOR UPDATE",
    )
    .bind(appt_uuid)
    .bind(program_id)
    .fetch_optional(&mut *tx)
    .await
    {
        Ok(Some(r)) => r,
        Ok(None) => return json_error(StatusCode::NOT_FOUND, "Agendamento não encontrado."),
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao carregar agendamento.",
            )
        }
    };

    let current_status: String = existing.get("status");
    if !can_owner_transition_status(&current_status, &target_status) {
        return json_error(
            StatusCode::CONFLICT,
            "Transição de status não permitida para o estado atual deste agendamento.",
        );
    }

    if target_status == "CONFIRMED" && !req.allow_overlap {
        if let Some(slot_dt) = parsed_scheduled_at {
            let has_conflict: bool = sqlx::query_scalar(
                "SELECT EXISTS( \
                    SELECT 1 FROM program_appointments \
                     WHERE program_id = $1 \
                       AND id <> $2 \
                       AND status = 'CONFIRMED' \
                       AND scheduled_at IS NOT NULL \
                       AND ABS(EXTRACT(EPOCH FROM (scheduled_at - $3))) < $4 \
                )",
            )
            .bind(program_id)
            .bind(appt_uuid)
            .bind(slot_dt)
            .bind(CONFLICT_WINDOW_SECONDS)
            .fetch_one(&mut *tx)
            .await
            .unwrap_or(false);

            if has_conflict {
                return json_error(
                    StatusCode::CONFLICT,
                    "Já existe outro agendamento confirmado na janela de 30 minutos para este programa.",
                );
            }
        }
    }

    let patient_uid: String = existing.get("patient_user_id");
    let final_scheduled_at: Option<DateTime<Utc>> = match target_status.as_str() {
        "CONFIRMED" => parsed_scheduled_at,
        "RESCHEDULE_REQUESTED" => parsed_scheduled_at,
        _ => existing.try_get("scheduled_at").ok().flatten(),
    };

    let updated = match sqlx::query(
        "UPDATE program_appointments \
            SET status = $3, \
                scheduled_at = $4, \
                partner_note = COALESCE($5, partner_note), \
                updated_at = NOW() \
          WHERE id = $1 AND program_id = $2 \
          RETURNING id::text AS appointment_id, program_id::text AS program_id, patient_user_id, \
                    modality, requested_slot_text, scheduled_at, patient_note, partner_note, \
                    status, created_at, updated_at",
    )
    .bind(appt_uuid)
    .bind(program_id)
    .bind(&target_status)
    .bind(final_scheduled_at)
    .bind(&partner_note)
    .fetch_one(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao atualizar agendamento.",
            )
        }
    };

    let ts_suffix = format!("{target_status}:{}", Utc::now().timestamp_millis());
    let fanout = AppointmentFanoutCtx {
        program_id,
        patient_uid: &patient_uid,
        appointment_id: appt_uuid,
        actor_uid: &identity.uid,
        status: &target_status,
        suffix: &ts_suffix,
        family_link_enabled: config.features.family_link_enabled,
    };
    emit_patient_and_family_appointment_notifications(&mut tx, &fanout).await;

    let _ = record_audit(
        &mut *tx,
        Some(program_id),
        &identity.uid,
        &patient_uid,
        "PARTNER_APPOINTMENT_UPDATED",
        json!({
            "appointmentId": appt_uuid.to_string(),
            "from": current_status,
            "to": target_status,
        }),
    )
    .await;

    if tx.commit().await.is_err() {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar atualização do agendamento.",
        );
    }

    let dto = map_appointment_row(&updated, false);
    (StatusCode::OK, Json(json!({ "appointment": dto }))).into_response()
}

/// Lista agendamentos do participante em um programa (`GET /api/b2b/enrollments/:program_id/appointments`).
pub async fn list_enrollment_appointments_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id_raw): Path<String>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "GET", "/api/b2b/enrollments/appointments")
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
    let (_, program_name, config) =
        match fetch_active_enrolled_program(pool, program_id, &identity.uid).await {
            Ok(v) => v,
            Err((code, msg)) => return json_error(code, msg),
        };

    let rows = match sqlx::query(
        "SELECT id::text AS appointment_id, program_id::text AS program_id, patient_user_id, \
                modality, requested_slot_text, scheduled_at, patient_note, partner_note, \
                status, created_at, updated_at \
           FROM program_appointments \
          WHERE program_id = $1 AND patient_user_id = $2 \
          ORDER BY CASE status \
                     WHEN 'CONFIRMED' THEN 0 \
                     WHEN 'RESCHEDULE_REQUESTED' THEN 1 \
                     WHEN 'PENDING' THEN 2 \
                     ELSE 3 \
                   END, \
                   COALESCE(scheduled_at, updated_at) DESC \
          LIMIT 30",
    )
    .bind(program_id)
    .bind(&identity.uid)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao listar seus agendamentos no programa.",
            )
        }
    };

    let appointments: Vec<ProgramAppointmentDto> = rows
        .iter()
        .map(|row| map_appointment_row(row, false))
        .collect();
    let pending_count = appointments
        .iter()
        .filter(|a| a.status == "PENDING")
        .count();

    (
        StatusCode::OK,
        Json(json!({
            "programId": program_id.to_string(),
            "programName": program_name,
            "appointmentsPolicy": config.appointments_policy,
            "pendingCount": pending_count,
            "appointments": appointments
        })),
    )
        .into_response()
}

/// Participante solicita horario em programa parceiro (`POST /api/b2b/enrollments/:program_id/appointments`).
pub async fn create_enrollment_appointment_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id_raw): Path<String>,
    Json(req): Json<CreateAppointmentRequest>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/enrollments/appointments",
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
    let (owner_uid, _, config) =
        match fetch_active_enrolled_program(pool, program_id, &identity.uid).await {
            Ok(v) => v,
            Err((code, msg)) => return json_error(code, msg),
        };
    let input = match validate_create_appointment_input(&req, &config.appointments_policy) {
        Ok(i) => i,
        Err((code, msg)) => return json_error(code, msg),
    };

    let mut tx = match pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao iniciar transação.",
            )
        }
    };

    let enroll_locked = sqlx::query_scalar::<_, String>(
        "SELECT status FROM program_enrollments \
          WHERE program_id = $1 AND patient_user_id = $2 \
          FOR UPDATE",
    )
    .bind(program_id)
    .bind(&identity.uid)
    .fetch_optional(&mut *tx)
    .await;

    match enroll_locked {
        Ok(Some(st)) if st == "ACTIVE" => {}
        _ => {
            return json_error(
                StatusCode::FORBIDDEN,
                "Vínculo ativo necessário para solicitar agendamento.",
            )
        }
    }

    let pending_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM program_appointments \
          WHERE program_id = $1 AND patient_user_id = $2 AND status = 'PENDING'",
    )
    .bind(program_id)
    .bind(&identity.uid)
    .fetch_one(&mut *tx)
    .await
    .unwrap_or(0);

    if pending_count >= MAX_PENDING_APPOINTMENTS_PER_PROGRAM {
        return json_error(
            StatusCode::CONFLICT,
            "Limite de 3 solicitações pendentes atingido neste programa.",
        );
    }

    let inserted = match sqlx::query(
        "INSERT INTO program_appointments \
            (program_id, patient_user_id, modality, requested_slot_text, patient_note, status) \
         VALUES ($1, $2, $3, $4, $5, 'PENDING') \
         RETURNING id, id::text AS appointment_id, program_id::text AS program_id, \
                   patient_user_id, modality, requested_slot_text, scheduled_at, \
                   patient_note, partner_note, status, created_at, updated_at",
    )
    .bind(program_id)
    .bind(&identity.uid)
    .bind(&input.modality)
    .bind(&input.requested_slot_text)
    .bind(&input.patient_note)
    .fetch_one(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao registrar solicitação de agendamento.",
            )
        }
    };

    let appt_uuid: Uuid = inserted.get("id");
    emit_partner_appointment_notification(
        &mut tx,
        &owner_uid,
        &identity.uid,
        appt_uuid,
        &identity.uid,
        "PENDING",
        "PENDING",
    )
    .await;

    let _ = record_audit(
        &mut *tx,
        Some(program_id),
        &identity.uid,
        &identity.uid,
        "PARTNER_APPOINTMENT_REQUESTED",
        json!({
            "appointmentId": appt_uuid.to_string(),
            "modality": input.modality,
        }),
    )
    .await;

    if tx.commit().await.is_err() {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar solicitação de agendamento.",
        );
    }

    let dto = map_appointment_row(&inserted, false);
    (StatusCode::CREATED, Json(json!({ "appointment": dto }))).into_response()
}

/// Participante cancela solicitacao ou horario futuro (`POST /api/b2b/enrollments/:program_id/appointments/:appointment_id/cancel`).
pub async fn cancel_enrollment_appointment_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((program_id_raw, appointment_id_raw)): Path<(String, String)>,
    body: Option<Json<CancelAppointmentRequest>>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/enrollments/appointments/cancel",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let (Ok(program_id), Ok(appt_uuid)) = (
        Uuid::parse_str(program_id_raw.trim()),
        Uuid::parse_str(appointment_id_raw.trim()),
    ) else {
        return json_error(StatusCode::BAD_REQUEST, "Identificador inválido.");
    };
    let cancel_reason = body
        .and_then(|Json(b)| b.reason)
        .map(|s| sanitize_non_https_urls(&s))
        .filter(|s| !s.is_empty())
        .map(|s| s.chars().take(400).collect::<String>());

    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };
    let (owner_uid, _, _) =
        match fetch_active_enrolled_program(pool, program_id, &identity.uid).await {
            Ok(v) => v,
            Err((code, msg)) => return json_error(code, msg),
        };

    let mut tx = match pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao iniciar transação.",
            )
        }
    };

    let existing = match sqlx::query(
        "SELECT status, scheduled_at \
           FROM program_appointments \
          WHERE id = $1 AND program_id = $2 AND patient_user_id = $3 \
          FOR UPDATE",
    )
    .bind(appt_uuid)
    .bind(program_id)
    .bind(&identity.uid)
    .fetch_optional(&mut *tx)
    .await
    {
        Ok(Some(r)) => r,
        Ok(None) => return json_error(StatusCode::NOT_FOUND, "Agendamento não encontrado."),
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao carregar agendamento.",
            )
        }
    };

    let current_status: String = existing.get("status");
    let scheduled_at: Option<DateTime<Utc>> = existing.try_get("scheduled_at").ok().flatten();
    let cancellable = match current_status.as_str() {
        "PENDING" | "RESCHEDULE_REQUESTED" => true,
        "CONFIRMED" => scheduled_at.is_none_or(|dt| dt > Utc::now()),
        _ => false,
    };
    if !cancellable {
        return json_error(
            StatusCode::CONFLICT,
            "Este agendamento não pode mais ser cancelado.",
        );
    }

    let updated = match sqlx::query(
        "UPDATE program_appointments \
            SET status = 'CANCELLED', \
                patient_note = COALESCE($4, patient_note), \
                updated_at = NOW() \
          WHERE id = $1 AND program_id = $2 AND patient_user_id = $3 \
          RETURNING id::text AS appointment_id, program_id::text AS program_id, \
                    patient_user_id, modality, requested_slot_text, scheduled_at, \
                    patient_note, partner_note, status, created_at, updated_at",
    )
    .bind(appt_uuid)
    .bind(program_id)
    .bind(&identity.uid)
    .bind(&cancel_reason)
    .fetch_one(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao cancelar agendamento.",
            )
        }
    };

    let suffix = format!("CANCELLED:{}", Utc::now().timestamp_millis());
    emit_partner_appointment_notification(
        &mut tx,
        &owner_uid,
        &identity.uid,
        appt_uuid,
        &identity.uid,
        "CANCELLED",
        &suffix,
    )
    .await;

    let _ = record_audit(
        &mut *tx,
        Some(program_id),
        &identity.uid,
        &identity.uid,
        "PARTNER_APPOINTMENT_CANCELLED",
        json!({ "appointmentId": appt_uuid.to_string(), "previous": current_status }),
    )
    .await;

    if tx.commit().await.is_err() {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar cancelamento do agendamento.",
        );
    }

    let dto = map_appointment_row(&updated, false);
    (StatusCode::OK, Json(json!({ "appointment": dto }))).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    fn sample_policy(mode: &str) -> AppointmentsPolicyDto {
        AppointmentsPolicyDto {
            allowed_modalities: vec!["IN_PERSON".to_string(), "REMOTE".to_string()],
            booking_mode: mode.to_string(),
            availabilities_summary: "Ter e Qui 09h-17h".to_string(),
            external_booking_url: Some("https://agenda.clinica.example".to_string()),
        }
    }

    #[test]
    fn external_link_only_blocks_native_request_with_409() {
        let req = CreateAppointmentRequest {
            modality: "REMOTE".to_string(),
            requested_slot_text: "Terca 14h".to_string(),
            patient_note: None,
        };
        let err = validate_create_appointment_input(&req, &sample_policy("EXTERNAL_LINK_ONLY"))
            .expect_err("EXTERNAL_LINK_ONLY deve retornar 409");
        assert_eq!(err.0, StatusCode::CONFLICT);
    }

    #[test]
    fn create_validation_sanitizes_links_and_allows_valid_request() {
        let req = CreateAppointmentRequest {
            modality: "remote".to_string(),
            requested_slot_text: "Quinta de manha http://phish.local".to_string(),
            patient_note: Some("Prefiro apos as 10h javascript:alert(1)".to_string()),
        };
        let normalized =
            validate_create_appointment_input(&req, &sample_policy("REQUEST_CONFIRMATION"))
                .expect("Solicitacao valida deve passar");
        assert_eq!(normalized.modality, "REMOTE");
        assert!(!normalized
            .requested_slot_text
            .contains("http://phish.local"));
        assert!(normalized.requested_slot_text.contains("[link removido]"));
        assert!(!normalized.patient_note.unwrap().contains("javascript:"));
    }

    #[test]
    fn owner_status_transitions_and_future_timestamp_validation() {
        assert!(can_owner_transition_status("PENDING", "CONFIRMED"));
        assert!(can_owner_transition_status("CONFIRMED", "COMPLETED"));
        assert!(!can_owner_transition_status("CANCELLED", "CONFIRMED"));
        assert!(!can_owner_transition_status("COMPLETED", "CANCELLED"));

        let now = Utc::now();
        let past = (now - Duration::minutes(10)).to_rfc3339();
        let future = (now + Duration::hours(2)).to_rfc3339();
        assert_eq!(
            parse_future_utc_timestamp(Some(&past), now).unwrap_err().0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert!(parse_future_utc_timestamp(Some(&future), now).is_ok());
    }
}
