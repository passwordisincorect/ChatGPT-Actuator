from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import patch

import chatgpt_actuator
from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import BrowserDOMConfig
from chatgpt_actuator.tool_policy import Principal, principal_context
from chatgpt_actuator.tools.browser_dom import BrowserDOMService


class FakeCDPSession:
    def __init__(self, page):
        self.page = page

    async def send(self, method):
        assert method == "Target.getTargetInfo"
        return {"targetInfo": {"targetId": self.page.target_id}}

    async def detach(self):
        return None


class FakePage:
    def __init__(self, context, url, target_id):
        self.context, self.url, self.target_id = context, url, target_id
        self.events = {}
        self.closed = False

    def is_closed(self):
        return self.closed

    def on(self, name, callback):
        self.events.setdefault(name, []).append(callback)

    def remove_listener(self, name, callback):
        if callback in self.events.get(name, []):
            self.events[name].remove(callback)

    async def close(self):
        self.closed = True

    async def title(self):
        return self.target_id


class FakeContext:
    def __init__(self):
        self.pages = []

    async def new_cdp_session(self, page):
        return FakeCDPSession(page)


class FakeBrowser:
    def __init__(self, pages):
        context = FakeContext()
        context.pages = [FakePage(context, url, target) for url, target in pages]
        self.contexts = [context]
        self.connected = True

    def is_connected(self):
        return self.connected

    def close_driver_transport(self):
        stopped = types.SimpleNamespace(done=lambda: True)
        process = types.SimpleNamespace(returncode=0)
        transport = types.SimpleNamespace(_stopped=True, _stopped_future=stopped,
                                          _proc=process)
        connection = types.SimpleNamespace(_closed_error=RuntimeError("TargetClosedError"),
                                           _transport=transport)
        self._impl_obj = types.SimpleNamespace(_connection=connection)


class FakeDriver:
    def __init__(self):
        self.stopped = False
        self.fail_stops = 0

    async def stop(self):
        if self.fail_stops:
            self.fail_stops -= 1
            raise RuntimeError("simulated stop failure")
        self.stopped = True


class AutoConnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = BrowserDOMConfig(
            enabled=True, allow_read=True, allow_click=True, allow_set_value=True,
            allow_select=True, allow_toggle=True, allow_scroll=True,
            allow_navigation=True, cdp_url="http://127.0.0.1:9222",
            user_data_dir="unused", allowed_hosts=("github.com",), max_items=100,
            max_text_chars=1000, default_timeout_ms=5000, max_timeout_ms=30000,
            max_scroll_pixels=10000,
        )
        self.service = BrowserDOMService(config, AuditLogger(False, "unused.log"))
        self.service.config = types.SimpleNamespace(
            enabled=True, allow_navigation=True, visible_browser=True,
            allowed_hosts=("github.com",), max_timeout_ms=30000,
        )
        self.service.resource_limits = {"max_browser_contexts": 4}
        self.principal_token = principal_context.set(
            Principal(owner="alice", session="work-1")
        )
        self.drivers = []
        self.browsers = []
        self.prepare_calls = []
        self.discovery = {"owner": "alice", "pid": 11, "started": 1.0,
                          "exe": "msedge.exe", "user_data_dir": "edge-user-data",
                          "profile_directory": "Default",
                          "endpoint": "ws://127.0.0.1:9222/devtools/browser/test"}

        async def prepare(policy, owner, limits):
            self.prepare_calls.append((policy, owner))
            return {"endpoint": "ws://127.0.0.1:9222/devtools/browser/test",
                    "pid": 11, "started": 1.0, "exe": "msedge.exe",
                    "user_data_dir": "edge-user-data", "profile_directory": "Default",
                    "debugging_enabled_by_tool": False}

        def validate(binding, policy, owner):
            return dict(binding)

        def discover(policy, owner):
            return dict(self.discovery)

        async def connect(driver, binding, limits):
            browser = FakeBrowser([
                ("https://github.com/one", "target-1"),
                ("https://outside.example/secret", "hidden"),
            ])
            self.browsers.append(browser)
            return browser, True

        self.auto_module = types.ModuleType("chatgpt_actuator.dom_auto_connect")
        self.auto_module.prepare = prepare
        self.auto_module.validate = validate
        self.auto_module.discover = discover
        self.auto_module.connect = connect
        self.mock_connect = connect

        async def start():
            driver = FakeDriver()
            self.drivers.append(driver)
            return driver

        api = types.ModuleType("playwright.async_api")
        api.async_playwright = lambda: types.SimpleNamespace(start=start)
        package = types.ModuleType("playwright")
        package.async_api = api
        self.modules = patch.dict(sys.modules, {
            "chatgpt_actuator.dom_auto_connect": self.auto_module,
            "playwright": package,
            "playwright.async_api": api,
        })
        self.modules.start()
        self.package_patch = patch.object(
            chatgpt_actuator, "dom_auto_connect", self.auto_module, create=True
        )
        self.package_patch.start()

    def tearDown(self):
        self.package_patch.stop()
        self.modules.stop()
        principal_context.reset(self.principal_token)

    async def attach(self, *args):
        return await self.service.auto_attach_browser({"enabled": True}, *args)

    async def test_pending_connection_reused_then_selected(self):
        pending = await self.attach()
        self.assertFalse(pending["attached"])
        self.assertTrue(pending["selection_required"])
        self.assertEqual([row["url"] for row in pending["candidates"]],
                         ["https://github.com/one"])
        attached = await self.attach("https://github.com/one")
        self.assertTrue(attached["attached"])
        self.assertEqual(attached["mode"], "selected_cdp_tab")
        self.assertEqual(len(self.prepare_calls), 1)
        self.assertEqual(len(self.drivers), 1)
        self.assertFalse(self.drivers[0].stopped)

    async def test_foreign_profile_is_rejected_before_bootstrap(self):
        self.discovery["endpoint"] = "ws://127.0.0.1:9555/devtools/browser/new"
        self.service._pending_auto_sessions[("bob", "work-2")] = {
            "endpoint": "ws://127.0.0.1:9333/devtools/browser/foreign",
            "binding": {"pid": 900, "user_data_dir": "edge-user-data",
                        "profile_directory": "Default"},
        }
        with self.assertRaisesRegex(PermissionError, "already leased"):
            await self.attach("https://github.com/one")
        self.assertEqual(self.prepare_calls, [])
        self.assertEqual(self.drivers, [])

    async def test_resolved_nondefault_port_is_rejected_before_bootstrap(self):
        self.discovery["endpoint"] = "ws://127.0.0.1:9555/devtools/browser/new"
        self.service._external_sessions[("bob", "work-2")] = {
            "endpoint": "http://127.0.0.1:9555",
        }
        with self.assertRaisesRegex(PermissionError, "already leased"):
            await self.attach("https://github.com/one")
        self.assertEqual(self.prepare_calls, [])
        self.assertEqual(self.drivers, [])

    async def test_foreign_owner_cannot_reuse_pending_port(self):
        await self.attach()
        token = principal_context.set(Principal(owner="bob", session="work-2"))
        try:
            with self.assertRaisesRegex(PermissionError, "already leased"):
                await self.attach("https://github.com/one")
        finally:
            principal_context.reset(token)
        self.assertEqual(len(self.prepare_calls), 1)

    async def test_ambiguous_selection_keeps_pending_lease(self):
        await self.attach()
        browser = self.browsers[0]
        context = browser.contexts[0]
        context.pages.append(FakePage(context, "https://github.com/one", "target-2"))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await self.attach("https://github.com/one")
        self.assertIn(("alice", "work-1"), self.service._pending_auto_sessions)
        self.assertFalse(self.drivers[0].stopped)

    async def test_disconnect_retires_lease_and_reconnects_without_replay(self):
        await self.attach()
        await self.attach("https://github.com/one")
        self.browsers[0].connected = False
        result = await self.attach("https://github.com/one")
        self.assertTrue(result["attached"])
        self.assertEqual(len(self.prepare_calls), 2)
        self.assertTrue(self.drivers[0].stopped)
        self.assertFalse(self.drivers[1].stopped)

    async def test_disconnected_pending_transport_reconnects_fresh(self):
        await self.attach()
        self.browsers[0].connected = False
        result = await self.attach("https://github.com/one")
        self.assertTrue(result["attached"])
        self.assertEqual(len(self.prepare_calls), 2)
        self.assertTrue(self.drivers[0].stopped)
        self.assertFalse(self.drivers[1].stopped)

    async def test_closed_driver_transport_reconnects_pending_lease(self):
        await self.attach()
        await self.drivers[0].stop()
        self.browsers[0].close_driver_transport()
        # The public browser flag deliberately remains stale and says connected.
        self.assertTrue(self.browsers[0].is_connected())
        result = await self.attach("https://github.com/one")
        self.assertTrue(result["attached"])
        self.assertEqual(len(self.prepare_calls), 2)
        self.assertTrue(self.drivers[0].stopped)
        self.assertFalse(self.drivers[1].stopped)

    async def test_closed_driver_transport_reconnects_selected_lease(self):
        await self.attach()
        await self.attach("https://github.com/one")
        await self.drivers[0].stop()
        self.browsers[0].close_driver_transport()
        self.assertTrue(self.browsers[0].is_connected())
        result = await self.attach("https://github.com/one")
        self.assertTrue(result["attached"])
        self.assertEqual(len(self.prepare_calls), 2)
        self.assertFalse(self.drivers[1].stopped)

    async def test_selected_page_reuse_keeps_context_and_popup_listener(self):
        await self.attach()
        first = await self.attach("https://github.com/one")
        context = self.service._external_sessions[("alice", "work-1")]["context"]
        page = context.selected
        popup_handlers = len(page.events["popup"])
        second = await self.attach("https://github.com/one")
        self.assertIs(self.service._session_contexts[("alice", "work-1")], context)
        self.assertIs(self.service._external_sessions[("alice", "work-1")]["context"], context)
        self.assertEqual(len(page.events["popup"]), popup_handlers)
        self.assertEqual(first["tab_ref"], second["tab_ref"])

    async def test_auto_binding_reports_verified_profile_without_child_target(self):
        await self.attach()
        result = await self.attach("https://github.com/one")
        lease = self.service._external_sessions[("alice", "work-1")]
        selected_binding = self.service._binding(lease["context"].selected,
                                                 ("alice", "work-1"))
        self.assertTrue(selected_binding["profile_verified"])
        self.assertEqual(selected_binding["profile_persistence"], "current_edge_profile")
        self.assertEqual(selected_binding["target_id"], lease["target_id"])
        self.assertEqual(result["binding"]["target_id"], lease["target_id"])
        child = FakePage(lease["context"].context, "https://github.com/child", "child-target")
        child_binding = self.service._binding(child, ("alice", "work-1"))
        self.assertTrue(child_binding["profile_verified"])
        self.assertEqual(child_binding["ownership"], "owned_child_tab")
        self.assertIsNone(child_binding["target_id"])

    async def test_retarget_preserves_pages_children_and_retires_old_ref(self):
        await self.attach()
        first = await self.attach("https://github.com/one")
        lease = self.service._external_sessions[("alice", "work-1")]
        context = lease["context"]
        old_page = context.selected
        child = FakePage(old_page.context, "https://github.com/child", "child-target")
        context.owned.append(child)
        new_page = FakePage(old_page.context, "https://github.com/two", "target-2")
        old_page.context.pages.append(new_page)
        second = await self.attach("https://github.com/two")
        self.assertNotEqual(first["tab_ref"], second["tab_ref"])
        self.assertIs(lease["context"], context)
        self.assertIs(self.service._session_contexts[("alice", "work-1")], context)
        self.assertIs(context.selected, new_page)
        self.assertIn(old_page, context.retained)
        self.assertIn(child, context.owned)
        self.assertIn(child, context.pages)
        self.assertFalse(old_page.is_closed())
        old_ident = self.service._decode(first["tab_ref"], "tab")["id"]
        self.assertNotIn(old_ident, self.service._stable_tabs)
        self.assertEqual(old_page.events.get("popup", []), [])

    async def test_retired_original_popup_is_not_claimed_as_owned(self):
        await self.attach()
        await self.attach("https://github.com/one")
        lease = self.service._external_sessions[("alice", "work-1")]
        context = lease["context"]
        old_page = context.selected
        new_page = FakePage(old_page.context, "https://github.com/two", "target-2")
        old_page.context.pages.append(new_page)
        await self.attach("https://github.com/two")
        popup = FakePage(old_page.context, "https://github.com/later-popup", "popup")
        for handler in list(old_page.events.get("popup", [])):
            handler(popup)
        self.assertNotIn(popup, context.owned)
        self.assertNotIn(popup, context.pages)

    async def test_selecting_owned_child_keeps_ownership_and_release_cleanup(self):
        await self.attach()
        await self.attach("https://github.com/one")
        lease = self.service._external_sessions[("alice", "work-1")]
        context = lease["context"]
        child = FakePage(context.context, "https://github.com/child", "child")
        context.owned.append(child)
        context.context.pages.append(child)
        context.register(child)
        context._watch(child)
        await self.attach("https://github.com/child")
        self.assertIn(child, context.owned)
        self.assertEqual(context.pages.count(child), 1)
        await self.service.release_session("alice", "work-1")
        self.assertTrue(child.is_closed())
        self.assertFalse(context.retained[0].is_closed())
        self.assertTrue(all(not page.events.get("popup") for page in [context.retained[0], child]))

    async def test_release_cleans_pending_transport(self):
        await self.attach()
        result = await self.service.release_session("alice", "work-1")
        self.assertTrue(result["released"])
        self.assertTrue(self.drivers[0].stopped)
        self.assertNotIn(("alice", "work-1"), self.service._pending_auto_sessions)

    async def test_release_stop_failure_retains_retryable_pending_transport(self):
        await self.attach()
        self.drivers[0].fail_stops = 1
        first = await self.service.release_session("alice", "work-1")
        self.assertEqual(first["resource_cleanup"], "pending")
        self.assertIn(("alice", "work-1"), self.service._pending_auto_sessions)
        second = await self.service.release_session("alice", "work-1")
        self.assertEqual(second["resource_cleanup"], "completed")
        self.assertNotIn(("alice", "work-1"), self.service._pending_auto_sessions)

    async def test_idle_expiry_stops_client_without_closing_edge(self):
        await self.attach()
        self.service._pending_auto_sessions[("alice", "work-1")]["last_activity"] = 0
        result = await self.service.expire_pending_auto_connections(idle_seconds=0)
        self.assertEqual(len(result["expired"]), 1)
        self.assertTrue(self.drivers[0].stopped)
        self.assertFalse(hasattr(self.browsers[0], "close"))
        self.assertNotIn(("alice", "work-1"), self.service._pending_auto_sessions)

    async def test_idle_expiry_retains_lease_when_driver_stop_fails(self):
        await self.attach()
        self.service._pending_auto_sessions[("alice", "work-1")]["last_activity"] = 0
        self.drivers[0].fail_stops = 1
        first = await self.service.expire_pending_auto_connections(idle_seconds=0)
        self.assertEqual(first["expired"][0]["resource_cleanup"], "pending")
        self.assertIn(("alice", "work-1"), self.service._pending_auto_sessions)
        second = await self.service.expire_pending_auto_connections(idle_seconds=0)
        self.assertEqual(second["expired"][0]["resource_cleanup"], "completed")
        self.assertNotIn(("alice", "work-1"), self.service._pending_auto_sessions)

    async def test_cancelled_candidate_read_cleans_pending_client(self):
        async def cancel_candidates(lease, selected=False):
            raise asyncio.CancelledError

        self.service._auto_candidates = cancel_candidates
        with self.assertRaises(asyncio.CancelledError):
            await self.attach()
        self.assertTrue(self.drivers[0].stopped)
        self.assertNotIn(("alice", "work-1"), self.service._pending_auto_sessions)
        self.assertFalse(hasattr(self.browsers[0], "close"))

    async def test_cancelled_candidate_stop_failure_retains_cleanup_lease(self):
        async def cancel_candidates(lease, selected=False):
            raise asyncio.CancelledError

        self.service._auto_candidates = cancel_candidates
        self.drivers.clear()
        # Fail the owned-client stop after the connect path has created it.
        original_start = self.auto_module.connect

        async def connect_with_stop_failure(driver, binding, limits):
            browser, handled = await original_start(driver, binding, limits)
            driver.fail_stops = 1
            return browser, handled

        self.auto_module.connect = connect_with_stop_failure
        with self.assertRaises(asyncio.CancelledError):
            await self.attach()
        lease = self.service._pending_auto_sessions[("alice", "work-1")]
        self.assertTrue(lease["cleanup_only"])
        self.assertFalse(self.drivers[0].stopped)
        result = await self.service.expire_pending_auto_connections(idle_seconds=0)
        self.assertEqual(result["expired"][0]["resource_cleanup"], "completed")
        self.assertTrue(self.drivers[0].stopped)

    async def test_connect_error_stop_failure_retains_cleanup_lease(self):
        async def fail_connect(driver, binding, limits):
            driver.fail_stops = 1
            raise RuntimeError("simulated connect failure")

        self.auto_module.connect = fail_connect
        with self.assertRaisesRegex(RuntimeError, "connect failure"):
            await self.attach()
        lease = self.service._pending_auto_sessions[("alice", "work-1")]
        self.assertTrue(lease["cleanup_only"])
        self.assertFalse(self.drivers[0].stopped)
        self.auto_module.connect = self.mock_connect
        await self.attach()
        self.assertTrue(self.drivers[0].stopped)
        self.assertEqual(len(self.prepare_calls), 2)
        self.assertEqual(len(self.drivers), 2)

    async def test_missing_local_grant_does_not_start_driver(self):
        def denied(policy, owner):
            raise PermissionError("browser_auto_connect grant required")

        self.auto_module.discover = denied
        with self.assertRaisesRegex(PermissionError, "grant required"):
            await self.attach()
        self.assertEqual(self.prepare_calls, [])
        self.assertEqual(self.drivers, [])


if __name__ == "__main__":
    unittest.main()
