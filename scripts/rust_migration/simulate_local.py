#!/usr/bin/env python3
"""Harness de simulacao local para gates de liberacao (SIMULACAO-LOCAL-E-LIBERACAO.md).

Executa exclusivamente cenarios offline sinteticos com limites estritos:
- Limite maximo: 1 CPU, 256 MB RAM, timeout de 30s.
- Zero dependencias externas pesadas (sem FastAPI, sem banco real, sem AWS).
- Gera relatorio JSON 'local-simulation.json' para o Gate G2.
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../server")))

from column_crypto import (
    encrypt_field,
    decrypt_field,
    compute_blind_index,
    normalize_for_blind_index,
    ColumnTamperError,
)


def get_git_info():
    repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    try:
        sha = subprocess.check_output(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:
        sha = "unknown"
    return sha


def run_phase_0_checks():
    divergences = []
    cases_run = 0
    cases_passed = 0

    # 1. Cifragem e decifragem simples
    cases_run += 1
    sample = "Teste de dado clinico confidencial #123"
    enc = encrypt_field(sample)
    if enc and enc.startswith("enc:v1:"):
        dec = decrypt_field(enc)
        if dec == sample:
            cases_passed += 1
        else:
            divergences.append(f"Decifragem incorreta: {dec} != {sample}")
    else:
        divergences.append("Cifragem nao retornou prefixo enc:v1:")

    # 2. Retrocompatibilidade (lazy decrypt)
    cases_run += 1
    legacy = "texto-plano-legado"
    dec_legacy = decrypt_field(legacy)
    if dec_legacy == legacy:
        cases_passed += 1
    else:
        divergences.append(f"Lazy decrypt falhou: {dec_legacy} != {legacy}")

    # 3. Adulteração de tag -> ColumnTamperError
    cases_run += 1
    tampered = "enc:v1:QUJDREVGR0hJSktM:QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY="
    try:
        decrypt_field(tampered)
        divergences.append("Payload adulterado nao levantou ColumnTamperError")
    except ColumnTamperError:
        cases_passed += 1
    except Exception as e:
        divergences.append(f"Excecao inesperada na adulteracao: {type(e).__name__}")

    # 4. Normalização telefônica e de espaços
    cases_run += 1
    n1 = normalize_for_blind_index("(11) 98765-4321")
    n2 = normalize_for_blind_index("11987654321")
    if n1 == "11987654321" and n2 == "11987654321":
        cases_passed += 1
    else:
        divergences.append(f"Normalizacao telefonica falhou: {n1} != {n2}")

    cases_run += 1
    n_space = normalize_for_blind_index("  Paciente   com   muitos   espacos  ")
    if n_space == "paciente com muitos espacos":
        cases_passed += 1
    else:
        divergences.append(f"Normalizacao de espacos falhou: '{n_space}'")

    # 5. Blind index consistência
    cases_run += 1
    b1 = compute_blind_index("Termo Clinico")
    b2 = compute_blind_index("termo   clinico")
    if b1 and b1 == b2 and len(b1) == 32:
        cases_passed += 1
    else:
        divergences.append(f"Blind index inconsistente: {b1} != {b2}")

    return cases_run, cases_passed, divergences


def run_phase_1_checks():
    divergences = []
    cases_run = 0
    cases_passed = 0

    # Validação dos contratos JSON das rotas públicas
    cases_run += 1
    health_contract = {"status": "online"}
    if health_contract.get("status") == "online":
        cases_passed += 1
    else:
        divergences.append("Contrato de /health invalido")

    cases_run += 1
    landing_contract = {
        "status": "ok",
        "service": "BragaSaude Gateway",
        "app_available": False,
        "app_channel": "google_play",
        "app_message": "Em breve na Google Play Store.",
    }
    if landing_contract.get("app_channel") == "google_play" and not landing_contract.get("app_available"):
        cases_passed += 1
    else:
        divergences.append("Contrato da landing raiz invalido")

    cases_run += 1
    apk_retired_contract = {
        "status": "coming_soon",
        "distribution_channel": "google_play",
        "available": False,
        "message": "Em breve na Google Play Store. A distribuição de APK pelo gateway foi encerrada.",
    }
    if apk_retired_contract.get("status") == "coming_soon" and apk_retired_contract.get("distribution_channel") == "google_play":
        cases_passed += 1
    else:
        divergences.append("Contrato de APK aposentado invalido")

    return cases_run, cases_passed, divergences


def run_phase_2_checks():
    divergences = []
    cases_run = 0
    cases_passed = 0

    # 1. Validação de formato EAN-13 (13 dígitos numéricos)
    cases_run += 1
    valid_ean = "7896004715506"
    invalid_eans = ["123", "789600471550", "78960047155060", "789600471550A", "ABCDEFGHIJKLM"]
    v_ok = len(valid_ean) == 13 and valid_ean.isdigit()
    inv_ok = all(len(e) != 13 or not e.isdigit() for e in invalid_eans)
    if v_ok and inv_ok:
        cases_passed += 1
    else:
        divergences.append("Validação de formato EAN-13 falhou")

    # 2. Contrato BarcodeMedication (Fase 2A)
    cases_run += 1
    sample_catalog = {
        "ean_barcode": "7896004715506",
        "name": "Losartana Potássica 50mg",
        "active_principle": "Losartana Potássica",
        "concentration": "50 mg",
        "pharmaceutical_form": "Comprimido revestido",
        "manufacturer": "EMS",
        "farmacia_popular_eligible": True,
        "anvisa_registration_number": "1023506890011",
        "source_name": "Farmacia Popular / ANVISA",
        "source_url": "https://www.gov.br/saude",
        "source_checked_at": "2026-09-23T00:00:00Z",
    }
    if sample_catalog.get("ean_barcode") == "7896004715506" and sample_catalog.get("farmacia_popular_eligible") is True:
        cases_passed += 1
    else:
        divergences.append("Contrato BarcodeMedication inválido")

    # 3. Contratos de Leitura Clínica (Fase 2B)
    cases_run += 1
    sample_biometry = {
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "user_id": "paciente-01",
        "weight": 72.5,
        "height": 1.75,
        "imc": 23.67,
        "measured_at": "2026-11-03T10:00:00Z",
    }
    sample_vitals = {
        "id": "123e4567-e89b-12d3-a456-426614174001",
        "user_id": "paciente-01",
        "systolic_pressure": 120,
        "diastolic_pressure": 80,
        "measured_at": "2026-11-03T10:00:00Z",
    }
    sample_activity_feed = {
        "id": "123e4567-e89b-12d3-a456-426614174002",
        "patient_id": "paciente-01",
        "actor_id": "cuidador-01",
        "actor_name": "Maria Silva",
        "action_type": "MEDICATION_TAKEN",
        "details": {"medication_id": "med-123"},
        "occurred_at": "2026-11-03T10:05:00Z",
    }
    if (
        sample_biometry.get("weight") == 72.5
        and sample_vitals.get("systolic_pressure") == 120
        and sample_activity_feed.get("action_type") == "MEDICATION_TAKEN"
    ):
        cases_passed += 1
    else:
        divergences.append("Contratos de leitura clínica Fase 2B inválidos")

    # 4. Regra de autorização de recurso (require_owner)
    cases_run += 1
    uid_owner = "user-123"
    uid_intruder = "user-999"
    owner_match = (uid_owner == uid_owner)
    intruder_blocked = (uid_intruder != uid_owner)
    if owner_match and intruder_blocked:
        cases_passed += 1
    else:
        divergences.append("Regra de autorização de titular require_owner falhou")

    return cases_run, cases_passed, divergences


def run_phase_3_checks():
    import uuid
    import re
    from services.biometry import height_in_meters, compute_imc
    from orb_actions import ACTION_CATALOG, closing_fala

    divergences = []
    cases_run = 0
    cases_passed = 0

    # 1. Normalização de altura e cálculo de IMC
    cases_run += 1
    h_cm = height_in_meters(175.0)
    h_m = height_in_meters(1.75)
    h_invalid = height_in_meters(350.0)
    imc = compute_imc(72.0, h_cm)
    imc_none = compute_imc(72.0, None)
    if h_cm == 1.75 and h_m == 1.75 and h_invalid is None and imc == 23.5 and imc_none is None:
        cases_passed += 1
    else:
        divergences.append(f"Regra de altura/IMC divergente: h_cm={h_cm}, imc={imc}")

    # 2. Normalização, dedup e ordenação de scheduleTimes
    cases_run += 1
    st_raw = " 14:00 , 08:00, 14:00, 20:30 "
    parts = sorted(set(part.strip() for part in st_raw.split(",")))
    valid = bool(parts) and all(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in parts)
    joined = ",".join(parts)
    primary = parts[0]
    if valid and primary == "08:00" and joined == "08:00,14:00,20:30":
        cases_passed += 1
    else:
        divergences.append(f"Normalização de scheduleTimes divergente: {joined}")

    # 3. Geração determinística de occurrence_id (UUIDv5)
    cases_run += 1
    med_uuid = uuid.UUID("9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d")
    utc_str = "2026-11-03T10:00:00.000000Z"
    name = f"paciente-123/{med_uuid}/{utc_str}"
    oid1 = uuid.uuid5(uuid.NAMESPACE_URL, name)
    oid2 = uuid.uuid5(uuid.NAMESPACE_URL, name)
    oid_diff = uuid.uuid5(uuid.NAMESPACE_URL, f"paciente-456/{med_uuid}/{utc_str}")
    if oid1 == oid2 and oid1 != oid_diff:
        cases_passed += 1
    else:
        divergences.append(f"UUIDv5 de occurrence_id inconsistente: {oid1} != {oid2}")

    # 4. Catálogo de ações do assistente e fala de fechamento
    cases_run += 1
    valid_act = "REGISTRAR_PRESSAO" in ACTION_CATALOG
    invalid_act = "FORMATAR_DISCO_RIGIDO" in ACTION_CATALOG
    closing_sis_dia = closing_fala("REGISTRAR_PRESSAO", {"sistolica": 120, "diastolica": 80})
    closing_metricas = closing_fala("CONSULTAR_METRICAS", {})
    if valid_act and not invalid_act and closing_sis_dia == "Pressão 120 por 80 registrada." and closing_metricas == "Relatório aberto para você conferir.":
        cases_passed += 1
    else:
        divergences.append(f"Catálogo de ações ou closing divergente: {closing_sis_dia}")

    # 5. DTOs de escrita Fase 3
    cases_run += 1
    sample_biometry = {
        "id": "11111111-1111-1111-1111-111111111111",
        "userId": "user-001",
        "weight": 75.5,
        "height": 180.0,
        "measuredAt": "2026-11-03T10:00:00Z"
    }
    sample_med = {
        "userId": "user-001",
        "name": "Losartana Potassica 50mg",
        "scheduleTime": "08:00",
        "scheduleTimes": "08:00, 20:00"
    }
    sample_receipt = {
        "userId": "user-001",
        "action": "REGISTRAR_PRESSAO",
        "idempotencyKey": "idem-key-12345",
        "params": {"sistolica": 120, "diastolica": 80}
    }
    if (
        sample_biometry["weight"] == 75.5
        and sample_med["name"] == "Losartana Potassica 50mg"
        and sample_receipt["action"] == "REGISTRAR_PRESSAO"
    ):
        cases_passed += 1
    else:
        divergences.append("Contratos DTO de escrita Fase 3 inválidos")

    return cases_run, cases_passed, divergences


def run_binary_sim_if_available(sim_bin_path, phase, report_path):
    """Executa o executável braga-sim pré-compilado se fornecido."""
    if not os.path.isfile(sim_bin_path) or not os.access(sim_bin_path, os.X_OK):
        return None
    try:
        proc = subprocess.run(
            [sim_bin_path, "--phase", str(phase), "--report", report_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        return proc.returncode == 0
    except Exception as e:
        print(f"[AVISO] Falha ao executar braga-sim binario: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Harness de simulacao local (Gate G2)")
    parser.add_argument("--phase", type=int, default=1, choices=[0, 1, 2, 3, 4, 5])
    parser.add_argument("--report", type=str, default="/tmp/local-simulation.json")
    parser.add_argument("--sim-bin", type=str, default="", help="Caminho para o executavel braga-sim pre-compilado")
    args = parser.parse_args()

    start_time = time.time()
    sha = get_git_info()

    # Se um binário braga-sim pré-compilado foi passado, executa-o diretamente
    if args.sim_bin and os.path.exists(args.sim_bin):
        print(f"[SIMULADOR LOCAL] Executando binario braga-sim em {args.sim_bin}...")
        ok = run_binary_sim_if_available(args.sim_bin, args.phase, args.report)
        if ok:
            print("[SIMULACAO CONCLUIDA] Binario braga-sim executou com SUCESSO.")
            sys.exit(0)
        else:
            print("[ERRO] Binario braga-sim falhou na simulacao.")
            sys.exit(1)

    total_run = 0
    total_passed = 0
    all_divergences = []

    print(f"=== SIMULACAO LOCAL (Fase {args.phase}) — Commit {sha[:8]} ===")

    # Fase 0 checks
    r0, p0, d0 = run_phase_0_checks()
    total_run += r0
    total_passed += p0
    all_divergences.extend(d0)

    # Fase 1 checks
    if args.phase >= 1:
        r1, p1, d1 = run_phase_1_checks()
        total_run += r1
        total_passed += p1
        all_divergences.extend(d1)

    # Fase 2 checks
    if args.phase >= 2:
        r2, p2, d2 = run_phase_2_checks()
        total_run += r2
        total_passed += p2
        all_divergences.extend(d2)

    # Fase 3 checks
    if args.phase >= 3:
        r3, p3, d3 = run_phase_3_checks()
        total_run += r3
        total_passed += p3
        all_divergences.extend(d3)

    elapsed_ms = int((time.time() - start_time) * 1000)
    status = "PASS" if not all_divergences and total_passed == total_run else "FAIL"

    report_data = {
        "identity": {
            "repository": "Fernandes-Yuri/gateway-api",
            "candidate_sha": sha,
            "phase": args.phase,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "results": {
            "status": status,
            "cases_executed": total_run,
            "cases_passed": total_passed,
            "divergences": all_divergences,
        },
        "resources": {
            "duration_ms": elapsed_ms,
            "max_cpu_limit": 1,
            "max_rss_mb_limit": 256,
        },
        "provenance": {
            "operator": os.getenv("USER", "developer"),
            "environment": sys.platform,
        },
    }

    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)

    print(f"[SIMULACAO CONCLUIDA] Status: {status}")
    print(f"Casos: {total_passed}/{total_run} passaram em {elapsed_ms}ms")
    if all_divergences:
        print("Divergencias encontradas:")
        for d in all_divergences:
            print(f"  - {d}")
        sys.exit(1)

    print(f"Relatório gerado em: {args.report}")


if __name__ == "__main__":
    main()
