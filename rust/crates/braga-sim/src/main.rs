use std::path::PathBuf;
use std::time::Instant;

use braga_contracts::BarcodeMedication;
use braga_crypto::{
    compute_blind_index, decrypt_field, encrypt_field, hash_password, normalize_for_blind_index,
    verify_password, ColumnTamperError,
};
use braga_identity::{require_owner, IdentityError};
use braga_limiter::StrictRateLimiter;
use clap::Parser;
use serde::{Deserialize, Serialize};

#[derive(Parser, Debug)]
#[command(
    author,
    version,
    about = "BragaSaude Local Simulation Harness (Offline)"
)]
struct Args {
    #[arg(short, long, default_value_t = 0)]
    phase: u8,

    #[arg(short, long)]
    report: Option<PathBuf>,
}

#[derive(Serialize, Deserialize)]
struct SimulationReport {
    phase: u8,
    status: String,
    duration_ms: u128,
    cases_executed: usize,
    cases_passed: usize,
    divergences: Vec<String>,
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    let start = Instant::now();

    let mut executed = 0;
    let mut passed = 0;
    let mut divergences = Vec::new();

    println!(
        "[SIMULADOR LOCAL] Executando simulacao offline para Fase {}",
        args.phase
    );

    // -----------------------------------------------------------------------
    // Casos Fase 0: Criptografia, ALE, Blind Index e Senhas
    // -----------------------------------------------------------------------
    executed += 1;
    // Caso 0.1: Cifragem e decifragem simples
    match encrypt_field(Some("teste-paciente-123"), None) {
        Ok(Some(ct)) => {
            if ct.starts_with("enc:v1:") {
                match decrypt_field(Some(&ct), None) {
                    Ok(Some(pt)) if pt == "teste-paciente-123" => passed += 1,
                    Ok(other) => divergences.push(format!("Decifragem incorreta: {:?}", other)),
                    Err(e) => divergences.push(format!("Erro na decifragem: {e}")),
                }
            } else {
                divergences.push("Payload nao comecou com enc:v1:".to_string());
            }
        }
        Ok(None) => divergences.push("Cifragem retornou None".to_string()),
        Err(e) => divergences.push(format!("Erro na cifragem: {e}")),
    }

    executed += 1;
    // Caso 0.2: Retrocompatibilidade (lazy decrypt de dado em texto claro)
    match decrypt_field(Some("dado-legado-sem-cifragem"), None) {
        Ok(Some(pt)) if pt == "dado-legado-sem-cifragem" => passed += 1,
        other => divergences.push(format!("Lazy decrypt falhou: {:?}", other)),
    }

    executed += 1;
    // Caso 0.3: Detecção de adulteração de tag (ColumnTamperError)
    let fake_tampered = "enc:v1:QUJDREVGR0hJSktM:QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY=";
    match decrypt_field(Some(fake_tampered), None) {
        Err(ColumnTamperError::InvalidTag) => passed += 1,
        other => divergences.push(format!("Esperado InvalidTag, obtido: {:?}", other)),
    }

    executed += 1;
    // Caso 0.4: Normalização telefônica de Blind Index
    let b1 = normalize_for_blind_index("(11) 98888-7777", None).unwrap_or_default();
    let b2 = normalize_for_blind_index("11988887777", None).unwrap_or_default();
    if b1 == "11988887777" && b2 == "11988887777" {
        passed += 1;
    } else {
        divergences.push(format!(
            "Blind index normalizacao telefone falhou: {b1} != {b2}"
        ));
    }

    executed += 1;
    // Caso 0.5: Senhas - Argon2id + Pepper
    let pepper = b"test-pepper-secret-spec10";
    match hash_password("senha-admin-forte-123", pepper) {
        Ok(hash) => {
            let (valid, needs_rehash) =
                verify_password("senha-admin-forte-123", Some(&hash), pepper);
            let (invalid, _) = verify_password("senha-errada", Some(&hash), pepper);
            if valid && !needs_rehash && !invalid {
                passed += 1;
            } else {
                divergences.push("Verificacao Argon2id falhou".to_string());
            }
        }
        Err(e) => divergences.push(format!("Erro hash password: {e}")),
    }

    executed += 1;
    // Caso 0.6: Cálculo do Blind Index
    match compute_blind_index(Some("Teste Blind Index"), None, None) {
        Ok(Some(bi)) if bi.len() == 32 => passed += 1,
        other => divergences.push(format!("Blind index computation falhou: {:?}", other)),
    }

