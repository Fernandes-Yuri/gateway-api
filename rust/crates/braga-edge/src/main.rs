use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use axum::body::Body;
use axum::extract::{Path, Query, Request, State};
use axum::http::header::{HeaderMap, HeaderName, HeaderValue, CACHE_CONTROL, CONTENT_TYPE};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{any, get, post};
use axum::Router;
use braga_contracts::{
    compute_imc, compute_occurrence_id, format_agent_closing, height_in_meters,
    is_valid_agent_action, is_websocket_upgrade_headers, normalize_schedule_times,
    ActivityFeedRecord, AgentReceiptDto, AgentReceiptResponse, AppUnavailableResponse,
    AudioRetiredResponse, AuditLogSyncDto, BarcodeMedication, BiometryRecord, BiometrySyncDto,
    DailyMetricRecord, DailyMetricSyncDto, HealthResponse, HttpErrorResponse, LandingResponse,
    MedicationLogRecord, MedicationLogSyncDto, MedicationRecord, MedicationSyncDto, OrbChatRequest,
    OrbErrorCode, OrbErrorResponse, OrbMessage, OrbRole, SyncResponse, VitalSignRecord,
    VitalSignSyncDto,
};
use braga_identity::{
    check_user_moderation, require_family_access, require_owner, require_patient_permission,
    verify_firebase_id_token, FirebaseCertCache, VerifiedIdentity,
};
use braga_limiter::{run_limiter_ipc_socket, StrictRateLimiter};
use chrono::{DateTime, Utc};
use clap::Parser;
use hyper_util::rt::TokioIo;
use serde::Deserialize;
use sqlx::{PgPool, Row};
use tokio::net::UnixStream;
use tracing::{error, info, warn};

#[derive(Parser, Debug)]
#[command(author, version, about = "BragaSaude Gateway Edge Proxy (Rust)")]
pub struct CliArgs {
    #[arg(short, long, env = "PORT", default_value_t = 8080)]
    pub port: u16,

    #[arg(long, env = "HOST", default_value = "127.0.0.1")]
    pub host: String,

    #[arg(
        long,
        env = "BACKEND_SOCK",
        default_value = "/run/braga-saude/backend.sock"
    )]
    pub backend_sock: PathBuf,

    #[arg(
        long,
        env = "LIMITER_SOCK",
        default_value = "/run/braga-saude/limiter.sock"
    )]
    pub limiter_sock: PathBuf,

    #[arg(long, env = "DATABASE_URL")]
    pub database_url: Option<String>,

    #[arg(long, env = "FIREBASE_PROJECT_ID", default_value = "bragasaude-app")]
    pub firebase_project_id: String,

    #[arg(long, env = "READ_ROUTES_ENABLED", default_value_t = true)]
    pub read_routes_enabled: bool,

    #[arg(long, env = "WRITE_ROUTES_ENABLED", default_value_t = true)]
    pub write_routes_enabled: bool,

    #[arg(long, env = "VITALS_WRITE_ENABLED", default_value_t = false)]
    pub vitals_write_enabled: bool,

    #[arg(long, env = "ORB_WS_ENABLED", default_value_t = true)]
    pub orb_ws_enabled: bool,
}

#[derive(Clone)]
pub struct AppState {
    pub backend_sock: PathBuf,
    pub cert_cache: FirebaseCertCache,
    pub limiter: StrictRateLimiter,
    pub pool: Option<PgPool>,
    pub firebase_project_id: String,
    pub catalog_cache: moka::future::Cache<String, braga_contracts::BarcodeMedication>,
    pub read_routes_enabled: bool,
    pub write_routes_enabled: bool,
    pub vitals_write_enabled: bool,
    pub orb_ws_enabled: bool,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();
    let args = CliArgs::parse();

    info!("Iniciando braga-edge na porta {}:{}", args.host, args.port);

    let pool = if let Some(ref db_url) = args.database_url {
        match PgPool::connect(db_url).await {
            Ok(p) => {
                info!("Conectado ao PostgreSQL com sucesso.");
                Some(p)
            }
            Err(e) => {
                warn!("Falha ao conectar no PostgreSQL: {e}. Operando sem pool local.");
                None
            }
        }
    } else {
        None
    };

    let limiter = StrictRateLimiter::new();
    if let Some(ref p) = pool {
        limiter.load_blocked_ips_from_db(p).await;
    }

    // Inicia o socket Unix IPC para os handlers legados do Python
    let limiter_clone = limiter.clone();
    let limiter_sock_path = args.limiter_sock.to_string_lossy().to_string();
    let pool_clone = pool.clone();
    tokio::spawn(async move {
        if let Err(e) = run_limiter_ipc_socket(limiter_sock_path, limiter_clone, pool_clone).await {
            error!("Erro no IPC do limiter: {e}");
        }
    });

    let catalog_cache: moka::future::Cache<String, BarcodeMedication> =
        moka::future::Cache::builder()
            .max_capacity(10_000)
            .time_to_live(std::time::Duration::from_secs(300))
            .build();

    let state = Arc::new(AppState {
        backend_sock: args.backend_sock,
        cert_cache: FirebaseCertCache::new(),
        limiter,
        pool,
        firebase_project_id: args.firebase_project_id,
        catalog_cache,
        read_routes_enabled: args.read_routes_enabled,
        write_routes_enabled: args.write_routes_enabled,
        vitals_write_enabled: args.vitals_write_enabled,
        orb_ws_enabled: args.orb_ws_enabled,
    });

    let mut app = Router::new()
        // Rotas públicas servidas diretamente pela borda nativa
        .route("/health", get(health_handler))
        .route("/", get(landing_handler))
        .route(
            "/api/app/latest",
            get(apk_retired_handler).options(options_cors_handler),
        )
        .route(
            "/api/app/download",
            get(apk_retired_handler).options(options_cors_handler),
        )
        .route(
            "/api/app/version",
            get(apk_retired_handler).options(options_cors_handler),
        )
        .route(
            "/api/app/notify-update",
            post(apk_retired_handler).options(options_cors_handler),
        )
        // Fase 4: Áudio Legado Aposentado (410 Gone)
        .route(
            "/synthesize_voice",
            post(audio_retired_handler).options(options_cors_handler),
        )
        .route(
            "/v1/audio/speech",
            get(audio_retired_handler)
                .post(audio_retired_handler)
                .options(options_cors_handler),
        )
        // Fase 2A: Catálogo ANVISA e Cache Moka
        .route(
            "/api/anvisa/medications/barcode/:ean",
            get(anvisa_barcode_handler),
        )
        // Fase 2B: Leituras Pessoais com Autorização Autoritativa
        .route("/api/vitals/:user_id", get(vitals_handler))
        .route("/api/biometry/:user_id", get(biometry_handler))
        .route("/api/metrics/daily/:user_id", get(daily_metrics_handler))
        .route("/api/medications/:user_id", get(medications_handler))
        .route(
            "/api/medications/:user_id/logs",
            get(medication_logs_handler),
        )
        .route(
            "/api/sync/medication-logs/:user_id",
            get(medication_logs_handler),
        )
        .route(
            "/api/family/patients/:patient_id/activity-feed",
            get(activity_feed_handler),
        );

    // Fase 3: Escritas de Alta Frequência e Sincronização
    if args.write_routes_enabled {
        app = app
            .route("/api/sync/biometry", post(sync_biometry_handler))
            .route("/api/sync/daily-metrics", post(sync_daily_metrics_handler))
            .route("/api/sync/medication", post(sync_medication_handler))
            .route(
                "/api/sync/medication-log",
                post(sync_medication_log_handler),
            )
            .route("/api/sync/audit-log", post(sync_audit_log_handler))
            .route("/api/agent/receipt", post(agent_receipt_handler));

        if args.vitals_write_enabled {
            app = app.route("/api/sync/vitals", post(sync_vitals_handler));
        }
    }

    // Fase 4: Orb WebSocket Transport
    if args.orb_ws_enabled {
        app = app.route("/ws/orb", get(ws_orb_handler));
    }

    let app = app
        // Fallback: proxy reverso transparente para o socket Unix do Python
        .fallback(any(proxy_to_backend))
        .with_state(state);

    let addr: SocketAddr = format!("{}:{}", args.host, args.port).parse()?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    info!("braga-edge escutando em http://{}", addr);

    axum::serve(listener, app).await?;
    Ok(())
}

