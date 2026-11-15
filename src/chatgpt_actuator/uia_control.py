"""Session-bound Win32 patterns. Never uses focus, physical input or fallback."""
import os,time
import psutil,win32gui,win32process,pywintypes
from .automation_errors import PolicyDenied

def identity(hwnd):
    hwnd=int(hwnd)
    if not win32gui.IsWindow(hwnd) or win32gui.GetAncestor(hwnd,2)!=hwnd:
        raise ValueError('Bind an existing top-level window')
    try:
        pid=win32process.GetWindowThreadProcessId(hwnd)[1];process=psutil.Process(pid)
        protected={os.getpid(),*(p.pid for p in psutil.Process().parents())}
        name=process.name().casefold()
        if pid in protected or name in {'csrss.exe','winlogon.exe','lsass.exe','services.exe','tailscaled.exe','tunnel-client.exe'}:
            raise PermissionError('Protected process cannot be controlled')
        return {'hwnd':hwnd,'pid':pid,'started':process.create_time(),'class':win32gui.GetClassName(hwnd)}
    except psutil.NoSuchProcess:
        raise PolicyDenied('ownership_lost') from None
    except pywintypes.error as exc:
        if exc.winerror==1400:raise PolicyDenied('ownership_lost') from None
        raise

def bind(hwnd,private=False,desktop_control=False):
    target=identity(hwnd)
    if not private and not desktop_control and win32gui.GetForegroundWindow()==target['hwnd']:raise PermissionError('Use a background target window')
    return {**target,'expires':time.time()+600,'private_desktop':private,'desktop_control':desktop_control}

def validate(target):
    if not isinstance(target,dict) or time.time()>target.get('expires',0):raise PolicyDenied('expired_target')
    current=identity(target['hwnd'])
    if any(current[k]!=target[k] for k in ['pid','started','class']):raise PolicyDenied('ownership_lost')
    if not target.get('private_desktop') and not target.get('desktop_control') and win32gui.GetForegroundWindow()==target['hwnd']:raise PermissionError('Target is foreground; background action refused')
    return target['hwnd']

