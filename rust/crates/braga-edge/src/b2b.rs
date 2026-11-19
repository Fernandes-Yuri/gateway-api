//! Motor de dominio B2B 100% em Rust (`braga-edge`):
//! - B2B-04: Resolucao autoritativa e troca de contexto (JWT RS256 -> PostgreSQL)
//! - B2B-07: Onboarding empresarial enxuto, personalizacao e previa de convite
//! - B2B-09: Aprovacao manual assistida (SERVICE_TOKEN, maquina de estados e invite_code)
//! - B2B-10: Vinculo do paciente por codigo e consentimento granular por categoria
//! - B2B-11: Franquia atomica de participantes ativos (SELECT ... FOR UPDATE), pausa e regeneracao de codigo
//! - B2B-12: Projecao autorizada de rotina compartilhada / registros autorizados (Anti-IDOR + paginacao por cursor)
//! - B2B-15: Motor No-Code, 7 Templates de Perfil (`experience_config`), guardrails de modulo e encerramento seguro

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
use sqlx::{PgPool, Row};
use uuid::Uuid;

use crate::{authenticate_request, AppState};

pub const DEFAULT_PRIMARY_COLOR: &str = "#1A6B5C";
pub const DEFAULT_TEMPLATE_KEY: &str = "CLINICA_GERAL";
pub const ALLOWED_SHARED_CATEGORIES: &[&str] =
    &["steps", "hydration", "medications", "authorized_records"];
pub const ALLOWED_APPOINTMENT_MODALITIES: &[&str] = &["IN_PERSON", "REMOTE"];
pub const ALLOWED_BOOKING_MODES: &[&str] = &["REQUEST_CONFIRMATION", "EXTERNAL_LINK_ONLY"];
pub const ALLOWED_AI_ACTIVATION_MODES: &[&str] =
    &["ALWAYS_FIRST", "OUT_OF_HOURS_ONLY", "MANUAL_ONLY"];
const CODE_ALPHABET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TemplateKey {
    ClinicaGeral,
    NutricaoBemEstar,
    FisioReabilitacao,
    CuidadoSeniorHomecare,
    FarmaciaAdesao,
    SaudeCorporativa,
    Custom,
}

impl TemplateKey {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::ClinicaGeral => "CLINICA_GERAL",
            Self::NutricaoBemEstar => "NUTRICAO_BEM_ESTAR",
            Self::FisioReabilitacao => "FISIO_REABILITACAO",
            Self::CuidadoSeniorHomecare => "CUIDADO_SENIOR_HOMECARE",
            Self::FarmaciaAdesao => "FARMACIA_ADESAO",
            Self::SaudeCorporativa => "SAUDE_CORPORATIVA",
            Self::Custom => "CUSTOM",
        }
    }

    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_uppercase().as_str() {
            "CLINICA_GERAL" => Some(Self::ClinicaGeral),
            "NUTRICAO_BEM_ESTAR" => Some(Self::NutricaoBemEstar),
            "FISIO_REABILITACAO" => Some(Self::FisioReabilitacao),
            "CUIDADO_SENIOR_HOMECARE" => Some(Self::CuidadoSeniorHomecare),
            "FARMACIA_ADESAO" => Some(Self::FarmaciaAdesao),
            "SAUDE_CORPORATIVA" => Some(Self::SaudeCorporativa),
            "CUSTOM" => Some(Self::Custom),
            _ => None,
        }
    }

    pub fn all() -> &'static [Self] {
        &[
            Self::ClinicaGeral,
            Self::NutricaoBemEstar,
            Self::FisioReabilitacao,
            Self::CuidadoSeniorHomecare,
            Self::FarmaciaAdesao,
            Self::SaudeCorporativa,
            Self::Custom,
        ]
    }
}

#[allow(dead_code)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProgramModule {
    AssistantChat,
    DirectPartnerChat,
    AiReceptionist,
    FamilyLink,
    PartnerReminders,
    Appointments,
    PreConsultationPdf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ApprovalStatus {
    Draft,
    PendingReview,
    ChangesRequested,
    Approved,
    Rejected,
}

impl ApprovalStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Draft => "DRAFT",
            Self::PendingReview => "PENDING_REVIEW",
            Self::ChangesRequested => "CHANGES_REQUESTED",
            Self::Approved => "APPROVED",
            Self::Rejected => "REJECTED",
        }
    }

    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_uppercase().as_str() {
            "DRAFT" => Some(Self::Draft),
            "PENDING_REVIEW" => Some(Self::PendingReview),
            "CHANGES_REQUESTED" => Some(Self::ChangesRequested),
            "APPROVED" => Some(Self::Approved),
            "REJECTED" => Some(Self::Rejected),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReviewAction {
    Approve,
    RequestChanges,
    Reject,
}

impl ReviewAction {
    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "approve" => Some(Self::Approve),
            "request_changes" => Some(Self::RequestChanges),
            "reject" => Some(Self::Reject),
            _ => None,
        }
    }
}

fn default_schema_version() -> i32 {
    1
}

