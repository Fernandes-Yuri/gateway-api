use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::NaiveDateTime;
use jsonwebtoken::{decode, decode_header, Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};
use sqlx::{PgPool, Row};
use thiserror::Error;
use tokio::sync::RwLock;

const GOOGLE_CERTS_URL: &str =
    "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FirebaseClaims {
    pub sub: String,
    pub auth_time: i64,
    pub iss: String,
    pub aud: String,
    pub exp: i64,
    pub iat: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VerifiedIdentity {
    pub uid: String,
    pub auth_time: i64,
}

#[derive(Debug, Error)]
pub enum IdentityError {
    #[error("Token de autenticacao ausente ou malformado.")]
    MissingToken,
    #[error("Falha ao validar token Firebase: {0}")]
    InvalidToken(String),
    #[error("Kid desconhecido nos certificados publicos do Firebase.")]
    UnknownKid,
    #[error("Erro ao buscar certificados publicos do Google: {0}")]
    CertFetchError(String),
    #[error("Sessao invalida ou expirada. Entre novamente.")]
    ExpiredOrInvalidSession,
    #[error("Banco de dados indisponivel.")]
    DatabaseUnavailable,
    #[error("Acesso restrito: {status} - {reason}")]
    RestrictedAccess { status: u16, reason: String },
    #[error("Esta conta foi excluída. Entre com outra conta para continuar.")]
    AccountDeleted,
    #[error("Conta pausada. Faça um novo login para retornar.")]
    AccountPaused,
    #[error("Faça um novo login para reativar sua conta.")]
    LoginRequiredToReactivate,
    #[error("Sessão revogada. Entre novamente.")]
    SessionRevoked,
    #[error("Esta operação pertence a outra conta.")]
    ResourceForbidden,
    #[error("O acompanhamento deste familiar não está autorizado.")]
    FamilyAccessForbidden,
    #[error("Seu vínculo não permite esta operação.")]
    PatientPermissionForbidden,
}

/// Gerenciador em memória do cache de certificados públicos do Firebase
#[derive(Clone)]
pub struct FirebaseCertCache {
    certs: Arc<RwLock<HashMap<String, String>>>,
    last_fetch: Arc<RwLock<Option<Instant>>>,
    http_client: reqwest::Client,
}

impl Default for FirebaseCertCache {
    fn default() -> Self {
        Self {
            certs: Arc::new(RwLock::new(HashMap::new())),
            last_fetch: Arc::new(RwLock::new(None)),
            http_client: reqwest::Client::new(),
        }
    }
}

impl FirebaseCertCache {
    pub fn new() -> Self {
        Self::default()
    }

    pub async fn get_cert(&self, kid: &str) -> Result<String, IdentityError> {
        {
            let map = self.certs.read().await;
            if let Some(cert) = map.get(kid) {
                return Ok(cert.clone());
            }
        }

        self.refresh().await?;

        let map = self.certs.read().await;
        map.get(kid).cloned().ok_or(IdentityError::UnknownKid)
    }

    pub async fn refresh(&self) -> Result<(), IdentityError> {
        let mut last = self.last_fetch.write().await;
        if let Some(instant) = *last {
            if instant.elapsed() < Duration::from_secs(300) {
                return Ok(());
            }
        }

        let resp = self
            .http_client
            .get(GOOGLE_CERTS_URL)
            .timeout(Duration::from_secs(5))
            .send()
            .await
            .map_err(|e| IdentityError::CertFetchError(e.to_string()))?;

        let certs_map: HashMap<String, String> = resp
            .json()
            .await
            .map_err(|e| IdentityError::CertFetchError(e.to_string()))?;

        let mut lock = self.certs.write().await;
        *lock = certs_map;
        *last = Some(Instant::now());

        Ok(())
    }
}

