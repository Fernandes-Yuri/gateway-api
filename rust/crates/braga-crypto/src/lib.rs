use std::sync::OnceLock;

use aes_gcm::aead::{Aead, KeyInit, OsRng, Nonce};
use aes_gcm::Aes256Gcm;
use argon2::{Algorithm, Argon2, Params, Version};
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use hkdf::Hkdf;
use hmac::{Hmac, Mac};
use regex::Regex;
use sha2::Sha256;
use subtle::ConstantTimeEq;
use thiserror::Error;
use unicode_normalization::UnicodeNormalization;

pub const ENCRYPTED_PREFIX: &str = "enc:v1:";
pub const IV_LENGTH_BYTES: usize = 12;
pub const TAG_LENGTH_BYTES: usize = 16;
pub const KEY_LENGTH_BYTES: usize = 32;
pub const BLIND_INDEX_HEX_LENGTH: usize = 32;

pub const HKDF_SALT: &[u8] = b"braga-saude-spec12-ale-salt-v1";
pub const HKDF_INFO_DEK: &[u8] = b"braga-saude:column-crypto:aes-256-gcm:v1";
pub const HKDF_INFO_BINDEX: &[u8] = b"braga-saude:column-crypto:blind-index-hmac:v1";

pub const LOCAL_CI_FALLBACK_IKM: &[u8] = b"braga-saude-local-ci-fallback-dek-spec12-v1";
pub const LOCAL_CI_FALLBACK_PEPPER: &[u8] = b"braga-saude-local-ci-fallback-blind-pepper-spec12-v1";

static PHONE_CANDIDATE_RE: OnceLock<Regex> = OnceLock::new();

fn get_phone_candidate_re() -> &'static Regex {
    PHONE_CANDIDATE_RE.get_or_init(|| Regex::new(r"^[\d\s\-\(\)\+\.]+$").unwrap())
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ColumnTamperError {
    #[error("Chave mestra vazia nao permitida para derivacao HKDF.")]
    EmptyMasterKey,
    #[error("Formato de payload criptografado invalido (esperado enc:v1:<iv_b64>:<ciphertext_com_tag_b64>).")]
    InvalidPayloadFormat,
    #[error("Componentes Base64 vazios no payload criptografado.")]
    EmptyBase64Components,
    #[error("Codificacao Base64 corrompida no payload criptografado.")]
    CorruptBase64,
    #[error("Tamanho de IV invalido: esperado {expected} bytes, recebido {received} bytes.")]
    InvalidIvLength { expected: usize, received: usize },
    #[error("Payload truncado: menor que a tag GCM de {0} bytes.")]
    TruncatedPayload(usize),
    #[error("Falha de integridade AES-256-GCM: tag de autenticacao invalida ou dados adulterados.")]
    InvalidTag,
    #[error("Bytes decifrados nao correspondem a UTF-8 valido.")]
    InvalidUtf8,
    #[error("Erro de inicializacao criptografica: {0}")]
    CipherError(String),
}

pub fn derive_key_hkdf(
    master_secret: &[u8],
    info: &[u8],
    salt: &[u8],
    length: usize,
) -> Result<Vec<u8>, ColumnTamperError> {
    if master_secret.is_empty() {
        return Err(ColumnTamperError::EmptyMasterKey);
    }
    let hk = Hkdf::<Sha256>::new(Some(salt), master_secret);
    let mut okm = vec![0u8; length];
    hk.expand(info, &mut okm)
        .map_err(|e| ColumnTamperError::CipherError(e.to_string()))?;
    Ok(okm)
}

pub fn resolve_default_dek() -> Vec<u8> {
    if let Ok(key) = std::env::var("DATA_ENCRYPTION_KEY") {
        let trimmed = key.trim();
        if !trimmed.is_empty() {
            if let Ok(derived) = derive_key_hkdf(trimmed.as_bytes(), HKDF_INFO_DEK, HKDF_SALT, KEY_LENGTH_BYTES) {
                return derived;
            }
        }
    }
    derive_key_hkdf(LOCAL_CI_FALLBACK_IKM, HKDF_INFO_DEK, HKDF_SALT, KEY_LENGTH_BYTES)
        .unwrap_or_else(|_| vec![0u8; KEY_LENGTH_BYTES])
}

pub fn resolve_default_blind_pepper() -> Vec<u8> {
    if let Ok(pepper) = std::env::var("BLIND_INDEX_PEPPER") {
        let trimmed = pepper.trim();
        if !trimmed.is_empty() {
            return trimmed.as_bytes().to_vec();
        }
    }
    if let Ok(key) = std::env::var("DATA_ENCRYPTION_KEY") {
        let trimmed = key.trim();
        if !trimmed.is_empty() {
            if let Ok(derived) = derive_key_hkdf(trimmed.as_bytes(), HKDF_INFO_BINDEX, HKDF_SALT, KEY_LENGTH_BYTES) {
                return derived;
            }
        }
    }
    LOCAL_CI_FALLBACK_PEPPER.to_vec()
}