fn default_template_key() -> String {
    DEFAULT_TEMPLATE_KEY.to_string()
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProgramFeaturesDto {
    #[serde(default = "default_true", alias = "assistant_chat_enabled")]
    pub assistant_chat_enabled: bool,
    #[serde(default, alias = "direct_partner_chat_enabled")]
    pub direct_partner_chat_enabled: bool,
    #[serde(default, alias = "ai_receptionist_enabled")]
    pub ai_receptionist_enabled: bool,
    #[serde(default, alias = "family_link_enabled")]
    pub family_link_enabled: bool,
    #[serde(default = "default_true", alias = "partner_reminders_enabled")]
    pub partner_reminders_enabled: bool,
    #[serde(default = "default_true", alias = "appointments_enabled")]
    pub appointments_enabled: bool,
    #[serde(default = "default_true", alias = "pre_consultation_pdf_enabled")]
    pub pre_consultation_pdf_enabled: bool,
}

impl Default for ProgramFeaturesDto {
    fn default() -> Self {
        Self {
            assistant_chat_enabled: true,
            direct_partner_chat_enabled: false,
            ai_receptionist_enabled: false,
            family_link_enabled: false,
            partner_reminders_enabled: true,
            appointments_enabled: true,
            pre_consultation_pdf_enabled: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DirectChatPolicyDto {
    #[serde(default = "default_service_hours_label", alias = "service_hours_label")]
    pub service_hours_label: String,
    #[serde(default = "default_response_sla_label", alias = "response_sla_label")]
    pub response_sla_label: String,
    #[serde(default = "default_true", alias = "allow_off_hours_queue")]
    pub allow_off_hours_queue: bool,
}

fn default_service_hours_label() -> String {
    "Seg a Sex, 08h às 18h".to_string()
}

fn default_response_sla_label() -> String {
    "Resposta em até 1 dia útil".to_string()
}

impl Default for DirectChatPolicyDto {
    fn default() -> Self {
        Self {
            service_hours_label: default_service_hours_label(),
            response_sla_label: default_response_sla_label(),
            allow_off_hours_queue: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AiReceptionistPolicyDto {
    #[serde(default = "default_ai_persona_name", alias = "persona_name")]
    pub persona_name: String,
    #[serde(default = "default_ai_activation_mode", alias = "activation_mode")]
    pub activation_mode: String,
    #[serde(default = "default_ai_greeting_message", alias = "greeting_message")]
    pub greeting_message: String,
    #[serde(default, alias = "partner_faq_knowledge")]
    pub partner_faq_knowledge: String,
    #[serde(default = "default_true", alias = "auto_escalate_to_human_on_unknown")]
    pub auto_escalate_to_human_on_unknown: bool,
}

fn default_ai_persona_name() -> String {
    "Assistente Virtual do Programa".to_string()
}

fn default_ai_activation_mode() -> String {
    "OUT_OF_HOURS_ONLY".to_string()
}

fn default_ai_greeting_message() -> String {
    "Olá! Sou a recepcionista virtual deste programa. Posso tirar dúvidas sobre horários, funcionamento e como enviar seu resumo — ou chamar o responsável quando precisar.".to_string()
}

impl Default for AiReceptionistPolicyDto {
    fn default() -> Self {
        Self {
            persona_name: default_ai_persona_name(),
            activation_mode: default_ai_activation_mode(),
            greeting_message: default_ai_greeting_message(),
            partner_faq_knowledge: String::new(),
            auto_escalate_to_human_on_unknown: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AppointmentsPolicyDto {
    #[serde(default = "default_allowed_modalities", alias = "allowed_modalities")]
    pub allowed_modalities: Vec<String>,
    #[serde(default = "default_booking_mode", alias = "booking_mode")]
    pub booking_mode: String,
    #[serde(
        default = "default_availabilities_summary",
        alias = "availabilities_summary"
    )]
    pub availabilities_summary: String,
    #[serde(default, alias = "external_booking_url")]
    pub external_booking_url: Option<String>,
}

fn default_allowed_modalities() -> Vec<String> {
    vec!["IN_PERSON".to_string(), "REMOTE".to_string()]
}

fn default_booking_mode() -> String {
    "REQUEST_CONFIRMATION".to_string()
}

fn default_availabilities_summary() -> String {
    "Terças e Quintas, 09h às 17h".to_string()
}

impl Default for AppointmentsPolicyDto {
    fn default() -> Self {
        Self {
            allowed_modalities: default_allowed_modalities(),
            booking_mode: default_booking_mode(),
            availabilities_summary: default_availabilities_summary(),
            external_booking_url: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProgramExperienceConfigDto {
    #[serde(default = "default_schema_version", alias = "schema_version")]
    pub schema_version: i32,
    #[serde(default = "default_template_key", alias = "template_key")]
    pub template_key: String,
    #[serde(alias = "welcome_headline")]
    pub welcome_headline: String,
    #[serde(alias = "welcome_message")]
    pub welcome_message: String,
    #[serde(alias = "enabled_metrics")]
    pub enabled_metrics: Vec<String>,
    #[serde(default)]
    pub features: ProgramFeaturesDto,
    #[serde(default, alias = "direct_chat_policy")]
    pub direct_chat_policy: DirectChatPolicyDto,
    #[serde(default, alias = "ai_receptionist_policy")]
    pub ai_receptionist_policy: AiReceptionistPolicyDto,
    #[serde(default, alias = "appointments_policy")]
    pub appointments_policy: AppointmentsPolicyDto,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TemplateCatalogItemDto {
    pub template_key: String,
    pub title: String,
    pub subtitle: String,
    pub sample_safe_reminder: String,
    pub default_config: ProgramExperienceConfigDto,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateExperienceRequest {
    #[serde(alias = "template_key")]
    pub template_key: Option<String>,
    #[serde(default, alias = "apply_template_defaults")]
    pub apply_template_defaults: bool,
    #[serde(alias = "experience_config")]
    pub experience_config: Option<ProgramExperienceConfigDto>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CloseProgramRequest {
    pub reason: Option<String>,
    #[serde(alias = "transfer_target_user_id")]
    pub transfer_target_user_id: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ContextDescriptor {
    pub mode: String,
    pub user_id: String,
    pub program_id: Option<String>,
    pub program_name: Option<String>,
    pub company_name: Option<String>,
    pub primary_color: Option<String>,
    pub logo_url: Option<String>,
    pub approval_status: Option<String>,
    pub shared_categories: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub template_key: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub experience_config: Option<ProgramExperienceConfigDto>,
    pub family_share_enabled: bool,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SelectContextRequest {
    #[serde(default = "default_personal_mode")]
    pub mode: String,
    pub program_id: Option<String>,
}

fn default_personal_mode() -> String {
    "PERSONAL".to_string()
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpsertPartnerProgramRequest {
    pub company_name: String,
    pub program_name: String,
    pub cnpj: Option<String>,
    pub primary_color: Option<String>,
    pub logo_url: Option<String>,
    #[serde(alias = "template_key")]
    pub template_key: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PartnerProgramDto {
    pub program_id: String,
    pub owner_user_id: String,
    pub company_name: String,
    pub program_name: String,
    pub cnpj: Option<String>,
    pub primary_color: String,
    pub logo_url: Option<String>,
    pub approval_status: String,
    pub review_note: Option<String>,
    pub invite_code: Option<String>,
    pub invite_paused: bool,
    pub seat_limit: i32,
    pub active_participants: i64,
    pub can_promote: bool,
    pub template_key: String,
    pub experience_config: ProgramExperienceConfigDto,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub closed_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reviewed_by: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reviewed_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub updated_at: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct AdminProgramsQuery {
    pub status: Option<String>,
    pub limit: Option<i64>,
    pub offset: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AdminReviewRequest {
    pub action: String,
    pub note: Option<String>,
    pub reviewer: Option<String>,
    pub seat_limit: Option<i32>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct JoinProgramRequest {
    pub invite_code: String,
    #[serde(default)]
    pub shared_categories: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateConsentRequest {
    #[serde(default)]
    pub shared_categories: Vec<String>,
    #[serde(alias = "family_share_enabled")]
    pub family_share_enabled: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateInvitePolicyRequest {
    pub paused: bool,
}

#[derive(Debug, Deserialize)]
pub struct ParticipantsQuery {
    pub limit: Option<i64>,
    pub cursor: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ParticipantSummaryQuery {
    pub days: Option<i32>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PartnerParticipantItemDto {
    pub patient_user_id: String,
    pub display_name: String,
    pub shared_categories: Vec<String>,
    pub consented_at: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SharedStepsDayDto {
    pub date: String,
    pub steps: i32,
    pub active_minutes: i32,
    pub distance_meters: f64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SharedStepsSectionDto {
    pub source: String,
    pub average_steps: i64,
    pub days_recorded: usize,
    pub days: Vec<SharedStepsDayDto>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SharedHydrationDayDto {
    pub date: String,
    pub total_ml: i64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SharedHydrationSectionDto {
    pub source: String,
    pub average_ml: i64,
    pub days_recorded: usize,
    pub days: Vec<SharedHydrationDayDto>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SharedMedicationItemDto {
    pub name: String,
    pub dosage_mg: Option<f64>,
    pub schedule: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SharedMedicationsSectionDto {
    pub source: String,
    pub reminder_count: usize,
    pub user_confirmations_in_period: i64,
    pub note: String,
    pub items: Vec<SharedMedicationItemDto>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SharedVitalEntryDto {
    pub measured_at: String,
    pub origin: String,
    pub systolic_pressure: Option<i32>,
    pub diastolic_pressure: Option<i32>,
    pub heart_rate: Option<i32>,
    pub glucose_level: Option<i32>,
    pub glucose_type: Option<String>,
    pub oxygen_saturation: Option<f64>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SharedAuthorizedRecordsSectionDto {
    pub source: String,
    pub zero_file_egress_notice: String,
    pub records: Vec<SharedVitalEntryDto>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ParticipantRoutineSummaryDto {
    pub program_id: String,
    pub program_name: String,
    pub company_name: String,
    pub patient_user_id: String,
    pub participant_name: String,
    pub period_days: i32,
    pub shared_categories: Vec<String>,
    pub steps_section: Option<SharedStepsSectionDto>,
    pub hydration_section: Option<SharedHydrationSectionDto>,
    pub medications_section: Option<SharedMedicationsSectionDto>,
    pub authorized_records_section: Option<SharedAuthorizedRecordsSectionDto>,
}

fn json_error(status: StatusCode, detail: &str) -> Response {
    (status, Json(json!({ "detail": detail }))).into_response()
}

fn personal_context(uid: &str) -> ContextDescriptor {
    ContextDescriptor {
        mode: "PERSONAL".to_string(),
        user_id: uid.to_string(),
        program_id: None,
        program_name: None,
        company_name: None,
        primary_color: None,
        logo_url: None,
        approval_status: None,
        shared_categories: Vec::new(),
        template_key: None,
        experience_config: None,
        family_share_enabled: false,
    }
}

pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff: u8 = 0;
    for (&x, &y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

fn require_service_token(headers: &HeaderMap) -> Result<(), (StatusCode, &'static str)> {
    let expected = std::env::var("SERVICE_TOKEN")
        .unwrap_or_default()
        .trim()
        .to_string();
    if expected.is_empty() {
        return Err((
            StatusCode::SERVICE_UNAVAILABLE,
            "Autenticação administrativa não configurada.",
        ));
    }
    let auth_hdr = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let provided = auth_hdr
        .strip_prefix("Bearer ")
        .map(str::trim)
        .unwrap_or("");
    if provided.is_empty() || !constant_time_eq(provided.as_bytes(), expected.as_bytes()) {
        return Err((
            StatusCode::UNAUTHORIZED,
            "Token administrativo inválido ou ausente.",
        ));
    }
    Ok(())
}

pub fn normalize_primary_color(raw: Option<&str>) -> Result<String, &'static str> {
    let value = raw
        .unwrap_or(DEFAULT_PRIMARY_COLOR)
        .trim()
        .to_ascii_uppercase();
    let bytes = value.as_bytes();
    if bytes.len() != 7 || bytes[0] != b'#' || !bytes[1..].iter().all(|b| b.is_ascii_hexdigit()) {
        return Err("Cor principal inválida. Use o formato hexadecimal #RRGGBB.");
    }
    Ok(value)
}

pub fn normalize_cnpj(raw: Option<&str>, required: bool) -> Result<Option<String>, &'static str> {
    let cleaned = raw.unwrap_or("").trim();
    if cleaned.is_empty() {
        if required {
            return Err("Informe o CNPJ da empresa para enviar a solicitação para análise.");
        }
        return Ok(None);
    }
    let digits: String = cleaned.chars().filter(|c| c.is_ascii_digit()).collect();
    if digits.len() != 14 {
        return Err("CNPJ inválido. Informe os 14 dígitos numéricos.");
    }
    let first = digits.as_bytes()[0];
    if digits.bytes().all(|b| b == first) {
        return Err("CNPJ inválido. Informe os 14 dígitos numéricos.");
    }
    Ok(Some(format!(
        "{}.{}.{}/{}-{}",
        &digits[0..2],
        &digits[2..5],
        &digits[5..8],
        &digits[8..12],
        &digits[12..14]
    )))
}

pub fn normalize_logo_url(raw: Option<&str>) -> Result<Option<String>, &'static str> {
    let cleaned = raw.unwrap_or("").trim();
    if cleaned.is_empty() {
        return Ok(None);
    }
    if cleaned.len() > 512 || !cleaned.starts_with("https://") {
        return Err("A URL da logo deve utilizar HTTPS.");
    }
    Ok(Some(cleaned.to_string()))
}

pub fn is_valid_invite_code(code: &str) -> bool {
    let len = code.len();
    (3..=32).contains(&len)
        && code
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

pub fn generate_invite_code(program_name: &str) -> String {
    let mut prefix = String::with_capacity(4);
    for ch in program_name.chars() {
        if prefix.len() >= 4 {
            break;
        }
        let mapped = match ch {
            'á' | 'à' | 'â' | 'ã' | 'ä' | 'Á' | 'À' | 'Â' | 'Ã' | 'Ä' => 'A',
            'é' | 'è' | 'ê' | 'ë' | 'É' | 'È' | 'Ê' | 'Ë' => 'E',
            'í' | 'ì' | 'î' | 'ï' | 'Í' | 'Ì' | 'Î' | 'Ï' => 'I',
            'ó' | 'ò' | 'ô' | 'õ' | 'ö' | 'Ó' | 'Ò' | 'Ô' | 'Õ' | 'Ö' => 'O',
            'ú' | 'ù' | 'û' | 'ü' | 'Ú' | 'Ù' | 'Û' | 'Ü' => 'U',
            'ç' | 'Ç' => 'C',
            c if c.is_ascii_alphabetic() => c.to_ascii_uppercase(),
            _ => continue,
        };
        prefix.push(mapped);
    }
    if prefix.is_empty() {
        prefix.push_str("BRAG");
    }
    while prefix.len() < 4 {
        prefix.push('X');
    }
    let random_bytes = Uuid::new_v4();
    let bytes = random_bytes.as_bytes();
    let mut suffix = String::with_capacity(4);
    for b in &bytes[0..4] {
        let idx = (*b as usize) % CODE_ALPHABET.len();
        suffix.push(CODE_ALPHABET[idx] as char);
    }
    format!("{prefix}-{suffix}")
}

pub fn validate_review_transition(
    current: ApprovalStatus,
    action: ReviewAction,
    note: &str,
    seat_limit: Option<i32>,
) -> Result<(ApprovalStatus, &'static str), (StatusCode, &'static str)> {
    if current != ApprovalStatus::PendingReview {
        return Err((
            StatusCode::CONFLICT,
            "Apenas solicitações em análise (PENDING_REVIEW) podem receber decisão.",
        ));
    }
    if note.len() > 600 {
        return Err((
            StatusCode::BAD_REQUEST,
            "A mensagem para o parceiro deve ter no máximo 600 caracteres.",
        ));
    }
    match action {
        ReviewAction::Approve => {
            if let Some(limit) = seat_limit {
                if !(1..=10_000).contains(&limit) {
                    return Err((
                        StatusCode::BAD_REQUEST,
                        "Limite de vagas deve estar entre 1 e 10000.",
                    ));
                }
            }
            Ok((ApprovalStatus::Approved, "PROGRAM_APPROVED"))
        }
        ReviewAction::RequestChanges => {
            if note.len() < 5 {
                return Err((
                    StatusCode::BAD_REQUEST,
                    "Informe uma justificativa (mínimo 5 caracteres) ao solicitar ajustes ou recusar.",
                ));
            }
            Ok((
                ApprovalStatus::ChangesRequested,
                "PROGRAM_CHANGES_REQUESTED",
            ))
        }
        ReviewAction::Reject => {
            if note.len() < 5 {
                return Err((
                    StatusCode::BAD_REQUEST,
                    "Informe uma justificativa (mínimo 5 caracteres) ao solicitar ajustes ou recusar.",
                ));
            }
            Ok((ApprovalStatus::Rejected, "PROGRAM_REJECTED"))
        }
    }
}

pub fn validate_shared_categories(raw: &[String]) -> Result<Vec<String>, &'static str> {
    if raw.is_empty() {
        return Err(
            "Selecione ao menos uma categoria de registros para compartilhar com o programa.",
        );
    }
    let mut normalized = Vec::with_capacity(raw.len());
    for item in raw {
        let key = item.trim().to_ascii_lowercase();
        let Some(&canonical) = ALLOWED_SHARED_CATEGORIES.iter().find(|&&c| c == key) else {
            return Err("Categoria de compartilhamento inválida.");
        };
        let s = canonical.to_string();
        if !normalized.contains(&s) {
            normalized.push(s);
        }
    }
    if normalized.is_empty() {
        return Err(
            "Selecione ao menos uma categoria de registros para compartilhar com o programa.",
        );
    }
    Ok(normalized)
}

pub fn check_enrollment_eligibility(
    approval_status: ApprovalStatus,
    invite_paused: bool,
    owner_user_id: &str,
    patient_user_id: &str,
    already_active: bool,
    active_count: i64,
    seat_limit: i32,
) -> Result<(), (StatusCode, &'static str)> {
    if approval_status != ApprovalStatus::Approved {
        return Err((
            StatusCode::FORBIDDEN,
            "Este programa ainda não está disponível para adesão.",
        ));
    }
    if owner_user_id == patient_user_id {
        return Err((
            StatusCode::BAD_REQUEST,
            "Você já é o responsável por este programa e não ocupa vaga de participante.",
        ));
    }
    if !already_active {
        if invite_paused {
            return Err((
                StatusCode::CONFLICT,
                "Novas adesões estão temporariamente pausadas pelo responsável do programa.",
            ));
        }
        if active_count >= i64::from(seat_limit) {
            return Err((
                StatusCode::CONFLICT,
                "O programa atingiu o limite de participantes da franquia atual.",
            ));
        }
    }
    Ok(())
}

pub fn parse_cursor_and_limit(
    cursor: Option<&str>,
    limit: Option<i64>,
) -> (Option<DateTime<Utc>>, i64) {
    let clamped_limit = limit.unwrap_or(20).clamp(1, 50);
    let parsed_cursor = cursor
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
        .map(|dt| dt.with_timezone(&Utc));
    (parsed_cursor, clamped_limit)
}

pub fn clamp_summary_days(days: Option<i32>) -> i32 {
    days.unwrap_or(30).clamp(1, 90)
}

pub fn check_partner_summary_access(
    program_status: ApprovalStatus,
    program_owner: &str,
    requester_uid: &str,
    enrollment_status: Option<&str>,
) -> Result<(), (StatusCode, &'static str)> {
    if program_owner != requester_uid || program_status != ApprovalStatus::Approved {
        return Err((
            StatusCode::FORBIDDEN,
            "Acesso restrito ao responsável de programa aprovado.",
        ));
    }
    if enrollment_status != Some("ACTIVE") {
        return Err((
            StatusCode::FORBIDDEN,
            "Participante sem vínculo ativo neste programa.",
        ));
    }
    Ok(())
}

pub fn format_confirmed_via_label(raw: Option<&str>) -> &'static str {
    match raw.unwrap_or("app").trim().to_ascii_lowercase().as_str() {
        "braga.voice" => "Braga Voz (autoinformado)",
        "braga.manual" => "Registro manual (autoinformado)",
        "whatsapp" | "braga.whatsapp" => "Canal externo (autoinformado)",
        "web" => "Portal Web (autoinformado)",
        _ => "Braga Saúde (autoinformado)",
    }
}

pub fn default_experience_config_for_template(template: TemplateKey) -> ProgramExperienceConfigDto {
    let (headline, message, metrics, features) = match template {
        TemplateKey::ClinicaGeral => (
            "Seu espaço de acompanhamento entre consultas",
            "Organize passos, hidratação e registros de rotina para compartilhar apenas o que desejar antes do retorno.",
            vec!["steps", "hydration", "medications", "authorized_records"],
            ProgramFeaturesDto {
                assistant_chat_enabled: true,
                direct_partner_chat_enabled: false,
                ai_receptionist_enabled: false,
                family_link_enabled: false,
                partner_reminders_enabled: true,
                appointments_enabled: true,
                pre_consultation_pdf_enabled: true,
            },
        ),
        TemplateKey::NutricaoBemEstar => (
            "Hábitos diários mais leves e constantes",
            "Acompanhe sua hidratação, movimento e registros autorizados no seu ritmo, mantendo o controle do que compartilha.",
            vec!["hydration", "steps", "authorized_records"],
            ProgramFeaturesDto {
                assistant_chat_enabled: true,
                direct_partner_chat_enabled: true,
                ai_receptionist_enabled: false,
                family_link_enabled: false,
                partner_reminders_enabled: true,
                appointments_enabled: true,
                pre_consultation_pdf_enabled: true,
            },
        ),
        TemplateKey::FisioReabilitacao => (
            "Sua rotina de movimento passo a passo",
            "Registre sua movimentação diária e marque horários de retorno combinados com o programa.",
            vec!["steps", "medications", "authorized_records"],
            ProgramFeaturesDto {
                assistant_chat_enabled: true,
                direct_partner_chat_enabled: true,
                ai_receptionist_enabled: false,
                family_link_enabled: false,
                partner_reminders_enabled: true,
                appointments_enabled: true,
                pre_consultation_pdf_enabled: true,
            },
        ),
        TemplateKey::CuidadoSeniorHomecare => (
            "Cuidado conectado com quem apoia você",
            "Com sua permissão, seu familiar de apoio também acompanha os lembretes e agendamentos do programa.",
            vec!["steps", "hydration", "medications", "authorized_records"],
            ProgramFeaturesDto {
                assistant_chat_enabled: true,
                direct_partner_chat_enabled: true,
                ai_receptionist_enabled: false,
                family_link_enabled: true,
                partner_reminders_enabled: true,
                appointments_enabled: true,
                pre_consultation_pdf_enabled: true,
            },
        ),
        TemplateKey::FarmaciaAdesao => (
            "Lembretes e organização da sua rotina",
            "Use o alarme pessoal do seu app no dia a dia; aqui combinamos avisos simples de acompanhamento e apoio.",
            vec!["medications", "hydration"],
            ProgramFeaturesDto {
                assistant_chat_enabled: true,
                direct_partner_chat_enabled: false,
                ai_receptionist_enabled: false,
                family_link_enabled: true,
                partner_reminders_enabled: true,
                appointments_enabled: false,
                pre_consultation_pdf_enabled: true,
            },
        ),
        TemplateKey::SaudeCorporativa => (
            "Bem-estar no dia a dia, com privacidade",
            "Este espaço acompanha apenas hábitos de rotina que você escolher compartilhar (passos e hidratação).",
            vec!["steps", "hydration"],
            ProgramFeaturesDto {
                assistant_chat_enabled: true,
                direct_partner_chat_enabled: false,
                ai_receptionist_enabled: false,
                family_link_enabled: false,
                partner_reminders_enabled: true,
                appointments_enabled: false,
                pre_consultation_pdf_enabled: false,
            },
        ),
        TemplateKey::Custom => (
            "Bem-vindo ao nosso espaço de acompanhamento",
            "Personalizamos os recursos abaixo para facilitar sua rotina. Você escolhe o que compartilhar a qualquer momento.",
            vec!["steps", "hydration"],
            ProgramFeaturesDto {
                assistant_chat_enabled: true,
                direct_partner_chat_enabled: false,
                ai_receptionist_enabled: false,
                family_link_enabled: false,
                partner_reminders_enabled: false,
                appointments_enabled: false,
                pre_consultation_pdf_enabled: true,
            },
        ),
    };

    ProgramExperienceConfigDto {
        schema_version: 1,
        template_key: template.as_str().to_string(),
        welcome_headline: headline.to_string(),
        welcome_message: message.to_string(),
        enabled_metrics: metrics.into_iter().map(ToString::to_string).collect(),
        features,
        direct_chat_policy: DirectChatPolicyDto::default(),
        ai_receptionist_policy: AiReceptionistPolicyDto::default(),
        appointments_policy: AppointmentsPolicyDto::default(),
    }
}

pub fn template_catalog() -> Vec<TemplateCatalogItemDto> {
    TemplateKey::all()
        .iter()
        .copied()
        .map(|tk| {
            let (title, subtitle, sample_reminder) = match tk {
                TemplateKey::ClinicaGeral => (
                    "Clínica Geral e Ambulatório",
                    "Consultórios médicos e clínicas ambulatoriais",
                    "Prepare seu Resumo em PDF 1 dia antes da próxima visita para revisarmos sua rotina juntos.",
                ),
                TemplateKey::NutricaoBemEstar => (
                    "Nutrição e Bem-Estar",
                    "Nutricionistas, hábitos e estilo de vida",
                    "Como está sua meta de água hoje? Lembre-se de registrar seus copos ao longo do dia.",
                ),
                TemplateKey::FisioReabilitacao => (
                    "Fisioterapia e Reabilitação",
                    "Fisioterapia, ortopedia e reabilitação física",
                    "Antes do próximo encontro, atualize sua contagem de passos da semana no app.",
                ),
                TemplateKey::CuidadoSeniorHomecare => (
                    "Geriatria e Home Care",
                    "Geriatria, Home Care e cuidado assistido",
                    "Lembrete de preparo: confirme com seu familiar o melhor horário para nossa próxima visita.",
                ),
                TemplateKey::FarmaciaAdesao => (
                    "Farmácia e Adesão à Rotina",
                    "Farmácias clínicas e programas de adesão a lembretes",
                    "Que tal revisar hoje se os horários dos seus lembretes pessoais estão confortáveis para sua semana?",
                ),
                TemplateKey::SaudeCorporativa => (
                    "Saúde Corporativa e RH",
                    "Empresas / RH (bem-estar e hábitos preventivos)",
                    "Desafio leve da semana: faça pequenas pausas para se hidratar e caminhar 10 minutos após o almoço.",
                ),
                TemplateKey::Custom => (
                    "Personalizado do Zero",
                    "Configuração livre do zero",
                    "Novo aviso do programa disponível — toque para confirmar a leitura quando puder.",
                ),
            };
            TemplateCatalogItemDto {
                template_key: tk.as_str().to_string(),
                title: title.to_string(),
                subtitle: subtitle.to_string(),
                sample_safe_reminder: sample_reminder.to_string(),
                default_config: default_experience_config_for_template(tk),
            }
        })
        .collect()
}

fn contains_dangerous_markup_or_scheme(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    text.chars()
        .any(|c| c.is_control() && c != '\n' && c != '\r' && c != '\t')
        || text.contains('<')
        || text.contains('>')
        || lower.contains("javascript:")
        || lower.contains("vbscript:")
        || lower.contains("data:text/html")
        || lower.contains("intent:")
        || lower.contains("file://")
}

pub fn sanitize_plain_text_field(
    raw: &str,
    min_len: usize,
    max_len: usize,
    err_msg: &'static str,
) -> Result<String, &'static str> {
    let trimmed = raw.trim();
    let count = trimmed.chars().count();
    if count < min_len || count > max_len || contains_dangerous_markup_or_scheme(trimmed) {
        return Err(err_msg);
    }
    Ok(trimmed.to_string())
}

pub fn validate_and_normalize_experience_config(
    raw: ProgramExperienceConfigDto,
) -> Result<ProgramExperienceConfigDto, &'static str> {
    let Some(tk) = TemplateKey::parse(&raw.template_key) else {
        return Err("Template do programa inválido.");
    };

    let welcome_headline = sanitize_plain_text_field(
        &raw.welcome_headline,
        2,
        140,
        "Título de boas-vindas inválido (use entre 2 e 140 caracteres sem tags ou scripts).",
    )?;

    let welcome_message = sanitize_plain_text_field(
        &raw.welcome_message,
        2,
        500,
        "Mensagem de boas-vindas inválida (use entre 2 e 500 caracteres sem tags ou scripts).",
    )?;

    let enabled_metrics = validate_shared_categories(&raw.enabled_metrics)?;

    if raw.features.ai_receptionist_enabled && !raw.features.direct_partner_chat_enabled {
        return Err("Ative o Chat Direto com o responsável antes de habilitar a Recepcionista IA.");
    }

    let service_hours_label = sanitize_plain_text_field(
        &raw.direct_chat_policy.service_hours_label,
        2,
        80,
        "Horário de atendimento inválido (entre 2 e 80 caracteres sem tags).",
    )?;
    let response_sla_label = sanitize_plain_text_field(
        &raw.direct_chat_policy.response_sla_label,
        2,
        80,
        "Expectativa de resposta inválida (entre 2 e 80 caracteres sem tags).",
    )?;

    let persona_name = sanitize_plain_text_field(
        &raw.ai_receptionist_policy.persona_name,
        2,
        60,
        "Nome da Recepcionista IA inválido (entre 2 e 60 caracteres sem tags).",
    )?;
    let activation_upper = raw
        .ai_receptionist_policy
        .activation_mode
        .trim()
        .to_ascii_uppercase();
    let Some(&activation_mode) = ALLOWED_AI_ACTIVATION_MODES
        .iter()
        .find(|&&m| m == activation_upper)
    else {
        return Err("Modo de ativação da Recepcionista IA inválido.");
    };
    let greeting_message = sanitize_plain_text_field(
        &raw.ai_receptionist_policy.greeting_message,
        2,
        400,
        "Saudação da Recepcionista IA inválida (entre 2 e 400 caracteres sem tags).",
    )?;
    let partner_faq_knowledge = sanitize_plain_text_field(
        &raw.ai_receptionist_policy.partner_faq_knowledge,
        0,
        1500,
        "FAQ da Recepcionista IA inválido (máximo de 1500 caracteres sem tags HTML ou scripts).",
    )?;

    if raw.appointments_policy.allowed_modalities.is_empty() {
        return Err("Selecione ao menos uma modalidade de atendimento (IN_PERSON ou REMOTE).");
    }
    let mut allowed_modalities = Vec::new();
    for m in &raw.appointments_policy.allowed_modalities {
        let key = m.trim().to_ascii_uppercase();
        let Some(&canonical) = ALLOWED_APPOINTMENT_MODALITIES.iter().find(|&&c| c == key) else {
            return Err("Modalidade de agendamento inválida.");
        };
        let s = canonical.to_string();
        if !allowed_modalities.contains(&s) {
            allowed_modalities.push(s);
        }
    }

    let booking_upper = raw
        .appointments_policy
        .booking_mode
        .trim()
        .to_ascii_uppercase();
    let Some(&booking_mode) = ALLOWED_BOOKING_MODES.iter().find(|&&m| m == booking_upper) else {
        return Err("Modo de agendamento inválido.");
    };

    let availabilities_summary = sanitize_plain_text_field(
        &raw.appointments_policy.availabilities_summary,
        2,
        160,
        "Resumo de disponibilidade inválido (entre 2 e 160 caracteres sem tags).",
    )?;

    let external_booking_url = match raw.appointments_policy.external_booking_url.as_deref() {
        Some(u) if !u.trim().is_empty() => {
            let trimmed = u.trim();
            if trimmed.len() > 512
                || !trimmed.starts_with("https://")
                || trimmed.chars().any(char::is_whitespace)
                || contains_dangerous_markup_or_scheme(trimmed)
            {
                return Err("A URL externa de agendamento deve utilizar HTTPS válido.");
            }
            Some(trimmed.to_string())
        }
        _ => None,
    };

    if raw.features.appointments_enabled
        && booking_mode == "EXTERNAL_LINK_ONLY"
        && external_booking_url.is_none()
    {
        return Err(
            "Informe uma URL HTTPS válida quando o agendamento operar apenas por link externo.",
        );
    }

    Ok(ProgramExperienceConfigDto {
        schema_version: 1,
        template_key: tk.as_str().to_string(),
        welcome_headline,
        welcome_message,
        enabled_metrics,
        features: raw.features,
        direct_chat_policy: DirectChatPolicyDto {
            service_hours_label,
            response_sla_label,
            allow_off_hours_queue: raw.direct_chat_policy.allow_off_hours_queue,
        },
        ai_receptionist_policy: AiReceptionistPolicyDto {
            persona_name,
            activation_mode: activation_mode.to_string(),
            greeting_message,
            partner_faq_knowledge,
            auto_escalate_to_human_on_unknown: raw
                .ai_receptionist_policy
                .auto_escalate_to_human_on_unknown,
        },
        appointments_policy: AppointmentsPolicyDto {
            allowed_modalities,
            booking_mode: booking_mode.to_string(),
            availabilities_summary,
            external_booking_url,
        },
    })
}

#[allow(dead_code)]
pub fn guard_program_module(
    approval_status: ApprovalStatus,
    closed_at_present: bool,
    config: &ProgramExperienceConfigDto,
    module: ProgramModule,
) -> Result<(), (StatusCode, &'static str)> {
    if closed_at_present || approval_status != ApprovalStatus::Approved {
        return Err((
            StatusCode::FORBIDDEN,
            "Este programa não está ativo ou aprovado.",
        ));
    }
    let enabled = match module {
        ProgramModule::AssistantChat => config.features.assistant_chat_enabled,
        ProgramModule::DirectPartnerChat => config.features.direct_partner_chat_enabled,
        ProgramModule::AiReceptionist => {
            config.features.direct_partner_chat_enabled && config.features.ai_receptionist_enabled
        }
        ProgramModule::FamilyLink => config.features.family_link_enabled,
        ProgramModule::PartnerReminders => config.features.partner_reminders_enabled,
        ProgramModule::Appointments => config.features.appointments_enabled,
        ProgramModule::PreConsultationPdf => config.features.pre_consultation_pdf_enabled,
    };
    if !enabled {
        return Err((
            StatusCode::CONFLICT,
            "Este recurso foi desativado pelo responsável do programa.",
        ));
    }
    Ok(())
}

pub fn intersect_effective_categories(
    enabled_metrics: &[String],
    shared_categories: &[String],
) -> Vec<String> {
    let mut effective = Vec::new();
    for metric in enabled_metrics {
        let key = metric.trim().to_ascii_lowercase();
        if ALLOWED_SHARED_CATEGORIES.contains(&key.as_str())
            && shared_categories
                .iter()
                .any(|c| c.trim().eq_ignore_ascii_case(&key))
            && !effective.contains(&key)
        {
            effective.push(key);
        }
    }
    effective
}

pub fn check_approved_identity_mutation(
    status: ApprovalStatus,
    existing_company: &str,
    new_company: &str,
    existing_cnpj: Option<&str>,
    new_cnpj: Option<&str>,
) -> Result<(), (StatusCode, &'static str)> {
    if status == ApprovalStatus::Approved {
        let company_changed = existing_company.trim() != new_company.trim();
        let cnpj_changed = existing_cnpj.map(str::trim) != new_cnpj.map(str::trim);
        if company_changed || cnpj_changed {
            return Err((
                StatusCode::CONFLICT,
                "Razão social e CNPJ de programa aprovado exigem nova revisão pela equipe Braga.",
            ));
        }
    }
    Ok(())
}

fn parse_experience_config_json(
    raw_template_key: Option<&str>,
    raw_config: Option<&Value>,
) -> (String, ProgramExperienceConfigDto) {
    let tk = raw_template_key
        .and_then(TemplateKey::parse)
        .unwrap_or(TemplateKey::ClinicaGeral);
    if let Some(val) = raw_config {
        if let Ok(mut parsed) = serde_json::from_value::<ProgramExperienceConfigDto>(val.clone()) {
            parsed.template_key = tk.as_str().to_string();
            if let Ok(normalized) = validate_and_normalize_experience_config(parsed.clone()) {
                return (tk.as_str().to_string(), normalized);
            }
            return (tk.as_str().to_string(), parsed);
        }
    }
    (
        tk.as_str().to_string(),
        default_experience_config_for_template(tk),
    )
}

fn require_pool(state: &AppState) -> Result<&PgPool, (StatusCode, &'static str)> {
    state.pool.as_ref().ok_or((
        StatusCode::SERVICE_UNAVAILABLE,
        "Banco de dados não disponível.",
    ))
}

fn parse_categories_json(raw: &Value) -> Vec<String> {
    raw.as_array()
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(ToString::to_string))
                .collect()
        })
        .unwrap_or_default()
}

async fn count_active_participants(pool: &PgPool, program_id: Uuid) -> Result<i64, sqlx::Error> {
    let count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM program_enrollments WHERE program_id = $1 AND status = 'ACTIVE'",
    )
    .bind(program_id)
    .fetch_one(pool)
    .await?;
    Ok(count)
}

async fn record_audit(
    executor: impl sqlx::Executor<'_, Database = sqlx::Postgres>,
    program_id: Option<Uuid>,
    actor_user_id: &str,
    target_user_id: &str,
    action: &str,
    metadata: Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO program_audit_logs (program_id, actor_user_id, target_user_id, action, metadata) \
         VALUES ($1, $2, $3, $4, $5)",
    )
    .bind(program_id)
    .bind(actor_user_id)
    .bind(target_user_id)
    .bind(action)
    .bind(metadata)
    .execute(executor)
    .await?;
    Ok(())
}

// =========================================================================
// B2B-04: Identidade & Troca de Contexto (JWT RS256 -> PostgreSQL)
// =========================================================================

pub async fn get_contexts_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Response {
    let identity = match authenticate_request(&headers, &state, "GET", "/api/b2b/contexts").await {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let mut contexts = vec![personal_context(&identity.uid)];

    let owner_rows = match sqlx::query(
        "SELECT id::text AS id, program_name, company_name, primary_color, logo_url, \
                approval_status, template_key, experience_config \
           FROM partner_programs \
          WHERE owner_user_id = $1 \
            AND closed_at IS NULL \
          ORDER BY created_at ASC",
    )
    .bind(&identity.uid)
    .fetch_all(pool)
    .await
    {
        Ok(rows) => rows,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar programas do usuário.",
            )
        }
    };

    for row in owner_rows {
        let raw_tk: Option<String> = row.try_get("template_key").ok();
        let raw_cfg: Option<Value> = row.try_get("experience_config").ok();
        let (tk, cfg) = parse_experience_config_json(raw_tk.as_deref(), raw_cfg.as_ref());
        contexts.push(ContextDescriptor {
            mode: "PARTNER_OWNER".to_string(),
            user_id: identity.uid.clone(),
            program_id: Some(row.get("id")),
            program_name: Some(row.get("program_name")),
            company_name: Some(row.get("company_name")),
            primary_color: Some(row.get("primary_color")),
            logo_url: row.get("logo_url"),
            approval_status: Some(row.get("approval_status")),
            shared_categories: Vec::new(),
            template_key: Some(tk),
            experience_config: Some(cfg),
            family_share_enabled: false,
        });
    }

    let participant_rows = match sqlx::query(
        "SELECT p.id::text AS id, p.program_name, p.company_name, p.primary_color, p.logo_url, \
                p.approval_status, p.template_key, p.experience_config, \
                e.shared_categories, e.family_share_enabled \
           FROM program_enrollments e \
           JOIN partner_programs p ON p.id = e.program_id \
          WHERE e.patient_user_id = $1 \
            AND e.status = 'ACTIVE' \
            AND p.approval_status = 'APPROVED' \
            AND p.closed_at IS NULL \
          ORDER BY e.consented_at ASC",
    )
    .bind(&identity.uid)
    .fetch_all(pool)
    .await
    {
        Ok(rows) => rows,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar vínculos do usuário.",
            )
        }
    };

    for row in participant_rows {
        let cats: Value = row.get("shared_categories");
        let raw_tk: Option<String> = row.try_get("template_key").ok();
        let raw_cfg: Option<Value> = row.try_get("experience_config").ok();
        let (tk, cfg) = parse_experience_config_json(raw_tk.as_deref(), raw_cfg.as_ref());
        let family_share: bool = row.try_get("family_share_enabled").unwrap_or(false);
        contexts.push(ContextDescriptor {
            mode: "PARTNER_PARTICIPANT".to_string(),
            user_id: identity.uid.clone(),
            program_id: Some(row.get("id")),
            program_name: Some(row.get("program_name")),
            company_name: Some(row.get("company_name")),
            primary_color: Some(row.get("primary_color")),
            logo_url: row.get("logo_url"),
            approval_status: Some(row.get("approval_status")),
            shared_categories: parse_categories_json(&cats),
            template_key: Some(tk),
            experience_config: Some(cfg),
            family_share_enabled: family_share,
        });
    }

    (
        StatusCode::OK,
        Json(json!({
            "defaultMode": "PERSONAL",
            "contexts": contexts
        })),
    )
        .into_response()
}

pub async fn select_context_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<SelectContextRequest>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "POST", "/api/b2b/contexts/select").await {
            Ok(id) => id,
            Err(resp) => return resp,
        };
    let mode = req.mode.trim().to_ascii_uppercase();
    if mode == "PERSONAL" {
        return (
            StatusCode::OK,
            Json(json!({ "activeContext": personal_context(&identity.uid) })),
        )
            .into_response();
    }
    if mode != "PARTNER_OWNER" && mode != "PARTNER_PARTICIPANT" {
        return json_error(StatusCode::BAD_REQUEST, "Modo de contexto inválido.");
    }

    let raw_id = req.program_id.unwrap_or_default();
    let program_uuid = match Uuid::parse_str(raw_id.trim()) {
        Ok(id) => id,
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

    let resolved: Option<ContextDescriptor> = if mode == "PARTNER_OWNER" {
        sqlx::query(
            "SELECT id::text AS id, program_name, company_name, primary_color, logo_url, \
                    approval_status, template_key, experience_config \
               FROM partner_programs \
              WHERE id = $1 AND owner_user_id = $2 AND closed_at IS NULL",
        )
        .bind(program_uuid)
        .bind(&identity.uid)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .map(|row| {
            let raw_tk: Option<String> = row.try_get("template_key").ok();
            let raw_cfg: Option<Value> = row.try_get("experience_config").ok();
            let (tk, cfg) = parse_experience_config_json(raw_tk.as_deref(), raw_cfg.as_ref());
            ContextDescriptor {
                mode: "PARTNER_OWNER".to_string(),
                user_id: identity.uid.clone(),
                program_id: Some(row.get("id")),
                program_name: Some(row.get("program_name")),
                company_name: Some(row.get("company_name")),
                primary_color: Some(row.get("primary_color")),
                logo_url: row.get("logo_url"),
                approval_status: Some(row.get("approval_status")),
                shared_categories: Vec::new(),
                template_key: Some(tk),
                experience_config: Some(cfg),
                family_share_enabled: false,
            }
        })
    } else {
        sqlx::query(
            "SELECT p.id::text AS id, p.program_name, p.company_name, p.primary_color, p.logo_url, \
                    p.approval_status, p.template_key, p.experience_config, \
                    e.shared_categories, e.family_share_enabled \
               FROM program_enrollments e \
               JOIN partner_programs p ON p.id = e.program_id \
              WHERE e.program_id = $1 AND e.patient_user_id = $2 \
                AND e.status = 'ACTIVE' AND p.approval_status = 'APPROVED' AND p.closed_at IS NULL",
        )
        .bind(program_uuid)
        .bind(&identity.uid)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .map(|row| {
            let cats: Value = row.get("shared_categories");
            let raw_tk: Option<String> = row.try_get("template_key").ok();
            let raw_cfg: Option<Value> = row.try_get("experience_config").ok();
            let (tk, cfg) = parse_experience_config_json(raw_tk.as_deref(), raw_cfg.as_ref());
            let family_share: bool = row.try_get("family_share_enabled").unwrap_or(false);
            ContextDescriptor {
                mode: "PARTNER_PARTICIPANT".to_string(),
                user_id: identity.uid.clone(),
                program_id: Some(row.get("id")),
                program_name: Some(row.get("program_name")),
                company_name: Some(row.get("company_name")),
                primary_color: Some(row.get("primary_color")),
                logo_url: row.get("logo_url"),
                approval_status: Some(row.get("approval_status")),
                shared_categories: parse_categories_json(&cats),
                template_key: Some(tk),
                experience_config: Some(cfg),
                family_share_enabled: family_share,
            }
        })
    };

    let Some(ctx) = resolved else {
        return json_error(
            StatusCode::FORBIDDEN,
            "Acesso não autorizado a este programa.",
        );
    };

    let _ = record_audit(
        pool,
        Some(program_uuid),
        &identity.uid,
        &identity.uid,
        "CONTEXT_SELECTED",
        json!({ "mode": ctx.mode }),
    )
    .await;

    (StatusCode::OK, Json(json!({ "activeContext": ctx }))).into_response()
}

// =========================================================================
// B2B-07 & B2B-15: Onboarding Empresarial, No-Code Experience & Previa
// =========================================================================

fn map_program_row(
    row: &sqlx::postgres::PgRow,
    active_participants: i64,
    include_admin_meta: bool,
) -> PartnerProgramDto {
    let status: String = row.get("approval_status");
    let invite_paused: bool = row.get("invite_paused");
    let raw_invite: Option<String> = row.get("invite_code");
    let closed_dt: Option<DateTime<Utc>> = row.try_get("closed_at").ok().flatten();
    let closed_at = closed_dt.map(|d| d.to_rfc3339());
    let visible_invite = if (include_admin_meta || status == "APPROVED") && closed_at.is_none() {
        raw_invite
    } else {
        None
    };

    let raw_tk: Option<String> = row.try_get("template_key").ok();
    let raw_cfg: Option<Value> = row.try_get("experience_config").ok();
    let (template_key, experience_config) =
        parse_experience_config_json(raw_tk.as_deref(), raw_cfg.as_ref());

    let (reviewed_by, reviewed_at, created_at, updated_at) = if include_admin_meta {
        let rev_at: Option<DateTime<Utc>> = row.try_get("reviewed_at").ok().flatten();
        let cre_at: Option<DateTime<Utc>> = row.try_get("created_at").ok().flatten();
        let upd_at: Option<DateTime<Utc>> = row.try_get("updated_at").ok().flatten();
        (
            row.try_get("reviewed_by").ok().flatten(),
            rev_at.map(|d| d.to_rfc3339()),
            cre_at.map(|d| d.to_rfc3339()),
            upd_at.map(|d| d.to_rfc3339()),
        )
    } else {
        (None, None, None, None)
    };

    PartnerProgramDto {
        program_id: row.get("id"),
        owner_user_id: row.get("owner_user_id"),
        company_name: row.get("company_name"),
        program_name: row.get("program_name"),
        cnpj: row.get("cnpj"),
        primary_color: row.get("primary_color"),
        logo_url: row.get("logo_url"),
        can_promote: status == "APPROVED" && !invite_paused && closed_at.is_none(),
        approval_status: status,
        review_note: row.get("review_note"),
        invite_code: visible_invite,
        invite_paused,
        seat_limit: row.get("seat_limit"),
        active_participants,
        template_key,
        experience_config,
        closed_at,
        reviewed_by,
        reviewed_at,
        created_at,
        updated_at,
    }
}

pub async fn get_my_program_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Response {
    let identity = match authenticate_request(&headers, &state, "GET", "/api/b2b/program/me").await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let row = match sqlx::query(
        "SELECT id::text AS id, owner_user_id, company_name, program_name, cnpj, primary_color, \
                logo_url, approval_status, review_note, invite_code, invite_paused, seat_limit, \
                template_key, experience_config, closed_at \
           FROM partner_programs \
          WHERE owner_user_id = $1 AND closed_at IS NULL",
    )
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar programa.",
            )
        }
    };

    let Some(row) = row else {
        return (StatusCode::OK, Json(json!({ "program": Value::Null }))).into_response();
    };
    let id_str: String = row.get("id");
    let active = match Uuid::parse_str(&id_str) {
        Ok(u) => count_active_participants(pool, u).await.unwrap_or(0),
        Err(_) => 0,
    };
    let dto = map_program_row(&row, active, false);
    (StatusCode::OK, Json(json!({ "program": dto }))).into_response()
}

pub async fn save_my_program_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<UpsertPartnerProgramRequest>,
) -> Response {
    let identity = match authenticate_request(&headers, &state, "POST", "/api/b2b/program/me").await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let clean_company = req.company_name.trim();
    let clean_program = req.program_name.trim();
    if !(2..=160).contains(&clean_company.chars().count()) {
        return json_error(
            StatusCode::BAD_REQUEST,
            "Informe o nome da empresa (entre 2 e 160 caracteres).",
        );
    }
    if !(2..=120).contains(&clean_program.chars().count()) {
        return json_error(
            StatusCode::BAD_REQUEST,
            "Informe o nome do programa (entre 2 e 120 caracteres).",
        );
    }
    let clean_color = match normalize_primary_color(req.primary_color.as_deref()) {
        Ok(c) => c,
        Err(msg) => return json_error(StatusCode::BAD_REQUEST, msg),
    };
    let clean_cnpj = match normalize_cnpj(req.cnpj.as_deref(), false) {
        Ok(c) => c,
        Err(msg) => return json_error(StatusCode::BAD_REQUEST, msg),
    };
    let clean_logo = match normalize_logo_url(req.logo_url.as_deref()) {
        Ok(l) => l,
        Err(msg) => return json_error(StatusCode::BAD_REQUEST, msg),
    };
    let requested_template = match req.template_key.as_deref() {
        Some(raw) if !raw.trim().is_empty() => match TemplateKey::parse(raw) {
            Some(tk) => Some(tk),
            None => return json_error(StatusCode::BAD_REQUEST, "Template do programa inválido."),
        },
        _ => None,
    };

    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let existing = match sqlx::query(
        "SELECT id, approval_status, company_name, cnpj, closed_at, template_key, experience_config \
           FROM partner_programs \
          WHERE owner_user_id = $1",
    )
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar programa existente.",
            )
        }
    };

    let (row, audit_action, prev_status) = if let Some(ex) = existing {
        let current_status_str: String = ex.get("approval_status");
        let closed_at: Option<DateTime<Utc>> = ex.try_get("closed_at").ok().flatten();
        let current_status = if closed_at.is_some() {
            ApprovalStatus::Draft
        } else {
            ApprovalStatus::parse(&current_status_str).unwrap_or(ApprovalStatus::Draft)
        };
        let existing_company: String = ex.get("company_name");
        let existing_cnpj: Option<String> = ex.get("cnpj");

        if let Err((code, msg)) = check_approved_identity_mutation(
            current_status,
            &existing_company,
            clean_company,
            existing_cnpj.as_deref(),
            clean_cnpj.as_deref(),
        ) {
            return json_error(code, msg);
        }

        let next_status = if current_status == ApprovalStatus::Approved {
            "APPROVED"
        } else {
            "DRAFT"
        };

        let existing_tk_raw: Option<String> = ex.try_get("template_key").ok();
        let existing_cfg_raw: Option<Value> = ex.try_get("experience_config").ok();
        let (next_tk, next_cfg_val) = if let Some(new_tk) = requested_template {
            let cfg = default_experience_config_for_template(new_tk);
            (
                new_tk.as_str().to_string(),
                serde_json::to_value(cfg).unwrap_or_else(|_| json!({})),
            )
        } else {
            let (tk_s, cfg) =
                parse_experience_config_json(existing_tk_raw.as_deref(), existing_cfg_raw.as_ref());
            (
                tk_s,
                serde_json::to_value(cfg).unwrap_or_else(|_| json!({})),
            )
        };

        let updated = match sqlx::query(
            "UPDATE partner_programs \
                SET company_name = $2, program_name = $3, cnpj = $4, primary_color = $5, \
                    logo_url = $6, approval_status = $7, template_key = $8, \
                    experience_config = $9, closed_at = NULL, updated_at = NOW() \
              WHERE owner_user_id = $1 \
             RETURNING id::text AS id, owner_user_id, company_name, program_name, cnpj, \
                       primary_color, logo_url, approval_status, review_note, invite_code, \
                       invite_paused, seat_limit, template_key, experience_config, closed_at",
        )
        .bind(&identity.uid)
        .bind(clean_company)
        .bind(clean_program)
        .bind(&clean_cnpj)
        .bind(&clean_color)
        .bind(&clean_logo)
        .bind(next_status)
        .bind(&next_tk)
        .bind(&next_cfg_val)
        .fetch_one(pool)
        .await
        {
            Ok(r) => r,
            Err(_) => {
                return json_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Erro ao atualizar programa.",
                )
            }
        };
        (updated, "PROGRAM_UPDATED", Some(current_status_str))
    } else {
        let initial_tk = requested_template.unwrap_or(TemplateKey::ClinicaGeral);
        let initial_cfg = default_experience_config_for_template(initial_tk);
        let initial_cfg_val = serde_json::to_value(&initial_cfg).unwrap_or_else(|_| json!({}));
        let created = match sqlx::query(
            "INSERT INTO partner_programs \
                (owner_user_id, company_name, program_name, cnpj, primary_color, logo_url, \
                 approval_status, template_key, experience_config) \
             VALUES ($1, $2, $3, $4, $5, $6, 'DRAFT', $7, $8) \
             RETURNING id::text AS id, owner_user_id, company_name, program_name, cnpj, \
                       primary_color, logo_url, approval_status, review_note, invite_code, \
                       invite_paused, seat_limit, template_key, experience_config, closed_at",
        )
        .bind(&identity.uid)
        .bind(clean_company)
        .bind(clean_program)
        .bind(&clean_cnpj)
        .bind(&clean_color)
        .bind(&clean_logo)
        .bind(initial_tk.as_str())
        .bind(&initial_cfg_val)
        .fetch_one(pool)
        .await
        {
            Ok(r) => r,
            Err(_) => {
                return json_error(StatusCode::INTERNAL_SERVER_ERROR, "Erro ao criar programa.")
            }
        };
        (created, "PROGRAM_CREATED", None)
    };

    let id_str: String = row.get("id");
    let prog_uuid = Uuid::parse_str(&id_str).ok();
    let active = match prog_uuid {
        Some(u) => count_active_participants(pool, u).await.unwrap_or(0),
        None => 0,
    };
    let next_status: String = row.get("approval_status");
    let _ = record_audit(
        pool,
        prog_uuid,
        &identity.uid,
        &identity.uid,
        audit_action,
        json!({ "previousStatus": prev_status, "approvalStatus": next_status }),
    )
    .await;

    let dto = map_program_row(&row, active, false);
    (StatusCode::OK, Json(json!({ "program": dto }))).into_response()
}

pub async fn submit_my_program_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "POST", "/api/b2b/program/me/submit").await {
            Ok(id) => id,
            Err(resp) => return resp,
        };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let existing = match sqlx::query(
        "SELECT id::text AS id, owner_user_id, company_name, program_name, cnpj, primary_color, \
                logo_url, approval_status, review_note, invite_code, invite_paused, seat_limit, \
                template_key, experience_config, closed_at \
           FROM partner_programs \
          WHERE owner_user_id = $1 AND closed_at IS NULL",
    )
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar programa.",
            )
        }
    };

    let Some(existing) = existing else {
        return json_error(
            StatusCode::NOT_FOUND,
            "Prepare e salve os dados do programa antes de enviar para aprovação.",
        );
    };

    let current_status: String = existing.get("approval_status");
    let id_str: String = existing.get("id");
    let prog_uuid = Uuid::parse_str(&id_str).ok();
    if current_status == "APPROVED" {
        let active = match prog_uuid {
            Some(u) => count_active_participants(pool, u).await.unwrap_or(0),
            None => 0,
        };
        let dto = map_program_row(&existing, active, false);
        return (StatusCode::OK, Json(json!({ "program": dto }))).into_response();
    }

    let cnpj: Option<String> = existing.get("cnpj");
    if let Err(msg) = normalize_cnpj(cnpj.as_deref(), true) {
        return json_error(StatusCode::BAD_REQUEST, msg);
    }

    let updated = match sqlx::query(
        "UPDATE partner_programs \
            SET approval_status = 'PENDING_REVIEW', updated_at = NOW() \
          WHERE owner_user_id = $1 \
         RETURNING id::text AS id, owner_user_id, company_name, program_name, cnpj, \
                   primary_color, logo_url, approval_status, review_note, invite_code, \
                   invite_paused, seat_limit, template_key, experience_config, closed_at",
    )
    .bind(&identity.uid)
    .fetch_one(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao enviar programa para análise.",
            )
        }
    };

    let active = match prog_uuid {
        Some(u) => count_active_participants(pool, u).await.unwrap_or(0),
        None => 0,
    };
    let _ = record_audit(
        pool,
        prog_uuid,
        &identity.uid,
        &identity.uid,
        "PROGRAM_SUBMITTED_FOR_REVIEW",
        json!({ "approvalStatus": "PENDING_REVIEW" }),
    )
    .await;

    let dto = map_program_row(&updated, active, false);
    (StatusCode::OK, Json(json!({ "program": dto }))).into_response()
}

// =========================================================================
// B2B-15: Catalogo de Templates, Experience Builder & Encerramento Seguro
// =========================================================================

pub async fn list_templates_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Response {
    if let Err(resp) = authenticate_request(&headers, &state, "GET", "/api/b2b/templates").await {
        return resp;
    }
    (
        StatusCode::OK,
        Json(json!({
            "templates": template_catalog()
        })),
    )
        .into_response()
}

pub async fn update_program_experience_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<UpdateExperienceRequest>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "PUT", "/api/b2b/program/me/experience").await
        {
            Ok(id) => id,
            Err(resp) => return resp,
        };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let existing = match sqlx::query(
        "SELECT id, template_key, experience_config \
           FROM partner_programs \
          WHERE owner_user_id = $1 AND closed_at IS NULL",
    )
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar programa para atualizar experiência.",
            )
        }
    };

    let Some(existing) = existing else {
        return json_error(
            StatusCode::NOT_FOUND,
            "Programa parceiro não encontrado para esta conta.",
        );
    };

    let prog_uuid: Uuid = existing.get("id");
    let current_tk_str: Option<String> = existing.try_get("template_key").ok();
    let target_tk = match req.template_key.as_deref() {
        Some(raw) if !raw.trim().is_empty() => match TemplateKey::parse(raw) {
            Some(tk) => tk,
            None => return json_error(StatusCode::BAD_REQUEST, "Template do programa inválido."),
        },
        _ => current_tk_str
            .as_deref()
            .and_then(TemplateKey::parse)
            .unwrap_or(TemplateKey::ClinicaGeral),
    };

    let candidate_config = match (req.apply_template_defaults, req.experience_config) {
        (true, _) | (false, None) => default_experience_config_for_template(target_tk),
        (false, Some(mut provided)) => {
            if req.template_key.is_some() {
                provided.template_key = target_tk.as_str().to_string();
            }
            provided
        }
    };

    let normalized = match validate_and_normalize_experience_config(candidate_config) {
        Ok(cfg) => cfg,
        Err(msg) => return json_error(StatusCode::BAD_REQUEST, msg),
    };

    let cfg_json = match serde_json::to_value(&normalized) {
        Ok(v) => v,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao serializar configuração de experiência.",
            )
        }
    };

    let updated = match sqlx::query(
        "UPDATE partner_programs \
            SET template_key = $2, experience_config = $3, updated_at = NOW() \
          WHERE id = $1 \
         RETURNING id::text AS id, owner_user_id, company_name, program_name, cnpj, \
                   primary_color, logo_url, approval_status, review_note, invite_code, \
                   invite_paused, seat_limit, template_key, experience_config, closed_at",
    )
    .bind(prog_uuid)
    .bind(&normalized.template_key)
    .bind(&cfg_json)
    .fetch_one(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao salvar configuração No-Code do programa.",
            )
        }
    };

    let _ = record_audit(
        pool,
        Some(prog_uuid),
        &identity.uid,
        &identity.uid,
        "EXPERIENCE_UPDATED",
        json!({
            "templateKey": normalized.template_key,
            "enabledMetrics": normalized.enabled_metrics,
            "features": normalized.features
        }),
    )
    .await;

    let active = count_active_participants(pool, prog_uuid)
        .await
        .unwrap_or(0);
    let dto = map_program_row(&updated, active, false);
    (StatusCode::OK, Json(json!({ "program": dto }))).into_response()
}

