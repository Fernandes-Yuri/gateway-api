"""Run with unittest; no PostgreSQL server or account is created."""
import ast
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel, Field
from fastapi import FastAPI
from fastapi.testclient import TestClient
from profile_contract import validate_profile, profile_upsert
from family_contract import validate_acceptance
from family_api import install_family_routes
from api_identity import firebase_uid


# Load the actual DTO declarations without starting the gateway's AI services.
tree = ast.parse(Path(__file__).with_name('server.py').read_text(encoding='utf-8'))
names = {'ProfileSyncDto', 'FamilyBindingSyncDto', 'FamilyMessageSyncDto'}
namespace = dict(BaseModel=BaseModel, Field=Field, Optional=Optional, List=List)
exec(compile(ast.Module([n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names], type_ignores=[]), 'server.py', 'exec'), namespace)
Profile = namespace['ProfileSyncDto']


class ProfileTests(unittest.TestCase):
    def test_stub_can_exist_before_role_choice(self):
        validate_profile(Profile(id='same-firebase-uid', fullName=''))

    def test_viewer_to_hybrid_preserves_identity_and_requires_biometrics(self):
        viewer = Profile(id='same-firebase-uid', fullName='Maria', userRole='CAREGIVER', caregiverMode='VIEWER_ONLY', basicProfileComplete=True)
        validate_profile(viewer)
        pending = viewer.model_copy(update={'selfCareSetupPending': True})
        validate_profile(pending)
        hybrid = pending.model_copy(update={'caregiverMode': 'HYBRID', 'selfCareSetupPending': False, 'selfCareComplete': True})
        with self.assertRaises(ValueError):
            validate_profile(hybrid)
        hybrid = hybrid.model_copy(update={'weight': 70.5, 'height': 170.0})
        validate_profile(hybrid)
        sql, values = profile_upsert(hybrid)
        self.assertEqual(viewer.id, values[0])
        self.assertIn('ON CONFLICT (id) DO UPDATE', sql)
        self.assertNotIn('family_bindings', sql)

    def test_invalid_combinations_rejected(self):
        for change in ({'userRole': 'ADMIN'}, {'caregiverMode': 'HYBRID'},
                       {'selfCareComplete': True}, {'selfCareSetupPending': True},
                       {'basicProfileComplete': True}, {'birthDate': '31/02/2020'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_profile(Profile(id='uid', fullName='', **change))

    def test_consent_allergies_and_fractional_calories_round_trip_values(self):
        p = Profile(id='uid', fullName="O'Brien", consentAcceptedAt='2026-09-08T10:00:00-03:00', foodAllergies=['Ovos'], dailyCalorieTarget=1800.5)
        sql, values = profile_upsert(p)
        self.assertNotIn(p.fullName, sql)
        self.assertIn(p.fullName, values)
        self.assertIn(['Ovos'], values)
        self.assertIn(1800.5, values)
        self.assertTrue(any(isinstance(v, datetime) and v.utcoffset() == timedelta(hours=-3) for v in values))


class InvitationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.binding = dict(id=uuid.uuid4(), patient_id='patient', caregiver_id=None, status='PENDING', expires_at=self.now + timedelta(days=1))

    def test_only_available_invitation_can_be_accepted(self):
        validate_acceptance(self.binding, 'caregiver', self.now)
        for change in ({'status': 'REVOKED'}, {'status': 'EXPIRED'}, {'expires_at': self.now}, {'caregiver_id': 'other'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_acceptance(dict(self.binding, **change), 'caregiver', self.now)
        with self.assertRaises(ValueError):
            validate_acceptance(self.binding, 'patient', self.now)

    def test_retry_is_allowed_only_for_same_caregiver(self):
        active = dict(self.binding, status='ACTIVE', caregiver_id='caregiver')
        validate_acceptance(active, 'caregiver', self.now)
        with self.assertRaises(ValueError):
            validate_acceptance(active, 'other', self.now)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        install_family_routes(self.app, lambda: None, namespace['FamilyBindingSyncDto'], namespace['FamilyMessageSyncDto'])
        self.client = TestClient(self.app)

    def test_unauthenticated_family_routes_fail_closed(self):
        self.assertEqual(401, self.client.get('/api/family/bindings/victim').status_code)
        self.assertEqual(401, self.client.get('/api/family/messages/victim').status_code)
        self.assertEqual(401, self.client.post('/api/family/accept', json=dict(code='ABCD1234', caregiverName='Ana', caregiverRelation='Filha')).status_code)

    def test_wrong_owner_rejected_and_outage_never_reports_success(self):
        self.app.dependency_overrides[firebase_uid] = lambda: 'caregiver'
        self.assertEqual(403, self.client.get('/api/family/bindings/victim').status_code)
        self.assertEqual(503, self.client.get('/api/family/bindings/caregiver').status_code)
        self.assertEqual(503, self.client.post('/api/family/accept', json=dict(code='ABCD1234', caregiverName='Ana', caregiverRelation='Filha')).status_code)


if __name__ == '__main__':
    unittest.main()