async fn health_handler() -> impl IntoResponse {
    let mut headers = HeaderMap::new();
    headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    (StatusCode::OK, headers, Json(HealthResponse::default()))
}

async fn landing_handler() -> impl IntoResponse {
    let mut headers = HeaderMap::new();
    headers.insert(
        HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    (StatusCode::OK, headers, Json(LandingResponse::default()))
}

async fn apk_retired_handler() -> impl IntoResponse {
    let mut headers = HeaderMap::new();
    headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        HeaderName::from_static("access-control-allow-origin"),
        HeaderValue::from_static("*"),
    );
    headers.insert(
        HeaderName::from_static("access-control-allow-methods"),
        HeaderValue::from_static("GET, OPTIONS, POST"),
    );
    (
        StatusCode::GONE,
        headers,
        Json(AppUnavailableResponse::default()),
    )
}

async fn options_cors_handler() -> impl IntoResponse {
    let mut headers = HeaderMap::new();
    headers.insert(
        HeaderName::from_static("access-control-allow-origin"),
        HeaderValue::from_static("*"),
    );
    headers.insert(
        HeaderName::from_static("access-control-allow-methods"),
        HeaderValue::from_static("GET, OPTIONS, POST"),
    );
    headers.insert(
        HeaderName::from_static("access-control-allow-headers"),
        HeaderValue::from_static("*"),
    );
    (StatusCode::NO_CONTENT, headers)
}

async fn audio_retired_handler() -> impl IntoResponse {
    let mut headers = HeaderMap::new();
    headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        HeaderName::from_static("access-control-allow-origin"),
        HeaderValue::from_static("*"),
    );
    headers.insert(
        HeaderName::from_static("access-control-allow-methods"),
        HeaderValue::from_static("GET, OPTIONS, POST"),
    );
    (
        StatusCode::GONE,
        headers,
        Json(AudioRetiredResponse::default()),
    )
}