pub async fn close_my_program_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    payload: Option<Json<CloseProgramRequest>>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "POST", "/api/b2b/program/me/close").await {
            Ok(id) => id,
            Err(resp) => return resp,
        };
    let req = payload.map(|Json(r)| r).unwrap_or_default();
    let clean_reason = match req.reason.as_deref() {
        Some(r) if !r.trim().is_empty() => {
            match sanitize_plain_text_field(
                r,
                2,
                300,
                "Motivo de encerramento inválido (entre 2 e 300 caracteres sem tags).",
            ) {
                Ok(s) => Some(s),
                Err(msg) => return json_error(StatusCode::BAD_REQUEST, msg),
            }
        }
        _ => None,
    };
    let clean_transfer_target = match req.transfer_target_user_id.as_deref() {
        Some(uid) if !uid.trim().is_empty() => {
            let trimmed = uid.trim();
            if trimmed == identity.uid
                || trimmed.len() > 128
                || !trimmed
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            {
                return json_error(
                    StatusCode::BAD_REQUEST,
                    "Identificador do sócio destinatário inválido.",
                );
            }
            Some(trimmed.to_string())
        }
        _ => None,
    };

    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let mut tx = match pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao iniciar transação de encerramento.",
            )
        }
    };

    let existing = match sqlx::query(
        "SELECT id, closed_at \
           FROM partner_programs \
          WHERE owner_user_id = $1 \
            FOR UPDATE",
    )
    .bind(&identity.uid)
    .fetch_optional(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao carregar programa para encerramento.",
            )
        }
    };

    let Some(existing) = existing else {
        return json_error(
            StatusCode::NOT_FOUND,
            "Programa parceiro não encontrado para esta conta.",
        );
    };

    let prog_uuid: Uuid = existing.get("id");

    if let Some(ref target_uid) = clean_transfer_target {
        let target_exists: Option<i32> = sqlx::query_scalar("SELECT 1 FROM profiles WHERE id = $1")
            .bind(target_uid)
            .fetch_optional(&mut *tx)
            .await
            .ok()
            .flatten();
        if target_exists.is_none() {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Conta destinatária para transferência não encontrada.",
            );
        }
    }

    let revoked_res = match sqlx::query(
        "UPDATE program_enrollments \
            SET status = 'REVOKED', \
                shared_categories = '[]'::jsonb, \
                revoked_at = NOW(), \
                updated_at = NOW() \
          WHERE program_id = $1 AND status = 'ACTIVE'",
    )
    .bind(prog_uuid)
    .execute(&mut *tx)
    .await
    {
        Ok(res) => res.rows_affected(),
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao revogar vínculos ativos do programa.",
            )
        }
    };

    if sqlx::query(
        "UPDATE partner_programs \
            SET closed_at = COALESCE(closed_at, NOW()), \
                invite_paused = true, \
                invite_code = NULL, \
                transfer_target_user_id = $2, \
                updated_at = NOW() \
          WHERE id = $1",
    )
    .bind(prog_uuid)
    .bind(&clean_transfer_target)
    .execute(&mut *tx)
    .await
    .is_err()
    {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao encerrar programa parceiro.",
        );
    }

    let _ = record_audit(
        &mut *tx,
        Some(prog_uuid),
        &identity.uid,
        clean_transfer_target.as_deref().unwrap_or(&identity.uid),
        "PROGRAM_CLOSED",
        json!({
            "revokedEnrollments": revoked_res,
            "reason": clean_reason,
            "transferTargetUserId": clean_transfer_target
        }),
    )
    .await;

    if tx.commit().await.is_err() {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar encerramento do programa.",
        );
    }

    (
        StatusCode::OK,
        Json(json!({
            "success": true,
            "closed": true,
            "revokedEnrollments": revoked_res,
            "activeContext": personal_context(&identity.uid)
        })),
    )
        .into_response()
}

