"""Local-owner task view. No desktop automation, automatic launch or file opening."""
from contextlib import contextmanager
import json
import re
import sqlite3
from pathlib import Path

def clean_output(value):
    text=str(value or '')[:8192]
    return '\n'.join('[đã ẩn dữ liệu nhạy cảm]' if re.search(r'(?i)token|cookie|authorization|bearer|password|secret|api[_-]?key',line) else line for line in text.splitlines())

class TaskDashboard:
    def __init__(self,path):self.path=Path(path)
    @contextmanager
    def connect(self):
        if not self.path.is_file():raise RuntimeError('Task database not ready')
        c=sqlite3.connect(self.path,timeout=5);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def list(self):
        with self.connect() as c:
            has_labels=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempt_labels'").fetchone()
            query=("SELECT a.id,a.session,a.status,a.created,a.cancel,COALESCE(l.label,'') AS label FROM attempts a LEFT JOIN attempt_labels l ON l.id=a.id"
                   if has_labels else "SELECT a.id,a.session,a.status,a.created,a.cancel,'' AS label FROM attempts a")
            rows=c.execute(query+' ORDER BY a.created DESC LIMIT 100').fetchall()
        tasks=[dict(row) for row in rows]
        with self.connect() as c:
            lifecycle=c.execute("SELECT 1 FROM sqlite_master WHERE name='attempt_lifecycle'").fetchone()
            for task in tasks:
                started=c.execute('SELECT started FROM attempt_lifecycle WHERE id=?',(task['id'],)).fetchone() if lifecycle else None
                task['phase']='finished' if task['status']!='running' else 'cancelling' if task['cancel'] else 'received' if started and started[0] is None else 'running'
        return {'tasks':tasks,'background_only':True}
    def detail(self,ident):
        with self.connect() as c:row=c.execute('SELECT status,result FROM attempts WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('Unknown task')
        result=json.loads(row['result']) if row['result'] else {}
        return {'status':row['status'],'stdout':clean_output(result.get('stdout')),'stderr':clean_output(result.get('stderr')),
                'exit_code':result.get('exit_code'),'output_truncated':result.get('output_truncated',False)}
    def cancel(self,ident):
        with self.connect() as c:
            row=c.execute('SELECT status FROM attempts WHERE id=?',(ident,)).fetchone()
            if not row:raise ValueError('Unknown task')
            if row['status']=='running':c.execute('UPDATE attempts SET cancel=1 WHERE id=? AND status=?',(ident,'running'))
        return {'cancel_requested':row['status']=='running'}

HTML='''<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Tác vụ nền Actuator</title>
<style>body{font:16px system-ui;background:#101725;color:#e6edf7;margin:2rem auto;max-width:1000px;padding:1rem}button{padding:.5rem;margin:.3rem;background:#253653;color:inherit;border:1px solid #526681;border-radius:6px}article{padding:1rem;border:1px solid #40516d;margin:1rem 0;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#a9bad3}</style>
<h1>Tác vụ nền</h1><p>Thao tác chạy nền. Kết quả chỉ hiển thị khi bạn bấm “Xem kết quả”.</p><button id="refresh">Làm mới</button><span id="state"></span><main id="tasks"></main>
<script>const token=__TOKEN__;const headers={'X-Control-Center-Token':token,'Content-Type':'application/json'};
async function api(path,body){const response=await fetch(path,{headers,method:body?'POST':'GET',body:body?JSON.stringify(body):undefined});if(!response.ok)throw Error('Không đọc được tác vụ');return response.json()}
async function load(){try{const data=await api('/api/tasks');const root=document.getElementById('tasks');root.replaceChildren();for(const t of data.tasks){const card=document.createElement('article');const title=document.createElement('strong');title.textContent=(t.label||'Lệnh nền')+' — '+({received:'Đã nhận',running:'Đang chạy',cancelling:'Đang hủy',finished:'Đã kết thúc'}[t.phase]||t.status)+' ('+t.status+')';card.append(title);const date=document.createElement('p');date.textContent=new Date(t.created*1000).toLocaleString('vi-VN');card.append(date);const output=document.createElement('pre');output.hidden=true;const show=document.createElement('button');show.textContent='Xem kết quả';show.onclick=async()=>{try{const r=await api('/api/task?id='+encodeURIComponent(t.id));output.textContent=(r.stdout||'')+'\\n'+(r.stderr||'');output.hidden=false}catch(e){output.textContent=e.message;output.hidden=false}};card.append(show,output);if(t.status==='running'){const cancel=document.createElement('button');cancel.textContent=t.cancel?'Đang hủy…':'Hủy tác vụ';cancel.disabled=!!t.cancel;cancel.onclick=async()=>{await api('/api/tasks/cancel',{attempt_id:t.id});await load()};card.append(cancel)}root.append(card)}document.getElementById('state').textContent=data.tasks.length?'':'Chưa có tác vụ'}catch(e){document.getElementById('state').textContent=e.message}}
document.getElementById('refresh').onclick=load;load();</script></html>'''
