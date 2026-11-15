"""Fixed local service commands via an owner-controlled SQLite mailbox, no HTTP API."""
from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3
import time
import os
import psutil

ACTIONS={'start','status','stop'}
def secure_directory(path):
    """The mailbox accepts commands only from the current Windows owner or SYSTEM."""
    import os
    path=Path(path);path.mkdir(parents=True,exist_ok=True)
    if os.name!='nt':return
    import win32api,win32con,win32security
    token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
    try:owner=win32security.GetTokenInformation(token,win32security.TokenUser)[0]
    finally:token.Close()
    acl=win32security.ACL()
    for sid in (owner,win32security.ConvertStringSidToSid('S-1-5-18')):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS,0x1|0x2,0x1F01FF,sid)
    win32security.SetNamedSecurityInfo(str(path),win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION|win32security.PROTECTED_DACL_SECURITY_INFORMATION,None,None,acl,None)

def initialize(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as c,c:
        c.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,action TEXT,created REAL,expires REAL,state TEXT,result TEXT)')
        columns={row[1] for row in c.execute('PRAGMA table_info(requests)')}
        for name,kind in [('client_pid','INTEGER'),('client_created','REAL')]:
            if name not in columns:c.execute('ALTER TABLE requests ADD COLUMN '+name+' '+kind)

def caller_alive(pid,created):
    try:return type(pid) is int and psutil.Process(pid).create_time()==created
    except (psutil.NoSuchProcess,psutil.AccessDenied,ValueError):return False

def request(path,action,timeout=25):
    if action not in ACTIONS:raise PermissionError('Unknown service action')
    path=Path(path)
    if not path.is_file():raise RuntimeError('Owner service supervisor is not installed')
    ident=secrets.token_urlsafe(24);now=time.time()
    with closing(sqlite3.connect(path,timeout=5)) as c,c:
        c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?)',(ident,action,now,now+timeout,'pending',None,os.getpid(),psutil.Process().create_time()))
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        with closing(sqlite3.connect(path,timeout=5)) as c:
            row=c.execute('SELECT state,result FROM requests WHERE id=?',(ident,)).fetchone()
        if row and row[0]=='done':return json.loads(row[1])
        time.sleep(.1)
    with closing(sqlite3.connect(path)) as c,c:
        c.execute("UPDATE requests SET state='expired' WHERE id=? AND state='pending'",(ident,))
    raise TimeoutError('Service command has no confirmed result; status is safe to retry')

def claim(path,now=None):
    now=time.time() if now is None else now
    with closing(sqlite3.connect(path,timeout=5)) as c,c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE requests SET state='expired' WHERE state='pending' AND expires<?",(now,))
        c.execute("DELETE FROM requests WHERE state IN ('done','expired') AND created<?",(now-86400,))
        row=c.execute("SELECT id,action,client_pid,client_created FROM requests WHERE state='pending' AND expires>=? ORDER BY created LIMIT 1",(now,)).fetchone()
        if row:
            if row[1] not in ACTIONS:
                c.execute("UPDATE requests SET state='done',result=? WHERE id=?",(json.dumps({'state':'denied'}),row[0]));return None
            if not caller_alive(row[2],row[3]):
                c.execute("UPDATE requests SET state='expired' WHERE id=?",(row[0],));return None
            c.execute("UPDATE requests SET state='running' WHERE id=?",(row[0],))
    return row

def complete(path,ident,result):
    with closing(sqlite3.connect(path)) as c,c:
        c.execute("UPDATE requests SET state='done',result=? WHERE id=? AND state='running'",(json.dumps(result),ident))