pub async fn lookup_invite_handler(
    State(state): State<Arc<AppState>>,
    Path(raw_code): Path<String>,
) -> Response {
    let code = raw_code.trim().to_ascii_uppercase();
    if !is_valid_invite_code(&code) {
        return json_error(StatusCode::BAD_REQUEST, "Código do programa inválido.");
    }
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let row = match sqlx::query(
        "SELECT id::text AS id, company_name, program_name, primary_color, logo_url, \
                invite_code, invite_paused, seat_limit, template_key, experience_config \
           FROM partner_programs \
          WHERE UPPER(invite_code) = $1 AND approval_status = 'APPROVED' AND closed_at IS NULL",
    )
    .bind(&code)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar convite.",
            )
        }
    };

    let Some(row) = row else {
        return json_error(
            StatusCode::NOT_FOUND,
            "Programa não encontrado ou ainda não disponível para adesão.",
        );
    };

    let id_str: String = row.get("id");
    let active = match Uuid::parse_str(&id_str) {
        Ok(u) => count_active_participants(pool, u).await.unwrap_or(0),
        Err(_) => 0,
    };
    let seat_limit: i32 = row.get("seat_limit");
    let invite_paused: bool = row.get("invite_paused");
    let has_available_seats = active < i64::from(seat_limit) && !invite_paused;
    let raw_tk: Option<String> = row.try_get("template_key").ok();
    let raw_cfg: Option<Value> = row.try_get("experience_config").ok();
    let (template_key, experience_config) =
        parse_experience_config_json(raw_tk.as_deref(), raw_cfg.as_ref());

    (
        StatusCode::OK,
        Json(json!({
            "invite": {
                "programId": id_str,
                "companyName": row.get::<String, _>("company_name"),
                "programName": row.get::<String, _>("program_name"),
                "primaryColor": row.get::<String, _>("primary_color"),
                "logoUrl": row.get::<Option<String>, _>("logo_url"),
                "inviteCode": row.get::<Option<String>, _>("invite_code"),
                "invitePaused": invite_paused,
                "hasAvailableSeats": has_available_seats,
                "templateKey": template_key,
                "experienceConfig": experience_config
            }
        })),
    )
        .into_response()
}

