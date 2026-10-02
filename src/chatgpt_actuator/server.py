from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .admin import AdminServer
from .runtime import RuntimeManager


runtime = RuntimeManager()
initial_config = runtime.config

filesystem = runtime.proxy("filesystem")
process_system = runtime.proxy("process_system")
powershell = runtime.proxy("powershell")
screen = runtime.proxy("screen")
window = runtime.proxy("window")
mouse = runtime.proxy("mouse")
keyboard = runtime.proxy("keyboard")
clipboard = runtime.proxy("clipboard")
verified_input = runtime.proxy("verified_input")
uia = runtime.proxy("uia")

mcp = MCPServer(
    name=initial_config.name,
    version=initial_config.version,
    description="Permission-gated Windows actuator for ChatGPT.",
)

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

WRITE_ADDITIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)

WRITE_STATEFUL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)

WRITE_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)


# ----------------------------
# Filesystem tools
# ----------------------------

@mcp.tool(title="List allowed filesystem roots", annotations=READ_ONLY)
def filesystem_list_roots() -> dict:
    """List allowed Windows filesystem roots and enabled permissions."""
    return filesystem.list_roots()


@mcp.tool(title="List directory", annotations=READ_ONLY)
def filesystem_list(path: str, max_items: int | None = None) -> dict:
    """List files and folders inside an allowed directory."""
    return filesystem.list_directory(path, max_items=max_items)


@mcp.tool(title="Read text file", annotations=READ_ONLY)
def filesystem_read_text(path: str, max_bytes: int | None = None) -> dict:
    """Read one UTF-8 text file inside an allowed root."""
    return filesystem.read_text(path, max_bytes=max_bytes)


@mcp.tool(title="Get file or directory metadata", annotations=READ_ONLY)
def filesystem_stat(path: str) -> dict:
    """Return metadata for one existing file or directory inside an allowed root."""
    return filesystem.stat_path(path)


@mcp.tool(title="Search filesystem names", annotations=READ_ONLY)
def filesystem_search(
    root: str,
    query: str,
    recursive: bool = True,
    max_results: int | None = None,
) -> dict:
    """Search file/folder names below an allowed root."""
    return filesystem.search(root=root, query=query, recursive=recursive, max_results=max_results)


@mcp.tool(title="Create directory", annotations=WRITE_ADDITIVE)
def filesystem_create_directory(path: str, parents: bool = True, exist_ok: bool = False) -> dict:
    """Create a directory inside an allowed root."""
    return filesystem.create_directory(path, parents=parents, exist_ok=exist_ok)


@mcp.tool(title="Create or overwrite text file", annotations=WRITE_DESTRUCTIVE)
def filesystem_write_text(
    path: str,
    content: str,
    overwrite: bool = False,
    create_parents: bool = True,
) -> dict:
    """Create a UTF-8 text file, or overwrite one when overwrite=true, inside an allowed root."""
    return filesystem.write_text(path, content, overwrite=overwrite, create_parents=create_parents)


@mcp.tool(title="Replace text in file", annotations=WRITE_DESTRUCTIVE)
def filesystem_replace_in_file(
    path: str,
    old_text: str,
    new_text: str,
    max_replacements: int = 1,
) -> dict:
    """Replace exact text inside an allowed UTF-8 file."""
    return filesystem.replace_in_file(path, old_text, new_text, max_replacements=max_replacements)


@mcp.tool(title="Move or rename path", annotations=WRITE_STATEFUL)
def filesystem_move(source: str, destination: str, overwrite: bool = False) -> dict:
    """Move or rename a file/directory inside allowed roots."""
    return filesystem.move_path(source, destination, overwrite=overwrite)


@mcp.tool(title="Delete file or directory", annotations=WRITE_DESTRUCTIVE)
def filesystem_delete(path: str, recursive: bool = False) -> dict:
    """Delete a file or directory inside an allowed root. Allowed roots themselves are protected."""
    return filesystem.delete_path(path, recursive=recursive)


# ----------------------------
# System tools
# ----------------------------

@mcp.tool(title="Get Windows system information", annotations=READ_ONLY)
def system_info() -> dict:
    """Return basic OS, CPU, memory, boot-time, and Python runtime information."""
    if not runtime.config.system.enabled:
        raise PermissionError("System tools are disabled.")
    return process_system.system_info()


@mcp.tool(title="Get resource usage snapshot", annotations=READ_ONLY)
def system_resource_snapshot() -> dict:
    """Return a point-in-time snapshot of CPU, memory, disk, swap, and network usage."""
    if not runtime.config.system.enabled:
        raise PermissionError("System tools are disabled.")
    return process_system.resource_snapshot()


# ----------------------------
# Process tools
# ----------------------------

