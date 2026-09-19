"""Testes do validador de aceite/recusa de consulta (doc 10 §2.1)."""
import unittest
from datetime import datetime, timedelta, timezone
from family_contract import validate_consultation_acceptance


def consultation(**kw):
    base = dict(user_id='patient', status='SUGGESTED', caregiver_id=None,
                scheduled_date=datetime.now(timezone.utc) + timedelta(days=2))
    base.update(kw)
    return base


class ValidateConsultationAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)

    def test_patient_cannot_answer_own_suggestion(self):
        with self.assertRaises(ValueError) as error:
            validate_consultation_acceptance(consultation(), 'patient', self.now)
        self.assertIn('familiar', str(error.exception).lower())

    def test_caregiver_can_accept_pending_suggestion(self):
        validate_consultation_acceptance(consultation(), 'caregiver', self.now)

    def test_accepted_by_same_caregiver_is_idempotent(self):
        c = consultation(status='ACCEPTED', caregiver_id='caregiver')
        validate_consultation_acceptance(c, 'caregiver', self.now)

    def test_already_answered_is_rejected(self):
        c = consultation(status='REJECTED')
        with self.assertRaises(ValueError):
            validate_consultation_acceptance(c, 'caregiver', self.now)

    def test_past_consultation_cannot_be_answered(self):
        c = consultation(scheduled_date=self.now - timedelta(hours=1))
        with self.assertRaises(ValueError) as error:
            validate_consultation_acceptance(c, 'caregiver', self.now)
        self.assertIn('horário', str(error.exception))


if __name__ == '__main__':
    unittest.main()
