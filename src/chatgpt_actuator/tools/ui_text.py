from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from dataclasses import dataclass

import pythoncom
from pywinauto import Desktop

from ..audit import AuditLogger
from ..config import VerifiedInputConfig


@dataclass
class TextControlState:
    target_hwnd: int
    control_type: str
    class_name: str
    name: str
    native_handle: int | None
    text: str
    selected_text: str
    full_selection: bool
    value_pattern_available: bool
    value_read_only: bool
    keyboard_focus: bool


class VerifiedInputService:
    def __init__(self, config: VerifiedInputConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit

    def snapshot(
        self,
        target_hwnd: int,
        *,
        focus_control: bool | None = None,
    ) -> TextControlState:
        self._require_enabled()
        focus = self.config.focus_text_control if focus_control is None else bool(focus_control)

        with self._com_scope():
            control = self._find_text_control(int(target_hwnd))
            if focus:
                try:
                    if not control.has_keyboard_focus():
                        control.set_focus()
                        deadline = time.monotonic() + 0.5
                        while time.monotonic() < deadline:
                            try:
                                if control.has_keyboard_focus():
                                    break
                            except Exception:
                                break
                            time.sleep(0.02)
                except Exception:
                    # Verification below is authoritative; focus failure is not silently
                    # treated as success.
                    pass

            text = self._read_text(control)
            selected_text = self._read_selection(control)
            full_selection = bool(text) and self._normalize(selected_text) == self._normalize(text)
            value_available, value_read_only = self._value_capabilities(control)

            info = control.element_info
            return TextControlState(
                target_hwnd=int(target_hwnd),
                control_type=str(getattr(info, "control_type", "") or ""),
                class_name=str(getattr(info, "class_name", "") or ""),
                name=str(getattr(info, "name", "") or ""),
                native_handle=self._optional_int(getattr(info, "handle", None)),
                text=text,
                selected_text=selected_text,
                full_selection=full_selection,
                value_pattern_available=value_available,
                value_read_only=value_read_only,
                keyboard_focus=self._has_keyboard_focus(control),
            )

    def verify_write(
        self,
        target_hwnd: int,
        expected_inserted_text: str,
        before: TextControlState,
    ) -> dict:
        self._require_enabled()
        if not self.config.verify_keyboard_write:
            return {
                "supported": False,
                "status": "verification_disabled",
                "passed": None,
            }

        wait_seconds = max(0, self.config.verify_wait_ms) / 1000.0
        deadline = time.monotonic() + wait_seconds
        after = self.snapshot(target_hwnd, focus_control=False)

        if before.full_selection:
            expected_after = expected_inserted_text
            mode = "exact_full_selection"
            passed = self._normalize(after.text) == self._normalize(expected_after)
        else:
            expected_after = None
            mode = "contains_inserted_text"
            passed = (
                expected_inserted_text == ""
                or (
                    expected_inserted_text in after.text
                    and (
                        after.text != before.text
                        or after.text == expected_inserted_text
                    )
                )
            )

        while not passed and time.monotonic() < deadline:
            time.sleep(0.03)
            after = self.snapshot(target_hwnd, focus_control=False)
            if before.full_selection:
                passed = self._normalize(after.text) == self._normalize(expected_inserted_text)
            else:
                passed = (
                    expected_inserted_text == ""
                    or (
                        expected_inserted_text in after.text
                        and (
                            after.text != before.text
                            or after.text == expected_inserted_text
                        )
                    )
                )

        repaired = False
        rolled_back = False
        repair_attempted = False
        rollback_attempted = False

        if not passed and before.full_selection and self.config.repair_full_selection_failure:
            repair_attempted = True
            set_result = self.set_value_exact(
                target_hwnd,
                expected_inserted_text,
                focus_control=False,
                audit_action="verified_input_repair",
            )
            repaired = bool(set_result["verification_passed"])
            if repaired:
                after = self.snapshot(target_hwnd, focus_control=False)
                passed = True
                mode = "exact_full_selection_repaired"

        if (
            not passed
            and not before.full_selection
            and self.config.rollback_partial_failure
            and before.value_pattern_available
            and not before.value_read_only
        ):
            rollback_attempted = True
            rollback = self.set_value_exact(
                target_hwnd,
                before.text,
                focus_control=False,
                audit_action="verified_input_rollback",
            )
            rolled_back = bool(rollback["verification_passed"])
            after = self.snapshot(target_hwnd, focus_control=False)

        status = "verified"
        if repaired:
            status = "verified_after_repair"
        elif not passed and rolled_back:
            status = "verification_failed_rolled_back"
        elif not passed:
            status = "verification_failed"

        result = {
            "supported": True,
            "status": status,
            "passed": bool(passed),
            "mode": mode,
            "before_chars": len(before.text),
            "after_chars": len(after.text),
            "expected_chars": len(expected_inserted_text),
            "before_sha256": self._sha256(before.text),
            "after_sha256": self._sha256(after.text),
            "expected_sha256": self._sha256(expected_inserted_text),
            "full_selection_before": bool(before.full_selection),
            "repair_attempted": repair_attempted,
            "repaired": repaired,
            "rollback_attempted": rollback_attempted,
            "rolled_back": rolled_back,
            "control": self._state_metadata(after),
        }

        self.audit.record(
            "keyboard_write_verification",
            target_hwnd=int(target_hwnd),
            status=status,
            passed=bool(passed),
            mode=mode,
            before_chars=len(before.text),
            after_chars=len(after.text),
            expected_chars=len(expected_inserted_text),
            full_selection_before=bool(before.full_selection),
            repair_attempted=repair_attempted,
            repaired=repaired,
            rollback_attempted=rollback_attempted,
            rolled_back=rolled_back,
            expected_sha256=self._sha256(expected_inserted_text),
            after_sha256=self._sha256(after.text),
        )
        return result

    def replace_all(self, target_hwnd: int, text: str) -> dict:
        self._require_enabled()
        if not isinstance(text, str):
            raise TypeError("text must be a string.")
        if len(text) > self.config.max_read_chars:
            raise ValueError(
                f"text exceeds configured verified-input limit {self.config.max_read_chars}."
            )

        result = self.set_value_exact(
            int(target_hwnd),
            text,
            focus_control=True,
            audit_action="text_replace_all",
        )
        return result

    def set_value_exact(
        self,
        target_hwnd: int,
        text: str,
        *,
        focus_control: bool,
        audit_action: str,
    ) -> dict:
        with self._com_scope():
            control = self._find_text_control(int(target_hwnd))
            if focus_control:
                try:
                    control.set_focus()
                except Exception:
                    pass

            value_iface = self._get_value_iface(control)
            if value_iface is None:
                raise PermissionError(
                    "The target text control does not expose UI Automation ValuePattern."
                )
            if bool(value_iface.CurrentIsReadOnly):
                raise PermissionError("The target text control is read-only.")

            value_iface.SetValue(text)

        deadline = time.monotonic() + max(0, self.config.verify_wait_ms) / 1000.0
        state = self.snapshot(target_hwnd, focus_control=False)
        passed = self._normalize(state.text) == self._normalize(text)
        while not passed and time.monotonic() < deadline:
            time.sleep(0.03)
            state = self.snapshot(target_hwnd, focus_control=False)
            passed = self._normalize(state.text) == self._normalize(text)

        status = "verified" if passed else "verification_failed"
        self.audit.record(
            audit_action,
            target_hwnd=int(target_hwnd),
            status=status,
            verification_passed=bool(passed),
            text_chars=len(text),
            expected_sha256=self._sha256(text),
            actual_sha256=self._sha256(state.text),
            actual_chars=len(state.text),
            control_type=state.control_type,
            class_name=state.class_name,
        )
        return {
            "status": status,
            "verification_passed": bool(passed),
            "target_hwnd": int(target_hwnd),
            "expected_chars": len(text),
            "actual_chars": len(state.text),
            "expected_sha256": self._sha256(text),
            "actual_sha256": self._sha256(state.text),
            "control": self._state_metadata(state),
        }

    def _find_text_control(self, target_hwnd: int):
        window = Desktop(backend="uia").window(handle=int(target_hwnd)).wrapper_object()
        candidates = []
        for element in window.descendants():
            info = element.element_info
            control_type = str(getattr(info, "control_type", "") or "")
            if control_type not in {"Document", "Edit"}:
                continue
            if not bool(getattr(info, "enabled", True)):
                continue
            if not bool(getattr(info, "visible", True)):
                continue
            if bool(getattr(info, "is_password", False)):
                continue

            score = 0
            if control_type == "Document":
                score += 100
            elif control_type == "Edit":
                score += 90

            try:
                if element.has_keyboard_focus():
                    score += 200
            except Exception:
                pass

            value_iface = self._get_value_iface(element)
            if value_iface is not None:
                score += 40
                try:
                    if not bool(value_iface.CurrentIsReadOnly):
                        score += 20
                except Exception:
                    pass

            try:
                rect = element.rectangle()
                area = max(0, rect.width()) * max(0, rect.height())
                score += min(area // 10000, 30)
            except Exception:
                pass

            candidates.append((score, element))

        if not candidates:
            raise LookupError(
                f"No visible editable Document/Edit control was found inside window {target_hwnd}."
            )

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _read_text(self, control) -> str:
        value_iface = self._get_value_iface(control)
        if value_iface is not None:
            try:
                value = value_iface.CurrentValue
                if value is not None:
                    return str(value)[: self.config.max_read_chars]
            except Exception:
                pass

        try:
            value = control.iface_text.DocumentRange.GetText(self.config.max_read_chars)
            return str(value or "")
        except Exception:
            pass

        try:
            return str(control.window_text() or "")[: self.config.max_read_chars]
        except Exception as exc:
            raise RuntimeError("Unable to read text from the target control.") from exc

    def _read_selection(self, control) -> str:
        try:
            ranges = control.iface_text.GetSelection()
            length = int(getattr(ranges, "Length", 0) or 0)
            if length <= 0:
                return ""
            parts = []
            for index in range(length):
                rng = ranges.GetElement(index)
                parts.append(str(rng.GetText(self.config.max_read_chars) or ""))
            return "".join(parts)[: self.config.max_read_chars]
        except Exception:
            return ""

    @staticmethod
    def _get_value_iface(control):
        try:
            return control.iface_value
        except Exception:
            return None

    @classmethod
    def _value_capabilities(cls, control) -> tuple[bool, bool]:
        iface = cls._get_value_iface(control)
        if iface is None:
            return False, True
        try:
            return True, bool(iface.CurrentIsReadOnly)
        except Exception:
            return True, True

    @staticmethod
    def _has_keyboard_focus(control) -> bool:
        try:
            return bool(control.has_keyboard_focus())
        except Exception:
            return False

    @staticmethod
    def _optional_int(value):
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _normalize(text: str) -> str:
        return str(text).replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()

    @staticmethod
    def _state_metadata(state: TextControlState) -> dict:
        return {
            "control_type": state.control_type,
            "class_name": state.class_name,
            "name": state.name,
            "native_handle": state.native_handle,
            "keyboard_focus": state.keyboard_focus,
            "value_pattern_available": state.value_pattern_available,
            "value_read_only": state.value_read_only,
        }

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("Verified input is disabled.")

    @staticmethod
    @contextmanager
    def _com_scope():
        pythoncom.CoInitialize()
        try:
            yield
        finally:
            pythoncom.CoUninitialize()