@mcp.tool(title="List running processes", annotations=READ_ONLY)
def process_list(name_filter: str | None = None, max_items: int | None = None) -> dict:
    """List running Windows processes, optionally filtered by process name."""
    return process_system.list_processes(name_filter=name_filter, max_items=max_items)


@mcp.tool(title="Get process details", annotations=READ_ONLY)
def process_details(pid: int) -> dict:
    """Return details for one running process by PID."""
    return process_system.process_details(pid)


@mcp.tool(title="Start process", annotations=WRITE_ADDITIVE)
def process_start(
    executable: str,
    arguments: list[str] | None = None,
    cwd: str | None = None,
) -> dict:
    """Start an executable directly without a shell. Executable must be an absolute existing file path."""
    return process_system.start_process(executable=executable, arguments=arguments, cwd=cwd)


@mcp.tool(title="Stop process", annotations=WRITE_DESTRUCTIVE)
def process_stop(
    pid: int,
    force: bool = False,
    include_children: bool = False,
) -> dict:
    """Terminate a process by PID. Critical Windows processes and the active actuator/tunnel chain are protected."""
    return process_system.stop_process(pid=pid, force=force, include_children=include_children)


# ----------------------------
# PowerShell tools
# ----------------------------

@mcp.tool(title="Get PowerShell runtime information", annotations=READ_ONLY)
def powershell_info() -> dict:
    """Return the PowerShell runtime and ChatGPT-Actuator PowerShell execution settings."""
    if not runtime.config.powershell.enabled:
        raise PermissionError("PowerShell tools are disabled.")
    return powershell.info()


@mcp.tool(title="Run PowerShell script", annotations=WRITE_DESTRUCTIVE)
def powershell_run(
    script: str,
    cwd: str | None = None,
    timeout_seconds: float | None = None,
) -> dict:
    """Run a PowerShell script as the current Windows user. The cwd is constrained, but PowerShell itself is not a filesystem sandbox."""
    return powershell.run(script=script, cwd=cwd, timeout_seconds=timeout_seconds)


# ----------------------------
# Screen tools
# ----------------------------

@mcp.tool(title="Get display/monitor information", annotations=READ_ONLY)
def screen_info() -> dict:
    """Return monitor geometry and virtual desktop information."""
    return screen.info()


@mcp.tool(title="Capture screenshot", annotations=READ_ONLY)
def screen_capture(
    all_screens: bool = False,
    max_dimension: int | None = None,
):
    """Capture the primary screen or all monitors and return the screenshot directly as an image."""
    return screen.capture(all_screens=all_screens, max_dimension=max_dimension)


# ----------------------------
# Window tools
# ----------------------------

@mcp.tool(title="List top-level windows", annotations=READ_ONLY)
def window_list(
    title_filter: str | None = None,
    visible_only: bool = True,
    include_untitled: bool = False,
    max_items: int | None = None,
) -> dict:
    """List top-level Windows desktop windows with process and geometry metadata."""
    return window.list_windows(
        title_filter=title_filter,
        visible_only=visible_only,
        include_untitled=include_untitled,
        max_items=max_items,
    )


@mcp.tool(title="Get active window", annotations=READ_ONLY)
def window_active() -> dict:
    """Return the current foreground window and its metadata."""
    return window.active_window()


@mcp.tool(title="Get window details", annotations=READ_ONLY)
def window_details(hwnd: int) -> dict:
    """Return details for one window handle."""
    return window.details(hwnd)


@mcp.tool(title="Focus window", annotations=WRITE_STATEFUL)
def window_focus(hwnd: int) -> dict:
    """Restore if needed and request foreground focus for one window."""
    return window.focus(hwnd)


@mcp.tool(title="Change window state", annotations=WRITE_STATEFUL)
def window_set_state(hwnd: int, state: str) -> dict:
    """Set a window state: minimize, maximize, restore, show, or hide."""
    return window.set_state(hwnd, state)


@mcp.tool(title="Close window", annotations=WRITE_DESTRUCTIVE)
def window_close(hwnd: int) -> dict:
    """Request a graceful WM_CLOSE for one window. Does not force-kill the owning process."""
    return window.close(hwnd)


# ----------------------------
# Mouse tools
# ----------------------------

@mcp.tool(title="Get mouse position", annotations=READ_ONLY)
def mouse_position() -> dict:
    """Return the current mouse cursor position and virtual desktop bounds."""
    return mouse.position()


@mcp.tool(title="Move mouse cursor", annotations=WRITE_STATEFUL)
def mouse_move(x: int, y: int, duration_ms: int = 0) -> dict:
    """Move the cursor to an absolute virtual-desktop coordinate."""
    return mouse.move(x=x, y=y, duration_ms=duration_ms)


