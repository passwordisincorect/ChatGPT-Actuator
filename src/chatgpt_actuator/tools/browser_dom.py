from __future__ import annotations

import asyncio
import base64
import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import psutil
from uuid import uuid4

from ..audit import AuditLogger
from ..config import BrowserDOMConfig


class BrowserDOMService:
    """Semantic browser DOM automation over a persistent Playwright + CDP session."""

    TOKEN_PREFIX = "dom1:"

    def __init__(self, config: BrowserDOMConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit
        self._playwright = None
        self._browser = None
        self._connect_lock: asyncio.Lock | None = None
        self._connected_endpoint: str | None = None
        self._stable_tabs: dict[str, object] = {}
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

    async def _ensure_browser(self):
        """Return one persistent remote browser connection for this service instance."""
        self._require_enabled()

        browser = self._browser
        if browser is not None:
            try:
                if browser.is_connected():
                    return browser
            except Exception:
                pass

        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()

        async with self._connect_lock:
            browser = self._browser
            if browser is not None:
                try:
                    if browser.is_connected():
                        return browser
                except Exception:
                    pass

            await self._disconnect_playwright()

            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Playwright is not installed. Run the ChatGPT-Actuator setup/install script again."
                ) from exc

            try:
                endpoint = self._resolved_cdp_url()
            except RuntimeError:
                if not (
                    self.config.cdp_url.strip().casefold() == "auto"
                    and self.config.launch_if_missing
                ):
                    raise
                await self._launch_dedicated_edge()
                endpoint = self._resolved_cdp_url()
            connect_timeout = max(1000, min(
                self.config.max_timeout_ms,
                max(self.config.default_timeout_ms, 15000),
            ))

            playwright = await async_playwright().start()
            try:
                browser = await self._connect_over_cdp_with_auto_allow(
                    playwright,
                    endpoint,
                    connect_timeout,
                )
            except Exception as exc:
                try:
                    await playwright.stop()
                except Exception:
                    pass

                # Normal Edge can expose DevToolsActivePort before showing the
                # native "Allow remote debugging?" consent. A failed CDP
                # connection is therefore the reliable point to approve that
                # exact dialog for the dedicated worker and retry once.
                recovered_after_allow = False
                allow_retry_exc: Exception | None = None
                if (
                    self.config.auto_allow_remote_debugging
                    and self.config.cdp_url.strip().casefold() == "auto"
                    and self.config.launch_if_missing
                ):
                    worker_pids = self._worker_edge_pids()
                    if self._auto_allow_remote_debugging_prompt(
                        worker_pids,
                        timeout=3.0,
                    ):
                        endpoint = self._resolved_cdp_url()
                        playwright = await async_playwright().start()
                        try:
                            browser = await self._connect_over_cdp_with_auto_allow(
                                playwright,
                                endpoint,
                                connect_timeout,
                            )
                            recovered_after_allow = True
                            self.audit.record(
                                "dom_browser_connected_after_auto_allow",
                                resolved_cdp_url=endpoint,
                            )
                        except Exception as retry_after_allow_exc:
                            allow_retry_exc = retry_after_allow_exc
                            try:
                                await playwright.stop()
                            except Exception:
                                pass

                if not recovered_after_allow:
                    effective_exc = allow_retry_exc or exc
                    message = str(effective_exc).casefold()
                    connection_failure = (
                        "connect" in message
                        or "econnrefused" in message
                        or "timeout" in message
                        or "websocket" in message
                    )
                    can_relaunch = (
                        connection_failure
                        and self.config.cdp_url.strip().casefold() == "auto"
                        and self.config.launch_if_missing
                    )
                    if can_relaunch:
                        await self._launch_dedicated_edge()
                        endpoint = self._resolved_cdp_url()
                        playwright = await async_playwright().start()
                        try:
                            browser = await self._connect_over_cdp_with_auto_allow(
                                playwright,
                                endpoint,
                                connect_timeout,
                            )
                        except Exception as retry_exc:
                            try:
                                await playwright.stop()
                            except Exception:
                                pass
                            raise RuntimeError(
                                "Cannot connect to the dedicated worker Edge CDP "
                                "after an automatic relaunch."
                            ) from retry_exc
                    elif connection_failure:
                        raise RuntimeError(
                            f"Cannot connect to Edge CDP using {self.config.cdp_url}. "
                            "Enable remote debugging for this Edge browser instance, then retry."
                        ) from effective_exc
                    else:
                        raise effective_exc

            self._playwright = playwright
            self._browser = browser
            self._connected_endpoint = endpoint
            browser.on("disconnected", self._on_browser_disconnected)
            self.audit.record(
                "dom_browser_connected",
                backend="playwright_cdp",
                configured_cdp_url=self.config.cdp_url,
                resolved_cdp_url=endpoint,
            )
            return browser

    def _on_browser_disconnected(self, _browser=None) -> None:
        self._browser = None
        self._connected_endpoint = None

    async def _disconnect_playwright(self) -> None:
        """Disconnect the Playwright client without closing the user's Edge browser."""
        playwright = self._playwright
        self._playwright = None
        self._browser = None
        self._connected_endpoint = None
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    async def disconnect(self) -> None:
        """Explicitly disconnect the automation client without closing Edge."""
        self._stop_worker_focus_guard()
        if self._connect_lock is None:
            await self._disconnect_playwright()
            return
        async with self._connect_lock:
            await self._disconnect_playwright()
        self.audit.record("dom_browser_disconnected")

    async def list_tabs(self) -> dict:
        self._require_read()
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
                }
            )

        self.audit.record("dom_list_tabs", returned=len(tabs))
        return {
            "backend": "playwright_cdp",
            "connection_mode": "persistent",
            "cdp_url": self.config.cdp_url,
            "resolved_cdp_url": self._connected_endpoint or self._resolved_cdp_url(),
            "user_data_dir": self.config.user_data_dir,
            "launch_if_missing": self.config.launch_if_missing,
            "background_mode": self.config.background_mode,
            "tabs": tabs,
            "count": len(tabs),
        }

    async def open_tab(
        self,
        url: str,
        timeout_ms: int | None = None,
    ) -> dict:
        self._require_mutation("allow_navigation", "DOM navigation")
        self._require_allowed_url(url)
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        if not browser.contexts:
            raise RuntimeError("Connected Edge browser has no available browser context.")

        page = await browser.contexts[0].new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            self._require_allowed_url(str(page.url or ""))
        except Exception:
            try:
                await page.close()
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
    ) -> dict:
        self._require_mutation("allow_navigation", "DOM navigation")
        self._require_allowed_url(url)
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        page = await self._resolve_tab(browser, tab_ref)
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
        }

    async def close_tab(self, tab_ref: str) -> dict:
        self._require_mutation("allow_navigation", "DOM tab close")
        browser = await self._ensure_browser()
        page = await self._resolve_tab(browser, tab_ref)
        stable_id = None
        try:
            token = self._decode(tab_ref, expected_type="tab")
            stable_id = token.get("id")
        except Exception:
            pass
        await page.close()
        if stable_id is not None:
            self._stable_tabs.pop(str(stable_id), None)
        else:
            for key, candidate in list(self._stable_tabs.items()):
                if candidate is page:
                    self._stable_tabs.pop(key, None)
        self.audit.record("dom_close_tab", tab_ref=tab_ref)
        return {"tab_ref": tab_ref, "closed": True}

    async def peek_tabs(self) -> dict:
        """Inspect the current worker browser without auto-launching it."""
        self._require_read()
        running_pids = self._worker_edge_pids()
        browser = self._browser
        connected = False
        if browser is not None:
            try:
                connected = bool(browser.is_connected())
            except Exception:
                connected = False

        tabs = []
        if connected:
            for page in self._pages(browser):
                url = str(page.url or "")
                if not self._is_allowed_url(url):
                    continue
                tabs.append(
                    {
                        "tab_ref": self._tab_ref_for_page(page),
                        "title": await self._safe_title(page),
                        "url": url,
                    }
                )

        return {
            "backend": "playwright_cdp",
            "running": bool(running_pids),
            "worker_edge_pids": running_pids,
            "connected": connected,
            "tabs": tabs,
            "count": len(tabs),
            "launch_if_missing": self.config.launch_if_missing,
            "background_mode": self.config.background_mode,
        }

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
        base, descriptor = self._base_locator(
            page,
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
            element_ref = self._encode(
                {
                    "type": "element",
                    "tab_ref": tab_ref,
                    "strategy": descriptor,
                    "index": index,
                }
            )
            try:
                meta = await self._element_metadata(locator)
                meta["element_ref"] = element_ref
                matches.append(meta)
            except Exception as exc:
                matches.append(
                    {
                        "element_ref": element_ref,
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

    async def details(self, element_ref: str) -> dict:
        self._require_read()
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        result = await self._element_metadata(locator, include_attributes=True)
        result["element_ref"] = element_ref

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

    async def click(self, element_ref: str, timeout_ms: int | None = None) -> dict:
        self._require_mutation("allow_click", "DOM click")
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        if self.config.background_safe_events:
            await locator.wait_for(state="attached", timeout=timeout)
            await locator.evaluate(
                """(el) => {
                    if (el.disabled) throw new Error('Element is disabled.');
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
        return {
            "element_ref": element_ref,
            "clicked": True,
            "click_mode": click_mode,
        }

    async def set_value(
        self,
        element_ref: str,
        value: str,
        timeout_ms: int | None = None,
    ) -> dict:
        self._require_mutation("allow_set_value", "DOM set value")
        if len(value) > self.config.max_text_chars:
            raise ValueError(
                f"value exceeds browser_dom.max_text_chars ({self.config.max_text_chars})."
            )
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)

        if self.config.background_safe_events:
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
    ) -> dict:
        self._require_mutation("allow_select", "DOM select")
        if (value is None) == (label is None):
            raise ValueError("Provide exactly one of value or label.")
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        selected = await locator.select_option(
            value=value if value is not None else None,
            label=label if label is not None else None,
            timeout=timeout,
        )
        self.audit.record(
            "dom_select",
            element_ref=element_ref,
            selection_kind="value" if value is not None else "label",
        )
        return {"element_ref": element_ref, "selected": selected}

    async def toggle(
        self,
        element_ref: str,
        checked: bool | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        self._require_mutation("allow_toggle", "DOM toggle")
        timeout = self._timeout(timeout_ms)
        browser = await self._ensure_browser()
        _page, locator = await self._resolve_element(browser, element_ref)
        before = bool(await locator.is_checked(timeout=timeout))
        target = (not before) if checked is None else bool(checked)
        if target:
            await locator.check(timeout=timeout)
        else:
            await locator.uncheck(timeout=timeout)
        after = bool(await locator.is_checked(timeout=timeout))
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

    async def _resolve_tab(self, browser, tab_ref: str):
        token = self._decode(tab_ref, expected_type="tab")
        stable_id = token.get("id")
        if stable_id is not None:
            page = self._stable_tabs.get(str(stable_id))
            if page is None:
                raise LookupError(
                    "The referenced Edge tab is no longer available. Call dom_list_tabs again."
                )
            try:
                if page.is_closed():
                    self._stable_tabs.pop(str(stable_id), None)
                    raise LookupError(
                        "The referenced Edge tab is no longer available. Call dom_list_tabs again."
                    )
            except LookupError:
                raise
            except Exception as exc:
                raise LookupError("The referenced Edge tab is no longer available.") from exc
            self._require_allowed_url(str(page.url or ""))
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
        descriptor = token.get("strategy")
        if not isinstance(descriptor, dict):
            raise ValueError("Invalid DOM element reference.")
        kind = str(descriptor.get("kind", ""))
        query = str(descriptor.get("query", ""))
        exact = bool(descriptor.get("exact", False))

        if kind == "selector":
            locator = page.locator(query)
        elif kind == "text":
            locator = page.get_by_text(query, exact=exact)
        elif kind == "role":
            kwargs = {"exact": exact}
            if descriptor.get("name") is not None:
                kwargs["name"] = str(descriptor["name"])
            locator = page.get_by_role(query, **kwargs)
        else:
            raise ValueError("Unsupported DOM element reference strategy.")

        index = int(token.get("index", 0))
        return page, locator.nth(index)

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
        user_dir = self._dedicated_user_data_dir()
        user_dir.mkdir(parents=True, exist_ok=True)
        active_port = user_dir / "DevToolsActivePort"

        # Never create another Edge window for the same dedicated worker
        # profile. Repeated connection probes can arrive while an existing
        # worker is disconnected from CDP; launching with --new-window in that
        # state would accumulate orphan about:blank windows.
        existing_pids = self._worker_edge_pids()
        if existing_pids:
            self._auto_allow_remote_debugging_prompt(existing_pids, timeout=1.5)
            endpoint_ready = False
            try:
                endpoint_ready = bool(
                    active_port.is_file()
                    and active_port.read_text(encoding="utf-8").strip()
                )
            except OSError:
                endpoint_ready = False

            self.audit.record(
                "dom_worker_edge_launch_reused_or_blocked",
                worker_edge_pids=existing_pids,
                user_data_dir=str(user_dir),
                endpoint_ready=endpoint_ready,
            )
            if endpoint_ready:
                return
            raise RuntimeError(
                "Dedicated Worker Edge is already running for this profile, "
                "but its remote-debugging endpoint is unavailable. Duplicate "
                "launch blocked to prevent extra about:blank windows. Close or "
                "restart the dedicated Worker Edge, or enable remote debugging "
                "for that profile, then retry."
            )

        edge = Path(os.path.expandvars(self.config.edge_executable))
        if not edge.is_file():
            raise RuntimeError(f"Edge executable not found: {edge}")

        try:
            if active_port.is_file():
                active_port.unlink()
        except OSError:
            pass

        args = [
            str(edge),
            f"--user-data-dir={user_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--new-window",
        ]
        startup_offscreen = (
            self.config.background_mode == "offscreen"
            or self.config.visibility_guard_enabled
        )
        if startup_offscreen:
            # Startup is intentionally offscreen. For minimized mode the
            # normal restore bounds are applied only after CDP is connected.
            args.extend([
                "--window-position=-32000,-32000",
                "--window-size=1280,900",
            ])
            if self.config.background_mode == "minimized":
                args.append("--start-minimized")
        else:
            args.extend([
                "--window-position=80,80",
                "--window-size=1280,900",
                "--start-minimized",
            ])
        args.append("about:blank")

        creationflags = 0
        creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        creationflags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
        startupinfo = None
        startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_cls is not None:
            startupinfo = startupinfo_cls()
            startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
            # During guarded startup prefer a non-activating offscreen show.
            startupinfo.wShowWindow = 4 if startup_offscreen else 7
        previous_foreground = self._foreground_window_handle()
        if previous_foreground:
            self._worker_restore_foreground = int(previous_foreground)

        # Install WinEvent hooks before CreateProcess so the first top-level
        # Worker window is caught as early as Windows exposes it.
        self._start_worker_focus_guard(previous_foreground, ())
        process = subprocess.Popen(
            args,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        self._edge_process = process
        self._worker_focus_guard_pids.add(int(process.pid))
        self.audit.record(
            "dom_worker_edge_started",
            pid=int(process.pid),
            user_data_dir=str(user_dir),
            background_mode=self.config.background_mode,
            launch_mode="normal_edge_autoconnect",
        )

        # The worker is launched as a normal browser. Remote debugging is
        # enabled from edge://inspect and persists for this dedicated profile.
        # Hide/minimize the worker immediately so startup never steals focus.
        self._enforce_worker_background_window(
            int(process.pid),
            previous_foreground,
        )
        self._auto_allow_remote_debugging_prompt(int(process.pid), timeout=1.5)

        # Normal Edge may surface the remote-debugging consent dialog only
        # after ~20 seconds. Keep this below the broker's 40-second request
        # ceiling, but long enough for UIA auto-approval to observe it.
        deadline = time.monotonic() + max(
            5.0,
            min(28.0, self.config.max_timeout_ms / 1000.0),
        )
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._stop_worker_focus_guard()
                raise RuntimeError(
                    f"Dedicated worker Edge exited early with code {process.returncode}."
                )
            self._auto_allow_remote_debugging_prompt(int(process.pid))
            if active_port.is_file():
                try:
                    if active_port.read_text(encoding="utf-8").strip():
                        self._auto_allow_remote_debugging_prompt(int(process.pid))
                        return
                except OSError:
                    pass
            await asyncio.sleep(0.1)

        self._stop_worker_focus_guard()
        raise TimeoutError(
            "Timed out waiting for the dedicated worker Edge auto-connect endpoint. "
            "Open edge://inspect/#remote-debugging once in this worker profile and "
            "enable 'Allow remote debugging for this browser instance', then retry. "
            f"Profile: {user_dir}"
        )

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
        if os.name != "nt" or not hwnd:
            return False
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(hwnd)
        if not user32.IsWindow(ctypes.c_void_p(hwnd)):
            return False

        current_thread = int(kernel32.GetCurrentThreadId() or 0)
        foreground = int(user32.GetForegroundWindow() or 0)
        fg_pid = ctypes.c_ulong()
        target_pid = ctypes.c_ulong()
        foreground_thread = (
            int(
                user32.GetWindowThreadProcessId(
                    ctypes.c_void_p(foreground),
                    ctypes.byref(fg_pid),
                )
                or 0
            )
            if foreground
            else 0
        )
        target_thread = int(
            user32.GetWindowThreadProcessId(
                ctypes.c_void_p(hwnd),
                ctypes.byref(target_pid),
            )
            or 0
        )

        attached_current = False
        attached_foreground = False
        try:
            if (
                current_thread
                and target_thread
                and current_thread != target_thread
            ):
                attached_current = bool(
                    user32.AttachThreadInput(
                        current_thread,
                        target_thread,
                        True,
                    )
                )
            if (
                foreground_thread
                and target_thread
                and foreground_thread != target_thread
            ):
                attached_foreground = bool(
                    user32.AttachThreadInput(
                        foreground_thread,
                        target_thread,
                        True,
                    )
                )
            user32.BringWindowToTop(ctypes.c_void_p(hwnd))
            user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        finally:
            if attached_foreground:
                user32.AttachThreadInput(
                    foreground_thread,
                    target_thread,
                    False,
                )
            if attached_current:
                user32.AttachThreadInput(
                    current_thread,
                    target_thread,
                    False,
                )
        focused = int(user32.GetForegroundWindow() or 0) == hwnd
        if not focused:
            try:
                switch_to = getattr(user32, "SwitchToThisWindow")
                switch_to(ctypes.c_void_p(hwnd), True)
                time.sleep(0.02)
            except Exception:
                pass
            focused = int(user32.GetForegroundWindow() or 0) == hwnd
        return focused

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
            return str(await page.title() or "")
        except Exception:
            return ""

    def _timeout(self, timeout_ms: int | None) -> int:
        if timeout_ms is None:
            return self.config.default_timeout_ms
        return min(max(1, int(timeout_ms)), self.config.max_timeout_ms)

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("Browser DOM automation is disabled.")

    def _require_read(self) -> None:
        self._require_enabled()
        if not self.config.allow_read:
            raise PermissionError("Browser DOM read access is disabled.")

    def _require_mutation(self, flag: str, label: str) -> None:
        self._require_enabled()
        if not bool(getattr(self.config, flag)):
            raise PermissionError(f"{label} is disabled by configuration.")

    def _is_allowed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").casefold()
        if not host:
            return False
        allowed = tuple(x.casefold().strip() for x in self.config.allowed_hosts if x.strip())
        if "*" in allowed:
            return True
        return any(host == item or host.endswith("." + item) for item in allowed)

    def _require_allowed_url(self, url: str) -> None:
        if not self._is_allowed_url(url):
            raise PermissionError(
                f"Browser DOM access to this URL is not allowed by browser_dom.allowed_hosts: {url}"
            )

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
