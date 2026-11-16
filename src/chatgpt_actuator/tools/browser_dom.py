from __future__ import annotations

import asyncio
import base64
import ctypes
from ctypes import wintypes
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import psutil
from uuid import uuid4

from ..audit import AuditLogger
from ..config import BrowserDOMConfig
from .dom_verification import value_result, checked_result


class BrowserDOMService:
    """Semantic browser DOM automation over a persistent Playwright + CDP session."""

    TOKEN_PREFIX = "dom1:"

    def __init__(self, config: BrowserDOMConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit
        self._playwright = None
        self._browser_job = None
        self._browser = None
        self._session_contexts = {}
        self._external_sessions = {}
        self._pending_auto_sessions = {}
        self._connect_lock: asyncio.Lock | None = None
        self._connected_endpoint: str | None = None
        self._stable_tabs: dict[str, object] = {}
        self._pinned_elements = {}
        self._retired_pins = {}
        self._snapshot_frames = {}
        from ..snapshot_cache import SnapshotCache
        self._snapshots = SnapshotCache()
        self._lifecycle_pages = set()
        self._edge_process = None
        self._worker_restore_foreground: int = 0
        self._worker_focus_guard_stop: threading.Event | None = None
        self._worker_focus_guard_thread: threading.Thread | None = None
        self._worker_focus_guard_thread_id: int = 0
        self._worker_focus_guard_pids: set[int] = set()

    async def _connect_over_cdp_with_auto_allow(
        self,
        playwright,
        endpoint: str,
        timeout_ms: int,
    ):
        """Connect to CDP while guarding Worker visibility/focus and consent."""
        watcher = None
        stop_event: threading.Event | None = None
        connected = False

        need_worker_pids = (
            self.config.focus_guard_enabled
            or self.config.visibility_guard_enabled
            or self.config.auto_allow_remote_debugging
        )
        worker_pids = self._worker_edge_pids() if need_worker_pids else []
        previous_foreground = int(
            self._worker_restore_foreground
            or self._foreground_window_handle()
            or 0
        )

        if (
            worker_pids
            and (
                self.config.focus_guard_enabled
                or self.config.visibility_guard_enabled
            )
        ):
            self._start_worker_focus_guard(
                previous_foreground,
                worker_pids,
            )

        if (
            worker_pids
            and self.config.auto_allow_remote_debugging
            and self.config.cdp_url.strip().casefold() == "auto"
            and self.config.launch_if_missing
        ):
            stop_event = threading.Event()
            watch_seconds = max(
                3.0,
                min(20.0, float(timeout_ms) / 1000.0 + 3.0),
            )
            watcher = asyncio.create_task(
                asyncio.to_thread(
                    self._auto_allow_remote_debugging_prompt,
                    worker_pids,
                    watch_seconds,
                    stop_event,
                    previous_foreground,
                )
            )

        try:
            browser = await playwright.chromium.connect_over_cdp(
                endpoint,
                timeout=timeout_ms,
            )
            connected = True
            return browser
        finally:
            if stop_event is not None:
                stop_event.set()
            if watcher is not None:
                try:
                    await asyncio.wait_for(watcher, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    watcher.cancel()
                except Exception:
                    pass
            self._stop_worker_focus_guard()
            if connected and worker_pids:
                self._finalize_worker_window_background_state(worker_pids)

    async def _ensure_browser(self,allow_managed_start=False):
        """Reuse the selected session; managed context creation requires explicit opt-in."""
        self._require_enabled()
        from ..tool_policy import principal_context
        from types import SimpleNamespace
        principal = principal_context.get()
        if principal is None or principal.session == "default":
            raise PermissionError("DOM requires an authenticated Work Session")
        key = (principal.owner, principal.session)
        from ..automation_errors import BrowserSelectionRequired,BrowserConnectionUnavailable
        external=self._external_sessions.get(key)
        if external is not None:
            if not external['browser'].is_connected():raise BrowserConnectionUnavailable('Attached browser disconnected; detach and attach again')
            return SimpleNamespace(contexts=[external['context']],is_connected=external['browser'].is_connected)
        context=self._session_contexts.get(key)
        if context is not None and self._browser is not None and self._browser.is_connected():
            return SimpleNamespace(contexts=[context],is_connected=self._browser.is_connected)
        if not allow_managed_start:
            raise BrowserSelectionRequired('No selected browser for this session; attach the intended tab or explicitly open with browser_mode=managed. No temporary profile was created')
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._browser is None or not self._browser.is_connected():
                await self._disconnect_playwright(include_external=False)
                from playwright.async_api import async_playwright
                playwright = await async_playwright().start()
                job = None
                try:
                    if os.name == 'nt':
                        # Assign the owned driver before it spawns headless Edge.
                        # Child processes inherit memory/CPU/lifetime limits.
                        from ..windows_job import WindowsJob
                        from ..resource_limits import validated
                        driver = playwright._impl_obj._connection._transport._proc
                        if driver is None or driver.returncode is not None:
                            raise RuntimeError('Headless driver is unavailable')
                        job = WindowsJob.from_pid(driver.pid,getattr(self,'resource_limits',validated()))
                    browser = await playwright.chromium.launch(channel="msedge", headless=not self.config.visible_browser,
                        args=['--renderer-process-limit=2','--disable-features=SpareRendererForSitePerProcess'],
                        timeout=self.config.max_timeout_ms)
                except BaseException as exc:
                    # Close the owned process Job first: a failed/cancelled
                    # launch can leave transport.stop waiting for a dead driver.
                    self._observe_intentional_transport_shutdown(playwright)
                    if job is not None: job.close()
                    try:await asyncio.wait_for(playwright.stop(),timeout=3)
                    except (Exception,asyncio.CancelledError):pass
                    if isinstance(exc,asyncio.CancelledError):raise
                    raise RuntimeError("Managed Edge unavailable")
                self._playwright, self._browser = playwright, browser
                self._browser_job = job
                self._connected_endpoint = "visible://managed" if self.config.visible_browser else "headless://isolated"
            context = self._session_contexts.get(key)
            from ..resource_limits import validated
            budget=getattr(self,"resource_limits",validated())
            if context is None:
                if len(self._session_contexts)>=budget['max_browser_contexts']:
                    raise PermissionError('RESOURCE_LIMIT: browser context budget reached')
                context = await self._browser.new_context(accept_downloads=False)
                self._session_contexts[key] = context
                context.on('page',lambda page:self._register_page_resource(page,key))
            return SimpleNamespace(contexts=[context], is_connected=self._browser.is_connected)

    def _register_page_resource(self,page,key):
        sessions=getattr(self,'resource_sessions',None)
        ref=self._tab_ref_for_page(page)
        if sessions is not None:
            sessions.bind(key[1],'dom_tab_ref',ref)
            def release(_page=None):
                with sessions.db() as db:
                    for handle, in db.execute("SELECT id FROM handles WHERE session=? AND kind='dom_tab_ref' AND value=?",(key[1],json.dumps(ref,sort_keys=True))).fetchall():sessions.remember_retired(db,key[1],handle,'tab_closed')
                    db.execute("UPDATE handles SET kind='dom_closed_tab_ref' WHERE session=? AND kind='dom_tab_ref' AND value=?",(key[1],json.dumps(ref,sort_keys=True)))
                    for handle,value in db.execute("SELECT id,value FROM handles WHERE session=? AND kind='dom_element_ref'",(key[1],)).fetchall():
                        try:belongs=self._decode(json.loads(value),'element').get('tab_ref')==ref
                        except (ValueError,TypeError):belongs=False
                        if belongs:
                            sessions.remember_retired(db,key[1],handle,'tab_closed');db.execute('DELETE FROM handles WHERE id=?',(handle,))
            page.on('close',release)

    async def browser_sessions(self,cdp_urls=None):
        self._require_read()
        if not self.config.visible_browser:raise PermissionError('Desktop browser policy disabled')
        from ..dom_attachment import discover_owned
        from ..tool_policy import principal_context
        principal=principal_context.get()
        key=(principal.owner,principal.session) if principal else ('default','default')
        return await discover_owned(['http://127.0.0.1:9222','http://127.0.0.1:9223'] if cdp_urls is None else cdp_urls,
                                    self._external_sessions,key,self._is_allowed_url)

    async def attach_browser(self,cdp_url,tab_url=None,tab_id=None):
        if isinstance(cdp_url,str) and cdp_url.strip().casefold()=='auto':
            raise ValueError('Automatic attachment requires the local browser_auto_connect policy')
        self._require_mutation('allow_navigation','DOM browser attachment')
        if not self.config.visible_browser:raise PermissionError('Desktop browser access is not enabled by owner policy')
        if tab_url is not None:self._require_allowed_url(tab_url)
        if tab_id is not None and (not isinstance(tab_id,str) or not 1<=len(tab_id)<=128):raise ValueError('Bounded tab ID required')
        if tab_url is None and tab_id is None:raise ValueError('Choose a tab ID or unique URL')
        from ..dom_attachment import endpoint,SelectedContext,connect_selected
        from urllib.parse import urlsplit
        from ..tool_policy import principal_context
        principal=principal_context.get()
        if principal is None or principal.session=='default':raise PermissionError('Authenticated Work Session required')
        key=(principal.owner,principal.session);url=endpoint(cdp_url)
        if key in self._session_contexts:raise ValueError('Detach the current DOM session first')
        pending=getattr(self,'_pending_auto_sessions',{})
        if any(urlsplit(item['endpoint']).port==urlsplit(url).port for item in [*self._external_sessions.values(),*pending.values()]):raise PermissionError('This loopback browser port is already leased to a session')
        from ..resource_limits import validated
        if len(self._session_contexts)>=getattr(self,'resource_limits',validated())['max_browser_contexts']:raise PermissionError('RESOURCE_LIMIT: browser context budget reached')
        from playwright.async_api import async_playwright
        driver=await async_playwright().start()
        try:
            browser=await connect_selected(driver,url,self.config.max_timeout_ms)
            pages=[]
            for c in browser.contexts:
                for p in c.pages:
                    if p.is_closed() or not self._is_allowed_url(p.url):continue
                    if tab_url is not None and p.url!=tab_url:continue
                    if tab_id is not None:
                        session=await c.new_cdp_session(p)
                        try:info=await session.send('Target.getTargetInfo')
                        finally:await session.detach()
                        if info['targetInfo']['targetId']!=tab_id:continue
                    pages.append(p)
            if len(pages)!=1:raise ValueError('Select a unique existing tab URL; duplicate or missing tabs refused')
            page=pages[0]
            probe=await page.context.new_cdp_session(page)
            try:selected_target=(await probe.send('Target.getTargetInfo'))['targetInfo']['targetId']
            finally:await probe.detach()
            context=SelectedContext(page.context,page,lambda p:self._register_page_resource(p,key))
            proc=driver._impl_obj._connection._transport._proc
            self._external_sessions[key]={'browser':browser,'driver':driver,'context':context,'endpoint':url,'driver_pid':proc.pid,'driver_started':psutil.Process(proc.pid).create_time(),
                'target_id':selected_target,'attached_at':time.time(),'expected_url':str(page.url)}
            self._session_contexts[key]=context
            return {'attached':True,'tab_ref':self._tab_ref_for_page(page),'url':page.url,'mode':'selected_cdp_tab','existing_tabs_closed_on_release':False,'binding':self._binding(page,key)}
        except BaseException:
            await driver.stop();raise

    async def auto_attach_browser(self, policy, tab_url=None, tab_id=None):
        """Attach to the owner's current Edge profile without exposing an endpoint."""
        self._require_mutation('allow_navigation','DOM browser attachment')
        if not self.config.visible_browser:
            raise PermissionError('Desktop browser access is not enabled by owner policy')
        if tab_url is not None:
            self._require_allowed_url(tab_url)
        if tab_id is not None and (not isinstance(tab_id,str) or not 1<=len(tab_id)<=128):
            raise ValueError('Bounded tab ID required')
        from ..tool_policy import principal_context
        principal=principal_context.get()
        owner_id=getattr(principal,'owner',None)
        session=getattr(principal,'session',None)
        if not isinstance(owner_id,str) or not owner_id or not isinstance(session,str) or session=='default':
            raise PermissionError('Authenticated Work Session required')
        key=(owner_id,session)
        selector=tab_url is not None or tab_id is not None
        if tab_url is not None and tab_id is not None:
            raise ValueError('Choose one tab selector')
        from ..resource_limits import validated
        budget=getattr(self,'resource_limits',None) or validated()
        other_leases=[item for lease_key,item in self._external_sessions.items()
                      if lease_key!=key and item.get('endpoint')]
        pending=self._pending_auto_sessions
        other_leases.extend(item for lease_key,item in pending.items() if lease_key!=key)
        from urllib.parse import urlsplit
        def port_of(item):
            try:return urlsplit(item.get('endpoint','')).port
            except ValueError:return None

        from .. import dom_auto_connect
        def check_other_leases():
            discovered=dom_auto_connect.discover(policy,owner_id)
            discovered_port=port_of(discovered)
            for item in other_leases:
                leased_binding=item.get('binding') or {}
                same_identity=(
                    (discovered.get('pid') is not None and leased_binding.get('pid')==discovered.get('pid'))
                    or (discovered.get('user_data_dir') and discovered.get('profile_directory')
                        and leased_binding.get('user_data_dir')==discovered.get('user_data_dir')
                        and leased_binding.get('profile_directory')==discovered.get('profile_directory'))
                )
                if same_identity or (discovered_port is not None and port_of(item)==discovered_port):
                    raise PermissionError('This Edge profile or CDP port is already leased to another owner/session')
            return discovered

        current=self._external_sessions.get(key)
        waiting=pending.get(key)
        if current is not None and not current.get('auto'):
            raise ValueError('Detach the current DOM session before automatic attachment')
        if current is None and waiting is None and key in self._session_contexts:
            raise ValueError('Detach the current DOM session before automatic attachment')
        if current is None and waiting is None:
            if len(self._session_contexts)+len(pending)>=budget['max_browser_contexts']:
                raise PermissionError('RESOURCE_LIMIT: browser context budget reached')

        if current is not None and current.get('auto'):
            alive=self._auto_transport_alive(current)
            binding=current.get('binding')
            if alive and binding:
                try:
                    refreshed=dom_auto_connect.validate(binding,policy,owner_id)
                    alive=True
                    binding=refreshed
                    current['binding']=refreshed
                except Exception:
                    alive=False
            if alive:
                if not selector:
                    return await self._auto_candidates(current,selected=False)
                return await self._select_auto_page(current,tab_url,tab_id,key)
            # The old CDP transport is stale. Retire its references and stop
            # only Playwright's client before making one fresh connection.
            released=await self.release_session(owner_id,session)
            current=None
            if released.get('resource_cleanup')=='pending':
                waiting=pending.get(key)
        if waiting is not None:
            try:
                if waiting.get('cleanup_only'):
                    if not await self._stop_pending_auto(key,waiting):
                        raise RuntimeError('Pending Playwright driver cleanup remains incomplete')
                    waiting=None
                else:
                    if not self._auto_transport_alive(waiting):
                        raise RuntimeError('Pending Edge CDP transport disconnected')
                    refreshed=dom_auto_connect.validate(waiting['binding'],policy,owner_id)
                    if refreshed.get('endpoint')!=waiting.get('endpoint'):
                        raise PermissionError('Current Edge debugging endpoint changed; pending attachment was retired')
                    waiting['binding']=refreshed
                    waiting['last_activity']=time.monotonic()
            except asyncio.CancelledError:
                await self._stop_pending_auto(key,waiting)
                raise
            except Exception:
                if not await self._stop_pending_auto(key,waiting):
                    waiting['last_activity']=time.monotonic()
                    raise
                waiting=None
        if waiting is None:
            check_other_leases()
            binding=await dom_auto_connect.prepare(policy,owner_id,budget)
            binding_port=port_of(binding)
            if binding_port is not None and any(port_of(item)==binding_port for item in other_leases):
                raise PermissionError('This loopback browser port is already leased to another owner/session')
            from playwright.async_api import async_playwright
            driver=await async_playwright().start()
            waiting={'browser':None,'driver':driver,'endpoint':binding['endpoint'],
                     'binding':binding,'auto':True,'cleanup_only':True,
                     'created_at':time.time(),'last_activity':time.monotonic(),'pending':True}
            pending[key]=waiting
            try:
                browser,consent_handled=await dom_auto_connect.connect(driver,binding,budget)
                waiting.update({'browser':browser,'consent_handled':bool(consent_handled),
                                'cleanup_only':False})
            except BaseException:
                await self._stop_pending_auto(key,waiting)
                raise
        waiting['last_activity']=time.monotonic()
        if not selector:
            try:return await self._auto_candidates(waiting,selected=False)
            except asyncio.CancelledError:
                await self._stop_pending_auto(key,waiting)
                raise
        try:
            result=await self._select_auto_page(waiting,tab_url,tab_id,key)
            waiting['pending']=False
            pending.pop(key,None)
            self._external_sessions[key]=waiting
            self._session_contexts[key]=waiting['context']
            return result
        except asyncio.CancelledError:
            await self._stop_pending_auto(key,waiting)
            raise
        except BaseException:
            # Selection errors do not throw away the consented connection.
            raise

    async def _stop_pending_auto(self,key,lease):
        """Stop only the Playwright client and retain failed cleanup for retry."""
        lease['cleanup_only']=True
        lease['last_activity']=time.monotonic()
        try:
            await asyncio.wait_for(lease['driver'].stop(),timeout=3)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._pending_auto_sessions[key]=lease
            return False
        if self._pending_auto_sessions.get(key) is lease:
            self._pending_auto_sessions.pop(key,None)
        return True

    @staticmethod
    def _auto_transport_alive(lease):
        """Check both Playwright's browser flag and its underlying driver pipe."""
        browser=lease.get('browser')
        try:
            if browser is None or not browser.is_connected():return False
        except Exception:return False
        impl=getattr(browser,'_impl_obj',None)
        connection=getattr(impl,'_connection',None)
        if connection is None:return True
        if getattr(connection,'_closed_error',None) is not None:return False
        transport=getattr(connection,'_transport',None)
        if transport is None:return True
        if getattr(transport,'_stopped',False):return False
        stopped=getattr(transport,'_stopped_future',None)
        try:
            if stopped is not None and stopped.done():return False
        except Exception:return False
        process=getattr(transport,'_proc',None)
        if process is not None and getattr(process,'returncode',None) is not None:return False
        return True

    async def expire_pending_auto_connections(self, idle_seconds=60.0):
        """Release only idle, unselected Playwright transports; preserve Edge."""
        now=time.monotonic()
        expired=[]
        for key,lease in list(self._pending_auto_sessions.items()):
            if now-float(lease.get('last_activity',now))<max(0.0,float(idle_seconds)):
                continue
            lease['cleanup_only']=True
            try:
                await asyncio.wait_for(lease['driver'].stop(),timeout=3)
                state='completed'
                self._pending_auto_sessions.pop(key,None)
            except Exception:
                state='pending'
            expired.append({'owner':key[0],'session':key[1],'resource_cleanup':state,
                            'browser_closed':False,'selected_tabs_closed':False})
        return {'expired':expired}

    async def _auto_candidates(self, lease, selected=False):
        candidates=[]
        try:
            for context in lease['browser'].contexts[:8]:
                for page in context.pages[:32]:
                    if page.is_closed() or not self._is_allowed_url(str(page.url)):continue
                    observed_url=str(page.url)
                    try:
                        session=await context.new_cdp_session(page)
                        try:info=await session.send('Target.getTargetInfo')
                        finally:await session.detach()
                        target=info['targetInfo']['targetId']
                        title=str(await page.title())[:512]
                    except Exception:continue
                    if str(page.url)!=observed_url or not self._is_allowed_url(str(page.url)):continue
                    candidates.append({'tab_id':target,'url':observed_url[:4096],'title':title})
        except Exception:
            pass
        return {'attached':False,'selection_required':True,'candidates':candidates[:64],
                'consent_handled':bool(lease.get('consent_handled')),
                'existing_tabs_closed_on_release':False}

    async def _select_auto_page(self, lease, tab_url, tab_id, key):
        from ..dom_attachment import SelectedContext
        pages=[]
        for context in lease['browser'].contexts:
            for page in context.pages:
                if page.is_closed() or not self._is_allowed_url(str(page.url)):continue
                if tab_url is not None and str(page.url)!=tab_url:continue
                if tab_id is not None:
                    probe=await context.new_cdp_session(page)
                    try:target=(await probe.send('Target.getTargetInfo'))['targetInfo']['targetId']
                    finally:await probe.detach()
                    if target!=tab_id:continue
                pages.append(page)
        if len(pages)!=1:
            raise ValueError('Select exactly one allowed existing browser tab')
        page=pages[0]
        probe=await page.context.new_cdp_session(page)
        try:target=(await probe.send('Target.getTargetInfo'))['targetInfo']['targetId']
        finally:await probe.detach()
        context=lease.get('context')
        if context is None:
            context=SelectedContext(page.context,page,lambda p:self._register_page_resource(p,key))
        elif context.selected is not page:
            previous=context.retarget(page)
            await self._retire_auto_selected_refs(previous,key)
        lease.update({'context':context,'target_id':target,'attached_at':time.time(),
                      'expected_url':str(page.url),'pending':False})
        return {'attached':True,'selection_required':False,'tab_ref':self._tab_ref_for_page(page),
                'url':page.url,'mode':'selected_cdp_tab','existing_tabs_closed_on_release':False,
                'consent_handled':bool(lease.get('consent_handled')),
                'binding':{'mode':'selected_cdp_tab','ownership':'selected_existing_tab',
                           'target_id':target,'profile_verified':True,
                           'profile_persistence':'current_edge_profile'}}

    async def _retire_auto_selected_refs(self,page,key):
        """Invalidate refs for a former selected page while leaving the page open."""
        stable_ids=[ident for ident,candidate in self._stable_tabs.items() if candidate is page]
        refs=[self._encode({'type':'tab','id':ident}) for ident in stable_ids]
        for ref in refs:
            await self._prune_pinned_elements(tab_ref=ref)
            self._snapshots.forget_target(ref)
            for pin,(tab,_) in list(self._snapshot_frames.items()):
                if tab==ref:self._snapshot_frames.pop(pin,None)
        for ident in stable_ids:self._stable_tabs.pop(ident,None)
        sessions=getattr(self,'resource_sessions',None)
        if not sessions or not refs:return
        ref_set=set(refs)
        with sessions.db() as db:
            for handle,value in db.execute(
                "SELECT id,value FROM handles WHERE session=? AND kind='dom_tab_ref'",(key[1],)
            ).fetchall():
                try:matches=json.loads(value) in ref_set
                except (ValueError,TypeError):matches=False
                if matches:
                    sessions.remember_retired(db,key[1],handle,'lease_expired')
                    db.execute("UPDATE handles SET kind='dom_closed_tab_ref' WHERE id=?",(handle,))
            for handle,value in db.execute(
                "SELECT id,value FROM handles WHERE session=? AND kind='dom_element_ref'",(key[1],)
            ).fetchall():
                try:tab_ref=self._decode(json.loads(value),'element').get('tab_ref')
                except (ValueError,TypeError):tab_ref=None
                if tab_ref in ref_set:
                    sessions.remember_retired(db,key[1],handle,'lease_expired')
                    db.execute("DELETE FROM handles WHERE id=?",(handle,))

    async def recover_browser(self,cdp_url,tab_id,expected_url):
        # Explicit reattachment only. No action replay or URL navigation.
        from ..tool_policy import principal_context
        p=principal_context.get()
        if p is None or p.session=='default':raise PermissionError('Authenticated Work Session required')
        key=(p.owner,p.session)
        if key in self._session_contexts:raise ValueError('Detach the old browser session first; recovery never closes its tabs automatically')
        result=await self.attach_browser(cdp_url,expected_url,tab_id)
        return {**result,'recovered':True,'actions_replayed':0,'navigation_performed':False,'references':'fresh'}

    async def resource_inventory(self,owner,session):
        context=self._session_contexts.get((owner,session))
        pages=[p for p in context.pages if not p.is_closed()] if context else []
        tabs=[self._tab_ref_for_page(p) for p in pages]
        await self._prune_pinned_elements()
        return {'tab_refs':tabs,'element_pins':[pin for pin,(tab,_,_) in self._pinned_elements.items() if tab in tabs],'state':'observed','browser_started':False}

    def recover_owned_transport(self,owner,session):
        """Terminate only an identified owned driver/job; never the selected user's browser."""
        key=(owner,session);external=self._external_sessions.get(key)
        if external is None and any(k!=key and k not in self._external_sessions for k in self._session_contexts):
            raise PermissionError('Shared managed browser contains other sessions; restart requires owner review')
        context=self._session_contexts.get(key)
        pages=list(context.pages) if context else []
        if external:
            pid,started=external.get('driver_pid'),external.get('driver_started')
            if not pid or started is None:raise ValueError('Driver identity not available')
            process=psutil.Process(pid)
            if process.create_time()!=started or process.name().casefold()!='node.exe':raise PermissionError('Driver identity changed')
            self._observe_intentional_transport_shutdown(external['driver'])
            process.terminate()  # PID-only: the user's Chromium process is independent.
            self._external_sessions.pop(key,None)
        else:
            job=self._browser_job
            if job is None:raise ValueError('No owned managed browser job available')
            self._observe_intentional_transport_shutdown(self._playwright)
            job.close();self._browser_job=None
            self._browser=self._playwright=None;self._connect_lock=None;self._connected_endpoint=None
        self._session_contexts.pop(key,None)
        refs={self._encode({'type':'tab','id':ident}) for ident,page in self._stable_tabs.items() if page in pages}
        self._stable_tabs={ident:page for ident,page in self._stable_tabs.items() if page not in pages}
        for ref in refs:self._snapshots.forget_target(ref)
        self._pinned_elements={pin:entry for pin,entry in self._pinned_elements.items() if entry[0] not in refs}
        self._snapshot_frames={pin:entry for pin,entry in self._snapshot_frames.items() if entry[0] not in refs}
        sessions=getattr(self,'resource_sessions',None)
        if sessions:
            with sessions.db() as db:
                for ref, in db.execute("SELECT id FROM handles WHERE session=? AND kind LIKE 'dom_%'",(session,)).fetchall():sessions.remember_retired(db,session,ref,'lease_expired')
                db.execute("DELETE FROM handles WHERE session=? AND kind LIKE 'dom_%'",(session,))
        return {'transport_terminated':True,'user_browser_preserved':True,'actions_replayed':0,'references':'retired'}

    def _observe_intentional_transport_shutdown(self,driver):
        # Consume only Playwright's owned transport shutdown diagnostic.
        # Pending action Futures retain their errors and uncertainty.
        transport=getattr(getattr(getattr(driver,'_impl_obj',None),'_connection',None),'_transport',None)
        future=getattr(transport,'on_error_future',None)
        if future is None:return
        def observed(done):
            if done.cancelled():return
            error=done.exception()
            if error is not None:self.audit.record('dom_owned_transport_shutdown',error_type=type(error).__name__)
        future.add_done_callback(observed)

    def _on_browser_disconnected(self, _browser=None) -> None:
        self._browser = None
        self._connected_endpoint = None

    async def _disconnect_playwright(self,include_external=True) -> None:
        for owner,session in list(self._session_contexts):
            if include_external or (owner,session) not in self._external_sessions:await self.release_session(owner,session)
        if include_external:
            for owner,session in list(self._pending_auto_sessions):
                await self.release_session(owner,session)
        if not self._external_sessions:await self._prune_pinned_elements(all_nodes=True)
        browser, playwright = self._browser, self._playwright
        self._browser = self._playwright = None
        self._session_contexts = {key:value for key,value in self._session_contexts.items() if key in self._external_sessions}
        if not self._external_sessions:
            self._stable_tabs.clear()
            self._snapshot_frames.clear()
            self._snapshots.entries.clear()
            self._lifecycle_pages.clear()
        self._connected_endpoint = None
        if browser is not None:
            try: await asyncio.wait_for(browser.close(),timeout=3)
            except Exception: pass
        if playwright is not None:
            try: await asyncio.wait_for(playwright.stop(),timeout=3)
            except Exception: pass
        job, self._browser_job = self._browser_job, None
        if job is not None: job.close()

    async def disconnect(self) -> None:
        """Explicitly disconnect the automation client without closing Edge."""
        self._stop_worker_focus_guard()
        if self._connect_lock is None:
            await self._disconnect_playwright()
            return
        async with self._connect_lock:
            await self._disconnect_playwright()
        self.audit.record("dom_browser_disconnected")

    def _binding(self,page,key):
        external=self._external_sessions.get(key)
        if external is None:
            return {'mode':'managed_ephemeral','profile_verified':False,'profile_persistence':'ephemeral'}
        selected=page is external['context'].selected
        if external.get('auto') and external.get('binding'):
            return {'mode':'selected_cdp_tab',
                    'ownership':'selected_existing_tab' if selected else 'owned_child_tab',
                    'target_id':external.get('target_id') if selected else None,
                    'attached_at':external.get('attached_at'),
                    'profile_verified':True,
                    'profile_persistence':'current_edge_profile',
                    'url_changed_since_attach':str(page.url)!=external.get('expected_url') if selected else None}
        return {'mode':'selected_cdp_tab','ownership':'selected_existing_tab' if selected else 'owned_child_tab',
                'target_id':external.get('target_id') if selected else None,
                'attached_at':external.get('attached_at'),'profile_verified':False,
                'profile_persistence':'external_browser_managed',
                'url_changed_since_attach':str(page.url)!=external.get('expected_url') if selected else None,
                'profile_note':'Browser profile is not inferred from configured worker paths or page account labels'}

    async def page_diagnostics(self,tab_ref):
        self._require_read()
        browser=await self._ensure_browser()
        page=await self._resolve_tab(browser,tab_ref)
        from ..dom_diagnostics import passive
        return await passive(page,self._require_allowed_url)

    async def cart_coverage(self,tab_ref,selectors):
        self._require_read()
        from ..cart_coverage import validate,inspect
        validate(selectors)
        browser=await self._ensure_browser()
        page=await self._resolve_tab(browser,tab_ref)
        start=str(page.url)
        result=await inspect(page,selectors)
        self._require_allowed_url(str(page.url))
        if str(page.url)!=start:raise ValueError('Cart target changed during observation; read again before recording coverage')
        return result

    async def list_tabs(self) -> dict:
        self._require_read()
        from ..tool_policy import principal_context
        principal=principal_context.get()
        key=(principal.owner,principal.session) if principal else ('default','default')
        if key not in self._session_contexts:
            return {'backend':'dom','connection_mode':'not_selected','tabs':[],'count':0,
                    'browser_started':False,'next_action':'Attach the intended existing tab or explicitly open with browser_mode=managed'}
        browser = await self._ensure_browser()
        pages = self._pages(browser)
        tabs = []
        for page in pages:
            url = str(page.url or "")
            title = await self._safe_title(page)
            if not self._is_allowed_url(url):
                continue
            tabs.append(
                {
                    "tab_ref": self._tab_ref_for_page(page),
                    "title": title,
                    "url": url,
                    "binding": self._binding(page,key),
                }
            )

        self.audit.record("dom_list_tabs", returned=len(tabs))
        from ..tool_policy import principal_context
        principal=principal_context.get()
        key=(principal.owner,principal.session) if principal else ('default','default')
        selected=key in self._external_sessions
        return {
            "backend": "dom",
            "connection_mode": "selected_cdp_tab" if selected else "managed_ephemeral",
            "cdp_url": self.config.cdp_url,
            "resolved_cdp_url": "selected://local-cdp" if selected else self._connected_endpoint or "disconnected",
            "user_data_dir": None,
            "configured_user_data_dir": self.config.user_data_dir,
            "actual_profile_known": False,
            "profile_persistence": "external_browser_managed" if selected else "ephemeral",
            "launch_if_missing": self.config.launch_if_missing,
            "background_mode": "visible" if self.config.visible_browser else "headless",
            "tabs": tabs,
            "count": len(tabs),
        }

    async def guard_mutation(self, method):
        """Pause this principal's session on blockers, without navigation or replay."""
        if method not in {'navigate','click','set_value','select','toggle','scroll','scroll_into_view','history'}:return
        from ..tool_policy import principal_context
        from ..dom_content_state import classify
        from ..automation_errors import DOMHumanActionRequired
        principal=principal_context.get()
        key=(principal.owner,principal.session) if principal else ('default','default')
        context=self._session_contexts.get(key)
        if context is None:return
        for page in context.pages:
            if page.is_closed():continue
            state=classify('', '', page.url)
            if state['human_action_required']:raise DOMHumanActionRequired(state['state'])
            try:
                body=await page.locator('xpath=/html/body').inner_text(timeout=2000)
            except Exception:
                raise DOMHumanActionRequired('observation_unavailable') from None
            state=classify('',body[:2000],page.url)
            if state['human_action_required']:raise DOMHumanActionRequired(state['state'])

    async def open_tab(
        self,
        url: str,
        timeout_ms: int | None = None,
        browser_mode: str = 'current',
    ) -> dict:
        self._require_mutation("allow_navigation", "DOM navigation")
        self._require_allowed_url(url)
        timeout = self._timeout(timeout_ms)
        if browser_mode not in {'current','managed'}:raise ValueError('browser_mode must be current or managed')
        if browser_mode=='managed':
            from ..tool_policy import principal_context
            p=principal_context.get()
            if p and (p.owner,p.session) in self._external_sessions:
                raise ValueError('Selected external browser is active; detach explicitly before choosing a managed browser')
        browser = await self._ensure_browser(allow_managed_start=browser_mode=='managed')
        if not browser.contexts:
            raise RuntimeError("Connected Edge browser has no available browser context.")

        from ..resource_limits import validated
        if len(browser.contexts[0].pages)>=getattr(self,'resource_limits',validated())['max_tabs_per_session']:
            raise PermissionError('RESOURCE_LIMIT: tab budget reached')
        page = await browser.contexts[0].new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            self._require_allowed_url(str(page.url or ""))
        except Exception:
            try:
                await asyncio.wait_for(page.close(),timeout=3)
            except Exception:
                pass
            raise

        tab_ref = self._tab_ref_for_page(page)
        title = await self._safe_title(page)
        self.audit.record("dom_open_tab", tab_ref=tab_ref, url=str(page.url or ""))
        return {
            "tab_ref": tab_ref,
            "title": title,
            "url": str(page.url or ""),
            "opened": True,
        }

    async def navigate(
        self,
        tab_ref: str,
        url: str,
        timeout_ms: int | None = None,
        verification: dict | None = None,
    ) -> dict:
        self._require_mutation("allow_navigation", "DOM navigation")
        self._require_allowed_url(url)
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        page = await self._resolve_tab(browser, tab_ref)
        from .dom_verification import validate_click,click_result
        await validate_click(page,None,verification)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        self._require_allowed_url(str(page.url or ""))
        stable_ref = self._tab_ref_for_page(page)
        title = await self._safe_title(page)
        self.audit.record(
            "dom_navigate",
            tab_ref=stable_ref,
            url=str(page.url or ""),
        )
        return {
            "tab_ref": stable_ref,
            "title": title,
            "url": str(page.url or ""),
            "navigated": True,
            "verification":await click_result(page,None,None,'',allowed=self._is_allowed_url,verification=verification or {'url_equals':str(page.url),'wait_for_match':True}),
        }

    async def close_tab(self, tab_ref: str) -> dict:
        from ..automation_errors import ReferenceUnavailable
        self._require_mutation("allow_navigation", "DOM tab close")
        browser = await self._ensure_browser()
        try:page = await self._resolve_tab(browser, tab_ref,allow_blocked_url=True)
        except (LookupError,ReferenceUnavailable):
            await self._prune_pinned_elements(tab_ref=tab_ref)
            return {'tab_ref':tab_ref,'closed':True,'already_closed':True}
        stable_id = None
        try:
            token = self._decode(tab_ref, expected_type="tab")
            stable_id = token.get("id")
        except Exception:
            pass
        await asyncio.wait_for(page.close(),timeout=3)
        await self._prune_pinned_elements(tab_ref=tab_ref)
        if stable_id is not None:
            self._stable_tabs.pop(str(stable_id), None)
        else:
            for key, candidate in list(self._stable_tabs.items()):
                if candidate is page:
                    self._stable_tabs.pop(key, None)
        if not browser.contexts[0].pages:
            from ..tool_policy import principal_context
            principal=principal_context.get()
            await self.release_session(principal.owner,principal.session)
        self.audit.record("dom_close_tab", tab_ref=tab_ref)
        return {"tab_ref": tab_ref, "closed": True}

    async def peek_tabs(self) -> dict:
        self._require_read()
        from ..tool_policy import principal_context
        principal = principal_context.get()
        contexts = getattr(self, "_session_contexts", {})
        context = contexts.get((principal.owner, principal.session)) if principal else None
        tabs = []
        for page in context.pages if context else []:
            if self._is_allowed_url(str(page.url or "")):
                tabs.append({"tab_ref": self._tab_ref_for_page(page),
                             "url": page.url, "title": await self._safe_title(page)})
        return {"backend": "dom", "background_only": not self.config.visible_browser, "tabs": tabs,
                "count": len(tabs), "connected": context is not None}

    async def ensure_tab_pool(
        self,
        url: str,
        count: int = 5,
        timeout_ms: int | None = None,
    ) -> dict:
        """Ensure N allowed tabs exist, creating missing targets in the background."""
        self._require_mutation("allow_navigation", "DOM navigation")
        self._require_allowed_url(url)
        desired = min(max(1, int(count)), 20)
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        target_host = (urlparse(url).hostname or "").casefold()

        def matching_pages() -> list:
            out = []
            for page in self._pages(browser):
                page_url = str(page.url or "")
                host = (urlparse(page_url).hostname or "").casefold()
                if host == target_host or host.endswith("." + target_host):
                    out.append(page)
            return out

        pages = matching_pages()
        missing = max(0, desired - len(pages))
        created = 0
        if missing:
            session = await browser.new_browser_cdp_session()
            try:
                for _ in range(missing):
                    await session.send(
                        "Target.createTarget",
                        {"url": url, "background": True},
                    )
                    created += 1
            finally:
                try:
                    await session.detach()
                except Exception:
                    pass

            deadline = time.monotonic() + max(1.0, timeout / 1000.0)
            while time.monotonic() < deadline:
                pages = matching_pages()
                if len(pages) >= desired:
                    break
                await asyncio.sleep(0.1)
            pages = matching_pages()
            if len(pages) < desired:
                raise TimeoutError(
                    f"Background tab-pool refill created {created} target(s), "
                    f"but only {len(pages)}/{desired} ChatGPT tabs became available."
                )

        # Once a real worker tab exists, remove launch-only blank tabs. This is
        # limited to the dedicated Worker Edge connection.
        if pages:
            for page in list(self._pages(browser)):
                try:
                    if str(page.url or "") == "about:blank":
                        await page.close()
                except Exception:
                    pass

        tabs = [
            {
                "tab_ref": self._tab_ref_for_page(page),
                "title": await self._safe_title(page),
                "url": str(page.url or ""),
            }
            for page in matching_pages()
        ]
        self.audit.record(
            "dom_tab_pool_ensured",
            desired=desired,
            created=created,
            available=len(tabs),
            background=True,
        )
        return {
            "desired": desired,
            "created": created,
            "available": len(tabs),
            "tabs": tabs,
            "background": True,
        }

    async def shutdown_worker_edge(self, timeout_ms: int | None = None) -> dict:
        """Close only the dedicated Worker Edge profile, never the main Edge."""
        self._require_enabled()
        self._stop_worker_focus_guard()
        if self.config.cdp_url.strip().casefold() != "auto":
            raise RuntimeError(
                "Automatic Worker Edge shutdown requires browser_dom.cdp_url='auto'."
            )
        self._dedicated_user_data_dir()
        timeout = max(500, min(int(timeout_ms or 6000), 15000))

        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()

        async with self._connect_lock:
            pids_before = self._worker_edge_pids()
            graceful = False
            browser = self._browser
            if browser is not None:
                try:
                    if browser.is_connected():
                        session = await browser.new_browser_cdp_session()
                        try:
                            await session.send("Browser.close")
                            graceful = True
                        finally:
                            try:
                                await session.detach()
                            except Exception:
                                pass
                except Exception:
                    graceful = False

            await self._disconnect_playwright()
            self._stable_tabs.clear()
            stopped = await asyncio.to_thread(
                self._stop_worker_edge_processes,
                timeout / 1000.0,
            )
            self._edge_process = None

            active_port = self._dedicated_user_data_dir() / "DevToolsActivePort"
            try:
                if active_port.is_file():
                    active_port.unlink()
            except OSError:
                pass

        self.audit.record(
            "dom_worker_edge_stopped",
            pids_before=pids_before,
            stopped_pids=stopped,
            graceful=graceful,
        )
        return {
            "stopped": True,
            "graceful": graceful,
            "pids_before": pids_before,
            "stopped_pids": stopped,
            "remaining_pids": self._worker_edge_pids(),
        }

    async def find(
        self,
        tab_ref: str,
        *,
        selector: str | None = None,
        text: str | None = None,
        role: str | None = None,
        name: str | None = None,
        exact: bool = False,
        max_items: int = 20,
        frame_ref: str | None = None,
    ) -> dict:
        self._require_read()
        strategy_count = sum(bool(x) for x in (selector, text, role))
        if strategy_count != 1:
            raise ValueError("Use exactly one DOM strategy: selector, text, or role.")
        if name and not role:
            raise ValueError("name is only valid together with role.")

        limit = min(max(1, int(max_items)), self.config.max_items)
        browser = await self._ensure_browser()
        page = await self._resolve_tab(browser, tab_ref)
        target = self._resolve_snapshot_frame(page, tab_ref, frame_ref) if frame_ref else page
        base, descriptor = self._base_locator(
            target,
            selector=selector,
            text=text,
            role=role,
            name=name,
            exact=exact,
        )
        count = int(await base.count())
        returned = min(count, limit)
        matches = []
        for index in range(returned):
            locator = base.nth(index)
            try:
                await self._prune_pinned_elements()
                if len(self._pinned_elements) >= 1000:
                    raise PermissionError('RESOURCE_LIMIT: pinned DOM reference budget reached')
                handle = await locator.element_handle(timeout=self.config.default_timeout_ms)
                if handle is None:
                    raise LookupError('DOM node disappeared during find')
                from .pinned_dom import PinnedNode
                meta = await self._element_metadata(PinnedNode(handle))
                owner_frame = await handle.owner_frame()
                if owner_frame is None: raise LookupError('DOM frame detached during find')
                self._require_allowed_url(str(owner_frame.url or ''))
                pin = uuid4().hex
                self._pinned_elements[pin] = (tab_ref, handle, time.monotonic()+300)
                element_ref = self._encode({'type':'element', 'tab_ref':tab_ref, 'pin':pin})
                meta["element_ref"] = element_ref
                matches.append(meta)
            except Exception as exc:
                matches.append(
                    {
                        "error": f"Element changed while being inspected: {exc}",
                    }
                )

        self.audit.record(
            "dom_find",
            selector=selector,
            text=text,
            role=role,
            name=name,
            exact=bool(exact),
            returned=len(matches),
            total=count,
        )
        return {
            "tab_ref": tab_ref,
            "matches": matches,
            "count": count,
            "returned": len(matches),
            "truncated": count > len(matches),
        }

    async def snapshot(self, tab_ref: str, max_items: int = 100,
                       max_depth: int = 12, include_hidden: bool = False,
                       include_frames: bool = False, include_shadow: bool = False, root_selector: str | None = None) -> dict:
        """Inspect the owned main document; obtain action refs separately via find()."""
        self._require_read()
        if max_items < 1 or max_depth < 0:
            raise ValueError("max_items must be positive and max_depth nonnegative")
        if root_selector is not None:
            if not isinstance(root_selector,str) or not root_selector or len(root_selector)>512:
                raise ValueError('root_selector must contain 1..512 characters')
            if include_frames:raise ValueError('A scoped root cannot include child frames')
        limit = min(int(max_items), self.config.max_items)
        depth = min(int(max_depth), 32)
        browser = await self._ensure_browser()
        page = await self._resolve_tab(browser, tab_ref)
        if root_selector is not None:
            exists=await page.evaluate("selector=>{try{return !!document.querySelector(selector)}catch{return null}}",root_selector)
            if exists is None:raise ValueError('Invalid root CSS selector')
            if not exists:raise LookupError('Snapshot root is unavailable')
        from .semantic_snapshot import DOM_SNAPSHOT_SCRIPT
        result = await page.evaluate(DOM_SNAPSHOT_SCRIPT, {
            "limit": limit, "maxDepth": depth,
            "textLimit": min(self.config.max_text_chars, 32768),
            "includeHidden": bool(include_hidden),
            "includeShadow": bool(include_shadow),
            "rootSelector": root_selector,
        })
        # Recheck after inspection if a navigation raced with this request.
        self._require_allowed_url(str(page.url or ""))
        for node in result['nodes']: node['frame_index'] = 0
        documents = [{'frame_index':0,'frame_ref':None}]
        if include_frames:
            for ordinal, frame in enumerate(page.frames[1:17], 1):
                remaining = limit-result['scanned']
                if remaining <= 0:
                    result['truncated'] = True
                    result['truncation_reasons'].append('scan_limit')
                    break
                if not self._is_allowed_url(str(frame.url or '')):
                    documents.append({'frame_index':ordinal,'state':'blocked_host'})
                    continue
                try:
                    part = await frame.evaluate(DOM_SNAPSHOT_SCRIPT, {
                        'limit':remaining,'maxDepth':depth,
                        'textLimit':max(0,min(self.config.max_text_chars,32768)-result['text_chars']),
                        'includeHidden':bool(include_hidden),'includeShadow':bool(include_shadow)})
                    self._require_allowed_url(str(frame.url or ''))
                except Exception:
                    documents.append({'frame_index':ordinal,'state':'changed_or_unavailable'})
                    continue
                frame_ref = self._snapshot_frame_ref(tab_ref, frame)
                documents.append({'frame_index':ordinal,'frame_ref':frame_ref,'state':'read'})
                offset = len(result['nodes'])
                for node in part['nodes']:
                    node['index'] += offset
                    if node['parent_index'] is not None: node['parent_index'] += offset
                    node['frame_index'] = ordinal
                result['nodes'].extend(part['nodes'])
                for field in ('scanned','returned','text_chars','omitted_open_shadow_roots'):
                    result[field] += part[field]
                result['truncated'] |= part['truncated']
                result['truncation_reasons'].extend(part['truncation_reasons'])
            if len(page.frames)>17:
                result['truncated']=True
                result['truncation_reasons'].append('frame_limit')
        result['documents'] = documents
        if include_frames:
            result['omitted_frames'] = max(0, len(page.frames)-1-sum(d.get('state')=='read' for d in documents))
        result['scope'] = 'allowed_frames' if include_frames else 'main_document'
        result['include_open_shadow'] = bool(include_shadow)
        result['truncation_reasons'] = sorted(set(result['truncation_reasons']))
        result.update(backend="headless_dom", tab_ref=tab_ref,
                      values_included=False,
                      limits={"max_scanned": limit, "max_depth": depth},
                      action_reference_hint="Use dom_find with role/name or CSS for current element_refs.")
        self.audit.record("dom_snapshot", returned=result["returned"],
                          scanned=result["scanned"], truncated=result["truncated"])
        from ..snapshot_cache import owner_scope
        result['capture_options']={'include_hidden':bool(include_hidden),'include_frames':bool(include_frames),'include_shadow':bool(include_shadow),'root_selector':root_selector}
        result['snapshot_ref']=self._snapshots.store(owner_scope(tab_ref),result)
        return result

    def _snapshot_frame_ref(self, tab_ref, frame):
        for key, (tab, candidate) in list(self._snapshot_frames.items()):
            if candidate.is_detached(): self._snapshot_frames.pop(key, None)
            elif tab == tab_ref and candidate is frame:
                return self._encode({'type':'frame','tab_ref':tab_ref,'id':key})
        if len(self._snapshot_frames)>=512:
            raise PermissionError('RESOURCE_LIMIT: frame reference budget reached')
        key = uuid4().hex
        self._snapshot_frames[key] = (tab_ref, frame)
        return self._encode({'type':'frame','tab_ref':tab_ref,'id':key})

    async def accessibility_snapshot(self, tab_ref: str, max_items: int = 100,
                                     max_depth: int = 12, include_frames: bool = False) -> dict:
        self._require_read()
        if max_items < 1 or max_depth < 0:
            raise ValueError('Invalid accessibility capture limits')
        browser = await self._ensure_browser()
        page = await self._resolve_tab(browser, tab_ref)
        from .browser_accessibility import bounded_capture
        result = await bounded_capture(self, page, min(int(max_items),self.config.max_items),
                                       min(int(max_depth),32), bool(include_frames))
        result['tab_ref'] = tab_ref
        self.audit.record('dom_accessibility_snapshot', returned=result['returned'],
                          scanned=result['scanned'],truncated=result['truncated'])
        return result

    def _resolve_snapshot_frame(self, page, tab_ref, frame_ref):
        token = self._decode(frame_ref, 'frame')
        entry = self._snapshot_frames.get(token.get('id'))
        if not entry or token.get('tab_ref') != tab_ref or entry[0] != tab_ref:
            raise PermissionError('Frame belongs to another tab or session')
        frame = entry[1]
        if frame.is_detached() or frame not in page.frames:
            raise LookupError('STALE_DOM_REF: frame detached')
        self._require_allowed_url(str(frame.url or ''))
        return frame

    async def details(self, element_ref: str) -> dict:
        self._require_read()
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        result = await self._element_metadata(locator, include_attributes=True)
        result["element_ref"] = element_ref
        result["tab_ref"] = self._tab_ref_for_page(_page)

        self.audit.record("dom_details", element_ref=element_ref)
        return result

    async def read_text(self, element_ref: str, max_chars: int | None = None) -> dict:
        self._require_read()
        limit = self.config.max_text_chars if max_chars is None else min(
            max(1, int(max_chars)), self.config.max_text_chars
        )
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        text = await locator.evaluate(
            "(el) => (el.innerText !== undefined ? el.innerText : (el.textContent || ''))"
        )
        text = str(text or "")
        result = {
            "element_ref": element_ref,
            "text": text[:limit],
            "truncated": len(text) > limit,
        }
        self.audit.record(
            "dom_read_text",
            element_ref=element_ref,
            returned_chars=len(result["text"]),
            truncated=result["truncated"],
        )
        return result

    async def click(self, element_ref: str, timeout_ms: int | None = None, expected_selector: str | None = None, verification: dict | None = None, precondition: dict | None = None, popup_timeout_ms: int = 0, cart_identity=None) -> dict:
        if type(popup_timeout_ms) is not int or not 0<=popup_timeout_ms<=3000:
            from .dom_verification import DOMValidationError
            raise DOMValidationError('popup_timeout_ms requires 0..3000')
        self._require_mutation("allow_click", "DOM click")
        from .dom_preconditions import validate,check
        validate(precondition)
        from .dom_verification import options
        options(verification)
        if expected_selector is not None and verification and verification.get('selector'):raise ValueError('Provide one selector condition')
        if expected_selector is not None and (not isinstance(expected_selector,str) or not expected_selector or len(expected_selector)>512):
            raise ValueError('expected_selector must contain 1..512 characters')
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        from urllib.parse import urlsplit
        if urlsplit(_page.url).hostname in {'shopee.vn','www.shopee.vn','m.shopee.vn'}:
            label=await locator.evaluate("el => {const b=el.closest('button')||el;return (b.innerText||'')+' '+(b.getAttribute('aria-label')||'')}")
            from ..cart_checkpoint import check_action
            if check_action(_page.url,label,cart_identity):
                from ..automation_errors import CartCheckpointRequired
                quantity=_page.locator("input[role='spinbutton'], input.shopee-input-quantity__input")
                if await quantity.count()!=1:raise CartCheckpointRequired('Cannot uniquely observe product quantity')
                if await quantity.input_value()!=str(cart_identity.delta):raise CartCheckpointRequired('Product quantity differs from reserved delta')
        from .dom_verification import validate_click
        condition_scope = await locator.owner_frame()
        if condition_scope is None:raise LookupError('Verification frame detached')
        self._require_allowed_url(str(condition_scope.url or ''))
        await validate_click(condition_scope, expected_selector, verification)
        await check(locator,precondition)
        await self._check_modal_access(locator)
        action_context=browser.contexts[0]
        previous_pages=set(action_context.pages)
        from .dom_verification import state,click_result
        before=await state(locator)
        before_url=_page.url
        from .popup_tracker import PopupTracker
        tracker=PopupTracker(action_context)
        try:
            if precondition is not None:
                # A trusted click can auto-wait after the first condition check.
                # Trial checks actionability without dispatch; the final condition
                # and semantic click share one JS task on the same pinned node.
                if not self.config.background_safe_events:await locator.click(trial=True,timeout=timeout)
                from .dom_guarded_click import dispatch
                await dispatch(locator,precondition)
                click_mode='guarded_dom_event'
            elif self.config.background_safe_events:
                await locator.wait_for(state="attached", timeout=timeout)
                await locator.evaluate(
                    """(el) => {
                        if (el.disabled) throw new Error('Element is disabled.');
                        const modal=el.ownerDocument.querySelector('dialog:modal');
                    let inside=!modal;
                    for(let n=el;n;n=n.assignedSlot||n.parentElement||n.getRootNode().host||null){
                        if(n.hasAttribute('inert')) throw new Error('Element is inert.');
                        if(n===modal)inside=true;
                    }
                    if(!inside) throw new Error('Modal blocks this element.');
                        if (typeof el.click !== 'function') {
                            throw new Error('Element does not expose click().');
                        }
                        el.click();
                        return true;
                    }"""
                )
                click_mode = "dom_event"
            else:
                await locator.click(timeout=timeout)
                click_mode = "trusted_playwright"
            self.audit.record(
                "dom_click",
                element_ref=element_ref,
                click_mode=click_mode,
            )
            observed=await click_result(_page,locator,before,before_url,expected_selector,allowed=self._is_allowed_url,verification=verification,condition_scope=condition_scope)
            await tracker.wait(popup_timeout_ms)
            created=[];blocked=0
            budget=(getattr(self,'resource_limits',None) or {}).get('max_tabs_per_session',8)
            for popup in list(set(action_context.pages)|set(tracker.pages)):
                if popup in previous_pages:continue
                if popup.url in ('','about:blank'):
                    try:await popup.wait_for_url(lambda url:str(url) not in ('','about:blank'),timeout=min(timeout,1000))
                    except Exception:pass
                try:await popup.wait_for_load_state('domcontentloaded',timeout=min(timeout,1000))
                except Exception:pass
                if not self._is_allowed_url(popup.url) or len(previous_pages)+len(created)>=budget:
                    await popup.close();blocked+=1;continue
                created.append({'tab_ref':self._tab_ref_for_page(popup),'url':popup.url,'title':await self._safe_title(popup)})
            return {
                "element_ref": element_ref,
                "clicked": True,
                "verification": observed,
                "created_tabs":created,"blocked_created_tabs":blocked,"popup_observation_ms":popup_timeout_ms,
                "click_mode": click_mode,
            }
        finally:tracker.close()

    async def set_value(
        self,
        element_ref: str,
        value: str,
        timeout_ms: int | None = None,
        verification: dict | None = None,
        precondition: dict | None = None,
    ) -> dict:
        self._require_mutation("allow_set_value", "DOM set value")
        from .dom_preconditions import validate,check
        validate(precondition)
        from .dom_verification import options
        options(verification)
        if verification and set(verification)&{'selector','state','text_contains','url_equals','url_contains'}:raise ValueError('Form verification checks the requested value; selector conditions require dom_click')
        if len(value) > self.config.max_text_chars:
            raise ValueError(
                f"value exceeds browser_dom.max_text_chars ({self.config.max_text_chars})."
            )
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        await check(locator,precondition)
        await self._check_modal_access(locator)

        if precondition is not None:
            from .dom_guarded_form import dispatch
            await dispatch(locator,'set_value',precondition,timeout,value=value)
            set_mode='guarded_dom_event'
        elif self.config.background_safe_events:
            await locator.wait_for(state="attached", timeout=timeout)
            result = await locator.evaluate(
                """(el, value) => {
                    const fireInput = (node, data) => {
                        try {
                            node.dispatchEvent(new InputEvent('input', {
                                bubbles: true,
                                composed: true,
                                inputType: data ? 'insertText' : 'deleteContentBackward',
                                data: data || null,
                            }));
                        } catch (_) {
                            node.dispatchEvent(new Event('input', {
                                bubbles: true,
                                composed: true,
                            }));
                        }
                    };

                    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
                        const proto = el instanceof HTMLTextAreaElement
                            ? HTMLTextAreaElement.prototype
                            : HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                        if (!setter) throw new Error('Native value setter is unavailable.');
                        setter.call(el, value);
                        fireInput(el, value);
                        el.dispatchEvent(new Event('change', {
                            bubbles: true,
                            composed: true,
                        }));
                        return {mode: 'native_value', value: String(el.value || '')};
                    }

                    if (el.isContentEditable) {
                        const expected = String(value).split(String.fromCharCode(13)).join('');

                        try {
                            el.dispatchEvent(new InputEvent('beforeinput', {
                                bubbles: true,
                                composed: true,
                                cancelable: true,
                                inputType: expected ? 'insertText' : 'deleteContentBackward',
                                data: expected || null,
                            }));
                        } catch (_) {}

                        // Keep ProseMirror's usual paragraph-shaped DOM without
                        // calling focus(), selection APIs, or trusted keyboard
                        // input. Those can restore a minimized Edge window.
                        while (el.firstChild) el.removeChild(el.firstChild);
                        const p = document.createElement('p');
                        if (expected) {
                            p.textContent = expected;
                        } else {
                            p.appendChild(document.createElement('br'));
                        }
                        el.appendChild(p);
                        fireInput(el, expected);

                        const actual = String(
                            el.innerText ?? el.textContent ?? ''
                        ).split(String.fromCharCode(13)).join('');
                        return {
                            mode: 'contenteditable_dom_no_focus',
                            value: actual,
                        };
                    }

                    if ('value' in el) {
                        el.value = value;
                        fireInput(el, value);
                        el.dispatchEvent(new Event('change', {
                            bubbles: true,
                            composed: true,
                        }));
                        return {mode: 'generic_value', value: String(el.value || '')};
                    }

                    throw new Error('Element is not writable by background-safe DOM events.');
                }""",
                value,
            )
            set_mode = str((result or {}).get("mode") or "dom_event")
        else:
            await locator.fill(value, timeout=timeout)
            set_mode = "trusted_playwright"

        self.audit.record(
            "dom_set_value",
            element_ref=element_ref,
            value_chars=len(value),
            set_mode=set_mode,
        )
        return {
            "element_ref": element_ref,
            "set": True,
            "verification": await value_result(locator,value,verification),
            "value_chars": len(value),
            "set_mode": set_mode,
        }

    async def select(
        self,
        element_ref: str,
        *,
        value: str | None = None,
        label: str | None = None,
        timeout_ms: int | None = None,
        verification: dict | None = None,
        precondition: dict | None = None,
    ) -> dict:
        self._require_mutation("allow_select", "DOM select")
        from .dom_preconditions import validate,check
        validate(precondition)
        from .dom_verification import options
        options(verification)
        if verification and set(verification)&{'selector','state','text_contains','url_equals','url_contains'}:raise ValueError('Form verification checks the requested value; selector conditions require dom_click')
        if (value is None) == (label is None):
            raise ValueError("Provide exactly one of value or label.")
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        await check(locator,precondition)
        await self._check_modal_access(locator)
        if precondition is not None:
            from .dom_guarded_form import dispatch
            selected=(await dispatch(locator,'select',precondition,timeout,value=value,label=label))['selected']
        else:
            selected = await locator.select_option(value=value,label=label,timeout=timeout)
        self.audit.record(
            "dom_select",
            element_ref=element_ref,
            selection_kind="value" if value is not None else "label",
        )
        from .dom_verification import selection_result
        return {"element_ref": element_ref, "selected": selected,"verification":await selection_result(locator,selected,verification)}

    async def toggle(
        self,
        element_ref: str,
        checked: bool | None = None,
        timeout_ms: int | None = None,
        verification: dict | None = None,
        precondition: dict | None = None,
    ) -> dict:
        self._require_mutation("allow_toggle", "DOM toggle")
        from .dom_preconditions import validate,check
        validate(precondition)
        from .dom_verification import options
        options(verification)
        if verification and set(verification)&{'selector','state','text_contains','url_equals','url_contains'}:raise ValueError('Form verification checks the requested value; selector conditions require dom_click')
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        await check(locator,precondition)
        await self._check_modal_access(locator)
        if precondition is not None:
            from .dom_guarded_form import dispatch
            result=await dispatch(locator,'toggle',precondition,timeout,checked=checked)
            before,target,after=result['before'],result['requested'],result['after']
        else:
            before = bool(await asyncio.wait_for(locator.is_checked(), timeout=timeout / 1000))
            target = (not before) if checked is None else bool(checked)
            if target:await locator.check(timeout=timeout)
            else:await locator.uncheck(timeout=timeout)
            after = bool(await asyncio.wait_for(locator.is_checked(), timeout=timeout / 1000))
        self.audit.record(
            "dom_toggle",
            element_ref=element_ref,
            before=before,
            after=after,
        )
        return {
            "element_ref": element_ref,
            "before": before,
            "after": after,
            "requested": target,
            "verification": await checked_result(locator,target,verification),
        }

    async def scroll_into_view(
        self,
        element_ref: str,
        timeout_ms: int | None = None,
    ) -> dict:
        self._require_mutation("allow_scroll", "DOM scroll")
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        await locator.scroll_into_view_if_needed(timeout=timeout)
        self.audit.record("dom_scroll_into_view", element_ref=element_ref)
        return {"element_ref": element_ref, "scrolled_into_view": True}

    async def scroll(
        self,
        tab_ref: str,
        *,
        direction: str,
        amount: int = 600,
        element_ref: str | None = None,
    ) -> dict:
        self._require_mutation("allow_scroll", "DOM scroll")
        normalized = str(direction).strip().casefold()
        if normalized not in {"up", "down", "left", "right"}:
            raise ValueError("direction must be up, down, left, or right.")
        pixels = min(max(1, int(amount)), self.config.max_scroll_pixels)
        dx = 0
        dy = 0
        if normalized == "up":
            dy = -pixels
        elif normalized == "down":
            dy = pixels
        elif normalized == "left":
            dx = -pixels
        else:
            dx = pixels

        browser = await self._ensure_browser()
        page = await self._resolve_tab(browser, tab_ref)
        if element_ref:
            token = self._decode(element_ref, expected_type="element")
            if str(token.get("tab_ref", "")) != tab_ref:
                raise ValueError("element_ref belongs to a different tab.")
            _element_page, locator = await self._resolve_element(browser, element_ref)
            await locator.evaluate(
                "(el, delta) => el.scrollBy({left: delta.dx, top: delta.dy, behavior: 'instant'})",
                {"dx": dx, "dy": dy},
            )
        else:
            await page.evaluate(
                "([dx, dy]) => window.scrollBy({left: dx, top: dy, behavior: 'instant'})",
                [dx, dy],
            )

        self.audit.record(
            "dom_scroll",
            tab_ref=tab_ref,
            element_ref=element_ref,
            direction=normalized,
            amount=pixels,
        )
        return {
            "tab_ref": tab_ref,
            "element_ref": element_ref,
            "direction": normalized,
            "amount": pixels,
            "scrolled": True,
        }

    async def wait(
        self,
        element_ref: str,
        state: str = "visible",
        timeout_ms: int | None = None,
    ) -> dict:
        self._require_read()
        normalized = str(state).strip().casefold()
        if normalized not in {"attached", "detached", "visible", "hidden"}:
            raise ValueError("state must be attached, detached, visible, or hidden.")
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        await locator.wait_for(state=normalized, timeout=timeout)
        self.audit.record(
            "dom_wait",
            element_ref=element_ref,
            state=normalized,
            timeout_ms=timeout,
        )
        return {
            "element_ref": element_ref,
            "state": normalized,
            "satisfied": True,
        }

    @staticmethod
    def _pages(browser) -> list:
        pages = []
        for context in browser.contexts:
            pages.extend(context.pages)
        return pages

    def _tab_ref_for_page(self, page) -> str:
        if page not in self._lifecycle_pages:
            self._lifecycle_pages.add(page)
            def closed(_page=None):
                self._lifecycle_pages.discard(page)
                refs=[self._encode({'type':'tab','id':key}) for key,value in self._stable_tabs.items() if value is page]
                async def cleanup():
                    for ref in refs:
                        await self._prune_pinned_elements(tab_ref=ref)
                        self._snapshots.forget_target(ref)
                        for key,(tab,_) in list(self._snapshot_frames.items()):
                            if tab==ref:self._snapshot_frames.pop(key,None)
                    for key,value in list(self._stable_tabs.items()):
                        if value is page:self._stable_tabs.pop(key,None)
                asyncio.create_task(cleanup())
            page.on('close',closed)
        for stable_id, candidate in list(self._stable_tabs.items()):
            try:
                if candidate is page and not candidate.is_closed():
                    return self._encode({"type": "tab", "id": stable_id})
                if candidate.is_closed():
                    self._stable_tabs.pop(stable_id, None)
            except Exception:
                self._stable_tabs.pop(stable_id, None)

        stable_id = uuid4().hex
        self._stable_tabs[stable_id] = page
        return self._encode({"type": "tab", "id": stable_id})

    async def _resolve_tab(self, browser, tab_ref: str,allow_blocked_url=False):
        token = self._decode(tab_ref, expected_type="tab")
        stable_id = token.get("id")
        if stable_id is not None:
            page = self._stable_tabs.get(str(stable_id))
            if page is None:
                from ..automation_errors import ReferenceUnavailable
                raise ReferenceUnavailable('tab_closed')
            if page.is_closed():
                self._stable_tabs.pop(str(stable_id), None)
                from ..automation_errors import ReferenceUnavailable
                raise ReferenceUnavailable('tab_closed')
            if page not in self._pages(browser):raise PermissionError("Tab belongs to another session context")
            if not allow_blocked_url:self._require_allowed_url(str(page.url or ""))
            return page

        pages = self._pages(browser)
        target_url = str(token.get("url", ""))
        target_title = str(token.get("title", ""))
        ordinal = int(token.get("ordinal", 0))

        exact_matches = []
        url_matches = []
        for page in pages:
            if str(page.url or "") != target_url:
                continue
            url_matches.append(page)
            if await self._safe_title(page) == target_title:
                exact_matches.append(page)

        candidates = exact_matches or url_matches
        if not candidates or ordinal >= len(candidates):
            raise LookupError(
                "The referenced Edge tab is no longer available. Call dom_list_tabs again."
            )
        page = candidates[ordinal]
        self._require_allowed_url(str(page.url or ""))
        return page

    def _base_locator(
        self,
        page,
        *,
        selector: str | None,
        text: str | None,
        role: str | None,
        name: str | None,
        exact: bool,
    ):
        if selector:
            descriptor = {"kind": "selector", "query": str(selector)}
            return page.locator(str(selector)), descriptor
        if text:
            descriptor = {
                "kind": "text",
                "query": str(text),
                "exact": bool(exact),
            }
            return page.get_by_text(str(text), exact=bool(exact)), descriptor

        descriptor = {
            "kind": "role",
            "query": str(role),
            "name": None if name is None else str(name),
            "exact": bool(exact),
        }
        kwargs = {"exact": bool(exact)}
        if name is not None:
            kwargs["name"] = str(name)
        return page.get_by_role(str(role), **kwargs), descriptor

    async def _resolve_element(self, browser, element_ref: str):
        token = self._decode(element_ref, expected_type="element")
        tab_ref = str(token.get("tab_ref", ""))
        page = await self._resolve_tab(browser, tab_ref)
        await self._prune_pinned_elements()
        pin = token.get('pin')
        if not pin:
            raise LookupError('STALE_DOM_REF: legacy locator reference; run dom_find again')
        entry = self._pinned_elements.get(str(pin))
        if entry is None or entry[0] != tab_ref:
            from ..automation_errors import ReferenceUnavailable
            raise ReferenceUnavailable(self._retired_pins.get(str(pin),('node_expired',0))[0])
        try:
            owner_frame = await entry[1].owner_frame()
            if owner_frame is None: raise LookupError('Detached frame')
            self._require_allowed_url(str(owner_frame.url or ''))
            connected = await entry[1].evaluate('(el) => el.isConnected && el.ownerDocument.defaultView !== null')
        except PermissionError:
            raise
        except Exception:
            connected = False
        if not connected:
            self._pinned_elements.pop(str(pin), None)
            try: await asyncio.wait_for(entry[1].dispose(),timeout=1)
            except Exception: pass
            self._retired_pins[str(pin)]=('node_replaced',time.monotonic())
            from ..automation_errors import ReferenceUnavailable
            raise ReferenceUnavailable('node_replaced')
        from .pinned_dom import PinnedNode
        return page, PinnedNode(entry[1])

    async def _prune_pinned_elements(self, all_nodes=False, tab_ref=None):
        now = time.monotonic()
        self._retired_pins={pin:value for pin,value in self._retired_pins.items() if now-value[1]<600}
        for pin, (tab, handle, expires) in list(self._pinned_elements.items()):
            if all_nodes or expires <= now or (tab_ref is not None and tab == tab_ref):
                self._pinned_elements.pop(pin, None)
                self._retired_pins[pin]=('node_expired' if expires<=now else 'node_replaced',now)
                while len(self._retired_pins)>1000:self._retired_pins.pop(next(iter(self._retired_pins)))
                try: await asyncio.wait_for(handle.dispose(),timeout=1)
                except Exception: pass
        for pin,(tab,frame) in list(self._snapshot_frames.items()):
            if all_nodes or frame.is_detached() or (tab_ref is not None and tab==tab_ref):
                self._snapshot_frames.pop(pin,None)

    async def snapshot_diff(self,tab_ref,previous_snapshot_ref,max_items=100,max_depth=12,
                            include_hidden=False,include_frames=False,include_shadow=False,root_selector=None,compact=False,max_changes=100):
        from ..snapshot_cache import owner_scope
        scope=owner_scope(tab_ref)
        self._snapshots.get(previous_snapshot_ref,scope)
        current=await self.snapshot(tab_ref,max_items,max_depth,include_hidden,include_frames,include_shadow,root_selector)
        return self._snapshots.diff(previous_snapshot_ref,current['snapshot_ref'],scope,compact=compact,max_changes=max_changes)

    async def _check_modal_access(self,locator):
        blocked=await locator.evaluate("el=>{const m=el.ownerDocument.querySelector('dialog:modal');let inside=!m;for(let n=el;n;n=n.assignedSlot||n.parentElement||n.getRootNode().host||null){if(n.hasAttribute('inert'))return true;if(n===m)inside=true;}return !!el.disabled||!inside;}")
        if blocked:
            from .dom_preconditions import DOMPreconditionError
            raise DOMPreconditionError('Element disabled, inert or blocked by a modal')

    async def release_session(self,owner,session):
        cleanup_pending=False
        context=self._session_contexts.pop((owner,session),None)
        external=self._external_sessions.pop((owner,session),None)
        pending=self._pending_auto_sessions.pop((owner,session),None)
        if context is not None:
            pages=list(context.pages)
            refs=[self._encode({'type':'tab','id':key}) for key,page in self._stable_tabs.items() if page in pages]
            for ref in refs:
                await self._prune_pinned_elements(tab_ref=ref)
                self._snapshots.forget_target(ref)
            try:await asyncio.wait_for(context.close(),timeout=3)
            except Exception:cleanup_pending=True
            for key,page in list(self._stable_tabs.items()):
                if page in pages:self._stable_tabs.pop(key,None)
        if external is not None or pending is not None:
            lease=external if external is not None else pending
            try:await asyncio.wait_for(lease['driver'].stop(),timeout=3)
            except Exception:
                cleanup_pending=True
                if lease.get('auto'):
                    lease['context']=None
                    lease['pending']=True
                    lease['cleanup_only']=True
                    lease['last_activity']=time.monotonic()
                    self._pending_auto_sessions[(owner,session)]=lease
        sessions=getattr(self,'resource_sessions',None)
        if sessions and (external is not None or pending is not None):
            try:
                with sessions.db() as db:
                    rows=db.execute("SELECT id FROM handles WHERE session=? AND kind LIKE 'dom_%'",(session,)).fetchall()
                    for handle, in rows:sessions.remember_retired(db,session,handle,'lease_expired')
                    db.execute("DELETE FROM handles WHERE session=? AND kind LIKE 'dom_%'",(session,))
            except Exception:cleanup_pending=True
        self._snapshots.forget_session(owner,session)
        return {'released':True,'resource_cleanup':'pending' if cleanup_pending else 'completed'}

    async def _element_metadata(self, locator, include_attributes: bool = False) -> dict:
        script = """(el) => {
          const type = (el.getAttribute('type') || '').toLowerCase();
          const password = type === 'password';
          const text = (el.innerText !== undefined ? el.innerText : (el.textContent || ''));
          const out = {
            tag: (el.tagName || '').toLowerCase(),
            id: el.id || null,
            class_name: typeof el.className === 'string' ? el.className : null,
            role: el.getAttribute('role'),
            aria_label: el.getAttribute('aria-label'),
            name: el.getAttribute('name'),
            type: type || null,
            href: el.getAttribute('href'),
            is_password: password,
            value: password ? null : (('value' in el) ? String(el.value ?? '') : null),
            checked: ('checked' in el) ? Boolean(el.checked) : null,
            disabled: ('disabled' in el) ? Boolean(el.disabled) : null,
            text: String(text || '')
          };
          if (INCLUDE_ATTRIBUTES) {
            out.attributes = {};
            for (const attr of el.attributes || []) out.attributes[attr.name] = attr.value;
            if (password && out.attributes) delete out.attributes.value;
          }
          return out;
        }""".replace("INCLUDE_ATTRIBUTES", "true" if include_attributes else "false")
        data = await locator.evaluate(script)
        if not isinstance(data, dict):
            data = {}
        text = str(data.get("text") or "")
        data["text"] = text[: min(500, self.config.max_text_chars)]
        data["text_truncated"] = len(text) > len(data["text"])
        try:
            data["visible"] = bool(await locator.is_visible())
        except Exception:
            data["visible"] = None
        return data

    def _dedicated_user_data_dir(self) -> Path:
        user_dir = Path(os.path.expandvars(self.config.user_data_dir)).resolve()
        main_dir = Path(
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        ).resolve()
        if user_dir == main_dir:
            raise RuntimeError(
                "Refusing to manage the worker browser with the main Edge user-data directory. "
                "Configure a dedicated browser_dom.user_data_dir first."
            )
        return user_dir

    def _matching_worker_edge_roots(self) -> list[psutil.Process]:
        target = str(self._dedicated_user_data_dir()).casefold()
        roots: list[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if str(proc.info.get("name") or "").casefold() != "msedge.exe":
                    continue
                for arg in proc.info.get("cmdline") or []:
                    text = str(arg)
                    if not text.casefold().startswith("--user-data-dir="):
                        continue
                    raw = text.split("=", 1)[1].strip().strip('"')
                    try:
                        resolved = str(
                            Path(os.path.expandvars(raw)).resolve()
                        ).casefold()
                    except OSError:
                        resolved = raw.casefold()
                    if resolved == target:
                        roots.append(proc)
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return roots

    def _worker_edge_pids(self) -> list[int]:
        return sorted({int(proc.pid) for proc in self._matching_worker_edge_roots()})

    def _stop_worker_edge_processes(self, timeout: float = 6.0) -> list[int]:
        roots = self._matching_worker_edge_roots()
        if not roots:
            return []

        targets: dict[int, psutil.Process] = {}
        for root in roots:
            try:
                for child in root.children(recursive=True):
                    targets[int(child.pid)] = child
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            targets[int(root.pid)] = root

        ordered = list(targets.values())
        for proc in reversed(ordered):
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        _gone, alive = psutil.wait_procs(
            ordered,
            timeout=max(0.5, float(timeout)),
        )
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=2.0)
        return sorted(targets)

    async def _launch_dedicated_edge(self) -> None:
        raise PermissionError("BACKGROUND_ONLY: headed browser launch is forbidden")

    @staticmethod
    def _foreground_window_handle() -> int:
        if os.name != "nt":
            return 0
        try:
            return int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            return 0

    @staticmethod
    def _restore_foreground_window(hwnd: int) -> bool:
        raise PermissionError("BACKGROUND_ONLY: foreground manipulation is forbidden")

    def _start_worker_focus_guard(
        self,
        restore_hwnd: int,
        worker_pids=(),
    ) -> None:
        """Start one temporary WinEvent guard for Worker focus and visibility."""
        if (
            os.name != "nt"
            or not (
                self.config.focus_guard_enabled
                or self.config.visibility_guard_enabled
            )
        ):
            return

        user32 = ctypes.windll.user32
        restore_hwnd = int(restore_hwnd or 0)
        if restore_hwnd and not user32.IsWindow(ctypes.c_void_p(restore_hwnd)):
            restore_hwnd = 0

        self._stop_worker_focus_guard()
        self._worker_focus_guard_pids = {
            int(pid) for pid in worker_pids if int(pid) > 0
        }

        stop_event = threading.Event()
        ready_event = threading.Event()
        thread = threading.Thread(
            target=self._worker_focus_guard_loop,
            args=(restore_hwnd, stop_event, ready_event),
            name="ChatGPT-Actuator-WindowGuard",
            daemon=True,
        )
        self._worker_focus_guard_stop = stop_event
        self._worker_focus_guard_thread = thread
        thread.start()
        ready_event.wait(timeout=0.5)

    def _stop_worker_focus_guard(self) -> None:
        stop_event = self._worker_focus_guard_stop
        thread = self._worker_focus_guard_thread
        thread_id = int(self._worker_focus_guard_thread_id or 0)

        self._worker_focus_guard_stop = None
        self._worker_focus_guard_thread = None

        if stop_event is not None:
            stop_event.set()

        if os.name == "nt" and thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    wintypes.DWORD(thread_id),
                    0x0012,
                    0,
                    0,
                )
            except Exception:
                pass

        if (
            thread is not None
            and thread.is_alive()
            and threading.current_thread() is not thread
        ):
            thread.join(timeout=0.75)

        self._worker_focus_guard_thread_id = 0
        self._worker_focus_guard_pids = set()

    def _worker_focus_guard_loop(
        self,
        restore_hwnd: int,
        stop_event: threading.Event,
        ready_event: threading.Event,
    ) -> None:
        """Use WinEvent hooks only; there is no idle polling."""
        if os.name != "nt":
            ready_event.set()
            return

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.SetWinEventHook.restype = ctypes.c_void_p
        user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
        user32.UnhookWinEvent.restype = wintypes.BOOL
        user32.GetAncestor.restype = wintypes.HWND

        EVENT_SYSTEM_FOREGROUND = 0x0003
        EVENT_OBJECT_SHOW = 0x8002
        EVENT_OBJECT_LOCATIONCHANGE = 0x800B
        OBJID_WINDOW = 0
        GA_ROOT = 2
        WINEVENT_OUTOFCONTEXT = 0x0000
        WINEVENT_SKIPOWNPROCESS = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010

        callback_type = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            wintypes.LONG,
            wintypes.LONG,
            wintypes.DWORD,
            wintypes.DWORD,
        )

        def is_worker_pid(pid: int) -> bool:
            pid = int(pid)
            if pid <= 0:
                return False
            if pid in self._worker_focus_guard_pids:
                return True
            try:
                detected = set(self._worker_edge_pids())
            except Exception:
                detected = set()
            if pid in detected:
                self._worker_focus_guard_pids.update(detected)
                return True
            return False

        def top_level_chrome_window(hwnd) -> bool:
            try:
                root = user32.GetAncestor(hwnd, GA_ROOT)
                if int(root or 0) != int(hwnd or 0):
                    return False
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buf, len(class_buf))
                return class_buf.value == "Chrome_WidgetWin_1"
            except Exception:
                return False

        def move_worker_offscreen(hwnd, pid: int, event_name: str) -> None:
            if not self.config.visibility_guard_enabled:
                return
            if not top_level_chrome_window(hwnd):
                return

            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return

            if int(rect.left) <= -30000 and int(rect.top) <= -30000:
                return

            width = max(1, int(rect.right) - int(rect.left))
            height = max(1, int(rect.bottom) - int(rect.top))
            moved = bool(
                user32.SetWindowPos(
                    hwnd,
                    None,
                    -32000,
                    -32000,
                    width,
                    height,
                    SWP_NOZORDER | SWP_NOACTIVATE,
                )
            )
            if moved:
                self.audit.record(
                    "dom_worker_visibility_guard_offscreen",
                    worker_pid=int(pid),
                    worker_hwnd=int(hwnd),
                    trigger=event_name,
                    previous_left=int(rect.left),
                    previous_top=int(rect.top),
                )

        @callback_type
        def on_window_event(
            _hook,
            event,
            hwnd,
            object_id,
            _child_id,
            _event_thread,
            _event_time,
        ):
            if stop_event.is_set() or not hwnd:
                return
            try:
                pid_value = wintypes.DWORD()
                user32.GetWindowThreadProcessId(
                    hwnd,
                    ctypes.byref(pid_value),
                )
                pid = int(pid_value.value)
                if not is_worker_pid(pid):
                    return

                event_value = int(event)
                object_value = int(object_id)

                if (
                    self.config.visibility_guard_enabled
                    and event_value in {
                        EVENT_OBJECT_SHOW,
                        EVENT_OBJECT_LOCATIONCHANGE,
                    }
                    and object_value == OBJID_WINDOW
                ):
                    move_worker_offscreen(
                        hwnd,
                        pid,
                        (
                            "show"
                            if event_value == EVENT_OBJECT_SHOW
                            else "location_change"
                        ),
                    )

                if (
                    self.config.focus_guard_enabled
                    and event_value == EVENT_SYSTEM_FOREGROUND
                    and restore_hwnd
                    and int(hwnd) != int(restore_hwnd)
                ):
                    restored = self._restore_foreground_window(restore_hwnd)
                    self.audit.record(
                        "dom_worker_focus_guard_restore",
                        worker_pid=pid,
                        worker_hwnd=int(hwnd),
                        restore_hwnd=int(restore_hwnd),
                        restored=bool(restored),
                    )
            except Exception as exc:
                self.audit.record(
                    "dom_worker_window_guard_error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

        self._worker_focus_guard_thread_id = int(
            kernel32.GetCurrentThreadId() or 0
        )

        hooks = []
        hook_specs = []
        if self.config.focus_guard_enabled:
            hook_specs.append(
                (
                    EVENT_SYSTEM_FOREGROUND,
                    EVENT_SYSTEM_FOREGROUND,
                    "foreground",
                )
            )
        if self.config.visibility_guard_enabled:
            hook_specs.extend(
                [
                    (EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, "object_show"),
                    (
                        EVENT_OBJECT_LOCATIONCHANGE,
                        EVENT_OBJECT_LOCATIONCHANGE,
                        "object_locationchange",
                    ),
                ]
            )

        for event_min, event_max, label in hook_specs:
            hook = user32.SetWinEventHook(
                event_min,
                event_max,
                None,
                on_window_event,
                0,
                0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
            )
            if hook:
                hooks.append(hook)
            else:
                self.audit.record(
                    "dom_worker_window_guard_error",
                    error_type="WinEventHookError",
                    error=f"SetWinEventHook returned NULL for {label}.",
                )

        if not hooks:
            ready_event.set()
            self._worker_focus_guard_thread_id = 0
            return

        self.audit.record(
            "dom_worker_focus_guard_started",
            restore_hwnd=int(restore_hwnd),
            mode="event_driven",
            focus_guard=bool(self.config.focus_guard_enabled),
            visibility_guard=bool(self.config.visibility_guard_enabled),
            hook_count=len(hooks),
        )
        if self.config.visibility_guard_enabled:
            self.audit.record(
                "dom_worker_visibility_guard_started",
                mode="event_driven",
                events=["EVENT_OBJECT_SHOW", "EVENT_OBJECT_LOCATIONCHANGE"],
            )
        ready_event.set()

        msg = wintypes.MSG()
        try:
            while not stop_event.is_set():
                result = int(
                    user32.GetMessageW(
                        ctypes.byref(msg),
                        None,
                        0,
                        0,
                    )
                )
                if result <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for hook in hooks:
                try:
                    user32.UnhookWinEvent(hook)
                except Exception:
                    pass
            self._worker_focus_guard_thread_id = 0
            self.audit.record(
                "dom_worker_focus_guard_stopped",
                restore_hwnd=int(restore_hwnd),
                focus_guard=bool(self.config.focus_guard_enabled),
                visibility_guard=bool(self.config.visibility_guard_enabled),
            )
            if self.config.visibility_guard_enabled:
                self.audit.record(
                    "dom_worker_visibility_guard_stopped",
                    mode="event_driven",
                )

    def _finalize_worker_window_background_state(self, pids) -> None:
        """Apply long-lived Worker placement after startup hooks are removed."""
        if os.name != "nt":
            return

        pid_set = {int(pid) for pid in pids if int(pid) > 0}
        if not pid_set:
            return

        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.UINT),
                ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT),
            ]

        windows = []

        @enum_proc
        def callback(hwnd, _lparam):
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(
                ctypes.c_void_p(hwnd),
                ctypes.byref(process_id),
            )
            if int(process_id.value) not in pid_set:
                return True
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(
                ctypes.c_void_p(hwnd),
                class_buf,
                len(class_buf),
            )
            if class_buf.value != "Chrome_WidgetWin_1":
                return True
            title_len = int(
                user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)) or 0
            )
            title_buf = ctypes.create_unicode_buffer(max(1, title_len + 1))
            user32.GetWindowTextW(
                ctypes.c_void_p(hwnd),
                title_buf,
                len(title_buf),
            )
            if (
                str(title_buf.value or "").strip().casefold()
                == "allow remote debugging?"
            ):
                return True
            windows.append(int(hwnd))
            return True

        user32.EnumWindows(callback, 0)

        finalized = 0
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        for hwnd in list(dict.fromkeys(windows)):
            if self.config.background_mode == "offscreen":
                user32.SetWindowPos(
                    ctypes.c_void_p(hwnd),
                    None,
                    -32000,
                    -32000,
                    1280,
                    900,
                    SWP_NOZORDER | SWP_NOACTIVATE,
                )
                user32.ShowWindow(ctypes.c_void_p(hwnd), 4)
            else:
                placement = WINDOWPLACEMENT()
                placement.length = ctypes.sizeof(WINDOWPLACEMENT)
                if user32.GetWindowPlacement(
                    ctypes.c_void_p(hwnd),
                    ctypes.byref(placement),
                ):
                    placement.showCmd = 7
                    placement.rcNormalPosition = wintypes.RECT(
                        80,
                        80,
                        1360,
                        980,
                    )
                    user32.SetWindowPlacement(
                        ctypes.c_void_p(hwnd),
                        ctypes.byref(placement),
                    )
                user32.ShowWindow(ctypes.c_void_p(hwnd), 7)
            finalized += 1

        restore_hwnd = int(self._worker_restore_foreground or 0)
        if restore_hwnd:
            self._restore_foreground_window(restore_hwnd)

        self.audit.record(
            "dom_worker_window_finalized",
            worker_pids=sorted(pid_set),
            finalized_windows=finalized,
            background_mode=self.config.background_mode,
            restore_left=(
                80 if self.config.background_mode == "minimized" else -32000
            ),
            restore_top=(
                80 if self.config.background_mode == "minimized" else -32000
            ),
        )

    def _enforce_worker_background_window(
        self,
        pid: int,
        previous_foreground: int,
    ) -> None:
        """Keep exactly one dedicated worker window non-intrusive and inspectable."""
        if os.name != "nt":
            return

        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        def window_title(hwnd: int) -> str:
            length = int(user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)) or 0)
            buf = ctypes.create_unicode_buffer(max(1, length + 1))
            user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, len(buf))
            return str(buf.value or "")

        def worker_windows() -> list[int]:
            hwnd_box: list[int] = []

            @enum_proc
            def callback(hwnd, _lparam):
                process_id = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(
                    ctypes.c_void_p(hwnd),
                    ctypes.byref(process_id),
                )
                if int(process_id.value) != int(pid):
                    return True
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(
                    ctypes.c_void_p(hwnd),
                    class_buf,
                    len(class_buf),
                )
                if class_buf.value == "Chrome_WidgetWin_1":
                    hwnd_box.append(int(hwnd))
                return True

            user32.EnumWindows(callback, 0)
            return list(dict.fromkeys(hwnd_box))

        deadline = time.monotonic() + 3.0
        hwnds: list[int] = []
        while time.monotonic() < deadline:
            hwnds = worker_windows()
            if hwnds:
                break
            time.sleep(0.05)

        if not hwnds:
            return

        # Edge may restore multiple windows from the dedicated profile even
        # though this launch requested only one about:blank window. Keep one
        # top-level worker window and close any late-restored extras without
        # activating them. Never close the Edge remote-debugging consent dialog;
        # it is handled separately with UI Automation.
        normal_hwnds = [
            item for item in hwnds
            if window_title(item).strip().casefold() != "allow remote debugging?"
        ]
        hwnd = normal_hwnds[0] if normal_hwnds else hwnds[0]
        closed_extra_hwnds: set[int] = set()
        settle_deadline = time.monotonic() + 1.5
        WM_CLOSE = 0x0010
        while time.monotonic() < settle_deadline:
            current = worker_windows()
            if hwnd not in current and current:
                hwnd = current[0]
            for extra_hwnd in current:
                if extra_hwnd == hwnd:
                    continue
                if window_title(extra_hwnd).strip().casefold() == "allow remote debugging?":
                    continue
                closed_extra_hwnds.add(int(extra_hwnd))
                user32.PostMessageW(
                    ctypes.c_void_p(extra_hwnd),
                    WM_CLOSE,
                    0,
                    0,
                )
            time.sleep(0.1)

        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        startup_offscreen = (
            self.config.background_mode == "offscreen"
            or self.config.visibility_guard_enabled
        )
        if startup_offscreen:
            user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                None,
                -32000,
                -32000,
                1280,
                900,
                SWP_NOZORDER | SWP_NOACTIVATE,
            )
            user32.ShowWindow(ctypes.c_void_p(hwnd), 4)  # SW_SHOWNOACTIVATE
        else:
            user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                None,
                80,
                80,
                1280,
                900,
                SWP_NOZORDER | SWP_NOACTIVATE,
            )
            user32.ShowWindow(ctypes.c_void_p(hwnd), 6)  # SW_MINIMIZE

        if previous_foreground and previous_foreground != hwnd:
            self._restore_foreground_window(previous_foreground)

        self.audit.record(
            "dom_worker_window_backgrounded",
            pid=int(pid),
            hwnd=int(hwnd),
            background_mode=self.config.background_mode,
            restore_left=(-32000 if startup_offscreen else 80),
            restore_top=(-32000 if startup_offscreen else 80),
            startup_offscreen=bool(startup_offscreen),
            closed_extra_hwnds=sorted(closed_extra_hwnds),
            closed_extra_window_count=len(closed_extra_hwnds),
        )

    def _auto_allow_remote_debugging_prompt(
        self,
        pids: int | list[int] | tuple[int, ...] | set[int],
        timeout: float = 0.0,
        stop_event: threading.Event | None = None,
        restore_foreground: int = 0,
    ) -> bool:
        """Approve Edge remote debugging only for the dedicated worker process."""
        if not self.config.auto_allow_remote_debugging or os.name != "nt":
            return False

        if isinstance(pids, int):
            pid_set = {int(pids)}
        else:
            pid_set = {int(pid) for pid in pids if int(pid) > 0}
        if not pid_set:
            return False

        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        def prompt_hwnds() -> list[tuple[int, int]]:
            found: list[tuple[int, int]] = []

            @enum_proc
            def callback(hwnd, _lparam):
                process_id = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(
                    ctypes.c_void_p(hwnd),
                    ctypes.byref(process_id),
                )
                pid = int(process_id.value)
                if pid not in pid_set:
                    return True
                length = int(
                    user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)) or 0
                )
                buf = ctypes.create_unicode_buffer(max(1, length + 1))
                user32.GetWindowTextW(
                    ctypes.c_void_p(hwnd),
                    buf,
                    len(buf),
                )
                if str(buf.value or "").strip().casefold() == "allow remote debugging?":
                    found.append((int(hwnd), pid))
                return True

            user32.EnumWindows(callback, 0)
            return found

        def rebackground_worker_windows() -> None:
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010

            @enum_proc
            def callback(hwnd, _lparam):
                process_id = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(
                    ctypes.c_void_p(hwnd),
                    ctypes.byref(process_id),
                )
                if int(process_id.value) not in pid_set:
                    return True
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(
                    ctypes.c_void_p(hwnd),
                    class_buf,
                    len(class_buf),
                )
                if class_buf.value != "Chrome_WidgetWin_1":
                    return True
                length = int(
                    user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)) or 0
                )
                title_buf = ctypes.create_unicode_buffer(max(1, length + 1))
                user32.GetWindowTextW(
                    ctypes.c_void_p(hwnd),
                    title_buf,
                    len(title_buf),
                )
                if str(title_buf.value or "").strip().casefold() == "allow remote debugging?":
                    return True
                if (
                    self.config.background_mode == "offscreen"
                    or self.config.visibility_guard_enabled
                ):
                    user32.SetWindowPos(
                        ctypes.c_void_p(hwnd),
                        None,
                        -32000,
                        -32000,
                        1280,
                        900,
                        SWP_NOZORDER | SWP_NOACTIVATE,
                    )
                    user32.ShowWindow(ctypes.c_void_p(hwnd), 4)
                else:
                    user32.ShowWindow(ctypes.c_void_p(hwnd), 6)
                return True

            user32.EnumWindows(callback, 0)
            if restore_foreground:
                try:
                    self._restore_foreground_window(int(restore_foreground))
                except Exception:
                    pass

        deadline = time.monotonic() + max(0.0, float(timeout))
        first = True
        while (
            (first or time.monotonic() < deadline)
            and not (stop_event is not None and stop_event.is_set())
        ):
            first = False
            hwnds = prompt_hwnds()
            if hwnds:
                try:
                    import pythoncom
                    from pywinauto import Desktop

                    pythoncom.CoInitialize()
                    try:
                        for hwnd, prompt_pid in hwnds:
                            window = Desktop(backend="uia").window(handle=int(hwnd))
                            for button in window.descendants(control_type="Button"):
                                try:
                                    name = str(button.window_text() or "").strip()
                                except Exception:
                                    continue
                                if name.casefold() != "allow":
                                    continue
                                iface = getattr(button, "iface_invoke", None)
                                if iface is None:
                                    continue
                                iface.Invoke()
                                time.sleep(0.12)
                                rebackground_worker_windows()
                                self.audit.record(
                                    "dom_remote_debugging_auto_allowed",
                                    pid=int(prompt_pid),
                                    hwnd=int(hwnd),
                                    window_title="Allow remote debugging?",
                                    button=name,
                                    background=True,
                                    worker_rebackgrounded=True,
                                )
                                return True
                    finally:
                        try:
                            pythoncom.CoUninitialize()
                        except Exception:
                            pass
                except Exception as exc:
                    self.audit.record(
                        "dom_remote_debugging_auto_allow_error",
                        error=f"{type(exc).__name__}: {exc}",
                        stage="uia",
                        prompt_hwnds=[item[0] for item in hwnds],
                    )
            if time.monotonic() < deadline:
                if stop_event is not None:
                    if stop_event.wait(0.1):
                        break
                else:
                    time.sleep(0.1)
        return False

    def _resolved_cdp_url(self) -> str:
        configured = self.config.cdp_url.strip()
        if configured.casefold() != "auto":
            return configured

        active_port = Path(os.path.expandvars(self.config.user_data_dir)) / "DevToolsActivePort"
        if not active_port.is_file():
            raise RuntimeError(
                "Edge auto-connect is not enabled for the configured worker profile. "
                "Launch the worker as a normal Edge instance, open "
                "edge://inspect/#remote-debugging, and enable "
                "'Allow remote debugging for this browser instance' once."
            )
        lines = active_port.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise RuntimeError(f"Invalid DevToolsActivePort file: {active_port}")
        try:
            port = int(lines[0].strip())
        except ValueError as exc:
            raise RuntimeError(f"Invalid DevToolsActivePort port: {active_port}") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError(f"Invalid DevToolsActivePort port: {port}")

        # Edge daily-driver remote debugging may expose the exact browser
        # WebSocket path while /json/version returns 404.
        if len(lines) >= 2:
            browser_path = lines[1].strip()
            if browser_path.startswith("/devtools/browser/"):
                return f"ws://127.0.0.1:{port}{browser_path}"

        # Chromium/headless fallback.
        return f"http://127.0.0.1:{port}"

    @staticmethod
    async def _safe_title(page) -> str:
        try:
            return str(await asyncio.wait_for(page.title(),timeout=2) or "")
        except Exception:
            return ""

    def _timeout(self, timeout_ms: int | None) -> int:
        if timeout_ms is None:
            return self.config.default_timeout_ms
        return min(max(1, int(timeout_ms)), self.config.max_timeout_ms)

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            from ..automation_errors import PolicyDenied
            raise PolicyDenied('local_policy')

    def _require_read(self) -> None:
        self._require_enabled()
        if not self.config.allow_read:
            from ..automation_errors import PolicyDenied
            raise PolicyDenied('local_policy')

    def _require_mutation(self, flag: str, label: str) -> None:
        self._require_enabled()
        if not bool(getattr(self.config, flag)):
            from ..automation_errors import PolicyDenied
            raise PolicyDenied('local_policy')

    def _is_allowed_url(self, url: str) -> bool:
        from ..control_plane_urls import protected_origin
        if protected_origin(url):return False
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").casefold()
        if not host:
            return False
        from ..tool_policy import principal_context
        principal = principal_context.get()
        delegated = getattr(principal, 'autonomous_hosts', None)
        if delegated is not None and not any(host == item or host.endswith('.' + item) for item in delegated):
            return False
        allowed = tuple(x.casefold().strip() for x in self.config.allowed_hosts if x.strip())
        if "*" in allowed:
            return True
        return any(host == item or host.endswith("." + item) for item in allowed)

    def _require_allowed_url(self, url: str) -> None:
        from ..control_plane_urls import protected_origin
        if protected_origin(url):
            from ..automation_errors import PolicyDenied
            raise PolicyDenied('local_policy')
        if not self._is_allowed_url(url):
            from ..automation_errors import PolicyDenied
            raise PolicyDenied('blocked_host')

    @classmethod
    def _encode(cls, payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
        return cls.TOKEN_PREFIX + token

    @classmethod
    def _decode(cls, token: str, expected_type: str) -> dict:
        if not isinstance(token, str) or not token.startswith(cls.TOKEN_PREFIX):
            raise ValueError("Invalid DOM reference.")
        data = token[len(cls.TOKEN_PREFIX):]
        data += "=" * (-len(data) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8"))
        except Exception as exc:
            raise ValueError("Invalid DOM reference.") from exc
        if not isinstance(payload, dict) or payload.get("type") != expected_type:
            raise ValueError("Invalid DOM reference type.")
        return payload
