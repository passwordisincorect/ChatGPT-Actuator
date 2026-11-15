"""Bounded counters without UI text, URLs, refs or credentials."""
import json,os,time
from pathlib import Path
import httpx,psutil

def export(runtime):
    try:
        data={'pid':os.getpid(),'started':psutil.Process().create_time(),'checked_at':time.time(),
              'uia':runtime.get_service('uia').metrics(),
              'dom':{'contexts':len(runtime.get_service('dom')._session_contexts),
                     'pinned_refs':len(runtime.get_service('dom')._pinned_elements),
                     'frame_refs':len(runtime.get_service('dom')._snapshot_frames)}}
        root=runtime.config_path.parent.parent/'deployment/automation-metrics';root.mkdir(parents=True,exist_ok=True)
        path=root/(str(data['pid'])+'.json');temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(data),encoding='utf-8');os.replace(temp,path)
    except (OSError,ValueError,AttributeError,psutil.Error):pass

def snapshot(home):
    records=[]
    for path in list((Path(home)/'deployment/automation-metrics').glob('*.json'))[:32]:
        try:
            if path.stat().st_size>8192:continue
            data=json.loads(path.read_text())
            if time.time()-data['checked_at']>120 or psutil.Process(data['pid']).create_time()!=data['started']:continue
            allowed={'samples','p50_ms','p95_ms','timeouts','failures','worker_running','timeout_seconds','sample_window','percentiles_provisional','peak_process_rss_mb'}
            uia={k:v for k,v in data['uia'].items() if k in allowed and isinstance(v,(int,float,bool,type(None)))}
            dom={k:v for k,v in data['dom'].items() if k in {'contexts','pinned_refs','frame_refs'} and type(v) is int}
            records.append({'uia':uia,'dom':dom,'checked_at':data['checked_at']})
        except (OSError,ValueError,KeyError,psutil.Error):continue
    native={'state':'unavailable'}
    try:
        with httpx.Client(timeout=2,trust_env=False) as client:
            data=client.get('http://127.0.0.1:8081/health?details=true').json()
        components=data.get('components',{})
        native={'ready':data.get('ready',False),'control_plane':components.get('control-plane',{}).get('state'),
                'mcp_discovery':components.get('mcp',{}).get('state'),
                'note':'Read connection-status for actual connector proof; readiness does not verify the alias.'}
    except (httpx.HTTPError,ValueError):pass
    return {'checked_at':time.time(),'records':records,'native':native,
            'labels_included':False,'stale_after_seconds':120}
