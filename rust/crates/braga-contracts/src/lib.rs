use serde::{Deserialize, Serialize};

/// Resposta de liveness do endpoint GET /health
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HealthResponse {
    pub status: String,
}

impl Default for HealthResponse {
    fn default() -> Self {
        Self {
            status: "online".to_string(),
        }
    }
}

/// Resposta da raiz GET /
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LandingResponse {
    pub status: String,
    pub service: String,
    pub app_available: bool,
    pub app_channel: String,
    pub app_message: String,
}

impl Default for LandingResponse {
    fn default() -> Self {
        Self {
            status: "ok".to_string(),
            service: "BragaSaude Gateway".to_string(),
            app_available: false,
            app_channel: "google_play".to_string(),
            app_message: "Em breve na Google Play Store.".to_string(),
        }
    }
}

/// Resposta para rotas de APK aposentadas (HTTP 410 Gone)
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AppUnavailableResponse {
    pub status: String,
    pub distribution_channel: String,
    pub available: bool,
    pub message: String,
}

impl Default for AppUnavailableResponse {
    fn default() -> Self {
        Self {
            status: "coming_soon".to_string(),
            distribution_channel: "google_play".to_string(),
            available: false,
            message:
                "Em breve na Google Play Store. A distribuição de APK pelo gateway foi encerrada."
                    .to_string(),
        }
    }
}

/// Formato padronizado de erro da API (compatível com FastAPI HTTPException)
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HttpErrorResponse {
    pub detail: String,
}

impl HttpErrorResponse {
    pub fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

/// Nível de restrição de moderação do usuário (tabela user_restrictions)
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum RestrictionLevel {
    Suspended,
    ReadOnly,
    NoSync,
}

impl std::str::FromStr for RestrictionLevel {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "suspended" => Ok(Self::Suspended),
            "read-only" => Ok(Self::ReadOnly),
            "no-sync" => Ok(Self::NoSync),
            other => Err(format!("Nivel de restricao desconhecido: {other}")),
        }
    }
}

/// Contrato de medicamento retornado pelo catálogo ANVISA
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BarcodeMedication {
    pub ean_barcode: String,
    pub name: String,
    pub active_principle: Option<String>,
    pub concentration: Option<String>,
    pub pharmaceutical_form: Option<String>,
    pub manufacturer: Option<String>,
    pub farmacia_popular_eligible: Option<bool>,
    pub anvisa_registration_number: Option<String>,
    pub source_name: String,
    pub source_url: Option<String>,
    pub source_checked_at: String,
}

/// Contrato de biometria (/api/biometry/{user_id})
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BiometryRecord {
    pub id: String,
    pub user_id: String,
    pub weight: Option<f64>,
    pub height: Option<f64>,
    pub imc: Option<f64>,
    pub measured_at: String,
}

/// Contrato de sinais vitais (/api/vitals/{user_id})
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct VitalSignRecord {
    pub id: String,
    pub user_id: String,
    pub measured_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub systolic_pressure: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub diastolic_pressure: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pulse_bpm: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub blood_glucose_mg_dl: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body_temperature_c: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub oxygen_saturation_percent: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hydration_ml: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
}

/// Contrato de métricas diárias (/api/metrics/daily/{user_id})
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DailyMetricRecord {
    pub id: String,
    pub user_id: String,
    pub date: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    pub steps: Option<i32>,
    pub distance_meters: Option<f64>,
    pub distance_gps_meters: Option<f64>,
    pub distance_steps_meters: Option<f64>,
    pub distance_final_meters: Option<f64>,
    pub reliability_score: Option<f64>,
    pub calories_burned: Option<f64>,
    pub active_minutes: Option<i32>,
}

/// Contrato de medicamento (/api/medications/{user_id})
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MedicationRecord {
    pub id: String,
    pub user_id: String,
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dosage_mg: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub schedule_time: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub schedule_times: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ean_barcode: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub active_principle: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub manufacturer: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pharmaceutical_form: Option<String>,
    pub total_units: Option<i32>,
    pub current_units: Option<i32>,
    pub alert_threshold_days: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub photo_reference_url: Option<String>,
    pub confirmed_with_prescription: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub updated_at: Option<String>,
}

