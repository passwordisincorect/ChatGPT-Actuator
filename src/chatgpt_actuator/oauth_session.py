"""Gateway aggregate session state is independent of authenticated MCP proof."""
from contextlib import closing
from pathlib import Path
import sqlite3,time
def session_state(counts,last=None,*,verified=False):
    if verified:return 'verified'
    if counts.get('access_live',0)>0:return 'awaiting_actual_call'
    if counts.get('refresh_live',0)>0:
        return 'refresh_failed' if last and last.get('state')=='failed' and last.get('grant_type')=='refresh_token' else 'refresh_needed'
    return 'reconnect_required' if counts.get('access_total',0)>0 or counts.get('refresh_total',0)>0 else 'not_connected'
def snapshot(home,*,verified=False,now=None):
    now=time.time() if now is None else now
    root=Path(home).parent/'MCP-Funnel-Gateway';path=root/'gateway.sqlite3';counts={};last=None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
            for kind,total,live in c.execute("SELECT kind,COUNT(*),SUM(CASE WHEN expires>? THEN 1 ELSE 0 END) FROM tokens WHERE resource LIKE '%/full/mcp' GROUP BY kind",(now,)):
                if kind in {'access','refresh'}:counts[kind+'_total']=total;counts[kind+'_live']=live
        events=root/'gateway-auth-diagnostics.sqlite3'
        if events.is_file():
            with closing(sqlite3.connect(events.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
                row=c.execute("SELECT grant_type,state,error_code,checked FROM auth_events WHERE resource_kind='full' AND checked>? ORDER BY id DESC LIMIT 1",(now-86400,)).fetchone()
                if row:last=dict(zip(('grant_type','state','error_code','checked_at'),row))
        state=session_state(counts,last,verified=verified)
    except (OSError,sqlite3.Error):state='verified' if verified else 'unavailable'
    action={'refresh_needed':'refresh_session','refresh_failed':'reconnect_if_refresh_retry_fails','reconnect_required':'reconnect_connector','not_connected':'connect_connector','awaiting_actual_call':'verify_actual_mcp_call','verified':'none','unavailable':'inspect_gateway'}[state]
    return {'state':state,'recommended_action':action,'counts':counts,'last_request':last,'checked_at':now,'actual_mcp_verified':verified,'basis':'aggregate_gateway_session_metadata'}