@mcp.tool(title="Click mouse button", annotations=WRITE_DESTRUCTIVE)
def mouse_click(
    button: str = "left",
    clicks: int = 1,
    interval_ms: int = 100,
) -> dict:
    """Click the current cursor position using left, right, or middle mouse button."""
    return mouse.click(button=button, clicks=clicks, interval_ms=interval_ms)


@mcp.tool(title="Scroll mouse wheel", annotations=WRITE_STATEFUL)
def mouse_scroll(steps: int, horizontal: bool = False) -> dict:
    """Scroll the mouse wheel. Positive/negative steps select direction."""
    return mouse.scroll(steps=steps, horizontal=horizontal)


# ----------------------------
# Keyboard tools
# ----------------------------

@mcp.tool(title="Type and verify Unicode text in a specific window", annotations=WRITE_DESTRUCTIVE)
def keyboard_write(
    text: str,
    target_hwnd: int,
    interval_ms: int = 0,
    auto_focus: bool = True,
    require_verification: bool = True,
) -> dict:
    """Type Unicode text only into target_hwnd. By default the server focuses the primary editable Document/Edit control, snapshots its text/selection, sends input, reads it back, and verifies the typed text. If a full-selection replacement is corrupted, it may repair using UI Automation ValuePattern. Do not report success unless verification.passed is true when verification is required."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus)

    before = None
    if require_verification:
        before = verified_input.snapshot(target_hwnd, focus_control=True)
        # Focusing the child text control must not move foreground away from the
        # requested top-level window.
        window.prepare_input_target(target_hwnd, auto_focus=False)

    result = keyboard.write(
        text=text,
        target_hwnd=target_hwnd,
        interval_ms=interval_ms,
    )

    if not require_verification:
        return {
            **result,
            "target": target,
            "status": "sent_unverified",
            "verification": {
                "supported": False,
                "status": "verification_not_required",
                "passed": None,
            },
        }

    verification = verified_input.verify_write(target_hwnd, text, before)
    return {
        **result,
        "target": target,
        "status": verification["status"],
        "verification": verification,
    }


@mcp.tool(title="Press keyboard key in a specific window", annotations=WRITE_DESTRUCTIVE)
def keyboard_press(
    key: str,
    target_hwnd: int,
    presses: int = 1,
    interval_ms: int = 80,
    auto_focus: bool = True,
) -> dict:
    """Press a key only in target_hwnd. Resolve the intended app/window first. The server verifies/focuses the target and sends no key if foreground verification fails."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus)
    result = keyboard.press(
        key=key,
        target_hwnd=target_hwnd,
        presses=presses,
        interval_ms=interval_ms,
    )
    return {**result, "target": target}


@mcp.tool(title="Send hotkey to a specific window", annotations=WRITE_DESTRUCTIVE)
def keyboard_hotkey(
    keys: list[str],
    target_hwnd: int,
    auto_focus: bool = True,
    focus_text_control: bool = False,
) -> dict:
    """Send a hotkey only to target_hwnd. The server verifies/focuses the top-level target. For text-selection operations such as Ctrl+A, the primary editable Document/Edit control is focused automatically. focus_text_control can also be requested explicitly."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus)

    normalized = [str(k).strip().casefold() for k in keys]
    select_all = len(normalized) == 2 and set(normalized) in ({"ctrl", "a"}, {"control", "a"})
    if focus_text_control or select_all:
        verified_input.snapshot(target_hwnd, focus_control=True)
        window.prepare_input_target(target_hwnd, auto_focus=False)

    result = keyboard.hotkey(keys=keys, target_hwnd=target_hwnd)
    return {**result, "target": target}


# ----------------------------
# Verified text-control tools
# ----------------------------

@mcp.tool(title="Replace all text in a specific editable control", annotations=WRITE_DESTRUCTIVE)
def text_replace_all(
    text: str,
    target_hwnd: int,
    auto_focus: bool = True,
) -> dict:
    """Replace the entire value of the primary editable Document/Edit control inside target_hwnd using UI Automation ValuePattern, then read it back and verify an exact match. Prefer this tool when the user explicitly asks to replace all text."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus)
    result = verified_input.replace_all(target_hwnd, text)
    return {
        **result,
        "target": target,
        "status": "verified" if result["verification_passed"] else "verification_failed",
    }


# ----------------------------
# Windows UI Automation tools
# ----------------------------