// =========================================================================
// B2B-09: Aprovacao Manual Assistida (SERVICE_TOKEN + transacao FOR UPDATE)
// =========================================================================

pub async fn admin_list_programs_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<AdminProgramsQuery>,
) -> Response {
    if let Err((code, msg)) = require_service_token(&headers) {
        return json_error(code, msg);
    }
    let filter_status: Option<String> = match query.status.as_deref() {
        Some(raw) if !raw.trim().is_empty() => match ApprovalStatus::parse(raw) {
            Some(st) => Some(st.as_str().to_string()),
            None => return json_error(StatusCode::BAD_REQUEST, "Status de aprovação inválido."),
        },
        _ => None,
    };
    let limit = query.limit.unwrap_or(50).clamp(1, 200);
    let offset = query.offset.unwrap_or(0).max(0);

    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let rows = match sqlx::query(
        "SELECT p.id::text AS id, p.owner_user_id, p.company_name, p.program_name, p.cnpj, \
                p.primary_color, p.logo_url, p.approval_status, p.review_note, p.reviewed_by, \
                p.reviewed_at, p.invite_code, p.invite_paused, p.seat_limit, p.created_at, \
                p.updated_at, p.template_key, p.experience_config, p.closed_at, \
                COALESCE((SELECT COUNT(*) FROM program_enrollments e \
                           WHERE e.program_id = p.id AND e.status = 'ACTIVE'), 0) AS active_participants \
           FROM partner_programs p \
          WHERE ($1::text IS NULL OR p.approval_status = $1) \
          ORDER BY CASE WHEN p.approval_status = 'PENDING_REVIEW' THEN 0 ELSE 1 END, \
                   p.updated_at DESC \
          LIMIT $2 OFFSET $3",
    )
    .bind(filter_status)
    .bind(limit)
    .bind(offset)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao listar programas B2B.",
            )
        }
    };

    let programs: Vec<PartnerProgramDto> = rows
        .iter()
        .map(|row| {
            let active: i64 = row.get("active_participants");
            map_program_row(row, active, true)
        })
        .collect();

    (
        StatusCode::OK,
        Json(json!({ "success": true, "programs": programs })),
    )
        .into_response()
}

