"""Optimistic UIA guards and unique semantic search; no focus/input fallback."""
import math
import hashlib
import re
import time
class UIAPreconditionError(ValueError):pass
class UIAValidationError(ValueError):pass
class UIAAmbiguousTarget(LookupError):pass
class UIASearchIncomplete(LookupError):pass

def verification_options(value):
    if value is None:return None
    from .tools.dom_verification import options
    try:
        config=options(value)
        if set(value)&{'selector','state','text_contains','url_equals','url_contains'} or config['timeout_ms']>5000:raise ValueError('Invalid UIA observation options')
        return config
    except ValueError:raise UIAValidationError('Invalid bounded UIA observation options') from None

def observe(service,hwnd,reference,condition,config):
    began=time.monotonic();matched_since=None;samples=0
    with service._com_scope():
        element=service._resolve_ref(hwnd,reference)
        while (time.monotonic()-began)*1000<config['timeout_ms']:
            samples+=1;now=time.monotonic()
            try:check(service,element,condition);matched=True
            except UIAPreconditionError:matched=False
            if matched:
                if matched_since is None:matched_since=now
                if (now-matched_since)*1000>=config['stable_ms'] and (now-began)*1000>=config.get('observe_ms',0):state='verified';break
            else:
                matched_since=None
                if not config['wait_for_match']:state='mismatch';break
            time.sleep(.05)
        else:state='unconfirmed'
    return {'state':state,'samples':samples,'observed_ms':round((time.monotonic()-began)*1000),'values_included':False,'retry_safe':False,'business_outcome_verified':False}

def validate(condition):
    if condition is None:return
    if not isinstance(condition,dict) or not condition or set(condition)-{'enabled','value_equals','value_sha256','selected','toggle_state','expand_state','range_equals','name_equals'}:raise UIAValidationError('Invalid UIA precondition')
    if 'value_sha256' in condition and (not isinstance(condition['value_sha256'],str) or re.fullmatch('[0-9a-f]{64}',condition['value_sha256']) is None):raise UIAValidationError('Invalid SHA-256 condition')
    for key in ('enabled','selected'):
        if key in condition and type(condition[key]) is not bool:raise UIAValidationError('Boolean condition required')
    for key in ('value_equals','name_equals'):
        if key in condition and (not isinstance(condition[key],str) or len(condition[key])>4096):raise UIAValidationError('Bounded string condition required')
    if 'toggle_state' in condition and condition['toggle_state'] not in ('off','on','indeterminate'):raise UIAValidationError('Invalid toggle state')
    if 'expand_state' in condition and condition['expand_state'] not in ('collapsed','expanded','partially_expanded','leaf_node'):raise UIAValidationError('Invalid expand state')
    if 'range_equals' in condition and (type(condition['range_equals']) not in (int,float) or not math.isfinite(condition['range_equals'])):raise UIAValidationError('Finite range condition required')

def check(service,element,condition):
    validate(condition)
    if not condition:return
    if getattr(element.element_info,'is_password',False):raise UIAPreconditionError('Sensitive control conditions refused')
    for key,expected in condition.items():
        if key=='enabled':actual=bool(element.is_enabled())
        elif key=='name_equals':actual=str(element.element_info.name or '')
        else:
            iface=service._iface(element,{'value_equals':'iface_value','value_sha256':'iface_value','selected':'iface_selection_item','toggle_state':'iface_toggle','expand_state':'iface_expand_collapse','range_equals':'iface_range_value'}[key])
            if iface is None:raise UIAPreconditionError('Condition pattern unavailable')
            if key in ('value_equals','value_sha256'):
                actual=str(iface.CurrentValue or '')
                if len(actual)>service.config.max_value_chars:raise UIAPreconditionError('Value exceeds bound')
                if key=='value_sha256':actual=hashlib.sha256(actual.encode('utf-8')).hexdigest()
            elif key=='selected':actual=bool(iface.CurrentIsSelected)
            elif key=='toggle_state':actual={0:'off',1:'on',2:'indeterminate'}.get(int(iface.CurrentToggleState))
            elif key=='expand_state':actual={0:'collapsed',1:'expanded',2:'partially_expanded',3:'leaf_node'}.get(int(iface.CurrentExpandCollapseState))
            else:actual=float(iface.CurrentValue)
        matches=math.isclose(actual,expected,abs_tol=1e-9,rel_tol=1e-9) if key=='range_equals' else actual==expected
        if not matches:raise UIAPreconditionError('UIA condition changed before dispatch')

def validate_search_query(query):
    if not isinstance(query,dict) or not query or set(query)-{'name','automation_id','class_name','control_type','pattern','region'}:raise UIAValidationError('Provide a search criterion')
    for key in ('name','automation_id','class_name','control_type','pattern'):
        if key in query and (not isinstance(query[key],str) or not 1<=len(query[key])<=512):raise UIAValidationError('Invalid search criterion')
    if 'pattern' in query and query['pattern'] not in {'invoke','value','toggle','selection_item','expand_collapse','scroll','scroll_item','range_value','text','window'}:raise UIAValidationError('Unknown pattern')
    region=query.get('region')
    if region is not None and (not isinstance(region,dict) or set(region)!={'left','top','right','bottom'} or any(type(v) not in (int,float) or not math.isfinite(v) for v in region.values()) or region['right']<=region['left'] or region['bottom']<=region['top']):raise UIAValidationError('Invalid region rectangle')
    return region

def filter_snapshot(snapshot,query):
    region=validate_search_query(query)
    if snapshot.get('truncated'):raise UIASearchIncomplete('Cannot prove uniqueness from a truncated snapshot')
    matches=[]
    for item in snapshot['elements']:
        if any(item.get(field+'_truncated') for field in ('name','automation_id','class_name')):raise UIASearchIncomplete('Truncated identifiers or labels')
        if 'name' in query and item['name']!=query['name']:continue
        if 'automation_id' in query and item.get('automation_id')!=query['automation_id']:continue
        if 'class_name' in query and item.get('class_name','').casefold()!=query['class_name'].casefold():continue
        if 'control_type' in query and item['control_type'].casefold()!=query['control_type'].casefold():continue
        if 'pattern' in query and not item['patterns'].get(query['pattern'],False):continue
        rect=item['rect']
        if region and not (rect['left']>=region['left'] and rect['top']>=region['top'] and rect['right']<=region['right'] and rect['bottom']<=region['bottom']):continue
        matches.append(item)
    if len(matches)>1:raise UIAAmbiguousTarget('Several elements match; narrow the query')
    if not matches:raise LookupError('No matching element')
    return {'match':matches[0],'unique':True,'values_included':False,'selection_mode':'exact','search_scanned':snapshot.get('scanned')}
