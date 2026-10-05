from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import BrowserDOMConfig
from chatgpt_actuator.tools.browser_dom import BrowserDOMService


class BrowserDOMServiceTests(unittest.TestCase):
    def make_service(
        self,
        *,
        enabled: bool = True,
        allow_read: bool = True,
        allow_click: bool = True,
        allow_set_value: bool = True,
        allow_select: bool = True,
        allow_toggle: bool = True,
        allow_scroll: bool = True,
        allow_navigation: bool = True,
        allowed_hosts: tuple[str, ...] = ("github.com", "chatgpt.com"),
    ) -> BrowserDOMService:
        return BrowserDOMService(
            BrowserDOMConfig(
                enabled=enabled,
                allow_read=allow_read,
                allow_click=allow_click,
                allow_set_value=allow_set_value,
                allow_select=allow_select,
                allow_toggle=allow_toggle,
                allow_scroll=allow_scroll,
                allow_navigation=allow_navigation,
                cdp_url="http://127.0.0.1:9222",
                user_data_dir=r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
                allowed_hosts=allowed_hosts,
                max_items=100,
                max_text_chars=1000,
                default_timeout_ms=5000,
                max_timeout_ms=30000,
                max_scroll_pixels=10000,
            ),
            AuditLogger(False, "unused.log"),
        )

    def test_reference_round_trip(self) -> None:
        payload = {
            "type": "tab",
            "url": "https://github.com/example/repo",
            "title": "Example",
            "ordinal": 0,
        }
        token = BrowserDOMService._encode(payload)
        self.assertTrue(token.startswith("dom1:"))
        self.assertEqual(
            BrowserDOMService._decode(token, expected_type="tab"),
            payload,
        )

    def test_reference_type_is_checked(self) -> None:
        token = BrowserDOMService._encode({"type": "tab"})
        with self.assertRaises(ValueError):
            BrowserDOMService._decode(token, expected_type="element")

    def test_allowed_host_includes_subdomains(self) -> None:
        service = self.make_service()
        self.assertTrue(service._is_allowed_url("https://github.com/openai"))
        self.assertTrue(service._is_allowed_url("https://docs.github.com/test"))
        self.assertFalse(service._is_allowed_url("https://example.com/"))
        self.assertFalse(service._is_allowed_url("edge://settings/"))

    def test_wildcard_host(self) -> None:
        service = self.make_service(allowed_hosts=("*",))
        self.assertTrue(service._is_allowed_url("https://example.com/"))
        self.assertFalse(service._is_allowed_url("file:///C:/secret.txt"))

    def test_auto_cdp_discovers_devtools_active_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "DevToolsActivePort").write_text(
                "9333\n/devtools/browser/test\n",
                encoding="utf-8",
            )
            service = self.make_service()
            service.config = BrowserDOMConfig(
                **{
                    **service.config.__dict__,
                    "cdp_url": "auto",
                    "user_data_dir": tmp,
                }
            )
            self.assertEqual(
                service._resolved_cdp_url(),
                "ws://127.0.0.1:9333/devtools/browser/test",
            )

    def test_auto_cdp_falls_back_to_http_without_browser_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "DevToolsActivePort").write_text("9444\n", encoding="utf-8")
            service = self.make_service()
            service.config = BrowserDOMConfig(
                **{
                    **service.config.__dict__,
                    "cdp_url": "auto",
                    "user_data_dir": tmp,
                }
            )
            self.assertEqual(service._resolved_cdp_url(), "http://127.0.0.1:9444")

    def test_dedicated_profile_guard_rejects_main_edge(self) -> None:
        service = self.make_service()
        with self.assertRaises(RuntimeError):
            service._dedicated_user_data_dir()

    def test_launch_dedicated_edge_blocks_duplicate_existing_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service()
            service.config = BrowserDOMConfig(
                **{
                    **service.config.__dict__,
                    "cdp_url": "auto",
                    "user_data_dir": tmp,
                    "launch_if_missing": True,
                }
            )
            service._worker_edge_pids = lambda: [4242]
            with self.assertRaisesRegex(RuntimeError, "Duplicate launch blocked"):
                asyncio.run(service._launch_dedicated_edge())

    def test_launch_dedicated_edge_reuses_existing_ready_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active_port = Path(tmp, "DevToolsActivePort")
            active_port.write_text(
                "9333\n/devtools/browser/existing\n",
                encoding="utf-8",
            )
            service = self.make_service()
            service.config = BrowserDOMConfig(
                **{
                    **service.config.__dict__,
                    "cdp_url": "auto",
                    "user_data_dir": tmp,
                    "launch_if_missing": True,
                }
            )
            service._worker_edge_pids = lambda: [4242]
            asyncio.run(service._launch_dedicated_edge())
            self.assertTrue(active_port.is_file())

    def test_timeout_is_clamped(self) -> None:
        service = self.make_service()
        self.assertEqual(service._timeout(None), 5000)
        self.assertEqual(service._timeout(0), 1)
        self.assertEqual(service._timeout(999999), 30000)

    def test_read_permission_gate_runs_before_browser_connection(self) -> None:
        service = self.make_service(allow_read=False)
        with self.assertRaises(PermissionError):
            asyncio.run(service.list_tabs())

    def test_mutation_permission_gate_runs_before_browser_connection(self) -> None:
        service = self.make_service(allow_click=False)
        with self.assertRaises(PermissionError):
            asyncio.run(service.click("dom1:invalid"))

    def test_value_length_is_checked_before_browser_connection(self) -> None:
        service = self.make_service()
        with self.assertRaises(ValueError):
            asyncio.run(service.set_value("dom1:invalid", "x" * 1001))

    def test_persistent_browser_connection_is_reused(self) -> None:
        service = self.make_service()

        class FakeBrowser:
            def is_connected(self):
                return True

        browser = FakeBrowser()
        service._browser = browser

        async def check():
            first = await service._ensure_browser()
            second = await service._ensure_browser()
            self.assertIs(first, browser)
            self.assertIs(second, browser)

        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
