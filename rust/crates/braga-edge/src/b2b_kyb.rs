//! B2B-09B: Criterios de cadastro (CNPJ obrigatorio) & Motor de Aprovacao Automatizada (Auto-KYB).
//!
//! Executa validacao mod-11 de CNPJ e CPF, proibe cadastros apenas com CPF, nao exige dominio de
//! e-mail corporativo, aplica blacklist de marcas/orgaos protegidos, consulta a Receita Federal
//! com cache de 24h no Postgres e fallback multi-provedor (BrasilAPI -> CNPJ.ws -> ReceitaWS),
//! e aplica o semaforo Verde (aprovacao automatica), Amarelo (fila manual) ou Vermelho (bloqueio).

use std::sync::OnceLock;
use std::time::Duration;

use axum::http::StatusCode;
use chrono::{NaiveDate, Utc};
use hmac::{Hmac, Mac};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row};

use crate::b2b::TemplateKey;

type HmacSha256 = Hmac<Sha256>;

pub const ALLOWED_RESPONSIBLE_ROLES: &[&str] =
    &["SOCIO_TITULAR", "RESPONSAVEL_TECNICO", "GERENTE_ADMIN"];
pub const ALLOWED_COUNCIL_TYPES: &[&str] = &["CRM", "CRN", "CREFITO", "COREN", "CRF", "CRP", "CRO"];
pub const ALLOWED_PROOF_DOC_TYPES: &[&str] = &[
    "CRM_CARTEIRA",
    "CARTAO_CNPJ_CCMEI",
    "CARTA_AUTORIZACAO",
    "DOC_RESPONSAVEL",
];
pub const ALLOWED_BR_UFS: &[&str] = &[
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR",
    "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
];

const PROTECTED_WHOLE_WORD_BRANDS: &[&str] = &["sus"];
const PROTECTED_SUBSTRING_BRANDS: &[&str] = &[
    "hospital",
    "ministerio",
    "secretaria de saude",
    "prefeitura",
    "governo",
    "braga",
    "albert einstein",
    "sirio libanes",
    "fleury",
    "sabin",
    "dasa",
    "rede dor",
    "hapvida",
    "notredame",
    "unimed",
    "amil",
    "bradesco saude",
    "sulamerica",
];

static KYB_HTTP_CLIENT: OnceLock<Client> = OnceLock::new();

