from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .admin import AdminServer
from .dom_host import DOMAsyncProxy, DOMExecutionHost
from .batch import PARALLEL_READ_ONLY_TOOLS, execute_batch
from .routing_policy import RoutingPolicyEngine
from .runtime import RuntimeManager
from .transport import run_options


runtime = RuntimeManager()
initial_config = runtime.config
routing_policy = RoutingPolicyEngine()

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
dom_host = DOMExecutionHost(runtime)
dom = DOMAsyncProxy(dom_host)
eda = runtime.proxy("eda")

mcp = MCPServer(
    name=initial_config.name,
    version=initial_config.version,
    description="Direct local execution plane for ChatGPT MAIN. Prefer for simple local machine actions and short deterministic workflows; use ChatGPT-Orchestrator for independent multi-worker analysis, review/rework, parallel reasoning, or recovery.",
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
# Routing policy tools
# ----------------------------

@mcp.tool(title="Read local routing policy", annotations=READ_ONLY)
def routing_policy_info() -> dict:
    """Read the local MAIN/Actuator/Orchestrator routing policy and loop guards."""
    return routing_policy.info()


@mcp.tool(title="Recommend execution route", annotations=READ_ONLY)
def routing_decide(
    goal: str,
    origin: str = "main",
    requires_execution: bool = True,
    local_machine_action: bool = False,
    short_deterministic_workflow: bool = False,
    complex_multi_step: bool = False,
    independent_workers: bool = False,
    parallel_reasoning: bool = False,
    review_rework: bool = False,
    restart_recovery: bool = False,
) -> dict:
    """Recommend DIRECT, ChatGPT-Actuator, or ChatGPT-Orchestrator for an ambiguous execution request.

    This tool only decides a route; it never executes or delegates the goal.
    MAIN should call it only when routing is genuinely ambiguous. Complex signals
    take priority over local-action signals so a complex local workflow can be
    orchestrated first and handed down to Actuator later. Loop guards are read
    from the local routing-policy.json before a recommendation is returned.
    """
    result = routing_policy.decide(
        goal,
        origin=origin,
        requires_execution=requires_execution,
        local_machine_action=local_machine_action,
        short_deterministic_workflow=short_deterministic_workflow,
        complex_multi_step=complex_multi_step,
        independent_workers=independent_workers,
        parallel_reasoning=parallel_reasoning,
        review_rework=review_rework,
        restart_recovery=restart_recovery,
    )
    runtime.audit.record(
        "routing_decide",
        origin=result["origin"],
        requested_route=result["requested_route"],
        recommended_route=result["recommended_route"],
        guard_blocked=result["guard"]["blocked"],
        signals=result["signals"],
    )
    return result


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
    auto_focus: bool = False,
) -> dict:
    """Replace the entire value of the primary editable Document/Edit control inside target_hwnd using UI Automation ValuePattern, then read it back and verify an exact match. Prefer this tool when the user explicitly asks to replace all text."""
    target = None
    if auto_focus:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = verified_input.replace_all(target_hwnd, text, focus_control=auto_focus)
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
    """Focus a specific semantic control. The top-level target window is focused only when auto_focus_window=true; background operation is the default."""
    target = window.prepare_input_target(target_hwnd, auto_focus=auto_focus_window)
    result = uia.focus(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target}


@mcp.tool(title="Invoke UI Automation control", annotations=WRITE_DESTRUCTIVE)
def uia_invoke(
    target_hwnd: int,
    element_ref: str,
    auto_focus_window: bool = False,
) -> dict:
    """Invoke a control through UI Automation InvokePattern, for example a Button or invokable MenuItem. No coordinate click is used."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.invoke(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target, "background_requested": not auto_focus_window}


@mcp.tool(title="Set UI Automation value", annotations=WRITE_DESTRUCTIVE)
def uia_set_value(
    target_hwnd: int,
    element_ref: str,
    value: str,
    auto_focus_window: bool = False,
) -> dict:
    """Set a writable non-password ValuePattern control and verify the exact resulting value. The value is not stored verbatim in audit logs."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.set_value(
        target_hwnd=target_hwnd,
        element_ref=element_ref,
        value=value,
    )
    return {**result, "target": target, "background_requested": not auto_focus_window}


