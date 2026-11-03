use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use sqlx::{PgPool, Row};
use thiserror::Error;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::Mutex;
use tracing::{error, info, warn};

#[derive(Debug, Error)]
pub enum LimiterError {
    #[error("IP bloqueado por 24 horas")]
    IpBlocked { remaining_seconds: u64 },
    #[error("Muitas tentativas. Aguarde {retry_after}s.")]
    RateLimitExceeded { retry_after: u64 },
    #[error("Limite de seguranca violado 3 vezes. Endereco IP bloqueado por 24 horas.")]
    ThresholdViolationBlocked,
    #[error("Erro de persistencia no banco: {0}")]
    DbError(String),
}

#[derive(Clone)]
pub struct StrictRateLimiter {
    hits: Arc<Mutex<HashMap<String, VecDeque<Instant>>>>,
    violations: Arc<Mutex<HashMap<String, VecDeque<Instant>>>>,
    blocked_ips: Arc<Mutex<HashMap<String, Instant>>>,
}

impl Default for StrictRateLimiter {
    fn default() -> Self {
        Self {
            hits: Arc::new(Mutex::new(HashMap::new())),
            violations: Arc::new(Mutex::new(HashMap::new())),
            blocked_ips: Arc::new(Mutex::new(HashMap::new())),
        }
    }
}

impl StrictRateLimiter {
    pub fn new() -> Self {
        Self::default()
    }

    pub async fn set_manual_block(&self, ip: &str, blocked: bool, seconds: u64) {
        let mut b_lock = self.blocked_ips.lock().await;
        if blocked {
            b_lock.insert(
                ip.to_string(),
                Instant::now() + Duration::from_secs(seconds),
            );
        } else {
            b_lock.remove(ip);
            let mut v_lock = self.violations.lock().await;
            v_lock.remove(ip);
        }
    }

    pub async fn load_blocked_ips_from_db(&self, pool: &PgPool) {
        let rows = sqlx::query(
            "SELECT ip, EXTRACT(EPOCH FROM (blocked_until - NOW()))::float8 as remaining_sec FROM security_blocked_ips WHERE blocked_until > NOW()",
        )
        .fetch_all(pool)
        .await;

        match rows {
            Ok(list) => {
                let now = Instant::now();
                let mut lock = self.blocked_ips.lock().await;
                for r in list {
                    let ip: String = r.get("ip");
                    let rem: Option<f64> = r.get("remaining_sec");
                    if let Some(sec) = rem {
                        if sec > 0.0 {
                            lock.insert(ip, now + Duration::from_secs_f64(sec));
                        }
                    }
                }
                info!("[SECURITY FIREWALL] IPs bloqueados restaurados do PostgreSQL.");
            }
            Err(e) => {
                warn!("[SECURITY FIREWALL] Falha ao carregar IPs bloqueados do banco: {e}");
            }
        }
    }

    pub async fn is_ip_blocked(&self, ip: &str) -> (bool, u64) {
        if ip.is_empty() || matches!(ip, "127.0.0.1" | "::1" | "localhost" | "unknown") {
            return (false, 0);
        }

        let now = Instant::now();
        let mut lock = self.blocked_ips.lock().await;

        if let Some(&blocked_until) = lock.get(ip) {
            if now < blocked_until {
                let rem = (blocked_until - now).as_secs().max(1);
                return (true, rem);
            } else {
                lock.remove(ip);
                let mut v_lock = self.violations.lock().await;
                v_lock.remove(ip);
            }
        }

        (false, 0)
    }