pub async fn admin_review_program_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id): Path<String>,
    Json(req): Json<AdminReviewRequest>,
) -> Response {
    if let Err((code, msg)) = require_service_token(&headers) {
        return json_error(code, msg);
    }
    let prog_uuid = match Uuid::parse_str(program_id.trim()) {
        Ok(u) => u,
        Err(_) => {
            return json_error(
                StatusCode::BAD_REQUEST,
                "Identificador do programa inválido.",
            )
        }
    };
    let Some(action) = ReviewAction::parse(&req.action) else {
        return json_error(
            StatusCode::BAD_REQUEST,
            "Ação inválida. Use approve, request_changes ou reject.",
        );
    };
    let clean_note = req.note.as_deref().unwrap_or("").trim();
    let reviewer_raw = req.reviewer.as_deref().unwrap_or("equipe-braga").trim();
    let clean_reviewer: String = if reviewer_raw.is_empty() {
        "equipe-braga".to_string()
    } else {
        reviewer_raw.chars().take(128).collect()
    };

    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let mut tx = match pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao iniciar transação de aprovação.",
            )
        }
    };

    let existing = match sqlx::query(
        "SELECT id, program_name, approval_status, invite_code, seat_limit \
           FROM partner_programs \
          WHERE id = $1 \
            FOR UPDATE",
    )
    .bind(prog_uuid)
    .fetch_optional(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao carregar programa para análise.",
            )
        }
    };

    let Some(existing) = existing else {
        return json_error(StatusCode::NOT_FOUND, "Programa parceiro não encontrado.");
    };

    let current_status_str: String = existing.get("approval_status");
    let current_status =
        ApprovalStatus::parse(&current_status_str).unwrap_or(ApprovalStatus::Draft);
    let (next_status, audit_action) =
        match validate_review_transition(current_status, action, clean_note, req.seat_limit) {
            Ok(v) => v,
            Err((code, msg)) => return json_error(code, msg),
        };

    let mut next_invite_code: Option<String> = existing.get("invite_code");
    let current_seat_limit: i32 = existing.get("seat_limit");
    let next_seat_limit = if action == ReviewAction::Approve {
        req.seat_limit.unwrap_or(current_seat_limit)
    } else {
        current_seat_limit
    };

    if action == ReviewAction::Approve && next_invite_code.is_none() {
        let program_name: String = existing.get("program_name");
        for _ in 0..5 {
            let candidate = generate_invite_code(&program_name);
            let collision: Option<i32> =
                sqlx::query_scalar("SELECT 1 FROM partner_programs WHERE UPPER(invite_code) = $1")
                    .bind(&candidate)
                    .fetch_optional(&mut *tx)
                    .await
                    .ok()
                    .flatten();
            if collision.is_none() {
                next_invite_code = Some(candidate);
                break;
            }
        }
        if next_invite_code.is_none() {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Não foi possível gerar um código de convite único.",
            );
        }
    }

    let note_to_store = if clean_note.is_empty() {
        None
    } else {
        Some(clean_note.to_string())
    };

    let updated = match sqlx::query(
        "UPDATE partner_programs \
            SET approval_status = $2, review_note = $3, reviewed_by = $4, reviewed_at = NOW(), \
                invite_code = $5, seat_limit = $6, updated_at = NOW() \
          WHERE id = $1 \
         RETURNING id::text AS id, owner_user_id, company_name, program_name, cnpj, \
                   primary_color, logo_url, approval_status, review_note, reviewed_by, \
                   reviewed_at, invite_code, invite_paused, seat_limit, created_at, updated_at, \
                   template_key, experience_config, closed_at",
    )
    .bind(prog_uuid)
    .bind(next_status.as_str())
    .bind(&note_to_store)
    .bind(&clean_reviewer)
    .bind(&next_invite_code)
    .bind(next_seat_limit)
    .fetch_one(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao salvar decisão de aprovação.",
            )
        }
    };

    let _ = record_audit(
        &mut *tx,
        Some(prog_uuid),
        &clean_reviewer,
        &clean_reviewer,
        audit_action,
        json!({
            "previousStatus": current_status.as_str(),
            "approvalStatus": next_status.as_str(),
            "seatLimit": next_seat_limit
        }),
    )
    .await;

    if tx.commit().await.is_err() {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar transação de aprovação.",
        );
    }

    let active = count_active_participants(pool, prog_uuid)
        .await
        .unwrap_or(0);
    let dto = map_program_row(&updated, active, true);
    (
        StatusCode::OK,
        Json(json!({ "success": true, "program": dto })),
    )
        .into_response()
}

// =========================================================================
// B2B-10 & B2B-11: Vinculo por Codigo, Consentimento Granular & Franquia Atomica
// =========================================================================

pub async fn join_program_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<JoinProgramRequest>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "POST", "/api/b2b/enrollments/join").await {
            Ok(id) => id,
            Err(resp) => return resp,
        };
    let code = req.invite_code.trim().to_ascii_uppercase();
    if !is_valid_invite_code(&code) {
        return json_error(StatusCode::BAD_REQUEST, "Código do programa inválido.");
    }
    let categories = match validate_shared_categories(&req.shared_categories) {
        Ok(c) => c,
        Err(msg) => return json_error(StatusCode::BAD_REQUEST, msg),
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let mut tx = match pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao iniciar transação de adesão.",
            )
        }
    };

    let program_row = match sqlx::query(
        "SELECT id, owner_user_id, company_name, program_name, primary_color, logo_url, \
                approval_status, invite_paused, seat_limit, template_key, experience_config \
           FROM partner_programs \
          WHERE UPPER(invite_code) = $1 AND closed_at IS NULL \
            FOR UPDATE",
    )
    .bind(&code)
    .fetch_optional(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar programa para adesão.",
            )
        }
    };

    let Some(program_row) = program_row else {
        return json_error(
            StatusCode::NOT_FOUND,
            "Programa não encontrado para o código informado.",
        );
    };

    let prog_uuid: Uuid = program_row.get("id");
    let owner_user_id: String = program_row.get("owner_user_id");
    let status_str: String = program_row.get("approval_status");
    let approval_status = ApprovalStatus::parse(&status_str).unwrap_or(ApprovalStatus::Draft);
    let invite_paused: bool = program_row.get("invite_paused");
    let seat_limit: i32 = program_row.get("seat_limit");

    let existing_status: Option<String> = sqlx::query_scalar(
        "SELECT status FROM program_enrollments WHERE program_id = $1 AND patient_user_id = $2",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .fetch_optional(&mut *tx)
    .await
    .ok()
    .flatten();

    let already_active = existing_status.as_deref() == Some("ACTIVE");

    let active_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM program_enrollments WHERE program_id = $1 AND status = 'ACTIVE'",
    )
    .bind(prog_uuid)
    .fetch_one(&mut *tx)
    .await
    .unwrap_or(0);

    if let Err((err_code, err_msg)) = check_enrollment_eligibility(
        approval_status,
        invite_paused,
        &owner_user_id,
        &identity.uid,
        already_active,
        active_count,
        seat_limit,
    ) {
        return json_error(err_code, err_msg);
    }

    let categories_json = json!(categories);
    if sqlx::query(
        "INSERT INTO program_enrollments \
            (program_id, patient_user_id, status, shared_categories, consented_at, revoked_at, updated_at) \
         VALUES ($1, $2, 'ACTIVE', $3, NOW(), NULL, NOW()) \
         ON CONFLICT (program_id, patient_user_id) DO UPDATE \
            SET status = 'ACTIVE', \
                shared_categories = EXCLUDED.shared_categories, \
                consented_at = NOW(), \
                revoked_at = NULL, \
                updated_at = NOW()",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .bind(&categories_json)
    .execute(&mut *tx)
    .await
    .is_err()
    {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao registrar participação no programa.",
        );
    }

    let _ = record_audit(
        &mut *tx,
        Some(prog_uuid),
        &identity.uid,
        &identity.uid,
        "ENROLLMENT_JOINED",
        json!({
            "sharedCategories": categories,
            "rejoinedOrUpdated": already_active
        }),
    )
    .await;

    if tx.commit().await.is_err() {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar adesão ao programa.",
        );
    }

    let raw_tk: Option<String> = program_row.try_get("template_key").ok();
    let raw_cfg: Option<Value> = program_row.try_get("experience_config").ok();
    let (tk, cfg) = parse_experience_config_json(raw_tk.as_deref(), raw_cfg.as_ref());

    let ctx = ContextDescriptor {
        mode: "PARTNER_PARTICIPANT".to_string(),
        user_id: identity.uid,
        program_id: Some(prog_uuid.to_string()),
        program_name: Some(program_row.get("program_name")),
        company_name: Some(program_row.get("company_name")),
        primary_color: Some(program_row.get("primary_color")),
        logo_url: program_row.get("logo_url"),
        approval_status: Some(status_str),
        shared_categories: categories,
        template_key: Some(tk),
        experience_config: Some(cfg),
        family_share_enabled: false,
    };

    (StatusCode::OK, Json(json!({ "activeContext": ctx }))).into_response()
}

pub async fn update_enrollment_consent_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id): Path<String>,
    Json(req): Json<UpdateConsentRequest>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/enrollments/:program_id/consent",
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
    let categories = match validate_shared_categories(&req.shared_categories) {
        Ok(c) => c,
        Err(msg) => return json_error(StatusCode::BAD_REQUEST, msg),
    };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let categories_json = json!(categories);
    let updated = match sqlx::query(
        "UPDATE program_enrollments e \
            SET shared_categories = $3, \
                family_share_enabled = COALESCE($4, e.family_share_enabled), \
                updated_at = NOW() \
           FROM partner_programs p \
          WHERE e.program_id = p.id \
            AND e.program_id = $1 \
            AND e.patient_user_id = $2 \
            AND e.status = 'ACTIVE' \
            AND p.approval_status = 'APPROVED' \
            AND p.closed_at IS NULL \
         RETURNING p.id::text AS id, p.program_name, p.company_name, p.primary_color, \
                   p.logo_url, p.approval_status, p.template_key, p.experience_config, \
                   e.family_share_enabled",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .bind(&categories_json)
    .bind(req.family_share_enabled)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao atualizar preferências de compartilhamento.",
            )
        }
    };

    let Some(row) = updated else {
        return json_error(
            StatusCode::NOT_FOUND,
            "Vínculo ativo não encontrado para este programa.",
        );
    };

    let family_share: bool = row.try_get("family_share_enabled").unwrap_or(false);
    let _ = record_audit(
        pool,
        Some(prog_uuid),
        &identity.uid,
        &identity.uid,
        "CONSENT_UPDATED",
        json!({
            "sharedCategories": categories,
            "familyShareEnabled": family_share
        }),
    )
    .await;

    let raw_tk: Option<String> = row.try_get("template_key").ok();
    let raw_cfg: Option<Value> = row.try_get("experience_config").ok();
    let (tk, cfg) = parse_experience_config_json(raw_tk.as_deref(), raw_cfg.as_ref());

    let ctx = ContextDescriptor {
        mode: "PARTNER_PARTICIPANT".to_string(),
        user_id: identity.uid,
        program_id: Some(row.get("id")),
        program_name: Some(row.get("program_name")),
        company_name: Some(row.get("company_name")),
        primary_color: Some(row.get("primary_color")),
        logo_url: row.get("logo_url"),
        approval_status: Some(row.get("approval_status")),
        shared_categories: categories,
        template_key: Some(tk),
        experience_config: Some(cfg),
        family_share_enabled: family_share,
    };

    (StatusCode::OK, Json(json!({ "activeContext": ctx }))).into_response()
}

pub async fn leave_program_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(program_id): Path<String>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/enrollments/:program_id/leave",
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

    let result = match sqlx::query(
        "UPDATE program_enrollments \
            SET status = 'REVOKED', \
                shared_categories = '[]'::jsonb, \
                revoked_at = NOW(), \
                updated_at = NOW() \
          WHERE program_id = $1 AND patient_user_id = $2 AND status = 'ACTIVE'",
    )
    .bind(prog_uuid)
    .bind(&identity.uid)
    .execute(pool)
    .await
    {
        Ok(res) => res,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao encerrar vínculo com o programa.",
            )
        }
    };

    if result.rows_affected() > 0 {
        let _ = record_audit(
            pool,
            Some(prog_uuid),
            &identity.uid,
            &identity.uid,
            "ENROLLMENT_REVOKED",
            json!({ "status": "REVOKED" }),
        )
        .await;
    }

    (
        StatusCode::OK,
        Json(json!({ "activeContext": personal_context(&identity.uid) })),
    )
        .into_response()
}

pub async fn update_invite_policy_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<UpdateInvitePolicyRequest>,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/program/me/invite-policy",
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

    let updated = match sqlx::query(
        "UPDATE partner_programs \
            SET invite_paused = $2, updated_at = NOW() \
          WHERE owner_user_id = $1 AND closed_at IS NULL \
         RETURNING id::text AS id, owner_user_id, company_name, program_name, cnpj, \
                   primary_color, logo_url, approval_status, review_note, invite_code, \
                   invite_paused, seat_limit, template_key, experience_config, closed_at",
    )
    .bind(&identity.uid)
    .bind(req.paused)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao atualizar política de convite.",
            )
        }
    };

    let Some(row) = updated else {
        return json_error(
            StatusCode::NOT_FOUND,
            "Programa parceiro não encontrado para esta conta.",
        );
    };

    let id_str: String = row.get("id");
    let prog_uuid = Uuid::parse_str(&id_str).ok();
    let active = match prog_uuid {
        Some(u) => count_active_participants(pool, u).await.unwrap_or(0),
        None => 0,
    };

    let audit_action = if req.paused {
        "INVITE_PAUSED"
    } else {
        "INVITE_RESUMED"
    };
    let _ = record_audit(
        pool,
        prog_uuid,
        &identity.uid,
        &identity.uid,
        audit_action,
        json!({ "invitePaused": req.paused }),
    )
    .await;

    let dto = map_program_row(&row, active, false);
    (StatusCode::OK, Json(json!({ "program": dto }))).into_response()
}

pub async fn regenerate_invite_code_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "POST",
        "/api/b2b/program/me/invite-code/regenerate",
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

    let mut tx = match pool.begin().await {
        Ok(t) => t,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao iniciar transação de regeneração de código.",
            )
        }
    };

    let existing = match sqlx::query(
        "SELECT id, program_name, approval_status, invite_code \
           FROM partner_programs \
          WHERE owner_user_id = $1 AND closed_at IS NULL \
            FOR UPDATE",
    )
    .bind(&identity.uid)
    .fetch_optional(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao carregar programa para regenerar código.",
            )
        }
    };

    let Some(existing) = existing else {
        return json_error(
            StatusCode::NOT_FOUND,
            "Programa parceiro não encontrado para esta conta.",
        );
    };

    let status_str: String = existing.get("approval_status");
    if status_str != "APPROVED" {
        return json_error(
            StatusCode::CONFLICT,
            "O código de convite só pode ser regenerado para programas aprovados.",
        );
    }

    let prog_uuid: Uuid = existing.get("id");
    let program_name: String = existing.get("program_name");
    let previous_code: Option<String> = existing.get("invite_code");
    let mut new_code: Option<String> = None;

    for _ in 0..8 {
        let candidate = generate_invite_code(&program_name);
        if previous_code.as_deref() == Some(candidate.as_str()) {
            continue;
        }
        let collision: Option<i32> =
            sqlx::query_scalar("SELECT 1 FROM partner_programs WHERE UPPER(invite_code) = $1")
                .bind(&candidate)
                .fetch_optional(&mut *tx)
                .await
                .ok()
                .flatten();
        if collision.is_none() {
            new_code = Some(candidate);
            break;
        }
    }

    let Some(new_code) = new_code else {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Não foi possível gerar um novo código de convite único.",
        );
    };

    let updated = match sqlx::query(
        "UPDATE partner_programs \
            SET invite_code = $2, updated_at = NOW() \
          WHERE id = $1 \
         RETURNING id::text AS id, owner_user_id, company_name, program_name, cnpj, \
                   primary_color, logo_url, approval_status, review_note, invite_code, \
                   invite_paused, seat_limit, template_key, experience_config, closed_at",
    )
    .bind(prog_uuid)
    .bind(&new_code)
    .fetch_one(&mut *tx)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao salvar novo código de convite.",
            )
        }
    };

    let _ = record_audit(
        &mut *tx,
        Some(prog_uuid),
        &identity.uid,
        &identity.uid,
        "INVITE_CODE_REGENERATED",
        json!({ "inviteCode": new_code }),
    )
    .await;

    if tx.commit().await.is_err() {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Erro ao confirmar novo código de convite.",
        );
    }

    let active = count_active_participants(pool, prog_uuid)
        .await
        .unwrap_or(0);
    let dto = map_program_row(&updated, active, false);
    (StatusCode::OK, Json(json!({ "program": dto }))).into_response()
}