/// Contrato de log de tomada de medicamento (/api/medications/{user_id}/logs)
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MedicationLogRecord {
    pub id: String,
    pub user_id: String,
    pub medication_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub taken_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scheduled_for: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub actor_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub units_taken: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub idempotency_key: Option<String>,
}

/// Contrato do feed de auditoria e atividades da família (/api/family/patients/{patient_id}/activity-feed)
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ActivityFeedRecord {
    pub id: String,
    pub patient_id: String,
    pub actor_id: String,
    pub actor_name: String,
    pub action_type: String,
    pub details: serde_json::Value,
    pub occurred_at: String,
}

// =========================================================================
// DTOs de Ingestão e Sincronização (Fase 3 - POSTs de Alta Frequência)
// =========================================================================

/// DTO para POST /api/sync/biometry
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct BiometrySyncDto {
    pub id: String,
    pub user_id: String,
    pub weight: f64,
    #[serde(default)]
    pub height: Option<f64>,
    pub measured_at: String,
}

/// DTO para POST /api/sync/daily-metrics
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct DailyMetricSyncDto {
    #[serde(default)]
    pub id: Option<String>,
    pub user_id: String,
    pub date: String,
    pub steps: i32,
    pub distance_meters: f64,
    #[serde(default)]
    pub distance_gps_meters: Option<f64>,
    #[serde(default)]
    pub distance_steps_meters: Option<f64>,
    #[serde(default)]
    pub distance_final_meters: Option<f64>,
    #[serde(default)]
    pub reliability_score: Option<f64>,
    pub calories_burned: f64,
    pub active_minutes: i32,
}

/// DTO para POST /api/sync/medication
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct MedicationSyncDto {
    #[serde(default)]
    pub id: Option<String>,
    pub user_id: String,
    pub name: String,
    #[serde(default)]
    pub dosage_mg: Option<f64>,
    #[serde(default)]
    pub pill_quantity: Option<i32>,
    pub schedule_time: String,
    #[serde(default)]
    pub schedule_times: Option<String>,
}

/// DTO para POST /api/sync/medication-log
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct MedicationLogSyncDto {
    #[serde(default)]
    pub id: Option<String>,
    pub user_id: String,
    pub medication_id: String,
    pub taken_at: String,
    #[serde(default)]
    pub scheduled_for: Option<String>,
    #[serde(default = "default_units_taken")]
    pub units_taken: i32,
}

fn default_units_taken() -> i32 {
    1
}

/// DTO para POST /api/sync/audit-log
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AuditLogSyncDto {
    #[serde(default)]
    pub id: Option<String>,
    pub user_id: String,
    pub event_type: String,
    pub action: String,
    #[serde(default)]
    pub metadata_json: Option<String>,
}

/// DTO para POST /api/agent/receipt
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct AgentReceiptDto {
    pub user_id: String,
    pub action: String,
    #[serde(default)]
    pub params: serde_json::Value,
    pub idempotency_key: String,
    #[serde(default = "default_receipt_status")]
    pub status: String,
    #[serde(default)]
    pub result: Option<serde_json::Value>,
}

fn default_receipt_status() -> String {
    "confirmed".to_string()
}

/// DTO para POST /api/sync/vitals
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct VitalSignSyncDto {
    #[serde(default)]
    pub id: Option<String>,
    pub user_id: String,
    #[serde(default)]
    pub systolic_pressure: Option<i32>,
    #[serde(default)]
    pub diastolic_pressure: Option<i32>,
    #[serde(default)]
    pub heart_rate: Option<i32>,
    #[serde(default)]
    pub oxygen_saturation: Option<i32>,
    #[serde(default)]
    pub glucose_level: Option<i32>,
    #[serde(default)]
    pub glucose_type: Option<String>,
    #[serde(default)]
    pub hydration_ml: Option<i32>,
    pub measured_at: String,
}

/// Resposta padrão para endpoints de escrita com status e id
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SyncResponse {
    pub status: String,
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none", rename = "medicationContract")]
    pub medication_contract: Option<i32>,
    #[serde(
        skip_serializing_if = "Option::is_none",
        rename = "medication_batch_contract"
    )]
    pub medication_batch_contract: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dose_status: Option<String>,
}

/// Resposta para POST /api/agent/receipt
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AgentReceiptResponse {
    pub status: String,
    pub closing: String,
}