@mcp.tool(title="List UI Automation controls", annotations=READ_ONLY)
def uia_list(
    target_hwnd: int,
    control_type: str | None = None,
    name_filter: str | None = None,
    include_hidden: bool = False,
    max_depth: int | None = None,
    max_items: int | None = None,
) -> dict:
    """List semantic UI Automation controls inside target_hwnd. Returns element_ref values that can be used by UIA action tools."""
    return uia.list_elements(
        target_hwnd=target_hwnd,
        control_type=control_type,
        name_filter=name_filter,
        include_hidden=include_hidden,
        max_depth=max_depth,
        max_items=max_items,
    )


@mcp.tool(title="Find UI Automation controls", annotations=READ_ONLY)
def uia_find(
    target_hwnd: int,
    name: str | None = None,
    automation_id: str | None = None,
    control_type: str | None = None,
    class_name: str | None = None,
    exact_name: bool = False,
    include_hidden: bool = False,
    max_depth: int | None = None,
    max_items: int = 20,
) -> dict:
    """Find controls semantically by name, automation_id, control_type, or class_name. At least one selector is required."""
    return uia.find_elements(
        target_hwnd=target_hwnd,
        name=name,
        automation_id=automation_id,
        control_type=control_type,
        class_name=class_name,
        exact_name=exact_name,
        include_hidden=include_hidden,
        max_depth=max_depth,
        max_items=max_items,
    )


@mcp.tool(title="Get UI Automation control details", annotations=READ_ONLY)
def uia_details(
    target_hwnd: int,
    element_ref: str,
    include_value: bool = True,
) -> dict:
    """Get properties, supported UIA patterns, geometry, and optionally the current non-password value for one element_ref."""
    return uia.details(
        target_hwnd=target_hwnd,
        element_ref=element_ref,
        include_value=include_value,
    )


@mcp.tool(title="Focus UI Automation control", annotations=WRITE_STATEFUL)
def uia_focus(
    target_hwnd: int,
    element_ref: str,
    auto_focus_window: bool = True,
) -> dict:
    """Focus a specific semantic control. The top-level target window is verified first."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus_window)
    result = uia.focus(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target}


@mcp.tool(title="Invoke UI Automation control", annotations=WRITE_DESTRUCTIVE)
def uia_invoke(
    target_hwnd: int,
    element_ref: str,
    auto_focus_window: bool = True,
) -> dict:
    """Invoke a control through UI Automation InvokePattern, for example a Button or invokable MenuItem. No coordinate click is used."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus_window)
    result = uia.invoke(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target}


@mcp.tool(title="Set UI Automation value", annotations=WRITE_DESTRUCTIVE)
def uia_set_value(
    target_hwnd: int,
    element_ref: str,
    value: str,
    auto_focus_window: bool = True,
) -> dict:
    """Set a writable non-password ValuePattern control and verify the exact resulting value. The value is not stored verbatim in audit logs."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus_window)
    result = uia.set_value(
        target_hwnd=target_hwnd,
        element_ref=element_ref,
        value=value,
    )
    return {**result, "target": target}


@mcp.tool(title="Toggle UI Automation control", annotations=WRITE_DESTRUCTIVE)
def uia_toggle(
    target_hwnd: int,
    element_ref: str,
    auto_focus_window: bool = True,
) -> dict:
    """Toggle a CheckBox or other TogglePattern control and report the before/after state."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus_window)
    result = uia.toggle(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target}


@mcp.tool(title="Select UI Automation item", annotations=WRITE_DESTRUCTIVE)
def uia_select(
    target_hwnd: int,
    element_ref: str,
    auto_focus_window: bool = True,
) -> dict:
    """Select a TabItem, ListItem, TreeItem, or other SelectionItemPattern control."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus_window)
    result = uia.select(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target}


@mcp.tool(title="Expand or collapse UI Automation control", annotations=WRITE_DESTRUCTIVE)
def uia_expand_collapse(
    target_hwnd: int,
    element_ref: str,
    action: str,
    auto_focus_window: bool = True,
) -> dict:
    """Expand or collapse a control that exposes UI Automation ExpandCollapsePattern."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus_window)
    result = uia.expand_collapse(
        target_hwnd=target_hwnd,
        element_ref=element_ref,
        action=action,
    )
    return {**result, "target": target}


# ----------------------------
# Clipboard tools
# ----------------------------

@mcp.tool(title="Read clipboard text", annotations=READ_ONLY)
def clipboard_read_text() -> dict:
    """Read Unicode text from the Windows clipboard when text is available."""
    return clipboard.read_text()


@mcp.tool(title="Write clipboard text", annotations=WRITE_DESTRUCTIVE)
def clipboard_write_text(text: str) -> dict:
    """Replace the Windows clipboard contents with Unicode text."""
    return clipboard.write_text(text=text)


def main() -> None:
    admin = AdminServer(runtime)
    admin.start()
    try:
        mcp.run()
    finally:
        admin.stop()


if __name__ == "__main__":
    main()
