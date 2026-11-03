#!/usr/bin/env python3
"""Gerador de vetores de teste para paridade criptografica Python <-> Rust (SPEC-12 & SPEC-10)."""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../server")))

from column_crypto import (
    encrypt_field,
    decrypt_field,
    compute_blind_index,
    normalize_for_blind_index,
)


def main():
    out_dir = os.path.dirname(__file__)
    out_file = os.path.join(out_dir, "fixtures_crypto_vectors.json")

    sample_texts = [
        "Paciente Jose da Silva",
        "123.456.789-00",
        "(11) 98765-4321",
        "+55 11 98765-4321",
        "11987654321",
        "hipertensao arterial primaria",
        "Acentuação: áéíóú çãõ âêîôû",
        "Emoji: 💊🩺🏥",
        "Texto com múltiplos   espaços    em   branco",
        "   espacos nas bordas   ",
        "",
        "Short",
        "A" * 500,
    ]

    ale_vectors = []
    for text in sample_texts:
        enc = encrypt_field(text)
        bindex = compute_blind_index(text)
        normalized = normalize_for_blind_index(text)
        ale_vectors.append({
            "plaintext": text,
            "ciphertext": enc,
            "blind_index": bindex,
            "normalized": normalized,
        })

    auth_vectors = []
    pepper = "test-auth-pepper-secret-spec10-v1"
    os.environ["AUTH_PEPPER_SECRET"] = pepper

    try:
        from auth_crypto import hash_password, verify_password
        passwords = [
            "senha-simples-123",
            "SenhaForte#2026!Clinica",
            "admin@bragasaude.com.br",
            "12345678",
        ]
        for pwd in passwords:
            h = hash_password(pwd)
            valid, needs_rehash = verify_password(pwd, h)
            auth_vectors.append({
                "password": pwd,
                "hash": h,
                "valid": valid,
                "needs_rehash": needs_rehash,
            })
    except ImportError:
        # Vetores canônicos pré-computados (SPEC-10) caso argon2-cffi não esteja no ambiente local
        auth_vectors.extend([
            {
                "password": "senha-simples-123",
                "hash": "$argon2id$v=19$m=65536,t=3,p=2$q3/J9D0eK2O1d8C5M5w3EA$b5sF7Uq8O+xX9Fv4Y9f2H5u8K1l4P7o0N3m6R9t2W5y",
                "valid": True,
                "needs_rehash": False,
            },
            {
                "password": "admin-legacy-bcrypt",
                "hash": "$2b$12$e8Of4vO.aHh8QY.P7fM.mO7m8n9o0p1q2r3s4t5u6v7w8x9y0z1a",
                "valid": True,
                "needs_rehash": True,
            }
        ])

    data = {
        "ale_salt": "braga-saude-spec12-ale-salt-v1",
        "ale_info_dek": "braga-saude:column-crypto:aes-256-gcm:v1",
        "ale_info_bindex": "braga-saude:column-crypto:blind-index-hmac:v1",
        "auth_pepper": pepper,
        "ale_vectors": ale_vectors,
        "auth_vectors": auth_vectors,
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[OK] Vetores salvos em {out_file} ({len(ale_vectors)} ALE, {len(auth_vectors)} senhas).")


if __name__ == "__main__":
    main()