// =========================================================================
// B2B-12: Projecao Autorizada de Rotina Compartilhada (Anti-IDOR + Cursor)
// =========================================================================

pub async fn list_program_participants_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<ParticipantsQuery>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "GET", "/api/b2b/program/me/participants")
            .await
        {
            Ok(id) => id,
            Err(resp) => return resp,
        };
    let pool = match require_pool(&state) {
        Ok(p) => p,
        Err((code, msg)) => return json_error(code, msg),
    };

    let program_row = match sqlx::query(
        "SELECT id::text AS id, owner_user_id, company_name, program_name, cnpj, primary_color, \
                logo_url, approval_status, review_note, invite_code, invite_paused, seat_limit, \
                template_key, experience_config, closed_at \
           FROM partner_programs \
          WHERE owner_user_id = $1 AND closed_at IS NULL",
    )
    .bind(&identity.uid)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao consultar programa do responsável.",
            )
        }
    };

    let Some(program_row) = program_row else {
        return json_error(
            StatusCode::NOT_FOUND,
            "Nenhum programa parceiro vinculado a esta conta.",
        );
    };

    let status_str: String = program_row.get("approval_status");
    if status_str != "APPROVED" {
        return json_error(
            StatusCode::FORBIDDEN,
            "A área de participantes é liberada somente após aprovação do programa.",
        );
    }

    let id_str: String = program_row.get("id");
    let Ok(prog_uuid) = Uuid::parse_str(&id_str) else {
        return json_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "Identificador do programa inválido.",
        );
    };

    let (cursor_dt, limit) = parse_cursor_and_limit(query.cursor.as_deref(), query.limit);
    let fetch_limit = limit + 1;

    let rows = match sqlx::query(
        "SELECT e.patient_user_id, \
                COALESCE(NULLIF(BTRIM(pr.full_name), ''), 'Participante') AS display_name, \
                e.shared_categories, \
                e.consented_at \
           FROM program_enrollments e \
           JOIN profiles pr ON pr.id = e.patient_user_id \
          WHERE e.program_id = $1 \
            AND e.status = 'ACTIVE' \
            AND ($2::timestamptz IS NULL OR e.consented_at < $2) \
          ORDER BY e.consented_at DESC, e.patient_user_id ASC \
          LIMIT $3",
    )
    .bind(prog_uuid)
    .bind(cursor_dt)
    .bind(fetch_limit)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao listar participantes vinculados.",
            )
        }
    };

    let has_more = rows.len() > limit as usize;
    let slice_len = if has_more { limit as usize } else { rows.len() };
    let mut participants = Vec::with_capacity(slice_len);
    let mut next_cursor: Option<String> = None;

    for row in rows.into_iter().take(slice_len) {
        let consented: Option<DateTime<Utc>> = row.try_get("consented_at").ok();
        let consented_iso = consented.map(|dt| dt.to_rfc3339());
        if has_more {
            next_cursor = consented_iso.clone();
        }
        let cats: Value = row.get("shared_categories");
        participants.push(PartnerParticipantItemDto {
            patient_user_id: row.get("patient_user_id"),
            display_name: row.get("display_name"),
            shared_categories: parse_categories_json(&cats),
            consented_at: consented_iso,
        });
    }

    let active_participants = count_active_participants(pool, prog_uuid)
        .await
        .unwrap_or(0);
    let program_dto = map_program_row(&program_row, active_participants, false);

    (
        StatusCode::OK,
        Json(json!({
            "program": program_dto,
            "participants": participants,
            "nextCursor": next_cursor,
            "hasMore": has_more
        })),
    )
        .into_response()
}

async fn load_steps_section(pool: &PgPool, patient_id: &str, days: i32) -> SharedStepsSectionDto {
    let rows = sqlx::query(
        "SELECT date, steps, active_minutes, \
                COALESCE(distance_final_meters, distance_meters, 0.0)::double precision AS dist \
           FROM daily_metrics \
          WHERE user_id = $1 \
            AND date >= (CURRENT_DATE - make_interval(days => $2))::date \
          ORDER BY date DESC \
          LIMIT 31",
    )
    .bind(patient_id)
    .bind(days)
    .fetch_all(pool)
    .await
    .unwrap_or_default();

    let mut total_steps: i64 = 0;
    let items: Vec<SharedStepsDayDto> = rows
        .into_iter()
        .map(|r| {
            let d: chrono::NaiveDate = r.try_get("date").unwrap_or_default();
            let steps: i32 = r
                .try_get::<Option<i32>, _>("steps")
                .ok()
                .flatten()
                .unwrap_or(0);
            let active_minutes: i32 = r
                .try_get::<Option<i32>, _>("active_minutes")
                .ok()
                .flatten()
                .unwrap_or(0);
            let dist: f64 = r
                .try_get::<Option<f64>, _>("dist")
                .ok()
                .flatten()
                .unwrap_or(0.0);
            total_steps += i64::from(steps.max(0));
            SharedStepsDayDto {
                date: d.to_string(),
                steps: steps.max(0),
                active_minutes: active_minutes.max(0),
                distance_meters: dist.max(0.0),
            }
        })
        .collect();

    let days_recorded = items.len();
    let average_steps = if days_recorded > 0 {
        total_steps / (days_recorded as i64)
    } else {
        0
    };

    SharedStepsSectionDto {
        source: "Braga Saúde • Passos".to_string(),
        average_steps,
        days_recorded,
        days: items,
    }
}

async fn load_hydration_section(
    pool: &PgPool,
    patient_id: &str,
    days: i32,
) -> SharedHydrationSectionDto {
    let rows = sqlx::query(
        "SELECT (measured_at AT TIME ZONE 'America/Sao_Paulo')::date AS day, \
                COALESCE(SUM(hydration_ml), 0)::bigint AS total_ml \
           FROM vital_signs \
          WHERE user_id = $1 \
            AND deleted_at IS NULL \
            AND hydration_ml IS NOT NULL \
            AND hydration_ml > 0 \
            AND measured_at >= NOW() - make_interval(days => $2) \
          GROUP BY 1 \
          ORDER BY 1 DESC \
          LIMIT 31",
    )
    .bind(patient_id)
    .bind(days)
    .fetch_all(pool)
    .await
    .unwrap_or_default();

    let mut sum_ml: i64 = 0;
    let items: Vec<SharedHydrationDayDto> = rows
        .into_iter()
        .map(|r| {
            let day: chrono::NaiveDate = r.try_get("day").unwrap_or_default();
            let total_ml: i64 = r.try_get::<i64, _>("total_ml").unwrap_or(0).max(0);
            sum_ml += total_ml;
            SharedHydrationDayDto {
                date: day.to_string(),
                total_ml,
            }
        })
        .collect();

    let days_recorded = items.len();
    let average_ml = if days_recorded > 0 {
        sum_ml / (days_recorded as i64)
    } else {
        0
    };

    SharedHydrationSectionDto {
        source: "Braga Saúde • Autoinformado".to_string(),
        average_ml,
        days_recorded,
        days: items,
    }
}

async fn load_medications_section(
    pool: &PgPool,
    patient_id: &str,
    days: i32,
) -> SharedMedicationsSectionDto {
    let med_rows = sqlx::query(
        "SELECT name, dosage_mg, COALESCE(NULLIF(schedule_times, ''), schedule_time, '') AS sched \
           FROM medications \
          WHERE user_id = $1 \
          ORDER BY created_at DESC \
          LIMIT 30",
    )
    .bind(patient_id)
    .fetch_all(pool)
    .await
    .unwrap_or_default();

    let items: Vec<SharedMedicationItemDto> = med_rows
        .into_iter()
        .map(|r| {
            let dosage_mg = r
                .try_get::<Option<f64>, _>("dosage_mg")
                .ok()
                .flatten()
                .or_else(|| {
                    r.try_get::<Option<i32>, _>("dosage_mg")
                        .ok()
                        .flatten()
                        .map(f64::from)
                });
            SharedMedicationItemDto {
                name: r.try_get("name").unwrap_or_default(),
                dosage_mg,
                schedule: r.try_get::<String, _>("sched").unwrap_or_default(),
            }
        })
        .collect();

    let confirmations: i64 = sqlx::query_scalar(
        "SELECT COUNT(*)::bigint \
           FROM medication_logs \
          WHERE user_id = $1 \
            AND taken_at >= NOW() - make_interval(days => $2)",
    )
    .bind(patient_id)
    .bind(days)
    .fetch_one(pool)
    .await
    .unwrap_or(0);

    SharedMedicationsSectionDto {
        source: "Braga Saúde • Lembretes".to_string(),
        reminder_count: items.len(),
        user_confirmations_in_period: confirmations,
        note: "Confirmações de rotina registradas pelo usuário (não comprova tomada).".to_string(),
        items,
    }
}

async fn load_authorized_records_section(
    pool: &PgPool,
    patient_id: &str,
    days: i32,
) -> SharedAuthorizedRecordsSectionDto {
    let rows = sqlx::query(
        "SELECT measured_at, confirmed_via, systolic_pressure, diastolic_pressure, \
                heart_rate, glucose_level, glucose_type, oxygen_saturation \
           FROM vital_signs \
          WHERE user_id = $1 \
            AND deleted_at IS NULL \
            AND measured_at >= NOW() - make_interval(days => $2) \
            AND (systolic_pressure IS NOT NULL OR diastolic_pressure IS NOT NULL \
                 OR heart_rate IS NOT NULL OR glucose_level IS NOT NULL \
                 OR oxygen_saturation IS NOT NULL) \
          ORDER BY measured_at DESC \
          LIMIT 30",
    )
    .bind(patient_id)
    .bind(days)
    .fetch_all(pool)
    .await
    .unwrap_or_default();

    let records: Vec<SharedVitalEntryDto> = rows
        .into_iter()
        .map(|r| {
            let measured_at: Option<DateTime<Utc>> = r.try_get("measured_at").ok();
            let confirmed_via: Option<String> = r.try_get("confirmed_via").ok();
            let oxygen_saturation = r
                .try_get::<f64, _>("oxygen_saturation")
                .ok()
                .or_else(|| r.try_get::<i32, _>("oxygen_saturation").ok().map(f64::from))
                .map(|v| (v * 100.0).round() / 100.0);
            SharedVitalEntryDto {
                measured_at: measured_at.map(|dt| dt.to_rfc3339()).unwrap_or_default(),
                origin: format_confirmed_via_label(confirmed_via.as_deref()).to_string(),
                systolic_pressure: r.try_get("systolic_pressure").ok(),
                diastolic_pressure: r.try_get("diastolic_pressure").ok(),
                heart_rate: r.try_get("heart_rate").ok(),
                glucose_level: r.try_get("glucose_level").ok(),
                glucose_type: r.try_get("glucose_type").ok(),
                oxygen_saturation,
            }
        })
        .collect();

    SharedAuthorizedRecordsSectionDto {
        source: "Braga Saúde • Registros autorizados".to_string(),
        zero_file_egress_notice:
            "Arquivos e exames do organizador pessoal permanecem privados neste fluxo (Zero File Egress)."
                .to_string(),
        records,
    }
}

