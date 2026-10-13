"""Maquina de confirmacao de leituras vitais via WhatsApp.

Extraida do monolito server.py na Fase A passo 6
(doc 11_PLANO_MODULARIZACAO_E_TTS.md).

Fluxo: o usuario dita "minha pressao e 12 por 8" no WhatsApp -> o bot cria
uma pendencia (create_pending_confirmation) e envia botes Confirmar/Editar/
Cancelar (send_confirmation_buttons). O clique volta pelo webhook e chama
confirm_pending_confirmation, que so entao grava em vital_signs. Nada e
registrado sem o consentimento explicito do paciente — requisito clinico e
de auditoria (doc 10 §3.3).

A paridade do gatilho clinico (doc 10 §3.2) tambem mora aqui: uma leitura
critica confirmada pelo bot alerta o cuidador exatamente como a mesma
leitura vinda do app faria.
"""
import json
import secrets
from datetime import datetime, timedelta
from typing import Optional


# =============================================================
# VALIDACAO CLINICA (faixas SBC/SBD/OMS)
# =============================================================
HEALTH_VALIDATION_RULES = {
    "PRESSAO": {
        "sistolica": {"min": 60, "max": 300, "label": "Sistólica"},
        "diastolica": {"min": 40, "max": 200, "label": "Diastólica"},
    },
    "GLICEMIA": {
        "glicemia": {"min": 20, "max": 600, "label": "Glicose"},
    },
    "AGUA": {
        "quantidade_ml": {"min": 50, "max": 5000, "label": "Quantidade"},
    },
    "PESO": {
        "peso_kg": {"min": 20, "max": 300, "label": "Peso"},
    },
    "BATIMENTOS": {
        "batimentos": {"min": 30, "max": 250, "label": "Batimentos"},
    },
    "OXIGENACAO": {
        "saturacao": {"min": 50, "max": 100, "label": "Saturação"},
    },
}


def validate_health_data(metric_type: str, params: dict) -> tuple:
    """
    Valida dados de saude contra limites clinicos.
    Retorna (is_valid, error_message, validated_params)
    """
    rules = HEALTH_VALIDATION_RULES.get(metric_type)
    if not rules:
        return True, None, params

    validated = params.copy()
    errors = []

    for field, limits in rules.items():
        value = params.get(field)
        if value is None:
            continue

        try:
            value = int(value) if isinstance(value, (int, str)) and str(value).isdigit() else float(value)
        except (ValueError, TypeError):
            errors.append(f"{limits['label']}: valor inválido")
            continue

        if value < limits["min"] or value > limits["max"]:
            errors.append(f"{limits['label']}: {value} (deve ser entre {limits['min']} e {limits['max']})")
        else:
            validated[field] = value

    # Validacao especial para pressao: sistolica > diastolica
    if metric_type == "PRESSAO":
        sistolica = validated.get("sistolica")
        diastolica = validated.get("diastolica")
        if sistolica and diastolica and sistolica <= diastolica:
            errors.append(f"Sistólica ({sistolica}) deve ser maior que a Diastólica ({diastolica})")

    if errors:
        return False, "\n".join(errors), None

    return True, None, validated


# =============================================================
# PENDENCIAS (pending_confirmations)
# =============================================================
def generate_confirmation_code() -> str:
    """Gera codigo de confirmacao unico: CONF-YYYYMMDD-XXXX"""
    date_part = datetime.now().strftime("%Y%m%d")
    random_part = secrets.token_hex(2).upper()
    return f"CONF-{date_part}-{random_part}"


