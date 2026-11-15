"""Bounded local service probes; tunnel identity is an observation, not health."""
import hashlib
import json
from pathlib import Path
import time

import httpx
import psutil

from .coordination import health
from .queue import AgentQueue


def probe(component):
    try:
        with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
            if component == 'backend':
                from ..update_health import decode_rpc
                data = decode_rpc(client.post('http://127.0.0.1:8090/mcp',
                    headers={'accept': 'application/json, text/event-stream'},
                    json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                        'protocolVersion': '2025-03-26', 'capabilities': {},
                        'clientInfo': {'name': 'Actuator-local-health', 'version': '1'}}}))
                version = data['result']['serverInfo']['version']
                return {'state': 'healthy', 'version': version}
            response = client.get('http://127.0.0.1:8188/healthz')
            response.raise_for_status()
            if not isinstance(response.json(), dict):
                raise ValueError('Invalid health response')
            return {'state': 'healthy'}
    except Exception as exc:
        return {'state': 'unavailable', 'error_type': type(exc).__name__}


def tunnel_snapshot():
    items = []
    for process in psutil.process_iter(['name']):
        if (process.info['name'] or '').casefold() != 'tunnel-client.exe':
            continue
        try:
            # No command lines, aliases, credentials or local executable paths.
            items.append((process.pid, process.create_time()))
        except psutil.Error:
            continue
    items.sort()
    return {'process_count': len(items),
            'identity_sha256': hashlib.sha256(json.dumps(items).encode()).hexdigest(),
            'connectivity': 'not_probed'}


def collect(home):
    home = Path(home)
    queue = AgentQueue(home / 'config/missions.sqlite3')
    services = {name: probe(name) for name in ('backend', 'gateway')}
    try:
        expected = json.loads((home / 'runtime-current.json').read_text())['version']
    except (OSError, ValueError, KeyError):
        expected = None
    if services['backend']['state'] == 'healthy' and services['backend']['version'] != expected:
        services['backend']['state'] = 'version_mismatch'
    tunnels, now = tunnel_snapshot(), time.time()
    worker = health(queue.store)
    services['worker'] = {'state': worker['state'], 'age_seconds': worker.get('age_seconds')}
    with queue.store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS agent_health_events(seq INTEGER PRIMARY KEY,created REAL,document TEXT)')
        row = db.execute('SELECT document FROM agent_health_events ORDER BY seq DESC LIMIT 1').fetchone()
        previous = json.loads(row[0]) if row else {}
        changed = bool(previous.get('tunnel') and previous['tunnel']['identity_sha256'] != tunnels['identity_sha256'])
        for name, item in services.items():
            old = previous.get('components', {}).get(name, {})
            item['last_healthy_at'] = now if item['state'] == 'healthy' else old.get('last_healthy_at')
        report = {'checked_at': now, 'expected_version': expected, 'components': services,
                  'tunnel': {**tunnels, 'identity_changed_since_last_probe': changed},
                  'state': 'healthy' if all(v['state'] == 'healthy' for v in services.values()) else 'degraded',
                  'external_connector_connectivity': 'not_probed'}
        event = db.execute('SELECT state,created FROM agent_watchdog_events ORDER BY seq DESC LIMIT 1').fetchone() if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='agent_watchdog_events'").fetchone() else None
        report['last_watchdog_event'] = {'code': event[0], 'at': event[1]} if event else None
        try:
            report['last_backup_drill'] = json.loads((home / 'backups/agent-state/last-drill.json').read_text())
        except (OSError, ValueError):
            report['last_backup_drill'] = None
        db.execute('INSERT INTO agent_health_events(created,document) VALUES(?,?)', (now, json.dumps(report)))
        db.execute('DELETE FROM agent_health_events WHERE seq NOT IN (SELECT seq FROM agent_health_events ORDER BY seq DESC LIMIT 100)')
    return report
