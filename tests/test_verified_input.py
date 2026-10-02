from __future__ import annotations

import unittest
from unittest.mock import patch

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import VerifiedInputConfig
from chatgpt_actuator.tools.ui_text import TextControlState, VerifiedInputService


class VerifiedInputTests(unittest.TestCase):
    def make_service(self) -> VerifiedInputService:
        return VerifiedInputService(
            VerifiedInputConfig(
                enabled=True,
                focus_text_control=True,
                verify_keyboard_write=True,
                repair_full_selection_failure=True,
                rollback_partial_failure=True,
                verify_wait_ms=0,
                max_read_chars=1000000,
            ),
            AuditLogger(False, "unused.log"),
        )

    @staticmethod
    def state(text: str, selected_text: str = "", full_selection: bool = False) -> TextControlState:
        return TextControlState(
            target_hwnd=123,
            control_type="Document",
            class_name="RichEditD2DPT",
            name="Text editor",
            native_handle=456,
            text=text,
            selected_text=selected_text,
            full_selection=full_selection,
            value_pattern_available=True,
            value_read_only=False,
            keyboard_focus=True,
        )

    def test_full_selection_corruption_is_repaired(self):
        service = self.make_service()
        before = self.state("old text", selected_text="old text", full_selection=True)
        wrong = self.state("Guard 11111111111")
        repaired = self.state("Guard test v0.6.2")

        with (
            patch.object(service, "snapshot", side_effect=[wrong, repaired]),
            patch.object(
                service,
                "set_value_exact",
                return_value={"verification_passed": True},
            ) as setter,
        ):
            result = service.verify_write(
                123,
                "Guard test v0.6.2",
                before,
            )

        self.assertTrue(result["passed"])
        self.assertTrue(result["repair_attempted"])
        self.assertTrue(result["repaired"])
        self.assertEqual(result["status"], "verified_after_repair")
        setter.assert_called_once()

    def test_partial_corruption_is_rolled_back(self):
        service = self.make_service()
        before = self.state("prefix", selected_text="", full_selection=False)
        wrong = self.state("prefix111")
        restored = self.state("prefix")

        with (
            patch.object(service, "snapshot", side_effect=[wrong, restored]),
            patch.object(
                service,
                "set_value_exact",
                return_value={"verification_passed": True},
            ) as setter,
        ):
            result = service.verify_write(
                123,
                "ABC",
                before,
            )

        self.assertFalse(result["passed"])
        self.assertTrue(result["rollback_attempted"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(result["status"], "verification_failed_rolled_back")
        setter.assert_called_once()

    def test_successful_insert_needs_no_repair(self):
        service = self.make_service()
        before = self.state("prefix", full_selection=False)
        after = self.state("prefixABC", full_selection=False)

        with (
            patch.object(service, "snapshot", return_value=after),
            patch.object(service, "set_value_exact") as setter,
        ):
            result = service.verify_write(123, "ABC", before)

        self.assertTrue(result["passed"])
        self.assertEqual(result["status"], "verified")
        setter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
