"""Owner-local validation and guarded rotation. Never expose credential bodies."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import os
import subprocess

PROFILE = re.compile(r'chatgpt-actuator(?:-[a-z0-9][a-z0-9_-]{0,47})?\Z')
ORG = re.compile(r'org-[A-Za-z0-9]{8,64}\Z')
TUNNEL = re.compile(r'tunnel_[a-z0-9]{32}\Z')
CODES = {'mismatched_organization', 'invalid_api_key', 'tunnel_active_organization_required',
         'tunnel_use_forbidden', 'tunnel_not_found', 'rate_limit_exceeded'}

class ValidationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def validate_profile(profile, settings):
    if not PROFILE.fullmatch(profile) or not isinstance(settings, dict):
        raise ValueError('Invalid profile')
    if not ORG.fullmatch(settings.get('organization_id', '')):
        raise ValueError('Invalid Organization ID')
    if not TUNNEL.fullmatch(settings.get('tunnel_id', '')):
        raise ValueError('Invalid Tunnel ID')
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}/healthz', settings.get('health_url', '')):
        raise ValueError('Health endpoint must be loopback')
    port = int(settings['health_url'].split(':')[2].split('/')[0])
    if not 1 <= port <= 65535:
        raise ValueError('Invalid port')

def validate_key(key, settings, client=None):
    """Read metadata only. Never poll: polling could consume live MCP requests."""
    validate_profile('chatgpt-actuator', settings)
    if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValidationError('invalid_key_input')
    import httpx
    own = client is None
    if own:
        client = httpx.Client(timeout=15, trust_env=False, follow_redirects=False)
    try:
        with client.stream('GET', 'https://api.openai.com/v1/tunnels/'+settings['tunnel_id'],
            headers={'Authorization': 'Bearer '+key, 'OpenAI-Organization': settings['organization_id']}) as response:
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise ValidationError('oversized_response')
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                body = {}
            if response.status_code != 200:
                error = body.get('error', {}) if isinstance(body, dict) else {}
                code = error.get('code') if isinstance(error, dict) else None
                raise ValidationError(code if code in CODES else 'http_'+str(response.status_code))
            if not isinstance(body, dict) or body.get('id') != settings['tunnel_id']:
                raise ValidationError('metadata_mismatch')
            return {'state': 'verified', 'kind': 'remote_metadata',
                    'checked_at': datetime.now(timezone.utc).isoformat(), 'mcp_e2e': 'unverified'}
    except httpx.HTTPError:
        raise ValidationError('network_or_tls_error') from None
    finally:
        if own:
            client.close()

def atomic(path, data, expected):
    path = Path(path)
    current = path.read_bytes() if path.exists() else None
    if current != expected:
        raise RuntimeError('Concurrent configuration change; stopped')
    temp = path.with_name(path.name+'.rotation.tmp')
    with temp.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)

@contextmanager
def rotation_lock(home):
    lock = Path(home)/'deployment/tunnel-owner.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink()

def protect_key(key):
    """Use the same current-user PowerShell DPAPI format as the existing service."""
    if os.name != 'nt':
        raise RuntimeError('Windows DPAPI required')
    exe = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    env = dict(os.environ)
    env['CONTROL_PLANE_API_KEY'] = key
    code = "$s=ConvertTo-SecureString $env:CONTROL_PLANE_API_KEY -AsPlainText -Force; try { ConvertFrom-SecureString $s } finally { $s.Dispose(); Remove-Item Env:CONTROL_PLANE_API_KEY }"
    try:
        result = subprocess.run([str(exe), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', code],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    finally:
        env.pop('CONTROL_PLANE_API_KEY', None)
    protected = result.stdout.strip()
    if result.returncode or not re.fullmatch(rb'[0-9a-fA-F]{64,32768}', protected):
        raise RuntimeError('DPAPI protection failed')
    return protected

def rotate(home, profile, settings, key, restart, *, validator=validate_key, protector=protect_key):
    """Validate before writes; rollback stays in owner process memory, never an export."""
    validate_profile(profile, settings)
    home = Path(home)
    keypath = (home/'deployment/control-plane-key.dpapi' if profile == 'chatgpt-actuator'
               else home/'deployment/keys'/(profile+'.dpapi'))
    config = home/'deployment/tunnel-organizations.json'
    if not keypath.is_file() or not config.is_file():
        raise RuntimeError('Install owner profile settings first')
    with rotation_lock(home):
        old_key, old_config = keypath.read_bytes(), config.read_bytes()
        raw = json.loads(old_config)
        if profile not in raw.get('profiles', {}):
            raise RuntimeError('Unknown owner profile')
        evidence = validator(key, settings)
        protected = protector(key)
        raw['profiles'][profile] = dict(settings)
        new_config = json.dumps(raw, indent=2).encode('utf-8')
        atomic(config, new_config, old_config)
        key_written = False
        try:
            atomic(keypath, protected, old_key)
            key_written = True
            restart(profile, settings['health_url'])
        except BaseException:
            if key_written:
                atomic(keypath, old_key, protected)
            atomic(config, old_config, new_config)
            if key_written:
                try:
                    restart(profile, json.loads(old_config)['profiles'][profile]['health_url'])
                except Exception:
                    pass
            raise RuntimeError('Rotation failed; original configuration restored') from None
        return {'state': 'local_restart_passed', 'metadata': evidence, 'mcp_e2e': 'awaiting_remote_verification'}