fn kyb_http_client() -> &'static Client {
    KYB_HTTP_CLIENT.get_or_init(|| {
        Client::builder()
            .use_rustls_tls()
            .user_agent("BragaSaude-Gateway/1.0 (+https://bragasaude.online)")
            .connect_timeout(Duration::from_secs(4))
            .timeout(Duration::from_secs(6))
            .build()
            .expect("falha ao inicializar cliente HTTP para consulta CNPJ")
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NormalizedCpf {
    pub central_6: String,
    pub masked: String,
    pub hash_hex: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct QsaPartner {
    pub nome_socio: String,
    pub cpf_cnpj_socio: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ReceitaCnpjRecord {
    pub cnpj_digits: String,
    pub razao_social: String,
    pub nome_fantasia: Option<String>,
    pub situacao_cadastral: String,
    pub data_inicio_atividade: Option<String>,
    pub cnae_fiscal: String,
    pub cnae_fiscal_descricao: Option<String>,
    pub cnaes_secundarios: Vec<String>,
    pub natureza_juridica: Option<String>,
    pub opcao_pelo_mei: Option<bool>,
    pub uf: Option<String>,
    pub municipio: Option<String>,
    pub qsa: Vec<QsaPartner>,
    pub provider: String,
}

impl ReceitaCnpjRecord {
    pub fn is_active(&self) -> bool {
        self.situacao_cadastral.trim().eq_ignore_ascii_case("ATIVA")
    }

    pub fn is_mei(&self) -> bool {
        if self.opcao_pelo_mei == Some(true) {
            return true;
        }
        let nat = fold_ascii_lower(self.natureza_juridica.as_deref().unwrap_or(""));
        nat.contains("empresario individual") && nat.contains("mei")
    }

    pub fn start_date(&self) -> Option<NaiveDate> {
        let raw = self.data_inicio_atividade.as_deref()?.trim();
        NaiveDate::parse_from_str(raw, "%Y-%m-%d").ok()
    }

    pub fn age_days(&self, today: NaiveDate) -> Option<i64> {
        self.start_date().map(|d| (today - d).num_days())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KybLookupOutcome {
    Found {
        record: Box<ReceitaCnpjRecord>,
        from_cache: bool,
    },
    NotFound,
    Unavailable(String),
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct NormalizedCouncilFields {
    pub council_type: Option<String>,
    pub council_number: Option<String>,
    pub council_uf: Option<String>,
}

pub struct AutoKybInput<'a> {
    pub template: TemplateKey,
    pub company_name: &'a str,
    pub program_name: &'a str,
    pub responsible_name: &'a str,
    pub cpf: &'a NormalizedCpf,
    pub has_council: bool,
    pub terms_accepted: bool,
    pub auto_approve_enabled: bool,
    pub infra_ok: bool,
    pub today: NaiveDate,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KybEvaluation {
    Green {
        evidence: Value,
    },
    Yellow {
        flags: Vec<String>,
        summary_note: String,
        evidence: Value,
    },
    Red {
        status: StatusCode,
        detail: String,
        flags: Vec<String>,
    },
}

#[derive(Debug, Clone)]
pub struct KybProviderEndpoints {
    pub brasilapi_base: String,
    pub cnpjws_base: String,
    pub receitaws_base: String,
}

impl Default for KybProviderEndpoints {
    fn default() -> Self {
        Self {
            brasilapi_base: std::env::var("B2B_KYB_BRASILAPI_BASE")
                .ok()
                .filter(|v| !v.trim().is_empty())
                .unwrap_or_else(|| "https://brasilapi.com.br".to_string()),
            cnpjws_base: std::env::var("B2B_KYB_CNPJWS_BASE")
                .ok()
                .filter(|v| !v.trim().is_empty())
                .unwrap_or_else(|| "https://publica.cnpj.ws".to_string()),
            receitaws_base: std::env::var("B2B_KYB_RECEITAWS_BASE")
                .ok()
                .filter(|v| !v.trim().is_empty())
                .unwrap_or_else(|| "https://receitaws.com.br".to_string()),
        }
    }
}

pub fn auto_approve_enabled_from_env() -> bool {
    match std::env::var("B2B_AUTO_APPROVE_ENABLED") {
        Ok(raw) => {
            let trimmed = raw.trim().to_ascii_lowercase();
            !matches!(trimmed.as_str(), "0" | "false" | "no" | "off")
        }
        Err(_) => true,
    }
}

/// Validacao matematica mod-11 oficial de CNPJ brasileiro (14 digitos).
pub fn verify_cnpj_mod11(digits: &str) -> bool {
    let bytes = digits.as_bytes();
    if bytes.len() != 14 || !bytes.iter().all(u8::is_ascii_digit) {
        return false;
    }
    let first = bytes[0];
    if bytes.iter().all(|&b| b == first) {
        return false;
    }
    let nums: Vec<u32> = bytes.iter().map(|b| u32::from(b - b'0')).collect();

    let w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2];
    let sum1: u32 = nums[0..12].iter().zip(w1.iter()).map(|(a, b)| a * b).sum();
    let rem1 = sum1 % 11;
    let dv1 = if rem1 < 2 { 0 } else { 11 - rem1 };
    if nums[12] != dv1 {
        return false;
    }

    let w2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2];
    let sum2: u32 = nums[0..13].iter().zip(w2.iter()).map(|(a, b)| a * b).sum();
    let rem2 = sum2 % 11;
    let dv2 = if rem2 < 2 { 0 } else { 11 - rem2 };
    nums[13] == dv2
}

/// Validacao mod-11 de CPF, gerando forma mascarada (`***.456.789-**`) e blind-hash SHA-256/HMAC.
/// Nunca retorna nem persiste o CPF completo em texto puro.
pub fn normalize_and_hash_cpf(
    raw: Option<&str>,
    required: bool,
) -> Result<Option<NormalizedCpf>, &'static str> {
    let cleaned = raw.unwrap_or("").trim();
    if cleaned.is_empty() {
        if required {
            return Err(
                "Informe o CPF do responsável para conferência automática na Receita Federal.",
            );
        }
        return Ok(None);
    }

    let digits: String = cleaned.chars().filter(|c| c.is_ascii_digit()).collect();
    if !verify_cpf_mod11(&digits) {
        return Err("CPF do responsável inválido. Confira os 11 dígitos.");
    }

    let central_6 = digits[3..9].to_string();
    let masked = format!("***.{}.{}-**", &digits[3..6], &digits[6..9]);
    let hash_hex = compute_cpf_blind_hash(&digits);

    Ok(Some(NormalizedCpf {
        central_6,
        masked,
        hash_hex,
    }))
}

fn verify_cpf_mod11(digits: &str) -> bool {
    let bytes = digits.as_bytes();
    if bytes.len() != 11 || !bytes.iter().all(u8::is_ascii_digit) {
        return false;
    }
    let first = bytes[0];
    if bytes.iter().all(|&b| b == first) {
        return false;
    }
    let nums: Vec<u32> = bytes.iter().map(|b| u32::from(b - b'0')).collect();

    let sum1: u32 = nums[0..9]
        .iter()
        .enumerate()
        .map(|(i, d)| d * (10 - i as u32))
        .sum();
    let rem1 = (sum1 * 10) % 11;
    let dv1 = if rem1 == 10 { 0 } else { rem1 };
    if nums[9] != dv1 {
        return false;
    }

    let sum2: u32 = nums[0..10]
        .iter()
        .enumerate()
        .map(|(i, d)| d * (11 - i as u32))
        .sum();
    let rem2 = (sum2 * 10) % 11;
    let dv2 = if rem2 == 10 { 0 } else { rem2 };
    nums[10] == dv2
}

fn compute_cpf_blind_hash(digits_11: &str) -> String {
    let pepper = [
        "BLIND_INDEX_PEPPER",
        "AUTH_PEPPER_SECRET",
        "DATA_ENCRYPTION_KEY",
    ]
    .iter()
    .find_map(|key| {
        std::env::var(key)
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
    });

    if let Some(secret) = pepper {
        if let Ok(mut mac) = HmacSha256::new_from_slice(secret.as_bytes()) {
            mac.update(b"b2b:responsible_cpf:v1:");
            mac.update(digits_11.as_bytes());
            return hex::encode(mac.finalize().into_bytes());
        }
    }

    let mut hasher = Sha256::new();
    hasher.update(b"b2b:responsible_cpf:v1:");
    hasher.update(digits_11.as_bytes());
    hex::encode(hasher.finalize())
}

pub fn normalize_responsible_role(raw: Option<&str>) -> Result<Option<String>, &'static str> {
    let cleaned = raw.unwrap_or("").trim().to_ascii_uppercase();
    if cleaned.is_empty() {
        return Ok(None);
    }
    if ALLOWED_RESPONSIBLE_ROLES.contains(&cleaned.as_str()) {
        Ok(Some(cleaned))
    } else {
        Err("Cargo do responsável inválido (use SOCIO_TITULAR, RESPONSAVEL_TECNICO ou GERENTE_ADMIN).")
    }
}

pub fn normalize_responsible_phone(raw: Option<&str>) -> Result<Option<String>, &'static str> {
    let cleaned = raw.unwrap_or("").trim();
    if cleaned.is_empty() {
        return Ok(None);
    }
    let digits: String = cleaned.chars().filter(|c| c.is_ascii_digit()).collect();
    if !(10..=15).contains(&digits.len()) {
        return Err("Telefone comercial do responsável inválido (informe DDD + número).");
    }
    Ok(Some(digits))
}

pub fn normalize_uf(
    raw: Option<&str>,
    err_msg: &'static str,
) -> Result<Option<String>, &'static str> {
    let cleaned = raw.unwrap_or("").trim().to_ascii_uppercase();
    if cleaned.is_empty() {
        return Ok(None);
    }
    if ALLOWED_BR_UFS.contains(&cleaned.as_str()) {
        Ok(Some(cleaned))
    } else {
        Err(err_msg)
    }
}

pub fn normalize_council_fields(
    council_type: Option<&str>,
    council_number: Option<&str>,
    council_uf: Option<&str>,
) -> Result<NormalizedCouncilFields, &'static str> {
    let ct = council_type.unwrap_or("").trim().to_ascii_uppercase();
    let cn = council_number.unwrap_or("").trim().to_ascii_uppercase();
    let uf_opt = normalize_uf(council_uf, "UF do conselho profissional inválida.")?;

    if ct.is_empty() && cn.is_empty() && uf_opt.is_none() {
        return Ok(NormalizedCouncilFields::default());
    }
    if ct.is_empty() || cn.is_empty() || uf_opt.is_none() {
        return Err("Informe tipo, número e UF do conselho profissional.");
    }
    if !ALLOWED_COUNCIL_TYPES.contains(&ct.as_str()) {
        return Err(
            "Conselho profissional inválido (use CRM, CRN, CREFITO, COREN, CRF, CRP ou CRO).",
        );
    }
    let alnum_count = cn.chars().filter(|c| c.is_ascii_alphanumeric()).count();
    if !(3..=24).contains(&cn.len())
        || alnum_count < 3
        || !cn
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '/' | '.'))
    {
        return Err("Número do registro profissional inválido.");
    }
    Ok(NormalizedCouncilFields {
        council_type: Some(ct),
        council_number: Some(cn),
        council_uf: uf_opt,
    })
}

