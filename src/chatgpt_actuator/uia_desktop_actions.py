"""Additional scoped desktop patterns; no coordinates, keys or fallback."""
import time
import win32gui
from .automation_errors import PolicyDenied,MutationOutcomeUnknown
from .uia_conditions import validate,check

def perform(service,target,method,element_ref,**kwargs):
    from .uia_control import validate as validate_target
    if not target.get('desktop_control'):raise PolicyDenied('local_policy')
    hwnd=validate_target(target);validate(kwargs.get('precondition'));service.action_precondition=kwargs.get('precondition')
    direction=kwargs.get('direction');amount=kwargs.get('amount','page');count=kwargs.get('count',1);action=kwargs.get('action')
    if method=='scroll' and (direction not in {'up','down','left','right'} or amount not in {'line','page'} or type(count) is not int or not 1<=count<=10):raise ValueError('Bounded scroll direction/amount/count required')
    if method=='window_action' and action not in {'minimize','maximize','restore','close'}:raise ValueError('Invalid window action')
    iface_name={'scroll':'iface_scroll','scroll_into_view':'iface_scroll_item','window_action':'iface_window','focus':None}[method]
    with service._com_scope():
        element=service._resolve_ref(hwnd,element_ref)
        if getattr(element.element_info,'is_password',False) or not element.is_enabled():raise PolicyDenied('disabled_control')
        iface=service._iface(element,iface_name) if iface_name else None
        if method!='focus' and iface is None:raise PolicyDenied('unsupported_pattern')
        if method=='focus' and not element.element_info.element.CurrentIsKeyboardFocusable:raise PolicyDenied('unsupported_pattern')
        if method=='scroll' and not bool(iface.CurrentVerticallyScrollable if direction in {'up','down'} else iface.CurrentHorizontallyScrollable):raise PolicyDenied('unsupported_pattern')
        if method=='window_action':
            if element.handle!=hwnd:raise PermissionError('Use the bound top-level window root')
            if action=='maximize' and not iface.CurrentCanMaximize or action=='minimize' and not iface.CurrentCanMinimize:raise PolicyDenied('unsupported_pattern')
        check(service,element,kwargs.get('precondition'))
    foreground=win32gui.GetForegroundWindow();validate_target(target)
    args={'direction':direction,'amount':amount,'count':count} if method=='scroll' else {'action':action} if method=='window_action' else {}
    try:
        if method=='focus':
            with service._com_scope():service._resolve_ref(hwnd,element_ref).element_info.element.SetFocus()
            result={'focused':True,'element_ref':element_ref}
        else:result=getattr(service,method)(hwnd,element_ref,**args)
    except Exception as exc:raise MutationOutcomeUnknown('Desktop pattern outcome unknown: '+type(exc).__name__) from exc
    verified=False;samples=0;deadline=time.monotonic()+1.5
    while time.monotonic()<deadline:
        samples+=1
        if method=='focus':
            try:
                with service._com_scope():verified=bool(service._resolve_ref(hwnd,element_ref).element_info.element.CurrentHasKeyboardFocus)
            except Exception:break
        elif method=='window_action' and action=='close':verified=not win32gui.IsWindow(hwnd)
        elif method=='window_action':
            if not win32gui.IsWindow(hwnd):break
            try:
                validate_target(target)
                with service._com_scope():current=service._resolve_ref(hwnd,element_ref).iface_window.CurrentWindowVisualState
                verified=current=={'minimize':2,'maximize':1,'restore':0}[action]
            except Exception:break
        elif method=='scroll':verified=result.get('before')!=result.get('after')
        else:verified=result.get('after',{}).get('visible') is True
        if verified:break
        time.sleep(.05)
    result.update(physical_input_used=False,delivery_state='dispatched',foreground_change_allowed=True)
    result['verification']={'state':'verified' if verified else 'unconfirmed','samples':samples,'foreground_unchanged':foreground==win32gui.GetForegroundWindow(),'retry_safe':False,'business_outcome_verified':False}
    return result

def observe_postcondition(service,hwnd,postcondition,config):
    from .uia_conditions import filter_snapshot,UIAPreconditionError,UIASearchIncomplete,UIAAmbiguousTarget
    began=time.monotonic();stable_since=None;samples=0
    while (time.monotonic()-began)*1000<config['timeout_ms']:
        samples+=1;now=time.monotonic()
        try:
            capture=service.snapshot(hwnd,max_depth=8,max_items=service.config.max_items,include_hidden=True,content_only=False)
            match=filter_snapshot(capture,postcondition['query'])['match']
            with service._com_scope():check(service,service._resolve_ref(hwnd,match['element_ref']),postcondition['condition'])
            matched=True
        except (LookupError,UIAPreconditionError):matched=False
        if matched:
            stable_since=stable_since or now
            if (now-stable_since)*1000>=config['stable_ms'] and (now-began)*1000>=config.get('observe_ms',0):return {'state':'verified','samples':samples,'retry_safe':False,'business_outcome_verified':False,'condition':'selected_UIA_postcondition'}
        else:stable_since=None
        time.sleep(.05)
    return {'state':'unconfirmed','samples':samples,'retry_safe':False,'business_outcome_verified':False,'condition':'selected_UIA_postcondition'}
