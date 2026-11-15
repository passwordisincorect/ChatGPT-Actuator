"""Safe denial reasons and conservative post-dispatch failures."""
class PolicyDenied(PermissionError):
    def __init__(self,reason,required_scope=None):
        allowed={'missing_scope','blocked_host','wrong_owner','local_policy','unsupported_pattern','disabled_control','read_only','expired_target','ownership_lost'}
        if reason not in allowed:raise ValueError('Unknown denial reason')
        self.reason=reason
        self.required_scope=required_scope if required_scope in {'mcp:read','mcp:write','mcp:execute'} else None
        super().__init__('Access denied: '+reason)

class MutationOutcomeUnknown(RuntimeError):
    """Provider failed after dispatch was attempted; never safe to replay."""

class BrowserSelectionRequired(LookupError):
    """No browser selected; no implicit profile creation was attempted."""

class BrowserConnectionUnavailable(RuntimeError):
    """The selected browser disconnected before an action could be dispatched."""

class DOMHumanActionRequired(RuntimeError):
    """Session page has a blocker or cannot be inspected; no mutation dispatched."""
    def __init__(self,state):
        self.blocker_state=state
        super().__init__('Manual inspection required before DOM mutation')

class CartCheckpointRequired(ValueError):
    """A known cart action lacks a consumed reservation or targets another product."""

class CartCoverageRequired(ValueError):
    """A zero-quantity addition lacks consistent explicit loaded-cart coverage."""

class DOMDeadlineExceeded(TimeoutError):
    def __init__(self,dispatched):
        self.dispatched=bool(dispatched);super().__init__('DOM operation deadline exceeded')

class DOMOperationCancelled(RuntimeError):
    def __init__(self,dispatched):
        self.dispatched=bool(dispatched);super().__init__('DOM operation cancelled')

class ReferenceUnavailable(PermissionError):
    """Owned ref lifecycle information only; unknown/foreign refs remain denied."""
    def __init__(self,reason):
        if reason not in {'node_expired','node_replaced','tab_closed','workspace_closed','lease_expired'}:raise ValueError('Unknown lifecycle reason')
        self.reference_reason=reason;super().__init__('Owned reference unavailable')
