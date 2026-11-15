"""Stable MCP errors without exception text, arguments, paths or credentials."""
from .tool_policy import scope_for

def diagnostic(exc,tool):
    from .automation_errors import MutationOutcomeUnknown,PolicyDenied,ReferenceUnavailable
    unknown=isinstance(exc,MutationOutcomeUnknown)
    root=exc
    for _ in range(8):
        cause=getattr(root,'__cause__',None)
        if cause is None:break
        root=cause
    name=type(root).__name__
    if isinstance(root,ReferenceUnavailable):code,step='REFERENCE_UNAVAILABLE',{'node_expired':'Run dom_find again in the same owned tab','node_replaced':'Inspect the current DOM and obtain a new node reference','tab_closed':'Open a new owned tab; the old tab cannot be restored by retry','workspace_closed':'Open a new owned private workspace and bind its window','lease_expired':'Bind the current owned private window again'}[root.reference_reason]
    elif name=='UIAAmbiguousTarget':code,step='AMBIGUOUS_TARGET','Narrow the name/type/pattern/region query; no element was selected'
    elif name=='UIASearchIncomplete':code,step='SEARCH_INCOMPLETE','Inspect capture limits before trying a narrower query'
    elif name=='UIAPreconditionError':code,step='PRECONDITION_FAILED','Read current control state before another action'
    elif name in {'DOMValidationError','UIAValidationError'}:code,step='INVALID_REQUEST','Correct the verification condition before another action'
    elif name=='CartCheckpointRequired':code,step='CART_CHECKPOINT_REQUIRED','Use dom_review_record cart observe/reserve with fresh exact quantity and variant, then pass the receipt to dom_click.cart_checkpoint. Read dom_review_status and reconcile unknown outcomes before retry'
    elif name=='CartCoverageRequired':code,step='CART_COVERAGE_REQUIRED','Inspect current complete cart scope and count units; record fresh cart coverage counters before reserving a zero-quantity addition. Reconcile existing pending/unknown attempts instead of retrying'
    elif name=='BrowserSelectionRequired':code,step='BROWSER_SELECTION_REQUIRED','Attach the intended existing browser tab; use browser_mode=managed only when explicitly choosing a separate ephemeral profile'
    elif name=='BrowserConnectionUnavailable':code,step='BROWSER_DISCONNECTED','Inspect the chosen browser, detach the stale connection and explicitly reattach the same target; do not replay the action or start a replacement profile'
    elif name=='DOMHumanActionRequired':code,step='HUMAN_ACTION_REQUIRED','Resolve or inspect the blocker manually in the selected browser; read fresh state before another action. Do not retry or open another tab automatically'
    elif name=='DOMPreconditionError':code,step='PRECONDITION_FAILED','Refresh the DOM target before another action'
    elif name=='FileConflictError':code,step='FILE_CONFLICT','Read the file again and review changes before writing'
    elif name=='FilePreconditionRequired':code,step='PRECONDITION_REQUIRED','Read the file and supply its sha256'
    elif name=='DOMOperationCancelled':code,step='CANCELLED','Inspect target state before any new action; cancellation does not undo a dispatched action'
    elif isinstance(root,PermissionError) and not isinstance(root,PolicyDenied) and str(root).startswith('RESOURCE_LIMIT:'):
        code,step='RESOURCE_LIMIT','Wait for active operations to finish or release unused owned tabs/sessions; OAuth scope changes do not increase resource budgets'
    elif isinstance(root,PermissionError) and not isinstance(root,PolicyDenied) and str(root).startswith('NODE_BUSY:'):
        code,step='NODE_BUSY','Finish active work and release owned resources before retrying the update'
    elif isinstance(root,PermissionError):code,step='ACCESS_DENIED','Check scope, ownership and local policy'
    elif isinstance(root,FileNotFoundError):code,step='NOT_FOUND','Refresh the resource path or reference'
    elif isinstance(root,LookupError):code,step='REFERENCE_UNAVAILABLE','Refresh the snapshot and obtain a new reference'
    elif isinstance(root,TimeoutError) or name=='TimeoutError':code,step='DEADLINE_EXCEEDED','Inspect execution state before another request'
    elif isinstance(root,(ValueError,TypeError)) or name in {'ValidationError','ToolError'}:code,step='INVALID_REQUEST','Check the tool name and arguments'
    else:code,step='BACKEND_FAILURE','Inspect local backend diagnostics'
    try:read=scope_for(tool)=='mcp:read'
    except PermissionError:read=False
    before=code in {'ACCESS_DENIED','PRECONDITION_REQUIRED','FILE_CONFLICT','INVALID_REQUEST','NOT_FOUND','REFERENCE_UNAVAILABLE'}
    # Generic tool validation/provider errors may follow a dispatched mutation.
    known_before=isinstance(root,ReferenceUnavailable) or code in {'CART_COVERAGE_REQUIRED','BROWSER_SELECTION_REQUIRED','BROWSER_DISCONNECTED','CART_CHECKPOINT_REQUIRED','HUMAN_ACTION_REQUIRED','ACCESS_DENIED','RESOURCE_LIMIT','NODE_BUSY','PRECONDITION_REQUIRED','PRECONDITION_FAILED','FILE_CONFLICT'} or name in {'ValidationError','DOMValidationError','UIAValidationError'}
    delivery='not_mutating' if read else 'not_dispatched' if known_before else 'unknown'
    if name in {'DOMDeadlineExceeded','DOMOperationCancelled'} and not getattr(root,'dispatched',True):delivery='not_mutating' if read else 'not_dispatched'
    if unknown:delivery='unknown'
    result={'schema_version':1,'code':code,'tool':tool,'delivery_state':delivery,'phase':'failed',
            'retry_safe':read and code not in {'ACCESS_DENIED','INVALID_REQUEST','CANCELLED'},
            'next_action':step,'message':code.replace('_',' ').capitalize()}
    if unknown:result['retry_safe']=False;result['next_action']='Inspect the target state before another action'
    if isinstance(root,ReferenceUnavailable):result['reference_reason']=root.reference_reason
    if name=='DOMHumanActionRequired':result.update(blocker_state=root.blocker_state,retry_safe=False)
    if code in {'BROWSER_SELECTION_REQUIRED','BROWSER_DISCONNECTED'}:result['retry_safe']=False
    if isinstance(root,PolicyDenied):
        result['reason']=root.reason
        if root.required_scope:result['required_scope']=root.required_scope
        if root.reason=='expired_target':result['reference_reason']='lease_expired'
        if not unknown:
            result['next_action']={'missing_scope':'Reconnect OAuth and explicitly approve the required scope',
                'blocked_host':'Use an allowed host or ask the owner to update browser_dom.allowed_hosts',
                'wrong_owner':'Obtain fresh references in your own active Work Session',
                'expired_target':'Bind the owned private window again',
                'ownership_lost':'Refresh the owned workspace windows and bind the current target',
                'unsupported_pattern':'Inspect the control snapshot and choose an exposed supported pattern',
                'disabled_control':'Wait for the control to become enabled and inspect again',
                'read_only':'Choose a writable control',
                'local_policy':'Review local owner policy; OAuth scopes do not override it'}[root.reason]
    return result
