"""Frozen file workflow DAGs: explicit branches, durable checkpoints, no replay repair."""
import re
import time
import os

from .intake import output_conditions
from .recipes import prepare as prepare_recipe


def prepare(service,principal,workspace,nodes):
    if not isinstance(nodes,list) or not 1<=len(nodes)<=12:
        raise ValueError('Workflow requires 1..12 declared nodes')
    normalized=[];ids=set();paths={};total=0
    for spec in nodes:
        if not isinstance(spec,dict) or not {'id','title','needs'}<=set(spec) or set(spec)-{'id','title','needs','outputs','recipe','mode','when','needs_policy'}:
            raise ValueError('Node requires id, title, needs and outputs or recipe')
        nid=spec['id']
        if not isinstance(nid,str) or not re.fullmatch('[A-Za-z0-9_-]{1,40}',nid) or nid in ids:
            raise ValueError('Unique concrete node IDs required')
        ids.add(nid)
        if not isinstance(spec['title'],str) or not 1<=len(spec['title'])<=200:
            raise ValueError('Node title must be bounded')
        needs=spec['needs'];policy=spec.get('needs_policy','all')
        if not isinstance(needs,list) or len(needs)>12 or any(not isinstance(n,str) for n in needs) or len(set(needs))!=len(needs) or policy not in {'all','settled'}:
            raise ValueError('Explicit unique dependencies and all/settled policy required')
        recipe=None
        if 'recipe' in spec:
            if 'outputs' in spec or 'mode' in spec:
                raise ValueError('Choose recipe or outputs/mode')
            recipe,criteria=prepare_recipe(workspace,spec['recipe'])
        else:
            criteria=output_conditions(workspace,spec.get('outputs'))
            mode=spec.get('mode','exact')
            if mode=='exact':
                recipe,_=prepare_recipe(workspace,{'kind':'files','outputs':spec.get('outputs')})
            elif mode!='ai':
                raise ValueError('Workflow mode must be exact or ai')
        when=spec.get('when',[])
        service.engine._conditions(when,principal,required=False)
        if len(when)>10 or any(c['tool']!='filesystem_read_text' for c in when):
            raise ValueError('Branch requires at most 10 explicit file-read predicates')
        for c in criteria:
            path=os.path.normcase(c['arguments']['path'])
            if path in paths:
                raise ValueError('Different nodes cannot own the same output path')
            paths[path]=nid
        total+=len(criteria)+len(when)
        normalized.append({'id':nid,'title':spec['title'],'needs':needs,'needs_policy':policy,'criteria':criteria,'when':when,'recipe':recipe})
    if total>100:
        raise ValueError('At most 100 node output/branch conditions')
    if any(nid not in ids for n in normalized for nid in n['needs']):
        raise ValueError('Unknown dependency')
    settled=set()
    while len(settled)<len(ids):
        ready=[n['id'] for n in normalized if n['id'] not in settled and set(n['needs'])<=settled]
        if not ready:
            raise ValueError('Workflow dependency cycle')
        settled.update(ready)
    by_id={n['id']:n for n in normalized}
    def ancestors(nid):
        result=set(by_id[nid]['needs'])
        for parent in by_id[nid]['needs']:result.update(ancestors(parent))
        return result
    for node in normalized:
        for c in node['when']:
            producer=paths.get(os.path.normcase(c['arguments'].get('path','')))
            if producer and producer not in ancestors(node['id']):
                raise ValueError('Branch input producer must be a declared ancestor')
    return normalized


def current(doc):
    return next((n for n in doc.get('graph',[]) if n['id']==doc.get('graph_current')),None)


async def predicate(service,doc,principal,node):
    passed=await service._goals(doc,principal,node['when'])
    if any(e.get('error_type') for e in doc['goal_evidence']):
        raise RuntimeError('Branch predicate unavailable; no guessed branch')
    return passed


async def progress(service,doc,principal):
    states=doc.setdefault('graph_states',{})
    for node in doc['graph']:
        state=states.get(node['id'],{})
        if state.get('state')=='DONE':
            if not await service._goals(doc,principal,node['criteria']):
                raise RuntimeError('Verified workflow output changed; completed node will not replay')
        if state.get('predicate') is not None and node['when']:
            if await predicate(service,doc,principal,node)!=state['predicate']:
                raise RuntimeError('Frozen workflow branch changed; inspect before continuing')
    node=current(doc)
    if node:
        if await service._goals(doc,principal,node['criteria']):
            validators=node['recipe']['validators'] if node['recipe'] else []
            if not await service._quality(doc,principal,validators):
                raise RuntimeError('Workflow node quality failed')
            if doc['cycles']>=doc['max_cycles']:
                raise RuntimeError('Workflow cycle budget exhausted')
            states[node['id']].update(state='DONE',verified_at=time.time(),evidence=doc['goal_evidence'])
            doc['cycles']+=1;doc['graph_current']=None
            service._save(doc,'graph_node_verified')
            return 'advance'
        return 'plan'
    for node in doc['graph']:
        if node['id'] in states:
            continue
        parents=[states.get(p,{}).get('state') for p in node['needs']]
        if any(s not in {'DONE','SKIPPED'} for s in parents):
            continue
        if node['needs_policy']=='all' and 'SKIPPED' in parents:
            states[node['id']]={'state':'SKIPPED','reason':'dependency_skipped'}
            service._save(doc,'graph_dependency_skipped')
            return 'advance'
        passed=await predicate(service,doc,principal,node) if node['when'] else True
        states[node['id']]={'state':'SELECTED' if passed else 'SKIPPED','predicate':passed if node['when'] else None,
                            'branch_evidence':doc.get('goal_evidence',[]) if node['when'] else []}
        if passed:doc['graph_current']=node['id']
        service._save(doc,'graph_node_selected' if passed else 'graph_branch_skipped')
        return 'advance'
    if len(states)!=len(doc['graph']):
        raise RuntimeError('Workflow has no ready node')
    if await service._goals(doc,principal) and await service._quality(doc,principal):
        return 'complete'
    raise RuntimeError('Declared workflow nodes cannot satisfy remaining final requirements')
