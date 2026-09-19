"""Family invitation rules shared by the API and local unit tests."""
from datetime import datetime, timezone


def validate_acceptance(binding, uid, now=None):
    now = now or datetime.now(timezone.utc)
    if binding['patient_id'] == uid:
        raise ValueError('Você não pode acompanhar seu próprio perfil.')
    if binding['status'] == 'ACTIVE' and binding['caregiver_id'] == uid:
        return  # Idempotent retry after a lost response.
    if (binding['status'] != 'PENDING' or binding['caregiver_id'] is not None
            or binding['expires_at'] is None or binding['expires_at'] <= now):
        raise ValueError('Código indisponível ou expirado. Peça um novo convite.')


def validate_consultation_acceptance(consultation, uid, now=None):
    """Regras para um familiar aceitar/recusar uma consulta sugerida (doc 10 §2.1).

    A consulta pertence ao paciente (user_id); quem responde é um cuidador.
    """
    now = now or datetime.now(timezone.utc)
    if consultation['user_id'] == uid:
        raise ValueError('Apenas um familiar pode aceitar ou recusar a consulta.')
    if consultation['status'] == 'ACCEPTED' and consultation['caregiver_id'] == uid:
        return  # Retentativa idempotente após resposta perdida.
    if consultation['status'] != 'SUGGESTED':
        raise ValueError('Esta consulta já foi respondida.')
    if consultation['scheduled_date'] is not None and consultation['scheduled_date'] <= now:
        raise ValueError('Esta consulta já passou do horário.')
