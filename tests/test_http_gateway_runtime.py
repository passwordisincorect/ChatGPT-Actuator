"""Windows integration: fresh state, real HTTP Actuator and packaged gateway.

Uses only loopback, synthetic OAuth credentials and read-only tool calls.
Never touches the installed Funnel or OpenAI Tunnel.
"""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]


def ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


@unittest.skipUnless(sys.platform == 'win32', 'Real Actuator runtime requires Windows')
class HTTPGatewayRuntimeTests(unittest.TestCase):
    def test_fresh_install_oauth_real_mcp_and_revocation(self):
        with tempfile.TemporaryDirectory(prefix='actuator-http-gateway-') as td:
            temp = Path(td)
            backend_port, public_port, owner_port, admin_port = ports(4)
            raw = json.loads((ROOT / 'config' / 'config.json').read_text(encoding='utf-8-sig'))
            raw['filesystem']['allowed_roots'] = [str(temp)]
            raw['filesystem']['read_only'] = True
            raw['powershell']['default_cwd'] = str(temp)
            raw['powershell']['allowed_working_roots'] = [str(temp)]
            raw['powershell']['enabled'] = False
            raw['powershell']['allow_execute'] = False
            raw['browser_dom']['launch_if_missing'] = False
            raw['browser_dom']['auto_allow_remote_debugging'] = False
            raw['logging']['file'] = str(temp / 'actuator.log')
            raw['eda']['allowed_project_roots'] = [str(temp)]
            config_path = temp / 'config.json'
            config_path.write_text(json.dumps(raw), encoding='utf-8')
            profile = json.loads((ROOT / 'config' / 'connection-profiles.example.json').read_text())
            profile['profiles']['tailscale'].update(issuer='https://fixture.test.ts.net', public_port=public_port, owner_port=owner_port, actuator_admin_port=admin_port, actuator_backend=f'http://127.0.0.1:{backend_port}/mcp', state_dir=str(temp / 'state'))
            path = temp / 'profiles.json'
            path.write_text(json.dumps(profile), encoding='utf-8')
            env = dict(os.environ, CHATGPT_ACTUATOR_CONFIG=str(config_path), CHATGPT_DOM_BROKER_TOKEN_FILE=str(temp / 'dom-token'))
            processes = []
            with (temp / 'runtime.log').open('w', encoding='utf-8') as log:
                try:
                    for script in ('run_actuator_http.py', 'run_gateway.py'):
                        process = subprocess.Popen([sys.executable, str(ROOT / 'integrations' / 'tailscale_gateway' / script), '--profiles', str(path), '--profile', 'tailscale'], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                        processes.append(process)
                    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                        deadline = time.monotonic() + 25
                        while True:
                            try:
                                health = client.get(f'http://127.0.0.1:{public_port}/healthz')
                                backend = client.post(f'http://127.0.0.1:{backend_port}/mcp', headers={'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                                if health.status_code == 200 and backend.status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            if any(p.poll() is not None for p in processes) or time.monotonic() >= deadline:
                                self.fail('Isolated gateway/backend did not start: ' + (temp / 'runtime.log').read_text())
                            time.sleep(0.1)
                        public = f'http://127.0.0.1:{public_port}'
                        owner = f'http://127.0.0.1:{owner_port}'
                        resource = 'https://fixture.test.ts.net/full/mcp'
                        callback = 'https://chatgpt.com/connector_platform_oauth_redirect'
                        denied = client.post(public + '/full/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                        self.assertEqual(denied.status_code, 401)
                        registration = client.post(public + '/oauth/register', json={'redirect_uris': [callback], 'scope': 'mcp:read mcp:write mcp:execute'})
                        self.assertEqual(registration.status_code, 201)
                        client_id = registration.json()['client_id']
                        import hashlib, base64
                        verifier = 'x' * 64
                        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                        consent = client.get(public + '/oauth/authorize', params={'client_id': client_id, 'redirect_uri': callback, 'response_type': 'code', 'scope': 'mcp:read mcp:write mcp:execute', 'resource': resource, 'state': 'runtime-test', 'code_challenge_method': 'S256', 'code_challenge': challenge})
                        self.assertEqual(consent.status_code, 200)
                        req_id = re.search(r'/oauth/status\?id=([A-Za-z0-9_-]+)', consent.text).group(1)
                        console = client.get(owner + '/')
                        csrf = re.search(r"name='csrf' value='([^']+)'", console.text).group(1)
                        approve = client.post(owner + '/decision', headers={'Origin': owner}, data={'id': req_id, 'csrf': csrf, 'choice': 'approve'})
                        self.assertEqual(approve.status_code, 303)
                        redirect = client.get(public + '/oauth/status', params={'id': req_id})
                        code = parse_qs(urlsplit(redirect.headers['location']).query)['code'][0]
                        exchange = client.post(public + '/oauth/token', data={'grant_type': 'authorization_code', 'client_id': client_id, 'redirect_uri': callback, 'resource': resource, 'code': code, 'code_verifier': verifier})
                        self.assertEqual(exchange.status_code, 200)
                        headers = {'Authorization': 'Bearer ' + exchange.json()['access_token']}
                        def rpc(method, params):
                            return client.post(public + '/full/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 10, 'method': method, 'params': params})
                        initialized = rpc('initialize', {'protocolVersion': '2026-07-28', 'capabilities': {}, 'clientInfo': {'name': 'gateway-test', 'version': '1'}})
                        self.assertEqual(initialized.status_code, 200, initialized.text)
                        self.assertNotIn('error', initialized.json(), initialized.text)
                        self.assertEqual(rpc('notifications/initialized', {}).status_code, 202)
                        listing = rpc('tools/list', {})
                        self.assertEqual(listing.status_code, 200, listing.text)
                        tools = listing.json()['result']['tools']
                        self.assertEqual(len(tools), 73)
                        self.assertIn('actuator__system_info', {t['name'] for t in tools})
                        called = rpc('tools/call', {'name': 'actuator__system_info', 'arguments': {}})
                        self.assertEqual(called.status_code, 200, called.text)
                        self.assertFalse(called.json()['result'].get('isError'), called.text)
                        pending = rpc('tools/call', {'name': 'actuator__filesystem_write_text', 'arguments': {'path': str(temp / 'not-written.txt'), 'text': 'test'}})
                        self.assertTrue(pending.json()['result']['isError'])
                        self.assertIn('Approve request', pending.json()['result']['content'][0]['text'])
                        self.assertFalse((temp / 'not-written.txt').exists())
                        revoked = client.post(owner + '/revoke-connection', headers={'Origin': owner}, data={'client_id': client_id, 'csrf': csrf, 'choice': 'revoke'})
                        self.assertEqual(revoked.status_code, 303)
                        self.assertEqual(rpc('tools/list', {}).status_code, 401)
                finally:
                    for process in processes:
                        if process.poll() is None:
                            process.terminate()
                    for process in processes:
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
