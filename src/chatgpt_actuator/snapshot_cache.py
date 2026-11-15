"""Small session-bound in-memory snapshot history. No labels are written to disk."""
import json,time,secrets
from collections import OrderedDict
from .tool_policy import principal_context

def owner_scope(target):
    p=principal_context.get()
    return (p.owner,p.session,str(target)) if p else ('local','default',str(target))

class SnapshotCache:
    def __init__(self,limit=16,ttl=300):self.entries=OrderedDict();self.limit=limit;self.ttl=ttl
    def prune(self):
        now=time.monotonic()
        for token,(expires,scope,value) in list(self.entries.items()):
            if expires<=now:self.entries.pop(token,None)
    def forget_target(self,target):
        for token,(_,scope,_) in list(self.entries.items()):
            if scope[-1]==str(target):self.entries.pop(token,None)
    def forget_session(self,owner,session):
        for token,(_,scope,_) in list(self.entries.items()):
            if scope[:2]==(owner,session):self.entries.pop(token,None)
    def store(self,scope,snapshot):
        self.prune()
        raw=json.dumps(snapshot)
        if len(raw.encode())>2*1024*1024:raise ValueError('SNAPSHOT_LIMIT: capture too large')
        token='snapshot1:'+secrets.token_urlsafe(24)
        self.entries[token]=(time.monotonic()+self.ttl,scope,json.loads(raw))
        while len(self.entries)>self.limit:self.entries.popitem(last=False)
        return token
    def get(self,token,scope):
        self.prune();entry=self.entries.get(token)
        if not entry:raise LookupError('STALE_SNAPSHOT: expired or evicted; capture again')
        if entry[1]!=scope:raise PermissionError('Snapshot belongs to another session or target')
        return entry[2]
    def diff(self,previous,current,scope,*,compact=False,max_changes=100):
        old,new=self.get(previous,scope),self.get(current,scope)
        def index(snapshot):
            nodes=snapshot.get('nodes',snapshot.get('elements',[]));result={}
            for node in nodes:
                key=node.get('node_key') or node.get('element_ref')
                if not key:continue
                value={k:v for k,v in node.items() if k not in {'index','parent_index','frame_index','depth'}}
                parent=node.get('parent_index')
                value['parent_key']=(nodes[parent].get('node_key') or nodes[parent].get('element_ref')) if parent is not None and 0<=parent<len(nodes) else None
                result[key]=value
            return result
        a,b=index(old),index(new)
        partial=any(s.get('truncated') or s.get('omitted_frames',0) or s.get('omitted_open_shadow_roots',0) for s in (old,new)) or old.get('limits')!=new.get('limits') or old.get('capture_options')!=new.get('capture_options')
        missing=[key for key in a if key not in b]
        result={'previous_snapshot_ref':previous,'snapshot_ref':current,'partial':bool(partial),
                'added':[b[k] for k in b if k not in a],
                'changed':[{'before':a[k],'after':b[k]} for k in b if k in a and a[k]!=b[k]],
                'removed':[] if partial else missing,'not_observed':missing if partial else []}

        if not compact:return result
        if type(max_changes) is not int or not 1<=max_changes<=1000:
            raise ValueError('max_changes must be an integer in 1..1000')
        total=sum(len(result[key]) for key in ('added','changed','removed','not_observed'))
        left=max_changes
        for key in ('added','changed','removed','not_observed'):
            result[key]=result[key][:left]
            left-=len(result[key])
        result['changed']=[{'node_key':row['after'].get('node_key') or row['after'].get('element_ref'),
            'fields':{key:{'before':row['before'].get(key),'after':row['after'].get(key)}
            for key in set(row['before'])|set(row['after']) if row['before'].get(key)!=row['after'].get(key)}}
            for row in result['changed']]
        result.update(compact=True,total_changes=total,omitted_changes=max(0,total-max_changes),changes_truncated=total>max_changes)
        return result
