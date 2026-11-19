//! B2B-16B: Armazenamento desacoplado S3 / Cloudflare R2 + sanitizacao de logos em Rust.
//!
//! Valida magic bytes (PNG, JPEG, WebP), bloqueia payloads SVG/HTML/XML e decompression bombs,
//! re-encoda a imagem limpa como PNG (removendo metadados EXIF/XMP/tEXt) e persiste objetos
//! fora do disco efemero da EC2 via S3 SigV4 HTTP (compativel com Cloudflare R2 e AWS S3).

use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Duration;

use axum::http::StatusCode;
use chrono::Utc;
use hmac::{Hmac, Mac};
use image::imageops::FilterType;
use image::{ImageFormat, ImageReader, Limits};
use reqwest::Client;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use uuid::Uuid;

type HmacSha256 = Hmac<Sha256>;

pub const MAX_LOGO_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_KYB_PROOF_BYTES: usize = 5 * 1024 * 1024;
pub const B2B_LOGO_STORAGE_PREFIX: &str = "b2b-logos";
pub const B2B_KYB_STORAGE_PREFIX: &str = "b2b-kyb";
const MAX_IMAGE_DIMENSION: u32 = 4096;
const MAX_PROOF_IMAGE_DIMENSION: u32 = 6000;
const LOGO_TARGET_DIMENSION: u32 = 512;
const PROOF_IMAGE_TARGET_DIMENSION: u32 = 1600;
const MAX_DECODE_ALLOC_BYTES: u64 = 64 * 1024 * 1024;
const MAX_SIGNED_URL_TTL_SECS: i64 = 30 * 86400;
const EMPTY_PAYLOAD_SHA256: &str =
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

static STORAGE_HTTP_CLIENT: OnceLock<Client> = OnceLock::new();

fn http_client() -> &'static Client {
    STORAGE_HTTP_CLIENT.get_or_init(|| {
        Client::builder()
            .use_rustls_tls()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(20))
            .build()
            .expect("falha ao inicializar cliente HTTP para object storage")
    })
}

pub fn detect_allowed_image_format(
    raw: &[u8],
    max_bytes: usize,
) -> Result<ImageFormat, (StatusCode, &'static str)> {
    if raw.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            "Envie um arquivo de imagem válido.",
        ));
    }
    if raw.len() > max_bytes {
        return Err((
            StatusCode::PAYLOAD_TOO_LARGE,
            "O arquivo excede o tamanho máximo permitido.",
        ));
    }

    let head = &raw[..raw.len().min(256)];
    let head_lower = String::from_utf8_lossy(head).to_ascii_lowercase();
    if head_lower.contains("<svg")
        || head_lower.contains("<?xml")
        || head_lower.contains("<html")
        || head_lower.contains("<script")
        || head_lower.contains("<!doctype")
    {
        return Err((
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "SVG e arquivos HTML/XML não são permitidos por segurança.",
        ));
    }

    if raw.starts_with(&[0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) {
        return Ok(ImageFormat::Png);
    }
    if raw.starts_with(&[0xFF, 0xD8, 0xFF]) {
        return Ok(ImageFormat::Jpeg);
    }
    if raw.len() >= 12 && &raw[0..4] == b"RIFF" && &raw[8..12] == b"WEBP" {
        return Ok(ImageFormat::WebP);
    }

    Err((
        StatusCode::UNSUPPORTED_MEDIA_TYPE,
        "Formato inválido. Envie apenas PNG, JPEG ou WebP.",
    ))
}

fn sanitize_image_to_png_with_bounds(
    raw: &[u8],
    max_upload_bytes: usize,
    max_dimension: u32,
    target_dimension: u32,
) -> Result<Vec<u8>, (StatusCode, &'static str)> {
    let format = detect_allowed_image_format(raw, max_upload_bytes)?;

    let mut reader = ImageReader::with_format(Cursor::new(raw), format);
    let mut limits = Limits::default();
    limits.max_image_width = Some(max_dimension);
    limits.max_image_height = Some(max_dimension);
    limits.max_alloc = Some(MAX_DECODE_ALLOC_BYTES);
    reader.limits(limits);

    let decoded = reader.decode().map_err(|_| {
        (
            StatusCode::UNPROCESSABLE_ENTITY,
            "Não foi possível processar a imagem enviada. Verifique dimensões e integridade.",
        )
    })?;

    let resized = if decoded.width() > target_dimension || decoded.height() > target_dimension {
        decoded.resize(target_dimension, target_dimension, FilterType::Triangle)
    } else {
        decoded
    };

    let mut png_bytes: Vec<u8> = Vec::with_capacity(64 * 1024);
    resized
        .write_to(&mut Cursor::new(&mut png_bytes), ImageFormat::Png)
        .map_err(|_| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                "Erro ao gerar imagem sanitizada.",
            )
        })?;

    if png_bytes.is_empty() || png_bytes.len() > max_upload_bytes {
        return Err((
            StatusCode::PAYLOAD_TOO_LARGE,
            "A imagem processada excedeu o limite de tamanho permitido.",
        ));
    }

    Ok(png_bytes)
}