def perform(service,target,method,element_ref,value=None,action=None,precondition=None,verification=None,postcondition=None,direction=None,amount='page',count=1):
    from .uia_conditions import validate as validate_condition,UIAPreconditionError,UIAValidationError
    validate_condition(precondition);service.action_precondition=precondition
    from .uia_conditions import verification_options,observe
    observation=verification_options(verification)
    if postcondition is not None:
        from .uia_conditions import validate_search_query
        if method not in {'invoke','set_value','toggle'} or not isinstance(postcondition,dict) or set(postcondition)!={'query','condition'}:raise UIAValidationError('Provide a postcondition query and condition for Invoke, Value or Toggle')
        validate_search_query(postcondition['query']);validate_condition(postcondition['condition'])
        if not postcondition['condition']:raise UIAValidationError('Nonempty postcondition required')
        observation=observation or {'stable_ms':300,'timeout_ms':3000,'wait_for_match':True}
    if method in {'scroll','scroll_into_view','window_action','focus'}:
        from .uia_desktop_actions import perform as desktop_perform
        return desktop_perform(service,target,method,element_ref,action=action,direction=direction,amount=amount,count=count,precondition=precondition)
    hwnd=validate(target)
    if method not in {'invoke','set_value','toggle','select','expand_collapse','set_range_value'}:raise PermissionError('Unsupported Win32 pattern')
    if method=='set_value' and (not isinstance(value,str) or len(value)>service.config.max_value_chars):raise ValueError('Invalid value')
    if method=='expand_collapse' and action not in {'expand','collapse'}:raise ValueError('action must be expand or collapse')
    if method=='set_range_value':
        import math
        if type(value) not in (int,float) or not math.isfinite(value):raise ValueError('Range value must be finite')
    with service._com_scope():
        element=service._resolve_ref(hwnd,element_ref)
        classes={'invoke':{'button'},'set_value':{'edit'},'toggle':{'button'},
                 'select':{'listbox','combobox','combolbox','syslistview32','systreeview32','systabcontrol32'},
                 'expand_collapse':{'combobox','systreeview32'},'set_range_value':{'msctls_trackbar32','msctls_updown32'}}[method]
        node=element;supported=False
        for _ in range(8):
            if str(node.element_info.class_name).casefold() in classes:supported=True;break
            if method in {'invoke','set_value','toggle'}:break
            if node.handle==hwnd:break
            node=node.parent()
            if node is None:break
        if not supported and not target.get('desktop_control'):raise PolicyDenied('unsupported_pattern')
        if bool(getattr(element.element_info,'is_password',False)):raise PermissionError('Password controls refused')
        if not element.is_enabled():raise PolicyDenied('disabled_control')
        iface=service._iface(element,{'invoke':'iface_invoke','set_value':'iface_value','toggle':'iface_toggle','select':'iface_selection_item','expand_collapse':'iface_expand_collapse','set_range_value':'iface_range_value'}[method])
        if iface is None:raise PolicyDenied('unsupported_pattern')
        if method in {'set_value','set_range_value'} and bool(iface.CurrentIsReadOnly):raise PolicyDenied('read_only')
        if method=='set_range_value' and not float(iface.CurrentMinimum)<=value<=float(iface.CurrentMaximum):raise ValueError('Range value outside provider bounds')
    validate(target);foreground=win32gui.GetForegroundWindow()
    from .uia_followup import windows, observe as observe_windows
    try: previous_windows = windows(target) if target.get('desktop_control') else None
    except Exception: previous_windows = None
    kwargs={'value':value} if method in {'set_value','set_range_value'} else {'action':action} if method=='expand_collapse' else {}
    try:result=getattr(service,method)(hwnd,element_ref,**kwargs)
    except Exception as exc:
        if isinstance(exc,(UIAPreconditionError,UIAValidationError)):raise
        from .automation_errors import MutationOutcomeUnknown
        raise MutationOutcomeUnknown('UIA provider outcome unknown: '+type(exc).__name__) from exc
    unchanged=bool(target.get('private_desktop')) or foreground==win32gui.GetForegroundWindow()
    verified=result.get('verification_passed',result.get('changed',False))
    if method=='select':verified=result.get('selected') is True
    if method=='expand_collapse':verified=result.get('after')==('expanded' if action=='expand' else 'collapsed')
    permitted=unchanged or bool(target.get('desktop_control'))
    result['verification']={'state':'verified' if verified and permitted else 'unconfirmed' if permitted and (method=='invoke' or result.get('observation_unavailable')) else 'mismatch',
        'foreground_unchanged':unchanged,'retry_safe':False,'business_outcome_verified':False}
    result['physical_input_used']=False
    result['foreground_change_allowed']=bool(target.get('desktop_control'))
    result['delivery_state']='dispatched'
    if postcondition is not None:
        from .uia_desktop_actions import observe_postcondition
        try:result['verification']={**observe_postcondition(service,hwnd,postcondition,observation),'foreground_unchanged':unchanged}
        except Exception:result['verification']={'state':'unconfirmed','foreground_unchanged':unchanged,'retry_safe':False,'business_outcome_verified':False}
    if observation and method!='invoke' and postcondition is None:
        condition=({'value_equals':value} if method=='set_value' else {'selected':True} if method=='select' else {'toggle_state':result['after']} if method=='toggle' else {'expand_state':'expanded' if action=='expand' else 'collapsed'} if method=='expand_collapse' else {'range_equals':value})
        try:result['verification']={**observe(service,hwnd,element_ref,condition,observation),'foreground_unchanged':unchanged}
        except Exception:result['verification']={'state':'unconfirmed','foreground_unchanged':unchanged,'retry_safe':False,'business_outcome_verified':False}
    if previous_windows is not None:
        result['followup_windows'] = observe_windows(target, previous_windows)
    return result