/// Valida um token de ID do Firebase em formato Bearer
pub async fn verify_firebase_id_token(
    token_str: &str,
    expected_project_id: &str,
    cert_cache: &FirebaseCertCache,
) -> Result<VerifiedIdentity, IdentityError> {
    let header =
        decode_header(token_str).map_err(|e| IdentityError::InvalidToken(e.to_string()))?;

    let kid = header.kid.ok_or(IdentityError::MissingToken)?;
    let cert_pem = cert_cache.get_cert(&kid).await?;

    let decoding_key = DecodingKey::from_rsa_pem(cert_pem.as_bytes())
        .map_err(|e| IdentityError::InvalidToken(e.to_string()))?;

    let mut validation = Validation::new(Algorithm::RS256);
    validation.set_audience(&[expected_project_id]);
    validation.set_issuer(&[format!(
        "https://securetoken.google.com/{expected_project_id}"
    )]);

    let token_data = decode::<FirebaseClaims>(token_str, &decoding_key, &validation)
        .map_err(|e| IdentityError::InvalidToken(e.to_string()))?;

    let claims = token_data.claims;
    if claims.sub.trim().is_empty() {
        return Err(IdentityError::InvalidToken("sub vazio".to_string()));
    }

    Ok(VerifiedIdentity {
        uid: claims.sub,
        auth_time: claims.auth_time,
    })
}

/// Executa a política autoritativa de moderação no PostgreSQL (user_moderation.py)
pub async fn check_user_moderation(
    pool: &PgPool,
    uid: &str,
    method: &str,
    path: &str,
    auth_time: Option<i64>,
) -> Result<(), IdentityError> {
    if path.starts_with("/api/admin/") || (path == "/api/app/latest" && method == "GET") {
        return Ok(());
    }

    // 1. Checagem de user_restrictions
    let restriction_row = sqlx::query(
        "SELECT level, reason FROM user_restrictions WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > NOW())",
    )
    .bind(uid)
    .fetch_optional(pool)
    .await
    .map_err(|_| IdentityError::DatabaseUnavailable)?;

    if let Some(r) = restriction_row {
        let level: String = r.get("level");
        let reason: Option<String> = r.get("reason");
        let reason_str = reason.unwrap_or_else(|| "Acesso restrito".to_string());

        let denied = level == "suspended"
            || (level == "read-only" && !matches!(method, "GET" | "HEAD" | "OPTIONS"))
            || (level == "no-sync" && method == "POST" && path.starts_with("/api/sync/"));

        if denied {
            let status = if level == "suspended" { 401 } else { 403 };
            return Err(IdentityError::RestrictedAccess {
                status,
                reason: reason_str,
            });
        }
    }

    // 2. Checagem de Tombstone (usuário excluído)
    if !(method == "DELETE" && path == format!("/api/profile/{uid}")) {
        let is_deleted_row = sqlx::query(
            "SELECT (EXISTS(SELECT 1 FROM user_deletion_logs WHERE user_id = $1) AND NOT EXISTS(SELECT 1 FROM profiles WHERE id = $1)) as is_deleted",
        )
        .bind(uid)
        .fetch_one(pool)
        .await
        .map_err(|_| IdentityError::DatabaseUnavailable)?;

        let is_deleted: bool = is_deleted_row.get("is_deleted");
        if is_deleted {
            return Err(IdentityError::AccountDeleted);
        }
    }

    // 3. Checagem de Perfil Pausado
    let paused_row = sqlx::query("SELECT paused_at FROM profiles WHERE id = $1")
        .bind(uid)
        .fetch_optional(pool)
        .await
        .map_err(|_| IdentityError::DatabaseUnavailable)?;

    if let Some(r) = paused_row {
        let paused_at: Option<NaiveDateTime> = r.get("paused_at");
        if let Some(paused) = paused_at {
            if path != format!("/api/profile/{uid}/reactivate") || method != "POST" {
                return Err(IdentityError::AccountPaused);
            }
            if let Some(at) = auth_time {
                if at <= paused.and_utc().timestamp() {
                    return Err(IdentityError::LoginRequiredToReactivate);
                }
            } else {
                return Err(IdentityError::LoginRequiredToReactivate);
            }
        }
    }

    // 4. Checagem de Sessão Revogada
    if let Some(at) = auth_time {
        let session_row = sqlx::query("SELECT session_revoked_at FROM profiles WHERE id = $1")
            .bind(uid)
            .fetch_optional(pool)
            .await
            .map_err(|_| IdentityError::DatabaseUnavailable)?;

        if let Some(r) = session_row {
            let session_revoked_at: Option<NaiveDateTime> = r.get("session_revoked_at");
            if let Some(revoked) = session_revoked_at {
                if at <= revoked.and_utc().timestamp() {
                    return Err(IdentityError::SessionRevoked);
                }
            }
        }
    }

    Ok(())
}