async def create_pending_confirmation(
    user_id: str,
    metric_type: str,
    value_json: dict,
    db_pool
) -> Optional[str]:
    """
    Cria uma pendencia de confirmacao no banco.
    Retorna o confirmation_code ou None em caso de erro.
    """
    if not db_pool:
        return None

    confirmation_code = generate_confirmation_code()
    expires_at = datetime.utcnow() + timedelta(minutes=5)

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pending_confirmations
                    (user_id, confirmation_code, metric_type, value_json, status, expires_at)
                VALUES ($1, $2, $3, $4, 'pending', $5)
            """, user_id, confirmation_code, metric_type, json.dumps(value_json), expires_at)
            print(f"[Confirmation] Pendência criada: {confirmation_code} para {metric_type}")
            return confirmation_code
    except Exception as e:
        print(f"[Confirmation Error] Falha ao criar pendência: {e}")
        return None


async def confirm_pending_confirmation(
    confirmation_code: str,
    user_id: str,
    db_pool,
    was_edited: bool = False,
    final_values: Optional[dict] = None
) -> bool:
    """
    Confirma uma pendencia e grava nos registros de saude.
    Retorna True se confirmado com sucesso.
    """
    if not db_pool:
        return False

    try:
        async with db_pool.acquire() as conn:
            # Busca a pendencia
            row = await conn.fetchrow("""
                SELECT id, metric_type, value_json
                FROM pending_confirmations
                WHERE confirmation_code = $1
                  AND user_id = $2
                  AND status = 'pending'
                  AND expires_at > NOW()
            """, confirmation_code, user_id)

            if not row:
                print(f"[Confirmation] Pendência não encontrada ou expirada: {confirmation_code}")
                return False

            pending_id = row['id']
            metric_type = row['metric_type']
            original_values = json.loads(row['value_json'])
            values_to_save = final_values if was_edited else original_values

            # Transaction
            async with conn.transaction():
                # 1. Consome a pendencia de forma atomica (passo 1.4): o UPDATE
                # so vence se ela ainda estiver 'pending', fechando a race entre
                # dois cliques concorrentes no mesmo botao ou um reenvio da Meta.
                # Consumir a pendencia e gravar o registro ficam nesta mesma
                # transacao: ou entram juntos, ou nenhum entra.
                consumed = await conn.execute("""
                    UPDATE pending_confirmations
                    SET status = 'confirmed', confirmed_at = NOW()
                    WHERE id = $1 AND status = 'pending'
                """, pending_id)
                if consumed != "UPDATE 1":
                    print(f"[Confirmation] Pendência já resolvida por outra via: {confirmation_code}")
                    return False

                # 2. Grava no vital_signs
                if metric_type == "PRESSAO":
                    await conn.execute("""
                        INSERT INTO vital_signs
                            (user_id, systolic_pressure, diastolic_pressure, measured_at, confirmed_via, confirmation_code)
                        VALUES ($1, $2, $3, NOW(), 'whatsapp', $4)
                    """, user_id, values_to_save.get("sistolica"), values_to_save.get("diastolica"), confirmation_code)

                elif metric_type == "GLICEMIA":
                    await conn.execute("""
                        INSERT INTO vital_signs
                            (user_id, glucose_level, glucose_type, measured_at, confirmed_via, confirmation_code)
                        VALUES ($1, $2, $3, NOW(), 'whatsapp', $4)
                    """, user_id, values_to_save.get("glicemia"), values_to_save.get("tipo", "jejum"), confirmation_code)

                elif metric_type == "AGUA":
                    await conn.execute("""
                        INSERT INTO vital_signs
                            (user_id, hydration_ml, measured_at, confirmed_via, confirmation_code)
                        VALUES ($1, $2, NOW(), 'whatsapp', $3)
                    """, user_id, values_to_save.get("quantidade_ml"), confirmation_code)

                elif metric_type == "PESO":
                    # Passo 2.4: peso vai para a biometria do app (mesmo
                    # destino lógico do app), não para vital_signs. Altura e
                    # IMC vêm do perfil; IMC nunca é inventado.
                    from services.biometry import record_weight
                    await record_weight(
                        conn, patient_id=user_id,
                        weight_kg=float(values_to_save.get("peso_kg") or 0),
                        actor_id=user_id, confirmation_code=confirmation_code)

                elif metric_type == "BATIMENTOS":
                    await conn.execute("""
                        INSERT INTO vital_signs
                            (user_id, heart_rate, measured_at, confirmed_via, confirmation_code)
                        VALUES ($1, $2, NOW(), 'whatsapp', $3)
                    """, user_id, values_to_save.get("batimentos"), confirmation_code)

                elif metric_type == "OXIGENACAO":
                    await conn.execute("""
                        INSERT INTO vital_signs
                            (user_id, oxygen_saturation, measured_at, confirmed_via, confirmation_code)
                        VALUES ($1, $2, NOW(), 'whatsapp', $3)
                    """, user_id, values_to_save.get("saturacao"), confirmation_code)

                # 3. Registra auditoria
                await conn.execute("""
                    INSERT INTO confirmation_logs
                        (user_id, pending_conf_id, confirmation_code, action, values_submitted, values_confirmed, channel, confirmed_by_user, was_edited)
                    VALUES ($1, $2, $3, $4, $5, $6, 'whatsapp', TRUE, $7)
                """, user_id, pending_id, confirmation_code, f"REGISTRAR_{metric_type}",
                     json.dumps(original_values), json.dumps(values_to_save), was_edited)

            # Paridade do gatilho clinico (doc 10 §3.2): uma leitura critica
            # confirmada pelo bot do WhatsApp deve alertar o cuidador EXATAMENTE
            # como a mesma leitura vinda do app faria. Antes isso so existia em
            # sync_vital_sign, e o canal do WhatsApp era silencioso.
            if _state.dispatch_clinical_alert is not None:
                await _state.dispatch_clinical_alert(
                    db_pool,
                    user_id,
                    systolic=values_to_save.get("sistolica") if metric_type == "PRESSAO" else None,
                    diastolic=values_to_save.get("diastolica") if metric_type == "PRESSAO" else None,
                    glucose=values_to_save.get("glicemia") if metric_type == "GLICEMIA" else None,
                    glucose_type=values_to_save.get("tipo"),
                )

            print(f"[Confirmation] Registro confirmado: {confirmation_code} - {metric_type}")
            return True

    except Exception as e:
        print(f"[Confirmation Error] Falha ao confirmar: {e}")
        return False


async def cancel_pending_confirmation(confirmation_code: str, user_id: str, db_pool) -> bool:
    """Cancela uma pendencia de confirmacao."""
    if not db_pool:
        return False

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE pending_confirmations
                SET status = 'cancelled', cancelled_at = NOW()
                WHERE confirmation_code = $1
                  AND user_id = $2
                  AND status = 'pending'
            """, confirmation_code, user_id)
            print(f"[Confirmation] Pendência cancelada: {confirmation_code}")
            return True
    except Exception as e:
        print(f"[Confirmation Error] Falha ao cancelar: {e}")
        return False


