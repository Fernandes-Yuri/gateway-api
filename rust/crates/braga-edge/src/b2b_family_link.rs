//! Link Familiar B2B com Trava Dupla e Governanca LGPD (`braga-edge`) — B2B-21.
//!
//! Expoe ao cuidador (via `require_family_access` sobre `family_bindings = ACTIVE`)
//! exclusivamente uma visao de leitura dos lembretes e agendamentos do programa parceiro
//! quando AMBAS as chaves estiverem ativas:
//!   1. `experienceConfig.features.familyLinkEnabled == true` (programa permite link familiar)
//!   2. `program_enrollments.family_share_enabled == true` (opt-in explicito do paciente)
//!
//! Nunca expoe chat 1:1 (`program_direct_messages`) nem tabelas clinicas pessoais fora
//! dos guardrails de `family_bindings`.

use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use braga_identity::{require_family_access, IdentityError};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::{json, Value};
use sqlx::{PgPool, Row};
use uuid::Uuid;

use crate::b2b::{
    guard_program_module, json_error, parse_experience_config_json, record_audit, require_pool,
    ApprovalStatus, ProgramModule,
};
use crate::b2b_appointments::{map_appointment_row, ProgramAppointmentDto};
use crate::b2b_engagement::{map_enrollment_reminder_row, ProgramReminderDto};
use crate::{authenticate_request, AppState};

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FamilyProgramOverviewItemDto {
    pub program_id: String,
    pub program_name: String,
    pub company_name: String,
    pub primary_color: String,
    pub logo_url: Option<String>,
    pub template_key: String,
    pub consented_at: String,
    pub family_share_enabled: bool,
    pub reminders_enabled: bool,
    pub appointments_enabled: bool,
    pub reminders: Vec<ProgramReminderDto>,
    pub appointments: Vec<ProgramAppointmentDto>,
}

/// Trava dupla B2B-21: programa precisa permitir Link Familiar E participante ter dado opt-in.
pub fn is_family_link_authorized(
    program_family_link_enabled: bool,
    enrollment_family_share_enabled: bool,
) -> bool {
    program_family_link_enabled && enrollment_family_share_enabled
}

fn validate_patient_id_param(raw: &str) -> Result<&str, (StatusCode, &'static str)> {
    let clean = raw.trim();
    if clean.is_empty() || clean.len() > 128 {
        return Err((
            StatusCode::BAD_REQUEST,
            "Identificador do participante inválido.",
        ));
    }
    Ok(clean)
}

async fn ensure_family_access(
    pool: &PgPool,
    caller_uid: &str,
    patient_id: &str,
) -> Result<(), Response> {
    match require_family_access(pool, caller_uid, patient_id).await {
        Ok(()) => Ok(()),
        Err(IdentityError::FamilyAccessForbidden)
        | Err(IdentityError::PatientPermissionForbidden) => Err(json_error(
            StatusCode::FORBIDDEN,
            "Seu vínculo familiar não permite visualizar ou confirmar ações deste programa.",
        )),
        Err(_) => Err(json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao validar vínculo familiar.",
        )),
    }
}

