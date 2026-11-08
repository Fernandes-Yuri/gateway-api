"""Eventos de consulta entram na transação; estes testes não enviam push real."""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_identity import firebase_uid
from routers import consultations


class ConsultationNotificationTest(unittest.TestCase):
    def setUp(self):
        self.future = datetime.now(timezone.utc) + timedelta(days=2)
        self.stamp = datetime.now(timezone.utc)
        self.consultation = {
            'id': uuid.UUID(int=1), 'user_id': 'patient', 'title': 'Cardiologista',
            'scheduled_date': self.future, 'status': 'SUGGESTED', 'caregiver_id': None,
            'created_by': 'patient', 'created_at': self.stamp, 'updated_at': self.stamp,
        }
        self.conn = AsyncMock()
        self.conn.transaction = MagicMock()
        self.conn.transaction.return_value.__aenter__ = AsyncMock()
        self.conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
        self.conn.fetch.return_value = [{'caregiver_id': 'caregiver'}]
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=self.conn)
        self.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        self.emitter_patch = patch.object(consultations, 'emit_notification', new_callable=AsyncMock)
        self.emitter = self.emitter_patch.start()
        self.dispatch = MagicMock(return_value=True)
        consultations.configure_state(db_pool=self.pool, load_devices=lambda: {}, dispatch_push=self.dispatch)
        self.app = FastAPI()
        self.app.include_router(consultations.router)
        self.app.dependency_overrides[firebase_uid] = lambda: 'caregiver'
        self.client = TestClient(self.app)
        self.cid = str(self.consultation['id'])

    def tearDown(self):
        self.emitter_patch.stop()
        consultations.configure_state()

    def _select_then_update(self, new_status):
        updated = dict(self.consultation, status=new_status, caregiver_id='caregiver',
                       updated_at=self.stamp + timedelta(seconds=1))
        self.conn.fetchrow.side_effect = [dict(self.consultation), updated]
        return updated

    def test_accept_returns_new_status_and_enqueues_patient_without_direct_push(self):
        self._select_then_update('ACCEPTED')
        result = self.client.post('/api/consultation/accept', json={
            'consultationId': self.cid, 'caregiverName': 'Ana', 'caregiverRelation': 'filha'})
        self.assertEqual(200, result.status_code)
        self.assertEqual('ACCEPTED', result.json()['status'])
        self.emitter.assert_awaited_once()
        args = self.emitter.call_args
        self.assertIs(args.args[0], self.conn)
        self.assertEqual('patient', args.kwargs['recipient_id'])
        self.assertEqual('CONSULTATION_ACCEPTED', args.kwargs['kind'])
        self.assertEqual({'status': 'ACCEPTED'}, args.kwargs['payload'])
        self.assertEqual('consultation:' + self.cid, args.kwargs['group_key'])
        self.assertNotIn('Cardiologista', repr(args.kwargs))
        self.dispatch.assert_not_called()

    def test_reject_cancel_complete_enqueue_the_corresponding_change(self):
        for target, route in [('REJECTED', 'reject'), ('CANCELLED', 'cancel'), ('COMPLETED', 'complete')]:
            with self.subTest(target=target):
                self.emitter.reset_mock()
                self._select_then_update(target)
                result = self.client.post('/api/consultation/' + route, json={'consultationId': self.cid})
                self.assertEqual(200, result.status_code)
                self.assertEqual('CONSULTATION_' + target, self.emitter.call_args.kwargs['kind'])
                self.assertEqual('patient', self.emitter.call_args.kwargs['recipient_id'])
                self.dispatch.assert_not_called()

    def test_patient_cannot_accept_own_suggestion(self):
        self._select_then_update('ACCEPTED')
        self.app.dependency_overrides[firebase_uid] = lambda: 'patient'
        result = self.client.post('/api/consultation/accept', json={'consultationId': self.cid})
        self.assertEqual(409, result.status_code)
        self.emitter.assert_not_awaited()

    def test_patient_creation_notifies_only_distinct_other_active_caregivers(self):
        self.app.dependency_overrides[firebase_uid] = lambda: 'patient'
        self.conn.fetch.return_value = [{'caregiver_id': 'caregiver'}, {'caregiver_id': 'caregiver'},
                                       {'caregiver_id': 'patient'}, {'caregiver_id': 'second'}]
        self.conn.fetchrow.return_value = dict(self.consultation)
        result = self.client.post('/api/consultation/create', json={
            'id': self.cid, 'title': 'Cardiologista', 'scheduledDate': self.future.isoformat()})
        self.assertEqual(200, result.status_code)
        self.assertEqual(['caregiver', 'second'], [call.kwargs['recipient_id'] for call in self.emitter.call_args_list])
        self.assertTrue(all(call.kwargs['kind'] == 'CONSULTATION_CREATED' for call in self.emitter.call_args_list))
        self.assertIn("status='ACTIVE'", self.conn.fetch.call_args.args[0])
        self.dispatch.assert_not_called()

    def test_creation_retry_does_not_enqueue_again(self):
        self.conn.fetchrow.side_effect = [None, dict(self.consultation, created_by='caregiver')]
        result = self.client.post('/api/consultation/create', json={
            'id': self.cid, 'patientId': 'patient', 'title': 'Cardiologista', 'scheduledDate': self.future.isoformat()})
        self.assertEqual(200, result.status_code)
        self.emitter.assert_not_awaited()

    def test_accepted_retry_does_not_enqueue_again(self):
        self.conn.fetchrow.return_value = dict(self.consultation, status='ACCEPTED', caregiver_id='caregiver')
        result = self.client.post('/api/consultation/accept', json={'consultationId': self.cid})
        self.assertEqual(200, result.status_code)
        self.emitter.assert_not_awaited()
        self.assertEqual(1, self.conn.fetchrow.await_count)

    def test_emission_failure_rolls_back_source_transaction(self):
        self._select_then_update('ACCEPTED')
        self.emitter.side_effect = RuntimeError('caixa indisponível')
        with self.assertRaises(RuntimeError):
            self.client.post('/api/consultation/accept', json={'consultationId': self.cid})
        exit_args = self.conn.transaction.return_value.__aexit__.call_args.args
        self.assertIs(RuntimeError, exit_args[0])
        self.dispatch.assert_not_called()

    def test_reschedule_uses_same_consultation_and_emits_only_metadata(self):
        scheduled = self.future + timedelta(days=1)
        updated = dict(self.consultation, title='Clínica', scheduled_date=scheduled,
                       updated_at=self.stamp + timedelta(seconds=1))
        self.conn.fetchrow.side_effect = [dict(self.consultation), updated]
        result = self.client.post('/api/consultation/update', json={
            'consultationId': self.cid, 'title': 'Clínica', 'scheduledDate': scheduled.isoformat()})
        self.assertEqual(200, result.status_code)
        self.assertEqual(self.cid, result.json()['id'])
        self.assertEqual(scheduled.isoformat(), result.json()['scheduledDate'])
        self.assertEqual('CONSULTATION_UPDATED', self.emitter.call_args.kwargs['kind'])
        self.assertEqual({'status': 'SUGGESTED'}, self.emitter.call_args.kwargs['payload'])

    def test_identical_update_has_no_write_or_event(self):
        self.conn.fetchrow.return_value = self.consultation
        result = self.client.post('/api/consultation/update', json={
            'consultationId': self.cid, 'title': self.consultation['title'], 'scheduledDate': self.future.isoformat()})
        self.assertEqual(200, result.status_code)
        self.assertEqual(1, self.conn.fetchrow.await_count)
        self.emitter.assert_not_awaited()

    def test_closed_consultation_cannot_be_rescheduled(self):
        self.conn.fetchrow.return_value = dict(self.consultation, status='CANCELLED')
        result = self.client.post('/api/consultation/update', json={'consultationId': self.cid, 'title': 'Clínica'})
        self.assertEqual(409, result.status_code)
        self.emitter.assert_not_awaited()

    def test_reschedule_validation_rejects_empty_and_timezone_free_or_past_values(self):
        for changes in ({}, {'title': ' '}, {'scheduledDate': '2030-01-01T10:00:00'},
                        {'scheduledDate': '2000-01-01T10:00:00+00:00'}):
            with self.subTest(changes=changes):
                result = self.client.post('/api/consultation/update', json={'consultationId': self.cid, **changes})
                self.assertEqual(422, result.status_code)
        self.conn.fetchrow.assert_not_awaited()
        self.emitter.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
