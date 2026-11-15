"""Read pattern states without exposing text or invoking any pattern action."""
import hashlib
import math

def read(service, element):
    state={'enabled':bool(element.is_enabled()),'password':bool(getattr(element.element_info,'is_password',False))}
    unavailable=[]
    if state['password']:
        return {'state':state,'values_included':False,'sensitive_fields_omitted':True,'unavailable':[]}
    for pattern in ('value','selection_item','toggle','expand_collapse','range_value'):
        iface=service._iface(element,'iface_'+pattern)
        if iface is None:continue
        try:
            if pattern=='value':
                state['read_only']=bool(iface.CurrentIsReadOnly)
                value=str(iface.CurrentValue or '')
                if len(value)>service.config.max_value_chars:raise ValueError('Value exceeds bound')
                state['value_sha256']=hashlib.sha256(value.encode('utf-8')).hexdigest()
            elif pattern=='selection_item':state['selected']=bool(iface.CurrentIsSelected)
            elif pattern=='toggle':state['toggle_state']={0:'off',1:'on',2:'indeterminate'}[int(iface.CurrentToggleState)]
            elif pattern=='expand_collapse':state['expand_state']={0:'collapsed',1:'expanded',2:'partially_expanded',3:'leaf_node'}[int(iface.CurrentExpandCollapseState)]
            else:
                values={key:float(getattr(iface,prop)) for key,prop in [('value','CurrentValue'),('minimum','CurrentMinimum'),('maximum','CurrentMaximum'),('small_change','CurrentSmallChange'),('large_change','CurrentLargeChange')]}
                if not all(math.isfinite(v) for v in values.values()):raise ValueError('Non-finite provider range')
                values['read_only']=bool(iface.CurrentIsReadOnly);state['range']=values
        except Exception:unavailable.append(pattern)
    return {'state':state,'values_included':False,'text_values_included':False,'unavailable':unavailable,'observed':True,'atomic':False}
