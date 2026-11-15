"""One observation contract for semantic actions; never replays a mutation."""
MUTATIONS=frozenset({'uia_invoke','uia_set_value','uia_toggle','uia_control_select',
    'uia_control_expand_collapse','uia_control_set_range_value','dom_open_tab','dom_navigate',
    'dom_close_tab','dom_click','dom_set_value','dom_select','dom_toggle','dom_scroll','dom_scroll_into_view','dom_variant_select'})

def result(tool,data):
    if tool not in MUTATIONS or not isinstance(data,dict):return data
    output=dict(data)
    observed=data.get('verification',{}).get('state','unconfirmed')
    if tool=='dom_open_tab' and data.get('opened'):observed='verified'
    if tool=='dom_close_tab' and data.get('closed'):observed='verified'
    output['execution']={'schema_version':1,'delivery_state':'dispatched','phase':'completed',
        'verification_state':observed,'retry_safe':False,'business_outcome_verified':False}
    if tool.startswith('dom_'):
        output['evidence']={'level':'title_only' if tool=='dom_open_tab' else 'navigation_observed' if tool=='dom_navigate' else 'action_observed' if observed=='verified' else 'action_unconfirmed',
            'business_outcome_verified':False,'technical_suitability_verified':False}
    return output
