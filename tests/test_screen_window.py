import base64
import io
import subprocess
import sys

import psutil
import time
import unittest

from PIL import Image

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import ScreenConfig, WindowConfig
from chatgpt_actuator.tools import ScreenService, WindowService


class ScreenWindowTests(unittest.TestCase):
    TEST_TITLE = "ChatGPTActuatorV05TestWindow"

    def make_screen(self) -> ScreenService:
        cfg = ScreenConfig(
            enabled=True,
            default_max_dimension=1280,
            max_allowed_dimension=7680,
        )
        return ScreenService(cfg, AuditLogger(False, "unused.log"))

    def make_window(self) -> WindowService:
        cfg = WindowConfig(
            enabled=True,
            allow_focus=True,
            allow_state_change=True,
            allow_close=True,
            protect_actuator_chain=True,
            max_list_items=200,
            close_wait_seconds=2.0,
        )
        return WindowService(cfg, AuditLogger(False, "unused.log"))

    def test_screen_info_and_capture(self):
        service = self.make_screen()
        info = service.info()
        self.assertGreaterEqual(info["monitor_count"], 1)
        self.assertGreater(info["virtual_screen"]["width"], 0)
        self.assertGreater(info["virtual_screen"]["height"], 0)

        image_content = service.capture(all_screens=False, max_dimension=1280)
        self.assertEqual(image_content.type, "image")
        self.assertEqual(image_content.mime_type, "image/png")
        raw = base64.b64decode(image_content.data)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertGreater(image.width, 0)
            self.assertGreater(image.height, 0)
            self.assertLessEqual(max(image.size), 1280)

    def test_window_lifecycle(self):
        service = self.make_window()
        code = (
            "import tkinter as tk; "
            "r=tk.Tk(); "
            f"r.title('{self.TEST_TITLE}'); "
            "r.geometry('360x180+40+40'); "
            "r.mainloop()"
        )
        proc = subprocess.Popen([sys.executable, "-c", code])

        hwnd = None
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                result = service.list_windows(
                    title_filter=self.TEST_TITLE,
                    visible_only=True,
                    include_untitled=False,
                    max_items=20,
                )
                if result["windows"]:
                    hwnd = result["windows"][0]["hwnd"]
                    break
                time.sleep(0.1)

            self.assertIsNotNone(hwnd, "Test window did not appear.")

            details = service.details(hwnd)
            self.assertEqual(details["title"], self.TEST_TITLE)
            tree_pids = {proc.pid}
            try:
                tree_pids.update(child.pid for child in psutil.Process(proc.pid).children(recursive=True))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            self.assertIn(details["pid"], tree_pids)
            self.assertTrue(details["visible"])

            minimized = service.set_state(hwnd, "minimize")
            self.assertEqual(minimized["requested_state"], "minimize")

            restored = service.set_state(hwnd, "restore")
            self.assertEqual(restored["requested_state"], "restore")

            focus = service.focus(hwnd)
            self.assertEqual(focus["hwnd"], hwnd)
            self.assertIn("focused", focus)

            active = service.active_window()
            self.assertIn("active_window", active)

            closed = service.close(hwnd)
            self.assertTrue(closed["close_requested"])

            proc.wait(timeout=5)
            self.assertFalse(service._protected_pids().__contains__(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