pub fn is_encrypted(value: Option<&str>) -> bool {
    match value {
        Some(s) => s.starts_with(ENCRYPTED_PREFIX),
        None => false,
    }
}

pub fn encrypt_field(
    plaintext: Option<&str>,
    custom_dek: Option<&[u8]>,
) -> Result<Option<String>, ColumnTamperError> {
    let text = match plaintext {
        None => return Ok(None),
        Some("") => return Ok(Some(String::new())),
        Some(s) if s.starts_with(ENCRYPTED_PREFIX) => return Ok(Some(s.to_string())),
        Some(s) => s,
    };

    let dek = match custom_dek {
        Some(k) => k.to_vec(),
        None => resolve_default_dek(),
    };

    let cipher = Aes256Gcm::new_from_slice(&dek)
        .map_err(|e| ColumnTamperError::CipherError(e.to_string()))?;

    use aes_gcm::aead::rand_core::RngCore;
    let mut iv = [0u8; IV_LENGTH_BYTES];
    OsRng.fill_bytes(&mut iv);

    let nonce = Nonce::from_slice(&iv);
    let ciphertext_with_tag = cipher
        .encrypt(nonce, text.as_bytes())
        .map_err(|e| ColumnTamperError::CipherError(e.to_string()))?;

    let iv_b64 = BASE64_STANDARD.encode(iv);
    let ct_b64 = BASE64_STANDARD.encode(ciphertext_with_tag);

    Ok(Some(format!("{ENCRYPTED_PREFIX}{iv_b64}:{ct_b64}")))
}

pub fn decrypt_field(
    ciphertext: Option<&str>,
    custom_dek: Option<&[u8]>,
) -> Result<Option<String>, ColumnTamperError> {
    let ct = match ciphertext {
        None => return Ok(None),
        Some("") => return Ok(Some(String::new())),
        Some(s) if !s.starts_with(ENCRYPTED_PREFIX) => return Ok(Some(s.to_string())),
        Some(s) => s,
    };

    let parts: Vec<&str> = ct.split(':').collect();
    if parts.len() != 4 || parts[0] != "enc" || parts[1] != "v1" {
        return Err(ColumnTamperError::InvalidPayloadFormat);
    }

    let iv_b64 = parts[2];
    let ct_b64 = parts[3];

    if iv_b64.is_empty() || ct_b64.is_empty() {
        return Err(ColumnTamperError::EmptyBase64Components);
    }

    let iv = BASE64_STANDARD
        .decode(iv_b64)
        .map_err(|_| ColumnTamperError::CorruptBase64)?;
    let ciphertext_with_tag = BASE64_STANDARD
        .decode(ct_b64)
        .map_err(|_| ColumnTamperError::CorruptBase64)?;

    if iv.len() != IV_LENGTH_BYTES {
        return Err(ColumnTamperError::InvalidIvLength {
            expected: IV_LENGTH_BYTES,
            received: iv.len(),
        });
    }

    if ciphertext_with_tag.len() < TAG_LENGTH_BYTES {
        return Err(ColumnTamperError::TruncatedPayload(TAG_LENGTH_BYTES));
    }

    let dek = match custom_dek {
        Some(k) => k.to_vec(),
        None => resolve_default_dek(),
    };

    let cipher = Aes256Gcm::new_from_slice(&dek)
        .map_err(|e| ColumnTamperError::CipherError(e.to_string()))?;

    let nonce = Nonce::from_slice(&iv);
    let plaintext_bytes = cipher
        .decrypt(nonce, ciphertext_with_tag.as_slice())
        .map_err(|_| ColumnTamperError::InvalidTag)?;

    String::from_utf8(plaintext_bytes).map(Some).map_err(|_| ColumnTamperError::InvalidUtf8)
}

pub fn normalize_for_blind_index(
    plaintext: &str,
    dek: Option<&[u8]>,
) -> Result<String, ColumnTamperError> {
    let raw = if is_encrypted(Some(plaintext)) {
        decrypt_field(Some(plaintext), dek)?.unwrap_or_default()
    } else {
        plaintext.to_string()
    };

    let nfkc = raw.nfkc().collect::<String>();
    let trimmed_lower = nfkc.trim().to_lowercase();
    if trimmed_lower.is_empty() {
        return Ok(String::new());
    }

    let phone_re = get_phone_candidate_re();
    if phone_re.is_match(&trimmed_lower) && trimmed_lower.chars().any(|c| c.is_ascii_digit()) {
        let digits: String = trimmed_lower.chars().filter(|c| c.is_ascii_digit()).collect();
        if !digits.is_empty() {
            return Ok(digits);
        }
    }

    let collapsed = trimmed_lower
        .split_whitespace()
        .collect::<Vec<&str>>()
        .join(" ");

    Ok(collapsed)
}

