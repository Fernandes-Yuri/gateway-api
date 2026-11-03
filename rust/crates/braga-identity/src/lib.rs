use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use jsonwebtoken::{decode, decode_header, Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};
use sqlx::PgPool;
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
    let restriction = sqlx::query!(
        r#"
        SELECT level, reason
        FROM user_restrictions
        WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > NOW())
        "#,
        uid
    )
    .fetch_optional(pool)
    .await
    .map_err(|_| IdentityError::DatabaseUnavailable)?;

    if let Some(r) = restriction {
        let level = r.level;
        let reason = r.reason.unwrap_or_else(|| "Acesso restrito".to_string());

        let denied = level == "suspended"
            || (level == "read-only" && !matches!(method, "GET" | "HEAD" | "OPTIONS"))
            || (level == "no-sync" && method == "POST" && path.starts_with("/api/sync/"));

        if denied {
            let status = if level == "suspended" { 401 } else { 403 };
            return Err(IdentityError::RestrictedAccess { status, reason });
        }
    }

    // 2. Checagem de Tombstone (usuário excluído)
    if !(method == "DELETE" && path == format!("/api/profile/{uid}")) {
        let is_deleted = sqlx::query_scalar!(
            r#"
            SELECT (
                EXISTS(SELECT 1 FROM user_deletion_logs WHERE user_id = $1)
                AND NOT EXISTS(SELECT 1 FROM profiles WHERE id = $1)
            ) AS "is_deleted!"
            "#,
            uid
        )
        .fetch_one(pool)
        .await
        .map_err(|_| IdentityError::DatabaseUnavailable)?;

        if is_deleted {
            return Err(IdentityError::AccountDeleted);
        }
    }

    // 3. Checagem de Perfil Pausado
    let paused_at = sqlx::query_scalar!(r#"SELECT paused_at FROM profiles WHERE id = $1"#, uid)
        .fetch_optional(pool)
        .await
        .map_err(|_| IdentityError::DatabaseUnavailable)?
        .flatten();

    if let Some(paused) = paused_at {
        if path != format!("/api/profile/{uid}/reactivate") || method != "POST" {
            return Err(IdentityError::AccountPaused);
        }
        if let Some(at) = auth_time {
            if at <= paused.timestamp() {
                return Err(IdentityError::LoginRequiredToReactivate);
            }
        } else {
            return Err(IdentityError::LoginRequiredToReactivate);
        }
    }

    // 4. Checagem de Sessão Revogada
    if let Some(at) = auth_time {
        let session_revoked_at = sqlx::query_scalar!(
            r#"SELECT session_revoked_at FROM profiles WHERE id = $1"#,
            uid
        )
        .fetch_optional(pool)
        .await
        .map_err(|_| IdentityError::DatabaseUnavailable)?
        .flatten();

        if let Some(revoked) = session_revoked_at {
            if at <= revoked.timestamp() {
                return Err(IdentityError::SessionRevoked);
            }
        }
    }

    Ok(())
}
