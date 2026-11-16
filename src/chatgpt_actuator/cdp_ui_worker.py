"""Fixed Edge debugging controls only; no physical input or generic UI actions."""
from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
import time
from urllib.parse import urlsplit

import psutil

from . import cdp_identity

SETTINGS_URL = 'edge://inspect/#remote-debugging'
PROMPT_TITLE = 'Allow remote debugging?'
PROMPT_BODY = 'An external app wants full control over this Microsoft Edge session'


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def windows(binding, title=None):
    import win32gui
    import win32process
    found = []
    def visit(hwnd, _):
        if (win32process.GetWindowThreadProcessId(hwnd)[1] == binding['pid']
                and win32gui.GetClassName(hwnd) == 'Chrome_WidgetWin_1'
                and win32gui.IsWindowVisible(hwnd)
                and (title is None or win32gui.GetWindowText(hwnd) == title)):
            found.append(hwnd)
    win32gui.EnumWindows(visit, None)
    return found


def controls(hwnd):
    from pywinauto import Desktop
    window = Desktop(backend='uia').window(handle=hwnd)
    return window, window.descendants()


def consent_button(binding, hwnd, name):
    current = cdp_identity.refresh(binding)
    if current['endpoint'] != binding['endpoint'] or current['endpoint'] is None:
        raise RuntimeError('The consent request endpoint changed')
    if hwnd not in windows(binding, PROMPT_TITLE):
        raise RuntimeError('The CDP dialog identity changed')
    _, items = controls(hwnd)
    if any(e.element_info.control_type == 'Document' for e in items):
        raise RuntimeError('A webpage cannot authorize debugging')
    if not any(PROMPT_BODY in (e.element_info.name or '') for e in items):
        raise RuntimeError('Unsupported Edge consent dialog')
    matches = [e for e in items if e.element_info.control_type == 'Button'
               and e.element_info.name == name and e.is_enabled()]
    if len(matches) != 1:
        raise RuntimeError('The native CDP button is ambiguous or unavailable')
    return matches[0]


def handshake_owned(binding, driver):
    current = cdp_identity.refresh(binding)
    if current['endpoint'] != binding['endpoint'] or current['endpoint'] is None:
        raise RuntimeError('The requesting CDP listener identity changed')
    process = psutil.Process(driver['pid'])
    if process.create_time() != driver['started'] or process.name().casefold() != 'node.exe':
        raise RuntimeError('The requesting CDP driver identity changed')
    port = urlsplit(binding['endpoint']).port
    peers = [c for c in psutil.net_connections(kind='tcp')
             if c.status == psutil.CONN_ESTABLISHED and c.raddr
             and c.raddr.port == port and ipaddress.ip_address(c.raddr.ip).is_loopback]
    if not peers or any(c.pid != driver['pid'] for c in peers):
        raise RuntimeError('The pending CDP handshake is not exclusively owned')


def approve(binding, driver):
    cdp_identity.refresh(binding)
    if windows(binding, PROMPT_TITLE):
        raise RuntimeError('Refusing a pre-existing CDP consent dialog')
    emit({'ready': True})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        cdp_identity.refresh(binding)
        handles = windows(binding, PROMPT_TITLE)
        if len(handles) > 1:
            raise RuntimeError('Multiple CDP dialogs cannot be attributed')
        if handles:
            handshake_owned(binding, driver)
            button = consent_button(binding, handles[0], 'Allow')
            handshake_owned(binding, driver)
            emit({'prompt_hwnd': handles[0], 'request_correlated': True})
            button.iface_invoke.Invoke()
            return {'approved': True, 'prompt_hwnd': handles[0], 'pattern': 'InvokePattern'}
        time.sleep(.05)
    raise TimeoutError('No attributable Edge debugging dialog appeared')


def settings_window(binding, hwnd):
    cdp_identity.refresh(binding)
    if hwnd not in windows(binding):
        raise RuntimeError('The settings window identity changed')
    window, items = controls(hwnd)
    addresses = [e for e in items if e.element_info.control_type == 'Edit'
                 and e.element_info.name == 'Address and search bar']
    if len(addresses) != 1:
        return None
    return window, items, addresses[0]


