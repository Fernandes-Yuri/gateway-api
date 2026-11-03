use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::header::{HeaderMap, HeaderName, HeaderValue, CACHE_CONTROL, CONTENT_TYPE};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{any, get, post};
use axum::Router;
use braga_contracts::{AppUnavailableResponse, HealthResponse, LandingResponse};
use braga_identity::{check_user_moderation, verify_firebase_id_token, FirebaseCertCache};
use braga_limiter::{run_limiter_ipc_socket, StrictRateLimiter};
use clap::Parser;
use hyper_util::rt::TokioIo;
use sqlx::PgPool;
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
}

#[derive(Clone)]
pub struct AppState {
    pub backend_sock: PathBuf,
    pub cert_cache: FirebaseCertCache,
    pub limiter: StrictRateLimiter,
    pub pool: Option<PgPool>,
    pub firebase_project_id: String,
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

    let state = Arc::new(AppState {
        backend_sock: args.backend_sock,
        cert_cache: FirebaseCertCache::new(),
        limiter,
        pool,
        firebase_project_id: args.firebase_project_id,
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
