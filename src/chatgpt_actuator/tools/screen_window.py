from __future__ import annotations

import base64
import ctypes
import io
import os
import time

import psutil
import pywintypes
import win32api
import win32con
import win32gui
import win32process
from PIL import Image, ImageGrab
from mcp.types import ImageContent

from ..audit import AuditLogger
from ..config import ScreenConfig, WindowConfig


class ScreenService:
    def __init__(self, config: ScreenConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit

    def info(self) -> dict:
        if not self.config.enabled:
            raise PermissionError("Screen tools are disabled.")

        monitors = []
        for handle, _hdc, _rect in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(handle)
            monitor_rect = tuple(int(x) for x in info.get("Monitor", (0, 0, 0, 0)))
            work_rect = tuple(int(x) for x in info.get("Work", (0, 0, 0, 0)))
            monitors.append({
                "handle": int(handle),
                "monitor_rect": monitor_rect,
                "work_rect": work_rect,
                "primary": bool(info.get("Flags", 0) & win32con.MONITORINFOF_PRIMARY),
                "width": monitor_rect[2] - monitor_rect[0],
                "height": monitor_rect[3] - monitor_rect[1],
            })

        virtual = {
            "left": win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN),
            "top": win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN),
            "width": win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN),
            "height": win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN),
        }
        result = {
            "monitor_count": len(monitors),
            "monitors": monitors,
            "virtual_screen": virtual,
        }
        self.audit.record("screen_info", monitor_count=len(monitors))
        return result

    def capture(
        self,
        all_screens: bool = False,
        max_dimension: int | None = None,
    ) -> ImageContent:
        if not self.config.enabled:
            raise PermissionError("Screen tools are disabled.")

        dimension = self.config.default_max_dimension if max_dimension is None else int(max_dimension)
        if dimension < 320:
            raise ValueError("max_dimension must be at least 320.")
        if dimension > self.config.max_allowed_dimension:
            raise ValueError(
                f"max_dimension exceeds configured limit {self.config.max_allowed_dimension}."
            )

        image = ImageGrab.grab(all_screens=bool(all_screens))
        original_size = image.size

        longest = max(image.size)
        if longest > dimension:
            scale = dimension / float(longest)
            new_size = (
                max(1, round(image.width * scale)),
                max(1, round(image.height * scale)),
            )
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")

        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()

        self.audit.record(
            "screen_capture",
            all_screens=bool(all_screens),
            original_width=original_size[0],
            original_height=original_size[1],
            output_width=image.width,
            output_height=image.height,
            png_bytes=len(data),
        )
        return ImageContent(
            type="image",
            data=base64.b64encode(data).decode("ascii"),
            mime_type="image/png",
        )