pub fn fold_ascii_lower(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut prev_space = true;
    for ch in input.chars() {
        let mapped = match ch {
            'á' | 'à' | 'â' | 'ã' | 'ä' | 'Á' | 'À' | 'Â' | 'Ã' | 'Ä' => 'a',
            'é' | 'è' | 'ê' | 'ë' | 'É' | 'È' | 'Ê' | 'Ë' => 'e',
            'í' | 'ì' | 'î' | 'ï' | 'Í' | 'Ì' | 'Î' | 'Ï' => 'i',
            'ó' | 'ò' | 'ô' | 'õ' | 'ö' | 'Ó' | 'Ò' | 'Ô' | 'Õ' | 'Ö' => 'o',
            'ú' | 'ù' | 'û' | 'ü' | 'Ú' | 'Ù' | 'Û' | 'Ü' => 'u',
            'ç' | 'Ç' => 'c',
            'ñ' | 'Ñ' => 'n',
            c if c.is_ascii_alphanumeric() => c.to_ascii_lowercase(),
            _ => ' ',
        };
        if mapped == ' ' {
            if !prev_space {
                out.push(' ');
                prev_space = true;
            }
        } else {
            out.push(mapped);
            prev_space = false;
        }
    }
    out.trim().to_string()
}

pub fn check_protected_brand_terms(company_name: &str, program_name: &str) -> Vec<String> {
    let combined = format!(
        "{} {}",
        fold_ascii_lower(company_name),
        fold_ascii_lower(program_name)
    );
    let words: Vec<&str> = combined.split_whitespace().collect();
    let mut hits = Vec::new();

    for &w in PROTECTED_WHOLE_WORD_BRANDS {
        if words.contains(&w) {
            hits.push(format!("PROTECTED_BRAND_TERM:{}", w.to_ascii_uppercase()));
        }
    }
    for &phrase in PROTECTED_SUBSTRING_BRANDS {
        if combined.contains(phrase) {
            let tag = phrase.replace(' ', "_").to_ascii_uppercase();
            hits.push(format!("PROTECTED_BRAND_TERM:{tag}"));
        }
    }
    hits
}

pub fn is_assistencial_template(template: TemplateKey) -> bool {
    matches!(
        template,
        TemplateKey::ClinicaGeral
            | TemplateKey::NutricaoBemEstar
            | TemplateKey::FisioReabilitacao
            | TemplateKey::CuidadoSeniorHomecare
            | TemplateKey::FarmaciaAdesao
    )
}