    pub async fn record_violation(
        &self,
        ip: &str,
        reason: &str,
        severity: usize,
        pool: Option<&PgPool>,
    ) -> bool {
        if ip.is_empty() || matches!(ip, "127.0.0.1" | "::1" | "localhost" | "unknown") {
            return false;
        }

        let now = Instant::now();
        let cutoff_24h = now.checked_sub(Duration::from_secs(86400)).unwrap_or(now);

        let mut v_lock = self.violations.lock().await;
        let v_list = v_lock.entry(ip.to_string()).or_default();

        while let Some(&front) = v_list.front() {
            if front <= cutoff_24h {
                v_list.pop_front();
            } else {
                break;
            }
        }

        for _ in 0..severity {
            v_list.push_back(now);
        }

        let count = v_list.len();
        info!("[SECURITY FIREWALL] Violacao para IP {ip}: {reason} (Total em 24h: {count}/3)");

        if count >= 3 {
            let mut b_lock = self.blocked_ips.lock().await;
            b_lock.insert(ip.to_string(), now + Duration::from_secs(86400));
            warn!("[SECURITY ALERT] >>> IP {ip} BLOQUEADO POR 24H <<< Motivo: {reason} (Violacoes: {count})");

            if let Some(p) = pool {
                let ip_clone = ip.to_string();
                let reason_clone = reason.to_string();
                let count_i32 = count as i32;
                let pool_clone = p.clone();

                tokio::spawn(async move {
                    let _ = sqlx::query(
                        r#"
                        INSERT INTO security_blocked_ips (ip, reason, violations, blocked_until, updated_at)
                        VALUES ($1, $2, $3, NOW() + INTERVAL '24 hours', NOW())
                        ON CONFLICT (ip) DO UPDATE SET
                            reason = EXCLUDED.reason,
                            violations = security_blocked_ips.violations + EXCLUDED.violations,
                            blocked_until = NOW() + INTERVAL '24 hours',
                            updated_at = NOW()
                        "#,
                    )
                    .bind(ip_clone)
                    .bind(reason_clone)
                    .bind(count_i32)
                    .execute(&pool_clone)
                    .await;
                });
            }

            return true;
        }

        false
    }

    pub async fn check(
        &self,
        scope: &str,
        key: &str,
        limit: usize,
        window_seconds: u64,
        ip: Option<&str>,
        pool: Option<&PgPool>,
    ) -> Result<(), LimiterError> {
        let target_ip = ip.or_else(|| {
            if scope.ends_with("_ip") || scope.starts_with("ip_") {
                Some(key)
            } else {
                None
            }
        });

        // 1. Verifica se já está bloqueado
        if let Some(tip) = target_ip {
            let (blocked, rem) = self.is_ip_blocked(tip).await;
            if blocked {
                return Err(LimiterError::IpBlocked {
                    remaining_seconds: rem,
                });
            }
        }

        let now = Instant::now();
        let bucket_key = format!("{scope}:{key}");
        let cutoff = now
            .checked_sub(Duration::from_secs(window_seconds))
            .unwrap_or(now);

        let mut hits_overflow = false;
        let mut retry_after = 1;

        {
            let mut h_lock = self.hits.lock().await;
            let hits = h_lock.entry(bucket_key).or_default();

            while let Some(&front) = hits.front() {
                if front <= cutoff {
                    hits.pop_front();
                } else {
                    break;
                }
            }

            if hits.len() >= limit {
                if let Some(&first) = hits.front() {
                    let elapsed = now.duration_since(first).as_secs();
                    retry_after = (window_seconds.saturating_sub(elapsed) + 1).max(1);
                }
                hits_overflow = true;
            } else {
                hits.push_back(now);
            }
        }

        if hits_overflow {
            if let Some(tip) = target_ip {
                let reason =
                    format!("Rate limit excedido em {scope}: {limit} reqs em {window_seconds}s");
                let is_now_blocked = self.record_violation(tip, &reason, 1, pool).await;
                if is_now_blocked {
                    return Err(LimiterError::ThresholdViolationBlocked);
                }
            }
            return Err(LimiterError::RateLimitExceeded { retry_after });
        }

        Ok(())
    }
}

