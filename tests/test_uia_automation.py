from __future__ import annotations

import unittest
from unittest.mock import patch

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import UIAutomationConfig
from chatgpt_actuator.tools import UIAutomationService


class UIAutomationTests(unittest.TestCase):
    def make_service(self) -> UIAutomationService:
        return UIAutomationService(
            UIAutomationConfig(
                enabled=True,
                allow_focus=True,
                allow_invoke=True,
                allow_set_value=True,
                allow_toggle=True,
                allow_select=True,
                allow_expand_collapse=True,
                allow_scroll=True,
                allow_scroll_into_view=True,
                allow_range_value=True,
                allow_text_selection=True,
                allow_window_action=True,
                max_depth=10,
                max_items=500,
                max_value_chars=1000000,
                verify_wait_ms=500,
            ),
            AuditLogger(False, "unused.log"),
        )

    def test_element_ref_roundtrip(self):
        service = self.make_service()
        ref = service._make_ref(12345, (42, 100, 4, -2))
        hwnd, runtime_id = service._parse_ref(ref)
        self.assertEqual(hwnd, 12345)
        self.assertEqual(runtime_id, (42, 100, 4, -2))

    def test_invalid_element_ref(self):
        service = self.make_service()
        for value in ("", "bad", "uia:abc:42,1", "uia:123:"):
            with self.assertRaises(ValueError):
                service._parse_ref(value)

    def test_limits(self):
        service = self.make_service()
        self.assertEqual(service._depth_limit(None), 10)
        self.assertEqual(service._depth_limit(99), 10)
        self.assertEqual(service._item_limit(9999), 500)
        with self.assertRaises(ValueError):
            service._depth_limit(-1)
        with self.assertRaises(ValueError):
            service._item_limit(0)

    def test_find_requires_selector_before_window_access(self):
        service = self.make_service()
        with self.assertRaises(ValueError):
            service.find_elements(12345)

    def test_scroll_validates_before_window_access(self):
        service = self.make_service()
        with self.assertRaises(ValueError):
            service.scroll(12345, "uia:12345:42,1", "diagonal")
        with self.assertRaises(ValueError):
            service.scroll(12345, "uia:12345:42,1", "down", count=0)

    def test_range_rejects_non_finite_before_window_access(self):
        service = self.make_service()
        with self.assertRaises(ValueError):
            service.set_range_value(12345, "uia:12345:42,1", float("nan"))

    def test_target_window_guard_rejects_foreign_chromium_runtime_hwnd(self):
        service = self.make_service()

        class Info:
            handle = None
            runtime_id = (42, 22222, 4, 1)

        class Element:
            element_info = Info()

        with patch("chatgpt_actuator.tools.ui_automation.win32gui.IsWindow", return_value=True), patch(
            "chatgpt_actuator.tools.ui_automation.win32gui.GetAncestor", return_value=33333
        ):
            self.assertFalse(service._belongs_to_target_window(11111, Element()))

    def test_target_window_guard_accepts_descendant_hwnd(self):
        service = self.make_service()

        class Info:
            handle = 22222
            runtime_id = (42, 22222, 4, 1)

        class Element:
            element_info = Info()

        with patch("chatgpt_actuator.tools.ui_automation.win32gui.IsWindow", return_value=True), patch(
            "chatgpt_actuator.tools.ui_automation.win32gui.GetAncestor", return_value=11111
        ):
            self.assertTrue(service._belongs_to_target_window(11111, Element()))

    def test_walk_prunes_foreign_subtree(self):
        service = self.make_service()

        class Element:
            def __init__(self, name, children=None):
                self.name = name
                self._children = children or []

            def children(self):
                return self._children

        foreign_leaf = Element("foreign-leaf")
        foreign = Element("foreign", [foreign_leaf])
        local = Element("local")
        root = Element("root", [foreign, local])

        with patch.object(
            service,
            "_belongs_to_target_window",
            side_effect=lambda _hwnd, element: element.name != "foreign",
        ):
            walked = [element.name for _depth, element in service._walk(root, 3, target_hwnd=11111)]

        self.assertEqual(walked, ["root", "local"])

if __name__ == "__main__":
    unittest.main()
