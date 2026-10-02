from __future__ import annotations

import ctypes
import hashlib
import time
from ctypes import wintypes

import win32api
import win32clipboard
import win32con
import win32gui

from ..audit import AuditLogger
from ..config import ClipboardConfig, KeyboardConfig, MouseConfig


_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_INPUT_KEYBOARD = 1

_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_WHEEL_DELTA = 120

_EXTENDED_KEYS = {
    0x21, 0x22, 0x23, 0x24,
    0x25, 0x26, 0x27, 0x28,
    0x2D, 0x2E,
    0x5B, 0x5C,
}

_KEY_MAP = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "win": 0x5B,
    "windows": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
    "numlock": 0x90,
    "scrolllock": 0x91,
}
for _i in range(1, 25):
    _KEY_MAP[f"f{_i}"] = 0x6F + _i


_ULONG_PTR = wintypes.WPARAM


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTUNION),
    ]


_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_USER32.SendInput.argtypes = (
    wintypes.UINT,
    ctypes.POINTER(_INPUT),
    ctypes.c_int,
)
_USER32.SendInput.restype = wintypes.UINT


class MouseService:
    def __init__(self, config: MouseConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit

    def position(self) -> dict:
        self._require_enabled()
        x, y = win32api.GetCursorPos()
        bounds = self._virtual_bounds()
        self.audit.record("mouse_position", x=int(x), y=int(y))
        return {
            "x": int(x),
            "y": int(y),
            "virtual_screen": bounds,
        }

    def move(self, x: int, y: int, duration_ms: int = 0) -> dict:
        self._require_enabled()
        if not self.config.allow_move:
            raise PermissionError("Mouse movement is disabled.")

        x = int(x)
        y = int(y)
        duration_ms = int(duration_ms)
        if duration_ms < 0:
            raise ValueError("duration_ms must be >= 0.")
        if duration_ms > self.config.max_move_duration_ms:
            raise ValueError(
                f"duration_ms exceeds configured limit {self.config.max_move_duration_ms}."
            )
        self._validate_point(x, y)

        start_x, start_y = win32api.GetCursorPos()
        if duration_ms == 0:
            win32api.SetCursorPos((x, y))
        else:
            steps = max(1, min(300, round(duration_ms / 16)))
            delay = duration_ms / 1000.0 / steps
            for i in range(1, steps + 1):
                nx = round(start_x + (x - start_x) * i / steps)
                ny = round(start_y + (y - start_y) * i / steps)
                win32api.SetCursorPos((nx, ny))
                time.sleep(delay)

        final_x, final_y = win32api.GetCursorPos()
        self.audit.record(
            "mouse_move",
            from_x=int(start_x),
            from_y=int(start_y),
            to_x=int(final_x),
            to_y=int(final_y),
            duration_ms=duration_ms,
        )
        return {
            "from": {"x": int(start_x), "y": int(start_y)},
            "to": {"x": int(final_x), "y": int(final_y)},
            "duration_ms": duration_ms,
        }

    def click(
        self,
        button: str = "left",
        clicks: int = 1,
        interval_ms: int = 100,
    ) -> dict:
        self._require_enabled()
        if not self.config.allow_click:
            raise PermissionError("Mouse clicking is disabled.")

        normalized = str(button).strip().casefold()
        flags = {
            "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
            "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
            "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
        }.get(normalized)
        if flags is None:
            raise ValueError("button must be one of: left, right, middle.")

        clicks = int(clicks)
        interval_ms = int(interval_ms)
        if clicks < 1 or clicks > self.config.max_clicks:
            raise ValueError(
                f"clicks must be between 1 and {self.config.max_clicks}."
            )
        if interval_ms < 0 or interval_ms > 5000:
            raise ValueError("interval_ms must be between 0 and 5000.")

        x, y = win32api.GetCursorPos()
        for i in range(clicks):
            win32api.mouse_event(flags[0], 0, 0, 0, 0)
            win32api.mouse_event(flags[1], 0, 0, 0, 0)
            if i + 1 < clicks and interval_ms:
                time.sleep(interval_ms / 1000.0)

        self.audit.record(
            "mouse_click",
            button=normalized,
            clicks=clicks,
            x=int(x),
            y=int(y),
        )
        return {
            "button": normalized,
            "clicks": clicks,
            "position": {"x": int(x), "y": int(y)},
        }

    def scroll(self, steps: int, horizontal: bool = False) -> dict:
        self._require_enabled()
        if not self.config.allow_scroll:
            raise PermissionError("Mouse scrolling is disabled.")

        steps = int(steps)
        if steps == 0:
            raise ValueError("steps must not be 0.")
        if abs(steps) > self.config.max_scroll_steps:
            raise ValueError(
                f"abs(steps) exceeds configured limit {self.config.max_scroll_steps}."
            )

        flag = _MOUSEEVENTF_HWHEEL if horizontal else _MOUSEEVENTF_WHEEL
        wheel_data = steps * _WHEEL_DELTA
        if wheel_data < 0:
            wheel_data &= 0xFFFFFFFF
        win32api.mouse_event(flag, 0, 0, wheel_data, 0)
        x, y = win32api.GetCursorPos()

        self.audit.record(
            "mouse_scroll",
            steps=steps,
            horizontal=bool(horizontal),
            x=int(x),
            y=int(y),
        )
        return {
            "steps": steps,
            "horizontal": bool(horizontal),
            "position": {"x": int(x), "y": int(y)},
        }

    def _validate_point(self, x: int, y: int) -> None:
        bounds = self._virtual_bounds()
        if not (
            bounds["left"] <= x < bounds["left"] + bounds["width"]
            and bounds["top"] <= y < bounds["top"] + bounds["height"]
        ):
            raise ValueError(
                f"Point ({x}, {y}) is outside the virtual desktop bounds {bounds}."
            )

    @staticmethod
    def _virtual_bounds() -> dict:
        return {
            "left": int(win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)),
            "top": int(win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)),
            "width": int(win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)),
            "height": int(win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)),
        }

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("Mouse tools are disabled.")