async fn ws_orb_handler(State(state): State<Arc<AppState>>, mut req: Request) -> Response {
    let client_ip = req
        .headers()
        .get("cf-connecting-ip")
        .or_else(|| req.headers().get("x-forwarded-for"))
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.split(',').next())
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|| "127.0.0.1".to_string());

    let (blocked, rem) = state.limiter.is_ip_blocked(&client_ip).await;
    if blocked {
        let mut resp = Response::new(Body::from(format!(
            "{{\"detail\":\"Acesso bloqueado por 24 horas para este IP.\",\"retry_after\":{rem}}}"
        )));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return resp;
    }

    let connection_hdr = req
        .headers()
        .get("connection")
        .and_then(|v| v.to_str().ok());
    let upgrade_hdr = req.headers().get("upgrade").and_then(|v| v.to_str().ok());
    if !is_websocket_upgrade_headers(connection_hdr, upgrade_hdr) {
        let mut resp = Response::new(Body::from(
            "{\"detail\":\"Upgrade to WebSocket required.\"}",
        ));
        *resp.status_mut() = StatusCode::UPGRADE_REQUIRED;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return resp;
    }

    let identity = match authenticate_request(req.headers(), &state, "GET", "/ws/orb").await {
        Ok(id) => id,
        Err(err_resp) => return err_resp,
    };

    let header_keys: Vec<HeaderName> = req
        .headers()
        .keys()
        .filter(|k| k.as_str().starts_with("x-internal-"))
        .cloned()
        .collect();
    for k in header_keys {
        req.headers_mut().remove(&k);
    }

    if let Ok(val) = HeaderValue::from_str(&identity.uid) {
        req.headers_mut()
            .insert(HeaderName::from_static("x-internal-uid"), val);
    }
    if let Ok(val) = HeaderValue::from_str(&identity.auth_time.to_string()) {
        req.headers_mut()
            .insert(HeaderName::from_static("x-internal-auth-time"), val);
    }
    if let Ok(val) = HeaderValue::from_str(&client_ip) {
        req.headers_mut()
            .insert(HeaderName::from_static("x-internal-client-ip"), val);
    }

    let client_upgrade = hyper::upgrade::on(&mut req);

    let stream = match UnixStream::connect(&state.backend_sock).await {
        Ok(s) => s,
        Err(e) => {
            error!(
                "Falha ao conectar no socket do backend {:?}: {e}",
                state.backend_sock
            );
            let mut resp = Response::new(Body::from("{\"detail\":\"Bad Gateway\"}"));
            *resp.status_mut() = StatusCode::BAD_GATEWAY;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let io = TokioIo::new(stream);
    let (mut sender, conn) = match hyper::client::conn::http1::handshake(io).await {
        Ok(res) => res,
        Err(e) => {
            error!("Handshake HTTP/1 com backend Unix falhou: {e}");
            let mut resp = Response::new(Body::from("{\"detail\":\"Bad Gateway\"}"));
            *resp.status_mut() = StatusCode::BAD_GATEWAY;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    tokio::spawn(async move {
        if let Err(err) = conn.with_upgrades().await {
            warn!("Conexao upgrade com backend encerrou: {err:?}");
        }
    });

    let mut backend_resp = match sender.send_request(req).await {
        Ok(r) => r,
        Err(e) => {
            error!("Erro enviando requisição ao backend: {e}");
            let mut resp = Response::new(Body::from("{\"detail\":\"Bad Gateway\"}"));
            *resp.status_mut() = StatusCode::BAD_GATEWAY;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    if backend_resp.status() == StatusCode::SWITCHING_PROTOCOLS {
        let backend_upgrade = hyper::upgrade::on(&mut backend_resp);
        tokio::spawn(async move {
            match tokio::try_join!(client_upgrade, backend_upgrade) {
                Ok((client_upgraded, backend_upgraded)) => {
                    let mut client_io = TokioIo::new(client_upgraded);
                    let mut backend_io = TokioIo::new(backend_upgraded);
                    if let Err(e) =
                        tokio::io::copy_bidirectional(&mut client_io, &mut backend_io).await
                    {
                        warn!("WebSocket tunnel /ws/orb encerrou: {e}");
                    }
                }
                Err(err) => {
                    warn!("Falha no handshake de upgrade do WebSocket: {err}");
                }
            }
        });
    }

    backend_resp.into_response()
}

async fn proxy_to_backend(
    State(state): State<Arc<AppState>>,
    mut req: Request,
) -> Result<Response, StatusCode> {
    // 1. Extração segura do IP do cliente via CF-Connecting-IP
    let client_ip = req
        .headers()
        .get("cf-connecting-ip")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("127.0.0.1")
        .to_string();

    // 2. Checagem do firewall de IP
    let (blocked, rem) = state.limiter.is_ip_blocked(&client_ip).await;
    if blocked {
        let mut resp = Response::new(Body::from(format!(
            "{{\"detail\":\"Acesso bloqueado por 24 horas para este IP.\",\"retry_after\":{rem}}}"
        )));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return Ok(resp);
    }

    // 3. Sanitização: remove headers internos enviados pelo cliente
    let header_keys: Vec<HeaderName> = req
        .headers()
        .keys()
        .filter(|k| k.as_str().starts_with("x-internal-"))
        .cloned()
        .collect();
    for k in header_keys {
        req.headers_mut().remove(&k);
    }

    // 4. Subfase 1B: Validação de JWT Firebase se presente
    if let Some(auth_hdr) = req.headers().get("authorization") {
        if let Ok(auth_str) = auth_hdr.to_str() {
            if let Some(token) = auth_str.strip_prefix("Bearer ") {
                match verify_firebase_id_token(
                    token.trim(),
                    &state.firebase_project_id,
                    &state.cert_cache,
                )
                .await
                {
                    Ok(identity) => {
                        // Se houver banco, aplica moderação autoritativa
                        if let Some(ref pool) = state.pool {
                            let method = req.method().as_str();
                            let path = req.uri().path();
                            if let Err(e) = check_user_moderation(
                                pool,
                                &identity.uid,
                                method,
                                path,
                                Some(identity.auth_time),
                            )
                            .await
                            {
                                let mut resp =
                                    Response::new(Body::from(format!("{{\"detail\":\"{e}\"}}")));
                                *resp.status_mut() = StatusCode::FORBIDDEN;
                                resp.headers_mut().insert(
                                    CONTENT_TYPE,
                                    HeaderValue::from_static("application/json"),
                                );
                                return Ok(resp);
                            }
                        }

                        // Injeta identidade confiável para o backend Unix
                        if let Ok(val) = HeaderValue::from_str(&identity.uid) {
                            req.headers_mut()
                                .insert(HeaderName::from_static("x-internal-uid"), val);
                        }
                        if let Ok(val) = HeaderValue::from_str(&identity.auth_time.to_string()) {
                            req.headers_mut()
                                .insert(HeaderName::from_static("x-internal-auth-time"), val);
                        }
                    }
                    Err(e) => {
                        warn!("Falha de token Firebase na borda: {e}");
                    }
                }
            }
        }
    }

    // Injeta IP confiável
    if let Ok(val) = HeaderValue::from_str(&client_ip) {
        req.headers_mut()
            .insert(HeaderName::from_static("x-internal-client-ip"), val);
    }

    let client_upgrade = hyper::upgrade::on(&mut req);

    // 5. Encaminha para o socket Unix privado
    let stream = match UnixStream::connect(&state.backend_sock).await {
        Ok(s) => s,
        Err(e) => {
            error!(
                "Falha ao conectar no socket do backend {:?}: {e}",
                state.backend_sock
            );
            return Err(StatusCode::BAD_GATEWAY);
        }
    };

    let io = TokioIo::new(stream);
    let (mut sender, conn) = hyper::client::conn::http1::handshake(io)
        .await
        .map_err(|_| StatusCode::BAD_GATEWAY)?;

    tokio::spawn(async move {
        if let Err(err) = conn.with_upgrades().await {
            warn!("Conexao com backend encerrou com erro: {err:?}");
        }
    });

    let mut resp = sender
        .send_request(req)
        .await
        .map_err(|_| StatusCode::BAD_GATEWAY)?;

    if resp.status() == StatusCode::SWITCHING_PROTOCOLS {
        let backend_upgrade = hyper::upgrade::on(&mut resp);
        tokio::spawn(async move {
            match tokio::try_join!(client_upgrade, backend_upgrade) {
                Ok((client_upgraded, backend_upgraded)) => {
                    let mut client_io = TokioIo::new(client_upgraded);
                    let mut backend_io = TokioIo::new(backend_upgraded);
                    if let Err(e) =
                        tokio::io::copy_bidirectional(&mut client_io, &mut backend_io).await
                    {
                        warn!("WebSocket tunnel encerrou no fallback proxy: {e}");
                    }
                }
                Err(err) => {
                    warn!("Erro no upgrade HTTP no fallback proxy: {err}");
                }
            }
        });
    }

    Ok(resp.into_response())
}

// ============================================================================
// FASE 2: HANDLERS DE LEITURA E CACHE
// ============================================================================

#[derive(Debug, Deserialize)]
pub struct VitalsQuery {
    pub limit: Option<i64>,
    pub offset: Option<i64>,
    pub before: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct DailyMetricsQuery {
    pub days: Option<i64>,
    pub offset: Option<i64>,
    pub before_date: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct MedicationLogsQuery {
    pub days: Option<i32>,
}

#[derive(Debug, Deserialize)]
pub struct ActivityFeedQuery {
    pub limit: Option<i64>,
}

#[allow(clippy::result_large_err)]
async fn authenticate_request(
    headers: &HeaderMap,
    state: &AppState,
    method: &str,
    path: &str,
) -> Result<VerifiedIdentity, Response> {
    let auth_hdr = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .ok_or_else(|| {
            let mut resp = Response::new(Body::from("{\"detail\":\"Não autenticado.\"}"));
            *resp.status_mut() = StatusCode::UNAUTHORIZED;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            resp
        })?;

    let token = auth_hdr
        .strip_prefix("Bearer ")
        .map(|t| t.trim())
        .ok_or_else(|| {
            let mut resp = Response::new(Body::from(
                "{\"detail\":\"Formato de autorização inválido.\"}",
            ));
            *resp.status_mut() = StatusCode::UNAUTHORIZED;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            resp
        })?;

    let identity = verify_firebase_id_token(token, &state.firebase_project_id, &state.cert_cache)
        .await
        .map_err(|e| {
            let mut resp = Response::new(Body::from(format!(
                "{{\"detail\":\"Token inválido: {e}\"}}"
            )));
            *resp.status_mut() = StatusCode::UNAUTHORIZED;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            resp
        })?;

    if let Some(ref pool) = state.pool {
        if let Err(e) =
            check_user_moderation(pool, &identity.uid, method, path, Some(identity.auth_time)).await
        {
            let mut resp = Response::new(Body::from(format!("{{\"detail\":\"{e}\"}}")));
            *resp.status_mut() = StatusCode::FORBIDDEN;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return Err(resp);
        }
    }

    Ok(identity)
}

/// GET /api/anvisa/medications/barcode/{ean}
async fn anvisa_barcode_handler(
    State(state): State<Arc<AppState>>,
    Path(ean): Path<String>,
    headers: HeaderMap,
) -> Response {
    if ean.len() != 13 || !ean.chars().all(|c| c.is_ascii_digit()) {
        let mut resp = Response::new(Body::from("{\"detail\":\"EAN-13 inválido.\"}"));
        *resp.status_mut() = StatusCode::UNPROCESSABLE_ENTITY;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return resp;
    }

    if let Err(auth_resp) = authenticate_request(
        &headers,
        &state,
        "GET",
        &format!("/api/anvisa/medications/barcode/{ean}"),
    )
    .await
    {
        return auth_resp;
    }

    // Cache hit?
    if let Some(cached) = state.catalog_cache.get(&ean).await {
        let mut resp = Response::new(Body::from(
            serde_json::to_string(&cached).unwrap_or_default(),
        ));
        *resp.status_mut() = StatusCode::OK;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        resp.headers_mut().insert(
            HeaderName::from_static("x-cache"),
            HeaderValue::from_static("HIT"),
        );
        return resp;
    }

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            let mut resp = Response::new(Body::from(
                "{\"detail\":\"Catálogo indisponível temporariamente.\"}",
            ));
            *resp.status_mut() = StatusCode::SERVICE_UNAVAILABLE;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let row = match sqlx::query(
        "SELECT ean_barcode, name, active_principle, concentration, pharmaceutical_form, manufacturer,
                farmacia_popular_eligible, anvisa_registration_number, source_name, source_url, source_checked_at
         FROM anvisa_medication_catalog WHERE ean_barcode = $1",
    )
    .bind(&ean)
    .fetch_optional(pool)
    .await
    {
        Ok(r) => r,
        Err(e) => {
            error!("Erro na consulta do catálogo: {e}");
            let mut resp = Response::new(Body::from("{\"detail\":\"Erro no banco de dados.\"}"));
            *resp.status_mut() = StatusCode::SERVICE_UNAVAILABLE;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    match row {
        Some(r) => {
            let source_checked: Option<DateTime<Utc>> = r.try_get("source_checked_at").ok();
            let checked_str = source_checked
                .map(|dt| dt.to_rfc3339())
                .unwrap_or_else(|| Utc::now().to_rfc3339());

            let med = BarcodeMedication {
                ean_barcode: r.try_get("ean_barcode").unwrap_or_default(),
                name: r.try_get("name").unwrap_or_default(),
                active_principle: r.try_get("active_principle").ok(),
                concentration: r.try_get("concentration").ok(),
                pharmaceutical_form: r.try_get("pharmaceutical_form").ok(),
                manufacturer: r.try_get("manufacturer").ok(),
                farmacia_popular_eligible: r.try_get("farmacia_popular_eligible").ok(),
                anvisa_registration_number: r.try_get("anvisa_registration_number").ok(),
                source_name: r.try_get("source_name").unwrap_or_default(),
                source_url: r.try_get("source_url").ok(),
                source_checked_at: checked_str,
            };

            state.catalog_cache.insert(ean, med.clone()).await;

            let mut resp =
                Response::new(Body::from(serde_json::to_string(&med).unwrap_or_default()));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            resp.headers_mut().insert(
                HeaderName::from_static("x-cache"),
                HeaderValue::from_static("MISS"),
            );
            resp
        }
        None => {
            let mut resp = Response::new(Body::from(
                "{\"detail\":\"EAN não encontrado no catálogo versionado. Cadastre manualmente e confira a receita.\"}",
            ));
            *resp.status_mut() = StatusCode::NOT_FOUND;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            resp
        }
    }
}

/// GET /api/biometry/{user_id}
async fn biometry_handler(
    State(state): State<Arc<AppState>>,
    Path(user_id): Path<String>,
    headers: HeaderMap,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "GET", &format!("/api/biometry/{user_id}"))
            .await
        {
            Ok(id) => id,
            Err(resp) => return resp,
        };

    if require_owner(&identity.uid, &user_id).is_err() {
        let mut resp = Response::new(Body::from(
            "{\"detail\":\"Esta operação pertence a outra conta.\"}",
        ));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return resp;
    }

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            let mut resp = Response::new(Body::from("{\"detail\":\"Biometria indisponível.\"}"));
            *resp.status_mut() = StatusCode::SERVICE_UNAVAILABLE;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let rows = match sqlx::query(
        "SELECT id, user_id, weight_kg, height_m, imc, measured_at FROM biometrics WHERE user_id = $1 ORDER BY measured_at DESC LIMIT 1000",
    )
    .bind(&user_id)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(e) => {
            error!("Erro ao buscar biometria: {e}");
            let mut resp = Response::new(Body::from("{\"detail\":\"Biometria indisponível.\"}"));
            *resp.status_mut() = StatusCode::SERVICE_UNAVAILABLE;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let records: Vec<BiometryRecord> = rows
        .into_iter()
        .map(|r| {
            let id: sqlx::types::Uuid = r.try_get("id").unwrap_or_default();
            let measured_at: Option<DateTime<Utc>> = r.try_get("measured_at").ok();
            BiometryRecord {
                id: id.to_string(),
                user_id: r.try_get("user_id").unwrap_or_default(),
                weight: r.try_get("weight_kg").ok(),
                height: r.try_get("height_m").ok(),
                imc: r.try_get("imc").ok(),
                measured_at: measured_at.map(|d| d.to_rfc3339()).unwrap_or_default(),
            }
        })
        .collect();

    let mut resp = Response::new(Body::from(
        serde_json::to_string(&records).unwrap_or_default(),
    ));
    *resp.status_mut() = StatusCode::OK;
    resp.headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    resp
}

/// GET /api/vitals/{user_id}
async fn vitals_handler(
    State(state): State<Arc<AppState>>,
    Path(user_id): Path<String>,
    Query(query): Query<VitalsQuery>,
    headers: HeaderMap,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        &format!("/api/vitals/{user_id}"),
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            let mut resp = Response::new(Body::from("[]"));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    if require_family_access(pool, &identity.uid, &user_id)
        .await
        .is_err()
    {
        let mut resp = Response::new(Body::from(
            "{\"detail\":\"O acompanhamento deste familiar não está autorizado.\"}",
        ));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return resp;
    }

    let limit = query.limit.unwrap_or(50).clamp(1, 200);
    let offset = query.offset.unwrap_or(0).max(0);

    let rows = if let Some(ref before) = query.before {
        if let Ok(b_dt) = DateTime::parse_from_rfc3339(before) {
            sqlx::query(
                "SELECT * FROM vital_signs WHERE user_id = $1 AND measured_at < $2 ORDER BY measured_at DESC LIMIT $3 OFFSET $4",
            )
            .bind(&user_id)
            .bind(b_dt.with_timezone(&Utc))
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await
        } else {
            sqlx::query(
                "SELECT * FROM vital_signs WHERE user_id = $1 ORDER BY measured_at DESC LIMIT $2 OFFSET $3",
            )
            .bind(&user_id)
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await
        }
    } else {
        sqlx::query(
            "SELECT * FROM vital_signs WHERE user_id = $1 ORDER BY measured_at DESC LIMIT $2 OFFSET $3",
        )
        .bind(&user_id)
        .bind(limit)
        .bind(offset)
        .fetch_all(pool)
        .await
    };

    let rows = match rows {
        Ok(r) => r,
        Err(e) => {
            error!("Erro ao buscar sinais vitais: {e}");
            let mut resp = Response::new(Body::from("[]"));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let records: Vec<VitalSignRecord> = rows
        .into_iter()
        .map(|r| {
            let id: sqlx::types::Uuid = r.try_get("id").unwrap_or_default();
            let measured_at: Option<DateTime<Utc>> = r.try_get("measured_at").ok();
            let created_at: Option<DateTime<Utc>> = r.try_get("created_at").ok();
            VitalSignRecord {
                id: id.to_string(),
                user_id: r.try_get("user_id").unwrap_or_default(),
                measured_at: measured_at.map(|d| d.to_rfc3339()).unwrap_or_default(),
                created_at: created_at.map(|d| d.to_rfc3339()),
                systolic_pressure: r.try_get("systolic_pressure").ok(),
                diastolic_pressure: r.try_get("diastolic_pressure").ok(),
                pulse_bpm: r.try_get("pulse_bpm").ok(),
                blood_glucose_mg_dl: r.try_get("blood_glucose_mg_dl").ok(),
                body_temperature_c: r.try_get("body_temperature_c").ok(),
                oxygen_saturation_percent: r.try_get("oxygen_saturation_percent").ok(),
                hydration_ml: r.try_get("hydration_ml").ok(),
                notes: r.try_get("notes").ok(),
            }
        })
        .collect();

    let mut resp = Response::new(Body::from(
        serde_json::to_string(&records).unwrap_or_default(),
    ));
    *resp.status_mut() = StatusCode::OK;
    resp.headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    resp
}

/// GET /api/metrics/daily/{user_id}
async fn daily_metrics_handler(
    State(state): State<Arc<AppState>>,
    Path(user_id): Path<String>,
    Query(query): Query<DailyMetricsQuery>,
    headers: HeaderMap,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        &format!("/api/metrics/daily/{user_id}"),
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    if require_owner(&identity.uid, &user_id).is_err() {
        let mut resp = Response::new(Body::from(
            "{\"detail\":\"Esta operação pertence a outra conta.\"}",
        ));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return resp;
    }

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            let mut resp = Response::new(Body::from("[]"));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let days = query.days.unwrap_or(30).clamp(1, 365);
    let offset = query.offset.unwrap_or(0).max(0);

    let rows = if let Some(ref b_date) = query.before_date {
        if let Ok(d) = chrono::NaiveDate::parse_from_str(b_date, "%Y-%m-%d") {
            sqlx::query(
                "SELECT * FROM daily_metrics WHERE user_id = $1 AND date < $2 ORDER BY date DESC LIMIT $3 OFFSET $4",
            )
            .bind(&user_id)
            .bind(d)
            .bind(days)
            .bind(offset)
            .fetch_all(pool)
            .await
        } else {
            sqlx::query(
                "SELECT * FROM daily_metrics WHERE user_id = $1 ORDER BY date DESC LIMIT $2 OFFSET $3",
            )
            .bind(&user_id)
            .bind(days)
            .bind(offset)
            .fetch_all(pool)
            .await
        }
    } else {
        sqlx::query(
            "SELECT * FROM daily_metrics WHERE user_id = $1 ORDER BY date DESC LIMIT $2 OFFSET $3",
        )
        .bind(&user_id)
        .bind(days)
        .bind(offset)
        .fetch_all(pool)
        .await
    };

    let rows = match rows {
        Ok(r) => r,
        Err(e) => {
            error!("Erro ao buscar métricas diárias: {e}");
            let mut resp = Response::new(Body::from("[]"));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let records: Vec<DailyMetricRecord> = rows
        .into_iter()
        .map(|r| {
            let id: sqlx::types::Uuid = r.try_get("id").unwrap_or_default();
            let date: chrono::NaiveDate = r.try_get("date").unwrap_or_default();
            let created_at: Option<DateTime<Utc>> = r.try_get("created_at").ok();
            DailyMetricRecord {
                id: id.to_string(),
                user_id: r.try_get("user_id").unwrap_or_default(),
                date: date.to_string(),
                created_at: created_at.map(|d| d.to_rfc3339()),
                steps: r.try_get("steps").ok(),
                distance_meters: r.try_get("distance_meters").ok(),
                distance_gps_meters: r.try_get("distance_gps_meters").ok(),
                distance_steps_meters: r.try_get("distance_steps_meters").ok(),
                distance_final_meters: r.try_get("distance_final_meters").ok(),
                reliability_score: r.try_get("reliability_score").ok(),
                calories_burned: r.try_get("calories_burned").ok(),
                active_minutes: r.try_get("active_minutes").ok(),
            }
        })
        .collect();

    let mut resp = Response::new(Body::from(
        serde_json::to_string(&records).unwrap_or_default(),
    ));
    *resp.status_mut() = StatusCode::OK;
    resp.headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    resp
}

/// GET /api/medications/{user_id}
async fn medications_handler(
    State(state): State<Arc<AppState>>,
    Path(user_id): Path<String>,
    headers: HeaderMap,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        &format!("/api/medications/{user_id}"),
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    if require_owner(&identity.uid, &user_id).is_err() {
        let mut resp = Response::new(Body::from(
            "{\"detail\":\"Esta operação pertence a outra conta.\"}",
        ));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return resp;
    }

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            let mut resp = Response::new(Body::from("[]"));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let rows =
        match sqlx::query("SELECT * FROM medications WHERE user_id = $1 ORDER BY created_at DESC")
            .bind(&user_id)
            .fetch_all(pool)
            .await
        {
            Ok(r) => r,
            Err(e) => {
                error!("Erro ao buscar medicamentos: {e}");
                let mut resp = Response::new(Body::from("[]"));
                *resp.status_mut() = StatusCode::OK;
                resp.headers_mut()
                    .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
                return resp;
            }
        };

    let records: Vec<MedicationRecord> = rows
        .into_iter()
        .map(|r| {
            let id: sqlx::types::Uuid = r.try_get("id").unwrap_or_default();
            let created_at: Option<DateTime<Utc>> = r.try_get("created_at").ok();
            let updated_at: Option<DateTime<Utc>> = r.try_get("updated_at").ok();
            MedicationRecord {
                id: id.to_string(),
                user_id: r.try_get("user_id").unwrap_or_default(),
                name: r.try_get("name").unwrap_or_default(),
                dosage_mg: r.try_get("dosage_mg").ok(),
                schedule_time: r.try_get("schedule_time").ok(),
                schedule_times: r.try_get("schedule_times").ok(),
                ean_barcode: r.try_get("ean_barcode").ok(),
                active_principle: r.try_get("active_principle").ok(),
                manufacturer: r.try_get("manufacturer").ok(),
                pharmaceutical_form: r.try_get("pharmaceutical_form").ok(),
                total_units: r.try_get("total_units").ok(),
                current_units: r.try_get("current_units").ok(),
                alert_threshold_days: r.try_get("alert_threshold_days").ok(),
                photo_reference_url: r.try_get("photo_reference_url").ok(),
                confirmed_with_prescription: r.try_get("confirmed_with_prescription").ok(),
                created_at: created_at.map(|d| d.to_rfc3339()),
                updated_at: updated_at.map(|d| d.to_rfc3339()),
            }
        })
        .collect();

    let mut resp = Response::new(Body::from(
        serde_json::to_string(&records).unwrap_or_default(),
    ));
    *resp.status_mut() = StatusCode::OK;
    resp.headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    resp
}

/// GET /api/medications/{user_id}/logs e GET /api/sync/medication-logs/{user_id}
async fn medication_logs_handler(
    State(state): State<Arc<AppState>>,
    Path(user_id): Path<String>,
    Query(query): Query<MedicationLogsQuery>,
    headers: HeaderMap,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        &format!("/api/medications/{user_id}/logs"),
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    if require_owner(&identity.uid, &user_id).is_err() {
        let mut resp = Response::new(Body::from(
            "{\"detail\":\"Esta operação pertence a outra conta.\"}",
        ));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return resp;
    }

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            let mut resp = Response::new(Body::from("[]"));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let days = query.days.unwrap_or(7);

    let rows = match sqlx::query(
        "SELECT id, user_id, medication_id, taken_at, scheduled_for, actor_id, units_taken, idempotency_key
         FROM medication_logs
         WHERE user_id = $1 AND taken_at >= NOW() - make_interval(days => $2)
         ORDER BY taken_at DESC",
    )
    .bind(&user_id)
    .bind(days)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(e) => {
            error!("Erro ao buscar logs de medicação: {e}");
            let mut resp = Response::new(Body::from("[]"));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let records: Vec<MedicationLogRecord> = rows
        .into_iter()
        .map(|r| {
            let id: sqlx::types::Uuid = r.try_get("id").unwrap_or_default();
            let med_id: sqlx::types::Uuid = r.try_get("medication_id").unwrap_or_default();
            let taken_at: Option<DateTime<Utc>> = r.try_get("taken_at").ok();
            MedicationLogRecord {
                id: id.to_string(),
                user_id: r.try_get("user_id").unwrap_or_default(),
                medication_id: med_id.to_string(),
                taken_at: taken_at.map(|d| d.to_rfc3339()),
                scheduled_for: r.try_get("scheduled_for").ok(),
                actor_id: r.try_get("actor_id").ok(),
                units_taken: r.try_get("units_taken").ok(),
                idempotency_key: r.try_get("idempotency_key").ok(),
            }
        })
        .collect();

    let mut resp = Response::new(Body::from(
        serde_json::to_string(&records).unwrap_or_default(),
    ));
    *resp.status_mut() = StatusCode::OK;
    resp.headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    resp
}

/// GET /api/family/patients/{patient_id}/activity-feed
async fn activity_feed_handler(
    State(state): State<Arc<AppState>>,
    Path(patient_id): Path<String>,
    Query(query): Query<ActivityFeedQuery>,
    headers: HeaderMap,
) -> Response {
    let identity = match authenticate_request(
        &headers,
        &state,
        "GET",
        &format!("/api/family/patients/{patient_id}/activity-feed"),
    )
    .await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            let mut resp = Response::new(Body::from("[]"));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    if require_patient_permission(pool, &identity.uid, &patient_id, "care:read")
        .await
        .is_err()
    {
        let mut resp = Response::new(Body::from(
            "{\"detail\":\"Seu vínculo não permite esta operação.\"}",
        ));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp.headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return resp;
    }

    let limit = query.limit.unwrap_or(50).clamp(1, 100);

    let rows = match sqlx::query(
        "SELECT id, patient_id, actor_id, actor_name, action_type, details, occurred_at
         FROM care_audit_trail WHERE patient_id = $1 ORDER BY occurred_at DESC LIMIT $2",
    )
    .bind(&patient_id)
    .bind(limit)
    .fetch_all(pool)
    .await
    {
        Ok(r) => r,
        Err(e) => {
            error!("Erro ao buscar activity-feed: {e}");
            let mut resp = Response::new(Body::from("[]"));
            *resp.status_mut() = StatusCode::OK;
            resp.headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
            return resp;
        }
    };

    let records: Vec<ActivityFeedRecord> = rows
        .into_iter()
        .map(|r| {
            let id: sqlx::types::Uuid = r.try_get("id").unwrap_or_default();
            let occurred_at: Option<DateTime<Utc>> = r.try_get("occurred_at").ok();
            let details_val: serde_json::Value = r
                .try_get("details")
                .unwrap_or_else(|_| serde_json::json!({}));
            ActivityFeedRecord {
                id: id.to_string(),
                patient_id: r.try_get("patient_id").unwrap_or_default(),
                actor_id: r.try_get("actor_id").unwrap_or_default(),
                actor_name: r.try_get("actor_name").unwrap_or_default(),
                action_type: r.try_get("action_type").unwrap_or_default(),
                details: details_val,
                occurred_at: occurred_at.map(|d| d.to_rfc3339()).unwrap_or_default(),
            }
        })
        .collect();

    let mut resp = Response::new(Body::from(
        serde_json::to_string(&records).unwrap_or_default(),
    ));
    *resp.status_mut() = StatusCode::OK;
    resp.headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    resp
}

// =========================================================================
// Handlers de Escrita e Sincronização (Fase 3)
// =========================================================================

async fn sync_biometry_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(payload): Json<BiometrySyncDto>,
) -> Response {
    let identity = match authenticate_request(&headers, &state, "POST", "/api/sync/biometry").await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    if require_owner(&identity.uid, &payload.user_id).is_err() {
        return (
            StatusCode::FORBIDDEN,
            Json(HttpErrorResponse::new(
                "Não autorizado: o recurso pertence a outro usuário.",
            )),
        )
            .into_response();
    }

    if payload.weight < 20.0 || payload.weight > 300.0 || payload.weight.is_nan() {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(HttpErrorResponse::new(
                "Peso fora da faixa aceitável (20-300 kg).",
            )),
        )
            .into_response();
    }

    if let Some(h) = payload.height {
        if h < 0.0 || h > 250.0 || h.is_nan() {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(HttpErrorResponse::new(
                    "Altura fora da faixa aceitável (0-250 cm).",
                )),
            )
                .into_response();
        }
    }

    let measured_dt = match DateTime::parse_from_rfc3339(&payload.measured_at) {
        Ok(dt) => dt.with_timezone(&Utc),
        Err(_) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(HttpErrorResponse::new(
                    "A medição precisa conter fuso horário.",
                )),
            )
                .into_response();
        }
    };

    let bio_id = match uuid::Uuid::parse_str(&payload.id) {
        Ok(u) => u,
        Err(_) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(HttpErrorResponse::new("Identificador de medição inválido.")),
            )
                .into_response();
        }
    };

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(HttpErrorResponse::new(
                    "Biometria não sincronizada: banco indisponível.",
                )),
            )
                .into_response();
        }
    };

    let h_m = height_in_meters(payload.height);
    let imc = compute_imc(payload.weight, h_m);

    let row = sqlx::query(
        r#"INSERT INTO biometrics (id, user_id, weight_kg, height_m, imc, measured_at, source)
           VALUES ($1, $2, $3, $4, $5, $6, 'app')
           ON CONFLICT (id) DO NOTHING
           RETURNING id"#,
    )
    .bind(bio_id)
    .bind(&identity.uid)
    .bind(payload.weight)
    .bind(h_m)
    .bind(imc)
    .bind(measured_dt)
    .fetch_optional(pool)
    .await;

    match row {
        Ok(Some(r)) => {
            let id: uuid::Uuid = r.get("id");
            (
                StatusCode::OK,
                Json(SyncResponse {
                    status: "success".to_string(),
                    id: id.to_string(),
                    medication_contract: None,
                    medication_batch_contract: None,
                    dose_status: None,
                }),
            )
                .into_response()
        }
        Ok(None) => {
            let existing = sqlx::query("SELECT id FROM biometrics WHERE id = $1 AND user_id = $2")
                .bind(bio_id)
                .bind(&identity.uid)
                .fetch_optional(pool)
                .await;
            match existing {
                Ok(Some(r)) => {
                    let id: uuid::Uuid = r.get("id");
                    (
                        StatusCode::OK,
                        Json(SyncResponse {
                            status: "success".to_string(),
                            id: id.to_string(),
                            medication_contract: None,
                            medication_batch_contract: None,
                            dose_status: None,
                        }),
                    )
                        .into_response()
                }
                _ => (
                    StatusCode::CONFLICT,
                    Json(HttpErrorResponse::new(
                        "Identificador de medição já utilizado.",
                    )),
                )
                    .into_response(),
            }
        }
        Err(e) => {
            error!("Erro ao persistir biometria: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(HttpErrorResponse::new(
                    "Erro interno ao persistir biometria.",
                )),
            )
                .into_response()
        }
    }
}

async fn sync_daily_metrics_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(payload): Json<DailyMetricSyncDto>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "POST", "/api/sync/daily-metrics").await {
            Ok(id) => id,
            Err(resp) => return resp,
        };

    if identity.uid != payload.user_id {
        return (
            StatusCode::FORBIDDEN,
            Json(HttpErrorResponse::new(
                "Não autorizado: métricas só podem ser gravadas para a própria conta.",
            )),
        )
            .into_response();
    }

    let m_date = match chrono::NaiveDate::parse_from_str(&payload.date, "%Y-%m-%d") {
        Ok(d) => d,
        Err(_) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(HttpErrorResponse::new(
                    "Data inválida. Use o formato YYYY-MM-DD.",
                )),
            )
                .into_response();
        }
    };

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            return (
                StatusCode::OK,
                Json(serde_json::json!({"status": "offline_mode"})),
            )
                .into_response();
        }
    };

    let query = r#"
        INSERT INTO daily_metrics (
            user_id, date, steps, distance_meters, distance_gps_meters,
            distance_steps_meters, distance_final_meters, reliability_score,
            calories_burned, active_minutes
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (user_id, date) DO UPDATE SET
            steps = EXCLUDED.steps,
            distance_meters = EXCLUDED.distance_meters,
            distance_gps_meters = EXCLUDED.distance_gps_meters,
            distance_steps_meters = EXCLUDED.distance_steps_meters,
            distance_final_meters = EXCLUDED.distance_final_meters,
            reliability_score = EXCLUDED.reliability_score,
            calories_burned = EXCLUDED.calories_burned,
            active_minutes = EXCLUDED.active_minutes
        RETURNING id"#;

    let row = sqlx::query(query)
        .bind(&payload.user_id)
        .bind(m_date)
        .bind(payload.steps)
        .bind(payload.distance_meters)
        .bind(payload.distance_gps_meters)
        .bind(payload.distance_steps_meters)
        .bind(payload.distance_final_meters)
        .bind(payload.reliability_score)
        .bind(payload.calories_burned)
        .bind(payload.active_minutes)
        .fetch_one(pool)
        .await;

    match row {
        Ok(r) => {
            let id: uuid::Uuid = r.get("id");
            (
                StatusCode::OK,
                Json(SyncResponse {
                    status: "success".to_string(),
                    id: id.to_string(),
                    medication_contract: None,
                    medication_batch_contract: None,
                    dose_status: None,
                }),
            )
                .into_response()
        }
        Err(e) => {
            error!("Erro ao gravar daily_metrics: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(HttpErrorResponse::new(
                    "Erro interno ao persistir métricas diárias.",
                )),
            )
                .into_response()
        }
    }
}