/// Decodifica apenas PNG/JPEG/WebP dentro dos limites seguros de RAM e re-encoda para PNG limpo,
/// eliminando blocos EXIF, GPS, XMP e payloads embutidos.
pub fn sanitize_logo_image_to_png(raw: &[u8]) -> Result<Vec<u8>, (StatusCode, &'static str)> {
    sanitize_image_to_png_with_bounds(
        raw,
        MAX_LOGO_BYTES,
        MAX_IMAGE_DIMENSION,
        LOGO_TARGET_DIMENSION,
    )
}

pub struct PreparedProofMedia {
    pub bytes: Vec<u8>,
    pub content_type: &'static str,
    pub extension: &'static str,
}

/// Prepara comprovante privado B2B-09B (PDF ou imagem PNG/JPEG/WebP sanitizada sem EXIF, ate 5 MB).
pub fn prepare_kyb_proof_media(
    raw: &[u8],
) -> Result<PreparedProofMedia, (StatusCode, &'static str)> {
    if raw.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "Arquivo de comprovante vazio."));
    }
    if raw.len() > MAX_KYB_PROOF_BYTES {
        return Err((
            StatusCode::PAYLOAD_TOO_LARGE,
            "O comprovante deve ter no máximo 5 MB.",
        ));
    }
    if raw.starts_with(b"%PDF-1.") || raw.starts_with(b"%PDF-2.") {
        return Ok(PreparedProofMedia {
            bytes: raw.to_vec(),
            content_type: "application/pdf",
            extension: "pdf",
        });
    }
    let png = sanitize_image_to_png_with_bounds(
        raw,
        MAX_KYB_PROOF_BYTES,
        MAX_PROOF_IMAGE_DIMENSION,
        PROOF_IMAGE_TARGET_DIMENSION,
    )?;
    Ok(PreparedProofMedia {
        bytes: png,
        content_type: "image/png",
        extension: "png",
    })
}

pub fn validate_kyb_proof_filename(file: &str) -> Result<&'static str, (StatusCode, &'static str)> {
    let trimmed = file.trim();
    if !(5..=96).contains(&trimmed.len())
        || trimmed.starts_with('.')
        || trimmed.contains("..")
        || trimmed.contains('/')
        || trimmed.contains('\\')
        || !trimmed
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
    {
        return Err((StatusCode::BAD_REQUEST, "Nome de comprovante inválido."));
    }
    let lower = trimmed.to_ascii_lowercase();
    if lower.ends_with(".pdf") {
        Ok("application/pdf")
    } else if lower.ends_with(".png") {
        Ok("image/png")
    } else {
        Err((StatusCode::BAD_REQUEST, "Extensão de comprovante inválida."))
    }
}

pub fn validate_logo_filename(file: &str) -> Result<&'static str, (StatusCode, &'static str)> {
    let trimmed = file.trim();
    if !(5..=96).contains(&trimmed.len())
        || trimmed.starts_with('.')
        || trimmed.contains("..")
        || trimmed.contains('/')
        || trimmed.contains('\\')
        || !trimmed
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
    {
        return Err((StatusCode::BAD_REQUEST, "Nome de arquivo inválido."));
    }

    let lower = trimmed.to_ascii_lowercase();
    if lower.ends_with(".png") {
        Ok("image/png")
    } else if lower.ends_with(".jpg") || lower.ends_with(".jpeg") {
        Ok("image/jpeg")
    } else if lower.ends_with(".webp") {
        Ok("image/webp")
    } else {
        Err((StatusCode::BAD_REQUEST, "Extensão de logo não permitida."))
    }
}

