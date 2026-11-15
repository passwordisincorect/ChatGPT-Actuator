"""Token-protected local administrator view; controls share durable queue semantics."""
import json
from pathlib import Path
import sqlite3
from contextlib import closing

from .queue import AgentQueue


def snapshot(path):
    path = Path(path)
    if not path.exists():
        return {'jobs': [], 'counts': {}}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_jobs'").fetchone():
            return {'jobs': [], 'counts': {}}
        rows = db.execute('SELECT document FROM agent_jobs ORDER BY rowid DESC LIMIT 100').fetchall()
        counts = dict(db.execute('SELECT state,COUNT(*) FROM agent_jobs GROUP BY state'))
    return {'jobs': [AgentQueue.summary(json.loads(r[0])) for r in rows], 'counts': counts}


def detail(path, job_id):
    path = Path(path)
    if not isinstance(job_id, str) or not 1 <= len(job_id) <= 128:
        raise ValueError('Job ID required')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        row = db.execute('SELECT document FROM agent_jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Unknown job')
        doc = json.loads(row[0])
        events = db.execute('SELECT seq,kind,created,detail FROM agent_events WHERE job=? '
                            'ORDER BY seq DESC LIMIT 30', (job_id,)).fetchall()
    from .artifacts import history
    artifact_versions=history(AgentQueue(path),doc['owner'],job_id)['versions']
    return {**AgentQueue.summary(doc), 'goal_evidence': doc.get('goal_evidence', []),'artifact_versions':artifact_versions,
            'milestone_history': [{'index': h['index'], 'title': h['title'], 'verified_at': h['verified_at']}
                                  for h in doc.get('milestone_history', [])],
            'events': [{'seq': e[0], 'kind': e[1], 'created': e[2]} for e in reversed(events)]}


def control(path, payload):
    if not isinstance(payload, dict) or set(payload) != {'job_id', 'action', 'expected_updated'}:
        raise ValueError('Job ID, action and observed update time required')
    if type(payload['expected_updated']) not in (int, float):
        raise ValueError('An observed numeric update time is required')
    if not Path(path).is_file():
        raise ValueError('Agent database is unavailable')
    queue = AgentQueue(path)
    with queue.store.db() as db:
        row = db.execute('SELECT owner FROM agent_jobs WHERE id=?', (payload['job_id'],)).fetchone()
    if not row:
        raise ValueError('Unknown job')
    if payload['action'] == 'archive':
        return queue.archive.archive(row[0], payload['job_id'], payload['expected_updated'])
    return queue.control(row[0], payload['job_id'], payload['action'], payload['expected_updated'])


