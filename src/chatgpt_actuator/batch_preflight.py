"""Static validation before any dispatch; live provider state remains deferred."""
import inspect
import math
from typing import get_type_hints
from pydantic import TypeAdapter

def references(value,index):
    if isinstance(value,dict):
        if set(value)=={'$ref'}:
            parts=value['$ref'].split('.') if isinstance(value['$ref'],str) else []
            if len(parts)<2 or not parts[0].isdigit() or not 0<=int(parts[0])<index:raise ValueError('Reference must point to a prior action output')
            return True
        return any([references(child,index) for child in value.values()])
    if isinstance(value,(list,tuple)):return any([references(child,index) for child in value])
    return False

def check(index,tool,args,handler):
    signature=inspect.signature(handler);signature.bind(**args)
    hints=get_type_hints(inspect.unwrap(handler))
    deferred=[]
    for key,value in args.items():
        if tool!='batch_preview' and references(value,index):deferred.append(key);continue
        if key in hints:TypeAdapter(hints[key]).validate_python(value,strict=True)
    if tool.startswith('uia_'):
        from .uia_conditions import validate,verification_options
        if tool=='uia_control_find' and 'query' in args and 'query' not in deferred:
            from .uia_conditions import validate_search_query
            validate_search_query(args['query'])
        if 'precondition' in args and 'precondition' not in deferred:validate(args['precondition'])
        if 'verification' in args and 'verification' not in deferred:verification_options(args['verification'])
        if tool=='uia_control_expand_collapse' and 'action' in args and 'action' not in deferred and args['action'] not in ('expand','collapse'):raise ValueError('Invalid expand action')
        if tool=='uia_control_set_range_value' and 'value' in args and 'value' not in deferred and not math.isfinite(args['value']):raise ValueError('Range must be finite')
    elif tool.startswith('dom_'):
        from .tools.dom_preconditions import validate
        from .tools.dom_verification import options
        if 'precondition' in args and 'precondition' not in deferred:validate(args['precondition'])
        if 'verification' in args and 'verification' not in deferred:options(args['verification'])
        if tool=='dom_click' and 'popup_timeout_ms' in args and 'popup_timeout_ms' not in deferred and not 0<=args['popup_timeout_ms']<=3000:raise ValueError('Invalid popup timeout')
    callback=getattr(handler,'_actuator_preflight',None)
    if callback:callback({k:v for k,v in args.items() if k not in deferred})
    return {'index':index,'tool':tool,'state':'static_checked','deferred_arguments':deferred,'provider_state':'checked_at_dispatch','atomic':False}

def preview(actions,handlers):
    from .batch import _normalize_actions
    from .execution_errors import diagnostic
    normalized=_normalize_actions(actions,handlers)
    if any(tool=='batch_preview' for _,tool,_ in normalized):raise ValueError('Nested preview is not allowed')
    checks=[]
    for index,tool,args in normalized:
        try:checks.append({**check(index,tool,args,handlers[tool]),'dispatch_performed':False})
        except Exception as exc:checks.append({'index':index,'tool':tool,'state':'rejected','dispatch_performed':False,'error':{**diagnostic(exc,tool),'delivery_state':'not_dispatched','retry_safe':False}})
    return {'state':'rejected' if any(c['state']=='rejected' for c in checks) else 'static_checked','actions':checks,'requested':len(checks),'dispatch_performed':False,'atomic':False,'values_included':False,'provider_conditions_verified':False,'note':'This plan is a snapshot; execute rechecks permission, refs and live provider conditions.'}