pub fn extract_logo_key_from_url(logo_url: &str, program_id: Uuid) -> Option<String> {
    let without_query = logo_url.split('?').next().unwrap_or(logo_url).trim();
    let needle = format!("/api/files/{B2B_LOGO_STORAGE_PREFIX}/{program_id}/");
    let (_, file_part) = without_query.split_once(&needle)?;
    if validate_logo_filename(file_part).is_ok() {
        Some(format!(
            "{B2B_LOGO_STORAGE_PREFIX}/{program_id}/{file_part}"
        ))
    } else {
        None
    }
}

pub fn resolve_b2b_file_signing_secret() -> Option<Vec<u8>> {
    for key in [
        "B2B_FILE_SIGNING_SECRET",
        "SERVICE_TOKEN",
        "AUTH_PEPPER_SECRET",
        "DATA_ENCRYPTION_KEY",
    ] {
        if let Ok(val) = std::env::var(key) {
            let trimmed = val.trim();
            if trimmed.len() >= 16 {
                return Some(trimmed.as_bytes().to_vec());
            }
        }
    }
    None
}

pub fn compute_b2b_file_signature_hex(
    program_id: Uuid,
    file: &str,
    exp: i64,
    secret: &[u8],
) -> Result<String, &'static str> {
    if secret.is_empty() {
        return Err("Segredo de assinatura indisponível.");
    }
    let mut mac =
        HmacSha256::new_from_slice(secret).map_err(|_| "Chave HMAC inválida para assinatura.")?;
    let payload = format!("b2b-logo:{program_id}:{file}:{exp}");
    mac.update(payload.as_bytes());
    Ok(hex::encode(mac.finalize().into_bytes()))
}

pub fn verify_b2b_file_signature(
    program_id: Uuid,
    file: &str,
    exp: i64,
    sig_hex: &str,
    secret: &[u8],
    now_ts: i64,
) -> bool {
    if exp <= now_ts || exp > now_ts + MAX_SIGNED_URL_TTL_SECS {
        return false;
    }
    let Ok(expected) = compute_b2b_file_signature_hex(program_id, file, exp, secret) else {
        return false;
    };
    let provided = sig_hex.trim().to_ascii_lowercase();
    if provided.len() != expected.len() {
        return false;
    }
    bool::from(expected.as_bytes().ct_eq(provided.as_bytes()))
}

#[derive(Debug, Clone)]
pub struct S3SigV4Config {
    pub bucket: String,
    pub endpoint_url: String,
    pub region: String,
    pub access_key_id: String,
    pub secret_access_key: String,
}

#[derive(Debug, Clone)]
pub enum B2BObjectStorage {
    S3R2(S3SigV4Config),
    LocalDir(PathBuf),
}

fn first_nonempty_env(keys: &[&str]) -> Option<String> {
    for k in keys {
        if let Ok(v) = std::env::var(k) {
            let trimmed = v.trim();
            if !trimmed.is_empty() {
                return Some(trimmed.to_string());
            }
        }
    }
    None
}

fn validate_object_key(key: &str) -> Result<(), String> {
    if key.is_empty()
        || key.starts_with('/')
        || key.ends_with('/')
        || key.contains("//")
        || key.contains("..")
        || key.contains('\\')
        || !key
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'/'))
    {
        return Err("Chave de objeto de armazenamento inválida.".to_string());
    }
    Ok(())
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

fn hmac_sha256(key: &[u8], data: &[u8]) -> Result<Vec<u8>, String> {
    let mut mac =
        HmacSha256::new_from_slice(key).map_err(|e| format!("Falha ao inicializar HMAC: {e}"))?;
    mac.update(data);
    Ok(mac.finalize().into_bytes().to_vec())
}

fn extract_host_from_endpoint(endpoint_url: &str) -> Result<(String, String), String> {
    let clean = endpoint_url.trim().trim_end_matches('/');
    let rest = clean
        .strip_prefix("https://")
        .or_else(|| clean.strip_prefix("http://"))
        .ok_or_else(|| "Endpoint S3/R2 deve começar com https:// ou http://".to_string())?;
    let host_part = rest.split('/').next().unwrap_or("");
    if host_part.is_empty() {
        return Err("Endpoint S3/R2 sem host válido.".to_string());
    }
    Ok((clean.to_string(), host_part.to_string()))
}