async fn sync_medication_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(mut payload): Json<MedicationSyncDto>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "POST", "/api/sync/medication").await {
            Ok(id) => id,
            Err(resp) => return resp,
        };

    if require_owner(&identity.uid, &payload.user_id).is_err() {
        return (
            StatusCode::FORBIDDEN,
            Json(HttpErrorResponse::new(
                "Não autorizado: o recurso pertence a outro usuário.",
            )),
        )
            .into_response();
    }

    let (sched_time, sched_times) = if let Some(ref st) = payload.schedule_times {
        match normalize_schedule_times(st) {
            Ok((primary, joined)) => (primary, Some(joined)),
            Err(msg) => {
                return (
                    StatusCode::UNPROCESSABLE_ENTITY,
                    Json(HttpErrorResponse::new(msg)),
                )
                    .into_response();
            }
        }
    } else {
        (payload.schedule_time.clone(), None)
    };
    payload.schedule_time = sched_time;
    payload.schedule_times = sched_times;

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            return (
                StatusCode::OK,
                Json(serde_json::json!({"status": "offline_mode"})),
            )
                .into_response();
        }
    };

    let med_id = match payload.id {
        Some(ref id_str) => match uuid::Uuid::parse_str(id_str) {
            Ok(u) => u,
            Err(_) => {
                return (
                    StatusCode::UNPROCESSABLE_ENTITY,
                    Json(HttpErrorResponse::new(
                        "Identificador de medicamento inválido.",
                    )),
                )
                    .into_response();
            }
        },
        None => uuid::Uuid::new_v4(),
    };

    let query = r#"
        INSERT INTO medications (id, user_id, name, dosage_mg, pill_quantity, schedule_time, schedule_times, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            dosage_mg = EXCLUDED.dosage_mg,
            pill_quantity = EXCLUDED.pill_quantity,
            schedule_time = CASE WHEN EXCLUDED.schedule_times IS NULL AND medications.schedule_times IS NOT NULL THEN medications.schedule_time ELSE EXCLUDED.schedule_time END,
            schedule_times = COALESCE(EXCLUDED.schedule_times, medications.schedule_times),
            updated_at = NOW()
        WHERE medications.user_id = EXCLUDED.user_id
        RETURNING id"#;

    let row = sqlx::query(query)
        .bind(med_id)
        .bind(&payload.user_id)
        .bind(&payload.name)
        .bind(payload.dosage_mg)
        .bind(payload.pill_quantity)
        .bind(&payload.schedule_time)
        .bind(&payload.schedule_times)
        .fetch_optional(pool)
        .await;

    match row {
        Ok(Some(r)) => {
            let id: uuid::Uuid = r.get("id");
            (
                StatusCode::OK,
                Json(SyncResponse {
                    status: "success".to_string(),
                    id: id.to_string(),
                    medication_contract: Some(2),
                    medication_batch_contract: None,
                    dose_status: None,
                }),
            )
                .into_response()
        }
        Ok(None) => (
            StatusCode::FORBIDDEN,
            Json(HttpErrorResponse::new("Medicamento indisponível.")),
        )
            .into_response(),
        Err(e) => {
            error!("Erro ao gravar medicamento: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(HttpErrorResponse::new(
                    "Erro interno ao persistir medicamento.",
                )),
            )
                .into_response()
        }
    }
}