fn cnae_digits(raw: &str) -> String {
    raw.chars().filter(|c| c.is_ascii_digit()).collect()
}

pub fn is_cnae_compatible(template: TemplateKey, record: &ReceitaCnpjRecord) -> bool {
    let mut all_cnaes = Vec::with_capacity(1 + record.cnaes_secundarios.len());
    let main = cnae_digits(&record.cnae_fiscal);
    if !main.is_empty() {
        all_cnaes.push(main);
    }
    for sec in &record.cnaes_secundarios {
        let d = cnae_digits(sec);
        if !d.is_empty() {
            all_cnaes.push(d);
        }
    }
    if all_cnaes.is_empty() {
        return false;
    }

    match template {
        TemplateKey::ClinicaGeral | TemplateKey::CuidadoSeniorHomecare | TemplateKey::Custom => {
            all_cnaes
                .iter()
                .any(|c| c.starts_with("86") || c.starts_with("87"))
        }
        TemplateKey::FarmaciaAdesao => all_cnaes
            .iter()
            .any(|c| c.starts_with("4771") || c.starts_with("86")),
        TemplateKey::NutricaoBemEstar | TemplateKey::FisioReabilitacao => all_cnaes
            .iter()
            .any(|c| c.starts_with("86") || c.starts_with("931")),
        // SAUDE_CORPORATIVA: empresas empregadoras ativas de qualquer CNAE regular ou saude
        TemplateKey::SaudeCorporativa => true,
    }
}

fn extract_significant_name_tokens(name: &str) -> Vec<String> {
    fold_ascii_lower(name)
        .split_whitespace()
        .filter(|tok| tok.len() >= 3 && !matches!(*tok, "dos" | "das" | "del" | "van" | "von"))
        .map(ToString::to_string)
        .collect()
}

fn names_match_loosely(responsible_name: &str, candidate_name: &str) -> bool {
    let resp_tokens = extract_significant_name_tokens(responsible_name);
    let cand_folded = fold_ascii_lower(candidate_name);
    let cand_tokens: Vec<&str> = cand_folded.split_whitespace().collect();
    if resp_tokens.is_empty() || cand_tokens.is_empty() {
        return false;
    }
    let first = &resp_tokens[0];
    let last = &resp_tokens[resp_tokens.len() - 1];
    if resp_tokens.len() == 1 {
        return cand_tokens.iter().any(|t| t == first);
    }
    cand_tokens.iter().any(|t| t == first) && cand_tokens.iter().any(|t| t == last)
}

fn cpf_central6_matches(central_6: &str, masked_doc: Option<&str>) -> bool {
    let Some(raw) = masked_doc else {
        return false;
    };
    let digits: String = raw.chars().filter(|c| c.is_ascii_digit()).collect();
    if digits.len() < 6 {
        return false;
    }
    digits.contains(central_6)
}

pub fn matches_responsible_in_receita(
    responsible_name: &str,
    central_6_cpf: &str,
    record: &ReceitaCnpjRecord,
) -> bool {
    if record.qsa.iter().any(|partner| {
        names_match_loosely(responsible_name, &partner.nome_socio)
            && cpf_central6_matches(central_6_cpf, partner.cpf_cnpj_socio.as_deref())
    }) {
        return true;
    }

    if record.is_mei() {
        let razao_matches_name = names_match_loosely(responsible_name, &record.razao_social);
        let razao_matches_cpf = cpf_central6_matches(central_6_cpf, Some(&record.razao_social));
        if razao_matches_name && razao_matches_cpf {
            return true;
        }
    }

    false
}

pub fn evaluate_auto_kyb(input: &AutoKybInput<'_>, lookup: &KybLookupOutcome) -> KybEvaluation {
    if !input.terms_accepted {
        return KybEvaluation::Red {
            status: StatusCode::BAD_REQUEST,
            detail: "Aceite os Termos B2B, o DPA LGPD e a Declaração Não-SaMD antes de enviar."
                .to_string(),
            flags: vec!["TERMS_NOT_ACCEPTED".to_string()],
        };
    }

    if let KybLookupOutcome::NotFound = lookup {
        return KybEvaluation::Red {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            detail: "CNPJ não encontrado na base oficial da Receita Federal. Confira os números."
                .to_string(),
            flags: vec!["CNPJ_NOT_FOUND".to_string()],
        };
    }

    if let KybLookupOutcome::Found { record, .. } = lookup {
        if !record.is_active() {
            let situacao = record.situacao_cadastral.trim();
            return KybEvaluation::Red {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                detail: format!(
                    "Cadastro bloqueado: CNPJ com situação cadastral '{situacao}' na Receita Federal."
                ),
                flags: vec![format!(
                    "CNPJ_INACTIVE:{}",
                    situacao.to_ascii_uppercase()
                )],
            };
        }
    }

    let mut flags: Vec<String> =
        check_protected_brand_terms(input.company_name, input.program_name);

    if is_assistencial_template(input.template) && !input.has_council {
        flags.push("COUNCIL_NOT_INFORMED".to_string());
    }
    if !input.auto_approve_enabled {
        flags.push("AUTO_APPROVE_DISABLED".to_string());
    }
    if !input.infra_ok {
        flags.push("INFRA_DEGRADED".to_string());
    }

    let evidence = match lookup {
        KybLookupOutcome::Unavailable(reason) => {
            flags.push("API_UNAVAILABLE".to_string());
            json!({
                "provider": "unavailable",
                "reason": reason,
                "checkedAt": Utc::now().to_rfc3339(),
            })
        }
        KybLookupOutcome::Found { record, from_cache } => {
            let age_days = record.age_days(input.today);
            if age_days.is_none_or(|d| d < 30) {
                flags.push("RECENT_CNPJ".to_string());
            }
            if !is_cnae_compatible(input.template, record) {
                flags.push("CNAE_INCOMPATIBLE".to_string());
            }
            if !matches_responsible_in_receita(input.responsible_name, &input.cpf.central_6, record)
            {
                if record.is_mei() {
                    flags.push("MEI_UNVERIFIED_CPF".to_string());
                } else {
                    flags.push("OWNER_NOT_IN_QSA".to_string());
                }
            }
            json!({
                "provider": record.provider,
                "fromCache": from_cache,
                "situacaoCadastral": record.situacao_cadastral,
                "dataInicioAtividade": record.data_inicio_atividade,
                "idadeDias": age_days,
                "cnaeFiscal": record.cnae_fiscal,
                "razaoSocialReceita": record.razao_social,
                "checkedAt": Utc::now().to_rfc3339(),
            })
        }
        KybLookupOutcome::NotFound => unreachable!(),
    };

    if flags.is_empty() {
        KybEvaluation::Green { evidence }
    } else {
        let summary_note = build_yellow_summary_note(&flags);
        KybEvaluation::Yellow {
            flags,
            summary_note,
            evidence,
        }
    }
}