struct SignedS3Headers {
    url: String,
    amz_date: String,
    payload_sha256: String,
    authorization: String,
}

fn sign_s3_request(
    cfg: &S3SigV4Config,
    method: &str,
    key: &str,
    content_type: Option<&str>,
    payload: &[u8],
    amz_date: &str,
) -> Result<SignedS3Headers, String> {
    validate_object_key(key)?;
    if amz_date.len() < 8 {
        return Err("Timestamp SigV4 inválido.".to_string());
    }
    let date_stamp = &amz_date[0..8];
    let (base_endpoint, host) = extract_host_from_endpoint(&cfg.endpoint_url)?;
    let canonical_uri = format!("/{}/{}", cfg.bucket, key);
    let url = format!("{base_endpoint}{canonical_uri}");
    let payload_sha256 = if payload.is_empty() {
        EMPTY_PAYLOAD_SHA256.to_string()
    } else {
        sha256_hex(payload)
    };

    let (canonical_headers, signed_headers) = match content_type {
        Some(ct) => (
            format!(
                "content-type:{ct}\nhost:{host}\nx-amz-content-sha256:{payload_sha256}\nx-amz-date:{amz_date}\n"
            ),
            "content-type;host;x-amz-content-sha256;x-amz-date",
        ),
        None => (
            format!("host:{host}\nx-amz-content-sha256:{payload_sha256}\nx-amz-date:{amz_date}\n"),
            "host;x-amz-content-sha256;x-amz-date",
        ),
    };

    let canonical_request = format!(
        "{method}\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_sha256}"
    );
    let canonical_req_hash = sha256_hex(canonical_request.as_bytes());
    let credential_scope = format!("{date_stamp}/{}/s3/aws4_request", cfg.region);
    let string_to_sign =
        format!("AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{canonical_req_hash}");

    let k_date = hmac_sha256(
        format!("AWS4{}", cfg.secret_access_key).as_bytes(),
        date_stamp.as_bytes(),
    )?;
    let k_region = hmac_sha256(&k_date, cfg.region.as_bytes())?;
    let k_service = hmac_sha256(&k_region, b"s3")?;
    let k_signing = hmac_sha256(&k_service, b"aws4_request")?;
    let signature = hex::encode(hmac_sha256(&k_signing, string_to_sign.as_bytes())?);

    let authorization = format!(
        "AWS4-HMAC-SHA256 Credential={}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}",
        cfg.access_key_id
    );

    Ok(SignedS3Headers {
        url,
        amz_date: amz_date.to_string(),
        payload_sha256,
        authorization,
    })
}

fn local_safe_path(root: &Path, key: &str) -> Result<PathBuf, String> {
    validate_object_key(key)?;
    let mut full = root.to_path_buf();
    for seg in key.split('/') {
        full.push(seg);
    }
    Ok(full)
}

impl B2BObjectStorage {
    pub fn from_env() -> Result<Self, String> {
        let bucket = first_nonempty_env(&["B2B_STORAGE_BUCKET", "CF_R2_BUCKET_NAME"]);
        let endpoint = first_nonempty_env(&["B2B_STORAGE_ENDPOINT", "CF_R2_ENDPOINT_URL"]);
        let access_key = first_nonempty_env(&["B2B_STORAGE_ACCESS_KEY_ID", "CF_R2_ACCESS_KEY_ID"]);
        let secret_key =
            first_nonempty_env(&["B2B_STORAGE_SECRET_ACCESS_KEY", "CF_R2_SECRET_ACCESS_KEY"]);

        let any_cloud =
            bucket.is_some() || endpoint.is_some() || access_key.is_some() || secret_key.is_some();
        if any_cloud {
            let (Some(b), Some(ep), Some(ak), Some(sk)) =
                (bucket, endpoint, access_key, secret_key)
            else {
                return Err(
                    "Configuração incompleta de armazenamento S3/R2 no servidor.".to_string(),
                );
            };
            let region = first_nonempty_env(&["B2B_STORAGE_REGION", "AWS_REGION"])
                .unwrap_or_else(|| "auto".to_string());
            return Ok(Self::S3R2(S3SigV4Config {
                bucket: b,
                endpoint_url: ep,
                region,
                access_key_id: ak,
                secret_access_key: sk,
            }));
        }

        let root = first_nonempty_env(&["STORAGE_DIR"])
            .map(PathBuf::from)
            .unwrap_or_else(|| std::env::temp_dir().join("braga-b2b-storage"));
        Ok(Self::LocalDir(root))
    }

