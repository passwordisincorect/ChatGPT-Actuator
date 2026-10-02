from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager

import pythoncom
from pywinauto import Desktop

from ..audit import AuditLogger
from ..config import UIAutomationConfig


_TOGGLE_NAMES = {
    0: "off",
    1: "on",
    2: "indeterminate",
}

_EXPAND_NAMES = {
    0: "collapsed",
    1: "expanded",
    2: "partially_expanded",
    3: "leaf_node",
}


class UIAutomationService:
    def __init__(self, config: UIAutomationConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit

    def list_elements(
        self,
        target_hwnd: int,
        control_type: str | None = None,
        name_filter: str | None = None,
        include_hidden: bool = False,
        max_depth: int | None = None,
        max_items: int | None = None,
    ) -> dict:
        self._require_enabled()
        depth_limit = self._depth_limit(max_depth)
        item_limit = self._item_limit(max_items)
        type_filter = self._norm(control_type) if control_type else None
        name_needle = self._norm(name_filter) if name_filter else None

        with self._com_scope():
            root = self._window(int(target_hwnd))
            items = []
            scanned = 0
            truncated = False
            for depth, element in self._walk(root, depth_limit):
                scanned += 1
                info = element.element_info
                if not include_hidden and not bool(getattr(info, "visible", True)):
                    continue
                ctype = str(getattr(info, "control_type", "") or "")
                name = str(getattr(info, "name", "") or "")
                if type_filter and self._norm(ctype) != type_filter:
                    continue
                if name_needle and name_needle not in self._norm(name):
                    continue

                items.append(self._metadata(int(target_hwnd), element, depth=depth))
                if len(items) >= item_limit:
                    truncated = True
                    break

        self.audit.record(
            "uia_list",
            target_hwnd=int(target_hwnd),
            control_type=control_type,
            name_filter=name_filter,
            returned=len(items),
            scanned=scanned,
            truncated=truncated,
        )
        return {
            "target_hwnd": int(target_hwnd),
            "elements": items,
            "returned": len(items),
            "truncated": truncated,
        }

    def find_elements(
        self,
        target_hwnd: int,
        *,
        name: str | None = None,
        automation_id: str | None = None,
        control_type: str | None = None,
        class_name: str | None = None,
        exact_name: bool = False,
        include_hidden: bool = False,
        max_depth: int | None = None,
        max_items: int = 20,
    ) -> dict:
        self._require_enabled()
        if not any(
            value is not None and str(value) != ""
            for value in (name, automation_id, control_type, class_name)
        ):
            raise ValueError(
                "At least one selector is required: name, automation_id, control_type, or class_name."
            )

        depth_limit = self._depth_limit(max_depth)
        item_limit = min(max(1, int(max_items)), self.config.max_items)

        with self._com_scope():
            root = self._window(int(target_hwnd))
            matches = []
            scanned = 0
            for depth, element in self._walk(root, depth_limit):
                scanned += 1
                if self._matches(
                    element,
                    name=name,
                    automation_id=automation_id,
                    control_type=control_type,
                    class_name=class_name,
                    exact_name=exact_name,
                    include_hidden=include_hidden,
                ):
                    matches.append(
                        self._metadata(int(target_hwnd), element, depth=depth)
                    )
                    if len(matches) >= item_limit:
                        break

        self.audit.record(
            "uia_find",
            target_hwnd=int(target_hwnd),
            selector_name=name,
            automation_id=automation_id,
            control_type=control_type,
            class_name=class_name,
            exact_name=bool(exact_name),
            returned=len(matches),
            scanned=scanned,
        )
        return {
            "target_hwnd": int(target_hwnd),
            "matches": matches,
            "count": len(matches),
        }

    def details(
        self,
        target_hwnd: int,
        element_ref: str,
        include_value: bool = True,
    ) -> dict:
        self._require_enabled()
        with self._com_scope():
            element = self._resolve_ref(int(target_hwnd), element_ref)
            result = self._metadata(int(target_hwnd), element)
            result["patterns"] = self._patterns(element)

            is_password = bool(getattr(element.element_info, "is_password", False))
            result["is_password"] = is_password
            if include_value and not is_password:
                value = self._read_value(element)
                if value is not None:
                    result["value"] = value[: self.config.max_value_chars]
                    result["value_truncated"] = len(value) > self.config.max_value_chars

        self.audit.record(
            "uia_details",
            target_hwnd=int(target_hwnd),
            element_ref=element_ref,
            include_value=bool(include_value),
        )
        return result

    def focus(self, target_hwnd: int, element_ref: str) -> dict:
        self._require_enabled()
        if not self.config.allow_focus:
            raise PermissionError("UI Automation focus is disabled.")

        with self._com_scope():
            element = self._resolve_ref(int(target_hwnd), element_ref)
            before = self._metadata(int(target_hwnd), element)
            element.set_focus()

            deadline = time.monotonic() + self.config.verify_wait_ms / 1000.0
            focused = self._has_focus(element)
            while not focused and time.monotonic() < deadline:
                time.sleep(0.02)
                focused = self._has_focus(element)

            after = self._metadata(int(target_hwnd), element)

        self.audit.record(
            "uia_focus",
            target_hwnd=int(target_hwnd),
            element_ref=element_ref,
            focused=focused,
        )
        return {
            "target_hwnd": int(target_hwnd),
            "element_ref": element_ref,
            "focused": focused,
            "before": before,
            "after": after,
        }

    def invoke(self, target_hwnd: int, element_ref: str) -> dict:
        self._require_enabled()
        if not self.config.allow_invoke:
            raise PermissionError("UI Automation invoke is disabled.")

        with self._com_scope():
            element = self._resolve_ref(int(target_hwnd), element_ref)
            before = self._metadata(int(target_hwnd), element)
            iface = self._iface(element, "iface_invoke")
            if iface is None:
                raise PermissionError(
                    "The target element does not expose UI Automation InvokePattern."
                )
            iface.Invoke()

        time.sleep(min(0.15, max(0.0, self.config.verify_wait_ms / 1000.0)))
        post = self._post_state(int(target_hwnd), element_ref)

        self.audit.record(
            "uia_invoke",
            target_hwnd=int(target_hwnd),
            element_ref=element_ref,
            control_type=before.get("control_type"),
            name=before.get("name"),
        )
        return {
            "invoked": True,
            "before": before,
            **post,
        }

    def set_value(
        self,
        target_hwnd: int,
        element_ref: str,
        value: str,
    ) -> dict:
        self._require_enabled()
        if not self.config.allow_set_value:
            raise PermissionError("UI Automation value setting is disabled.")
        if not isinstance(value, str):
            raise TypeError("value must be a string.")
        if len(value) > self.config.max_value_chars:
            raise ValueError(
                f"value exceeds configured limit {self.config.max_value_chars} characters."
            )

        with self._com_scope():
            element = self._resolve_ref(int(target_hwnd), element_ref)
            if bool(getattr(element.element_info, "is_password", False)):
                raise PermissionError(
                    "Generic UIA set_value is disabled for password controls."
                )
            iface = self._iface(element, "iface_value")
            if iface is None:
                raise PermissionError(
                    "The target element does not expose UI Automation ValuePattern."
                )
            if bool(iface.CurrentIsReadOnly):
                raise PermissionError("The target UI Automation value is read-only.")

            before = str(iface.CurrentValue or "")
            iface.SetValue(value)

        passed, actual = self._wait_value(int(target_hwnd), element_ref, value)
        self.audit.record(
            "uia_set_value",
            target_hwnd=int(target_hwnd),
            element_ref=element_ref,
            before_chars=len(before),
            value_chars=len(value),
            actual_chars=len(actual),
            expected_sha256=self._sha256(value),
            actual_sha256=self._sha256(actual),
            verification_passed=passed,
        )
        return {
            "status": "verified" if passed else "verification_failed",
            "verification_passed": passed,
            "target_hwnd": int(target_hwnd),
            "element_ref": element_ref,
            "expected_chars": len(value),
            "actual_chars": len(actual),
            "expected_sha256": self._sha256(value),
            "actual_sha256": self._sha256(actual),
        }

    def toggle(self, target_hwnd: int, element_ref: str) -> dict:
        self._require_enabled()
        if not self.config.allow_toggle:
            raise PermissionError("UI Automation toggle is disabled.")

        with self._com_scope():
            element = self._resolve_ref(int(target_hwnd), element_ref)
            iface = self._iface(element, "iface_toggle")
            if iface is None:
                raise PermissionError(
                    "The target element does not expose UI Automation TogglePattern."
                )
            before = int(iface.CurrentToggleState)
            iface.Toggle()

        after = self._wait_toggle_change(int(target_hwnd), element_ref, before)
        changed = after != before
        self.audit.record(
            "uia_toggle",
            target_hwnd=int(target_hwnd),
            element_ref=element_ref,
            before=before,
            after=after,
            changed=changed,
        )
        return {
            "target_hwnd": int(target_hwnd),
            "element_ref": element_ref,
            "before": _TOGGLE_NAMES.get(before, str(before)),
            "after": _TOGGLE_NAMES.get(after, str(after)),
            "changed": changed,
        }

    def select(self, target_hwnd: int, element_ref: str) -> dict:
        self._require_enabled()
        if not self.config.allow_select:
            raise PermissionError("UI Automation selection is disabled.")

        with self._com_scope():
            element = self._resolve_ref(int(target_hwnd), element_ref)
            iface = self._iface(element, "iface_selection_item")
            if iface is None:
                raise PermissionError(
                    "The target element does not expose UI Automation SelectionItemPattern."
                )
            before = bool(iface.CurrentIsSelected)
            iface.Select()

        after = self._wait_selected(int(target_hwnd), element_ref)
        self.audit.record(
            "uia_select",
            target_hwnd=int(target_hwnd),
            element_ref=element_ref,
            before=before,
            after=after,
        )
        return {
            "target_hwnd": int(target_hwnd),
            "element_ref": element_ref,
            "before_selected": before,
            "selected": after,
        }

    def expand_collapse(
        self,
        target_hwnd: int,
        element_ref: str,
        action: str,
    ) -> dict:
        self._require_enabled()
        if not self.config.allow_expand_collapse:
            raise PermissionError("UI Automation expand/collapse is disabled.")

        normalized = str(action).strip().casefold()
        if normalized not in {"expand", "collapse"}:
            raise ValueError("action must be 'expand' or 'collapse'.")

        with self._com_scope():
            element = self._resolve_ref(int(target_hwnd), element_ref)
            iface = self._iface(element, "iface_expand_collapse")
            if iface is None:
                raise PermissionError(
                    "The target element does not expose UI Automation ExpandCollapsePattern."
                )
            before = int(iface.CurrentExpandCollapseState)
            if normalized == "expand":
                iface.Expand()
            else:
                iface.Collapse()

        after = self._wait_expand_state(int(target_hwnd), element_ref, normalized)
        self.audit.record(
            "uia_expand_collapse",
            target_hwnd=int(target_hwnd),
            element_ref=element_ref,
            requested_action=normalized,
            before=before,
            after=after,
        )
        return {
            "target_hwnd": int(target_hwnd),
            "element_ref": element_ref,
            "action": normalized,
            "before": _EXPAND_NAMES.get(before, str(before)),
            "after": _EXPAND_NAMES.get(after, str(after)),
        }

    def _window(self, target_hwnd: int):
        return Desktop(backend="uia").window(handle=int(target_hwnd)).wrapper_object()

    def _walk(self, root, max_depth: int):
        queue = [(0, root)]
        while queue:
            depth, element = queue.pop(0)
            yield depth, element
            if depth >= max_depth:
                continue
            try:
                children = element.children()
            except Exception:
                children = []
            for child in children:
                queue.append((depth + 1, child))

    def _matches(
        self,
        element,
        *,
        name: str | None,
        automation_id: str | None,
        control_type: str | None,
        class_name: str | None,
        exact_name: bool,
        include_hidden: bool,
    ) -> bool:
        info = element.element_info
        if not include_hidden and not bool(getattr(info, "visible", True)):
            return False

        actual_name = str(getattr(info, "name", "") or "")
        actual_automation_id = str(getattr(info, "automation_id", "") or "")
        actual_type = str(getattr(info, "control_type", "") or "")
        actual_class = str(getattr(info, "class_name", "") or "")

        if name is not None:
            if exact_name:
                if self._norm(actual_name) != self._norm(name):
                    return False
            elif self._norm(name) not in self._norm(actual_name):
                return False
        if automation_id is not None and self._norm(actual_automation_id) != self._norm(automation_id):
            return False
        if control_type is not None and self._norm(actual_type) != self._norm(control_type):
            return False
        if class_name is not None and self._norm(actual_class) != self._norm(class_name):
            return False
        return True

    def _metadata(self, target_hwnd: int, element, depth: int | None = None) -> dict:
        info = element.element_info
        runtime_id = self._runtime_id(element)
        rect = None
        try:
            r = element.rectangle()
            rect = {
                "left": int(r.left),
                "top": int(r.top),
                "right": int(r.right),
                "bottom": int(r.bottom),
                "width": int(r.width()),
                "height": int(r.height()),
            }
        except Exception:
            pass

        data = {
            "element_ref": self._make_ref(target_hwnd, runtime_id),
            "runtime_id": list(runtime_id),
            "control_type": str(getattr(info, "control_type", "") or ""),
            "name": str(getattr(info, "name", "") or ""),
            "automation_id": str(getattr(info, "automation_id", "") or ""),
            "class_name": str(getattr(info, "class_name", "") or ""),
            "native_handle": self._optional_int(getattr(info, "handle", None)),
            "visible": bool(getattr(info, "visible", True)),
            "enabled": bool(getattr(info, "enabled", True)),
            "is_password": bool(getattr(info, "is_password", False)),
            "keyboard_focus": self._has_focus(element),
            "rect": rect,
        }
        if depth is not None:
            data["depth"] = int(depth)
        return data

    def _patterns(self, element) -> dict:
        return {
            "invoke": self._iface(element, "iface_invoke") is not None,
            "value": self._iface(element, "iface_value") is not None,
            "toggle": self._iface(element, "iface_toggle") is not None,
            "selection_item": self._iface(element, "iface_selection_item") is not None,
            "expand_collapse": self._iface(element, "iface_expand_collapse") is not None,
            "scroll_item": self._iface(element, "iface_scroll_item") is not None,
            "text": self._iface(element, "iface_text") is not None,
        }

    def _resolve_ref(self, target_hwnd: int, element_ref: str):
        ref_hwnd, runtime_id = self._parse_ref(element_ref)
        if ref_hwnd != int(target_hwnd):
            raise PermissionError(
                f"element_ref belongs to HWND {ref_hwnd}, not target HWND {target_hwnd}."
            )

        root = self._window(int(target_hwnd))
        for _depth, element in self._walk(root, self.config.max_depth):
            if self._runtime_id(element) == runtime_id:
                return element
        raise LookupError(
            "UI Automation element_ref is stale or no longer exists in the target window."
        )

    def _post_state(self, target_hwnd: int, element_ref: str) -> dict:
        try:
            with self._com_scope():
                element = self._resolve_ref(target_hwnd, element_ref)
                return {
                    "element_still_present": True,
                    "after": self._metadata(target_hwnd, element),
                }
        except Exception:
            return {
                "element_still_present": False,
                "after": None,
            }

    def _wait_value(self, target_hwnd: int, element_ref: str, expected: str) -> tuple[bool, str]:
        deadline = time.monotonic() + self.config.verify_wait_ms / 1000.0
        actual = ""
        while True:
            with self._com_scope():
                element = self._resolve_ref(target_hwnd, element_ref)
                iface = self._iface(element, "iface_value")
                if iface is None:
                    return False, actual
                actual = str(iface.CurrentValue or "")
            if actual == expected or time.monotonic() >= deadline:
                return actual == expected, actual
            time.sleep(0.03)

    def _wait_toggle_change(self, target_hwnd: int, element_ref: str, before: int) -> int:
        deadline = time.monotonic() + self.config.verify_wait_ms / 1000.0
        after = before
        while True:
            with self._com_scope():
                element = self._resolve_ref(target_hwnd, element_ref)
                iface = self._iface(element, "iface_toggle")
                if iface is None:
                    return after
                after = int(iface.CurrentToggleState)
            if after != before or time.monotonic() >= deadline:
                return after
            time.sleep(0.03)

    def _wait_selected(self, target_hwnd: int, element_ref: str) -> bool:
        deadline = time.monotonic() + self.config.verify_wait_ms / 1000.0
        selected = False
        while True:
            with self._com_scope():
                element = self._resolve_ref(target_hwnd, element_ref)
                iface = self._iface(element, "iface_selection_item")
                if iface is None:
                    return False
                selected = bool(iface.CurrentIsSelected)
            if selected or time.monotonic() >= deadline:
                return selected
            time.sleep(0.03)

    def _wait_expand_state(self, target_hwnd: int, element_ref: str, action: str) -> int:
        desired = 1 if action == "expand" else 0
        deadline = time.monotonic() + self.config.verify_wait_ms / 1000.0
        state = -1
        while True:
            with self._com_scope():
                element = self._resolve_ref(target_hwnd, element_ref)
                iface = self._iface(element, "iface_expand_collapse")
                if iface is None:
                    return state
                state = int(iface.CurrentExpandCollapseState)
            if state == desired or time.monotonic() >= deadline:
                return state
            time.sleep(0.03)

    def _read_value(self, element) -> str | None:
        value_iface = self._iface(element, "iface_value")
        if value_iface is not None:
            try:
                return str(value_iface.CurrentValue or "")
            except Exception:
                pass

        text_iface = self._iface(element, "iface_text")
        if text_iface is not None:
            try:
                return str(text_iface.DocumentRange.GetText(self.config.max_value_chars) or "")
            except Exception:
                pass

        try:
            return str(element.window_text() or "")
        except Exception:
            return None

    @staticmethod
    def _iface(element, name: str):
        try:
            return getattr(element, name)
        except Exception:
            return None

    @staticmethod
    def _runtime_id(element) -> tuple[int, ...]:
        rid = getattr(element.element_info, "runtime_id", None)
        if not rid:
            handle = getattr(element.element_info, "handle", None)
            if handle:
                return (42, int(handle))
            raise LookupError("UI Automation element has no RuntimeId or native handle.")
        return tuple(int(x) for x in rid)

    @staticmethod
    def _make_ref(target_hwnd: int, runtime_id: tuple[int, ...]) -> str:
        return f"uia:{int(target_hwnd)}:" + ",".join(str(x) for x in runtime_id)

    @staticmethod
    def _parse_ref(element_ref: str) -> tuple[int, tuple[int, ...]]:
        try:
            prefix, hwnd_text, rid_text = str(element_ref).split(":", 2)
            if prefix != "uia":
                raise ValueError
            hwnd = int(hwnd_text)
            runtime_id = tuple(int(x) for x in rid_text.split(",") if x != "")
            if not runtime_id:
                raise ValueError
            return hwnd, runtime_id
        except Exception as exc:
            raise ValueError("Invalid UI Automation element_ref.") from exc

    def _depth_limit(self, max_depth: int | None) -> int:
        value = self.config.max_depth if max_depth is None else int(max_depth)
        if value < 0:
            raise ValueError("max_depth must be >= 0.")
        return min(value, self.config.max_depth)

    def _item_limit(self, max_items: int | None) -> int:
        value = self.config.max_items if max_items is None else int(max_items)
        if value < 1:
            raise ValueError("max_items must be >= 1.")
        return min(value, self.config.max_items)

    @staticmethod
    def _norm(value) -> str:
        return str(value).strip().casefold()

    @staticmethod
    def _has_focus(element) -> bool:
        try:
            return bool(element.has_keyboard_focus())
        except Exception:
            return False

    @staticmethod
    def _optional_int(value):
        try:
            return None if value is None else int(value)
        except Exception:
            return None

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("UI Automation tools are disabled.")

    @staticmethod
    @contextmanager
    def _com_scope():
        pythoncom.CoInitialize()
        try:
            yield
        finally:
            pythoncom.CoUninitialize()