// =========================================================================
// Helpers Clínicos e Algorítmicos da Fase 3
// =========================================================================

/// Normaliza altura do perfil em metros. None se ausente ou fora da faixa plausível (0.5..=2.5m).
pub fn height_in_meters(height: Option<f64>) -> Option<f64> {
    let mut h = height?;
    if h.is_nan() || h.is_infinite() {
        return None;
    }
    if h > 3.0 {
        h /= 100.0;
    }
    if (0.5..=2.5).contains(&h) {
        Some(h)
    } else {
        None
    }
}

/// Calcula o IMC arredondado em 1 casa decimal (peso / altura^2).
pub fn compute_imc(weight_kg: f64, height_m: Option<f64>) -> Option<f64> {
    let h = height_m?;
    if h <= 0.0 || weight_kg.is_nan() || weight_kg.is_infinite() {
        return None;
    }
    Some(((weight_kg / (h * h)) * 10.0).round() / 10.0)
}

/// Valida se uma string representa horário no formato HH:MM (00:00 até 23:59).
pub fn is_valid_hh_mm(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.len() != 5 || bytes[2] != b':' {
        return false;
    }
    let h1 = bytes[0].wrapping_sub(b'0');
    let h2 = bytes[1].wrapping_sub(b'0');
    let m1 = bytes[3].wrapping_sub(b'0');
    let m2 = bytes[4].wrapping_sub(b'0');
    if h1 > 9 || h2 > 9 || m1 > 9 || m2 > 9 {
        return false;
    }
    let hours = h1 * 10 + h2;
    let mins = m1 * 10 + m2;
    hours < 24 && mins < 60
}

/// Normaliza, dedup e ordena horários CSV de medicação ("08:00, 14:00, 08:00" -> ("08:00", "08:00,14:00")).
pub fn normalize_schedule_times(times_str: &str) -> Result<(String, String), &'static str> {
    let mut parts: Vec<&str> = times_str
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .collect();

    if parts.is_empty() {
        return Err("Horários inválidos.");
    }

    for p in &parts {
        if !is_valid_hh_mm(p) {
            return Err("Horários inválidos.");
        }
    }

    parts.sort_unstable();
    parts.dedup();

    let primary = parts[0].to_string();
    let joined = parts.join(",");
    Ok((primary, joined))
}

/// Valida se uma ação faz parte do catálogo de ações autorizadas (B0).
pub fn is_valid_agent_action(action: &str) -> bool {
    matches!(
        action,
        "REGISTRAR_PRESSAO"
            | "REGISTRAR_GLICEMIA"
            | "REGISTRAR_AGUA"
            | "CONSULTAR_METRICAS"
            | "LEMBRETES"
            | "EXAMES"
            | "EMERGENCIA"
    )
}

/// Fala de fechamento determinística montada do catálogo de ações (B0).
pub fn format_agent_closing(action: &str, params: &serde_json::Value) -> String {
    match action {
        "REGISTRAR_PRESSAO" => {
            let sis = params
                .get("sistolica")
                .and_then(|v| {
                    v.as_i64()
                        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                })
                .unwrap_or(0);
            let dia = params
                .get("diastolica")
                .and_then(|v| {
                    v.as_i64()
                        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                })
                .unwrap_or(0);
            format!("Pressão {} por {} registrada.", sis, dia)
        }
        "REGISTRAR_GLICEMIA" => {
            let gli = params
                .get("glicemia")
                .and_then(|v| {
                    v.as_i64()
                        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                })
                .unwrap_or(0);
            format!("Glicemia {} registrada.", gli)
        }
        "REGISTRAR_AGUA" => {
            let q = params
                .get("quantidade_ml")
                .and_then(|v| {
                    v.as_i64()
                        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                })
                .unwrap_or(0);
            format!("{} ml registrados.", q)
        }
        "CONSULTAR_METRICAS" => "Relatório aberto para você conferir.".to_string(),
        "LEMBRETES" => "Lembretes abertos.".to_string(),
        "EXAMES" => "Exames abertos.".to_string(),
        "EMERGENCIA" => "Acione o SAMU 192. Os links de socorro estão na resposta.".to_string(),
        _ => "Pronto! Confirma na tela?".to_string(),
    }
}