pub fn compute_blind_index(
    plaintext: Option<&str>,
    custom_pepper: Option<&[u8]>,
    dek: Option<&[u8]>,
) -> Result<Option<String>, ColumnTamperError> {
    let text = match plaintext {
        None => return Ok(None),
        Some(s) => s,
    };

    let normalized = normalize_for_blind_index(text, dek)?;
    if normalized.is_empty() {
        return Ok(Some(String::new()));
    }

    let pepper = match custom_pepper {
        Some(p) => p.to_vec(),
        None => resolve_default_blind_pepper(),
    };

    let mut mac = Hmac::<Sha256>::new_from_slice(&pepper)
        .map_err(|e| ColumnTamperError::CipherError(e.to_string()))?;
    mac.update(normalized.as_bytes());
    let result = mac.finalize().into_bytes();
    let hex = hex::encode(result);

    let truncated = if hex.len() > BLIND_INDEX_HEX_LENGTH {
        hex[..BLIND_INDEX_HEX_LENGTH].to_string()
    } else {
        hex
    };

    Ok(Some(truncated))
}

pub fn encrypt_with_blind_index(
    plaintext: Option<&str>,
    dek: Option<&[u8]>,
    pepper: Option<&[u8]>,
) -> Result<(Option<String>, Option<String>), ColumnTamperError> {
    match plaintext {
        None => Ok((None, None)),
        Some(s) => {
            let trimmed = s.trim();
            if trimmed.is_empty() {
                Ok((Some(String::new()), None))
            } else {
                let enc = encrypt_field(Some(trimmed), dek)?;
                let bindex = compute_blind_index(Some(trimmed), pepper, dek)?;
                Ok((enc, bindex))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// auth_crypto: Argon2id + HMAC-SHA256 Pepper & Bcrypt Legado (SPEC-10)
// ---------------------------------------------------------------------------

#[derive(Debug, Error)]
pub enum AuthCryptoError {
    #[error("AUTH_PEPPER_SECRET nao configurada no ambiente.")]
    MissingPepper,
    #[error("Erro ao gerar hash Argon2id: {0}")]
    Argon2Error(String),
}

pub fn pepper_password(password: &str, pepper: &[u8]) -> Result<[u8; 32], AuthCryptoError> {
    if pepper.is_empty() {
        return Err(AuthCryptoError::MissingPepper);
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(pepper)
        .map_err(|_| AuthCryptoError::MissingPepper)?;
    mac.update(password.as_bytes());
    let digest = mac.finalize().into_bytes();
    let mut out = [0u8; 32];
    out.copy_from_slice(&digest);
    Ok(out)
}

pub fn hash_password(password: &str, pepper: &[u8]) -> Result<String, AuthCryptoError> {
    let peppered = pepper_password(password, pepper)?;
    let params = Params::new(65536, 3, 2, Some(32))
        .map_err(|e| AuthCryptoError::Argon2Error(e.to_string()))?;
    let argon2 = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);

    use aes_gcm::aead::rand_core::RngCore;
    let mut salt = [0u8; 16];
    OsRng.fill_bytes(&mut salt);

    use argon2::PasswordHasher;
    let salt_str = argon2::password_hash::SaltString::encode_b64(&salt)
        .map_err(|e| AuthCryptoError::Argon2Error(e.to_string()))?;

    let hash = argon2
        .hash_password(&peppered, &salt_str)
        .map_err(|e| AuthCryptoError::Argon2Error(e.to_string()))?;

    Ok(hash.to_string())
}

pub fn verify_password(
    password: &str,
    stored_hash: Option<&str>,
    pepper: &[u8],
) -> (bool, bool) {
    let hash_str = match stored_hash {
        Some(h) if !h.is_empty() => h,
        _ => return (false, false),
    };

    // Caso 1: Bcrypt legado ($2a$, $2b$, $2y$)
    if hash_str.starts_with("$2a$") || hash_str.starts_with("$2b$") || hash_str.starts_with("$2y$") {
        match bcrypt::verify(password, hash_str) {
            Ok(true) => return (true, true), // Válido e precisa de rehash!
            _ => return (false, false),
        }
    }

    // Caso 2: Argon2id moderno
    if hash_str.starts_with("$argon2id$") {
        let peppered = match pepper_password(password, pepper) {
            Ok(p) => p,
            Err(_) => return (false, false),
        };

        use argon2::PasswordVerifier;
        let parsed = match argon2::PasswordHash::new(hash_str) {
            Ok(p) => p,
            Err(_) => return (false, false),
        };

        let argon2 = Argon2::default();
        let valid = argon2.verify_password(&peppered, &parsed).is_ok();
        if !valid {
            return (false, false);
        }

        let needs_rehash = if let Some(params) = parsed.version {
            params != Version::V0x13
        } else {
            false
        };

        return (valid, needs_rehash);
    }

    (false, false)
}