/// Lista programas parceiros compartilhados com o cuidador autorizado + lembretes/agendamentos
/// (`GET /api/b2b/family/patients/:patient_id/program-overview`).
pub async fn get_family_program_overview_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(patient_id_raw): Path<String>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        "/api/b2b/family/patients/:patient_id/program-overview",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let patient_id = match validate_patient_id_param(&patient_id_raw) {
        Ok(id) => id,
        Err((code, msg)) => return json_error(code, msg),
    };

    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    if let Err(resp) = ensure_family_access(pool, &identity.uid, patient_id).await {
        return resp;
    }

    let enroll_rows = match sqlx::query(
        "SELECT pr.id AS program_uuid, \
                pr.id::text AS program_id, \
                pr.program_name, \
                pr.company_name, \
                pr.primary_color, \
                pr.logo_url, \
                pr.template_key, \
                pr.experience_config, \
                e.consented_at, \
                e.shared_categories, \
                COALESCE(e.family_share_enabled, false) AS family_share_enabled \
           FROM program_enrollments e \
           JOIN partner_programs pr ON pr.id = e.program_id \
          WHERE e.patient_user_id = $1 \
            AND e.status = 'ACTIVE' \
            AND COALESCE(e.family_share_enabled, false) = true \
            AND pr.approval_status = 'APPROVED' \
            AND pr.closed_at IS NULL \
          ORDER BY e.consented_at DESC \
          LIMIT 5",
    )
    .bind(patient_id)
    .fetch_all(pool)
    .await
    {
        Ok(rows) => rows,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar programas vinculados ao familiar.",
            )
        }
    };

    let mut programs = Vec::new();

    for row in enroll_rows {
        let family_share_enabled: bool = row.try_get("family_share_enabled").unwrap_or(false);
        let tpl_raw: Option<String> = row.try_get("template_key").ok().flatten();
        let cfg_raw: Option<Value> = row.try_get("experience_config").ok().flatten();
        let (template_key, config) =
            parse_experience_config_json(tpl_raw.as_deref(), cfg_raw.as_ref());

        if !is_family_link_authorized(config.features.family_link_enabled, family_share_enabled) {
            continue;
        }

        let prog_uuid: Uuid = row.get("program_uuid");
        let consented_at: DateTime<Utc> = row.get("consented_at");
        let shared_categories: Value = row
            .try_get("shared_categories")
            .unwrap_or_else(|_| json!([]));

        let reminders_enabled = config.features.partner_reminders_enabled;
        let appointments_enabled = config.features.appointments_enabled;

        let reminders = if reminders_enabled {
            match sqlx::query(
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
                     ON rc.reminder_id = r.id AND rc.patient_user_id = $3 \
                  WHERE r.program_id = $1 \
                    AND r.expires_at > NOW() \
                    AND r.created_at >= $2 \
                    AND (r.target_patient_id IS NULL OR r.target_patient_id = $3) \
                    AND (r.target_category IS NULL OR $4::jsonb @> jsonb_build_array(r.target_category)) \
                  ORDER BY r.created_at DESC \
                  LIMIT 15",
            )
            .bind(prog_uuid)
            .bind(consented_at)
            .bind(patient_id)
            .bind(&shared_categories)
            .fetch_all(pool)
            .await
            {
                Ok(r) => r.iter().map(map_enrollment_reminder_row).collect(),
                Err(_) => {
                    return json_error(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Erro ao carregar avisos compartilhados do programa.",
                    )
                }
            }
        } else {
            Vec::new()
        };

        let appointments = if appointments_enabled {
            match sqlx::query(
                "SELECT id::text AS appointment_id, program_id::text AS program_id, patient_user_id, \
                        modality, requested_slot_text, scheduled_at, patient_note, partner_note, \
                        status, created_at, updated_at \
                   FROM program_appointments \
                  WHERE program_id = $1 \
                    AND patient_user_id = $2 \
                    AND status IN ('PENDING', 'RESCHEDULE_REQUESTED', 'CONFIRMED') \
                  ORDER BY CASE status \
                             WHEN 'CONFIRMED' THEN 0 \
                             WHEN 'RESCHEDULE_REQUESTED' THEN 1 \
                             ELSE 2 \
                           END, \
                           COALESCE(scheduled_at, updated_at) ASC \
                  LIMIT 15",
            )
            .bind(prog_uuid)
            .bind(patient_id)
            .fetch_all(pool)
            .await
            {
                Ok(a) => a.iter().map(|r| map_appointment_row(r, false)).collect(),
                Err(_) => {
                    return json_error(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "Erro ao carregar agenda compartilhada do programa.",
                    )
                }
            }
        } else {
            Vec::new()
        };

        programs.push(FamilyProgramOverviewItemDto {
            program_id: row.get("program_id"),
            program_name: row.get("program_name"),
            company_name: row.get("company_name"),
            primary_color: row.get("primary_color"),
            logo_url: row.try_get("logo_url").ok().flatten(),
            template_key: template_key.as_str().to_string(),
            consented_at: consented_at.to_rfc3339(),
            family_share_enabled,
            reminders_enabled,
            appointments_enabled,
            reminders,
            appointments,
        });
    }

    (
        StatusCode::OK,
        Json(json!({
            "patientUserId": patient_id,
            "programCount": programs.len(),
            "programs": programs,
        })),
    )
        .into_response()
}

