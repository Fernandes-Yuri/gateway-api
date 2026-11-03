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
    ActivityFeedRecord, AppUnavailableResponse, BarcodeMedication, BiometryRecord,
    DailyMetricRecord, HealthResponse, LandingResponse, MedicationLogRecord, MedicationRecord,
    VitalSignRecord,
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
    });

    let app = Router::new()
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
        )
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
        if let Err(err) = conn.await {
            warn!("Conexao com backend encerrou com erro: {err:?}");
        }
    });

    let resp = sender
        .send_request(req)
        .await
        .map_err(|_| StatusCode::BAD_GATEWAY)?;

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
