"""Bounded passive metadata from an already-authorized page connection."""
import asyncio
import re
import time
import secrets
import math
from urllib.parse import urlsplit

def resource_name(value):
    try:
        parsed=urlsplit(str(value or ''))
        if parsed.scheme not in {'http','https'}: return {'host':None,'file':None}
        name=parsed.path.rsplit('/',1)[-1].split(';',1)[0]
        return {'host':parsed.hostname,'file':name if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',name) else '[omitted]'}
    except ValueError:
        return {'host':None,'file':None}

async def passive(page, allowed):
    start=str(page.url)
    allowed(start)
    result={'schema_version':1,'observation_id':secrets.token_urlsafe(18),
            'checked_at':time.time(),'source':'existing_page_connection',
            'navigation_performed':False,'new_browser_connections':0,
            'resource_history_complete':False,'status_zero_means':'unknown, not proof of failure',
            'browser_error_history':'buffered; entries may predate the current document',
            'resources':[],'browser_errors':[],'state':'partial'}
    session=None
    try:
        async with asyncio.timeout(3):
            raw=await page.evaluate("""() => ({ready:document.readyState,visibility:document.visibilityState,
                resources:performance.getEntriesByType('resource').slice(-40).map(e=>({
                url:e.name,type:e.initiatorType,status:typeof e.responseStatus==='number'?e.responseStatus:null}))})""")
            result['document']={k:raw.get(k) for k in ('ready','visibility')}
            for row in raw.get('resources',[])[:40]:
                status=row.get('status')
                result['resources'].append({**resource_name(row.get('url')),
                    'type':str(row.get('type',''))[:32],
                    'http_status':status if type(status) is int and 0<=status<=599 else None})
            session=await page.context.new_cdp_session(page)
            def receive(event):
                entry=event.get('entry',{})
                if entry.get('level') not in {'error','warning'} or len(result['browser_errors'])>=20:return
                code=re.search(r'\bnet::ERR_[A-Z_]{1,60}\b',str(entry.get('text','')))
                stamp=entry.get('timestamp')
                result['browser_errors'].append({**resource_name(entry.get('url')),
                    'level':entry.get('level'),'source':str(entry.get('source',''))[:32],
                    'entry_time_epoch_ms':stamp if type(stamp) in (int,float) and math.isfinite(stamp) and 0<=stamp<=10**16 else None,
                    'network_error':code.group(0) if code else None})
            session.on('Log.entryAdded',receive)
            await session.send('Log.enable')
            await asyncio.sleep(.1)
            result['state']='observed'
    except (Exception,asyncio.TimeoutError) as exc:
        result['error_type']=type(exc).__name__
    finally:
        if session is not None:
            try:await asyncio.wait_for(session.detach(),timeout=.5)
            except Exception:pass
    # A navigation during observation invalidates the cross-step snapshot.
    allowed(str(page.url))
    if str(page.url)!=start:
        result.update(state='target_changed_during_observation',resources=[],browser_errors=[])
    result['finished_at']=time.time()
    return result

def handoff(state, tab_ref):
    needed=state.get('human_action_required') is True
    return {'state':'human_action_required' if needed else 'fresh_content_observed',
            'tab_ref':tab_ref,'reason':state.get('state'),'checked_at':time.time(),
            'automatic_resume':False,'browser_restart_performed':False,
            'next_action':'Complete verification manually in this tab, then obtain a fresh observation' if needed
                          else 'Validate current target, identity and action preconditions before any mutation',
            'business_outcome_verified':False}