/// Cuidador autorizado confirma leitura de lembrete do programa para o familiar
/// (`POST /api/b2b/family/patients/:patient_id/programs/:program_id/reminders/:reminder_id/ack`).
pub async fn acknowledge_family_program_reminder_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path((patient_id_raw, program_id_raw, reminder_id_raw)): Path<(String, String, String)>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/family/patients/:patient_id/programs/:program_id/reminders/:reminder_id/ack",
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let patient_id = match validate_patient_id_param(&patient_id_raw) {
        Ok(id) => id,
        Err((code, msg)) => return json_error(code, msg),
    };
    let prog_uuid = match Uuid::parse_str(program_id_raw.trim()) {
        Ok(u) => u,
        Err(_) => {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Identificador do programa inválido.",
            )
        }
    };
    let rem_uuid = match Uuid::parse_str(reminder_id_raw.trim()) {
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

    if let Err(resp) = ensure_family_access(pool, &identity.uid, patient_id).await {
        return resp;
    }

    let ctx_row = match sqlx::query(
        "SELECT pr.approval_status, pr.closed_at, pr.template_key, pr.experience_config, \
                e.status AS enroll_status, \
                COALESCE(e.family_share_enabled, false) AS family_share_enabled, \
                e.consented_at, e.shared_categories \
           FROM program_enrollments e \
           JOIN partner_programs pr ON pr.id = e.program_id \
          WHERE e.program_id = $1 AND e.patient_user_id = $2",
    )
    .bind(prog_uuid)
    .bind(patient_id)
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
                "Erro ao validar vínculo no programa.",
            )
        }
    };

    let enroll_status: String = ctx_row.get("enroll_status");
    if enroll_status != "ACTIVE" {
        return json_error(StatusCode::FORBIDDEN, "Vínculo encerrado ou inativo.");
    }
    let family_share_enabled: bool = ctx_row.try_get("family_share_enabled").unwrap_or(false);
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

    if !is_family_link_authorized(config.features.family_link_enabled, family_share_enabled) {
        return json_error(
            StatusCode::FORBIDDEN,
            "O compartilhamento familiar do programa ainda não foi autorizado pelo participante.",
        );
    }

    let consented_at: DateTime<Utc> = ctx_row.get("consented_at");
    let shared_categories: Value = ctx_row
        .try_get("shared_categories")
        .unwrap_or_else(|_| json!([]));

    let valid_rem = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) \
           FROM program_reminders \
          WHERE id = $1 AND program_id = $2 \
            AND expires_at > NOW() \
            AND created_at >= $3 \
            AND (target_patient_id IS NULL OR target_patient_id = $4) \
            AND (target_category IS NULL OR $5::jsonb @> jsonb_build_array(target_category))",
    )
    .bind(rem_uuid)
    .bind(prog_uuid)
    .bind(consented_at)
    .bind(patient_id)
    .bind(&shared_categories)
    .fetch_one(pool)
    .await
    .unwrap_or(0);

    if valid_rem == 0 {
        return json_error(
            StatusCode::NOT_FOUND,
            "Lembrete não encontrado para este participante.",
        );
    }

    let ack_row = match sqlx::query(
        "INSERT INTO program_reminder_receipts (reminder_id, patient_user_id, acknowledged_by_uid) \
         VALUES ($1, $2, $3) \
         ON CONFLICT (reminder_id, patient_user_id) DO UPDATE \
            SET acknowledged_by_uid = COALESCE(program_reminder_receipts.acknowledged_by_uid, EXCLUDED.acknowledged_by_uid) \
         RETURNING acknowledged_at, acknowledged_by_uid",
    )
    .bind(rem_uuid)
    .bind(patient_id)
    .bind(&identity.uid)
    .fetch_one(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao registrar confirmação familiar de leitura.",
            )
        }
    };

    let ack_at: DateTime<Utc> = ack_row.get("acknowledged_at");
    let ack_by: Option<String> = ack_row.try_get("acknowledged_by_uid").ok().flatten();

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

    if identity.uid != patient_id {
        let _ = record_audit(
            pool,
            Some(prog_uuid),
            &identity.uid,
            patient_id,
            "FAMILY_REMINDER_ACKNOWLEDGED",
            json!({
                "reminderId": rem_uuid.to_string()
            }),
        )
        .await;
    }

    (
        StatusCode::OK,
        Json(json!({
            "reminderId": rem_uuid.to_string(),
            "programId": prog_uuid.to_string(),
            "patientUserId": patient_id,
            "acknowledgedAt": ack_at.to_rfc3339(),
            "acknowledgedByUid": ack_by
        })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn double_key_allows_family_overview_only_when_both_flags_enabled() {
        assert!(is_family_link_authorized(true, true));
        assert!(!is_family_link_authorized(true, false));
        assert!(!is_family_link_authorized(false, true));
        assert!(!is_family_link_authorized(false, false));
    }

    #[test]
    fn patient_id_param_rejects_empty_or_oversized() {
        assert!(validate_patient_id_param("").is_err());
        assert!(validate_patient_id_param("   ").is_err());
        let oversized = "x".repeat(129);
        assert!(validate_patient_id_param(&oversized).is_err());
        assert_eq!(validate_patient_id_param("patient-1").unwrap(), "patient-1");
    }
}
