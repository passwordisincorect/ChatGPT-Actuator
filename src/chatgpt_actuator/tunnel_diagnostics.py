"""Independent layers; local liveness never implies remote authentication."""
import json
from pathlib import Path
import time
import hashlib

ERROR_HINTS = {
    'mismatched_organization': 'Organization ID does not match the runtime key',
    'invalid_api_key': 'Runtime key rejected',
    'tunnel_active_organization_required': 'Organization context is missing',
    'tunnel_use_forbidden': 'Runtime key or organization role lacks tunnel access',
    'tunnel_not_found': 'Selected tunnel is unavailable in this organization',
    'network_or_tls_error': 'Outbound HTTPS or TLS validation failed',
}

def metadata_report(path, now=None, max_age=300, settings_path=None):
    result = {'state':'unverified', 'mcp_e2e':'unverified'}
    path = Path(path)
    try:
        if path.stat().st_size > 16384:
            return result
        raw = json.loads(path.read_text())
        if raw.get('profile') != 'chatgpt-actuator':
            return result
        if settings_path is not None:
            settings=Path(settings_path)
            if settings.stat().st_size>65536 or raw.get('settings_sha256')!=hashlib.sha256(settings.read_bytes()).hexdigest():
                return {**result,'state':'stale'}
        age = (time.time() if now is None else now)-float(raw['checked_at_epoch'])
        if not 0 <= age <= max_age:
            return {**result, 'state':'stale'}
        if raw.get('state') not in {'verified','failed'}:
            return result
        result.update(state=raw['state'], checked_at_epoch=raw['checked_at_epoch'], kind='remote_metadata')
        code = raw.get('error_code')
        if code in ERROR_HINTS:
            result.update(error_code=code, next_step=ERROR_HINTS[code])
        # Do not relay arbitrary messages, response bodies or credential material.
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return result

def layers(backend, evidence, metadata):
    def state(ok): return 'verified' if ok else 'failed'
    gateway = evidence.get('tailscale_authenticated_mcp', {})
    return {
        'local_http': {'state':state(backend.get('http_status') == 200)},
        'local_mcp_list': {'state':state(backend.get('mcp_list_ok') is True), 'tool_count':backend.get('tool_count')},
        'local_mcp_call': {'state':state(backend.get('readonly_call_ok') is True)},
        'openai_metadata': metadata,
        'stdio_mcp': {'state':evidence.get('stdio_mcp', {}).get('state','unverified'),
                      'note':'stdio success alone does not identify the remote caller'},
        'tailscale_authenticated_mcp': {'state':gateway.get('state','unverified'),
                                       'checked_at':gateway.get('checked_at')},
        'openai_chatgpt_mcp': {'state':evidence.get('openai_authenticated_mcp',{}).get('state','unverified'),
                              'checked_at':evidence.get('openai_authenticated_mcp',{}).get('checked_at'),
                              'evidence_source':'successful tool call in owner-configured tunnel stdio child'},
    }
