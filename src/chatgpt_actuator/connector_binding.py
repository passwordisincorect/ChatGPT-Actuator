"""Classify caller evidence separately from the local backend probe."""
def binding(backend, observed_error=None, current=None):
    if observed_error not in {None, 'unknown_tool', 'unauthorized', 'timeout'}:
        raise ValueError('Unsupported connector error category')
    ready = backend.get('mcp_list_ok') is True and backend.get('readonly_call_ok') is True
    if not ready:
        state, action = 'backend_unavailable', 'Restore local MCP readiness before refreshing the connector'
    elif observed_error == 'unknown_tool':
        state, action = 'connector_dispatch_unavailable', 'Refresh or reconnect the Actuator connector catalog in the application'
    elif observed_error == 'unauthorized':
        state, action = 'authorization_unverified', 'Check the connector authentication response and approved scopes'
    elif observed_error == 'timeout':
        state, action = 'public_transport_unverified', 'Check public transport separately from local MCP'
    else:
        state, action = 'connector_unverified', 'Perform an actual read-only call from the application'
    dispatched = bool(current and current.get('current_request',{}).get('state')=='authenticated_dispatch_observed')
    if ready and observed_error is None and dispatched:
        state, action = 'authenticated_dispatch_observed', 'Current request reached this backend; client response receipt and originating application remain unverified'
    return {'state': state, 'local_backend_ready': ready, 'application_verified': False,
            'authenticated_backend_dispatch_observed': dispatched,
            'application_response_delivery_verified': False,
            'observed_error': observed_error, 'error_source': 'caller_supplied' if observed_error else None,
            'next_action': action, 'automatic_binding_reset_supported': False}
