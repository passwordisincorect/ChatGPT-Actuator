"""Owner-scoped durable progress and explicitly selected browser bookmarks."""
import json,re,sqlite3,time
from contextlib import contextmanager
from urllib.parse import urlsplit,parse_qsl
from .automation_errors import PolicyDenied

def key(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',value):raise ValueError('Invalid progress key')
    return value

def public_url(url):
    if not isinstance(url,str) or not 1<=len(url)<=4096 or any(ord(ch)<32 for ch in url):raise ValueError('Bounded URL without control characters required')
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:raise ValueError('HTTP/HTTPS URL required')
    parsed.port  # Validate malformed ports before persistence.
    fragment=parsed.fragment.split('?',1)[-1]
    if any(re.search(r'token|password|secret|authorization|cookie|api[_-]?key',k,re.I) for k,v in parse_qsl(parsed.query)+parse_qsl(fragment)):raise ValueError('Credential-bearing URLs cannot be persisted')
    return url

class DOMProgress:
    def __init__(self,path):
        self.path=path
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS dom_progress(owner TEXT,task TEXT,item TEXT,url TEXT,stage TEXT,status TEXT,summary TEXT,revision INTEGER,updated REAL,PRIMARY KEY(owner,task,item))')
            c.execute('CREATE TABLE IF NOT EXISTS browser_choices(owner TEXT,name TEXT,endpoint TEXT,target TEXT,url TEXT,revision INTEGER,updated REAL,PRIMARY KEY(owner,name))')
            c.execute('CREATE TABLE IF NOT EXISTS review_plans(owner TEXT,task TEXT,details TEXT,variants TEXT,PRIMARY KEY(owner,task))')
            c.execute('CREATE TABLE IF NOT EXISTS cart_checkpoints(owner TEXT,task TEXT,item TEXT,data TEXT,revision INTEGER,identity_hash TEXT,state TEXT,PRIMARY KEY(owner,task,item))')
            c.execute('CREATE INDEX IF NOT EXISTS cart_identity_state ON cart_checkpoints(owner,identity_hash,state)')
    @contextmanager
    def db(self):
        connection=sqlite3.connect(self.path,timeout=5)
        try:
            with connection:yield connection
        finally:connection.close()
    def status(self,owner,task):
        key(task)
        with self.db() as c:
            rows=c.execute('SELECT item,url,stage,status,summary,revision,updated FROM dom_progress WHERE owner=? AND task=? ORDER BY updated,item LIMIT 1000',(owner,task)).fetchall()
            plan=c.execute('SELECT details,variants FROM review_plans WHERE owner=? AND task=?',(owner,task)).fetchone()
            total=c.execute('SELECT COUNT(*) FROM dom_progress WHERE owner=? AND task=?',(owner,task)).fetchone()[0]
        return {'task_key':task,'total_items':total,'truncated':total>len(rows),'plan':{'detail_selector':plan[0],'variant_selector':plan[1]} if plan else None,'items':[dict(zip(('item_key','url','stage','status','summary','revision','updated'),r)) for r in rows], 'automatic_replay':False,'resume_rule':'Continue pending items with fresh owned DOM refs; inspect blocked/unknown outcomes before explicitly changing them','source':'caller_reported_checkpoint'}
    def ensure_plan(self,owner,task,details,variants):
        key(task)
        for selector in (details,variants):
            if selector is not None and (not isinstance(selector,str) or not selector.strip() or len(selector)>1000):raise ValueError('Bounded nonempty region selector required')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT details,variants FROM review_plans WHERE owner=? AND task=?',(owner,task)).fetchone()
            if old:
                if old!=(details,variants):raise ValueError('Review selectors changed; use a new task key to avoid reusing old evidence')
                return
            if c.execute('SELECT 1 FROM dom_progress WHERE owner=? AND task=? LIMIT 1',(owner,task)).fetchone():raise ValueError('Legacy checkpoint has no saved review selectors; use a new task key')
            if c.execute('SELECT COUNT(*) FROM review_plans WHERE owner=?',(owner,)).fetchone()[0]>=1000:raise PermissionError('RESOURCE_LIMIT: review plan budget reached')
            c.execute('INSERT INTO review_plans VALUES(?,?,?,?)',(owner,task,details,variants))
    def record(self,owner,task,item,url,stage,status,summary,expected_revision):
        key(task);key(item);public_url(url)
        if stage not in ('title','detail','variants','action') or status not in ('pending','completed','blocked','unknown'):raise ValueError('Invalid checkpoint state')
        if not isinstance(summary,str) or len(summary)>2000:raise ValueError('Summary exceeds 2000 characters')
        if not isinstance(expected_revision,int) or isinstance(expected_revision,bool) or expected_revision<0:raise ValueError('Expected revision required')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT revision FROM dom_progress WHERE owner=? AND task=? AND item=?',(owner,task,item)).fetchone()
            if (old[0] if old else 0)!=expected_revision:raise ValueError('Checkpoint revision conflict; read status again')
            if not old and c.execute('SELECT COUNT(*) FROM dom_progress WHERE owner=?',(owner,)).fetchone()[0]>=10000:raise PermissionError('RESOURCE_LIMIT: checkpoint budget reached')
            revision=expected_revision+1
            c.execute('INSERT OR REPLACE INTO dom_progress VALUES(?,?,?,?,?,?,?,?,?)',(owner,task,item,url,stage,status,summary,revision,time.time()))
        return {'task_key':task,'item_key':item,'revision':revision,'recorded':True,'automatic_replay':False,'source':'caller_reported_checkpoint'}
    def choices(self,owner):
        with self.db() as c:
            rows=c.execute('SELECT name,endpoint,target,url,revision FROM browser_choices WHERE owner=? ORDER BY name',(owner,)).fetchall()
        return {'choices':[dict(zip(('name','cdp_url','tab_id','expected_url','revision'),r)) for r in rows],'cookies_stored':False,'debugging_enabled':False}
    def remember(self,owner,name,endpoint,target,url,expected_revision):
        key(name);public_url(url)
        from .dom_attachment import endpoint as validate_endpoint
        endpoint=validate_endpoint(endpoint)
        if not isinstance(expected_revision,int) or isinstance(expected_revision,bool) or expected_revision<0:raise ValueError('Expected revision required')
        if not isinstance(target,str) or not 1<=len(target)<=256:raise ValueError('Target ID required')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT revision FROM browser_choices WHERE owner=? AND name=?',(owner,name)).fetchone()
            if (old[0] if old else 0)!=expected_revision:raise ValueError('Browser choice revision conflict')
            if not old and c.execute('SELECT COUNT(*) FROM browser_choices WHERE owner=?',(owner,)).fetchone()[0]>=16:raise PermissionError('RESOURCE_LIMIT: browser choice budget reached')
            c.execute('INSERT OR REPLACE INTO browser_choices VALUES(?,?,?,?,?,?,?)',(owner,name,endpoint,target,url,expected_revision+1,time.time()))
        return {'name':name,'revision':expected_revision+1,'remembered':True,'cookies_stored':False,'automatic_attach':False}
    def choice(self,owner,name):
        key(name)
        with self.db() as c:row=c.execute('SELECT endpoint,target,url FROM browser_choices WHERE owner=? AND name=?',(owner,name)).fetchone()
        if not row:raise PolicyDenied('wrong_owner')
        return dict(zip(('cdp_url','tab_id','expected_url'),row))

    def manage(self,owner,name,action,expected_revision,new_name=None):
        key(name)
        if action not in ('rename','forget'):raise ValueError('Invalid bookmark action')
        if action=='rename':key(new_name)
        elif new_name is not None:raise ValueError('Forget does not accept a new name')
        if type(expected_revision) is not int or expected_revision<1:raise ValueError('Expected revision required')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT revision FROM browser_choices WHERE owner=? AND name=?',(owner,name)).fetchone()
            if not row:raise PolicyDenied('wrong_owner')
            if row[0]!=expected_revision:raise ValueError('Bookmark revision conflict')
            if action=='rename':
                if c.execute('SELECT 1 FROM browser_choices WHERE owner=? AND name=?',(owner,new_name)).fetchone():raise ValueError('Bookmark name exists')
                c.execute('UPDATE browser_choices SET name=?,revision=?,updated=? WHERE owner=? AND name=?',(new_name,expected_revision+1,time.time(),owner,name))
            else:c.execute('DELETE FROM browser_choices WHERE owner=? AND name=?',(owner,name))
        return {'name':new_name if action=='rename' else name,'action':action,'updated':True,'revision':expected_revision+1,'browser_modified':False}