async fn sync_audit_log_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(payload): Json<AuditLogSyncDto>,
) -> Response {
    let identity = match authenticate_request(&headers, &state, "POST", "/api/sync/audit-log").await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            return (
                StatusCode::OK,
                Json(serde_json::json!({"status": "offline_mode"})),
            )
                .into_response();
        }
    };

    if identity.uid != payload.user_id
        && require_family_access(pool, &identity.uid, &payload.user_id)
            .await
            .is_err()
    {
        return (
            StatusCode::FORBIDDEN,
            Json(HttpErrorResponse::new(
                "Não autorizado: vínculo familiar inexistente ou inativo.",
            )),
        )
            .into_response();
    }

    let log_id = payload
        .id
        .clone()
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());

    let meta_val: Option<serde_json::Value> = payload
        .metadata_json
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok());

    let query = r#"
        INSERT INTO audit_logs (id, user_id, event_type, action, metadata, created_at)
        VALUES ($1, $2, $3, $4, $5, NOW())
        ON CONFLICT (id) DO NOTHING
        RETURNING id"#;

    let row = sqlx::query(query)
        .bind(&log_id)
        .bind(&payload.user_id)
        .bind(&payload.event_type)
        .bind(&payload.action)
        .bind(meta_val)
        .fetch_optional(pool)
        .await;

    match row {
        Ok(opt) => {
            let ret_id = opt.map(|r| r.get::<String, _>("id")).unwrap_or(log_id);
            (
                StatusCode::OK,
                Json(SyncResponse {
                    status: "success".to_string(),
                    id: ret_id,
                    medication_contract: None,
                    medication_batch_contract: None,
                    dose_status: None,
                }),
            )
                .into_response()
        }
        Err(e) => {
            error!("Erro ao gravar audit_log: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(HttpErrorResponse::new(
                    "Erro interno ao persistir log de auditoria.",
                )),
            )
                .into_response()
        }
    }
}

