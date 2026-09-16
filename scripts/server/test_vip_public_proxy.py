import unittest
import httpx
from fastapi.testclient import TestClient
from vip_public_proxy import create_proxy


class PublicProxyTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        def handle(request):
            self.requests.append(request)
            return httpx.Response(200, json={'success': True}, headers={'access-control-allow-origin': '*', 'retry-after':'600'})
        self.client = TestClient(create_proxy(httpx.MockTransport(handle)))

    def test_gateway_and_docs_are_not_exposed(self):
        for path in ('/health', '/docs', '/openapi.json', '/api/profile/uid', '/api/vitals/uid', '/api/auth/check-account'):
            self.assertEqual(404, self.client.get(path).status_code)
        self.assertEqual([], self.requests)

    def test_only_allowed_routes_and_headers_reach_gateway(self):
        result = self.client.post('/api/public/vip-signups', json={'fullName':'Ana','email':'a@b.co'},
                                  headers={'authorization':'Bearer must-not-forward','x-forwarded-for':'fake', 'origin':'https://braga-saude.web.app'})
        self.assertEqual(200,result.status_code)
        self.assertEqual('http://127.0.0.1:8080/api/public/vip-signups',str(self.requests[0].url))
        self.assertNotIn('authorization',self.requests[0].headers)
        self.assertNotIn('x-forwarded-for',self.requests[0].headers)
        self.assertEqual('*',result.headers['access-control-allow-origin'])
        self.assertEqual('no-store',result.headers['cache-control'])

    def test_rejects_oversize_and_listing(self):
        self.assertEqual(413,self.client.post('/api/public/vip-signups',content='x'*4097).status_code)
        self.assertEqual(405,self.client.get('/api/public/vip-signups').status_code)
        self.assertEqual([],self.requests)
