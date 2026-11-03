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
