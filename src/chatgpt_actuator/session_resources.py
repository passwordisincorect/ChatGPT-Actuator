"""Reconcile browser ownership without closing live resources or changing owners."""
import json,time

def validate_refs(sessions,sid,name,args):
    for key,value in args.items():
        if key not in ('target_ref','workspace_ref','tab_ref','element_ref','root_ref') or value is None:continue
        kind={'target_ref':'uia_control_target','workspace_ref':'uia_workspace'}.get(key)
        kind=kind or ('dom_' if name.startswith('dom_') else 'uia_')+('element_ref' if key=='root_ref' else key)
        try:raw=sessions.resolve(sid,kind,value)
        except PermissionError:
            if name!='dom_close_tab' or key!='tab_ref':raise
            raw=sessions.resolve(sid,'dom_closed_tab_ref',value)
        if name!='uia_desktop_recover' and kind=='uia_control_target' and time.time()>raw.get('expires',0):
            from .automation_errors import PolicyDenied
            raise PolicyDenied('expired_target')

def reconcile(sessions,host,owner,sid):
    # Inventory does not launch Edge or allocate a context.
    inventory=host.call('resource_inventory',owner,sid,timeout=5)
    tabs=set(inventory['tab_refs'])
    for raw in tabs:sessions.bind(sid,'dom_tab_ref',raw)
    removed=0
    with sessions.lock,sessions.db() as db:
        for handle,kind,value in db.execute("SELECT id,kind,value FROM handles WHERE session=? AND kind IN ('dom_tab_ref','dom_element_ref','uia_control_target')",(sid,)).fetchall():
            raw=json.loads(value);stale=False;retired_reason='lease_expired' if kind=='uia_control_target' else 'node_expired'
            if kind=='dom_tab_ref':stale=raw not in tabs
            elif kind=='dom_element_ref':
                try:
                    from .tools.browser_dom import BrowserDOMService
                    data=BrowserDOMService._decode(raw,'element')
                    stale=data.get('tab_ref') not in tabs or data.get('pin') not in inventory['element_pins']
                    if data.get('tab_ref') not in tabs:retired_reason='tab_closed'
                except (TypeError,ValueError):stale=True
            else:stale=time.time()>raw.get('expires',0)
            if stale:
                if kind=='dom_tab_ref':db.execute("UPDATE handles SET kind='dom_closed_tab_ref' WHERE id=?",(handle,))
                else:
                    sessions.remember_retired(db,sid,handle,retired_reason);db.execute('DELETE FROM handles WHERE id=?',(handle,))
                removed+=1
    return {'state':'observed','open_tabs':len(tabs),'stale_handles_removed':removed,'resources_auto_terminated':False}
