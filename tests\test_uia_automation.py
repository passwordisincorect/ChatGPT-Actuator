from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
