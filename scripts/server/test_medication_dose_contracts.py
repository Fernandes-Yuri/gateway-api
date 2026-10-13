"""Limites públicos de DTOs, fuso e versionamento do OpenAPI."""
from datetime import date, datetime
import unittest
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError

from generate_care_os_openapi import build_spec
from schemas.medication_doses import DoseBatchConfirm, DoseBatchPrepare, DoseScheduleUpdate
from services.medication_doses import instant_for, occurrence_id


class DoseContractTests(unittest.TestCase):
    def test_canonical_id_does_not_depend_on_offset(self):
        mid = uuid4()
        a = datetime.fromisoformat('2026-10-13T08:00:00-03:00')
        b = datetime.fromisoformat('2026-10-13T11:00:00+00:00')
        self.assertEqual(occurrence_id('p', mid, a), occurrence_id('p', mid, b))

    def test_quantities_require_explicit_integer_stock_units_and_unique_times(self):
        for slots in ([{'local_time':'08:00','quantity_confirmed':True}],
                      [{'local_time':'08:00','dose_units':1.5}],
                      [{'local_time':'08:00','dose_units':True}],
                      [{'local_time':'08:00'}, {'local_time':'08:00'}],
                      [{'local_time':'25:00'}]):
            with self.assertRaises(ValidationError):
                DoseScheduleUpdate(expected_revision=1, slots=slots)

    def test_no_actor_override_or_duplicate_occurrences(self):
        oid = uuid4()
        base = dict(client_batch_id=uuid4(), group_id=uuid4(), expected_schedule_digest='a'*64, channel='app')
        with self.assertRaises(ValidationError):
            DoseBatchPrepare(**base, occurrence_ids=[oid], actor_id='forged')
        with self.assertRaises(ValidationError):
            DoseBatchPrepare(**base, occurrence_ids=[oid, oid])
        for ids in ([], [oid, oid]):
            with self.assertRaises(ValidationError):
                DoseBatchConfirm(confirmation_id=uuid4(),snapshot_digest='a'*64, selected_occurrence_ids=ids)
        with self.assertRaises(ValidationError):
            DoseBatchConfirm(confirmation_id=uuid4(),snapshot_digest='a'*64, selected_occurrence_ids=[oid], declared_taken_at='2026-10-13T08:00')

    def test_dst_ambiguity_requires_review(self):
        for day, hour in ((date(2026,3,8),'02:30'), (date(2026,11,1),'01:30')):
            with self.assertRaises(HTTPException) as exc:
                instant_for(day, hour, ZoneInfo('America/New_York'))
            self.assertEqual(exc.exception.detail['code'], 'REVIEW_REQUIRED')

    def test_openapi_new_routes_are_authenticated_and_typed(self):
        spec = build_spec()
        routes = {p: v for p, v in spec['paths'].items() if 'medication-dose-' in p or '/dose-schedule' in p}
        self.assertEqual(len(routes), 7)
        for path, operations in routes.items():
            for operation in operations.values():
                self.assertEqual(operation['security'], [{'FirebaseBearer':[]}])
                if 'capabilities' not in path:
                    schema = next(v for k, v in operation['responses'].items() if k in ('200','201'))['content']['application/json']['schema']
                    self.assertIn('$ref', schema)
                    self.assertIn('409', operation['responses'])