// ---------------------------------------------------------------------------
// IPC Server Unix Socket para integração com Python
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum LimiterIpcRequest {
    Check {
        scope: String,
        key: String,
        limit: usize,
        window_seconds: u64,
        ip: Option<String>,
    },
    RecordViolation {
        ip: String,
        reason: String,
        severity: Option<usize>,
    },
    SetManualBlock {
        ip: String,
        blocked: bool,
        seconds: Option<u64>,
    },
}

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum LimiterIpcResponse {
    Ok,
    Rejected {
        http_status: u16,
        detail: String,
        retry_after: Option<u64>,
        blocked: bool,
    },
    Error {
        message: String,
    },
}

pub async fn run_limiter_ipc_socket(
    socket_path: String,
    limiter: StrictRateLimiter,
    pool: Option<PgPool>,
) -> std::io::Result<()> {
    let _ = std::fs::remove_file(&socket_path);
    let listener = UnixListener::bind(&socket_path)?;

    info!("[LIMITER IPC] Ouvindo canal de controle em {socket_path}");

    loop {
        match listener.accept().await {
            Ok((stream, _)) => {
                let lim = limiter.clone();
                let p = pool.clone();
                tokio::spawn(async move {
                    handle_ipc_client(stream, lim, p.as_ref()).await;
                });
            }
            Err(e) => {
                error!("[LIMITER IPC] Erro no accept: {e}");
            }
        }
    }
}

async fn handle_ipc_client(stream: UnixStream, limiter: StrictRateLimiter, pool: Option<&PgPool>) {
    let (reader, mut writer) = stream.into_split();
    let mut lines = BufReader::new(reader).lines();

    while let Ok(Some(line)) = lines.next_line().await {
        let req: Result<LimiterIpcRequest, _> = serde_json::from_str(&line);
        let resp = match req {
            Ok(LimiterIpcRequest::Check {
                scope,
                key,
                limit,
                window_seconds,
                ip,
            }) => match limiter
                .check(&scope, &key, limit, window_seconds, ip.as_deref(), pool)
                .await
            {
                Ok(()) => LimiterIpcResponse::Ok,
                Err(LimiterError::IpBlocked { remaining_seconds }) => {
                    LimiterIpcResponse::Rejected {
                        http_status: 403,
                        detail: "Acesso bloqueado por 24 horas para este endereco IP devido a excesso de tentativas e violacoes de seguranca.".to_string(),
                        retry_after: Some(remaining_seconds),
                        blocked: true,
                    }
                }
                Err(LimiterError::ThresholdViolationBlocked) => LimiterIpcResponse::Rejected {
                    http_status: 403,
                    detail: "Limite de seguranca violado 3 vezes. Seu endereco IP foi bloqueado por 24 horas.".to_string(),
                    retry_after: Some(86400),
                    blocked: true,
                },
                Err(LimiterError::RateLimitExceeded { retry_after }) => {
                    LimiterIpcResponse::Rejected {
                        http_status: 429,
                        detail: "Muitas tentativas. Aguarde alguns minutos antes de tentar novamente.".to_string(),
                        retry_after: Some(retry_after),
                        blocked: false,
                    }
                }
                Err(LimiterError::DbError(msg)) => LimiterIpcResponse::Error { message: msg },
            },
            Ok(LimiterIpcRequest::RecordViolation {
                ip,
                reason,
                severity,
            }) => {
                limiter
                    .record_violation(&ip, &reason, severity.unwrap_or(1), pool)
                    .await;
                LimiterIpcResponse::Ok
            }
            Ok(LimiterIpcRequest::SetManualBlock {
                ip,
                blocked,
                seconds,
            }) => {
                limiter
                    .set_manual_block(&ip, blocked, seconds.unwrap_or(86400))
                    .await;
                LimiterIpcResponse::Ok
            }
            Err(e) => LimiterIpcResponse::Error {
                message: format!("Payload JSON IPC invalido: {e}"),
            },
        };

        if let Ok(mut json) = serde_json::to_string(&resp) {
            json.push('\n');
            let _ = writer.write_all(json.as_bytes()).await;
        }
    }
}
