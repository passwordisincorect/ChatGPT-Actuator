from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import psutil

from chatgpt_actuator import cdp_ui_worker as worker


def binding(**changes):
    value = {"pid": 101, "started": 10.0, "exe": "msedge.exe",
             "profile_directory": "Default", "endpoint": "ws://127.0.0.1:9222/devtools/browser/edge"}
    value.update(changes)
    return value


class UIWorkerTests(unittest.TestCase):
    def test_windows_filters_foreign_pid(self):
        win32gui = SimpleNamespace(
            EnumWindows=lambda callback, extra: [callback(hwnd, extra) for hwnd in (1, 2)],
            GetClassName=lambda hwnd: "Chrome_WidgetWin_1",
            IsWindowVisible=lambda hwnd: True,
            GetWindowText=lambda hwnd: "Allow remote debugging?",
        )
        win32process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 101 if hwnd == 1 else 202))
        with patch.dict("sys.modules", {"win32gui": win32gui, "win32process": win32process}):
            self.assertEqual(worker.windows(binding(), worker.PROMPT_TITLE), [1])

    def test_approve_refuses_preexisting_dialog(self):
        with patch.object(worker.cdp_identity, "refresh", return_value=binding()), \
                patch.object(worker, "windows", return_value=[55]), \
                patch.object(worker, "emit") as emit:
            with self.assertRaisesRegex(RuntimeError, "pre-existing"):
                worker.approve(binding(), {"pid": 5, "started": 3.0})
        emit.assert_not_called()

    def test_endpoint_rotation_blocks_consent_button(self):
        changed = binding(endpoint="ws://127.0.0.1:9333/devtools/browser/new")
        with patch.object(worker.cdp_identity, "refresh", return_value=changed):
            with self.assertRaisesRegex(RuntimeError, "endpoint changed"):
                worker.consent_button(binding(), 55, "Allow")

    def test_web_document_cannot_spoof_native_consent(self):
        button = SimpleNamespace(element_info=SimpleNamespace(control_type="Button", name="Allow"),
                                 is_enabled=lambda: True)
        document = SimpleNamespace(element_info=SimpleNamespace(control_type="Document", name="Allow"))
        with patch.object(worker.cdp_identity, "refresh", return_value=binding()), \
                patch.object(worker, "windows", return_value=[55]), \
                patch.object(worker, "controls", return_value=(None, [document, button])):
            with self.assertRaisesRegex(RuntimeError, "webpage"):
                worker.consent_button(binding(), 55, "Allow")

    def test_handshake_requires_exclusive_loopback_node_connection(self):
        process = SimpleNamespace(create_time=lambda: 3.0, name=lambda: "node.exe")
        connection = SimpleNamespace(status=psutil.CONN_ESTABLISHED, raddr=SimpleNamespace(port=9222, ip="127.0.0.1"), pid=77)
        with patch.object(worker.cdp_identity, "refresh", return_value=binding()), \
                patch.object(worker.psutil, "Process", return_value=process), \
                patch.object(worker.psutil, "net_connections", return_value=[connection]):
            worker.handshake_owned(binding(), {"pid": 77, "started": 3.0})
            connection.pid = 88
            with self.assertRaisesRegex(RuntimeError, "exclusively"):
                worker.handshake_owned(binding(), {"pid": 77, "started": 3.0})

    def _run_enable(self, initial_state, *, temporary_focus=True, fail=False, input_race=False):
        state = {"value": initial_state, "toggles": 0}

        def toggle():
            state["toggles"] += 1
            state["value"] = 1

        class Toggle:
            @property
            def CurrentToggleState(self):
                return state["value"]

            def Toggle(self):
                toggle()

        checkbox = SimpleNamespace()
        checkbox.iface_toggle = Toggle()
        address = SimpleNamespace(iface_value=SimpleNamespace(CurrentValue=""))
        window = SimpleNamespace(window_text=lambda: "New tab - Microsoft Edge")
        hwnd = 55
        windows = [[], [hwnd]]
        child = SimpleNamespace(wait=lambda timeout: 0, poll=lambda: 0)
        focus = (8, 808, 4.0)
        selected = (window, [], checkbox)
        def settings_window(_binding, handle):
            self.assertEqual(handle, hwnd)
            return window, [], address

        with patch.object(worker.cdp_identity, "refresh", return_value=binding()), \
                patch.object(worker, "windows", side_effect=lambda *_: windows.pop(0) if windows else [hwnd]), \
                patch.object(worker, "focus_snapshot", return_value=focus), \
                patch.object(worker, "restore_focus", return_value=True) as restore, \
                patch.object(worker, "settings_window", side_effect=settings_window if temporary_focus else None), \
                patch.object(worker, "navigate_owned_settings") as navigate, \
                patch.object(worker, "settings_controls", side_effect=[selected, selected, selected, selected]), \
                patch.object(worker, "close_owned_settings", return_value=True) as close, \
                patch.object(worker, "emit"), \
                patch.object(worker.subprocess, "Popen", return_value=child), \
                patch.object(worker.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                patch.object(worker, "last_input_tick", side_effect=[100, 101] if input_race else [100, 100]), \
                patch.object(worker.time, "sleep", return_value=None):
            if input_race:
                with self.assertRaisesRegex(RuntimeError, "ownership ambiguous"):
                    worker.enable(binding(allow_temporary_focus=True))
                result = None
            elif fail:
                state["value"] = 2
                with self.assertRaisesRegex(RuntimeError, "Indeterminate"):
                    worker.enable(binding(allow_temporary_focus=temporary_focus))
                result = None
            else:
                result = worker.enable(binding(allow_temporary_focus=temporary_focus))
        if input_race:
            close.assert_not_called()
            restore.assert_not_called()
        else:
            close.assert_called_once_with(binding(allow_temporary_focus=temporary_focus), hwnd)
            restore.assert_called_once_with(focus)
        if temporary_focus and not input_race:
            navigate.assert_called_once_with(binding(allow_temporary_focus=True), hwnd)
        else:
            navigate.assert_not_called()
        return result, state["toggles"]

    def test_toggle_off_to_on_once_and_already_on_is_unchanged(self):
        result, count = self._run_enable(0, temporary_focus=True)
        self.assertEqual(count, 1)
        self.assertTrue(result["debugging_enabled_by_tool"])
        self.assertTrue(result["temporary_focus_used"])
        result, count = self._run_enable(1, temporary_focus=True)
        self.assertEqual(count, 0)
        self.assertFalse(result["debugging_enabled_by_tool"])

    def test_failure_closes_owned_window_and_restores_focus(self):
        self._run_enable(2, fail=True)

    def test_missing_focus_grant_refuses_before_edge_launch(self):
        with patch.object(worker.cdp_identity, "refresh") as refresh, \
                patch.object(worker.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(PermissionError, "explicit local owner grant"):
                worker.enable(binding(allow_temporary_focus=False))
        refresh.assert_not_called()
        launch.assert_not_called()

    def test_recent_user_input_keeps_candidate_window_and_foreground_untouched(self):
        self._run_enable(0, input_race=True)


if __name__ == "__main__":
    unittest.main()