async fn agent_receipt_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(payload): Json<AgentReceiptDto>,
) -> Response {
    let identity = match authenticate_request(&headers, &state, "POST", "/api/agent/receipt").await
    {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    if require_owner(&identity.uid, &payload.user_id).is_err() {
        return (
            StatusCode::FORBIDDEN,
            Json(HttpErrorResponse::new(
                "Não autorizado: o recurso pertence a outro usuário.",
            )),
        )
            .into_response();
    }

    if !is_valid_agent_action(&payload.action) {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(HttpErrorResponse::new("Ação desconhecida no contrato.")),
        )
            .into_response();
    }

    if payload.status != "confirmed" && payload.status != "failed" {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(HttpErrorResponse::new("Status inválido.")),
        )
            .into_response();
    }

    if payload.idempotency_key.is_empty() || payload.idempotency_key.len() > 64 {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(HttpErrorResponse::new("Chave de idempotência inválida.")),
        )
            .into_response();
    }

    let closing = format_agent_closing(&payload.action, &payload.params);

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            return (
                StatusCode::OK,
                Json(serde_json::json!({
                    "status": "offline_mode",
                    "closing": closing
                })),
            )
                .into_response();
        }
    };

    let query = r#"
        INSERT INTO agent_actions (user_id, channel, action, params, idempotency_key, status, result)
        VALUES ($1, 'app', $2, $3, $4, $5, $6)
        ON CONFLICT (user_id, idempotency_key) DO NOTHING
        RETURNING id"#;

    let row = sqlx::query(query)
        .bind(&payload.user_id)
        .bind(&payload.action)
        .bind(&payload.params)
        .bind(&payload.idempotency_key)
        .bind(&payload.status)
        .bind(
            payload
                .result
                .clone()
                .unwrap_or_else(|| serde_json::json!({})),
        )
        .fetch_optional(pool)
        .await;

    match row {
        Ok(opt) => {
            let status = if opt.is_some() {
                "success"
            } else {
                "duplicate"
            };
            (
                StatusCode::OK,
                Json(AgentReceiptResponse {
                    status: status.to_string(),
                    closing,
                }),
            )
                .into_response()
        }
        Err(e) => {
            error!("Erro ao gravar agent_receipt: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(HttpErrorResponse::new("Erro interno ao registrar recibo.")),
            )
                .into_response()
        }
    }
}