class WindowService:
    _STATE_MAP = {
        "minimize": win32con.SW_MINIMIZE,
        "maximize": win32con.SW_MAXIMIZE,
        "restore": win32con.SW_RESTORE,
        "show": win32con.SW_SHOW,
        "hide": win32con.SW_HIDE,
    }

    def __init__(self, config: WindowConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit

    def list_windows(
        self,
        title_filter: str | None = None,
        visible_only: bool = True,
        include_untitled: bool = False,
        max_items: int | None = None,
    ) -> dict:
        self._require_enabled()

        limit = self.config.max_list_items if max_items is None else int(max_items)
        if limit < 1:
            raise ValueError("max_items must be at least 1.")
        limit = min(limit, self.config.max_list_items)

        needle = title_filter.casefold().strip() if title_filter else None
        handles: list[int] = []

        def callback(hwnd: int, _extra) -> bool:
            if len(handles) >= limit:
                return False
            if visible_only and not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            if not include_untitled and not title.strip():
                return True
            if needle and needle not in title.casefold():
                return True
            handles.append(int(hwnd))
            return True

        try:
            win32gui.EnumWindows(callback, None)
        except pywintypes.error:
            pass

        windows = []
        for hwnd in handles:
            try:
                windows.append(self._window_info(hwnd))
            except (ValueError, pywintypes.error):
                continue

        truncated = len(handles) >= limit
        self.audit.record(
            "window_list",
            title_filter=title_filter,
            visible_only=bool(visible_only),
            include_untitled=bool(include_untitled),
            returned=len(windows),
            truncated=truncated,
        )
        return {"windows": windows, "truncated": truncated}

    def active_window(self) -> dict:
        self._require_enabled()
        hwnd = int(win32gui.GetForegroundWindow() or 0)
        result = {"active_window": None if hwnd == 0 else self._window_info(hwnd)}
        self.audit.record("window_active", hwnd=hwnd)
        return result

    def details(self, hwnd: int) -> dict:
        self._require_enabled()
        result = self._window_info(int(hwnd))
        self.audit.record("window_details", hwnd=int(hwnd))
        return result

    def focus(self, hwnd: int) -> dict:
        self._require_enabled()
        if not self.config.allow_focus:
            raise PermissionError("Window focus control is disabled.")

        hwnd = self._validate_hwnd(hwnd)
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        self._bring_to_foreground(hwnd)
        focused = int(win32gui.GetForegroundWindow() or 0) == hwnd

        self.audit.record("window_focus", hwnd=hwnd, focused=focused)
        return {
            "hwnd": hwnd,
            "focused": focused,
            "active_hwnd": int(win32gui.GetForegroundWindow() or 0),
        }

    def prepare_input_target(
        self,
        hwnd: int,
        auto_focus: bool = True,
        wait_ms: int = 500,
    ) -> dict:
        """Verify an intended top-level window and make it foreground before input."""
        self._require_enabled()
        hwnd = self._validate_hwnd(hwnd)
        info = self._window_info(hwnd)

        if not info["visible"]:
            raise PermissionError(
                f"Target window {hwnd} is not visible. Input was not sent."
            )

        before_hwnd = int(win32gui.GetForegroundWindow() or 0)
        focus_changed = False

        if before_hwnd != hwnd:
            if not auto_focus:
                raise PermissionError(
                    f"Target window {hwnd} is not foreground "
                    f"(active HWND is {before_hwnd}). Input was not sent."
                )
            if not self.config.allow_focus:
                raise PermissionError(
                    "Target window is not foreground and automatic focus is disabled."
                )

            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

            self._bring_to_foreground(hwnd)
            focus_changed = True

            deadline = time.monotonic() + max(0, int(wait_ms)) / 1000.0
            while time.monotonic() < deadline:
                if int(win32gui.GetForegroundWindow() or 0) == hwnd:
                    break
                time.sleep(0.02)

        active_hwnd = int(win32gui.GetForegroundWindow() or 0)
        verified = active_hwnd == hwnd
        if not verified:
            raise PermissionError(
                f"Could not verify target window {hwnd} as foreground "
                f"(active HWND is {active_hwnd}). Input was not sent."
            )

        self.audit.record(
            "input_target_verified",
            hwnd=hwnd,
            pid=info.get("pid"),
            title=info.get("title"),
            auto_focus=bool(auto_focus),
            focus_changed=focus_changed,
            active_hwnd=active_hwnd,
        )
        return {
            "target_hwnd": hwnd,
            "target_title": info.get("title"),
            "target_pid": info.get("pid"),
            "active_hwnd": active_hwnd,
            "foreground_verified": True,
            "auto_focus": bool(auto_focus),
            "focus_changed": focus_changed,
        }

    def set_state(self, hwnd: int, state: str) -> dict:
        self._require_enabled()
        if not self.config.allow_state_change:
            raise PermissionError("Window state control is disabled.")

        hwnd = self._validate_hwnd(hwnd)
        normalized = str(state).strip().casefold()
        command = self._STATE_MAP.get(normalized)
        if command is None:
            raise ValueError(
                "state must be one of: minimize, maximize, restore, show, hide."
            )

        win32gui.ShowWindow(hwnd, command)
        time.sleep(0.05)
        result = self._window_info(hwnd)

        self.audit.record("window_set_state", hwnd=hwnd, state=normalized)
        return {"requested_state": normalized, "window": result}

    def close(self, hwnd: int) -> dict:
        self._require_enabled()
        if not self.config.allow_close:
            raise PermissionError("Window close control is disabled.")

        hwnd = self._validate_hwnd(hwnd)
        info = self._window_info(hwnd)
        pid = int(info["pid"])

        if self.config.protect_actuator_chain and pid in self._protected_pids():
            raise PermissionError(
                f"Refusing to close a window owned by the active ChatGPT-Actuator/tunnel chain (PID {pid})."
            )

        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

        deadline = time.monotonic() + max(0.0, self.config.close_wait_seconds)
        while time.monotonic() < deadline and win32gui.IsWindow(hwnd):
            time.sleep(0.05)

        still_exists = bool(win32gui.IsWindow(hwnd))
        self.audit.record(
            "window_close",
            hwnd=hwnd,
            pid=pid,
            title=info.get("title"),
            still_exists=still_exists,
        )
        return {
            "hwnd": hwnd,
            "pid": pid,
            "title": info.get("title"),
            "close_requested": True,
            "still_exists": still_exists,
        }

    def _window_info(self, hwnd: int) -> dict:
        hwnd = self._validate_hwnd(hwnd)
        thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        title = win32gui.GetWindowText(hwnd) or ""
        class_name = win32gui.GetClassName(hwnd) or ""

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            rect = {
                "left": int(left),
                "top": int(top),
                "right": int(right),
                "bottom": int(bottom),
                "width": int(right - left),
                "height": int(bottom - top),
            }
        except pywintypes.error:
            rect = None

        process_name = None
        executable = None
        try:
            proc = psutil.Process(pid)
            process_name = proc.name()
            try:
                executable = proc.exe()
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                executable = None
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        return {
            "hwnd": int(hwnd),
            "title": title,
            "class_name": class_name,
            "pid": int(pid),
            "thread_id": int(thread_id),
            "process_name": process_name,
            "exe": executable,
            "visible": bool(win32gui.IsWindowVisible(hwnd)),
            "enabled": bool(win32gui.IsWindowEnabled(hwnd)),
            "minimized": bool(win32gui.IsIconic(hwnd)),
            "maximized": bool(ctypes.windll.user32.IsZoomed(hwnd)),
            "rect": rect,
        }

    def _bring_to_foreground(self, hwnd: int) -> None:
        foreground = int(win32gui.GetForegroundWindow() or 0)
        current_thread = int(win32api.GetCurrentThreadId())
        target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
        foreground_thread = 0
        if foreground:
            foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground)

        user32 = ctypes.windll.user32
        attached_current = False
        attached_foreground = False

        try:
            if current_thread != target_thread:
                attached_current = bool(user32.AttachThreadInput(current_thread, target_thread, True))
            if foreground_thread and foreground_thread != target_thread:
                attached_foreground = bool(user32.AttachThreadInput(foreground_thread, target_thread, True))

            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except pywintypes.error:
            pass
        finally:
            if attached_foreground:
                user32.AttachThreadInput(foreground_thread, target_thread, False)
            if attached_current:
                user32.AttachThreadInput(current_thread, target_thread, False)

    @staticmethod
    def _validate_hwnd(hwnd: int) -> int:
        value = int(hwnd)
        if value <= 0 or not win32gui.IsWindow(value):
            raise ValueError(f"Window handle does not exist: {value}")
        return value

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("Window tools are disabled.")

    @staticmethod
    def _protected_pids() -> set[int]:
        protected = {os.getpid()}
        try:
            proc = psutil.Process(os.getpid())
            for parent in proc.parents():
                protected.add(parent.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return protected