/// Gera o occurrence_id canônico via UUIDv5 para rastreabilidade e idempotência de doses.
pub fn compute_occurrence_id(
    patient_id: &str,
    medication_id: &str,
    instant: &chrono::DateTime<chrono::Utc>,
) -> uuid::Uuid {
    let canonical_medication = uuid::Uuid::parse_str(medication_id)
        .map(|u| u.to_string())
        .unwrap_or_else(|_| medication_id.to_string());
    let utc_text = instant.format("%Y-%m-%dT%H:%M:%S.%6fZ").to_string();
    let name = format!("{}/{}/{}", patient_id, canonical_medication, utc_text);
    uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_URL, name.as_bytes())
}

// =========================================================================
// Contratos e DTOs da Fase 4: Orb WebSocket e Áudio Aposentado
// =========================================================================

/// Resposta 410 Gone para endpoints de áudio/TTS desativados no backend.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AudioRetiredResponse {
    pub status: String,
    pub tts_engine: String,
    pub available: bool,
    pub detail: String,
}

impl Default for AudioRetiredResponse {
    fn default() -> Self {
        Self {
            status: "retired".to_string(),
            tts_engine: "android_native_tts".to_string(),
            available: false,
            detail: "Síntese vocal desativada no servidor: migrada para TextToSpeech nativo no aplicativo Android.".to_string(),
        }
    }
}

/// Papel de mensagem no Orb WebSocket.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum OrbRole {
    User,
    Assistant,
    System,
}

/// Mensagem individual no envelope do Orb.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OrbMessage {
    pub role: OrbRole,
    pub content: String,
}

fn default_orb_temperature() -> f64 {
    0.65
}

fn default_orb_max_tokens() -> u32 {
    350
}

/// Requisição de chat recebida via WebSocket no /ws/orb.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OrbChatRequest {
    #[serde(rename = "type")]
    pub msg_type: String,
    pub id: String,
    pub messages: Vec<OrbMessage>,
    #[serde(default)]
    pub user_id: Option<String>,
    #[serde(default = "default_orb_temperature")]
    pub temperature: f64,
    #[serde(default = "default_orb_max_tokens")]
    pub max_tokens: u32,
    #[serde(default)]
    pub acting_as: Option<String>,
    #[serde(default)]
    pub patient_id: Option<String>,
    #[serde(default)]
    pub nlu_context: Option<serde_json::Value>,
}

/// Códigos canônicos de erro do Orb WebSocket.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrbErrorCode {
    InvalidRequest,
    Busy,
    Forbidden,
    AuthExpired,
    Timeout,
    StreamFailed,
}

/// Mensagem de erro transmitida ao cliente WebSocket.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OrbErrorResponse {
    #[serde(rename = "type")]
    pub msg_type: String,
    pub id: String,
    pub code: OrbErrorCode,
    pub message: String,
}

/// Verifica se os headers HTTP caracterizam um handshake válido de upgrade para WebSocket.
pub fn is_websocket_upgrade_headers(connection: Option<&str>, upgrade: Option<&str>) -> bool {
    let has_upgrade_conn = connection
        .map(|c| {
            c.split(',')
                .any(|part| part.trim().eq_ignore_ascii_case("upgrade"))
        })
        .unwrap_or(false);
    let has_ws_upgrade = upgrade
        .map(|u| u.trim().eq_ignore_ascii_case("websocket"))
        .unwrap_or(false);
    has_upgrade_conn && has_ws_upgrade
}

/// Valida requisição de chat do Orb conforme protocolo da Fase 4.
pub fn validate_orb_chat_request(
    req: &OrbChatRequest,
    authenticated_uid: &str,
    raw_len: usize,
) -> Result<(), &'static str> {
    if raw_len > 64_000 {
        return Err("Tamanho total excede 64.000 caracteres.");
    }
    if req.msg_type != "chat" {
        return Err("Tipo de mensagem deve ser 'chat'.");
    }
    if req.id.is_empty() || req.id.len() > 128 {
        return Err("id deve ter entre 1 e 128 caracteres.");
    }
    if req.messages.is_empty() || req.messages.len() > 100 {
        return Err("messages deve conter entre 1 e 100 itens.");
    }
    for m in &req.messages {
        if m.content.len() > 16_000 {
            return Err("Mensagem excede limite de 16.000 caracteres.");
        }
    }
    if !(0.0..=2.0).contains(&req.temperature) {
        return Err("temperature deve estar entre 0.0 e 2.0.");
    }
    if req.max_tokens < 1 || req.max_tokens > 1024 {
        return Err("max_tokens deve estar entre 1 e 1024.");
    }
    if let Some(ref uid) = req.user_id {
        if uid != authenticated_uid {
            return Err("FORBIDDEN: Conta não autorizada.");
        }
    }
    Ok(())
}

