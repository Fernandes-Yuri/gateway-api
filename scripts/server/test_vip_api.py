import unittest
from unittest.mock import AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from vip_api import install_vip_routes, normalize_signup


class Acquire:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *args): pass


class Pool:
    def __init__(self): self.conn = AsyncMock()
    def acquire(self): return Acquire(self.conn)


class VipTests(unittest.TestCase):
    def setUp(self):
        self.pool = Pool()
        self.app = FastAPI()
        install_vip_routes(self.app, lambda: self.pool)
        self.client = TestClient(self.app)
        self.body = {'fullName': " Ana O'Brien ", 'email': ' ANA@EXAMPLE.COM '}
        self.url = '/api/public/vip-signups'

    def test_parameterized_and_identical_response_for_duplicates(self):
        self.pool.conn.execute.side_effect = ['INSERT 0 1', 'INSERT 0 0']
        first = self.client.post(self.url, json=self.body)
        duplicate = self.client.post(self.url, json=self.body)
        self.assertEqual(200, first.status_code)
        self.assertEqual(first.json(), duplicate.json())
        sql, name, email = self.pool.conn.execute.call_args.args
        self.assertEqual("Ana O'Brien", name)
        self.assertEqual('ana@example.com', email)
        self.assertNotIn(name, sql)
        self.assertIn('ON CONFLICT(email) DO NOTHING', sql)

    def test_no_public_list(self):
        self.assertEqual(405, self.client.get(self.url).status_code)

    def test_validation_rejects_bad_payload(self):
        for data in ({}, {'fullName': 'Ana', 'email': 'bad'}, {'fullName': 'x' * 256, 'email': 'a@b.co'}, []):
            self.assertEqual(422, self.client.post(self.url, json=data).status_code)
        self.pool.conn.execute.assert_not_called()

    def test_origin_and_size(self):
        self.assertEqual(403, self.client.post(self.url, json=self.body, headers={'origin': 'https://evil.example'}).status_code)
        self.assertEqual(413, self.client.post(self.url, content='x'*4097, headers={'content-type': 'application/json'}).status_code)
        self.assertEqual(200, self.client.post(self.url, json=self.body, headers={'origin': 'https://braga-saude.web.app'}).status_code)

    def test_outage_not_success_or_leaked_database_error(self):
        self.pool.conn.execute.side_effect = RuntimeError('private database details')
        res = self.client.post(self.url, json=self.body)
        self.assertEqual(503, res.status_code)
        self.assertNotIn('private database', res.text)
        self.pool = None
        self.assertEqual(503, self.client.post(self.url, json=self.body).status_code)

    def test_limit_enforced_by_server(self):
        for _ in range(10): self.client.post(self.url, json=self.body)
        response = self.client.post(self.url, json=self.body)
        self.assertEqual(429, response.status_code)
        self.assertEqual('600', response.headers['retry-after'])

    def test_health_does_not_return_contact_data(self):
        self.assertEqual({'status':'ready', 'vip_contract':1}, self.client.get(self.url+'/health').json())


if __name__ == '__main__': unittest.main()