class KeyboardService:
    def __init__(self, config: KeyboardConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit

    def write(self, text: str, target_hwnd: int, interval_ms: int = 0) -> dict:
        self._require_enabled()
        if not self.config.allow_write:
            raise PermissionError("Keyboard text input is disabled.")
        if not isinstance(text, str):
            raise TypeError("text must be a string.")
        if len(text) > self.config.max_text_chars:
            raise ValueError(
                f"text exceeds configured limit {self.config.max_text_chars} characters."
            )

        target_hwnd = self._validate_target_hwnd(target_hwnd)
        interval_ms = int(interval_ms)
        if interval_ms < 0 or interval_ms > 5000:
            raise ValueError("interval_ms must be between 0 and 5000.")

        sent_units = 0
        for index, char in enumerate(text):
            self._require_foreground(target_hwnd)
            units = self._utf16_units(char)
            for unit in units:
                self._send_unicode_unit(unit, keyup=False)
                self._send_unicode_unit(unit, keyup=True)
                sent_units += 1
            if interval_ms and index + 1 < len(text):
                time.sleep(interval_ms / 1000.0)

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.audit.record(
            "keyboard_write",
            target_hwnd=target_hwnd,
            text_sha256=digest,
            text_chars=len(text),
            utf16_units=sent_units,
            interval_ms=interval_ms,
        )
        return {
            "target_hwnd": target_hwnd,
            "characters_sent": len(text),
            "utf16_units_sent": sent_units,
            "interval_ms": interval_ms,
            "foreground_verified": True,
        }

    def press(
        self,
        key: str,
        target_hwnd: int,
        presses: int = 1,
        interval_ms: int = 80,
    ) -> dict:
        self._require_enabled()
        if not self.config.allow_press:
            raise PermissionError("Keyboard key presses are disabled.")

        target_hwnd = self._validate_target_hwnd(target_hwnd)
        vk = self._resolve_key(key)
        presses = int(presses)
        interval_ms = int(interval_ms)
        if presses < 1 or presses > self.config.max_press_count:
            raise ValueError(
                f"presses must be between 1 and {self.config.max_press_count}."
            )
        if interval_ms < 0 or interval_ms > 5000:
            raise ValueError("interval_ms must be between 0 and 5000.")

        for i in range(presses):
            self._require_foreground(target_hwnd)
            self._key_down(vk)
            self._key_up(vk)
            if interval_ms and i + 1 < presses:
                time.sleep(interval_ms / 1000.0)

        normalized = self._normalize_key_name(key)
        self.audit.record(
            "keyboard_press",
            target_hwnd=target_hwnd,
            key=normalized,
            presses=presses,
            interval_ms=interval_ms,
        )
        return {
            "target_hwnd": target_hwnd,
            "key": normalized,
            "presses": presses,
            "interval_ms": interval_ms,
            "foreground_verified": True,
        }

    def hotkey(self, keys: list[str], target_hwnd: int) -> dict:
        self._require_enabled()
        if not self.config.allow_hotkey:
            raise PermissionError("Keyboard hotkeys are disabled.")
        if not isinstance(keys, list) or not keys:
            raise ValueError("keys must be a non-empty list.")
        if len(keys) > self.config.max_hotkey_keys:
            raise ValueError(
                f"hotkey exceeds configured limit {self.config.max_hotkey_keys} keys."
            )

        target_hwnd = self._validate_target_hwnd(target_hwnd)
        self._require_foreground(target_hwnd)

        resolved = [(self._normalize_key_name(k), self._resolve_key(k)) for k in keys]
        pressed: list[int] = []
        try:
            for _name, vk in resolved:
                self._key_down(vk)
                pressed.append(vk)
            while pressed:
                self._key_up(pressed.pop())
        finally:
            # Best effort release if an exception occurs mid-sequence.
            while pressed:
                vk = pressed.pop()
                try:
                    self._key_up(vk)
                except Exception:
                    pass

        names = [name for name, _vk in resolved]
        self.audit.record("keyboard_hotkey", target_hwnd=target_hwnd, keys=names)
        return {
            "target_hwnd": target_hwnd,
            "keys": names,
            "foreground_verified": True,
        }

    @staticmethod
    def _validate_target_hwnd(target_hwnd: int) -> int:
        value = int(target_hwnd)
        if value <= 0 or not win32gui.IsWindow(value):
            raise ValueError(f"Target window handle does not exist: {value}")
        return value

    @staticmethod
    def _require_foreground(target_hwnd: int) -> None:
        active_hwnd = int(win32gui.GetForegroundWindow() or 0)
        if active_hwnd != int(target_hwnd):
            raise PermissionError(
                f"Target window {target_hwnd} is not foreground "
                f"(active HWND is {active_hwnd}). Keyboard input was not sent."
            )

    @staticmethod
    def _utf16_units(text: str) -> list[int]:
        raw = text.encode("utf-16le")
        return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]

    @staticmethod
    def _send_unicode_unit(unit: int, keyup: bool) -> None:
        flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if keyup else 0)
        event = _INPUT(
            type=_INPUT_KEYBOARD,
            ki=_KEYBDINPUT(
                wVk=0,
                wScan=int(unit),
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            ),
        )
        sent = _USER32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
        if sent != 1:
            error_code = ctypes.get_last_error()
            raise OSError(
                error_code,
                f"SendInput failed while sending Unicode text (INPUT size={ctypes.sizeof(_INPUT)}).",
            )

    @classmethod
    def _resolve_key(cls, key: str) -> int:
        normalized = cls._normalize_key_name(key)
        if normalized in _KEY_MAP:
            return _KEY_MAP[normalized]
        if len(normalized) == 1:
            ch = normalized.upper()
            if "A" <= ch <= "Z" or "0" <= ch <= "9":
                return ord(ch)
        raise ValueError(
            f"Unsupported key '{key}'. Use a named key, A-Z, 0-9, or F1-F24."
        )

    @staticmethod
    def _normalize_key_name(key: str) -> str:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("key must be a non-empty string.")
        return key.strip().casefold()

    @staticmethod
    def _key_down(vk: int) -> None:
        flags = win32con.KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_KEYS else 0
        win32api.keybd_event(vk, 0, flags, 0)

    @staticmethod
    def _key_up(vk: int) -> None:
        flags = win32con.KEYEVENTF_KEYUP
        if vk in _EXTENDED_KEYS:
            flags |= win32con.KEYEVENTF_EXTENDEDKEY
        win32api.keybd_event(vk, 0, flags, 0)

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("Keyboard tools are disabled.")


