"""Minimal versioned evidence: no payloads, identities, URLs or credentials."""
from contextlib import closing
from contextvars import ContextVar
import sqlite3
import time
import hashlib
import re
import json
from pathlib import Path
signed_gateway_request=ContextVar('signed_gateway_request',default=False)
ALLOWED={'windows_background','headless_dom','backend_rpc','gateway_signed_backend_rpc','tailscale_authenticated_mcp','stdio_mcp','openai_authenticated_mcp'}
def transport_category(signed,owner,transport,source,connection_kind=None):
    if signed:return 'tailscale_authenticated_mcp' if connection_kind=='oauth_gateway' else 'gateway_signed_backend_rpc'
    if transport=='stdio':return 'openai_authenticated_mcp' if source=='openai_tunnel' else 'stdio_mcp'
    return None
class Evidence:
    def __init__(self,path):self.path=Path(path)
    def record(self,name,version,status='verified'):
        if name not in ALLOWED or status not in {'verified','failed','unavailable'}:raise ValueError('Invalid evidence category')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path,timeout=5)) as c, c:
            c.execute('CREATE TABLE IF NOT EXISTS evidence(name TEXT PRIMARY KEY,version TEXT,status TEXT,checked REAL)')
            c.execute('INSERT OR REPLACE INTO evidence VALUES(?,?,?,?)',(name,version,status,time.time()))
    def snapshot(self,version,max_age=86400):
        if not self.path.is_file():return {}
        with closing(sqlite3.connect(self.path,timeout=5)) as c, c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence'").fetchone():return {}
            rows=c.execute('SELECT name,version,status,checked FROM evidence').fetchall()
        now=time.time()
        return {name:{'state':status if v==version and 0<=now-checked<=max_age else 'stale',
                      'runtime_version':v,'checked_at':checked} for name,v,status,checked in rows}

    @staticmethod
    def _scope(owner, session):
        return hashlib.sha256(json.dumps([owner,session],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

    def record_dispatch(self, owner, session, version, tool, source):
        if not re.fullmatch(r'[A-Za-z0-9_]{1,128}', tool):
            raise ValueError('Bounded tool name required')
        if source not in {'oauth_gateway','signed_gateway', 'configured_stdio', 'local_or_unidentified'}:
            raise ValueError('Known dispatch source required')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as c, c:
            c.execute('CREATE TABLE IF NOT EXISTS dispatch_evidence(scope TEXT PRIMARY KEY, version TEXT, tool TEXT, source TEXT, checked REAL)')
            now = time.time()
            c.execute('DELETE FROM dispatch_evidence WHERE checked<?', (now-7*86400,))
            c.execute('INSERT OR REPLACE INTO dispatch_evidence VALUES(?,?,?,?,?)',
                      (self._scope(owner,session), version, tool, source, now))
            c.execute('DELETE FROM dispatch_evidence WHERE scope IN (SELECT scope FROM dispatch_evidence ORDER BY checked DESC LIMIT -1 OFFSET 10000)')

    def dispatch_snapshot(self, owner, session, version, max_age=300):
        if not self.path.is_file(): return None
        with closing(sqlite3.connect(self.path, timeout=5)) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dispatch_evidence'").fetchone(): return None
            row = c.execute('SELECT version,tool,source,checked FROM dispatch_evidence WHERE scope=?',
                            (self._scope(owner,session),)).fetchone()
        if not row: return None
        v,tool,source,checked = row
        return {'state': 'observed' if v==version and 0<=time.time()-checked<=max_age else 'stale',
                'runtime_version': v, 'tool': tool, 'source': source, 'checked_at': checked,
                'response_delivery_verified': False}