async fn sync_medication_log_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(payload): Json<MedicationLogSyncDto>,
) -> Response {
    let identity =
        match authenticate_request(&headers, &state, "POST", "/api/sync/medication-log").await {
            Ok(id) => id,
            Err(resp) => return resp,
        };

    if require_owner(&identity.uid, &payload.user_id).is_err() {
        return (
            StatusCode::FORBIDDEN,
            Json(HttpErrorResponse::new(
                "Não autorizado: o recurso pertence a outro usuário.",
            )),
        )
            .into_response();
    }

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            return (
                StatusCode::OK,
                Json(serde_json::json!({"status": "offline_mode"})),
            )
                .into_response();
        }
    };

    let med_uuid = match uuid::Uuid::parse_str(&payload.medication_id) {
        Ok(u) => u,
        Err(_) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(HttpErrorResponse::new(
                    "Identificador de medicamento inválido.",
                )),
            )
                .into_response();
        }
    };

    let owned: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM medications WHERE id = $1 AND user_id = $2)",
    )
    .bind(med_uuid)
    .bind(&identity.uid)
    .fetch_one(pool)
    .await
    .unwrap_or(false);

    if !owned {
        return (
            StatusCode::FORBIDDEN,
            Json(HttpErrorResponse::new("Medicamento indisponível.")),
        )
            .into_response();
    }

    // Ramo 1: Agendado canônico (com scheduledFor)
    if let Some(ref sched_str) = payload.scheduled_for {
        if payload.units_taken < 1 || payload.units_taken > 20 {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(HttpErrorResponse::new("Quantidade de unidades inválida.")),
            )
                .into_response();
        }

        let scheduled_utc = match DateTime::parse_from_rfc3339(sched_str) {
            Ok(dt) => dt.with_timezone(&Utc),
            Err(_) => {
                if sched_str.len() == 16 && sched_str.chars().nth(10) == Some('T') {
                    match chrono::NaiveDateTime::parse_from_str(sched_str, "%Y-%m-%dT%H:%M") {
                        Ok(naive) => DateTime::<Utc>::from_naive_utc_and_offset(naive, Utc),
                        Err(_) => {
                            return (
                                StatusCode::UNPROCESSABLE_ENTITY,
                                Json(HttpErrorResponse::new("Identificador de dose inválido.")),
                            )
                                .into_response();
                        }
                    }
                } else {
                    return (
                        StatusCode::UNPROCESSABLE_ENTITY,
                        Json(HttpErrorResponse::new("Identificador de dose inválido.")),
                    )
                        .into_response();
                }
            }
        };

        let mut tx = match pool.begin().await {
            Ok(t) => t,
            Err(e) => {
                error!("Erro ao iniciar transação: {e}");
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(HttpErrorResponse::new("Erro ao iniciar transação.")),
                )
                    .into_response();
            }
        };

        let med_row = match sqlx::query(
            "SELECT name, current_units FROM medications WHERE id = $1 FOR UPDATE",
        )
        .bind(med_uuid)
        .fetch_optional(&mut *tx)
        .await
        {
            Ok(Some(r)) => r,
            Ok(None) => {
                return (
                    StatusCode::NOT_FOUND,
                    Json(HttpErrorResponse::new("Medicamento não encontrado.")),
                )
                    .into_response();
            }
            Err(e) => {
                error!("Erro ao bloquear medicamento: {e}");
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(HttpErrorResponse::new("Erro ao bloquear medicamento.")),
                )
                    .into_response();
            }
        };

        let med_name: String = med_row.get("name");
        let current_units: i32 = med_row.try_get("current_units").unwrap_or(0);

        let oid = compute_occurrence_id(&payload.user_id, &med_uuid.to_string(), &scheduled_utc);

        let existing = sqlx::query(
            r#"SELECT l.id, l.units_taken
               FROM medication_dose_occurrences o
               JOIN medication_logs l ON l.id = o.log_id
               WHERE o.id = $1"#,
        )
        .bind(oid)
        .fetch_optional(&mut *tx)
        .await;

        if let Ok(Some(r)) = existing {
            let log_id: uuid::Uuid = r.get("id");
            let _ = tx.commit().await;
            return (
                StatusCode::OK,
                Json(SyncResponse {
                    status: "success".to_string(),
                    id: log_id.to_string(),
                    medication_contract: Some(2),
                    medication_batch_contract: Some(1),
                    dose_status: Some("already_recorded".to_string()),
                }),
            )
                .into_response();
        }

        if current_units < payload.units_taken {
            return (
                StatusCode::CONFLICT,
                Json(serde_json::json!({
                    "code": "INSUFFICIENT_STOCK",
                    "message": "Estoque insuficiente para registrar esta dose."
                })),
            )
                .into_response();
        }

        let _ = sqlx::query(
            r#"INSERT INTO medication_dose_occurrences (id, patient_id, medication_id, scheduled_at, local_date, local_time, timezone)
               VALUES ($1, $2, $3, $4, $4::date, to_char($4, 'HH24:MI'), 'UTC')
               ON CONFLICT DO NOTHING"#,
        )
        .bind(oid)
        .bind(&payload.user_id)
        .bind(med_uuid)
        .bind(scheduled_utc)
        .execute(&mut *tx)
        .await;

        let log_uuid = payload
            .id
            .as_deref()
            .and_then(|s| uuid::Uuid::parse_str(s).ok())
            .unwrap_or_else(uuid::Uuid::new_v4);
        let idem_key = format!("sync:{}", log_uuid);

        let log_row = sqlx::query(
            r#"INSERT INTO medication_logs (
                   id, user_id, medication_id, taken_at, scheduled_for, actor_id,
                   units_taken, idempotency_key, occurrence_id, channel, canonical_guard
               )
               VALUES ($1, $2, $3, NOW(), $4, $5, $6, $7, $8, 'app', true)
               RETURNING id"#,
        )
        .bind(log_uuid)
        .bind(&payload.user_id)
        .bind(med_uuid)
        .bind(sched_str)
        .bind(&identity.uid)
        .bind(payload.units_taken)
        .bind(idem_key)
        .bind(oid)
        .fetch_one(&mut *tx)
        .await;

        let log_id: uuid::Uuid = match log_row {
            Ok(r) => r.get("id"),
            Err(e) => {
                error!("Erro ao gravar canonical medication_log: {e}");
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(HttpErrorResponse::new("Erro ao persistir log de dose.")),
                )
                    .into_response();
            }
        };

        let _ = sqlx::query("UPDATE medication_dose_occurrences SET log_id = $2 WHERE id = $1")
            .bind(oid)
            .bind(log_id)
            .execute(&mut *tx)
            .await;

        let _ = sqlx::query("UPDATE medications SET current_units = current_units - $2, updated_at = NOW() WHERE id = $1")
            .bind(med_uuid)
            .bind(payload.units_taken)
            .execute(&mut *tx)
            .await;

        let actor_name: String = sqlx::query_scalar("SELECT full_name FROM profiles WHERE id = $1")
            .bind(&identity.uid)
            .fetch_optional(&mut *tx)
            .await
            .ok()
            .flatten()
            .unwrap_or_else(|| "Usuário autorizado".to_string());

        let details = serde_json::json!({
            "medication_id": med_uuid.to_string(),
            "medication_name": med_name,
            "scheduled_for": sched_str,
            "units_taken": payload.units_taken
        });

        let _ = sqlx::query(
            r#"INSERT INTO care_audit_trail (patient_id, actor_id, actor_name, action_type, details)
               VALUES ($1, $2, $3, 'MEDICATION_TAKEN', $4)"#,
        )
        .bind(&payload.user_id)
        .bind(&identity.uid)
        .bind(actor_name)
        .bind(details)
        .execute(&mut *tx)
        .await;

        if let Err(e) = tx.commit().await {
            error!("Erro ao commitar dose: {e}");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(HttpErrorResponse::new(
                    "Erro ao finalizar registro de dose.",
                )),
            )
                .into_response();
        }

        (
            StatusCode::OK,
            Json(SyncResponse {
                status: "success".to_string(),
                id: log_id.to_string(),
                medication_contract: Some(2),
                medication_batch_contract: Some(1),
                dose_status: Some("recorded".to_string()),
            }),
        )
            .into_response()
    } else {
        // Ramo 2: Legado (sem scheduledFor)
        let log_uuid = match payload.id {
            Some(ref id_str) => match uuid::Uuid::parse_str(id_str) {
                Ok(u) => u,
                Err(_) => {
                    return (
                        StatusCode::UNPROCESSABLE_ENTITY,
                        Json(HttpErrorResponse::new("Identificador de dose inválido.")),
                    )
                        .into_response();
                }
            },
            None => uuid::Uuid::new_v4(),
        };

        let taken_at = DateTime::parse_from_rfc3339(&payload.taken_at)
            .map(|dt| dt.with_timezone(&Utc))
            .unwrap_or_else(|_| Utc::now());

        let query = r#"
            INSERT INTO medication_logs (id, user_id, medication_id, taken_at, scheduled_for)
            VALUES ($1, $2, $3, $4, NULL)
            ON CONFLICT (id) DO NOTHING
            RETURNING id"#;

        let row = sqlx::query(query)
            .bind(log_uuid)
            .bind(&payload.user_id)
            .bind(med_uuid)
            .bind(taken_at)
            .fetch_optional(pool)
            .await;

        match row {
            Ok(opt) => {
                let id_str = opt
                    .map(|r| r.get::<uuid::Uuid, _>("id").to_string())
                    .unwrap_or_else(|| log_uuid.to_string());
                (
                    StatusCode::OK,
                    Json(SyncResponse {
                        status: "success".to_string(),
                        id: id_str,
                        medication_contract: Some(2),
                        medication_batch_contract: None,
                        dose_status: None,
                    }),
                )
                    .into_response()
            }
            Err(e) => {
                error!("Erro ao gravar medication_log legado: {e}");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(HttpErrorResponse::new(
                        "Erro interno ao persistir log de medicamento.",
                    )),
                )
                    .into_response()
            }
        }
    }
}

