"""Safe, stable diagnostic fields; a dispatched mutation is never retry-safe."""
READ_ACTIONS = frozenset({'list_tabs', 'peek_tabs', 'find', 'details', 'read_text', 'wait','snapshot','snapshot_diff','accessibility_snapshot'})

def describe(exc, action, *, dispatched):
    from .execution_errors import diagnostic
    if action not in {'session_open','session_close','unknown'}:
        result=diagnostic(exc,'dom_'+action)
        result['action']=action
        return result
    mutation = action not in READ_ACTIONS
    detail = diagnostic(exc, 'dom_open_tab')
    if detail['code'] in {'RESOURCE_LIMIT','NODE_BUSY'}:
        code, instruction = detail['code'], detail['next_action']
    elif isinstance(exc, PermissionError):
        code, instruction = 'ACCESS_DENIED', 'Check ownership and local permissions'
    elif isinstance(exc, TimeoutError):
        code, instruction = 'DEADLINE_EXCEEDED', 'Inspect worker state before another call'
    elif isinstance(exc, (ValueError, TypeError)):
        code, instruction = 'INVALID_REQUEST', 'Refresh refs and validate arguments'
    else:
        code, instruction = 'BACKEND_FAILURE', 'Inspect backend diagnostics'
    unknown = mutation and dispatched
    return {'schema_version': 1, 'code': code, 'action': action,
            'delivery_state': 'unknown' if unknown else 'not_mutating' if not mutation else 'not_dispatched',
            'retry_safe': not unknown and code != 'ACCESS_DENIED', 'next_action': instruction,
            'message': code.replace('_', ' ').capitalize()}
