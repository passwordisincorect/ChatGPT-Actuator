"""Conservative provider-only retry classification; never mutation retry advice."""
import json


class PlannerUnavailable(RuntimeError):
    def __init__(self, code, transient=False):
        self.code, self.transient = code, transient
        super().__init__('Planner unavailable: ' + code)


def failure(stdout=b'', stderr=b''):
    diagnostics = [stderr.decode('utf-8', errors='replace')[:16000]]
    for line in stdout.decode('utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') in {'error', 'turn.failed'}:
            diagnostics.append(json.dumps(event.get('error', event.get('message', '')))[:8000])
    text = '\n'.join(diagnostics).casefold()
    if any(word in text for word in ('not logged in', '401', '403', 'unauthorized', 'authentication', 'invalid api key')):
        return PlannerUnavailable('authentication_required')
    if any(word in text for word in ('usage limit', 'quota', 'insufficient_quota', 'billing')):
        return PlannerUnavailable('usage_limit')
    if any(word in text for word in ('429', 'rate limit', '502', '503', '504', 'connection reset',
            'connection refused', 'network', 'timed out', 'timeout', 'temporarily unavailable', 'service unavailable')):
        return PlannerUnavailable('temporary_provider_failure', transient=True)
    return PlannerUnavailable('unclassified_failure')