/// Verifica se o usuário autenticado é o próprio titular do recurso (require_owner)
pub fn require_owner(caller_uid: &str, target_uid: &str) -> Result<(), IdentityError> {
    if caller_uid != target_uid {
        return Err(IdentityError::ResourceForbidden);
    }
    Ok(())
}

/// Verifica se o usuário autenticado tem acesso aos dados familiares (titular ou vínculo ativo)
pub async fn require_family_access(
    pool: &PgPool,
    caller_uid: &str,
    patient_id: &str,
) -> Result<(), IdentityError> {
    if caller_uid == patient_id {
        return Ok(());
    }

    let allowed: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM family_bindings WHERE patient_id = $1 AND caregiver_id = $2 AND status = 'ACTIVE')"
    )
    .bind(patient_id)
    .bind(caller_uid)
    .fetch_one(pool)
    .await
    .map_err(|_| IdentityError::DatabaseUnavailable)?;

    if !allowed {
        return Err(IdentityError::FamilyAccessForbidden);
    }

    Ok(())
}

/// Etapa 1: titular do recurso OU cuidador com vínculo ACTIVE na mesma conta.
/// Usado em rotas de sinais de saúde (métricas diárias, passos, hidratação) para
/// liberar o acompanhamento legítimo do cuidador autorizado sem abrir mão do
/// Zero-Trust: quem não é o titular precisa estar em family_bindings.
pub async fn require_owner_or_caregiver(
    pool: &PgPool,
    caller_uid: &str,
    target_uid: &str,
) -> Result<(), IdentityError> {
    require_family_access(pool, caller_uid, target_uid).await
}

/// Verifica se o usuário autenticado possui permissão clínica específica no vínculo familiar
pub async fn require_patient_permission(
    pool: &PgPool,
    caller_uid: &str,
    patient_id: &str,
    permission: &str,
) -> Result<(), IdentityError> {
    if caller_uid == patient_id {
        return Ok(());
    }

    let row = sqlx::query(
        "SELECT caregiver_role, permissions FROM family_bindings WHERE patient_id = $1 AND caregiver_id = $2 AND status = 'ACTIVE'"
    )
    .bind(patient_id)
    .bind(caller_uid)
    .fetch_optional(pool)
    .await
    .map_err(|_| IdentityError::DatabaseUnavailable)?;

    let row = match row {
        Some(r) => r,
        None => return Err(IdentityError::FamilyAccessForbidden),
    };

    let role: String = row
        .try_get("caregiver_role")
        .unwrap_or_else(|_| "CAREGIVER_VIEWER".to_string());
    let explicit_perms: Vec<String> = row.try_get("permissions").unwrap_or_default();

    // Regras de papel canônicas (ROLE_PERMISSIONS em services/care_os.py)
    let role_allows = match role.as_str() {
        "CAREGIVER_ADMIN" | "ADMIN" => true,
        "CAREGIVER_PRIMARY" => matches!(
            permission,
            "care:read" | "medication:write" | "vitals:write"
        ),
        "CAREGIVER_VIEWER" | "VIEWER" => permission == "care:read",
        _ => false,
    };

    if role_allows || explicit_perms.iter().any(|p| p == permission || p == "*") {
        Ok(())
    } else {
        Err(IdentityError::PatientPermissionForbidden)
    }
}
