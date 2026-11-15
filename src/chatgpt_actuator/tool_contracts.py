"""MCP contract metadata; preserve tool payloads and image outputs."""
DESCRIPTIONS={
 'security_policy_info':'Read granted scopes, runtime version, immutable background policy and finite resource budgets. Does not grant permissions.',
 'capability_readiness':'Read registered tools, current authorization and versioned representative provider evidence. Caller-supplied connector names are a comparison, not proof of application connectivity.',
 'connection_diagnostics':'Probe local MCP and classify transport evidence. Optional connector_error accepts unknown_tool, unauthorized or timeout; it is caller evidence and never proves application success.',
 'automation_status':'Read automation worker readiness and counters without UI contents or element references.',
 'command_profile_list':'List owner-defined fixed command profiles and background approval; does not execute them.',
 'work_session_open':'Open an isolated session owned by the authenticated principal; use its session_id in MCP work_session metadata.',
 'work_session_status':'Read only this principal\'s sessions and jobs; other owners are excluded.',
 'work_session_close':'Close an owned session only after active calls, attempts and live resources are released. Returns closed=false with blockers when busy.',
 'work_session_close_preview':'Read blockers for closing an owned session without closing it.',
 'task_attempt_status':'Read owned task phases received/running/cancelling/finished and terminal outcomes. A cancellation request does not mean the process stopped.',
 'task_attempt_cancel':'Request cancellation of an owned PowerShell or command-profile attempt. Wait for finished/cancelled before considering it stopped; never retries the command.',
 'worktree_create':'Create a session-owned Git worktree inside allowed roots; requires a valid Git repository and available Git executable.'
}

def attach(result,data,name):
    if not hasattr(result,'is_error'):return result  # Preserve SDK input-required and protocol results.
    if isinstance(data,dict) and getattr(result,'structured_content',None) is None:
        result.structured_content=data
    meta=dict(getattr(result,'meta',None) or {})
    meta['actuator/contract']={'schema_version':1,'tool':name,
                              'outcome':'error' if getattr(result,'is_error',False) else 'returned',
                              'provider_outcome_verified':False}
    result.meta=meta
    return result
