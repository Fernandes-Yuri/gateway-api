"""Consultas de saúde do app reusadas no WhatsApp (PR2, passo 2.1).

Cada consulta é uma leitura pura sobre vital_signs, devolvendo sempre
OperationResult estruturado: valor + unidade + hora da medição, ordenada pela
hora da medição. Dado antigo nunca é vendido como "de hoje" — a mensagem
mostra a data real do registro.

Sem registros -> missing_data (convidar a registrar). Banco indisponível ->
unavailable (nunca "você não tem registros"). A autorização é revalidada em
cada chamada (validate_actor do contrato do passo 1.3).
"""
from datetime import date, datetime, timedelta
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.operations import OperationContext, OperationResult, validate_actor

# Fuso padrão (passo 2.2: fuso cadastrado no perfil, se houver).
DEFAULT_ZONE = "America/Sao_Paulo"

_UNIT = {
    "pressao": "mmHg",
    "glicemia": "mg/dL",
    "batimentos": "bpm",
    "saturacao": "%",
    "peso": "kg",
}

_LABEL = {
    "pressao": "pressão",
    "glicemia": "glicemia",
    "batimentos": "batimentos",
    "saturacao": "saturação",
    "peso": "peso",
}

# Colunas selecionadas e condição "existe leitura" por métrica.
_COLS = {
    "pressao": ("systolic_pressure, diastolic_pressure",
                "systolic_pressure IS NOT NULL AND diastolic_pressure IS NOT NULL"),
    "glicemia": ("glucose_level", "glucose_level IS NOT NULL"),
    "batimentos": ("heart_rate", "heart_rate IS NOT NULL"),
    "saturacao": ("oxygen_saturation", "oxygen_saturation IS NOT NULL"),
    "peso": ("weight_kg", "weight_kg IS NOT NULL"),
}


def zone_of(name: Optional[str]) -> ZoneInfo:
    """Fuso do perfil, ou America/Sao_Paulo. Nunca usa o relógio da EC2."""
    try:
        return ZoneInfo(name or DEFAULT_ZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_ZONE)


async def patient_timezone(conn: Any, patient_id: str) -> Optional[str]:
    """Fuso cadastrado no perfil (migration 052). NULL -> America/Sao_Paulo."""
    try:
        return await conn.fetchval("SELECT timezone FROM profiles WHERE id=$1", patient_id)
    except Exception:
        return None


async def _resolve_zone(conn: Any, context: OperationContext,
                        zone: Optional[str]) -> ZoneInfo:
    """Fuso explícito na chamada; senão, o cadastrado no perfil; senão, o padrão."""
    if zone:
        return zone_of(zone)
    return zone_of(await patient_timezone(conn, context.patient_id))


async def _owner_first_name(conn: Any, context: OperationContext) -> Optional[str]:
    """
    Passo 4.2: primeiro nome do paciente-alvo, lido no banco.

    Autocuidado (actor == patient) devolve None: a mensagem trata a própria
    pessoa. Familiar devolve o nome, para a resposta dizer de quem é o dado
    — valor sem dono parece dado do próprio interlocutor.
    """
    if context.is_self_care:
        return None
    try:
        nome = await conn.fetchval(
            "SELECT full_name FROM profiles WHERE id=$1", context.patient_id)
    except Exception as err:
        print(f"[Health Queries] Aviso: nome do paciente não lido: {err}")
        nome = None
    return (nome or "").split()[0] if (nome or "").strip() else "seu familiar"


def _last_reading_message(owner: Optional[str], label: str, value: str,
                          unit: str, when: str) -> str:
    """Última leitura nomeando o dono do dado (passo 4.2)."""
    if owner is None:
        return f"Sua última {label} foi {value} {unit}, registrada {when}."
    return (f"A última {label} de {owner} foi {value} {unit}, "
            f"registrada {when}.")


def _no_reading_message(owner: Optional[str], label: str) -> str:
    """Sem registros: informa a ausência, sem inferir estado de saúde."""
    if owner is None:
        return (f"Ainda não encontrei registros de {label} seu. Quando medir, "
                f"é só me enviar aqui que eu anoto com sua confirmação.")
    return (f"Ainda não encontrei registros de {label} de {owner}. "
            f"Quando ele(a) medir, é só enviar aqui que eu anoto.")


def format_value(kind: str, row: Any) -> str:
    """Valor já formatado para exibição (ex.: '130 x 85', '74,5')."""
    if kind == "pressao":
        return f"{row['systolic_pressure']} x {row['diastolic_pressure']}"
    if kind == "peso":
        peso = row["weight_kg"]
        return f"{peso:.1f}".replace(".", ",") if isinstance(peso, float) else str(peso)
    campo = {
        "glicemia": "glucose_level",
        "batimentos": "heart_rate",
        "saturacao": "oxygen_saturation",
    }[kind]
    return str(row[campo])