    pub async fn put_object(
        &self,
        key: &str,
        content_type: &str,
        bytes: &[u8],
    ) -> Result<(), String> {
        match self {
            Self::S3R2(cfg) => {
                let amz_date = Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
                let signed =
                    sign_s3_request(cfg, "PUT", key, Some(content_type), bytes, &amz_date)?;
                let resp = http_client()
                    .put(&signed.url)
                    .header("content-type", content_type)
                    .header("x-amz-content-sha256", &signed.payload_sha256)
                    .header("x-amz-date", &signed.amz_date)
                    .header("authorization", &signed.authorization)
                    .body(bytes.to_vec())
                    .send()
                    .await
                    .map_err(|e| format!("Falha ao enviar objeto para R2/S3: {e}"))?;

                if !resp.status().is_success() {
                    let status = resp.status();
                    return Err(format!("R2/S3 recusou PUT ({status}) para {key}"));
                }
                Ok(())
            }
            Self::LocalDir(root) => {
                let path = local_safe_path(root, key)?;
                if let Some(parent) = path.parent() {
                    tokio::fs::create_dir_all(parent)
                        .await
                        .map_err(|e| format!("Falha ao criar diretório local de logo: {e}"))?;
                }
                tokio::fs::write(&path, bytes)
                    .await
                    .map_err(|e| format!("Falha ao gravar arquivo local de logo: {e}"))
            }
        }
    }

    pub async fn get_object(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        match self {
            Self::S3R2(cfg) => {
                let amz_date = Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
                let signed = sign_s3_request(cfg, "GET", key, None, &[], &amz_date)?;
                let resp = http_client()
                    .get(&signed.url)
                    .header("x-amz-content-sha256", &signed.payload_sha256)
                    .header("x-amz-date", &signed.amz_date)
                    .header("authorization", &signed.authorization)
                    .send()
                    .await
                    .map_err(|e| format!("Falha ao baixar objeto de R2/S3: {e}"))?;

                if resp.status() == reqwest::StatusCode::NOT_FOUND {
                    return Ok(None);
                }
                if !resp.status().is_success() {
                    let status = resp.status();
                    return Err(format!("R2/S3 recusou GET ({status}) para {key}"));
                }
                let data = resp
                    .bytes()
                    .await
                    .map_err(|e| format!("Falha ao ler corpo do objeto R2/S3: {e}"))?;
                Ok(Some(data.to_vec()))
            }
            Self::LocalDir(root) => {
                let path = local_safe_path(root, key)?;
                match tokio::fs::read(&path).await {
                    Ok(bytes) => Ok(Some(bytes)),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
                    Err(e) => Err(format!("Falha ao ler arquivo local de logo: {e}")),
                }
            }
        }
    }