    // -----------------------------------------------------------------------
    // Casos Fase 1: Rate Limiter e Bloqueio de IP
    // -----------------------------------------------------------------------
    if args.phase >= 1 {
        executed += 1;
        let limiter = StrictRateLimiter::new();
        let test_ip = "192.168.1.100";

        // Registra 3 infrações para disparar o bloqueio de 24h
        limiter
            .record_violation(test_ip, "Infracao teste 1", 1, None)
            .await;
        limiter
            .record_violation(test_ip, "Infracao teste 2", 1, None)
            .await;
        let blocked = limiter
            .record_violation(test_ip, "Infracao teste 3", 1, None)
            .await;

        let (is_blocked, rem) = limiter.is_ip_blocked(test_ip).await;
        if blocked && is_blocked && rem > 86000 {
            passed += 1;
        } else {
            divergences.push(format!(
                "Rate limiter bloqueio de 24h falhou: blocked={blocked}, is_blocked={is_blocked}"
            ));
        }
    }

    // -----------------------------------------------------------------------
    // Casos Fase 2: Leituras, Validação EAN-13, Cache Moka e Autorização
    // -----------------------------------------------------------------------
    if args.phase >= 2 {
        executed += 1;
        // Caso 2.1: Validação de EAN-13 numérico
        let valid_ean = "7896004715506";
        let invalid_eans = ["123", "789600471550", "78960047155060", "789600471550A"];
        let valid_ok = valid_ean.len() == 13 && valid_ean.chars().all(|c| c.is_ascii_digit());
        let invalids_ok = invalid_eans
            .iter()
            .all(|e| e.len() != 13 || !e.chars().all(|c| c.is_ascii_digit()));
        if valid_ok && invalids_ok {
            passed += 1;
        } else {
            divergences.push("Validação de formato EAN-13 falhou".to_string());
        }

        executed += 1;
        // Caso 2.2: Cache em memória Moka para Catálogo ANVISA
        let cache: moka::future::Cache<String, BarcodeMedication> = moka::future::Cache::builder()
            .max_capacity(100)
            .time_to_live(std::time::Duration::from_secs(300))
            .build();

        let sample_med = BarcodeMedication {
            ean_barcode: "7896004715506".to_string(),
            name: "Losartana Potassica 50mg".to_string(),
            active_principle: Some("Losartana Potassica".to_string()),
            concentration: Some("50 mg".to_string()),
            pharmaceutical_form: Some("Comprimido".to_string()),
            manufacturer: Some("EMS".to_string()),
            farmacia_popular_eligible: Some(true),
            anvisa_registration_number: None,
            source_name: "Farmacia Popular / ANVISA".to_string(),
            source_url: None,
            source_checked_at: "2026-09-23T00:00:00Z".to_string(),
        };

        // Miss antes de inserir
        let before_hit = cache.get("7896004715506").await.is_none();
        cache
            .insert("7896004715506".to_string(), sample_med.clone())
            .await;
        // Hit após inserir
        let after_hit = cache.get("7896004715506").await == Some(sample_med);

        if before_hit && after_hit {
            passed += 1;
        } else {
            divergences.push("Cache Moka miss/hit falhou".to_string());
        }

        executed += 1;
        // Caso 2.3: Autorização de recurso require_owner
        let uid1 = "usuario-titular-001";
        let uid2 = "usuario-intruso-002";
        let owner_ok = require_owner(uid1, uid1).is_ok();
        let intruder_err = matches!(
            require_owner(uid2, uid1),
            Err(IdentityError::ResourceForbidden)
        );
        if owner_ok && intruder_err {
            passed += 1;
        } else {
            divergences.push("require_owner autorização falhou".to_string());
        }
    }

    let duration_ms = start.elapsed().as_millis();
    let status = if divergences.is_empty() && passed == executed {
        "PASS".to_string()
    } else {
        "FAIL".to_string()
    };

    let report = SimulationReport {
        phase: args.phase,
        status: status.clone(),
        duration_ms,
        cases_executed: executed,
        cases_passed: passed,
        divergences,
    };

    println!(
        "[SIMULADOR LOCAL] Status: {} (Executados: {}, Passaram: {}, Tempo: {}ms)",
        report.status, report.cases_executed, report.cases_passed, report.duration_ms
    );

    if let Some(path) = args.report {
        if let Ok(json) = serde_json::to_string_pretty(&report) {
            let _ = std::fs::write(&path, json);
            println!("[SIMULADOR LOCAL] Relatorio salvo em {:?}", path);
        }
    }

    if status != "PASS" {
        std::process::exit(1);
    }
}