@mcp.tool(title="Toggle UI Automation control", annotations=WRITE_DESTRUCTIVE)
def uia_toggle(
    target_hwnd: int,
    element_ref: str,
    auto_focus_window: bool = False,
) -> dict:
    """Toggle a CheckBox or other TogglePattern control and report the before/after state."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.toggle(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target, "background_requested": not auto_focus_window}


@mcp.tool(title="Select UI Automation item", annotations=WRITE_DESTRUCTIVE)
def uia_select(
    target_hwnd: int,
    element_ref: str,
    auto_focus_window: bool = False,
) -> dict:
    """Select a TabItem, ListItem, TreeItem, or other SelectionItemPattern control."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.select(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target, "background_requested": not auto_focus_window}


@mcp.tool(title="Expand or collapse UI Automation control", annotations=WRITE_DESTRUCTIVE)
def uia_expand_collapse(
    target_hwnd: int,
    element_ref: str,
    action: str,
    auto_focus_window: bool = False,
) -> dict:
    """Expand or collapse a control that exposes UI Automation ExpandCollapsePattern."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.expand_collapse(
        target_hwnd=target_hwnd,
        element_ref=element_ref,
        action=action,
    )
    return {**result, "target": target, "background_requested": not auto_focus_window}


@mcp.tool(title="Scroll UI Automation control", annotations=WRITE_STATEFUL)
def uia_scroll(
    target_hwnd: int,
    element_ref: str,
    direction: str,
    amount: str = "line",
    count: int = 1,
    auto_focus_window: bool = False,
) -> dict:
    """Scroll through UI Automation ScrollPattern. No physical mouse wheel is used; background operation is the default."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.scroll(
        target_hwnd=target_hwnd,
        element_ref=element_ref,
        direction=direction,
        amount=amount,
        count=count,
    )
    return {**result, "target": target, "background_requested": not auto_focus_window}


@mcp.tool(title="Scroll UI Automation item into view", annotations=WRITE_STATEFUL)
def uia_scroll_into_view(
    target_hwnd: int,
    element_ref: str,
    auto_focus_window: bool = False,
) -> dict:
    """Bring a semantic control into view via ScrollItemPattern without moving the physical mouse."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.scroll_into_view(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target, "background_requested": not auto_focus_window}


@mcp.tool(title="Set UI Automation range value", annotations=WRITE_DESTRUCTIVE)
def uia_set_range_value(
    target_hwnd: int,
    element_ref: str,
    value: float,
    auto_focus_window: bool = False,
) -> dict:
    """Set a slider/spinner RangeValuePattern directly without mouse dragging or keyboard input."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.set_range_value(
        target_hwnd=target_hwnd,
        element_ref=element_ref,
        value=value,
    )
    return {**result, "target": target, "background_requested": not auto_focus_window}


