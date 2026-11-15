"""Local control of the existing shared OAuth Funnel; never expose private backends."""
from __future__ import annotations
from pathlib import Path
from threading import RLock
import json
import os
import subprocess
import time
import urllib.request

class FunnelControl:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.gateway = self.home.parent / 'MCP-Funnel-Gateway'
        self.cli = Path(r'C:\Program Files\Tailscale\tailscale.exe')
        self._lock = RLock()
        self._known_host = ""
        self._cache = None
        self._cached_at = 0.0

    def _run(self, *args: str) -> str:
        result = subprocess.run([str(self.cli), *args], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError('Tailscale command failed; check the local Tailscale service and permissions.')
        return result.stdout

    def _config(self) -> dict:
        value = json.loads(self._run('funnel', 'status', '--json'))
        if not isinstance(value, dict):
            raise RuntimeError('Invalid Tailscale configuration response.')
        return value

    @staticmethod
    def _mapping(config: dict) -> tuple[str, bool, bool]:
        web = config.get('Web') or {}
        mappings = [(host, item) for host, item in web.items() if host.endswith(':443')]
        if not mappings:
            # A different TCP listener must not be overwritten.
            return '', False, not bool((config.get('TCP') or {}).get('443'))
        if len(mappings) != 1:
            return '', False, False
        host, item = mappings[0]
        import re
        if not re.fullmatch(r'[a-zA-Z0-9.-]+\.ts\.net:443', host):
            return '', False, False
        expected = ((config.get('TCP') or {}).get('443') == {'HTTPS': True}
            and item.get('Handlers') == {'/': {'Proxy': 'http://127.0.0.1:8188'}})
        return host[:-4], (config.get('AllowFunnel') or {}).get(host) is True, expected

    def _gateway_ready(self) -> bool:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8188/healthz', timeout=2) as r:
                value=json.loads(r.read(8192))
                return r.status == 200 and value.get('ready') is True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _backend_port(port: int) -> bool:
        import socket
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return True
        except OSError:
            return False

    def status(self, force=False) -> dict:
        with self._lock:
            if not force and self._cache is not None and time.monotonic()-self._cached_at < 10:
                return dict(self._cache)
            result={'installed': self.cli.is_file(), 'enabled': None, 'config_matches': False,
                'shared': True, 'gateway_ready': self._gateway_ready(), 'checked_at': time.time(),
                'public_state': 'unverified', 'public_checked_at': None,
                'base_url': '', 'actuator_url': '', 'actuator_full_url': '', 'orchestrator_url': '',
                'last_action': None, 'error': '',
                'actuator_backend_listening': self._backend_port(8090),
                'orchestrator_backend_listening': self._backend_port(8092)}
            try:
                host, enabled, expected=self._mapping(self._config())
                result.update(enabled=enabled, config_matches=expected)
                if host:
                    self._known_host=host
                elif not self._known_host:
                    node=json.loads(self._run('status','--json'))
                    name=str((node.get('Self') or {}).get('DNSName') or '').rstrip('.')
                    import re
                    if re.fullmatch(r'[a-zA-Z0-9.-]+\.ts\.net',name):
                        self._known_host=name
                if self._known_host:
                    base='https://'+self._known_host
                    result.update(base_url=base, actuator_url=base+'/mcp',
                        actuator_full_url=base+'/full/mcp', orchestrator_url=base+'/orch/mcp')
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                result['error']='Unable to read Funnel status. Check Tailscale is running.'
            try:
                proof=json.loads((self.gateway/'public-health-current.json').read_text(encoding='utf-8'))
                stamp=proof.get('checked_at')
                if isinstance(stamp,(int,float)) and 0 <= time.time()-stamp < 900:
                    result.update(public_state=str(proof.get('state') or 'unverified'),public_checked_at=stamp)
            except (OSError, ValueError, TypeError):
                pass
            try:
                result['last_action']=json.loads((self.home/'deployment/funnel-last-action.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pass
            self._cache=result
            self._cached_at=time.monotonic()
            return dict(result)

    def change(self, action: str) -> dict:
        if action not in {'enable','disable'}:
            raise ValueError('Unsupported Funnel action.')
        with self._lock:
            config=self._config()
            _, enabled, expected=self._mapping(config)
            if not expected:
                raise RuntimeError('Port 443 has an unexpected configuration. Refusing to overwrite another service.')
            if action=='enable' and not self._gateway_ready():
                raise RuntimeError('OAuth Gateway on 127.0.0.1:8188 is not ready. Start it before enabling Funnel.')
            if enabled == (action=='enable'):
                return self.status(force=True)
            checkpoint=self.home/'deployment/funnel-checkpoints'
            checkpoint.mkdir(parents=True,exist_ok=True)
            (checkpoint/(str(time.time_ns())+'.json')).write_text(json.dumps(config,indent=2),encoding='utf-8')
            if action=='enable':
                self._run('funnel','--bg','--yes','--https=443','http://127.0.0.1:8188')
            else:
                self._run('funnel','--https=443','off')
            self._cache=None
            state=self.status(force=True)
            if state['enabled'] != (action=='enable') or state['error']:
                raise RuntimeError('Funnel command returned, but the requested state could not be verified. Refresh status.')
            record={'action':action,'at':time.time(),'verified':True}
            path=self.home/'deployment/funnel-last-action.json'
            tmp=path.with_suffix('.pending')
            tmp.write_text(json.dumps(record),encoding='utf-8');tmp.replace(path)
            return self.status(force=True)
