"""Verified DOM sequencing: static validation, observed states, no replay."""
MUTATIONS=frozenset({'dom_open_tab','dom_navigate','dom_close_tab','dom_click','dom_set_value','dom_select','dom_toggle','dom_variant_select'})

def validate(tool,args):
    from .tool_policy import READ_TOOLS
    if tool not in MUTATIONS and not (tool.startswith('dom_') and tool in READ_TOOLS):raise ValueError('verified_dom accepts DOM reads and verified page mutations only')
    if tool=='dom_click':
        verification=args.get('verification')
        if not args.get('expected_selector') and (not isinstance(verification,dict) or not any(verification.get(key) for key in ('selector','text_contains','url_equals','url_contains'))):raise ValueError('DOM click requires an explicit observation condition')

def assess(tool,item):
    if not item.get('ok'):return 'tool_failed'
    data=item.get('result')
    if not isinstance(data,dict):return 'result_unavailable'
    if data.get('content_state',{}).get('human_action_required'):return 'human_action_required'
    if tool not in MUTATIONS:return None
    state=data.get('verification',{}).get('state') or data.get('execution',{}).get('verification_state')
    if tool=='dom_open_tab' and data.get('opened') is True:state='verified'
    if tool=='dom_close_tab' and data.get('closed') is True:state='verified'
    return None if state=='verified' else 'mutation_not_confirmed'