// =========================================================================
// Contratos e DTOs da Fase 5: Operação, Pools e Prontidão (Readiness)
// =========================================================================

/// Estado individual de um componente monitorado pela sonda de prontidão.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ComponentStatus {
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl ComponentStatus {
    pub fn ok() -> Self {
        Self {
            status: "ok".to_string(),
            detail: None,
        }
    }

    pub fn ok_with(detail: impl Into<String>) -> Self {
        Self {
            status: "ok".to_string(),
            detail: Some(detail.into()),
        }
    }

    pub fn degraded(detail: impl Into<String>) -> Self {
        Self {
            status: "degraded".to_string(),
            detail: Some(detail.into()),
        }
    }

    pub fn unavailable(detail: impl Into<String>) -> Self {
        Self {
            status: "unavailable".to_string(),
            detail: Some(detail.into()),
        }
    }
}

/// Resposta estruturada da sonda de prontidão (Readiness Probe).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReadinessResponse {
    pub status: String,
    pub version: String,
    pub backend_sock: ComponentStatus,
    pub database: ComponentStatus,
    pub limiter: ComponentStatus,
    pub catalog_cache: ComponentStatus,
}

/// Estado das feature flags de rotas no gateway.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FeatureFlagsDto {
    pub read_routes: bool,
    pub write_routes: bool,
    pub vitals_write: bool,
    pub orb_ws: bool,
}

/// Métricas de uso do pool de conexões PostgreSQL.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DbPoolStatsDto {
    pub size: u32,
    pub num_idle: usize,
    pub max_connections: u32,
    pub min_connections: u32,
}

/// Telemetria e estatísticas operacionais internas.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InternalStatsResponse {
    pub status: String,
    pub version: String,
    pub uptime_seconds: u64,
    pub catalog_cache_entries: u64,
    pub features: FeatureFlagsDto,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub db_pool: Option<DbPoolStatsDto>,
}

// ============================================================================
// Protocolo Agêntico e Function Calling (Fase 0 - Mini Harness Android)
// ============================================================================

/// Papel do autor da mensagem no diálogo agêntico.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum AgentRole {
    System,
    User,
    Assistant,
    Tool,
}

/// Chamada de ferramenta solicitada pelo modelo de linguagem.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ToolCallDto {
    pub id: String,
    #[serde(rename = "type", default = "default_tool_type")]
    pub r#type: String,
    pub function: ToolCallFunctionDto,
}

fn default_tool_type() -> String {
    "function".to_string()
}

/// Detalhes da função na chamada de ferramenta.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ToolCallFunctionDto {
    pub name: String,
    pub arguments: String,
}

/// Mensagem individual no histórico de diálogo agêntico.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AgentMessageDto {
    pub role: AgentRole,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<Vec<ToolCallDto>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

/// Definição da assinatura da função de uma ferramenta.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ToolFunctionDefDto {
    pub name: String,
    pub description: String,
    pub parameters: serde_json::Value,
}

/// Declaração completa de ferramenta no catálogo exportado ao modelo.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ToolDeclarationDto {
    #[serde(rename = "type", default = "default_tool_type")]
    pub r#type: String,
    pub function: ToolFunctionDefDto,
}

/// Requisição de inferência agêntica para o relay (/api/ai/agent-relay).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AgentRelayRequest {
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    pub messages: Vec<AgentMessageDto>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tools: Option<Vec<ToolDeclarationDto>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,
}

/// Métricas de consumo de tokens da chamada de inferência.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct AgentUsageDto {
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub total_tokens: u32,
}

/// Resposta de inferência do relay agêntico para o harness Android.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AgentRelayResponse {
    pub status: String,
    pub message: AgentMessageDto,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage: Option<AgentUsageDto>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
}
