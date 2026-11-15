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
EXPECTED_TOOL_COUNT = 166
REQUIRED_CORE_TOOLS = frozenset({
    'actuator__system_info', 'actuator__filesystem_list_roots',
    'actuator__filesystem_read_text', 'actuator__filesystem_write_text',
    'actuator__batch_preview', 'actuator__security_policy_info',
})
REQUIRED_SCH3_TOOLS = frozenset({
    'actuator__eda_altium_sch_check_pin_contracts', 'actuator__eda_altium_sch_diff_connectivity',
    'actuator__eda_altium_sch_lint', 'actuator__eda_altium_sch_erc',
    'actuator__eda_altium_sch_edit_commit', 'actuator__eda_altium_sch_edit_restore',
})


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
            env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP') if os.environ.get(key)}
            env.update(PYTHONPATH=str(ROOT / 'src'), CHATGPT_ACTUATOR_CONFIG=str(config_path),
                       CHATGPT_DOM_BROKER_TOKEN_FILE=str(temp / 'dom-token'), ACTUATOR_MCP_TRANSPORT='stdio')
            processes = []
            jobs = []
            captures = []
            failure = None
            try:
                for script in ('run_actuator_http.py', 'run_gateway.py'):
                    from chatgpt_actuator.windows_job import WindowsJob
                    from chatgpt_actuator.windows_process import WindowsProcess
                    from chatgpt_actuator.background_process import Capture

                    argv = [sys.executable, str(ROOT / 'integrations' / 'tailscale_gateway' / script),
                            '--profiles', str(path), '--profile', 'tailscale']
                    process = WindowsProcess(argv, str(ROOT), env, None)
                    processes.append(process)
                    out = Capture(process.stdout, 1024 * 1024)
                    err = Capture(process.stderr, 1024 * 1024)
                    captures.append((out, err))
                    job = WindowsJob(process)
                    jobs.append(job)
                    # WindowsProcess creates the child suspended; assign its
                    # tree to the kill-on-close Job before any code can run.
                    process.resume()

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
                            self.fail('Isolated gateway/backend did not start')
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
                    tool_names = {tool['name'] for tool in tools}
                    self.assertEqual(len(tools), EXPECTED_TOOL_COUNT, f'Expected {EXPECTED_TOOL_COUNT} r2 foundation and SCH3 tool registrations; actual names: {sorted(tool_names)}')
                    self.assertFalse(REQUIRED_CORE_TOOLS - tool_names, f'Missing upstream core tools: {sorted(REQUIRED_CORE_TOOLS - tool_names)}')
                    self.assertFalse(REQUIRED_SCH3_TOOLS - tool_names, f'Missing SCH3 tools: {sorted(REQUIRED_SCH3_TOOLS - tool_names)}')
                    self.assertIn('actuator__system_info', tool_names)
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
            except Exception as exc:
                failure = exc
            finally:
                # Closing each kill-on-close Job stops only this fixture's tree.
                cleanup_failure = None
                for job in jobs:
                    try:
                        job.close()
                    except OSError as exc:
                        cleanup_failure = cleanup_failure or exc
                for process in processes:
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        try:
                            process.terminate()
                        except OSError:
                            # The Job may finish process termination between
                            # the wait timeout and this call.
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired as exc:
                            cleanup_failure = cleanup_failure or exc
                try:
                    for out, err in captures:
                        out.thread.join(timeout=2)
                        err.thread.join(timeout=2)
                        out.pipe.close()
                        err.pipe.close()
                        out.thread.join(timeout=1)
                        err.thread.join(timeout=1)
                finally:
                    for process in processes:
                        process.close()
                if cleanup_failure is not None and failure is None:
                    failure = cleanup_failure
            if failure is not None:
                logs = []
                for out, err in captures:
                    logs.append('stdout:\n' + out.snapshot()[0].decode('utf-8', errors='replace'))
                    logs.append('stderr:\n' + err.snapshot()[0].decode('utf-8', errors='replace'))
                raise AssertionError(f'{failure}\nIsolated runtime capture:\n' + '\n'.join(logs)) from failure
