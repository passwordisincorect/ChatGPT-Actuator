"""OAuth and gateway boundaries with temporary state and a fake loopback backend."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('gateway', 'gateway_full', 'gateway_config', 'gateway_guard', 'owner_ui')
CALLBACK = 'https://chatgpt.com/connector_platform_oauth_redirect'


class GatewaySecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved_modules = {name: sys.modules.pop(name, None) for name in MODULES}
        self.addCleanup(self.restore_modules)
        self.gateway_path = str(ROOT / 'integrations' / 'tailscale_gateway')
        sys.path.insert(0, self.gateway_path)
        self.addCleanup(sys.path.remove, self.gateway_path)
        sample = json.loads((ROOT / 'config' / 'connection-profiles.example.json').read_text())
        sample['profiles']['tailscale'].update(issuer='https://fixture.test.ts.net', state_dir=self.tmp.name)
        path = Path(self.tmp.name) / 'profiles.json'
        path.write_text(json.dumps(sample))
        env = patch.dict(os.environ, CHATGPT_ACTUATOR_PROFILE_FILE=str(path), CHATGPT_ACTUATOR_PROFILE='tailscale')
        env.start()
        self.addCleanup(env.stop)
        self.g = importlib.import_module('gateway')
        self.g.initialize_db()
        self.public = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.g.audited_public_app), base_url=self.g.ISSUER)
        self.owner = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.g.RequestGuard(self.g.admin_app)), base_url=f'http://127.0.0.1:{self.g.ADMIN_PORT}')
        self.addAsyncCleanup(self.public.aclose)
        self.addAsyncCleanup(self.owner.aclose)
        self.calls = []

        async def backend(url, payload, headers):
            self.calls.append((url, payload, headers))
            method = payload['method']
            if method == 'tools/list':
                result = {'tools': [{'name': name, 'inputSchema': {'type': 'object'}, 'description': name} for name in ('system_info', 'filesystem_write_text', 'powershell_run')]}
            elif method == 'initialize':
                result = {'protocolVersion': '2026-07-28', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': 'test'}}
            elif method == 'tools/call':
                result = {'content': [{'type': 'text', 'text': 'BACKEND_OK'}], 'isError': False}
            else:
                result = {}
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': payload.get('id'), 'result': result})
        stub = patch.object(self.g.full, 'backend_post', backend)
        stub.start()
        self.addCleanup(stub.stop)

    def restore_modules(self):
        for name in MODULES:
            sys.modules.pop(name, None)
            if self.saved_modules[name] is not None:
                sys.modules[name] = self.saved_modules[name]

    async def register(self):
        r = await self.public.post('/oauth/register', json={'redirect_uris': [CALLBACK], 'scope': 'mcp:read mcp:write mcp:execute offline_access'})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()['client_id']

    async def authorize(self, resource=None, scope=None):
        client_id = await self.register()
        verifier = 'x' * 64
        params = {'response_type': 'code', 'client_id': client_id, 'redirect_uri': CALLBACK, 'code_challenge_method': 'S256', 'code_challenge': self.g.pkce_s256(verifier), 'state': 'fixture-state', 'resource': resource or self.g.FULL_RESOURCE, 'scope': scope or 'mcp:read mcp:write mcp:execute offline_access'}
        response = await self.public.get('/oauth/authorize', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        req_id = next(k for k, v in self.g.APPROVALS.items() if v['client_id'] == client_id)
        return client_id, verifier, params, req_id

    async def grant(self, resource=None, scope=None):
        client_id, verifier, params, req_id = await self.authorize(resource, scope)
        decision = await self.owner.post('/decision', headers={'Origin': str(self.owner.base_url).rstrip('/')}, data={'id': req_id, 'choice': 'approve', 'csrf': self.g.CSRF})
        self.assertEqual(decision.status_code, 303)
        status = await self.public.get('/oauth/status', params={'id': req_id})
        fields = parse_qs(urlsplit(status.headers['location']).query)
        self.assertEqual(fields['state'], ['fixture-state'])
        self.assertEqual(fields['iss'], [self.g.ISSUER])
        exchange = {'grant_type': 'authorization_code', 'code': fields['code'][0], 'code_verifier': verifier, 'client_id': client_id, 'redirect_uri': CALLBACK, 'resource': params['resource']}
        response = await self.public.post('/oauth/token', data=exchange)
        self.assertEqual(response.status_code, 200, response.text)
        return client_id, response.json(), exchange

    async def rpc(self, tokens, method='tools/list', params=None, extra_headers=None, route='/full/mcp'):
        return await self.public.post(route, headers={'Authorization': 'Bearer ' + tokens['access_token'], **(extra_headers or {})}, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})

    async def test_oauth_mcp_refresh_revoke(self):
        client_id, tokens, exchange = await self.grant()
        replay = await self.public.post('/oauth/token', data=exchange)
        self.assertEqual(replay.json()['error'], 'invalid_grant')
        initialized = await self.rpc(tokens, 'initialize')
        self.assertEqual(initialized.json()['result']['serverInfo']['name'], 'fixture')
        listing = await self.rpc(tokens)
        self.assertEqual({t['name'] for t in listing.json()['result']['tools']}, {'actuator__system_info', 'actuator__filesystem_write_text', 'actuator__powershell_run'})
        called = await self.rpc(tokens, 'tools/call', {'name': 'actuator__system_info', 'arguments': {}})
        self.assertFalse(called.json()['result']['isError'])
        self.assertEqual(self.calls[-1][1]['params']['name'], 'system_info')
        self.assertFalse(any(k.lower() == 'authorization' for k in self.calls[-1][2]))
        refresh = {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'], 'client_id': client_id, 'resource': self.g.FULL_RESOURCE}
        rotated = await self.public.post('/oauth/token', data=refresh)
        self.assertEqual(rotated.status_code, 200)
        self.assertEqual((await self.public.post('/oauth/token', data=refresh)).json()['error'], 'invalid_grant')
        revoked = await self.owner.post('/revoke-connection', headers={'Origin': str(self.owner.base_url).rstrip('/')}, data={'client_id': client_id, 'choice': 'revoke', 'csrf': self.g.CSRF})
        self.assertEqual(revoked.status_code, 303)
        self.assertEqual((await self.rpc(tokens)).status_code, 401)
        self.assertEqual((await self.rpc(rotated.json())).status_code, 401)
        refresh['refresh_token'] = rotated.json()['refresh_token']
        self.assertEqual((await self.public.post('/oauth/token', data=refresh)).status_code, 401)
        with self.g.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM tokens').fetchone()[0], 0)
        audit = self.g.AUDIT_LOG.read_text()
        for secret in (tokens['access_token'], tokens['refresh_token'], client_id, exchange['code']):
            self.assertNotIn(secret, audit)

    async def test_pkce_target_and_consent_cannot_be_bypassed(self):
        client_id, verifier, params, req_id = await self.authorize()
        denied = await self.owner.post('/decision', headers={'Origin': 'https://attacker.example'}, data={'id': req_id, 'choice': 'approve', 'csrf': self.g.CSRF})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.g.APPROVALS[req_id]['status'], 'pending')
        bad_csrf = await self.owner.post('/decision', headers={'Origin': str(self.owner.base_url).rstrip('/')}, data={'id': req_id, 'choice': 'approve', 'csrf': 'bad'})
        self.assertEqual(bad_csrf.status_code, 403)
        self.assertEqual((await self.owner.get('/', headers={'Host': 'attacker.example'})).status_code, 403)
        self.assertEqual((await self.public.get('/decision')).status_code, 404)
        # Duplicate OAuth parameters cannot alter the resource being approved.
        response = await self.public.get('/oauth/authorize', params=[*params.items(), ('resource', self.g.RESOURCE)])
        self.assertEqual(response.status_code, 400)
        _, tokens, exchange = await self.grant()
        # Create a fresh code to test wrong PKCE and wrong audience before consumption.
        client_id, verifier, params, req_id = await self.authorize()
        await self.owner.post('/decision', headers={'Origin': str(self.owner.base_url).rstrip('/')}, data={'id': req_id, 'choice': 'approve', 'csrf': self.g.CSRF})
        status = await self.public.get('/oauth/status', params={'id': req_id})
        code = parse_qs(urlsplit(status.headers['location']).query)['code'][0]
        form = {'grant_type': 'authorization_code', 'client_id': client_id, 'redirect_uri': CALLBACK, 'resource': self.g.FULL_RESOURCE, 'code': code, 'code_verifier': 'y' * 64}
        self.assertEqual((await self.public.post('/oauth/token', data=form)).json()['error'], 'invalid_grant')
        form.update(code_verifier=verifier, resource=self.g.RESOURCE)
        self.assertEqual((await self.public.post('/oauth/token', data=form)).json()['error'], 'invalid_grant')
        form['resource'] = self.g.FULL_RESOURCE
        self.assertEqual((await self.public.post('/oauth/token', data=form)).status_code, 200)
        self.assertEqual((await self.rpc(tokens, route='/mcp')).status_code, 401)
        _, readonly, _ = await self.grant(self.g.RESOURCE, 'mcp:read')
        self.assertEqual((await self.rpc(readonly)).status_code, 401)

    async def test_one_time_approval_bound_to_token_tool_arguments(self):
        client_id, tokens, _ = await self.grant()
        args = {'path': 'fixture.txt', 'text': 'hello'}
        params = {'name': 'actuator__filesystem_write_text', 'arguments': args}
        pending = await self.rpc(tokens, 'tools/call', params)
        self.assertIn('Approve request', pending.json()['result']['content'][0]['text'])
        self.assertEqual(sum(payload['method'] == 'tools/call' for _, payload, _ in self.calls), 0)
        key = next(iter(self.g.full.PENDING))
        approved = await self.owner.post('/full-decision', headers={'Origin': str(self.owner.base_url).rstrip('/')}, data={'id': key, 'choice': 'approve', 'csrf': self.g.CSRF})
        self.assertEqual(approved.status_code, 303)
        altered = await self.rpc(tokens, 'tools/call', {**params, 'arguments': {'path': 'other.txt'}})
        self.assertTrue(altered.json()['result']['isError'])
        _, other, _ = await self.grant()
        self.assertTrue((await self.rpc(other, 'tools/call', params)).json()['result']['isError'])
        executed = await self.rpc(tokens, 'tools/call', params)
        self.assertFalse(executed.json()['result']['isError'])
        self.assertEqual(self.calls[-1][1]['params'], {'name': 'filesystem_write_text', 'arguments': args})
        self.assertTrue((await self.rpc(tokens, 'tools/call', params)).json()['result']['isError'])
        self.assertEqual(sum(payload['method'] == 'tools/call' for _, payload, _ in self.calls), 1)

    async def test_public_guard_and_protocol_headers(self):
        self.assertEqual((await self.public.post('/full/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})).status_code, 401)
        _, tokens, _ = await self.grant()
        self.assertEqual((await self.rpc(tokens, extra_headers={'Origin': 'https://attacker.example'})).status_code, 403)
        self.assertEqual((await self.rpc(tokens, extra_headers={'Mcp-Param-path': 'secret'})).status_code, 400)
        self.assertEqual((await self.rpc(tokens, extra_headers={'Mcp-Protocol-Version': '2026-07-28', 'Mcp-Method': 'tools/call'})).status_code, 400)
        hdrs = [('Authorization', 'Bearer ' + tokens['access_token']), ('Authorization', 'Bearer ' + tokens['access_token'])]
        self.assertEqual((await self.public.post('/full/mcp', headers=hdrs, json={'method': 'tools/list'})).status_code, 400)
        self.assertEqual((await self.public.post('/full/mcp', headers={'Authorization': 'Bearer ' + tokens['access_token']}, json=[{'method': 'tools/list'}])).status_code, 403)
        self.assertEqual((await self.public.post('/full/mcp', content=b'x' * (1024 * 1024 + 1))).status_code, 413)
        denied = await self.public.post('/oauth/register', json={'redirect_uris': ['https://attacker.example/callback']})
        self.assertEqual(denied.status_code, 400)
        before = len(self.calls)
        with self.g.db() as c:
            c.execute('UPDATE tokens SET expires=? WHERE tokenhash=?', (self.g.stamp(), self.g.digest(tokens['access_token'])))
        self.assertEqual((await self.rpc(tokens)).status_code, 401)
        self.assertEqual(before, len(self.calls))


if __name__ == '__main__':
    unittest.main()
