"""DOM session limited to one explicitly selected CDP tab and its owned children."""
from urllib.parse import urlsplit
import asyncio,json,re

async def discover(cdp_urls,allowed):
    if not isinstance(cdp_urls,list) or not 1<=len(cdp_urls)<=8 or any(not isinstance(value,str) for value in cdp_urls):raise ValueError('Provide 1..8 explicit local endpoints')
    urls=[endpoint(value) for value in cdp_urls]
    import httpx
    async def probe(url):
        if url.startswith('ws://'):
            return {'state':'attachment_required','tabs':[],'endpoint_type':'approval_socket','loopback_port':urlsplit(url).port,'listener_verified':False,
                    'next_action':'Attach one explicitly selected tab with browser permission first; discovery does not create another browser connection'}
        try:
            async with httpx.AsyncClient(timeout=2,trust_env=False,follow_redirects=False) as client:
                async with client.stream('GET',url+'/json/list') as response:
                    response.raise_for_status();body=b''
                    async for chunk in response.aiter_bytes():
                        body+=chunk
                        if len(body)>131072:raise ValueError('Discovery response exceeds limit')
                rows=json.loads(body)
                if not isinstance(rows,list):raise ValueError('Expected target list')
                tabs=[]
                for row in rows[:128]:
                    if isinstance(row,dict) and row.get('type')=='page' and allowed(row.get('url','')) and isinstance(row.get('id'),str) and 1<=len(row['id'])<=128:
                        tabs.append({'tab_id':row['id'],'url':row['url'][:4096],'title':str(row.get('title',''))[:512]})
                return {'cdp_url':url,'state':'available','tabs':tabs}
        except (httpx.HTTPError,ValueError,TypeError):return {'cdp_url':url,'state':'unavailable','tabs':[],'next_action':'Enable CDP on a browser you choose, or explicitly use dom_open_tab(browser_mode=managed) for a separate ephemeral profile'}
    async def bounded(url):
        try:return await asyncio.wait_for(probe(url),timeout=3)
        except asyncio.TimeoutError:return {'cdp_url':url,'state':'unavailable','tabs':[],'next_action':'Discovery deadline reached; retry or use managed Edge'}
    return {'sessions':await asyncio.gather(*(bounded(url) for url in dict.fromkeys(urls))), 'debugging_enabled_by_tool':False,'profiles_restarted':False}

async def discover_owned(cdp_urls,external_sessions,key,allowed):
    """Read only the current owner's selected/owned pages through an existing lease."""
    if not isinstance(cdp_urls,list) or not 1<=len(cdp_urls)<=8 or any(not isinstance(value,str) for value in cdp_urls):raise ValueError('Provide1..8 explicit local endpoints')
    urls=list(dict.fromkeys(endpoint(value) for value in cdp_urls))
    results=[];http_pending=[]
    for url in urls:
        port=urlsplit(url).port
        leases=[(owner,item) for owner,item in external_sessions.items() if urlsplit(item['endpoint']).port==port]
        base={'endpoint_type':'approval_socket' if url.startswith('ws://') else 'http','loopback_port':port,'tabs':[]}
        if any(owner!=key for owner,item in leases):
            results.append({**base,'state':'leased_elsewhere','next_action':'Choose a browser not leased by another owner/session'});continue
        if leases:
            item=leases[0][1]
            # A different socket on the same port can identify a restarted browser.
            if (url.startswith('ws://') and url!=item['endpoint']) or not item['browser'].is_connected():
                results.append({**base,'state':'disconnected','next_action':'Inspect and explicitly reattach the intended current target; no fallback or replay'});continue
            tabs=[]
            owned_pages=list(item['context'].pages)
            for page in owned_pages[:8]:
                current=str(page.url)
                if page.is_closed() or not allowed(current):continue
                selected=page is item['context'].selected
                target=item.get('target_id') if selected else None
                probe=None
                try:
                    async with asyncio.timeout(2):
                        title=str(await page.title())[:512]
                        if target is None:
                            probe=await page.context.new_cdp_session(page)
                            target=(await probe.send('Target.getTargetInfo'))['targetInfo']['targetId']
                except Exception:
                    title='';target=None
                finally:
                    if probe is not None:
                        try:await asyncio.wait_for(probe.detach(),timeout=.5)
                        except Exception:pass
                # Do not present metadata sampled across a navigation as a match.
                if str(page.url)!=current or not allowed(str(page.url)):continue
                if target is not None:tabs.append({'tab_id':target,'url':current[:4096],'title':title,
                    'ownership':'selected_existing_tab' if selected else 'owned_child_tab'})
            results.append({**base,'state':'selected_session_only','scope':'selected_tab_and_owned_children',
                            'tabs':tabs,'existing_connection_reused':True,'actual_profile_known':False,
                            'truncated':len(owned_pages)>8,'max_pages':8})
        elif url.startswith('ws://'):
            results.append({**base,'state':'attachment_required','listener_verified':False,'next_action':'Verify the chosen endpoint and explicitly attach one selected tab with browser permission; no discovery connection was opened'})
        else:
            http_pending.append((len(results),url));results.append(None)
    if http_pending:
        probed=(await discover([url for index,url in http_pending],allowed))['sessions']
        for (index,url),row in zip(http_pending,probed):results[index]=row
    return {'sessions':results,'debugging_enabled_by_tool':False,'profiles_restarted':False,'new_browser_connections':0}