fn build_yellow_summary_note(flags: &[String]) -> String {
    let mut reasons = Vec::new();
    for f in flags {
        let label = if f == "RECENT_CNPJ" {
            "CNPJ aberto há menos de 30 dias"
        } else if f.starts_with("PROTECTED_BRAND_TERM") {
            "nome com termo que requer conferência de vínculo"
        } else if f == "OWNER_NOT_IN_QSA" {
            "responsável não localizado no quadro societário (QSA)"
        } else if f == "MEI_UNVERIFIED_CPF" {
            "MEI sem dígitos verificáveis de CPF na base pública (envie CCMEI/Cartão CNPJ)"
        } else if f == "CNAE_INCOMPATIBLE" {
            "CNAE fora dos códigos padrão deste perfil"
        } else if f == "COUNCIL_NOT_INFORMED" {
            "registro no conselho profissional pendente"
        } else if f == "API_UNAVAILABLE" {
            "consulta externa temporariamente indisponível"
        } else if f == "AUTO_APPROVE_DISABLED" {
            "revisão humana habilitada no momento"
        } else {
            "verificação complementar necessária"
        };
        if !reasons.contains(&label) {
            reasons.push(label);
        }
    }
    format!(
        "Encaminhado para análise rápida da equipe Braga ({}). Envie 1 ou 2 comprovantes privados para agilizar a aprovação.",
        reasons.join("; ")
    )
}

pub fn parse_brasilapi_json(cnpj_digits: &str, body: &Value) -> Option<ReceitaCnpjRecord> {
    let razao = body.get("razao_social")?.as_str()?.trim().to_string();
    if razao.is_empty() {
        return None;
    }
    let situacao = body
        .get("descricao_situacao_cadastral")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(ToString::to_string)
        .or_else(|| {
            body.get("situacao_cadastral")
                .and_then(|v| v.as_i64())
                .map(|code| if code == 2 { "ATIVA" } else { "IRREGULAR" }.to_string())
        })?;

    let cnae_fiscal = match body.get("cnae_fiscal") {
        Some(Value::String(s)) => cnae_digits(s),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    };

    let cnaes_secundarios = body
        .get("cnaes_secundarios")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|item| match item.get("codigo") {
                    Some(Value::String(s)) => Some(cnae_digits(s)),
                    Some(Value::Number(n)) => Some(n.to_string()),
                    _ => None,
                })
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default();

    let qsa = body
        .get("qsa")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|p| {
                    let nome = p
                        .get("nome_socio")
                        .or_else(|| p.get("nome"))
                        .and_then(|v| v.as_str())?
                        .trim()
                        .to_string();
                    let doc = p
                        .get("cnpj_cpf_do_socio")
                        .or_else(|| p.get("cpf_cnpj_socio"))
                        .and_then(|v| v.as_str())
                        .map(|s| s.trim().to_string());
                    Some(QsaPartner {
                        nome_socio: nome,
                        cpf_cnpj_socio: doc,
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    Some(ReceitaCnpjRecord {
        cnpj_digits: cnpj_digits.to_string(),
        razao_social: razao,
        nome_fantasia: body
            .get("nome_fantasia")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()),
        situacao_cadastral: situacao.to_ascii_uppercase(),
        data_inicio_atividade: body
            .get("data_inicio_atividade")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        cnae_fiscal,
        cnae_fiscal_descricao: body
            .get("cnae_fiscal_descricao")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        cnaes_secundarios,
        natureza_juridica: body
            .get("natureza_juridica")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        opcao_pelo_mei: body.get("opcao_pelo_mei").and_then(|v| v.as_bool()),
        uf: body
            .get("uf")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_ascii_uppercase()),
        municipio: body
            .get("municipio")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        qsa,
        provider: "brasilapi".to_string(),
    })
}