HTML = r'''<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Nhiệm vụ tự động · Actuator</title><style>
body{font:16px system-ui;margin:0;background:#101827;color:#e7edf7}main{max-width:1100px;margin:40px auto;padding:0 24px}
h1{font-size:30px;margin-bottom:8px}p{color:#aab8cd}a{color:#8cc6ff}.summary{display:flex;gap:12px;flex-wrap:wrap;margin:28px 0}
.badge{background:#24324a;padding:10px 16px;border-radius:10px}article{background:#19253a;border:1px solid #30425d;padding:20px;border-radius:14px;margin:14px 0}
.top{display:flex;justify-content:space-between;gap:16px}h2{font-size:18px;margin:0;overflow-wrap:anywhere}.state{color:#8ed9bd;white-space:nowrap}
.details{color:#aab8cd;font-size:14px;margin-top:12px;line-height:1.8}.error{color:#ffbc97}button{background:#3867cc;color:white;border:0;border-radius:8px;padding:10px 16px;cursor:pointer}button:disabled{opacity:.5;cursor:wait}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.7 system-ui}ol{padding-left:24px;color:#b8c9e2}progress{width:100%;accent-color:#69cdb0;height:10px}button.cancel{background:#783e4a}
</style><main><a href="/">← Trung tâm điều khiển</a><h1>Nhiệm vụ tự động</h1><p>Theo dõi tiến độ, lịch chạy và lý do tạm dừng.</p>
<button id="refresh">Làm mới</button> <button id="health">Kiểm tra hệ thống</button> <button id="backup">Sao lưu và kiểm tra phục hồi</button><pre id="health-result" role="status"></pre><p id="message" class="error" role="status" aria-live="polite"></p><div class="summary" id="counts"></div><div id="jobs"></div><p id="updated"></p></main>
<script>const token=__TOKEN__;const labels={QUEUED:'Đang chờ',RUNNING:'Đang chạy',PAUSING:'Đang tạm dừng',PAUSED:'Đã tạm dừng',CANCELLING:'Đang hủy',CANCELLED:'Đã hủy',COMPLETED:'Hoàn thành',BLOCKED:'Cần kiểm tra',FAILED:'Dừng do giới hạn'};
function node(tag,text,cls){const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e}
const expanded=new Set();let loading=false;
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-Control-Center-Token':token,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Không đọc được trạng thái');return d}
function report(error){document.getElementById('message').textContent=error.message}
async function showEvidence(j,target){try{const d=await api('/api/agents/detail?id='+encodeURIComponent(j.id));target.replaceChildren();if((d.blockers||[]).length)target.append(node('pre',JSON.stringify(d.blockers,null,2)));for(const v of d.artifact_versions||[])target.append(node('pre','Phiên bản '+v.id+' '+v.state+' · '+v.path+'\n'+(v.diff||'')));const e=d.goal_evidence||[];target.append(node('p','Bằng chứng gần nhất: '+e.filter(x=>x.passed).length+'/'+e.length+' điều kiện đạt.'));for(const item of e)target.append(node('pre',(item.passed?'✓ ':'○ ')+item.tool+' · '+JSON.stringify(item.path||[])+' · '+(item.observed_sha256||item.error_type||'Chưa có bằng chứng')));target.append(node('pre',(d.events||[]).map(x=>new Date(x.created*1000).toLocaleTimeString('vi-VN')+' · '+x.kind).join('\n')))}catch(e){report(e)}}
async function load(){if(loading)return;loading=true;try{const d=await api('/api/agents');const c=document.getElementById('counts');c.replaceChildren();for(const [s,n]of Object.entries(d.counts))c.append(node('span',(labels[s]||s)+': '+n,'badge'));const list=document.getElementById('jobs');list.replaceChildren();for(const j of d.jobs){const a=node('article','');const t=node('div','','top');t.append(node('h2',j.goal),node('span',labels[j.state]||j.state,'state'));a.append(t,node('div',j.cycles+'/'+(j.max_cycles??'–')+' chu kỳ · '+j.planner_calls+'/'+(j.max_calls??'–')+' lượt AI · '+(j.reported_tokens||0).toLocaleString('vi-VN')+' token được báo cáo','details'));if(j.milestone_count){a.append(node('div','Giai đoạn '+j.milestone_index+'/'+j.milestone_count,'details'));const p=document.createElement('progress');p.max=j.milestone_count;p.value=j.milestone_index;p.setAttribute('aria-label','Tiến độ giai đoạn');a.append(p);const stages=node('ol','');for(const phase of j.milestones||[])stages.append(node('li',(phase.verified?'✓ ':'○ ')+phase.title));a.append(stages)}if((j.graph_nodes||[]).length){const graph=node('ol','');for(const n of j.graph_nodes)graph.append(node('li',n.title+' · '+n.state));a.append(graph)}if(j.waiting_for_inputs)a.append(node('div','Đang chờ tệp đầu vào được xác minh','details'));if(j.recipe_kind)a.append(node('div','Công thức: '+j.recipe_kind+' · '+(j.recipe_steps||0)+' bước được kiểm tra','details'));if(j.run_at)a.append(node('div','Lịch chạy: '+new Date(j.run_at*1000).toLocaleString('vi-VN'),'details'));if(j.reason)a.append(node('div',j.reason,'details error'));if(j.recovery){const r=j.recovery;a.append(node('div','Tiến độ đã kiểm chứng: '+r.verified_milestones+' chặng · '+r.verified_graph_nodes.length+' nhánh','details'));if(r.next_action==='inspect_evidence')a.append(node('div','Mở bằng chứng để đối soát trước khi tiếp tục.','details'));if(r.next_action==='resume')a.append(node('div','Có thể tiếp tục từ tiến độ đã lưu.','details'))}const buttons=node('div','','actions');const evidence=node('div','','details');const show=node('button',expanded.has(j.id)?'Ẩn bằng chứng':'Xem bằng chứng');show.onclick=async()=>{if(expanded.has(j.id)){expanded.delete(j.id);evidence.replaceChildren();show.textContent='Xem bằng chứng'}else{expanded.add(j.id);show.textContent='Ẩn bằng chứng';await showEvidence(j,evidence)}};buttons.append(show);if(j.archived)a.append(node('div','Đã lưu trữ lịch sử · Khóa chống trùng được giữ lại','details'));if(j.retry_at)a.append(node('div',(j.waiting_for_inputs?'Kiểm tra lại đầu vào lúc ':'Chờ dịch vụ AI đến ')+new Date(j.retry_at*1000).toLocaleTimeString('vi-VN'),'details'));const actions=['COMPLETED','CANCELLED','FAILED'].includes(j.state)?(j.archived?[]:['archive']):['PAUSING','CANCELLING'].includes(j.state)?[]:['PAUSED','BLOCKED'].includes(j.state)?['resume','cancel']:['pause','cancel'];for(const action of actions){const b=node('button',{pause:'Tạm dừng',resume:'Tiếp tục',cancel:'Hủy nhiệm vụ',archive:'Lưu trữ'}[action],action==='cancel'?'cancel':'');b.onclick=async()=>{b.disabled=true;try{await api('/api/agents/control',{job_id:j.id,action,expected_updated:j.updated});document.getElementById('message').textContent='';await load()}catch(e){report(e);b.disabled=false}};buttons.append(b)}a.append(buttons,evidence);list.append(a);if(expanded.has(j.id))await showEvidence(j,evidence)}if(!d.jobs.length)list.append(node('p','Chưa có nhiệm vụ tự động.'));document.getElementById('updated').textContent='Cập nhật lúc '+new Date().toLocaleTimeString('vi-VN')}catch(e){report(e)}finally{loading=false}}
document.getElementById('health').onclick=async()=>{const b=document.getElementById('health');b.disabled=true;try{const d=await api('/api/agents/health');const labels={healthy:'Hoạt động',degraded:'Cần kiểm tra',unavailable:'Không kết nối được',version_mismatch:'Sai phiên bản',not_started:'Chưa khởi động',stopped:'Đã dừng',dead:'Tiến trình đã thoát',stalled:'Chậm phản hồi'};document.getElementById('health-result').textContent=Object.entries(d.components).map(([k,v])=>k+': '+(labels[v.state]||v.state)+(v.last_healthy_at?' · Lần khỏe gần nhất: '+new Date(v.last_healthy_at*1000).toLocaleString('vi-VN'):'')).join('\n')+'\nTunnel: '+d.tunnel.process_count+' tiến trình'+(d.tunnel.identity_changed_since_last_probe?' · Có thay đổi tiến trình':'')+' · Chưa kiểm tra kết nối bên ngoài'+(d.last_watchdog_event?'\nWatchdog: '+d.last_watchdog_event.code:'')}catch(e){report(e)}finally{b.disabled=false}};
document.getElementById('backup').onclick=async()=>{const b=document.getElementById('backup');b.disabled=true;try{const d=await api('/api/agents/backup',{action:'create_and_drill'});document.getElementById('health-result').textContent='Đã sao lưu và kiểm tra phục hồi thành công. '+d.drill.counts.agent_jobs+' nhiệm vụ được giữ lại; không chạy lại tác vụ.'}catch(e){report(e)}finally{b.disabled=false}};
document.getElementById('refresh').onclick=load;load();setInterval(load,5000);</script></html>'''