def settings_controls(binding, hwnd):
    found = settings_window(binding, hwnd)
    if found is None:
        return None
    window, items, address = found
    if address.iface_value.CurrentValue != SETTINGS_URL:
        return None
    boxes = [e for e in items if e.element_info.control_type == 'CheckBox'
             and e.element_info.automation_id == 'remote-debugging-enabled'
             and e.element_info.name == 'Allow remote debugging for this browser instance']
    if len(boxes) != 1:
        return None
    return window, items, boxes[0]


def close_owned_settings(binding, hwnd):
    import win32gui
    selected = settings_window(binding, hwnd)
    if selected is None:
        return False
    window, items, address = selected
    value = address.iface_value.CurrentValue
    if value != SETTINGS_URL and not (value == '' and window.window_text().startswith('New tab - ')):
        return False
    if len([e for e in items if e.element_info.control_type == 'TabItem']) != 1:
        return False
    close = [e for e in items if e.element_info.control_type == 'Button'
             and e.element_info.name == 'Close' and e.element_info.automation_id == 'view_7']
    if len(close) != 1:
        return False
    close[0].iface_invoke.Invoke()
    deadline = time.monotonic() + 2
    while win32gui.IsWindow(hwnd) and time.monotonic() < deadline:
        time.sleep(.05)
    return not win32gui.IsWindow(hwnd)


def focus_snapshot():
    import win32gui
    import win32process
    hwnd = win32gui.GetForegroundWindow()
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    process = psutil.Process(pid)
    if process.name().casefold() == 'lockapp.exe':
        raise RuntimeError('The Windows desktop is locked')
    return hwnd, pid, process.create_time()


def last_input_tick():
    """Read the Windows last-input tick through the fixed user32 API."""
    import ctypes
    from ctypes import wintypes

    class LastInputInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]

    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    get_info = user32.GetLastInputInfo
    get_info.argtypes = [ctypes.POINTER(LastInputInfo)]
    get_info.restype = wintypes.BOOL
    if not get_info(ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), 'Cannot verify recent user input')
    return int(info.dwTime)


def restore_focus(previous):
    import win32gui
    import win32process
    hwnd, pid, started = previous
    emit({'focus_restore': {'expected_hwnd': hwnd, 'current_hwnd': win32gui.GetForegroundWindow(),
                            'window_exists': bool(win32gui.IsWindow(hwnd))}})
    if not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
        return False
    if psutil.Process(pid).create_time() != started:
        return False
    if win32gui.GetForegroundWindow() != hwnd:
        win32gui.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + 1
    while win32gui.GetForegroundWindow() != hwnd and time.monotonic() < deadline:
        time.sleep(.05)
    return win32gui.GetForegroundWindow() == hwnd


def navigate_owned_settings(binding, hwnd):
    """The explicit local focus grant applies only to this new, empty window."""
    import win32gui
    found = settings_window(binding, hwnd)
    if found is None:
        raise RuntimeError('Owned temporary Edge window is unavailable')
    window, items, address = found
    if not window.window_text().startswith('New tab - ') or address.iface_value.CurrentValue != '':
        raise RuntimeError('Refusing to overwrite another page or address edit')
    if len([e for e in items if e.element_info.control_type == 'TabItem']) != 1:
        raise RuntimeError('The temporary window acquired other tabs')
    win32gui.SetForegroundWindow(hwnd)
    address.element_info.element.SetFocus()
    focus_deadline = time.monotonic() + 2
    while time.monotonic() < focus_deadline:
        if win32gui.GetForegroundWindow() == hwnd and address.element_info.element.CurrentHasKeyboardFocus:
            break
        time.sleep(.05)
    else:
        raise RuntimeError('The owned settings address field did not acquire focus')
    address.iface_value.SetValue(SETTINGS_URL)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if settings_controls(binding, hwnd) is not None:
            return
        _, current = controls(hwnd)
        # Edge appends a removal shortcut to a history URL row, not to its search row.
        labels = {SETTINGS_URL, SETTINGS_URL + ', press Tab then Enter to Remove Suggestion.'}
        matches = [e for e in current if e.element_info.control_type == 'ListItem'
                   and e.element_info.name in labels]
        if len(matches) == 1:
            matches[0].iface_invoke.Invoke()
            return
        if len(matches) > 1:
            raise RuntimeError('The internal settings navigation target is ambiguous')
        time.sleep(.05)
    window, current = controls(hwnd)
    emit({'settings_navigation': {'title': window.window_text(),
                                 'suggestions': [e.element_info.name for e in current
                                                 if e.element_info.control_type == 'ListItem'
                                                 and 'inspect' in (e.element_info.name or '').lower()]}})
    raise RuntimeError('Edge did not expose semantic internal-page navigation')


