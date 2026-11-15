"""Bounded public-region review, automatically checkpointed per URL and stage."""
import asyncio,hashlib,json
from .dom_progress import key,public_url

async def run(progress,owner,task,urls,dom,allowed,max_items=3,detail_selector=None,variant_selector=None):
    key(task)
    if not isinstance(urls,list) or not 1<=len(urls)<=100 or any(not isinstance(u,str) for u in urls):raise ValueError('Provide 1..100 URLs')
    if type(max_items) is not int or not 1<=max_items<=3:raise ValueError('max_items requires 1..3')
    for url in urls:public_url(url);allowed(url)
    progress.ensure_plan(owner,task,detail_selector,variant_selector)
    stages=[('title',None)]+([('detail',detail_selector)] if detail_selector else [])+([('variants',variant_selector)] if variant_selector else [])
    checkpoint=progress.status(owner,task);previous=checkpoint['items'];state={r['item_key']:r for r in previous}
    if checkpoint['truncated']:raise ValueError('Checkpoint list limit reached; use a narrower task key')
    if any(row['status'] in {'blocked','unknown'} for row in previous):
        return {'task_key':task,'results':[{'url':url,'state':'inspection_required'} for url in dict.fromkeys(urls)],'automatic_checkpoints':True,'automatic_replay':False,'checkpoint_items':len(previous),'task_paused':True}
    results=[]
    def save(item,url,stage,status,summary):
        revision=state.get(item,{}).get('revision',0)
        record=progress.record(owner,task,item,url,stage,status,summary,revision)
        state[item]={'revision':record['revision'],'status':status}
    async with asyncio.timeout(20):
        for url in dict.fromkeys(urls):
            ident=hashlib.sha256(url.encode()).hexdigest()[:32]
            needed=[(stage,selector) for stage,selector in stages if state.get(ident+'-'+stage,{}).get('status')!='completed']
            if not needed:
                results.append({'url':url,'state':'already_completed'});continue
            if any(state.get(ident+'-'+stage,{}).get('status') in ('unknown','blocked') for stage,selector in needed):
                results.append({'url':url,'state':'inspection_required'});continue
            if sum(r['state']!='already_completed' and r['state']!='inspection_required' for r in results)>=max_items:break
            tab=None;keep_for_inspection=False;stage,selector=needed[0];item=ident+'-'+stage
            # If interrupted after dispatch starts, never silently replay it.
            save(item,url,stage,'unknown','Review in progress; inspect owned tabs before retry')
            try:
                opened=await dom.open_tab(url);tab=opened['tab_ref']
                from .dom_content_state import observe
                content_state = await observe(dom, tab, opened.get('title'), opened.get('url'))
                if content_state['human_action_required']:
                    for blocked_stage, _ in needed:
                        save(ident+'-'+blocked_stage,url,blocked_stage,'blocked',json.dumps(content_state))
                    keep_for_inspection=True
                    results.append({'url':url,'state':'inspection_required','content_state':content_state,'tab_ref':tab,'owned_tab_retained':True})
                    break
                for stage,selector in needed:
                    item=ident+'-'+stage
                    if stage=='title':
                        actual_url=opened.get('url')
                        try:public_url(actual_url)
                        except (ValueError,TypeError):actual_url=None
                        data={'title':opened.get('title'),'url':actual_url};status='completed' if data['title'] else 'blocked'
                    else:
                        save(item,url,stage,'unknown','Region read in progress')
                        found=await dom.find(tab,selector=selector,max_items=8)
                        data=[]
                        for match in found['matches']:
                            observed=await dom.read_text(match['element_ref'],max_chars=1200)
                            data.append({'text':observed.get('text',''),'truncated':bool(observed.get('truncated',False))})
                        status='completed' if data else 'blocked'
                    save(item,url,stage,status,json.dumps(data,ensure_ascii=False)[:2000])
                incomplete=any(state[ident+'-'+s]['status']!='completed' for s,_ in needed)
                keep_for_inspection=incomplete
                results.append({'url':url,'state':'inspection_required' if incomplete else 'observed','stages':[s for s,_ in needed],'business_outcome_verified':False,**({'tab_ref':tab,'owned_tab_retained':True} if incomplete else {})})
                if incomplete:break
            except Exception as exc:
                from .execution_errors import diagnostic
                error=diagnostic(exc,'dom_review_run');save(item,url,stage,'unknown' if error['delivery_state']=='unknown' else 'blocked',error['code'])
                keep_for_inspection=tab is not None
                results.append({'url':url,'state':'failed','error':error,**({'tab_ref':tab,'owned_tab_retained':True} if tab else {})})
                break
            finally:
                if tab and not keep_for_inspection:
                    try:
                        async with asyncio.timeout(5):await dom.close_tab(tab)
                    except BaseException:
                        save(ident+'-cleanup',url,'action','unknown','Owned-tab close did not finish; inspect before another action')
                        if asyncio.current_task().cancelling():raise
            if state.get(ident+'-cleanup',{}).get('status')=='unknown':break
    final=progress.status(owner,task)['items']
    return {'task_key':task,'results':results,'automatic_checkpoints':True,'automatic_replay':False,'checkpoint_items':len(final),'task_paused':any(row['status'] in {'blocked','unknown'} for row in final)}