async fn sync_vitals_handler(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(payload): Json<VitalSignSyncDto>,
) -> Response {
    let identity = match authenticate_request(&headers, &state, "POST", "/api/sync/vitals").await {
        Ok(id) => id,
        Err(resp) => return resp,
    };

    if require_owner(&identity.uid, &payload.user_id).is_err() {
        return (
            StatusCode::FORBIDDEN,
            Json(HttpErrorResponse::new(
                "Não autorizado: o recurso pertence a outro usuário.",
            )),
        )
            .into_response();
    }

    let pool = match state.pool {
        Some(ref p) => p,
        None => {
            return (
                StatusCode::OK,
                Json(serde_json::json!({
                    "status": "offline_mode",
                    "id": payload.id.unwrap_or_else(|| "local".to_string())
                })),
            )
                .into_response();
        }
    };

    let measured_dt = DateTime::parse_from_rfc3339(&payload.measured_at)
        .map(|dt| dt.with_timezone(&Utc))
        .unwrap_or_else(|_| Utc::now());

    let query = r#"
        INSERT INTO vital_signs (
            user_id, systolic_pressure, diastolic_pressure, heart_rate,
            oxygen_saturation, glucose_level, glucose_type, hydration_ml, measured_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id"#;

    let row = sqlx::query(query)
        .bind(&payload.user_id)
        .bind(payload.systolic_pressure)
        .bind(payload.diastolic_pressure)
        .bind(payload.heart_rate)
        .bind(payload.oxygen_saturation)
        .bind(payload.glucose_level)
        .bind(payload.glucose_type)
        .bind(payload.hydration_ml)
        .bind(measured_dt)
        .fetch_one(pool)
        .await;

    match row {
        Ok(r) => {
            let id: uuid::Uuid = r.get("id");
            (
                StatusCode::OK,
                Json(SyncResponse {
                    status: "success".to_string(),
                    id: id.to_string(),
                    medication_contract: None,
                    medication_batch_contract: None,
                    dose_status: None,
                }),
            )
                .into_response()
        }
        Err(e) => {
            error!("Erro ao gravar vital_signs: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(HttpErrorResponse::new(
                    "Erro interno ao persistir sinais vitais.",
                )),
            )
                .into_response()
        }
    }
}
