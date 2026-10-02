from __future__ import annotations

import unittest
from unittest.mock import patch

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import ClipboardConfig, KeyboardConfig, MouseConfig
from chatgpt_actuator.tools import ClipboardService, KeyboardService, MouseService


class InputControlTests(unittest.TestCase):
    def make_mouse(self) -> MouseService:
        return MouseService(
            MouseConfig(
                enabled=True,
                allow_move=True,
                allow_click=True,
                allow_scroll=True,
                max_clicks=5,
                max_move_duration_ms=5000,
                max_scroll_steps=100,
            ),
            AuditLogger(False, "unused.log"),
        )

    def make_keyboard(self) -> KeyboardService:
        return KeyboardService(
            KeyboardConfig(
                enabled=True,
                allow_write=True,
                allow_press=True,
                allow_hotkey=True,
                max_text_chars=10000,
                max_hotkey_keys=8,
                max_press_count=100,
            ),
            AuditLogger(False, "unused.log"),
        )

    def make_clipboard(self) -> ClipboardService:
        return ClipboardService(
            ClipboardConfig(
                enabled=True,
                allow_read=True,
                allow_write=True,
                max_chars=1000000,
            ),
            AuditLogger(False, "unused.log"),
        )

    def test_mouse_position_and_move_roundtrip(self):
        service = self.make_mouse()
        start = service.position()
        x, y = start["x"], start["y"]
        bounds = start["virtual_screen"]
        candidate_x = x + 1 if x + 1 < bounds["left"] + bounds["width"] else x - 1
        candidate_y = y
        try:
            moved = service.move(candidate_x, candidate_y, duration_ms=0)
            self.assertEqual(moved["to"]["x"], candidate_x)
            self.assertEqual(moved["to"]["y"], candidate_y)
        finally:
            service.move(x, y, duration_ms=0)

    def test_mouse_click_uses_expected_flags(self):
        service = self.make_mouse()
        with patch("chatgpt_actuator.tools.input_control.win32api.mouse_event") as mouse_event:
            result = service.click(button="left", clicks=2, interval_ms=0)
        self.assertEqual(result["button"], "left")
        self.assertEqual(result["clicks"], 2)
        self.assertEqual(mouse_event.call_count, 4)

    def test_mouse_scroll_uses_wheel_event(self):
        service = self.make_mouse()
        with patch("chatgpt_actuator.tools.input_control.win32api.mouse_event") as mouse_event:
            result = service.scroll(steps=3, horizontal=False)
        self.assertEqual(result["steps"], 3)
        self.assertFalse(result["horizontal"])
        self.assertEqual(mouse_event.call_count, 1)

    def test_keyboard_unicode_units(self):
        service = self.make_keyboard()
        sent = []
        with (
            patch("chatgpt_actuator.tools.input_control.win32gui.IsWindow", return_value=True),
            patch("chatgpt_actuator.tools.input_control.win32gui.GetForegroundWindow", return_value=123),
            patch.object(service, "_send_unicode_unit", side_effect=lambda unit, keyup: sent.append((unit, keyup))),
        ):
            result = service.write("A✓😀", target_hwnd=123, interval_ms=0)
        self.assertEqual(result["target_hwnd"], 123)
        self.assertEqual(result["characters_sent"], 3)
        # A + check mark + surrogate pair for emoji = 4 UTF-16 code units, each down/up.
        self.assertEqual(result["utf16_units_sent"], 4)
        self.assertEqual(len(sent), 8)

    def test_keyboard_press_and_hotkey(self):
        service = self.make_keyboard()
        with (
            patch("chatgpt_actuator.tools.input_control.win32gui.IsWindow", return_value=True),
            patch("chatgpt_actuator.tools.input_control.win32gui.GetForegroundWindow", return_value=123),
            patch("chatgpt_actuator.tools.input_control.win32api.keybd_event") as keybd,
        ):
            press_result = service.press("enter", target_hwnd=123, presses=2, interval_ms=0)
            hotkey_result = service.hotkey(["ctrl", "a"], target_hwnd=123)
        self.assertEqual(press_result["target_hwnd"], 123)
        self.assertEqual(press_result["presses"], 2)
        self.assertEqual(hotkey_result["target_hwnd"], 123)
        self.assertEqual(hotkey_result["keys"], ["ctrl", "a"])
        self.assertGreaterEqual(keybd.call_count, 8)

    def test_clipboard_read_and_write_logic(self):
        service = self.make_clipboard()
        with patch.object(service, "_read_text_raw", return_value="hello"):
            result = service.read_text()
        self.assertTrue(result["has_text"])
        self.assertEqual(result["text"], "hello")

        with patch.object(service, "_with_clipboard_write") as writer:
            result = service.write_text("world")
        writer.assert_called_once_with("world")
        self.assertEqual(result["characters_written"], 5)

    def test_limits(self):
        mouse = self.make_mouse()
        keyboard = self.make_keyboard()

        with self.assertRaises(ValueError):
            mouse.click(clicks=6)
        with self.assertRaises(ValueError):
            mouse.scroll(steps=101)
        with patch("chatgpt_actuator.tools.input_control.win32gui.IsWindow", return_value=True):
            with self.assertRaises(ValueError):
                keyboard.press("enter", target_hwnd=123, presses=101)
        with self.assertRaises(ValueError):
            keyboard.hotkey(["ctrl"] * 9, target_hwnd=123)

    def test_keyboard_blocks_wrong_foreground(self):
        service = self.make_keyboard()
        with (
            patch("chatgpt_actuator.tools.input_control.win32gui.IsWindow", return_value=True),
            patch("chatgpt_actuator.tools.input_control.win32gui.GetForegroundWindow", return_value=999),
            patch.object(service, "_send_unicode_unit") as sender,
            patch("chatgpt_actuator.tools.input_control.win32api.keybd_event") as keybd,
        ):
            with self.assertRaises(PermissionError):
                service.write("blocked", target_hwnd=123)
            with self.assertRaises(PermissionError):
                service.press("enter", target_hwnd=123)
            with self.assertRaises(PermissionError):
                service.hotkey(["ctrl", "a"], target_hwnd=123)

        sender.assert_not_called()
        keybd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