@mcp.tool(title="Select all text with UI Automation", annotations=WRITE_STATEFUL)
def uia_text_select_all(
    target_hwnd: int,
    element_ref: str,
    auto_focus_window: bool = False,
) -> dict:
    """Select a TextPattern document range directly, replacing Ctrl+A where the provider supports it."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.text_select_all(target_hwnd=target_hwnd, element_ref=element_ref)
    return {**result, "target": target, "background_requested": not auto_focus_window}


@mcp.tool(title="Run UI Automation window action", annotations=WRITE_DESTRUCTIVE)
def uia_window_action(
    target_hwnd: int,
    element_ref: str,
    action: str,
    auto_focus_window: bool = False,
) -> dict:
    """Minimize, maximize, restore, or close through WindowPattern without keyboard hotkeys or mouse input."""
    target = None
    if auto_focus_window:
        target = window.prepare_input_target(target_hwnd, auto_focus=True)
    result = uia.window_action(
        target_hwnd=target_hwnd,
        element_ref=element_ref,
        action=action,
    )
    return {**result, "target": target, "background_requested": not auto_focus_window}



# ----------------------------
# Browser DOM tools (Playwright + CDP)
# ----------------------------

@mcp.tool(title="List allowed Edge DOM tabs", annotations=READ_ONLY)
async def dom_list_tabs() -> dict:
    """List Edge tabs visible through the configured CDP session and allowed host list."""
    return await dom.list_tabs()


@mcp.tool(title="Open allowed Edge DOM tab", annotations=WRITE_ADDITIVE)
async def dom_open_tab(url: str, timeout_ms: int | None = None) -> dict:
    """Open one allowed URL in a new Edge tab through the shared Actuator CDP owner."""
    return await dom.open_tab(url, timeout_ms=timeout_ms)


@mcp.tool(title="Navigate Edge DOM tab", annotations=WRITE_DESTRUCTIVE)
async def dom_navigate(
    tab_ref: str,
    url: str,
    timeout_ms: int | None = None,
) -> dict:
    """Navigate an existing stable DOM tab reference to another allowed URL."""
    return await dom.navigate(tab_ref, url, timeout_ms=timeout_ms)


@mcp.tool(title="Close Edge DOM tab", annotations=WRITE_DESTRUCTIVE)
async def dom_close_tab(tab_ref: str) -> dict:
    """Close one Edge tab owned through the shared DOM/CDP connection."""
    return await dom.close_tab(tab_ref)


@mcp.tool(title="Find DOM elements", annotations=READ_ONLY)
async def dom_find(
    tab_ref: str,
    selector: str | None = None,
    text: str | None = None,
    role: str | None = None,
    name: str | None = None,
    exact: bool = False,
    max_items: int = 20,
) -> dict:
    """Find elements using one strategy: CSS selector, visible text, or accessible role."""
    return await dom.find(
        tab_ref,
        selector=selector,
        text=text,
        role=role,
        name=name,
        exact=exact,
        max_items=max_items,
    )


@mcp.tool(title="Get DOM element details", annotations=READ_ONLY)
async def dom_details(element_ref: str) -> dict:
    """Return semantic DOM properties and attributes for an opaque element_ref."""
    return await dom.details(element_ref)


@mcp.tool(title="Read DOM element text", annotations=READ_ONLY)
async def dom_read_text(element_ref: str, max_chars: int | None = None) -> dict:
    """Read rendered/text content without using keyboard selection or clipboard."""
    return await dom.read_text(element_ref, max_chars=max_chars)


@mcp.tool(title="Click DOM element", annotations=WRITE_DESTRUCTIVE)
async def dom_click(element_ref: str, timeout_ms: int | None = None) -> dict:
    """Activate a DOM element through the browser automation layer; no physical OS mouse is used."""
    return await dom.click(element_ref, timeout_ms=timeout_ms)


@mcp.tool(title="Set DOM form value", annotations=WRITE_DESTRUCTIVE)
async def dom_set_value(
    element_ref: str,
    value: str,
    timeout_ms: int | None = None,
) -> dict:
    """Fill an input/textarea through Playwright; no physical keyboard input is used."""
    return await dom.set_value(element_ref, value=value, timeout_ms=timeout_ms)


@mcp.tool(title="Select DOM option", annotations=WRITE_DESTRUCTIVE)
async def dom_select(
    element_ref: str,
    value: str | None = None,
    label: str | None = None,
    timeout_ms: int | None = None,
) -> dict:
    """Select one HTML option by value or label."""
    return await dom.select(
        element_ref,
        value=value,
        label=label,
        timeout_ms=timeout_ms,
    )


@mcp.tool(title="Toggle DOM checkbox", annotations=WRITE_DESTRUCTIVE)
async def dom_toggle(
    element_ref: str,
    checked: bool | None = None,
    timeout_ms: int | None = None,
) -> dict:
    """Check, uncheck, or invert a checkbox/radio-like control semantically."""
    return await dom.toggle(element_ref, checked=checked, timeout_ms=timeout_ms)


@mcp.tool(title="Scroll DOM element into view", annotations=WRITE_STATEFUL)
async def dom_scroll_into_view(
    element_ref: str,
    timeout_ms: int | None = None,
) -> dict:
    """Scroll a DOM element into view without moving the physical mouse."""
    return await dom.scroll_into_view(element_ref, timeout_ms=timeout_ms)


@mcp.tool(title="Scroll DOM document or element", annotations=WRITE_STATEFUL)
async def dom_scroll(
    tab_ref: str,
    direction: str,
    amount: int = 600,
    element_ref: str | None = None,
) -> dict:
    """Scroll with DOM JavaScript in a tab or scrollable element; no physical mouse wheel is used."""
    return await dom.scroll(
        tab_ref,
        direction=direction,
        amount=amount,
        element_ref=element_ref,
    )


@mcp.tool(title="Wait for DOM element state", annotations=READ_ONLY)
async def dom_wait(
    element_ref: str,
    state: str = "visible",
    timeout_ms: int | None = None,
) -> dict:
    """Wait until an element is attached, detached, visible, or hidden."""
    return await dom.wait(element_ref, state=state, timeout_ms=timeout_ms)


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


# ----------------------------
# EDA tools
# ----------------------------

@mcp.tool(title="Read EDA backend status", annotations=READ_ONLY)
def eda_info() -> dict:
    """Report EDA permissions and detected KiCad/Altium backends."""
    return eda.info()


@mcp.tool(title="Inspect an EDA project", annotations=READ_ONLY)
def eda_project_inspect(path: str) -> dict:
    """Inspect KiCad/Altium project files below one allowed project directory."""
    return eda.project_inspect(path)


@mcp.tool(title="Run KiCad ERC", annotations=WRITE_ADDITIVE)
def eda_kicad_erc(schematic_path: str, report_path: str | None = None) -> dict:
    """Run KiCad electrical-rules checking and write a report inside an allowed EDA root."""
    return eda.kicad_erc(schematic_path, report_path=report_path)


@mcp.tool(title="Run KiCad DRC", annotations=WRITE_ADDITIVE)
def eda_kicad_drc(pcb_path: str, report_path: str | None = None) -> dict:
    """Run KiCad design-rules checking and write a report inside an allowed EDA root."""
    return eda.kicad_drc(pcb_path, report_path=report_path)


@mcp.tool(title="Export KiCad Gerbers", annotations=WRITE_ADDITIVE)
def eda_kicad_export_gerbers(pcb_path: str, output_dir: str) -> dict:
    """Export fabrication Gerbers into an empty output directory.

    This is fail-closed unless eda.allow_manufacturing is explicitly enabled.
    """
    return eda.kicad_export_gerbers(pcb_path, output_dir)


# ----------------------------
# Batch/workflow tool
# ----------------------------

_BATCH_TOOL_NAMES = (
    "filesystem_list_roots",
    "filesystem_list",
    "filesystem_read_text",
    "filesystem_stat",
    "filesystem_search",
    "filesystem_create_directory",
    "filesystem_write_text",
    "filesystem_replace_in_file",
    "filesystem_move",
    "filesystem_delete",
    "system_info",
    "system_resource_snapshot",
    "process_list",
    "process_details",
    "process_start",
    "process_stop",
    "powershell_info",
    "powershell_run",
    "screen_info",
    "window_list",
    "window_active",
    "window_details",
    "window_focus",
    "window_set_state",
    "window_close",
    "mouse_position",
    "mouse_move",
    "mouse_click",
    "mouse_scroll",
    "keyboard_write",
    "keyboard_press",
    "keyboard_hotkey",
    "text_replace_all",
    "uia_list",
    "uia_find",
    "uia_details",
    "uia_focus",
    "uia_invoke",
    "uia_set_value",
    "uia_toggle",
    "uia_select",
    "uia_expand_collapse",
    "uia_scroll",
    "uia_scroll_into_view",
    "uia_set_range_value",
    "uia_text_select_all",
    "uia_window_action",
    "dom_list_tabs",
    "dom_open_tab",
    "dom_navigate",
    "dom_close_tab",
    "dom_find",
    "dom_details",
    "dom_read_text",
    "dom_click",
    "dom_set_value",
    "dom_select",
    "dom_toggle",
    "dom_scroll_into_view",
    "dom_scroll",
    "dom_wait",
    "clipboard_read_text",
    "clipboard_write_text",
    "eda_info",
    "eda_project_inspect",
    "eda_kicad_erc",
    "eda_kicad_drc",
    "eda_kicad_export_gerbers",
)
_BATCH_TOOL_HANDLERS = {name: globals()[name] for name in _BATCH_TOOL_NAMES}

_BATCH_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "filesystem_list_roots",
        "filesystem_list",
        "filesystem_read_text",
        "filesystem_stat",
        "filesystem_search",
        "system_info",
        "system_resource_snapshot",
        "process_list",
        "process_details",
        "powershell_info",
        "screen_info",
        "window_list",
        "window_active",
        "window_details",
        "mouse_position",
        "uia_list",
        "uia_find",
        "uia_details",
        "dom_list_tabs",
        "dom_find",
        "dom_details",
        "dom_read_text",
        "dom_wait",
        "clipboard_read_text",
        "eda_info",
        "eda_project_inspect",
    }
)


def _batch_contains_reference(value) -> bool:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return True
        return any(_batch_contains_reference(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_batch_contains_reference(item) for item in value)
    return False


@mcp.tool(title="Execute a read-only actuator batch", annotations=READ_ONLY)
async def batch_readonly(actions: list[dict]) -> dict:
    """Execute read-only actuator actions in one MCP round trip.

    Thread-safe read-only tools run concurrently. Read-only tools backed by
    window/UIA/DOM providers are automatically serialized so they do not make
    the entire batch fail merely because they are unsafe for parallel threads.
    Runtime failures stay isolated per action because stop_on_error is disabled.
    This remains the direct local path; use ChatGPT-Orchestrator only when delegation,
    independent review/rework, or recovery materially benefits the task.
    Write/destructive tools and $ref dependencies remain rejected fail-closed.
    """
    tool_names = [
        str(action.get("tool", "")).strip()
        for action in actions
        if isinstance(action, dict)
    ]
    disallowed = sorted({name for name in tool_names if name not in _BATCH_READ_ONLY_TOOL_NAMES})
    if disallowed:
        runtime.audit.record(
            "batch_readonly_rejected",
            requested=len(actions) if isinstance(actions, list) else None,
            tools=tool_names,
            error_type="PermissionError",
            error="read-only batch rejected non-read-only tools: " + ", ".join(disallowed),
        )
        raise PermissionError(
            "batch_readonly accepts only read-only tools; not allowed: " + ", ".join(disallowed)
        )

    if _batch_contains_reference(actions):
        raise ValueError("$ref dependencies are not supported by batch_readonly.")

    execution_mode = (
        "parallel_readonly"
        if all(name in PARALLEL_READ_ONLY_TOOLS for name in tool_names)
        else "sequential"
    )
    try:
        result = await execute_batch(
            actions,
            _BATCH_TOOL_HANDLERS,
            stop_on_error=False,
            mode=execution_mode,
        )
    except Exception as exc:
        runtime.audit.record(
            "batch_readonly_rejected",
            requested=len(actions) if isinstance(actions, list) else None,
            tools=tool_names,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise

    result["read_only"] = True
    result["serialized"] = execution_mode == "sequential"
    runtime.audit.record(
        "batch_readonly",
        requested=result["requested"],
        completed=result["completed"],
        succeeded=result["succeeded"],
        failed=result["failed"],
        duration_ms=result["duration_ms"],
        tools=tool_names,
        execution_mode=execution_mode,
    )
    return result

@mcp.tool(title="Execute a local actuator batch/workflow", annotations=WRITE_DESTRUCTIVE)
async def batch_execute(
    actions: list[dict],
    stop_on_error: bool = True,
    mode: str = "sequential",
) -> dict:
    """Execute up to 25 actuator actions in one MCP round trip.

    sequential supports dependencies with {"$ref": "0.result.path"}.
    parallel_readonly runs only a conservative read-only allowlist.
    Each underlying tool keeps its existing permission, guard,
    verification, and audit behavior. MAIN should prefer this for direct local
    workflows; use ChatGPT-Orchestrator when independent workers, review/rework,
    parallel reasoning, or recovery are materially useful.
    """
    tool_names = [
        str(action.get("tool", ""))
        for action in actions
        if isinstance(action, dict)
    ]
    try:
        result = await execute_batch(
            actions,
            _BATCH_TOOL_HANDLERS,
            stop_on_error=stop_on_error,
            mode=mode,
        )
    except Exception as exc:
        runtime.audit.record(
            "batch_execute_rejected",
            mode=str(mode),
            requested=len(actions) if isinstance(actions, list) else None,
            tools=tool_names,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise

    runtime.audit.record(
        "batch_execute",
        mode=result["mode"],
        requested=result["requested"],
        completed=result["completed"],
        succeeded=result["succeeded"],
        failed=result["failed"],
        stopped_early=result["stopped_early"],
        duration_ms=result["duration_ms"],
        tools=tool_names,
    )
    return result

def main() -> None:
    options = run_options()
    dom_host.start()
    admin = AdminServer(runtime, dom_host=dom_host)
    admin.start()
    try:
        mcp.run(**options)
    finally:
        admin.stop()
        dom_host.stop()


if __name__ == "__main__":
    main()