pub fn parse_cnpjws_json(cnpj_digits: &str, body: &Value) -> Option<ReceitaCnpjRecord> {
    let razao = body.get("razao_social")?.as_str()?.trim().to_string();
    let est = body.get("estabelecimento")?;
    let situacao = est
        .get("situacao_cadastral")?
        .as_str()?
        .trim()
        .to_ascii_uppercase();
    let cnae_fiscal = est
        .get("atividade_principal")
        .and_then(|a| a.get("id"))
        .and_then(|v| v.as_str())
        .map(cnae_digits)
        .unwrap_or_default();
    let cnaes_secundarios = est
        .get("atividades_secundarias")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|item| item.get("id").and_then(|v| v.as_str()).map(cnae_digits))
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let qsa = body
        .get("socios")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|p| {
                    let nome = p.get("nome")?.as_str()?.trim().to_string();
                    let doc = p
                        .get("cpf_cnpj_socio")
                        .and_then(|v| v.as_str())
                        .map(|s| s.trim().to_string());
                    Some(QsaPartner {
                        nome_socio: nome,
                        cpf_cnpj_socio: doc,
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    let is_mei = body
        .get("simples")
        .and_then(|s| s.get("mei"))
        .and_then(|v| v.as_str())
        .map(|s| s.trim().eq_ignore_ascii_case("sim"));

    Some(ReceitaCnpjRecord {
        cnpj_digits: cnpj_digits.to_string(),
        razao_social: razao,
        nome_fantasia: est
            .get("nome_fantasia")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()),
        situacao_cadastral: situacao,
        data_inicio_atividade: est
            .get("data_inicio_atividade")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        cnae_fiscal,
        cnae_fiscal_descricao: est
            .get("atividade_principal")
            .and_then(|a| a.get("descricao"))
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        cnaes_secundarios,
        natureza_juridica: body
            .get("natureza_juridica")
            .and_then(|n| n.get("descricao"))
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        opcao_pelo_mei: is_mei,
        uf: est
            .get("estado")
            .and_then(|e| e.get("sigla"))
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_ascii_uppercase()),
        municipio: est
            .get("cidade")
            .and_then(|c| c.get("nome"))
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        qsa,
        provider: "cnpjws".to_string(),
    })
}

pub fn parse_receitaws_json(cnpj_digits: &str, body: &Value) -> Option<ReceitaCnpjRecord> {
    if body.get("status").and_then(|v| v.as_str()) != Some("OK") {
        return None;
    }
    let razao = body.get("nome")?.as_str()?.trim().to_string();
    let situacao = body.get("situacao")?.as_str()?.trim().to_ascii_uppercase();
    let abertura = body
        .get("abertura")
        .and_then(|v| v.as_str())
        .and_then(|raw| {
            NaiveDate::parse_from_str(raw.trim(), "%d/%m/%Y")
                .ok()
                .map(|d| d.format("%Y-%m-%d").to_string())
        });
    let main_entry = body
        .get("atividade_principal")
        .and_then(|v| v.as_array())
        .and_then(|arr| arr.first());
    let cnae_fiscal = main_entry
        .and_then(|a| a.get("code"))
        .and_then(|v| v.as_str())
        .map(cnae_digits)
        .unwrap_or_default();
    let cnaes_secundarios = body
        .get("atividades_secundarias")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|item| item.get("code").and_then(|v| v.as_str()).map(cnae_digits))
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let qsa = body
        .get("qsa")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|p| {
                    let nome = p.get("nome")?.as_str()?.trim().to_string();
                    Some(QsaPartner {
                        nome_socio: nome,
                        cpf_cnpj_socio: None,
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    Some(ReceitaCnpjRecord {
        cnpj_digits: cnpj_digits.to_string(),
        razao_social: razao,
        nome_fantasia: body
            .get("fantasia")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty()),
        situacao_cadastral: situacao,
        data_inicio_atividade: abertura,
        cnae_fiscal,
        cnae_fiscal_descricao: main_entry
            .and_then(|a| a.get("text"))
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        cnaes_secundarios,
        natureza_juridica: body
            .get("natureza_juridica")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        opcao_pelo_mei: None,
        uf: body
            .get("uf")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_ascii_uppercase()),
        municipio: body
            .get("municipio")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string()),
        qsa,
        provider: "receitaws".to_string(),
    })
}

pub async fn fetch_receita_with_fallback(
    cnpj_digits: &str,
    endpoints: &KybProviderEndpoints,
) -> KybLookupOutcome {
    let client = kyb_http_client();

    // 1. BrasilAPI (primaria)
    let brasil_url = format!(
        "{}/api/cnpj/v1/{cnpj_digits}",
        endpoints.brasilapi_base.trim_end_matches('/')
    );
    if let Ok(resp) = client.get(&brasil_url).send().await {
        if resp.status() == reqwest::StatusCode::NOT_FOUND {
            return KybLookupOutcome::NotFound;
        }
        if resp.status().is_success() {
            if let Ok(val) = resp.json::<Value>().await {
                if let Some(rec) = parse_brasilapi_json(cnpj_digits, &val) {
                    return KybLookupOutcome::Found {
                        record: Box::new(rec),
                        from_cache: false,
                    };
                }
            }
        }
    }

    // 2. CNPJa publica (fallback 1)
    let cnpjws_url = format!(
        "{}/cnpj/{cnpj_digits}",
        endpoints.cnpjws_base.trim_end_matches('/')
    );
    if let Ok(resp) = client.get(&cnpjws_url).send().await {
        if resp.status() == reqwest::StatusCode::NOT_FOUND {
            return KybLookupOutcome::NotFound;
        }
        if resp.status().is_success() {
            if let Ok(val) = resp.json::<Value>().await {
                if let Some(rec) = parse_cnpjws_json(cnpj_digits, &val) {
                    return KybLookupOutcome::Found {
                        record: Box::new(rec),
                        from_cache: false,
                    };
                }
            }
        }
    }

    // 3. ReceitaWS publica (fallback 2)
    let rws_url = format!(
        "{}/v1/cnpj/{cnpj_digits}",
        endpoints.receitaws_base.trim_end_matches('/')
    );
    if let Ok(resp) = client.get(&rws_url).send().await {
        if resp.status().is_success() {
            if let Ok(val) = resp.json::<Value>().await {
                if let Some(rec) = parse_receitaws_json(cnpj_digits, &val) {
                    return KybLookupOutcome::Found {
                        record: Box::new(rec),
                        from_cache: false,
                    };
                }
            }
        }
    }

    KybLookupOutcome::Unavailable(
        "Receita Federal/APIs publicas indisponiveis no momento.".to_string(),
    )
}