class ClipboardService:
    def __init__(self, config: ClipboardConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit

    def read_text(self) -> dict:
        self._require_enabled()
        if not self.config.allow_read:
            raise PermissionError("Clipboard reading is disabled.")

        text = self._read_text_raw()
        if text is None:
            self.audit.record("clipboard_read_text", has_text=False, text_chars=0)
            return {
                "has_text": False,
                "text": None,
                "characters": 0,
                "truncated": False,
            }

        truncated = len(text) > self.config.max_chars
        returned = text[: self.config.max_chars]
        self.audit.record(
            "clipboard_read_text",
            has_text=True,
            text_chars=len(text),
            returned_chars=len(returned),
            truncated=truncated,
        )
        return {
            "has_text": True,
            "text": returned,
            "characters": len(text),
            "truncated": truncated,
        }

    def write_text(self, text: str) -> dict:
        self._require_enabled()
        if not self.config.allow_write:
            raise PermissionError("Clipboard writing is disabled.")
        if not isinstance(text, str):
            raise TypeError("text must be a string.")
        if len(text) > self.config.max_chars:
            raise ValueError(
                f"text exceeds configured limit {self.config.max_chars} characters."
            )

        self._with_clipboard_write(text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.audit.record(
            "clipboard_write_text",
            text_sha256=digest,
            text_chars=len(text),
        )
        return {
            "characters_written": len(text),
        }

    @staticmethod
    def _open_clipboard_with_retry() -> None:
        last_error = None
        for _ in range(20):
            try:
                win32clipboard.OpenClipboard()
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.025)
        if last_error:
            raise last_error
        raise RuntimeError("Unable to open clipboard.")

    @classmethod
    def _read_text_raw(cls) -> str | None:
        cls._open_clipboard_with_retry()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return None
            value = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            return str(value)
        finally:
            win32clipboard.CloseClipboard()

    @classmethod
    def _with_clipboard_write(cls, text: str) -> None:
        cls._open_clipboard_with_retry()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("Clipboard tools are disabled.")