def format_when(moment: Any, zone: ZoneInfo) -> str:
    """Hora local legível: 'hoje às 09:15', 'ontem às 22:00', 'em 12/10 às 09:15'."""
    if moment is None:
        return ""
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    local = moment.astimezone(zone)
    hoje = datetime.now(zone).date()
    if local.date() == hoje:
        dia = "hoje"
    elif (hoje - local.date()).days == 1:
        dia = "ontem"
    else:
        dia = f"em {local.strftime('%d/%m')}"
    return f"{dia} às {local.strftime('%H:%M')}"


def day_bounds(day: Optional[date], zone: ZoneInfo) -> Tuple[datetime, datetime]:
    """Intervalo [meia-noite local, próxima meia-noite) convertido para UTC.

    Passo 2.2: 'hoje' é no fuso do paciente, nunca no relógio da EC2.
    """
    alvo = day or datetime.now(zone).date()
    inicio = datetime.combine(alvo, datetime.min.time(), zone)
    return inicio, inicio + timedelta(days=1)


async def _query_reading(conn: Any, context: OperationContext, kind: str,
                         zone_name: Optional[str]) -> OperationResult:
    denied = await validate_actor(conn, context)
    if denied:
        return denied
    tz = await _resolve_zone(conn, context, zone_name)
    owner = await _owner_first_name(conn, context)
    colunas, nao_nulo = _COLS[kind]
    try:
        row = await conn.fetchrow(
            f"""SELECT id, {colunas}, measured_at FROM vital_signs
                WHERE user_id = $1 AND {nao_nulo}
                ORDER BY measured_at DESC LIMIT 1""",
            context.patient_id,
        )
    except Exception as err:
        print(f"[Health Queries] Falha ao consultar {kind}: {err}")
        return OperationResult.unavailable(
            "Não consegui acessar seus registros agora. Tente novamente em instantes.")
    if not row:
        return OperationResult.missing_data(
            _no_reading_message(owner, _LABEL[kind]),
            data={"kind": kind})
    return OperationResult.success(
        _last_reading_message(owner, _LABEL[kind], format_value(kind, row),
                              _UNIT[kind], format_when(row["measured_at"], tz)),
        data={"kind": kind, "value": format_value(kind, row),
              "unit": _UNIT[kind], "measured_at": row["measured_at"].isoformat()})


async def last_blood_pressure(conn: Any, context: OperationContext,
                              zone: Optional[str] = None) -> OperationResult:
    return await _query_reading(conn, context, "pressao", zone)


async def last_glucose(conn: Any, context: OperationContext,
                       zone: Optional[str] = None) -> OperationResult:
    return await _query_reading(conn, context, "glicemia", zone)


async def last_heart_rate(conn: Any, context: OperationContext,
                          zone: Optional[str] = None) -> OperationResult:
    return await _query_reading(conn, context, "batimentos", zone)


async def last_oxygen_saturation(conn: Any, context: OperationContext,
                                 zone: Optional[str] = None) -> OperationResult:
    return await _query_reading(conn, context, "saturacao", zone)


async def last_weight(conn: Any, context: OperationContext,
                      zone: Optional[str] = None) -> OperationResult:
    # Passo 2.4/2.5: o peso pode estar na biometria (novo destino) ou no
    # histórico de vital_signs; mostra o registro mais recente de qualquer
    # uma das fontes.
    denied = await validate_actor(conn, context)
    if denied:
        return denied
    tz = await _resolve_zone(conn, context, zone)
    owner = await _owner_first_name(conn, context)
    try:
        row = await conn.fetchrow(
            """SELECT weight_kg, measured_at FROM biometrics
               WHERE user_id = $1
               UNION ALL
               SELECT weight_kg, measured_at FROM vital_signs
               WHERE user_id = $1 AND weight_kg IS NOT NULL
               ORDER BY measured_at DESC LIMIT 1""",
            context.patient_id,
        )
    except Exception as err:
        print(f"[Health Queries] Falha ao consultar peso: {err}")
        return OperationResult.unavailable(
            "Não consegui acessar seus registros agora. Tente novamente em instantes.")
    if not row:
        return OperationResult.missing_data(
            _no_reading_message(owner, "peso"),
            data={"kind": "peso"})
    peso = row["weight_kg"]
    value = f"{peso:.1f}".replace(".", ",") if isinstance(peso, float) else str(peso)
    return OperationResult.success(
        _last_reading_message(owner, "peso", value, "kg",
                              format_when(row["measured_at"], tz)),
        data={"kind": "peso", "value": value, "unit": "kg",
              "measured_at": row["measured_at"].isoformat()})