pub async fn lookup_cnpj_cached(
    pool: &PgPool,
    cnpj_digits: &str,
    endpoints: &KybProviderEndpoints,
) -> KybLookupOutcome {
    if let Ok(Some(row)) = sqlx::query(
        "SELECT payload FROM cnpj_lookup_cache WHERE cnpj_digits = $1 AND expires_at > NOW()",
    )
    .bind(cnpj_digits)
    .fetch_optional(pool)
    .await
    {
        let payload: Value = row.get("payload");
        if let Ok(rec) = serde_json::from_value::<ReceitaCnpjRecord>(payload) {
            return KybLookupOutcome::Found {
                record: Box::new(rec),
                from_cache: true,
            };
        }
    }

    let fresh = fetch_receita_with_fallback(cnpj_digits, endpoints).await;
    if let KybLookupOutcome::Found { ref record, .. } = fresh {
        if let Ok(payload_val) = serde_json::to_value(record.as_ref()) {
            let _ = sqlx::query(
                "INSERT INTO cnpj_lookup_cache (cnpj_digits, provider, payload, fetched_at, expires_at) \
                 VALUES ($1, $2, $3, NOW(), NOW() + interval '24 hours') \
                 ON CONFLICT (cnpj_digits) DO UPDATE \
                    SET provider = EXCLUDED.provider, \
                        payload = EXCLUDED.payload, \
                        fetched_at = NOW(), \
                        expires_at = NOW() + interval '24 hours'",
            )
            .bind(cnpj_digits)
            .bind(&record.provider)
            .bind(payload_val)
            .execute(pool)
            .await;
        }
    }
    fresh
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        extract::Path as AxumPath, http::StatusCode as AxStatus, routing::get, Json, Router,
    };

    fn make_active_record(
        cnae: &str,
        days_ago: i64,
        qsa: Vec<QsaPartner>,
        mei: bool,
    ) -> ReceitaCnpjRecord {
        let start = Utc::now().date_naive() - chrono::Duration::days(days_ago);
        ReceitaCnpjRecord {
            cnpj_digits: "12345678000195".to_string(),
            razao_social: "CLINICA CARDIO PRIME LTDA".to_string(),
            nome_fantasia: Some("Cardio Prime".to_string()),
            situacao_cadastral: "ATIVA".to_string(),
            data_inicio_atividade: Some(start.format("%Y-%m-%d").to_string()),
            cnae_fiscal: cnae.to_string(),
            cnae_fiscal_descricao: Some("Atividade medica ambulatorial".to_string()),
            cnaes_secundarios: vec![],
            natureza_juridica: if mei {
                Some("213-5 - Empresário (Individual) MEI".to_string())
            } else {
                Some("206-2 - Sociedade Empresária Limitada".to_string())
            },
            opcao_pelo_mei: Some(mei),
            uf: Some("SP".to_string()),
            municipio: Some("Sao Paulo".to_string()),
            qsa,
            provider: "brasilapi".to_string(),
        }
    }

    #[test]
    fn mod11_cnpj_e_cpf_validam_e_mascaram_sem_expor_cpf_puro() {
        assert!(verify_cnpj_mod11("12345678000195"));
        assert!(!verify_cnpj_mod11("12345678000196"));
        assert!(!verify_cnpj_mod11("11111111111111"));

        let cpf = normalize_and_hash_cpf(Some("529.982.247-25"), true)
            .unwrap()
            .unwrap();
        assert_eq!(cpf.central_6, "982247");
        assert_eq!(cpf.masked, "***.982.247-**");
        assert_eq!(cpf.hash_hex.len(), 64);
        assert!(!cpf.masked.contains("529"));
        assert!(normalize_and_hash_cpf(Some("529.982.247-00"), true).is_err());
        assert!(normalize_and_hash_cpf(Some("111.111.111-11"), true).is_err());
    }

    #[test]
    fn blacklist_protege_hospital_sus_braga_sem_falso_positivo_em_consultorio() {
        assert!(!check_protected_brand_terms("Hospital Sao Lucas", "Cardio").is_empty());
        assert!(!check_protected_brand_terms("Clinica Norte", "Programa SUS").is_empty());
        assert!(!check_protected_brand_terms("Clinica Braga Ltda", "Saude").is_empty());
        assert!(check_protected_brand_terms(
            "Consultorio Silva Sustentavel Ltda",
            "Cuidado Cardio"
        )
        .is_empty());
    }

    #[test]
    fn semaforo_kyb_aprova_verde_medico_qsa_e_direciona_amarelo_e_vermelho() {
        let today = Utc::now().date_naive();
        let cpf = normalize_and_hash_cpf(Some("529.982.247-25"), true)
            .unwrap()
            .unwrap();

        let rec_green = make_active_record(
            "8630503",
            180,
            vec![QsaPartner {
                nome_socio: "JOAO CARLOS SILVA".to_string(),
                cpf_cnpj_socio: Some("***982247**".to_string()),
            }],
            false,
        );
        let green = evaluate_auto_kyb(
            &AutoKybInput {
                template: TemplateKey::ClinicaGeral,
                company_name: "Clinica Cardio Prime Ltda",
                program_name: "Cuidado Cardio",
                responsible_name: "Dr. Joao Carlos da Silva",
                cpf: &cpf,
                has_council: true,
                terms_accepted: true,
                auto_approve_enabled: true,
                infra_ok: true,
                today,
            },
            &KybLookupOutcome::Found {
                record: Box::new(rec_green),
                from_cache: false,
            },
        );
        assert!(matches!(green, KybEvaluation::Green { .. }));

        // Caso < 30 dias + nome protegido => AMARELO com flags explicitas
        let rec_recent = make_active_record(
            "8630503",
            10,
            vec![QsaPartner {
                nome_socio: "JOAO CARLOS SILVA".to_string(),
                cpf_cnpj_socio: Some("***982247**".to_string()),
            }],
            false,
        );
        let yellow = evaluate_auto_kyb(
            &AutoKybInput {
                template: TemplateKey::ClinicaGeral,
                company_name: "Hospital Coracao Ltda",
                program_name: "Cuidado Cardio",
                responsible_name: "Joao Carlos Silva",
                cpf: &cpf,
                has_council: true,
                terms_accepted: true,
                auto_approve_enabled: true,
                infra_ok: true,
                today,
            },
            &KybLookupOutcome::Found {
                record: Box::new(rec_recent),
                from_cache: false,
            },
        );
        match yellow {
            KybEvaluation::Yellow { flags, .. } => {
                assert!(flags.iter().any(|f| f == "RECENT_CNPJ"));
                assert!(flags.iter().any(|f| f == "PROTECTED_BRAND_TERM:HOSPITAL"));
            }
            other => panic!("esperava Yellow, veio {other:?}"),
        }

        // Caso CNPJ INAPTA => VERMELHO bloqueado sem poluir fila manual
        let mut rec_inapta = make_active_record("8630503", 180, vec![], false);
        rec_inapta.situacao_cadastral = "INAPTA".to_string();
        let red = evaluate_auto_kyb(
            &AutoKybInput {
                template: TemplateKey::ClinicaGeral,
                company_name: "Clinica Cardio Prime",
                program_name: "Cuidado Cardio",
                responsible_name: "Joao Carlos Silva",
                cpf: &cpf,
                has_council: true,
                terms_accepted: true,
                auto_approve_enabled: true,
                infra_ok: true,
                today,
            },
            &KybLookupOutcome::Found {
                record: Box::new(rec_inapta),
                from_cache: false,
            },
        );
        assert!(matches!(
            red,
            KybEvaluation::Red {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                ..
            }
        ));
    }

    #[tokio::test]
    async fn consulta_receita_faz_fallback_de_brasilapi_503_para_cnpjws_200() {
        async fn brasilapi_down(AxumPath(_cnpj): AxumPath<String>) -> AxStatus {
            AxStatus::SERVICE_UNAVAILABLE
        }
        async fn cnpjws_ok(AxumPath(_cnpj): AxumPath<String>) -> Json<Value> {
            Json(json!({
                "razao_social": "CLINICA ESPERANCA LTDA",
                "natureza_juridica": { "descricao": "Sociedade Empresária Limitada" },
                "estabelecimento": {
                    "situacao_cadastral": "Ativa",
                    "data_inicio_atividade": "2022-04-10",
                    "nome_fantasia": "Clinica Esperanca",
                    "atividade_principal": { "id": "8630503", "descricao": "Ambulatorio" },
                    "atividades_secundarias": [],
                    "estado": { "sigla": "SP" },
                    "cidade": { "nome": "Sao Paulo" }
                },
                "socios": [
                    { "nome": "MARIA CLARA MENDES", "cpf_cnpj_socio": "***982247**" }
                ]
            }))
        }

        let app = Router::new()
            .route("/api/cnpj/v1/:cnpj", get(brasilapi_down))
            .route("/cnpj/:cnpj", get(cnpjws_ok));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let task = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });

        let base = format!("http://{addr}");
        let endpoints = KybProviderEndpoints {
            brasilapi_base: base.clone(),
            cnpjws_base: base.clone(),
            receitaws_base: base,
        };
        let outcome = fetch_receita_with_fallback("12345678000195", &endpoints).await;
        task.abort();

        match outcome {
            KybLookupOutcome::Found { record, .. } => {
                assert_eq!(record.provider, "cnpjws");
                assert_eq!(record.razao_social, "CLINICA ESPERANCA LTDA");
                assert!(record.is_active());
                assert_eq!(record.qsa.len(), 1);
            }
            other => panic!("esperava fallback bem-sucedido, veio {other:?}"),
        }
    }
}