def enable(binding):
    if binding.get('allow_temporary_focus') is not True:
        raise PermissionError('Temporary Edge focus requires an explicit local owner grant')
    cdp_identity.refresh(binding)
    before = set(windows(binding))
    previous = focus_snapshot()
    emit({'focus_before': {'hwnd': previous[0], 'pid': previous[1]}})
    # shortcut: window attribution assumes no concurrent input; use a native window token if Edge exposes one.
    input_before = last_input_tick()
    # The fixed URI opens only a temporary window in this already running profile.
    child = subprocess.Popen([binding['exe'], '--profile-directory=' + binding['profile_directory'],
                              '--new-window', SETTINGS_URL],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    owned = None
    changed = False
    focused = False
    restore_allowed = True
    try:
        try:
            child.wait(timeout=5)
        except BaseException:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
            raise
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            cdp_identity.refresh(binding)
            matches = []
            added = set(windows(binding)) - before
            if added and last_input_tick() != input_before:
                restore_allowed = False
                raise RuntimeError('Recent user input made temporary window ownership ambiguous')
            if owned is None and binding.get('allow_temporary_focus') is True and len(added) == 1:
                hwnd = next(iter(added))
                temporary = settings_window(binding, hwnd)
                if temporary is not None:
                    window, items, address = temporary
                    if window.window_text().startswith('New tab - ') and address.iface_value.CurrentValue == '':
                        owned = hwnd
                        emit({'settings_window': hwnd, 'temporary_focus_authorized': True})
                        navigate_owned_settings(binding, hwnd)
                        focused = True
            for hwnd in added:
                selected = settings_controls(binding, hwnd)
                if selected is not None:
                    matches.append((hwnd, selected))
            if len(matches) > 1:
                raise RuntimeError('Multiple new settings windows cannot be attributed')
            if matches:
                owned, (_, _, checkbox) = matches[0]
                state = checkbox.iface_toggle.CurrentToggleState
                if state == 0:
                    checkbox.iface_toggle.Toggle()
                    changed = True
                elif state != 1:
                    raise RuntimeError('Indeterminate remote debugging toggle')
                settle = time.monotonic() + 1
                selected = settings_controls(binding, owned)
                while selected is not None and selected[2].iface_toggle.CurrentToggleState != 1 and time.monotonic() < settle:
                    time.sleep(.05)
                    selected = settings_controls(binding, owned)
                if selected is None or selected[2].iface_toggle.CurrentToggleState != 1:
                    raise RuntimeError('Remote debugging toggle was not verified')
                return {'debugging_enabled': True, 'debugging_enabled_by_tool': changed,
                        'settings_hwnd': owned, 'pattern': 'TogglePattern',
                        'temporary_focus_used': focused}
            time.sleep(.1)
        raise TimeoutError('Edge remote debugging controls were not available through UIA')
    finally:
        try:
            if owned is not None and not close_owned_settings(binding, owned):
                raise RuntimeError('The owned settings window could not be safely closed')
        finally:
            if restore_allowed and not restore_focus(previous):
                raise RuntimeError('The previous foreground window could not be restored')


def main():
    import pythoncom
    raw = sys.argv[1] if len(sys.argv) == 2 else ''
    if len(raw) > 16384:
        raise ValueError('Oversized CDP worker request')
    request = json.loads(raw)
    binding = request['binding']
    cdp_identity.refresh(binding)
    pythoncom.CoInitialize()
    try:
        if request['action'] == 'approve':
            result = approve(binding, request['driver'])
        elif request['action'] == 'enable':
            result = enable(binding)
        elif request['action'] == 'cancel':
            # Parent supplies only a newly observed, correlated dialog from its failed handshake.
            if request.get('request_correlated') is not True:
                raise RuntimeError('No owned consent request to cancel')
            consent_button(binding, request['prompt_hwnd'], 'Cancel').iface_invoke.Invoke()
            result = {'cancelled_owned_prompt': True}
        else:
            raise ValueError('Unsupported CDP worker action')
        emit({'ok': True, **result, 'physical_input_used': False})
    finally:
        pythoncom.CoUninitialize()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit({'ok': False, 'error_type': type(exc).__name__, 'error': str(exc)[:400],
              'context': str(exc.__context__)[:400] if exc.__context__ else None})
        sys.exit(1)