def endpoint(value):
    parsed=urlsplit(value)
    if parsed.scheme not in {'http','ws'} or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use a loopback CDP endpoint without credentials, query or fragment')
    if parsed.scheme=='http' and parsed.path not in {'','/'}:
        raise ValueError('HTTP CDP endpoint must be a root URL')
    if parsed.scheme=='ws' and not re.fullmatch(r'/devtools/browser/[A-Za-z0-9-]{1,128}',parsed.path):
        raise ValueError('WebSocket CDP endpoint must identify one browser target')
    if not parsed.port or not 1<=parsed.port<=65535:raise ValueError('CDP port required')
    return parsed.scheme+'://'+('['+parsed.hostname+']' if ':' in parsed.hostname else parsed.hostname)+':'+str(parsed.port)+(parsed.path if parsed.scheme=='ws' else '')

async def connect_selected(driver,url,timeout):
    """Approval-mode sockets preserve browser defaults and its permission prompt."""
    checked=endpoint(url)
    options={'timeout':180000 if checked.startswith('ws://') else timeout}
    if checked.startswith('ws://'):options['no_defaults']=True
    return await driver.chromium.connect_over_cdp(checked,**options)

def attachment_wait(method,args,kwargs,ordinary):
    if method!='attach_browser':return ordinary
    value=args[0] if args else kwargs.get('cdp_url')
    if isinstance(value,str) and value.startswith('ws://'):
        endpoint(value)
        return 180.0
    return ordinary

class SelectedContext:
    def __init__(self,context,page,register):
        self.context=context;self.selected=page;self.owned=[];self.retained=[];self.register=register
        self.listeners=[];self._watched={}
        register(page);self._watch(page)
    def _watch(self,page):
        if page in self._watched:return
        def popup(child):
            self.owned.append(child);self.register(child);self._watch(child)
            for listener in list(self.listeners):listener(child)
        self._watched[page]=popup;page.on('popup',popup)
    def retarget(self,page):
        """Select another existing page without closing the prior selection."""
        if page is self.selected:return self.selected
        previous=self.selected
        if previous not in self.retained:self.retained.append(previous)
        # A retired original tab no longer owns popups opened later by the user.
        # Owned children keep their watcher even when one becomes selected.
        if previous not in self.owned:self._unwatch(previous)
        self.selected=page;self.register(page);self._watch(page)
        return previous
    def _unwatch(self,page):
        handler=self._watched.pop(page,None)
        if handler is None:return
        try:page.remove_listener('popup',handler)
        except Exception:pass
    @property
    def pages(self):
        pages=[]
        for page in [self.selected,*self.owned]:
            if page not in pages and not page.is_closed():pages.append(page)
        return pages
    def on(self,event,listener):
        if event=='page':self.listeners.append(listener)
    def remove_listener(self,event,listener):
        if event=='page' and listener in self.listeners:self.listeners.remove(listener)
    async def new_page(self):
        page=await self.context.new_page();self.owned.append(page);self.register(page);self._watch(page);return page
    async def close(self):
        for page in list(self.owned):
            try:await page.close()
            except Exception:pass
        for page in list(self._watched):self._unwatch(page)
        # Never close a previously selected original page or its browser/context.