pub async fn get_participant_summary_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Path(patient_id): Path<String>,
    Query(query): Query<ParticipantSummaryQuery>,
) -> Response {
    let target_patient_id = patient_id.trim().to_string();
    if target_patient_id.is_empty() {
        return json_error(StatusCode::BAD_REQUEST, "Participante inválido.");
    }
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        &format!("/api/b2b/program/me/participants/{target_patient_id}/summary"),
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

    let access_row = match sqlx::query(
        "SELECT p.id AS program_id, p.owner_user_id, p.approval_status, p.program_name, \
                p.company_name, p.template_key, p.experience_config, \
                e.status AS enrollment_status, e.shared_categories, \
                COALESCE(NULLIF(BTRIM(pr.full_name), ''), 'Participante') AS participant_name \
           FROM partner_programs p \
           LEFT JOIN program_enrollments e \
             ON e.program_id = p.id AND e.patient_user_id = $2 \
           LEFT JOIN profiles pr ON pr.id = e.patient_user_id \
          WHERE p.owner_user_id = $1 AND p.closed_at IS NULL",
    )
    .bind(&identity.uid)
    .bind(&target_patient_id)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(_) => {
            return json_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao validar vínculo do participante.",
            )
        }
    };

    let Some(row) = access_row else {
        return json_error(
            StatusCode::FORBIDDEN,
            "Acesso restrito ao responsável de programa aprovado.",
        );
    };

    let prog_status_str: String = row.get("approval_status");
    let prog_status = ApprovalStatus::parse(&prog_status_str).unwrap_or(ApprovalStatus::Draft);
    let owner_uid: String = row.get("owner_user_id");
    let enrollment_status: Option<String> = row.try_get("enrollment_status").ok().flatten();

    if let Err((code, msg)) = check_partner_summary_access(
        prog_status,
        &owner_uid,
        &identity.uid,
        enrollment_status.as_deref(),
    ) {
        return json_error(code, msg);
    }

    let prog_uuid: Uuid = row.get("program_id");
    let raw_tk: Option<String> = row.try_get("template_key").ok();
    let raw_cfg: Option<Value> = row.try_get("experience_config").ok();
    let (_, program_config) = parse_experience_config_json(raw_tk.as_deref(), raw_cfg.as_ref());
    let cats_val: Value = row
        .try_get::<Option<Value>, _>("shared_categories")
        .ok()
        .flatten()
        .unwrap_or_else(|| json!([]));
    let consented_categories = parse_categories_json(&cats_val);
    let shared_categories =
        intersect_effective_categories(&program_config.enabled_metrics, &consented_categories);
    let period_days = clamp_summary_days(query.days);

    let steps_section = if shared_categories.iter().any(|c| c == "steps") {
        Some(load_steps_section(pool, &target_patient_id, period_days).await)
    } else {
        None
    };

    let hydration_section = if shared_categories.iter().any(|c| c == "hydration") {
        Some(load_hydration_section(pool, &target_patient_id, period_days).await)
    } else {
        None
    };

    let medications_section = if shared_categories.iter().any(|c| c == "medications") {
        Some(load_medications_section(pool, &target_patient_id, period_days).await)
    } else {
        None
    };

    let authorized_records_section = if shared_categories.iter().any(|c| c == "authorized_records")
    {
        Some(load_authorized_records_section(pool, &target_patient_id, period_days).await)
    } else {
        None
    };

    let _ = record_audit(
        pool,
        Some(prog_uuid),
        &identity.uid,
        &target_patient_id,
        "PARTNER_VIEWED_PARTICIPANT_SUMMARY",
        json!({
            "days": period_days,
            "categoriesReturned": shared_categories
        }),
    )
    .await;

    let summary = ParticipantRoutineSummaryDto {
        program_id: prog_uuid.to_string(),
        program_name: row.get("program_name"),
        company_name: row.get("company_name"),
        patient_user_id: target_patient_id,
        participant_name: row.get("participant_name"),
        period_days,
        shared_categories,
        steps_section,
        hydration_section,
        medications_section,
        authorized_records_section,
    };

    (StatusCode::OK, Json(json!({ "summary": summary }))).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_primary_color_and_cnpj() {
        assert_eq!(normalize_primary_color(Some("#1a6b5c")).unwrap(), "#1A6B5C");
        assert!(normalize_primary_color(Some("javascript:alert(1)")).is_err());

        assert_eq!(
            normalize_cnpj(Some("12345678000195"), true).unwrap(),
            Some("12.345.678/0001-95".to_string())
        );
        assert!(normalize_cnpj(Some(""), true).is_err());
        assert!(normalize_cnpj(Some("11.111.111/1111-11"), true).is_err());
        assert_eq!(normalize_cnpj(None, false).unwrap(), None);
    }

    #[test]
    fn test_generate_invite_code_format_and_no_ambiguous_chars() {
        let code = generate_invite_code("Clínica Horizonte");
        let parts: Vec<&str> = code.split('-').collect();
        assert_eq!(parts.len(), 2);
        assert_eq!(parts[0], "CLIN");
        assert_eq!(parts[1].len(), 4);
        for forbidden in ['0', 'O', '1', 'I'] {
            assert!(!parts[1].contains(forbidden));
        }
    }

    #[test]
    fn test_constant_time_eq_service_token() {
        assert!(constant_time_eq(b"secret-token-123", b"secret-token-123"));
        assert!(!constant_time_eq(b"secret-token-123", b"secret-token-124"));
        assert!(!constant_time_eq(b"short", b"longer-secret"));
    }

    #[test]
    fn test_review_state_machine_rules() {
        // Nao pode aprovar fora de PENDING_REVIEW
        let err_draft =
            validate_review_transition(ApprovalStatus::Draft, ReviewAction::Approve, "", None)
                .unwrap_err();
        assert_eq!(err_draft.0, StatusCode::CONFLICT);

        // Solicitar ajustes ou recusar exige justificativa
        let err_note = validate_review_transition(
            ApprovalStatus::PendingReview,
            ReviewAction::RequestChanges,
            "ok",
            None,
        )
        .unwrap_err();
        assert_eq!(err_note.0, StatusCode::BAD_REQUEST);

        // Aprovacao valida
        let (st, act) = validate_review_transition(
            ApprovalStatus::PendingReview,
            ReviewAction::Approve,
            "Tudo certo.",
            Some(20),
        )
        .unwrap();
        assert_eq!(st, ApprovalStatus::Approved);
        assert_eq!(act, "PROGRAM_APPROVED");

        // Recusa valida
        let (st_rej, act_rej) = validate_review_transition(
            ApprovalStatus::PendingReview,
            ReviewAction::Reject,
            "Fora do escopo do piloto.",
            None,
        )
        .unwrap();
        assert_eq!(st_rej, ApprovalStatus::Rejected);
        assert_eq!(act_rej, "PROGRAM_REJECTED");
    }

    #[test]
    fn test_validate_shared_categories_whitelist_and_dedup() {
        assert!(validate_shared_categories(&[]).is_err());
        assert!(validate_shared_categories(&["unknown_category".to_string()]).is_err());

        let cats = validate_shared_categories(&[
            " STEPS ".to_string(),
            "hydration".to_string(),
            "steps".to_string(),
            "authorized_records".to_string(),
        ])
        .unwrap();
        assert_eq!(
            cats,
            vec![
                "steps".to_string(),
                "hydration".to_string(),
                "authorized_records".to_string()
            ]
        );
    }

    #[test]
    fn test_check_enrollment_eligibility_seat_limit_and_pause() {
        // Programa nao aprovado
        let err_unapproved = check_enrollment_eligibility(
            ApprovalStatus::PendingReview,
            false,
            "owner-1",
            "patient-1",
            false,
            0,
            10,
        )
        .unwrap_err();
        assert_eq!(err_unapproved.0, StatusCode::FORBIDDEN);

        // Responsavel tentando ocupar vaga do proprio programa
        let err_owner = check_enrollment_eligibility(
            ApprovalStatus::Approved,
            false,
            "owner-1",
            "owner-1",
            false,
            0,
            10,
        )
        .unwrap_err();
        assert_eq!(err_owner.0, StatusCode::BAD_REQUEST);

        // Convites pausados bloqueiam novo participante
        let err_paused = check_enrollment_eligibility(
            ApprovalStatus::Approved,
            true,
            "owner-1",
            "patient-1",
            false,
            2,
            10,
        )
        .unwrap_err();
        assert_eq!(err_paused.0, StatusCode::CONFLICT);

        // Limite de vagas atingido bloqueia novo participante
        let err_full = check_enrollment_eligibility(
            ApprovalStatus::Approved,
            false,
            "owner-1",
            "patient-1",
            false,
            10,
            10,
        )
        .unwrap_err();
        assert_eq!(err_full.0, StatusCode::CONFLICT);

        // Participante ja ativo pode reatualizar consentimento mesmo com franquia cheia ou pausada
        assert!(check_enrollment_eligibility(
            ApprovalStatus::Approved,
            true,
            "owner-1",
            "patient-1",
            true,
            10,
            10,
        )
        .is_ok());

        // Novo participante elegivel com vaga disponivel
        assert!(check_enrollment_eligibility(
            ApprovalStatus::Approved,
            false,
            "owner-1",
            "patient-1",
            false,
            9,
            10,
        )
        .is_ok());
    }

    #[test]
    fn test_partner_summary_access_and_pagination_guards() {
        // Isolamento multi-tenant e aprovacao
        assert_eq!(
            check_partner_summary_access(
                ApprovalStatus::Approved,
                "owner-1",
                "owner-2",
                Some("ACTIVE")
            )
            .unwrap_err()
            .0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            check_partner_summary_access(
                ApprovalStatus::PendingReview,
                "owner-1",
                "owner-1",
                Some("ACTIVE")
            )
            .unwrap_err()
            .0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            check_partner_summary_access(
                ApprovalStatus::Approved,
                "owner-1",
                "owner-1",
                Some("REVOKED")
            )
            .unwrap_err()
            .0,
            StatusCode::FORBIDDEN
        );
        assert!(check_partner_summary_access(
            ApprovalStatus::Approved,
            "owner-1",
            "owner-1",
            Some("ACTIVE")
        )
        .is_ok());

        // Limites de protecao para o RDS Micro
        assert_eq!(clamp_summary_days(None), 30);
        assert_eq!(clamp_summary_days(Some(-5)), 1);
        assert_eq!(clamp_summary_days(Some(365)), 90);

        let (c_none, l_default) = parse_cursor_and_limit(None, None);
        assert!(c_none.is_none());
        assert_eq!(l_default, 20);

        let (c_ok, l_clamped) = parse_cursor_and_limit(Some("2026-11-18T20:00:00Z"), Some(500));
        assert!(c_ok.is_some());
        assert_eq!(l_clamped, 50);

        assert_eq!(
            format_confirmed_via_label(Some("braga.voice")),
            "Braga Voz (autoinformado)"
        );
    }

    #[test]
    fn test_zero_file_egress_and_invite_code_sanitization() {
        // Nenhuma categoria relacionada a exames/arquivos ou diario privado e aceita
        for forbidden in [
            "exams",
            "files",
            "organizer",
            "symptoms_diary",
            "pdf_attachments",
        ] {
            assert!(
                validate_shared_categories(&[forbidden.to_string()]).is_err(),
                "Categoria fora do escopo B2B deve ser bloqueada: {forbidden}"
            );
        }

        // Codigos maliciosos ou curtos devem ser rejeitados
        assert!(is_valid_invite_code("CARD-4F9A"));
        assert!(is_valid_invite_code("CLIN_2026"));
        assert!(!is_valid_invite_code("AB"));
        assert!(!is_valid_invite_code("CARD' OR 1=1"));
        assert!(!is_valid_invite_code("../etc/passwd"));
    }

    #[test]
    fn test_template_catalog_defaults_are_valid_and_safe() {
        let catalog = template_catalog();
        assert_eq!(catalog.len(), 7);

        for item in &catalog {
            let validated =
                validate_and_normalize_experience_config(item.default_config.clone()).unwrap();
            assert_eq!(validated.template_key, item.template_key);
            assert!(!validated.enabled_metrics.is_empty());
            assert!(!item.sample_safe_reminder.is_empty());
        }

        // SAUDE_CORPORATIVA protege contra exposicao ocupacional de medicamentos/exames ao RH
        let corp = default_experience_config_for_template(TemplateKey::SaudeCorporativa);
        assert_eq!(
            corp.enabled_metrics,
            vec!["steps".to_string(), "hydration".to_string()]
        );
        assert!(!corp.features.direct_partner_chat_enabled);
        assert!(!corp.features.family_link_enabled);
        assert!(!corp.features.appointments_enabled);

        // CUIDADO_SENIOR_HOMECARE nasce com vinculo familiar e todos os modulos de rotina ativos
        let senior = default_experience_config_for_template(TemplateKey::CuidadoSeniorHomecare);
        assert!(senior.features.family_link_enabled);
        assert!(senior.features.partner_reminders_enabled);
        assert!(senior.features.appointments_enabled);
        assert!(senior.features.direct_partner_chat_enabled);
    }

    #[test]
    fn test_experience_config_rejects_xss_schemes_and_invalid_urls() {
        let base = default_experience_config_for_template(TemplateKey::ClinicaGeral);

        // XSS em welcome_headline
        let mut bad_headline = base.clone();
        bad_headline.welcome_headline = "<script>alert(1)</script>".to_string();
        assert!(validate_and_normalize_experience_config(bad_headline).is_err());

        // Esquema javascript: em welcome_message
        let mut bad_scheme = base.clone();
        bad_scheme.welcome_message = "Clique em javascript:alert(1)".to_string();
        assert!(validate_and_normalize_experience_config(bad_scheme).is_err());

        // URL HTTP nao segura em external_booking_url
        let mut bad_url = base.clone();
        bad_url.appointments_policy.external_booking_url =
            Some("http://agenda.clinica.com".to_string());
        assert!(validate_and_normalize_experience_config(bad_url).is_err());

        // EXTERNAL_LINK_ONLY exige external_booking_url HTTPS quando appointments_enabled = true
        let mut missing_ext = base.clone();
        missing_ext.appointments_policy.booking_mode = "EXTERNAL_LINK_ONLY".to_string();
        missing_ext.appointments_policy.external_booking_url = None;
        assert!(validate_and_normalize_experience_config(missing_ext).is_err());

        // Recepcionista IA exige Chat Direto ativo
        let mut ai_without_chat = base.clone();
        ai_without_chat.features.direct_partner_chat_enabled = false;
        ai_without_chat.features.ai_receptionist_enabled = true;
        assert!(validate_and_normalize_experience_config(ai_without_chat).is_err());

        // Aceita JSONB snake_case do PostgreSQL (migration 091) e normaliza
        let pg_json = json!({
            "schema_version": 1,
            "template_key": "NUTRICAO_BEM_ESTAR",
            "welcome_headline": "Hábitos diários mais leves",
            "welcome_message": "Acompanhe sua hidratação e passos.",
            "enabled_metrics": ["hydration", "steps"],
            "features": {
                "assistant_chat_enabled": true,
                "direct_partner_chat_enabled": true,
                "ai_receptionist_enabled": true,
                "family_link_enabled": false,
                "partner_reminders_enabled": true,
                "appointments_enabled": true,
                "pre_consultation_pdf_enabled": true
            }
        });
        let (tk, parsed) = parse_experience_config_json(Some("NUTRICAO_BEM_ESTAR"), Some(&pg_json));
        assert_eq!(tk, "NUTRICAO_BEM_ESTAR");
        assert!(parsed.features.direct_partner_chat_enabled);
        assert!(parsed.features.ai_receptionist_enabled);
    }

    #[test]
    fn test_module_guard_returns_409_when_feature_disabled_and_403_when_inactive() {
        let cfg = default_experience_config_for_template(TemplateKey::ClinicaGeral);

        // Em CLINICA_GERAL, lembretes estao habilitados (true) e chat direto desligado (false)
        assert!(guard_program_module(
            ApprovalStatus::Approved,
            false,
            &cfg,
            ProgramModule::PartnerReminders
        )
        .is_ok());

        let err_disabled = guard_program_module(
            ApprovalStatus::Approved,
            false,
            &cfg,
            ProgramModule::DirectPartnerChat,
        )
        .unwrap_err();
        assert_eq!(err_disabled.0, StatusCode::CONFLICT);

        // Programa fechado ou nao aprovado retorna 403
        let err_closed = guard_program_module(
            ApprovalStatus::Approved,
            true,
            &cfg,
            ProgramModule::PartnerReminders,
        )
        .unwrap_err();
        assert_eq!(err_closed.0, StatusCode::FORBIDDEN);

        let err_draft = guard_program_module(
            ApprovalStatus::Draft,
            false,
            &cfg,
            ProgramModule::PartnerReminders,
        )
        .unwrap_err();
        assert_eq!(err_draft.0, StatusCode::FORBIDDEN);
    }

    #[test]
    fn test_dual_key_category_intersection_and_identity_mutation_lock() {
        // Intersecao: empresario habilitou [steps, hydration], paciente consentiu [hydration, medications]
        let enabled = vec!["steps".to_string(), "hydration".to_string()];
        let consented = vec!["hydration".to_string(), "medications".to_string()];
        assert_eq!(
            intersect_effective_categories(&enabled, &consented),
            vec!["hydration".to_string()]
        );

        // Programa aprovado nao permite trocar CNPJ ou razao social sem nova revisao (409)
        assert!(check_approved_identity_mutation(
            ApprovalStatus::Approved,
            "Clínica A",
            "Clínica A",
            Some("12.345.678/0001-95"),
            Some("12.345.678/0001-95"),
        )
        .is_ok());

        let err_cnpj = check_approved_identity_mutation(
            ApprovalStatus::Approved,
            "Clínica A",
            "Clínica A",
            Some("12.345.678/0001-95"),
            Some("98.765.432/0001-10"),
        )
        .unwrap_err();
        assert_eq!(err_cnpj.0, StatusCode::CONFLICT);

        let err_company = check_approved_identity_mutation(
            ApprovalStatus::Approved,
            "Clínica A",
            "Outra Empresa LTDA",
            Some("12.345.678/0001-95"),
            Some("12.345.678/0001-95"),
        )
        .unwrap_err();
        assert_eq!(err_company.0, StatusCode::CONFLICT);

        // Em DRAFT ou CHANGES_REQUESTED a edicao de CNPJ e livre
        assert!(check_approved_identity_mutation(
            ApprovalStatus::Draft,
            "Clínica A",
            "Outra Empresa LTDA",
            Some("12.345.678/0001-95"),
            Some("98.765.432/0001-10"),
        )
        .is_ok());
    }
}
