"""Run with unittest; no PostgreSQL server or account is created."""
import ast
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI  # noqa: F401 (mantido para compatibilidade de namespace)
from pydantic import BaseModel, Field, model_validator
from security_telemetry import mark_signal

from profile_contract import validate_profile, profile_upsert


# Load the actual DTO declarations without starting the gateway's AI services.
# ProfileSyncDto migrou para routers/sync.py na Fase A passo 5. Os DTOs de chat
# familiar/E2EE foram removidos na Fase #6 (chat vive no Go braga-chat), restando
# apenas o contrato de perfil neste alinhamento.
names = {'ProfileSyncDto'}
namespace = dict(BaseModel=BaseModel, Field=Field, Optional=Optional, List=List,
                 model_validator=model_validator, mark_signal=mark_signal)
for _src in ('server.py', 'routers/sync.py'):
    tree = ast.parse((Path(__file__).parent / _src).read_text(encoding='utf-8'))
    _classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
    if _classes:
        exec(compile(ast.Module(_classes, type_ignores=[]), _src, 'exec'), namespace)
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

    def test_consent_allergies_survive_and_retired_calories_are_ignored(self):
        p = Profile(id='uid', fullName="O'Brien", consentAcceptedAt='2026-09-08T10:00:00-03:00', foodAllergies=['Ovos'], dailyCalorieTarget=1800.5)
        sql, values = profile_upsert(p)
        self.assertNotIn(p.fullName, sql)
        self.assertIn(p.fullName, values)
        self.assertIn(['Ovos'], values)
        self.assertNotIn(1800.5, values)
        self.assertNotIn("daily_calorie_target", sql)
        self.assertTrue(any(isinstance(v, datetime) and v.utcoffset() == timedelta(hours=-3) for v in values))


if __name__ == '__main__':
    unittest.main()
