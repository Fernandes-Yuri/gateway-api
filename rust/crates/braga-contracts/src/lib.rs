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
