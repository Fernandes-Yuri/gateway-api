"""Migração do histórico de peso: vital_signs -> biometrics (PR2, passo 2.5).

Até o passo 2.4, o peso confirmado no WhatsApp era gravado em
vital_signs.weight_kg. A partir do 2.4, o destino correto é a biometria do
app (tabela biometrics). Este script copia o histórico antigo para o novo
destino.

Regras do plano:
- Preservar usuário e hora da medição (measured_at é a identidade do dado).
- Marcar origem: source='migrated_vital_signs'.
- Evitar duplicatas: só copia o que ainda não existe em biometrics para a
  mesma (user_id, measured_at, weight_kg). Idempotente — pode rodar N vezes.
- NÃO apaga a origem: vital_signs permanece intacto. Rollback definido:
  DELETE FROM biometrics WHERE source='migrated_vital_signs';
- Se não houver pesos em vital_signs, a migração é dispensensada e isto é
  documentado na saída (não cria linha nenhuma).

Uso:
  python3 scripts/migrate_weight_history.py --dry-run   # levantamento
  python3 scripts/migrate_weight_history.py             # aplica
"""
import asyncio
import os
import sys

import asyncpg

SOURCE_MARK = "migrated_vital_signs"

SQL_COUNT = "SELECT count(*) FROM vital_signs vs WHERE to_jsonb(vs)->>'weight_kg' IS NOT NULL"

SQL_SAMPLE = """
SELECT vs.user_id, (to_jsonb(vs)->>'weight_kg')::double precision AS weight_kg, vs.measured_at
FROM vital_signs vs WHERE to_jsonb(vs)->>'weight_kg' IS NOT NULL
ORDER BY vs.measured_at DESC LIMIT 20
"""

SQL_MIGRATE = """
WITH legacy AS (
    SELECT vs.user_id, (to_jsonb(vs)->>'weight_kg')::double precision AS weight_kg,
           vs.measured_at, CASE WHEN p.height > 3 THEN p.height / 100.0 ELSE p.height END AS height_m
    FROM vital_signs vs LEFT JOIN profiles p ON p.id = vs.user_id
    WHERE to_jsonb(vs)->>'weight_kg' IS NOT NULL
)
INSERT INTO biometrics (user_id, weight_kg, height_m, imc, measured_at, source)
SELECT vs.user_id, vs.weight_kg,
       CASE WHEN vs.height_m BETWEEN 0.5 AND 2.5 THEN vs.height_m END,
       CASE WHEN vs.height_m BETWEEN 0.5 AND 2.5
            THEN round((vs.weight_kg / power(vs.height_m, 2))::numeric, 1) END,
       vs.measured_at, $1
FROM legacy vs
WHERE NOT EXISTS (
    SELECT 1 FROM biometrics b WHERE b.user_id = vs.user_id
      AND b.measured_at = vs.measured_at AND b.weight_kg = vs.weight_kg
)
"""



async def main(dry_run: bool) -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL não configurado. Abortando (nada foi alterado).")
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        total = await conn.fetchval(SQL_COUNT)
        print(f"Levantamento: {total} registro(s) com peso em vital_signs.")
        if total == 0:
            print("Nenhum peso em vital_signs — migração DISPENSADA.")
            print("Rollback desta migração: nada a fazer (nenhuma linha copiada).")
            return 0

        amostra = await conn.fetch(SQL_SAMPLE)
        print("Amostra dos mais recentes (até 20):")
        for row in amostra:
            print(f"  {row['user_id'][:8]}… {row['weight_kg']} kg @ {row['measured_at']}")

        if dry_run:
            print("\n[dry-run] Nenhuma linha foi copiada. Rode sem --dry-run para aplicar.")
            return 0

        inseridos = await conn.execute(SQL_MIGRATE, SOURCE_MARK)
        print(f"\nMigração aplicada: {inseridos}.")
        print("Origem (vital_signs) NÃO foi alterada — permanece intacta.")
        print(f"Rollback: DELETE FROM biometrics WHERE source='{SOURCE_MARK}';")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sys.exit(asyncio.run(main(dry)))
