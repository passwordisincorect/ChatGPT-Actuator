"""Evidence about this dispatch, not a claim that a client received a response."""
import os
import sqlite3
import time
from .evidence import Evidence, signed_gateway_request
from .tool_policy import principal_context

FRESH_SECONDS = 300

def observation(runtime):
    from . import __version__
    principal = principal_context.get()
    source = (('oauth_gateway' if getattr(principal,'connection_kind',None)=='oauth_gateway' else 'signed_gateway') if signed_gateway_request.get() else
              'configured_stdio' if os.getenv('ACTUATOR_MCP_TRANSPORT') == 'stdio' else
              'local_or_unidentified')
    authenticated = principal is not None and source != 'local_or_unidentified'
    recent = None
    if principal is not None and hasattr(runtime, 'config_path'):
        try:
            recent = Evidence(runtime.config_path.parent/'runtime-evidence.sqlite3').dispatch_snapshot(
                principal.owner, principal.session, __version__, FRESH_SECONDS)
        except (OSError, sqlite3.Error):
            recent = {'state': 'unavailable'}
    return {'schema_version': 1, 'scope': 'current_owner_and_work_session',
            'runtime_version': __version__, 'freshness_seconds': FRESH_SECONDS,
            'current_request': {'state': 'authenticated_dispatch_observed' if authenticated else 'local_or_unidentified',
                                'source': source, 'checked_at': time.time()},
            'previous_successful_dispatch': recent,
            'response_delivery_verified': False,
            'oauth_gateway_dispatch_observed': source=='oauth_gateway',
            'oauth_flow_verified': False,
            'note': 'A signed request proves backend dispatch, not the browser, originating application, OAuth flow, or response receipt'}
