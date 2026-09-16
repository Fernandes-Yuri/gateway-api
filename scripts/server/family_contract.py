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