# =============================================================
# MENSAGEM INTERATIVA (botes do WhatsApp)
# =============================================================
def build_confirmation_message(metric_type: str, values: dict, confirmation_code: str, user_name: str = "") -> str:
    """Constroi a mensagem de confirmacao formatada."""
    display_name = user_name.split()[0] if user_name else "Usuário"

    if metric_type == "PRESSAO":
        value_display = f"{values.get('sistolica')} x {values.get('diastolica')} mmHg"
        icon = "🩺"
    elif metric_type == "GLICEMIA":
        value_display = f"{values.get('glicemia')} mg/dL"
        icon = "🩸"
    elif metric_type == "AGUA":
        value_display = f"{values.get('quantidade_ml')} ml"
        icon = "💧"
    elif metric_type == "PESO":
        peso = values.get("peso_kg")
        value_display = (f"{peso:.1f}".replace(".", ",") if isinstance(peso, float)
                         else f"{peso}") + " kg"
        icon = "⚖️"
    elif metric_type == "BATIMENTOS":
        value_display = f"{values.get('batimentos')} bpm"
        icon = "❤️"
    elif metric_type == "OXIGENACAO":
        value_display = f"{values.get('saturacao')}%"
        icon = "🫁"
    else:
        value_display = str(values)
        icon = "📋"

    return f"""{icon} *Confirmação de Registro*

{metric_type.replace('_', ' ').title()}: *{value_display}*
Usuário: {display_name}
Código: `{confirmation_code}`

Confirma este registro?
Seu registro será autenticado por você e pode ser consultado no app."""


async def send_confirmation_buttons(
    to_number: str,
    metric_type: str,
    values: dict,
    confirmation_code: str,
    user_name: str = ""
) -> bool:
    """Envia mensagem interativa com botes de confirmacao.

    O envio e Best-effort: se o servico de WhatsApp nao estiver configurado
    (_state.send_whatsapp_interactive is None) retorna False sem quebrar o
    fluxo de conversa.
    """
    if _state.send_whatsapp_interactive is None:
        print("[Confirmation] WhatsApp indisponível — botões não enviados.")
        return False

    body_text = build_confirmation_message(metric_type, values, confirmation_code, user_name)

    buttons = [
        {"id": f"confirm_{confirmation_code}", "title": "✅ Confirmar"},
        {"id": f"edit_{confirmation_code}", "title": "✏️ Editar"},
        {"id": f"cancel_{confirmation_code}", "title": "❌ Cancelar"},
    ]

    result = await _state.send_whatsapp_interactive(
        to_number=to_number,
        body_text=body_text,
        buttons=buttons,
        header_text="📋 Confirmação de Registro",
        footer_text="Braga Saúde - Seus dados, sua confiança"
    )

    return result.get("success", False)


class _AppState:
    """Porta das dependencias do monolito (dispatch_clinical_alert e o servico
    de WhatsApp). Preenchida por server.py no startup."""

    dispatch_clinical_alert = None
    send_whatsapp_interactive = None


_state = _AppState()


def configure_state(*, dispatch_clinical_alert=None, send_whatsapp_interactive=None):
    """Chamado por server.py ao subir a aplicacao."""
    _state.dispatch_clinical_alert = dispatch_clinical_alert
    _state.send_whatsapp_interactive = send_whatsapp_interactive