    pub async fn delete_object(&self, key: &str) -> Result<(), String> {
        match self {
            Self::S3R2(cfg) => {
                let amz_date = Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
                let signed = sign_s3_request(cfg, "DELETE", key, None, &[], &amz_date)?;
                let resp = http_client()
                    .delete(&signed.url)
                    .header("x-amz-content-sha256", &signed.payload_sha256)
                    .header("x-amz-date", &signed.amz_date)
                    .header("authorization", &signed.authorization)
                    .send()
                    .await
                    .map_err(|e| format!("Falha ao excluir objeto de R2/S3: {e}"))?;

                if resp.status() == reqwest::StatusCode::NOT_FOUND || resp.status().is_success() {
                    Ok(())
                } else {
                    let status = resp.status();
                    Err(format!("R2/S3 recusou DELETE ({status}) para {key}"))
                }
            }
            Self::LocalDir(root) => {
                let path = local_safe_path(root, key)?;
                match tokio::fs::remove_file(&path).await {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(e) => Err(format!("Falha ao remover arquivo local de logo: {e}")),
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::Bytes,
        extract::{Path as AxumPath, State as AxumState},
        http::HeaderMap,
        routing::put,
        Router,
    };
    use image::{ImageBuffer, Rgba};
    use std::collections::HashMap;
    use std::sync::Arc;
    use tokio::sync::Mutex;

    fn make_png_with_secret_text_chunk(width: u32, height: u32, secret_marker: &[u8]) -> Vec<u8> {
        let img: ImageBuffer<Rgba<u8>, Vec<u8>> =
            ImageBuffer::from_pixel(width, height, Rgba([26, 107, 92, 255]));
        let mut base = Vec::new();
        image::DynamicImage::ImageRgba8(img)
            .write_to(&mut Cursor::new(&mut base), ImageFormat::Png)
            .unwrap();

        // Injeta chunk tEXt antes do chunk IEND (ultimos 12 bytes de um PNG valido)
        let iend_start = base.len() - 12;
        let mut out = Vec::with_capacity(base.len() + secret_marker.len() + 32);
        out.extend_from_slice(&base[..iend_start]);

        let mut payload = b"Software\0".to_vec();
        payload.extend_from_slice(secret_marker);
        let len_be = (payload.len() as u32).to_be_bytes();
        out.extend_from_slice(&len_be);
        out.extend_from_slice(b"tEXt");
        out.extend_from_slice(&payload);
        // CRC simples apenas para demonstrar payload nao critico; decoder PNG ignora ou validamos ambos:
        // Nota: decoders estritos validam CRC de chunks auxiliares, entao calculamos CRC32 real de PNG.
        let crc = crc32_png(b"tEXt", &payload);
        out.extend_from_slice(&crc.to_be_bytes());
        out.extend_from_slice(&base[iend_start..]);
        out
    }

    fn crc32_png(chunk_type: &[u8], data: &[u8]) -> u32 {
        let mut crc: u32 = 0xFFFF_FFFF;
        for &b in chunk_type.iter().chain(data.iter()) {
            crc ^= u32::from(b);
            for _ in 0..8 {
                let mask = (crc & 1).wrapping_neg();
                crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
            }
        }
        !crc
    }

    #[test]
    fn sanitize_strips_exif_text_chunks_scales_oversize_and_blocks_svg() {
        assert!(matches!(
            detect_allowed_image_format(
                b"<svg xmlns='x'><script>alert(1)</script></svg>",
                MAX_LOGO_BYTES
            ),
            Err((StatusCode::UNSUPPORTED_MEDIA_TYPE, _))
        ));
        assert!(matches!(
            detect_allowed_image_format(b"<?xml version='1.0'?><svg/>", MAX_LOGO_BYTES),
            Err((StatusCode::UNSUPPORTED_MEDIA_TYPE, _))
        ));
        assert!(matches!(
            detect_allowed_image_format(b"%PDF-1.7 fake", MAX_LOGO_BYTES),
            Err((StatusCode::UNSUPPORTED_MEDIA_TYPE, _))
        ));
        assert!(matches!(
            detect_allowed_image_format(&vec![0u8; MAX_LOGO_BYTES + 1], MAX_LOGO_BYTES),
            Err((StatusCode::PAYLOAD_TOO_LARGE, _))
        ));

        let proof_pdf = prepare_kyb_proof_media(b"%PDF-1.4\n%test").unwrap();
        assert_eq!(proof_pdf.content_type, "application/pdf");
        assert_eq!(proof_pdf.extension, "pdf");
        assert!(prepare_kyb_proof_media(b"<svg></svg>").is_err());
        assert_eq!(
            validate_kyb_proof_filename("proof-abc123.pdf").unwrap(),
            "application/pdf"
        );
        assert!(validate_kyb_proof_filename("../hack.pdf").is_err());

        let marker = b"EXIF_GPS_SECRET_LAT_-23_LON_-46";
        let dirty_png = make_png_with_secret_text_chunk(800, 600, marker);
        assert!(dirty_png.windows(marker.len()).any(|w| w == marker));

        let clean_png = sanitize_logo_image_to_png(&dirty_png).unwrap();
        assert!(clean_png.starts_with(&[0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]));
        assert!(
            !clean_png.windows(marker.len()).any(|w| w == marker),
            "metadados tEXt/EXIF devem ser removidos pela re-codificacao"
        );
        let reader = ImageReader::with_format(Cursor::new(&clean_png), ImageFormat::Png);
        let decoded = reader.decode().unwrap();
        assert!(decoded.width() <= LOGO_TARGET_DIMENSION);
        assert!(decoded.height() <= LOGO_TARGET_DIMENSION);
    }

    #[test]
    fn signed_url_token_and_filename_guards() {
        assert_eq!(
            validate_logo_filename("logo-1234abcd.png").unwrap(),
            "image/png"
        );
        assert!(validate_logo_filename("../secret.png").is_err());
        assert!(validate_logo_filename("logo.svg").is_err());

        let pid = Uuid::new_v4();
        let secret = b"super-secret-b2b-signing-key-2026";
        let now = 1_700_000_000;
        let exp = now + 3600;
        let sig = compute_b2b_file_signature_hex(pid, "logo-a.png", exp, secret).unwrap();

        assert!(verify_b2b_file_signature(
            pid,
            "logo-a.png",
            exp,
            &sig,
            secret,
            now
        ));
        assert!(!verify_b2b_file_signature(
            pid,
            "logo-b.png",
            exp,
            &sig,
            secret,
            now
        ));
        assert!(!verify_b2b_file_signature(
            Uuid::new_v4(),
            "logo-a.png",
            exp,
            &sig,
            secret,
            now
        ));
        assert!(!verify_b2b_file_signature(
            pid,
            "logo-a.png",
            exp,
            &sig,
            secret,
            exp + 1
        ));
    }

    #[tokio::test]
    async fn s3_r2_sigv4_put_get_delete_roundtrip_against_mock_server() {
        #[derive(Default)]
        struct StoreState {
            objects: Mutex<HashMap<String, Vec<u8>>>,
            auth_headers: Mutex<Vec<String>>,
        }

        async fn put_obj(
            AxumState(st): AxumState<Arc<StoreState>>,
            AxumPath((bucket, key)): AxumPath<(String, String)>,
            headers: HeaderMap,
            body: Bytes,
        ) -> StatusCode {
            if let Some(auth) = headers.get("authorization").and_then(|v| v.to_str().ok()) {
                st.auth_headers.lock().await.push(auth.to_string());
            }
            st.objects
                .lock()
                .await
                .insert(format!("{bucket}/{key}"), body.to_vec());
            StatusCode::OK
        }

        async fn get_obj(
            AxumState(st): AxumState<Arc<StoreState>>,
            AxumPath((bucket, key)): AxumPath<(String, String)>,
            headers: HeaderMap,
        ) -> Result<Vec<u8>, StatusCode> {
            if let Some(auth) = headers.get("authorization").and_then(|v| v.to_str().ok()) {
                st.auth_headers.lock().await.push(auth.to_string());
            }
            st.objects
                .lock()
                .await
                .get(&format!("{bucket}/{key}"))
                .cloned()
                .ok_or(StatusCode::NOT_FOUND)
        }

        async fn del_obj(
            AxumState(st): AxumState<Arc<StoreState>>,
            AxumPath((bucket, key)): AxumPath<(String, String)>,
        ) -> StatusCode {
            st.objects.lock().await.remove(&format!("{bucket}/{key}"));
            StatusCode::NO_CONTENT
        }

        let store_state = Arc::new(StoreState::default());
        let app = Router::new()
            .route("/:bucket/*key", put(put_obj).get(get_obj).delete(del_obj))
            .with_state(store_state.clone());

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server_task = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });

        let storage = B2BObjectStorage::S3R2(S3SigV4Config {
            bucket: "braga-saude".to_string(),
            endpoint_url: format!("http://{addr}"),
            region: "auto".to_string(),
            access_key_id: "test-access".to_string(),
            secret_access_key: "test-secret".to_string(),
        });

        let key = "b2b-logos/11111111-1111-1111-1111-111111111111/logo-test.png";
        storage
            .put_object(key, "image/png", b"png-data")
            .await
            .unwrap();
        assert_eq!(
            storage.get_object(key).await.unwrap(),
            Some(b"png-data".to_vec())
        );
        storage.delete_object(key).await.unwrap();
        assert_eq!(storage.get_object(key).await.unwrap(), None);

        let recorded_auths = store_state.auth_headers.lock().await;
        assert!(recorded_auths.len() >= 2);
        assert!(recorded_auths[0].starts_with("AWS4-HMAC-SHA256 Credential=test-access/"));
        assert!(recorded_auths[0].contains("/auto/s3/aws4_request"));

        server_task.abort();
    }
}
