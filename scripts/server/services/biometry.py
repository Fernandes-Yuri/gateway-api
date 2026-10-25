"""Biometria do app no backend (PR2, passo 2.4).

Contrato do app (RemoteBiometry): user_id, weight, height, imc, measured_at.
O peso confirmado no WhatsApp passa a ser gravado aqui — mesmo destino lógico
do app — extinguindo a divergência com vital_signs.weight_kg.

Altura: vem do perfil; IMC só é calculado com altura real. Altura nunca é
inventada (o app usava 1.65 como padrão; aqui o IMC fica nulo).
"""
from typing import Any, Optional


def height_in_meters(height: Optional[float]) -> Optional[float]:
    """Altura do perfil normalizada em metros. None se ausente.

    O perfil pode guardar em metros (1.65) ou, por descuido, em centímetros
    (165); normaliza e, se não for uma altura plausível, devolve None.
    """
    if height is None:
        return None
    try:
        h = float(height)
    except (TypeError, ValueError):
        return None
    if h > 3.0:
        h = h / 100.0
    if not (0.5 <= h <= 2.5):
        return None
    return h


def compute_imc(weight_kg: float, height_m: Optional[float]) -> Optional[float]:
    if not height_m or height_m <= 0:
        return None
    return round(weight_kg / (height_m * height_m), 1)


async def _profile_height(conn: Any, patient_id: str) -> Optional[float]:
    try:
        return await conn.fetchval("SELECT height FROM profiles WHERE id=$1", patient_id)
    except Exception as err:
        print(f"[Biometry] Falha ao ler altura do perfil: {err}")
        return None


async def record_weight(conn: Any, *, patient_id: str, weight_kg: float,
                        actor_id: str, confirmation_code: Optional[str] = None,
                        source: str = "app") -> dict:
    """
    Grava o peso na biometria do paciente, na transação do chamador.

    Retorna {"id", "weight_kg", "height_m", "imc"}. imc é None quando não há
    altura cadastrada — nunca é inventada.
    """
    height_m = height_in_meters(await _profile_height(conn, patient_id))
    imc = compute_imc(weight_kg, height_m)
    row = await conn.fetchrow(
        """INSERT INTO biometrics
               (user_id, weight_kg, height_m, imc, measured_at, source, confirmation_code)
           VALUES ($1, $2, $3, $4, NOW(), $5, $6)
           RETURNING id, weight_kg, height_m, imc""",
        patient_id, weight_kg, height_m, imc, source, confirmation_code,
    )
    return {"id": str(row["id"]), "weight_kg": row["weight_kg"],
            "height_m": row["height_m"], "imc": row["imc"]}