async def water_today(conn: Any, context: OperationContext, *,
                      day: Optional[date] = None,
                      zone: Optional[str] = None) -> OperationResult:
    """Soma de hidratação do dia (no fuso do paciente)."""
    denied = await validate_actor(conn, context)
    if denied:
        return denied
    tz = await _resolve_zone(conn, context, zone)
    owner = await _owner_first_name(conn, context)
    inicio, fim = day_bounds(day, tz)
    try:
        total = await conn.fetchval(
            """SELECT COALESCE(SUM(hydration_ml), 0) FROM vital_signs
               WHERE user_id = $1 AND hydration_ml IS NOT NULL
                 AND measured_at >= $2 AND measured_at < $3""",
            context.patient_id, inicio, fim,
        )
    except Exception as err:
        print(f"[Health Queries] Falha ao consultar água: {err}")
        return OperationResult.unavailable(
            "Não consegui acessar seus registros agora. Tente novamente em instantes.")
    total = int(total or 0)
    dia_iso = (day or datetime.now(tz).date()).isoformat()
    sujeito = "Você" if owner is None else owner
    if total <= 0:
        return OperationResult.missing_data(
            (f"{sujeito} ainda não registrou água hoje. Que tal um copo agora? "
             "Me diga a quantidade, como 'bebi 200 ml'."),
            data={"kind": "agua", "ml": 0, "day": dia_iso})
    return OperationResult.success(
        f"{sujeito} registrou {total} ml de água hoje.",
        data={"kind": "agua", "ml": total, "day": dia_iso})


async def day_summary(conn: Any, context: OperationContext, *,
                      day: Optional[date] = None,
                      zone: Optional[str] = None) -> OperationResult:
    """Resumo do dia: hidratação total + leituras do dia, por hora."""
    denied = await validate_actor(conn, context)
    if denied:
        return denied
    tz = await _resolve_zone(conn, context, zone)
    owner = await _owner_first_name(conn, context)
    inicio, fim = day_bounds(day, tz)
    try:
        rows = await conn.fetch(
            """SELECT systolic_pressure, diastolic_pressure, heart_rate,
                      oxygen_saturation, glucose_level, hydration_ml, weight_kg,
                      measured_at
               FROM vital_signs
               WHERE user_id = $1 AND measured_at >= $2 AND measured_at < $3
               ORDER BY measured_at ASC""",
            context.patient_id, inicio, fim,
        )
    except Exception as err:
        print(f"[Health Queries] Falha ao montar o resumo: {err}")
        return OperationResult.unavailable(
            "Não consegui acessar seus registros agora. Tente novamente em instantes.")
    leituras = []
    for row in rows:
        if row["systolic_pressure"] and row["diastolic_pressure"]:
            leituras.append({"kind": "pressao", "value": format_value("pressao", row),
                             "unit": _UNIT["pressao"], "at": row["measured_at"].isoformat()})
        if row["glucose_level"]:
            leituras.append({"kind": "glicemia", "value": str(row["glucose_level"]),
                             "unit": _UNIT["glicemia"], "at": row["measured_at"].isoformat()})
        if row["heart_rate"]:
            leituras.append({"kind": "batimentos", "value": str(row["heart_rate"]),
                             "unit": _UNIT["batimentos"], "at": row["measured_at"].isoformat()})
        if row["oxygen_saturation"]:
            leituras.append({"kind": "saturacao", "value": str(row["oxygen_saturation"]),
                             "unit": _UNIT["saturacao"], "at": row["measured_at"].isoformat()})
        if row["weight_kg"]:
            leituras.append({"kind": "peso", "value": format_value("peso", row),
                             "unit": _UNIT["peso"], "at": row["measured_at"].isoformat()})
    # Passo 2.4: peso do dia pode estar na biometria (novo destino).
    try:
        bio_rows = await conn.fetch(
            """SELECT weight_kg, measured_at FROM biometrics
               WHERE user_id = $1 AND measured_at >= $2 AND measured_at < $3""",
            context.patient_id, inicio, fim,
        )
        for row in bio_rows:
            leituras.append({"kind": "peso", "value": format_value("peso", row),
                             "unit": _UNIT["peso"], "at": row["measured_at"].isoformat()})
        leituras.sort(key=lambda leitura: leitura["at"])
    except Exception as err:
        print(f"[Health Queries] Aviso: biometria do dia não lida: {err}")
    agua = sum(r["hydration_ml"] or 0 for r in rows)
    dia_iso = (day or datetime.now(tz).date()).isoformat()
    if not leituras and agua <= 0:
        return OperationResult.missing_data(
            (f"Ainda não há registros{' de ' + owner if owner else ' seus'} hoje. "
             "Quando medir ou beber água, é só me avisar."),
            data={"day": dia_iso, "readings": []})
    partes = []
    if agua > 0:
        partes.append(f"Água: {agua} ml")
    for leitura in leituras:
        partes.append(f"{_LABEL[leitura['kind']].capitalize()}: {leitura['value']} "
                      f"{leitura['unit']} {format_when(leitura['at'], tz)}")
    titulo = "Resumo de hoje" if owner is None else f"Resumo de hoje de {owner}"
    return OperationResult.success(
        titulo + ": " + "; ".join(partes) + ".",
        data={"day": dia_iso, "water_ml": agua, "readings": leituras})
