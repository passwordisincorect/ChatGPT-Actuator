from __future__ import annotations
import json
import os
from mcp.server.transport_security import TransportSecuritySettings
from .transport import run_options

from .secured_mcp import SecuredMCPServer
from mcp.types import ToolAnnotations

from .admin import AdminServer
from .dom_host import DOMAsyncProxy, DOMExecutionHost
from .batch import PARALLEL_READ_ONLY_TOOLS, execute_batch
from .routing_policy import RoutingPolicyEngine
from .runtime import RuntimeManager


runtime = RuntimeManager()
initial_config = runtime.config
routing_policy = RoutingPolicyEngine()

filesystem = runtime.proxy("filesystem")
process_system = runtime.proxy("process_system")
powershell = runtime.proxy("powershell")
screen = runtime.proxy("screen")
window = runtime.proxy("window")
clipboard = runtime.proxy("clipboard")
uia = runtime.proxy("uia")
dom_host = DOMExecutionHost(runtime)
dom = DOMAsyncProxy(dom_host)
eda = runtime.proxy("eda")

mcp = SecuredMCPServer(
    runtime=runtime,
    name=initial_config.name,
    version=initial_config.version,
    description="Local execution and durable background agents for ChatGPT. Prefer agent_start for autonomous file tasks and staged outputs, agent_submit for explicit tool/browser delegation, and batch tools for short direct local workflows. Use agent_status and agent_control to inspect or stop owned tasks. Background agents continue without an attached chat. ChatGPT-Orchestrator remains available for independent multi-worker analysis and parallel reasoning.",
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
def filesystem_read_text(path: str, max_bytes: int | None = None, proposed_content: str | None = None) -> dict:
    """Read UTF-8 text; optional proposed_content returns a bounded diff and checksums without writing."""
    return filesystem.read_text(path, max_bytes=max_bytes, proposed_content=proposed_content)


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
    expected_sha256: str | None = None,
    preview_sha256: str | None = None,
) -> dict:
    """Create UTF-8 text inside an allowed root. Existing-file overwrite requires overwrite=true and expected_sha256 from filesystem_read_text; optional preview_sha256 binds the reviewed path/baseline/content."""
    return filesystem.write_text(path, content, overwrite=overwrite, create_parents=create_parents, expected_sha256=expected_sha256, preview_sha256=preview_sha256)


@mcp.tool(title="Replace text in file", annotations=WRITE_DESTRUCTIVE)
def filesystem_replace_in_file(
    path: str,
    old_text: str,
    new_text: str,
    max_replacements: int = 1,
    expected_sha256: str | None = None,
) -> dict:
    """Replace exact text inside an allowed UTF-8 file. Requires expected_sha256 from filesystem_read_text; conflicts never overwrite the changed file."""
    return filesystem.replace_in_file(path, old_text, new_text, max_replacements=max_replacements, expected_sha256=expected_sha256)


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

@mcp.tool(title="Run PowerShell in an isolated background desktop", annotations=WRITE_ADDITIVE)
def powershell_run(script: str, cwd: str | None = None, timeout_seconds: float | None = None, attempt_key: str | None = None) -> dict:
    """Run with the Windows user's file rights on a private desktop, bounded Job and timeout. Child processes stop at request completion. An optional attempt_key prevents duplicate execution on retry."""
    limits=runtime.raw_config().get('security',{}).get('resource_limits')
    if attempt_key is None:return powershell.run(script,cwd,timeout_seconds,resource_limits=limits)
    import hashlib
    from .tool_policy import principal_context
    principal=principal_context.get();sessions=mcp.sessions
    fingerprint=hashlib.sha256(json.dumps([script,cwd,timeout_seconds],sort_keys=True).encode()).hexdigest()
    attempt=sessions.attempt_begin(principal.session,attempt_key,fingerprint,label='powershell')
    if not attempt['created']:return {'attempt_id':attempt['id'],'status':attempt['status'],'replayed':True,'result':attempt['result']}
    try:
        if not sessions.attempt_start(attempt['id']):
            result={'attempt_id':attempt['id'],'status':'cancelled','phase':'finished','cancelled':True,'dispatched':False,'replayed':False}
            sessions.attempt_finish(attempt['id'],'cancelled',result)
            return result
        result=powershell.run(script,cwd,timeout_seconds,resource_limits=limits,cancel=lambda:sessions.attempt_cancelled(attempt['id']))
        status='cancelled' if result['cancelled'] else 'timed_out' if result['timed_out'] else 'succeeded' if result['exit_code']==0 else 'failed'
        result.update(attempt_id=attempt['id'],status=status,phase='finished',replayed=False);sessions.attempt_finish(attempt['id'],status,result);return result
    except BaseException as exc:
        sessions.attempt_finish(attempt['id'],'failed',{'error_type':type(exc).__name__});raise




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








# ----------------------------
# Mouse tools
# ----------------------------









# ----------------------------
# Keyboard tools
# ----------------------------







# ----------------------------
# Verified text-control tools
# ----------------------------



# ----------------------------
# Windows UI Automation tools
# ----------------------------

from .uia_workspaces import UIAWorkspaces
uia_spaces=UIAWorkspaces(runtime,uia)

def _uia_owner():
    from .tool_policy import current_principal
    p=current_principal(runtime)
    return p.owner,mcp.sessions.require(p)

@mcp.tool(title="List private UIA application profiles", annotations=READ_ONLY)
def uia_workspace_profiles() -> dict:
    """List fixed owner-approved Win32 application profiles. Does not launch or attach the user's desktop."""
    return uia_spaces.profiles()

@mcp.tool(title="Open a private Win32 UIA workspace", annotations=WRITE_ADDITIVE)
def uia_workspace_open(profile: str = 'win32_controls') -> dict:
    """Launch a fixed approved app on a private desktop with a held Job. The default is a reference Win32 app; custom apps require owner security.uia_profiles configuration."""
    owner,sid=_uia_owner();key=uia_spaces.open(owner,sid,profile)
    try:
        item=uia_spaces.require(key,owner,sid)
        import psutil
        pid=item['process'].pid;started=psutil.Process(pid).create_time()
        mcp.sessions.bind(sid,'pid',[pid,started])
        ref=mcp.sessions.bind(sid,'uia_workspace',{'key':key,'pid':pid,'started':started})
        return {'workspace_ref':ref,'profile':profile,'mode':'private_desktop'}
    except BaseException:
        uia_spaces.close(key,owner,sid)
        raise

@mcp.tool(title="List windows in a private UIA workspace", annotations=READ_ONLY)
def uia_workspace_windows(workspace_ref: str) -> dict:
    """Return only windows whose process is still in this session's held UIA Job."""
    owner,sid=_uia_owner();return uia_spaces.windows(workspace_ref['key'],owner,sid)

@mcp.tool(title="Close a private UIA workspace", annotations=WRITE_STATEFUL)
def uia_workspace_close(workspace_ref: str) -> dict:
    """Close the owned app Job and private desktop; never terminates unrelated user applications."""
    owner,sid=_uia_owner();result=uia_spaces.close(workspace_ref['key'],owner,sid)
    mcp.sessions.release_uia_refs(sid,workspace_ref['key'])
    with mcp.sessions.db() as db:
        db.execute("DELETE FROM handles WHERE session=? AND kind='pid' AND value=?",(sid,json.dumps([workspace_ref['pid'],workspace_ref['started']],sort_keys=True)))
        db.execute("DELETE FROM handles WHERE session=? AND kind='uia_workspace' AND value=?",(sid,json.dumps(workspace_ref,sort_keys=True)))
    return result

@mcp.tool(title="Bind an owned private Win32 window", annotations=WRITE_STATEFUL)
def uia_control_bind(workspace_ref: str, target_hwnd: int) -> dict:
    """Bind a window inside the owned private UIA Job for 10 minutes; captures PID/start/class and refuses foreign windows."""
    owner,sid=_uia_owner();target=uia_spaces.bind(workspace_ref['key'],owner,sid,target_hwnd)
    ref=mcp.sessions.bind(sid,'uia_control_target',target)
    return {'target_ref':ref,'target_hwnd':target_hwnd,'expires':target['expires']}

@mcp.tool(title="Read a bound application UIA snapshot", annotations=READ_ONLY)
def uia_control_snapshot(target_ref: str, max_depth: int = 5, max_items: int = 100, root_ref: str | None = None, include_chrome: bool = False) -> dict:
    """Read the UIA tree of a session-bound desktop application or private workspace. Returns owned refs; no explicit focus or physical input."""
    owner,sid=_uia_owner();return uia_spaces.call(target_ref,owner,sid,'snapshot',max_depth=max_depth,max_items=max_items,include_hidden=True,content_only=not include_chrome,root_ref=root_ref)

@mcp.tool(title="Find one bound application UIA control", annotations=READ_ONLY)
def uia_control_find(target_ref: str, query: dict, max_depth: int = 8, root_ref: str | None = None, include_chrome: bool = False) -> dict:
    """Find exactly one element by name/automation_id/class_name/control_type/pattern/region in the bound desktop or private window. Ambiguous or truncated searches are refused; no values or physical input fallback."""
    owner,sid=_uia_owner();return uia_spaces.call(target_ref,owner,sid,"find_unique",query=query,max_depth=max_depth,root_ref=root_ref,include_chrome=include_chrome)

@mcp.tool(title="Read bound application UIA pattern states", annotations=READ_ONLY)
def uia_control_state(target_ref: str, element_ref: str) -> dict:
    """Read enabled, selected, toggle/expand, read-only and numeric range state. Text is SHA-256 only; password state omitted. Observation is not an atomic lock."""
    owner,sid=_uia_owner();return uia_spaces.call(target_ref,owner,sid,'state',read_ref=element_ref)

def _uia_control_action(target,method,element_ref,**kwargs):
    try:
        owner,sid=_uia_owner()
        result=uia_spaces.call(target,owner,sid,method,element_ref,**kwargs)
        if not target.get('desktop_control') and not result.get('verification',{}).get('foreground_unchanged',False):
            with mcp.sessions.db() as db:db.execute('DELETE FROM handles WHERE id=?',(target['lease_ref'],))
        return result
    except TimeoutError:
        with mcp.sessions.db() as db:db.execute('DELETE FROM handles WHERE id=?',(target['lease_ref'],))
        raise

@mcp.tool(title="Open a Windows application for UIA", annotations=WRITE_ADDITIVE)
def uia_app_open(application: str, arguments: list[str] | None = None) -> dict:
    """Launch a requested installed executable visibly without shell or physical input. Browsers must use DOM. Select a returned window and attach it before control; release never terminates the app."""
    from .desktop_apps import open_app
    return open_app(runtime,application,arguments or [])

@mcp.tool(title="Attach UIA to a selected desktop application", annotations=WRITE_STATEFUL)
def uia_desktop_attach(target_hwnd: int) -> dict:
    """Attach a selected existing application window for ten minutes. Allows provider focus effects, not physical input; browser windows must use DOM. Requires owner desktop UIA policy."""
    owner,sid=_uia_owner();target=uia_spaces.attach_desktop(owner,sid,target_hwnd)
    try:ref=mcp.sessions.bind(sid,'uia_control_target',target)
    except BaseException:
        uia_spaces.close(target['workspace'],owner,sid);raise
    return {'target_ref':ref,'target_hwnd':target_hwnd,'expires':target['expires'],'mode':'visible_desktop','physical_input_used':False}

@mcp.tool(title="Release a desktop UIA attachment", annotations=WRITE_STATEFUL)
def uia_desktop_release(target_ref: str) -> dict:
    """Release this session's desktop target and element references without closing or terminating the user's application."""
    owner,sid=_uia_owner()
    if not target_ref.get('desktop_control'):raise ValueError('Use workspace close for private targets')
    result=uia_spaces.close(target_ref['workspace'],owner,sid)
    mcp.sessions.release_uia_refs(sid,target_ref['workspace'])
    return result

@mcp.tool(title="Invoke a bound application UIA control", annotations=WRITE_DESTRUCTIVE)
def uia_invoke(target_ref: str, element_ref: str, precondition: dict | None = None, verification: dict | None = None, postcondition: dict | None = None) -> dict:
    """InvokePattern on a bound Win32 Button inside the owned private desktop. One dispatch, no focus/input fallback; invoked alone does not prove the business result."""
    return _uia_control_action(target_ref,'invoke',element_ref,precondition=precondition,verification=verification,postcondition=postcondition)

@mcp.tool(title="Control a bound desktop window through UIA", annotations=WRITE_DESTRUCTIVE)
def uia_control_window(target_ref: str, element_ref: str, action: str, precondition: dict | None = None) -> dict:
    """WindowPattern close/minimize/maximize/restore on the selected desktop window root; observe state without keyboard/mouse. Close can show the app's save dialog; does not force kill."""
    return _uia_control_action(target_ref,'window_action',element_ref,action=action,precondition=precondition)

@mcp.tool(title="Focus a selected desktop control through UIA", annotations=WRITE_STATEFUL)
def uia_control_focus(target_ref: str, element_ref: str, precondition: dict | None = None) -> dict:
    """Explicit IUIAutomationElement.SetFocus on a keyboard-focusable selected desktop app control. May activate its window; no pywinauto focus workaround, physical keys or mouse, and no automatic focus fallback."""
    return _uia_control_action(target_ref,'focus',element_ref,precondition=precondition)

@mcp.tool(title="Scroll a bound desktop application through UIA", annotations=WRITE_STATEFUL)
def uia_control_scroll(target_ref: str, element_ref: str, direction: str, amount: str = 'page', count: int = 1, precondition: dict | None = None) -> dict:
    """ScrollPattern up/down/left/right, line/page, at most ten repeats; desktop target required; no physical input fallback."""
    return _uia_control_action(target_ref,'scroll',element_ref,direction=direction,amount=amount,count=count,precondition=precondition)

@mcp.tool(title="Reveal a bound desktop UIA item", annotations=WRITE_STATEFUL)
def uia_control_reveal(target_ref: str, element_ref: str, precondition: dict | None = None) -> dict:
    """ScrollItemPattern reveals a selected item in its desktop app; no mouse, keys or explicit focus fallback."""
    return _uia_control_action(target_ref,'scroll_into_view',element_ref,precondition=precondition)

@mcp.tool(title="List related application windows and dialogs", annotations=READ_ONLY)
def uia_desktop_windows(target_ref: str) -> dict:
    """Read visible windows of the bound app and verified descendants, bounded to 32 processes/four levels. Observe PID/start/parent identity; use fresh attachment before control. Browser windows recommend DOM. Never auto-attaches, approves dialogs or grants control based only on parent PID."""
    from .desktop_apps import require
    from .uia_control import validate
    require(runtime)
    if not target_ref.get('desktop_control'):raise ValueError('Desktop target required')
    validate(target_ref)
    import win32gui
    from .uia_followup import inventory
    capture=inventory(target_ref)
    windows=[{**row,'target_hwnd':row['hwnd'],'class_name':row['class'],'owner_hwnd':win32gui.GetWindow(row['hwnd'],4)} for row in capture['windows'].values()]
    return {'windows':windows,'automatic_attach':False,'process_limit':32,'depth_limit':4,'process_inventory_truncated':capture['process_inventory_truncated'],'observed_processes':capture['observed_processes']}

@mcp.tool(title="Recover a selected desktop UIA target", annotations=WRITE_STATEFUL)
def uia_desktop_recover(target_ref: str) -> dict:
    """Rebind a previous owned desktop target, including expired leases, only if HWND/PID/start/class still match. Return fresh refs; replay zero actions. Refuses replaced or closed windows."""
    from .desktop_apps import require
    from .uia_control import identity
    require(runtime);owner,sid=_uia_owner()
    if not target_ref.get('desktop_control'):raise ValueError('Desktop target required')
    current=identity(target_ref['hwnd'])
    if any(current[key]!=target_ref[key] for key in ('pid','started','class')):raise PermissionError('Window identity changed; select it again')
    old=target_ref['workspace']
    if old in uia_spaces.items:uia_spaces.close(old,owner,sid)
    mcp.sessions.release_uia_refs(sid,old)
    target=uia_spaces.attach_desktop(owner,sid,current['hwnd'])
    try:
        mcp.sessions.bind(sid,'hwnd',current['hwnd']);ref=mcp.sessions.bind(sid,'uia_control_target',target)
    except BaseException:
        uia_spaces.close(target['workspace'],owner,sid);raise
    return {'target_ref':ref,'target_hwnd':current['hwnd'],'expires':target['expires'],'recovered':True,'actions_replayed':0,'references':'fresh'}

@mcp.tool(title="Set a standard Win32 Edit value", annotations=WRITE_DESTRUCTIVE)
def uia_set_value(target_ref: str, element_ref: str, value: str, precondition: dict | None = None, verification: dict | None = None, postcondition: dict | None = None) -> dict:
    """ValuePattern on a bound Win32 Edit inside the owned private desktop; passwords/read-only/unsupported controls refused. Returns verification without value contents."""
    return _uia_control_action(target_ref,'set_value',element_ref,value=value,precondition=precondition,verification=verification,postcondition=postcondition)

@mcp.tool(title="Toggle a standard Win32 checkbox", annotations=WRITE_DESTRUCTIVE)
def uia_toggle(target_ref: str, element_ref: str, precondition: dict | None = None, verification: dict | None = None, postcondition: dict | None = None) -> dict:
    """TogglePattern on a bound standard Win32 Button inside the owned private desktop. Requires a supported pattern, observes state change and never retries automatically."""
    return _uia_control_action(target_ref,'toggle',element_ref,precondition=precondition,verification=verification,postcondition=postcondition)

@mcp.tool(title="Select an owned Win32 item", annotations=WRITE_DESTRUCTIVE)
def uia_control_select(target_ref: str, element_ref: str, precondition: dict | None = None, verification: dict | None = None) -> dict:
    """SelectionItemPattern in a bound private Win32 List, ComboBox, Tree or Tab. No focus/input fallback; observes selected state."""
    return _uia_control_action(target_ref,'select',element_ref,precondition=precondition,verification=verification)

@mcp.tool(title="Expand or collapse an owned Win32 control", annotations=WRITE_DESTRUCTIVE)
def uia_control_expand_collapse(target_ref: str, element_ref: str, action: str, precondition: dict | None = None, verification: dict | None = None) -> dict:
    """ExpandCollapsePattern for a private Win32 ComboBox or Tree item. action is expand or collapse; never retries mutation."""
    return _uia_control_action(target_ref,'expand_collapse',element_ref,action=action,precondition=precondition,verification=verification)

@mcp.tool(title="Set an owned Win32 range value", annotations=WRITE_DESTRUCTIVE)
def uia_control_set_range_value(target_ref: str, element_ref: str, value: float, precondition: dict | None = None, verification: dict | None = None) -> dict:
    """RangeValuePattern for a private Win32 Slider/Spinner; rejects read-only, nonfinite and out-of-range values before dispatch."""
    return _uia_control_action(target_ref,'set_range_value',element_ref,value=value,precondition=precondition,verification=verification)


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


@mcp.tool(title="Snapshot one UI Automation window", annotations=READ_ONLY)
def uia_snapshot(target_hwnd: int, max_depth: int | None = None,
                 max_items: int | None = None, include_hidden: bool = False) -> dict:
    """Read a bounded window accessibility snapshot with UIA refs; no focus or values."""
    return uia.snapshot(target_hwnd, max_depth, max_items, include_hidden)


@mcp.tool(title="Find UI Automation controls", annotations=READ_ONLY)
def uia_snapshot_diff(target_hwnd:int,previous_snapshot_ref:str,max_depth:int|None=None,
                      max_items:int|None=None,include_hidden:bool=False,compact:bool=False,max_changes:int=100)->dict:
    """Compare session-owned UIA snapshots; partial captures cannot prove removals."""
    return uia.snapshot_diff(target_hwnd,previous_snapshot_ref,max_depth,max_items,include_hidden,compact,max_changes)


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

























# ----------------------------
# Browser DOM tools (Playwright + CDP)
# ----------------------------

def _dom_progress():
    from .dom_progress import DOMProgress
    from .tool_policy import current_principal
    return DOMProgress(runtime.config_path.parent/'dom-progress.sqlite3'),current_principal(runtime).owner

def _require_browser_default(required):
    if type(required) is not bool:raise ValueError('Boolean default-session guard required')
    if not required:return
    from .tool_policy import current_principal
    from .automation_errors import PolicyDenied
    principal=current_principal(runtime)
    with mcp.sessions.lock:
        if mcp.sessions.defaults.get(principal.owner)!=principal.session:raise PolicyDenied('wrong_owner')

@mcp.tool(title="Inspect owned DOM operations",annotations=READ_ONLY)
async def dom_operation_status(include_finished:bool=False)->dict:
    """List this owner's current-session DOM calls even when operation quota is full. Arguments and page contents are omitted."""
    return await dom.operation_status(include_finished=include_finished)

@mcp.tool(title="Cancel an owned DOM operation",annotations=WRITE_STATEFUL)
async def dom_operation_cancel(operation_id:str)->dict:
    """Request cancellation of one owned current-session DOM call. Does not undo or replay a dispatched action. Cleanup may remain pending."""
    return await dom.operation_cancel(operation_id=operation_id)

@mcp.tool(title="Recover a stalled owned DOM transport",annotations=WRITE_STATEFUL)
async def dom_operation_recover(operation_id:str)->dict:
    """After cancellation remains pending, terminate only an identified owned driver/job. Refuses shared managed sessions, preserves the selected user browser, never replays actions."""
    return await dom.operation_recover(operation_id=operation_id)

@mcp.tool(title="Review supplied URLs with automatic checkpoints",annotations=WRITE_STATEFUL)
async def dom_review_run(task_key:str,urls:list[str],max_items:int=3,detail_selector:str|None=None,variant_selector:str|None=None)->dict:
    """Review up to three supplied URLs per call, checkpoint each requested stage, skip completed stages and refuse automatic retry of blocked/unknown results. Uses fresh owned tabs; never selects variants or buys items."""
    from .dom_review import run
    progress,owner=_dom_progress()
    return await run(progress,owner,task_key,urls,dom,runtime.get_service('dom')._require_allowed_url,max_items,detail_selector,variant_selector)

@mcp.tool(title="Manage remembered browser choices",annotations=WRITE_STATEFUL)
async def dom_browser_choice_manage(name:str,action:str,expected_revision:int,new_name:str|None=None,expected_url:str|None=None)->dict:
    """Rename/forget an owned bookmark or refresh the expected URL after verifying the same CDP target. Metadata only; never enables debugging, restarts browsers or exports cookies."""
    progress,owner=_dom_progress()
    if action=='refresh':
        if new_name is not None or expected_url is None:raise ValueError('Refresh requires only expected_url')
        from .dom_progress import public_url
        public_url(expected_url);runtime.get_service('dom')._require_allowed_url(expected_url)
        choice=progress.choice(owner,name)
        inventory=await dom.browser_sessions([choice['cdp_url']])
        if sum(t.get('tab_id')==choice['tab_id'] and t.get('url')==expected_url for s in inventory['sessions'] for t in s.get('tabs',[]))!=1:raise ValueError('Saved target ID/URL not found')
        return progress.remember(owner,name,choice['cdp_url'],choice['tab_id'],expected_url,expected_revision)
    if expected_url is not None:raise ValueError('Unexpected URL for bookmark action')
    return progress.manage(owner,name,action,expected_revision,new_name)

@mcp.tool(title="Read a DOM review checkpoint",annotations=READ_ONLY)
def dom_review_status(task_key:str)->dict:
    """Read owner-scoped durable checkpoints. Saved refs are not reused and mutations are never automatically replayed."""
    progress,owner=_dom_progress()
    from .cart_checkpoint import status
    return {**progress.status(owner,task_key),'cart_checkpoints':status(progress,owner,task_key)}

@mcp.tool(title="Save a DOM review checkpoint",annotations=WRITE_STATEFUL)
def dom_review_record(task_key:str,item_key:str,url:str,stage:str,status:str,summary:str,expected_revision:int=0,cart:dict|None=None)->dict:
    """Save caller-reported progress. Cart observe/reserve/reconcile/resolve persist exact identity/quantity and CAS. Reserving or consuming a zero-quantity addition requires explicit consistent coverage counters (loaded_rows, declared_rows, matching_rows, has_more, loading); complete=true alone is insufficient. Reserve never clicks; unresolved attempts block another reservation. No purchase proof or automatic replay."""
    progress,owner=_dom_progress()
    if cart is not None:
        if stage!='action':raise ValueError('Cart checkpoints require action stage')
        if cart.get('event') in {'reconcile','resolve'} and dom_host.call('operation_status')['operations']:raise ValueError('Wait for DOM operations before reconciliation')
        from .cart_checkpoint import record
        return record(progress,owner,task_key,item_key,url,expected_revision,cart)
    return progress.record(owner,task_key,item_key,url,stage,status,summary,expected_revision)

@mcp.tool(title="List remembered selected browser tabs",annotations=READ_ONLY)
def dom_browser_choices()->dict:
    """List this owner's explicit browser bookmarks. No cookies or passwords are stored."""
    progress,owner=_dom_progress();return progress.choices(owner)

@mcp.tool(title="Remember an explicitly selected browser tab",annotations=WRITE_STATEFUL)
async def dom_browser_remember(name:str,cdp_url:str,tab_id:str,expected_url:str,expected_revision:int=0)->dict:
    """Validate an existing loopback CDP target and save its ID/URL for explicit reuse. Does not enable debugging, log in, or copy cookies."""
    from .dom_progress import key,public_url
    key(name);public_url(expected_url)
    # Read-only inventory proves the exact target before persisting metadata.
    inventory=await dom.browser_sessions([cdp_url])
    tabs=[tab for session in inventory['sessions'] for tab in session.get('tabs',[]) if tab.get('tab_id')==tab_id and tab.get('url')==expected_url]
    if len(tabs)!=1:raise ValueError('Selected CDP target ID/URL not available')
    progress,owner=_dom_progress();return progress.remember(owner,name,cdp_url,tab_id,expected_url,expected_revision)

@mcp.tool(title="Reuse a remembered selected browser tab",annotations=WRITE_STATEFUL)
async def dom_browser_resume(name:str,require_default_session:bool=False)->dict:
    """Attach the exact saved target ID and URL. Refuses changed/missing tabs; does not restart browsers or replay navigation."""
    _require_browser_default(require_default_session)
    progress,owner=_dom_progress();choice=progress.choice(owner,name)
    return await dom.attach_browser(choice['cdp_url'],choice['expected_url'],choice['tab_id'])

@mcp.tool(title="Observe DOM content evidence",annotations=READ_ONLY)
async def dom_observe(tab_ref:str,detail_selector:str|None=None,variant_selector:str|None=None,max_chars:int=6000,diagnostics:bool=False,cart_probe:dict|None=None)->dict:
    """Read explicitly selected regions. diagnostics reads bounded resource/error metadata in the existing connection, without navigation, retries or cookie export.

    Optional cart_probe requires five explicit selectors: row_selector,
    footer_selector, more_selector, loading_selector, match_selector. It reports
    loaded-list coverage; absence there never proves absence in the whole account.
    Handoff information requires fresh observations after manual verification;
    it never resumes actions automatically or certifies shopping success.
    """
    import asyncio
    if not 1<=max_chars<=12000:raise ValueError('max_chars requires 1..12000')
    if cart_probe is not None:
        from .cart_coverage import validate
        validate(cart_probe)
    result={'details':[],'variants':[],'evidence':{'level':'title_only','business_outcome_verified':False,'technical_suitability_verified':False}}
    async with asyncio.timeout(25):
        tabs=await dom.list_tabs();tab=next((t for t in tabs['tabs'] if t['tab_ref']==tab_ref),None)
        if tab is None:raise LookupError('Owned tab unavailable')
        result['title']=tab.get('title');result['url']=tab.get('url');result['browser_binding']=tab.get('binding')
        from .dom_content_state import observe
        result['content_state'] = await observe(dom, tab_ref, result['title'], result['url'])
        from .dom_diagnostics import handoff
        result['handoff']=handoff(result['content_state'],tab_ref)
        if diagnostics:result['diagnostics']=await dom.page_diagnostics(tab_ref)
        if cart_probe is not None:result['cart_coverage']=await dom.cart_coverage(tab_ref,cart_probe)
        for field,selector in [('details',detail_selector),('variants',variant_selector)]:
            if not selector:continue
            found=await dom.find(tab_ref,selector=selector,max_items=8)
            for element in found['matches']:
                text=await dom.read_text(element['element_ref'],max_chars=max_chars)
                result[field].append(text)
        result['evidence']['level']='variants_observed' if result['variants'] else 'detail_observed' if result['details'] else 'title_only'
    return result

@mcp.tool(title="Select a variant and observe resulting information", annotations=WRITE_DESTRUCTIVE)
async def dom_variant_select(tab_ref:str,element_ref:str,method:str,selected_selector:str,expected_text:str,value:str|None=None,price_selector:str|None=None,availability_selector:str|None=None)->dict:
    """Perform one explicit variant selection/click/toggle and observe exact selected text, price and availability regions. Validate observation selectors before mutation. Never buys, retries or certifies product suitability."""
    from .dom_variant import choose
    return await choose(dom,tab_ref,element_ref,method,value,selected_selector,expected_text,price_selector,availability_selector)

@mcp.tool(title="List allowed Edge DOM tabs", annotations=READ_ONLY)
def automation_status()->dict:
    """Read worker counters and native readiness without UI payloads or refs."""
    from .automation_health import snapshot
    return snapshot(runtime.config_path.parent.parent)


@mcp.tool(title="List allowed Edge DOM tabs", annotations=READ_ONLY)
async def dom_list_tabs() -> dict:
    """List Edge tabs visible through the configured CDP session and allowed host list."""
    return await dom.list_tabs()

@mcp.tool(title="Attach DOM to a selected browser tab", annotations=WRITE_STATEFUL)
async def dom_browser_attach(cdp_url: str, tab_url: str | None = None, tab_id: str | None = None,require_default_session:bool=False) -> dict:
    """Attach one uniquely matching HTTP/HTTPS page through an enabled loopback CDP endpoint. cdp_url accepts HTTP root or ws://loopback:port/devtools/browser/id for Edge approval mode; browser permission remains required, with defaults preserved. Does not enable debugging, attach other pages, or restart the browser."""
    _require_browser_default(require_default_session)
    return await dom.attach_browser(cdp_url,tab_url,tab_id)

@mcp.tool(title="Find available local DOM browser sessions", annotations=READ_ONLY)
async def dom_browser_sessions(cdp_urls: list[str] | None = None) -> dict:
    """List bounded HTTP/HTTPS page metadata at explicit loopback CDP endpoints. Reuses only this session's selected/owned pages on a leased approval socket; other leases are excluded. Unattached sockets are not connected or HTTP-probed, and require explicit selected-tab attachment. Default HTTP probes are9222/9223. No debugger enablement, new browser connection or restart."""
    return await dom.browser_sessions(cdp_urls)

@mcp.tool(title="Recover a chosen DOM tab without replay", annotations=WRITE_STATEFUL)
async def dom_browser_recover(cdp_url: str, tab_id: str, expected_url: str) -> dict:
    """Explicitly attach the existing target ID only when its URL also matches. Old session must be detached first. Returns fresh refs; never navigates or replays clicks."""
    return await dom.recover_browser(cdp_url,tab_id,expected_url)

@mcp.tool(title="Detach the DOM browser session", annotations=WRITE_STATEFUL)
async def dom_browser_detach() -> dict:
    """Release DOM references and disconnect. Existing attached user tabs remain open; newly created owned tabs close."""
    owner,sid=_uia_owner();result=await dom.release_session(owner,sid)
    with mcp.sessions.db() as db:
        for ref, in db.execute("SELECT id FROM handles WHERE session=? AND kind LIKE 'dom_%'",(sid,)).fetchall():mcp.sessions.remember_retired(db,sid,ref,'lease_expired')
        db.execute("DELETE FROM handles WHERE session=? AND kind LIKE 'dom_%'",(sid,))
    return result


@mcp.tool(title="Open allowed Edge DOM tab", annotations=WRITE_ADDITIVE)
async def dom_open_tab(url: str, timeout_ms: int | None = None,browser_mode:str='current') -> dict:
    """Open one allowed URL in the currently selected browser. With no selection, refuse; browser_mode=managed explicitly opts into a separate ephemeral profile. Never silently reuse another owner's browser or fall back from a disconnected personal profile."""
    return await dom.open_tab(url, timeout_ms=timeout_ms,browser_mode=browser_mode)


@mcp.tool(title="Navigate Edge DOM tab", annotations=WRITE_DESTRUCTIVE)
async def dom_navigate(
    tab_ref: str,
    url: str,
    timeout_ms: int | None = None,
    verification: dict | None = None,
) -> dict:
    """Navigate an existing stable DOM tab reference to another allowed URL."""
    return await dom.navigate(tab_ref, url, timeout_ms=timeout_ms,verification=verification)


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
    frame_ref: str | None = None,
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
        frame_ref=frame_ref,
    )


@mcp.tool(title="Snapshot owned headless DOM", annotations=READ_ONLY)
async def dom_snapshot(tab_ref: str, max_items: int = 100,
                       max_depth: int = 12, include_hidden: bool = False,
                       include_frames: bool = False, include_shadow: bool = False, root_selector: str | None = None) -> dict:
    """Read bounded main-document DOM structure and labels, without form values.

    Frames and open shadow roots are optional; frame hosts remain allowlisted.
    """
    return await dom.snapshot(tab_ref, max_items, max_depth, include_hidden,
                              include_frames, include_shadow,root_selector)


@mcp.tool(title="Snapshot Chromium accessibility", annotations=READ_ONLY)
async def dom_snapshot_diff(tab_ref:str,previous_snapshot_ref:str,max_items:int=100,
                            max_depth:int=12,include_hidden:bool=False,
                            include_frames:bool=False,include_shadow:bool=False,root_selector:str|None=None,
                            compact:bool=False,max_changes:int=100)->dict:
    """Return changed DOM nodes; history is bound to Work Session and tab."""
    return await dom.snapshot_diff(tab_ref,previous_snapshot_ref,max_items,max_depth,
                                   include_hidden,include_frames,include_shadow,root_selector,compact,max_changes)


@mcp.tool(title="Snapshot Chromium accessibility", annotations=READ_ONLY)
async def dom_accessibility_snapshot(tab_ref: str, max_items: int = 100,
                                     max_depth: int = 12, include_frames: bool = False) -> dict:
    """Read bounded Chromium-computed accessible roles/names, excluding AX values."""
    return await dom.accessibility_snapshot(tab_ref, max_items, max_depth, include_frames)


@mcp.tool(title="Get DOM element details", annotations=READ_ONLY)
async def dom_details(element_ref: str) -> dict:
    """Return semantic DOM properties and attributes for an opaque element_ref."""
    return await dom.details(element_ref)


@mcp.tool(title="Read DOM element text", annotations=READ_ONLY)
async def dom_read_text(element_ref: str, max_chars: int | None = None) -> dict:
    """Read rendered/text content without using keyboard selection or clipboard."""
    return await dom.read_text(element_ref, max_chars=max_chars)


@mcp.tool(title="Click DOM element", annotations=WRITE_DESTRUCTIVE)
async def dom_click(element_ref: str, timeout_ms: int | None = None, expected_selector: str | None = None, verification: dict | None = None, precondition: dict | None = None, popup_timeout_ms: int = 0, cart_checkpoint:dict|None=None) -> dict:
    """One DOM activation without physical input or replay. Known Shopee Add to Cart buttons require cart_checkpoint={task_key,item_key,attempt_id,expected_revision} from dom_review_record observe/reserve; page product and quantity must match. Reconcile the actual cart afterward; click success is not cart proof. Verification supports selector/state/text_contains/stable_ms/timeout_ms/wait_for_match."""
    receipt=None
    if cart_checkpoint is not None:
        progress,owner=_dom_progress()
        from .cart_checkpoint import claim
        try:receipt=claim(progress,owner,cart_checkpoint)
        except ValueError:
            from .automation_errors import CartCheckpointRequired
            raise CartCheckpointRequired('Reservation missing, stale or consumed; inspect cart') from None
    from .cart_checkpoint import ConsumedReservation
    result=await dom.click(element_ref, timeout_ms=timeout_ms,expected_selector=expected_selector,verification=verification,precondition=precondition,popup_timeout_ms=popup_timeout_ms,cart_identity=ConsumedReservation(receipt['identity'],receipt['delta']) if receipt else None)
    if receipt is not None:result['cart_checkpoint']={'task_key':receipt['task_key'],'item_key':receipt['item_key'],'attempt_id':receipt['attempt_id'],'revision':receipt['revision'],'state':'unknown','next_action':'Read the same cart identity and reconcile actual quantity; click completion is not cart proof','automatic_replay':False}
    return result


@mcp.tool(title="Set DOM form value", annotations=WRITE_DESTRUCTIVE)
async def dom_set_value(
    element_ref: str,
    value: str,
    timeout_ms: int | None = None,
    verification: dict | None = None,
    precondition: dict | None = None,
) -> dict:
    """Fill an input/textarea through Playwright; no physical keyboard input is used. Optional verification sets stable_ms, timeout_ms and wait_for_match."""
    return await dom.set_value(element_ref, value=value, timeout_ms=timeout_ms,verification=verification,precondition=precondition)


@mcp.tool(title="Select DOM option", annotations=WRITE_DESTRUCTIVE)
async def dom_select(
    element_ref: str,
    value: str | None = None,
    label: str | None = None,
    timeout_ms: int | None = None,
    verification: dict | None = None,
    precondition: dict | None = None,
) -> dict:
    """Select one HTML option by value or label."""
    return await dom.select(
        element_ref,
        value=value,
        label=label,
        timeout_ms=timeout_ms,
        verification=verification,
        precondition=precondition,
    )


@mcp.tool(title="Toggle DOM checkbox", annotations=WRITE_DESTRUCTIVE)
async def dom_toggle(
    element_ref: str,
    checked: bool | None = None,
    timeout_ms: int | None = None,
    verification: dict | None = None,
    precondition: dict | None = None,
) -> dict:
    """Check, uncheck, or invert a checkbox/radio-like control semantically."""
    return await dom.toggle(element_ref, checked=checked, timeout_ms=timeout_ms,verification=verification,precondition=precondition)


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


@mcp.tool(title="Inspect an Altium symbol library", annotations=WRITE_STATEFUL)
def eda_altium_library_inspect(library_path: str) -> dict:
    """Read real symbol/part/pin geometry from an authorized .SchLib/.IntLib using AD26.

    May launch Altium and create an adapter cache; never edits the source library.
    """
    return eda.altium_library_inspect(library_path)


@mcp.tool(title="Create an Altium schematic plan", annotations=WRITE_STATEFUL)
def eda_altium_sch_plan_create(project_dir: str, name: str, components: list[dict], nets: list[dict],
                              title: str = "", expected_revision: int | None = None) -> dict:
    """Create/revise one-sheet Altium IR. Components: reference,library_path,symbol,value,
    optional part,x,y,rotation. Nets: name,pins:[{reference,pin}]. Coordinates are mils.
    Inspects libraries through AD26; revising requires matching expected_revision.
    """
    return eda.altium_plan_create(project_dir, name, components, nets, title, expected_revision)


@mcp.tool(title="Validate an Altium schematic plan", annotations=WRITE_STATEFUL)
def eda_altium_sch_validate(ir_path: str) -> dict:
    """Validate symbol pins, units and named nets; may inspect libraries through AD26."""
    return eda.altium_validate(ir_path)


@mcp.tool(title="Create and verify an Altium schematic", annotations=WRITE_ADDITIVE)
def eda_altium_sch_materialize(ir_path: str, request_key: str, output_dir: str | None = None) -> dict:
    """Generate a new .PrjPcb/.SchDoc with short wires and net labels in AD26.

    Publishes a new directory only after saved-file component/net read-back matches.
    Reuse request_key to inspect the same operation; unconfirmed delivery never replays.
    """
    return eda.altium_materialize(ir_path, request_key, output_dir)


@mcp.tool(title="Verify saved Altium connectivity", annotations=WRITE_STATEFUL)
def eda_altium_sch_verify(ir_path: str, project_path: str) -> dict:
    """Reopen, compile and compare saved Altium project connectivity with Circuit IR."""
    return eda.altium_verify(ir_path, project_path)


@mcp.tool(title="Read an Altium operation", annotations=READ_ONLY)
def eda_altium_operation_status(operation_id: str) -> dict:
    """Read an owned operation without replay or publication; resume materialize to publish."""
    return eda.altium_operation_status(operation_id)


@mcp.tool(title="Inspect an existing Altium schematic", annotations=WRITE_STATEFUL)
def eda_altium_sch_inspect(project_path: str) -> dict:
    """Compile and snapshot an authorized standalone one-sheet .PrjPcb project using AD26."""
    return eda.altium_sch_inspect(project_path)


@mcp.tool(title="Preview edits to an existing Altium schematic", annotations=WRITE_STATEFUL)
def eda_altium_sch_edit_preview(snapshot_id: str, changes: list[dict], output_dir: str,
                                expected_connectivity: list[dict] | None = None,
                                intent: dict | None = None) -> dict:
    """Validate edits against an owned snapshot without changing schematic files.

    Topology-changing edits require the complete expected pin-to-net table.
    Coordinates in change objects are mils.
    """
    return eda.altium_sch_edit_preview(snapshot_id, changes, output_dir, expected_connectivity, intent)


@mcp.tool(title="Apply verified edits to a copy of an Altium schematic", annotations=WRITE_DESTRUCTIVE)
def eda_altium_sch_edit_apply(snapshot_id: str, changes: list[dict], output_dir: str,
                              preview_digest: str, request_key: str,
                              expected_connectivity: list[dict] | None = None,
                              intent: dict | None = None) -> dict:
    """Apply the exact preview to a new output directory, then reopen, compile and verify it."""
    return eda.altium_sch_edit_apply(snapshot_id, changes, output_dir, preview_digest, request_key,
                                     expected_connectivity, intent)


@mcp.tool(title="Check Altium pin-to-net contracts", annotations=READ_ONLY)
def eda_altium_sch_check_pin_contracts(snapshot_id: str, contracts: list[dict],
                                       require_all_pins: bool = True) -> dict:
    """Check each expected pin net or intentional no-connect with a reason."""
    return eda.altium_sch_check_pin_contracts(snapshot_id, contracts, require_all_pins)


@mcp.tool(title="Compare Altium schematic connectivity", annotations=READ_ONLY)
def eda_altium_sch_diff_connectivity(before_snapshot_id: str, after_snapshot_id: str) -> dict:
    """Report added, removed, and reassigned pins between owned snapshots."""
    return eda.altium_sch_diff_connectivity(before_snapshot_id, after_snapshot_id)


@mcp.tool(title="Lint an Altium schematic snapshot", annotations=READ_ONLY)
def eda_altium_sch_lint(snapshot_id: str, intent: dict | None = None) -> dict:
    """Run structural and declared electrical-intent checks on an owned snapshot."""
    return eda.altium_sch_lint(snapshot_id, intent)


@mcp.tool(title="Run Altium schematic ERC", annotations=WRITE_STATEFUL)
def eda_altium_sch_erc(project_path: str) -> dict:
    """Compile the authorized Altium project and read native ERC violations."""
    return eda.altium_sch_erc(project_path)


@mcp.tool(title="Commit a verified Altium schematic copy", annotations=WRITE_DESTRUCTIVE)
def eda_altium_sch_edit_commit(output_dir: str, request_key: str) -> dict:
    """Replace the clean source sheet with its verified copy after source-hash checks and backup."""
    return eda.altium_sch_edit_commit(output_dir, request_key)


@mcp.tool(title="Restore an Altium schematic commit", annotations=WRITE_DESTRUCTIVE)
def eda_altium_sch_edit_restore(commit_id: str, request_key: str) -> dict:
    """Restore a committed source backup only when source and backup hashes match."""
    return eda.altium_sch_edit_restore(commit_id, request_key)


@mcp.tool(title="Create schematic design plan", annotations=WRITE_ADDITIVE)
def eda_sch_plan_create(project_dir: str, name: str, title: str = "") -> dict:
    """Create a revisioned SCH Circuit IR under .actuator/schematic without touching the KiCad file."""
    return eda.sch_plan_create(project_dir, name, title=title)


@mcp.tool(title="Read schematic design plan", annotations=READ_ONLY)
def eda_sch_plan_get(ir_path: str) -> dict:
    """Read a SCH Circuit IR and its static validation result."""
    return eda.sch_plan_get(ir_path)


@mcp.tool(title="Edit schematic component plan", annotations=WRITE_STATEFUL)
def eda_sch_component(
    ir_path: str,
    action: str,
    reference: str,
    lib_id: str | None = None,
    value: str | None = None,
    footprint: str | None = None,
    x: float | None = None,
    y: float | None = None,
    rotation: float | None = None,
    multi_unit: bool | None = None,
    unit_spacing: float | None = None,
    component_type: str | None = None,
) -> dict:
    """Add/update/remove a component in SCH Circuit IR.

    Set multi_unit=true for symbols such as op-amps/logic packages whose units
    share one reference. This does not use GUI automation.
    """
    return eda.sch_component(
        ir_path,
        action,
        reference,
        lib_id=lib_id,
        value=value,
        footprint=footprint,
        x=x,
        y=y,
        rotation=rotation,
        multi_unit=multi_unit,
        unit_spacing=unit_spacing,
        component_type=component_type,
    )


@mcp.tool(title="Edit schematic power symbol", annotations=WRITE_STATEFUL)
def eda_sch_power(
    ir_path: str,
    action: str,
    power_name: str,
    reference: str | None = None,
    x: float | None = None,
    y: float | None = None,
) -> dict:
    """Add/remove a KiCad power symbol such as GND, +3V3, VCC or VSS."""
    return eda.sch_power(
        ir_path,
        action,
        power_name,
        reference=reference,
        x=x,
        y=y,
    )


@mcp.tool(title="Auto-layout schematic plan", annotations=WRITE_STATEFUL)
def eda_sch_auto_layout(
    ir_path: str,
    start_x: float = 50.0,
    start_y: float = 50.0,
    column_spacing: float = 35.0,
    row_spacing: float = 25.0,
    power_offset: float = 15.0,
) -> dict:
    """Apply collision-aware directed positions to the SCH Circuit IR."""
    return eda.sch_auto_layout(
        ir_path,
        start_x=start_x,
        start_y=start_y,
        column_spacing=column_spacing,
        row_spacing=row_spacing,
        power_offset=power_offset,
    )


@mcp.tool(title="Edit schematic connection plan", annotations=WRITE_STATEFUL)
def eda_sch_connection(
    ir_path: str,
    action: str,
    connection_id: str | None = None,
    from_reference: str | None = None,
    from_pin: str | None = None,
    to_reference: str | None = None,
    to_pin: str | None = None,
    net_name: str = "",
) -> dict:
    """Add or remove a pin-to-pin electrical connection in SCH Circuit IR."""
    return eda.sch_connection(
        ir_path,
        action,
        connection_id=connection_id,
        from_reference=from_reference,
        from_pin=from_pin,
        to_reference=to_reference,
        to_pin=to_pin,
        net_name=net_name,
    )


@mcp.tool(title="Edit schematic label or no-connect plan", annotations=WRITE_STATEFUL)
def eda_sch_marker(
    ir_path: str,
    action: str,
    kind: str,
    marker_id: str | None = None,
    name: str | None = None,
    label_kind: str = "local",
    x: float | None = None,
    y: float | None = None,
    reference: str | None = None,
    pin: str | None = None,
) -> dict:
    """Add/remove a label or explicit no-connect marker in SCH Circuit IR."""
    return eda.sch_marker(
        ir_path,
        action,
        kind,
        marker_id=marker_id,
        name=name,
        label_kind=label_kind,
        x=x,
        y=y,
        reference=reference,
        pin=pin,
    )


@mcp.tool(title="Validate schematic design plan", annotations=READ_ONLY)
def eda_sch_validate(ir_path: str) -> dict:
    """Run deterministic static checks over SCH Circuit IR."""
    return eda.sch_validate(ir_path)


@mcp.tool(title="Rollback schematic design plan", annotations=WRITE_DESTRUCTIVE)
def eda_sch_rollback(ir_path: str, revision: int) -> dict:
    """Restore a prior SCH Circuit IR revision while keeping the current revision in history."""
    return eda.sch_rollback(ir_path, revision)


@mcp.tool(title="Inspect KiCad schematic structurally", annotations=READ_ONLY)
def eda_sch_inspect(schematic_path: str) -> dict:
    """Parse a .kicad_sch read-only and summarize symbols, wires, labels, footprints, and hash."""
    return eda.sch_inspect(schematic_path)


@mcp.tool(title="Materialize schematic from Circuit IR", annotations=WRITE_DESTRUCTIVE)
def eda_sch_materialize(
    ir_path: str,
    schematic_path: str,
    overwrite: bool = False,
    force: bool = False,
) -> dict:
    """Generate .kicad_sch from revisioned Circuit IR without GUI automation.

    The staged schematic must round-trip through kicad-sch-api and export a
    KiCad netlist that matches the IR before the destination is committed.
    External edits are hash-protected unless force=true.
    """
    return eda.sch_materialize(
        ir_path,
        schematic_path,
        overwrite=overwrite,
        force=force,
    )


@mcp.tool(title="Compare Circuit IR with KiCad netlist", annotations=READ_ONLY)
def eda_sch_compare_netlist(ir_path: str, netlist_path: str) -> dict:
    """Verify components and electrical connections in a KiCad netlist against Circuit IR."""
    return eda.sch_compare_netlist(ir_path, netlist_path)


@mcp.tool(title="Freeze verified schematic revision", annotations=WRITE_STATEFUL)
def eda_sch_freeze(ir_path: str, schematic_path: str) -> dict:
    """Freeze a schematic only after IR validation, KiCad ERC/netlist and semantic checks pass."""
    return eda.sch_freeze(ir_path, schematic_path)


@mcp.tool(title="Export schematic netlist", annotations=WRITE_ADDITIVE)
def eda_sch_export_netlist(
    schematic_path: str,
    output_path: str | None = None,
    netlist_format: str = "kicadsexpr",
) -> dict:
    """Export a KiCad schematic netlist via kicad-cli; fails if KiCad is unavailable."""
    return eda.sch_export_netlist(
        schematic_path,
        output_path=output_path,
        netlist_format=netlist_format,
    )


@mcp.tool(title="Verify KiCad schematic", annotations=WRITE_ADDITIVE)
def eda_sch_verify(schematic_path: str) -> dict:
    """Inspect the schematic, then run authoritative KiCad ERC + netlist export when kicad-cli exists.

    Returns status=blocked rather than falling back to GUI automation when KiCad is unavailable.
    """
    return eda.sch_verify(schematic_path)




@mcp.tool(title="Read KiCad SCH worker status", annotations=READ_ONLY)
def eda_sch_worker_status() -> dict:
    """Report worker PID/window, pause reason, dirty state, and file baseline."""
    return eda.sch_worker_status()




@mcp.tool(title="Stop background KiCad SCH worker", annotations=WRITE_STATEFUL)
def eda_sch_worker_stop() -> dict:
    """Gracefully close the managed SCH worker; refuses unsaved user edits."""
    return eda.sch_worker_stop()




# ----------------------------
# Batch/workflow tool
# ----------------------------

@mcp.tool(title="Read UIA and DOM schemas", annotations=READ_ONLY)
def automation_schema_info(connector_schemas: list[dict] | None = None) -> dict:
    """Export authorized UIA/DOM input schemas and their fingerprint. Optional caller schemas detect drift; this comparison does not prove application dispatch or grant permissions."""
    from .automation_schema import report
    from .tool_policy import current_principal
    return report(mcp,current_principal(runtime),connector_schemas)

@mcp.tool(title="Preview a batch without dispatch", annotations=READ_ONLY)
def batch_preview(actions: list[dict]) -> dict:
    """Check a plan without calling its actions. Reports static errors and deferred prior-result refs; live provider conditions remain checked at execution. Permission checked for each proposed action; values are omitted."""
    from .batch_preflight import preview
    return preview(actions,_BATCH_TOOL_HANDLERS)

_BATCH_TOOL_NAMES = (
    "uia_control_focus",
    "uia_control_window", "uia_control_scroll", "uia_control_reveal", "uia_desktop_windows", "uia_desktop_recover", "dom_browser_sessions", "dom_browser_recover",
    "dom_operation_status", "dom_operation_cancel", "dom_review_status", "dom_review_record", "dom_browser_choices", "dom_browser_remember", "dom_browser_resume", "dom_observe",
    "dom_operation_recover", "dom_review_run", "dom_browser_choice_manage", "dom_variant_select",
    "uia_app_open", "uia_desktop_attach", "uia_desktop_release", "dom_browser_attach", "dom_browser_detach",
    "batch_preview",
    "automation_schema_info",
    "uia_workspace_profiles", "uia_workspace_open", "uia_workspace_windows", "uia_workspace_close",
    "uia_control_bind", "uia_control_snapshot", "uia_control_find", "uia_control_state", "uia_invoke", "uia_set_value", "uia_toggle",
    "uia_control_select", "uia_control_expand_collapse", "uia_control_set_range_value",
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
    "process_stop",
    "powershell_info",
    "powershell_run",
    "screen_info",
    "window_list",
    "window_active",
    "window_details",
    "uia_list",
    "uia_snapshot",
    "uia_snapshot_diff",
    "uia_find",
    "uia_details",
    "dom_list_tabs",
    "dom_open_tab",
    "dom_navigate",
    "dom_close_tab",
    "dom_find",
    "dom_snapshot",
    "dom_snapshot_diff",
    "automation_status",
    "dom_accessibility_snapshot",
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
    "eda_info",
    "eda_altium_library_inspect",
    "eda_altium_sch_plan_create",
    "eda_altium_sch_validate",
    "eda_altium_sch_materialize",
    "eda_altium_sch_verify",
    "eda_altium_sch_inspect",
    "eda_altium_sch_edit_preview",
    "eda_altium_sch_edit_apply",
    "eda_altium_sch_check_pin_contracts",
    "eda_altium_sch_diff_connectivity",
    "eda_altium_sch_lint",
    "eda_altium_sch_erc",
    "eda_altium_sch_edit_commit",
    "eda_altium_sch_edit_restore",
    "eda_altium_operation_status",

    "eda_project_inspect",
    "eda_kicad_erc",
    "eda_kicad_drc",
    "eda_kicad_export_gerbers",
    "eda_sch_plan_create",
    "eda_sch_plan_get",
    "eda_sch_component",
    "eda_sch_power",
    "eda_sch_auto_layout",
    "eda_sch_connection",
    "eda_sch_marker",
    "eda_sch_validate",
    "eda_sch_rollback",
    "eda_sch_inspect",
    "eda_sch_materialize",
    "eda_sch_compare_netlist",
    "eda_sch_freeze",
    "eda_sch_export_netlist",
    "eda_sch_verify",
    "eda_sch_worker_status",
    "eda_sch_worker_stop",
)
_BATCH_TOOL_HANDLERS = {name: globals()[name] for name in _BATCH_TOOL_NAMES}

_BATCH_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "batch_preview",
        "security_policy_info",
        "work_session_status",
        "automation_schema_info",
        "uia_desktop_windows", "dom_browser_sessions", "uia_workspace_profiles", "uia_workspace_windows", "uia_control_snapshot", "uia_control_find", "uia_control_state",
        "dom_operation_status", "dom_review_status", "dom_browser_choices", "dom_observe",
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
        "uia_list",
    "uia_snapshot",
    "uia_snapshot_diff",
        "uia_find",
        "uia_details",
        "dom_list_tabs",
        "dom_find",
    "dom_snapshot",
    "dom_snapshot_diff",
    "automation_status",
    "dom_accessibility_snapshot",
        "dom_details",
        "dom_read_text",
        "dom_wait",
        "clipboard_read_text",
        "eda_info",
        "eda_project_inspect",
        "eda_sch_plan_get",
        "eda_sch_validate",
        "eda_sch_inspect",
        "eda_sch_compare_netlist",
        "eda_sch_worker_status",
        "eda_altium_sch_check_pin_contracts",
        "eda_altium_sch_diff_connectivity",
        "eda_altium_sch_lint",
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
    verified_dom validates all step shapes before dispatch, requires explicit
    click observations and halts after any unconfirmed mutation or observed
    login/challenge blocker, even when stop_on_error=false. It never replays
    steps or certifies business outcomes; live refs/conditions remain per-step.
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


from .command_profiles import CommandProfiles
commands = CommandProfiles(runtime)

@mcp.tool(title="Read security policy", annotations=READ_ONLY)
def security_policy_info() -> dict:
    from .tool_policy import current_principal, TOOL_SCOPES
    principal = current_principal(runtime)
    from . import __version__
    from .resource_limits import validated
    return {'runtime_version':__version__,'resource_limits':validated(runtime.raw_config().get('security',{}).get('resource_limits')),"owner": principal.owner, "scopes": sorted(principal.scopes),
            "full_access": principal.full_access, "background_powershell":principal.background_powershell,"classified_tools": len(TOOL_SCOPES),
            "arbitrary_execution_is_os_sandboxed": False,
            "execution_policy": "scoped_desktop_patterns" if runtime.raw_config().get('security',{}).get('desktop_uia',{}).get('enabled') else "background_only", "physical_input": "removed",
            "browser_mode": "managed_visible_or_selected_cdp" if runtime.config.browser_dom.visible_browser else "isolated_headless",
            "browser_routing": "DOM", "application_routing": "UIA",
            "personal_browser_selection_required":True,"managed_browser_start":"Explicit browser_mode=managed; private broker sessions are isolated managed sessions",
            "http_https_hosts": list(runtime.config.browser_dom.allowed_hosts)}

@mcp.tool(title="List owner-approved command profiles", annotations=READ_ONLY)
def command_profile_list() -> dict:
    return commands.list()

@mcp.tool(title="Run an owner-approved fixed command", annotations=WRITE_ADDITIVE)
def command_profile_run(profile: str, attempt_key: str) -> dict:
    """Run a fixed background command once per session/key; retries return its status."""
    return commands.run(profile, attempt_key=attempt_key, sessions=mcp.sessions)


@mcp.tool(title="Open a work session", annotations=WRITE_ADDITIVE)
def work_session_open() -> dict:
    from .tool_policy import current_principal
    return mcp.sessions.open(current_principal(runtime).owner)

@mcp.tool(title="Read own work sessions", annotations=READ_ONLY)
def work_session_status(limit: int = 50, session_offset: int = 0, job_offset: int = 0) -> dict:
    from .tool_policy import current_principal
    owner=current_principal(runtime).owner
    result=mcp.sessions.status(owner,limit,session_offset,job_offset)
    with mcp.sessions.lock:
        default=mcp.sessions.defaults.get(owner)
    with mcp.sessions.db() as db:
        active_default=db.execute("SELECT id FROM sessions WHERE id=? AND owner=? AND status='active'",(default,owner)).fetchone()
    result['default_session_id']=default if active_default else None
    return result

_BATCH_TOOL_NAMES += ('security_policy_info','work_session_status')
_BATCH_TOOL_HANDLERS.update({name:globals()[name] for name in ('security_policy_info','work_session_status')})

@mcp.tool(title="Close an owned work session", annotations=WRITE_STATEFUL)
def work_session_close(session_id: str) -> dict:
    from .tool_policy import current_principal
    owner=current_principal(runtime).owner
    from .session_resources import reconcile
    mcp.sessions.require(__import__('dataclasses').replace(current_principal(runtime),session=session_id))
    reconcile(mcp.sessions,dom_host,owner,session_id)
    result=mcp.sessions.close(owner, session_id)
    if result.get('closed'):
        uia_spaces.release_desktop_session(owner,session_id)
        runtime.get_service('uia')._snapshots.forget_session(owner,session_id)
        dom_host.call('release_session',owner,session_id,timeout=5)
    return result


@mcp.tool(title="Read connection diagnostics", annotations=READ_ONLY)
async def connection_diagnostics(connector_error: str | None = None) -> dict:
    from .connection_diagnostics import snapshot
    return await snapshot(runtime, connector_error=connector_error)

# Defined after the batch table, but read-only diagnostics are valid serialized children.
_BATCH_TOOL_HANDLERS['connection_diagnostics']=connection_diagnostics
_BATCH_TOOL_NAMES+=('connection_diagnostics',)
_BATCH_READ_ONLY_TOOL_NAMES=_BATCH_READ_ONLY_TOOL_NAMES|{'connection_diagnostics'}


@mcp.tool(title="Preview session close blockers", annotations=READ_ONLY)
def work_session_close_preview(session_id: str) -> dict:
    from .tool_policy import current_principal
    from dataclasses import replace
    from .session_resources import reconcile
    principal=current_principal(runtime);preview=mcp.sessions.close_preview(principal.owner,session_id)
    if preview['status']!='active':return preview
    resources=reconcile(mcp.sessions,dom_host,principal.owner,session_id)
    return {**mcp.sessions.close_preview(principal.owner, session_id),'resources':resources}

@mcp.tool(title="Read own task attempts", annotations=READ_ONLY)
def task_attempt_status() -> dict:
    from .tool_policy import current_principal
    return mcp.sessions.attempts_status(current_principal(runtime).owner)

@mcp.tool(title="Cancel an owned task attempt", annotations=WRITE_STATEFUL)
def task_attempt_cancel(attempt_id: str) -> dict:
    from .tool_policy import current_principal
    return mcp.sessions.attempt_cancel(current_principal(runtime).owner, attempt_id)

@mcp.tool(title="Create a session-isolated Git worktree", annotations=WRITE_ADDITIVE)
def worktree_create(repository: str) -> dict:
    from .worktrees import Worktrees
    from .tool_policy import current_principal
    principal = current_principal(runtime)
    return Worktrees(runtime, mcp.sessions).create(principal, repository)

@mcp.tool(title="Read capability readiness", annotations=READ_ONLY)
def capability_readiness(connector_names: list[str] | None = None, connector_tools: list[dict] | None = None) -> dict:
    from .readiness import readiness
    return readiness(runtime, mcp, connector_names=connector_names, connector_tools=connector_tools)

# Mission tools are registered for catalog stability, but disabled by default.
from threading import RLock as _MissionLock
_mission_lock = _MissionLock()
_mission_engine = None


def _missions():
    global _mission_engine
    if runtime.raw_config().get('missions', {}).get('enabled') is not True:
        raise PermissionError('Mission Runtime is disabled by local owner policy')
    with _mission_lock:
        if _mission_engine is None:
            from .missions import MissionEngine
            _mission_engine = MissionEngine(runtime.config_path.parent / 'missions.sqlite3',
                _BATCH_TOOL_HANDLERS,
                approvals=lambda: runtime.raw_config().get('missions', {}).get('approvals', {}))
        return _mission_engine


@mcp.tool(title='Create a durable mission', annotations=WRITE_ADDITIVE)
def mission_create(goal: str, steps: list[dict], criteria: list[dict], request_key: str,
                   max_seconds: int = 1800, max_attempts: int = 3) -> dict:
    """Create an immutable ordered plan with read-only pre/postconditions and final criteria. Uses a stable owner-scoped request_key; no dispatch. ChatGPT drives subsequent calls."""
    from .tool_policy import current_principal
    return _missions().create(current_principal(runtime), goal, steps, criteria, request_key,
                              max_seconds, max_attempts)


@mcp.tool(title='Read an owned mission', annotations=READ_ONLY)
def mission_status(mission_id: str) -> dict:
    """Read durable steps, budgets, recent events and stop reasons for this authenticated owner."""
    from .tool_policy import current_principal
    return _missions().status(current_principal(runtime), mission_id)


@mcp.tool(title='Read next mission step', annotations=READ_ONLY)
def mission_next(mission_id: str) -> dict:
    """Return the next ordered step or request goal verification. Does not execute or independently reason."""
    from .tool_policy import current_principal
    return _missions().next(current_principal(runtime), mission_id)


@mcp.tool(title='Execute one claimed mission step', annotations=WRITE_DESTRUCTIVE)
async def mission_execute(mission_id: str, expected_digest: str, expected_step: int) -> dict:
    """Claim one step, recheck policy and preconditions, dispatch once and read postconditions. Bounded to 30s caller wait; uncertain mutations never replay. Sensitive tools need local owner approval for the plan digest and step."""
    from .tool_policy import current_principal
    return await _missions().execute(current_principal(runtime), mission_id, expected_digest, expected_step)


@mcp.tool(title='Verify mission evidence', annotations=WRITE_STATEFUL)
async def mission_verify(mission_id: str, step_index: int | None = None) -> dict:
    """Read final criteria before completing a mission, or verify an uncertain step using its immutable postconditions. Never dispatches the action again."""
    from .tool_policy import current_principal
    return await _missions().verify(current_principal(runtime), mission_id, step_index)


@mcp.tool(title='Pause resume or cancel an owned mission', annotations=WRITE_STATEFUL)
def mission_control(mission_id: str, action: str, bindings: dict | None = None) -> dict:
    """Pause/resume/cancel a settled mission, or rebind stale browser/UIA references after explicit recovery. Rebind changes the digest and revokes plan approvals; uncertain actions never replay. Budgets are retained."""
    from .tool_policy import current_principal
    return _missions().control(current_principal(runtime), mission_id, action, bindings)


# A separate worker loop continues while no chat is attached to the HTTP backend.
_agent_service = None


def _agents():
    global _agent_service
    if runtime.raw_config().get('agents', {}).get('enabled') is not True:
        raise PermissionError('Background agents are disabled by local owner policy')
    with _mission_lock:
        if _agent_service is None:
            from .agents import AgentRuntime
            from .work_sessions import WorkSessions
            with mcp._session_lock:
                if mcp.sessions is None:
                    mcp.sessions = WorkSessions(runtime.config_path.parent / 'work-sessions.sqlite3',
                        runtime.raw_config().get('security', {}).get('resource_limits'))
            def catalog():
                return [{'name': t.name, 'inputSchema': t.parameters,
                         'description': (t.description or '')[:800]} for t in mcp._tool_manager.list_tools()]
            _agent_service = AgentRuntime(runtime, _missions(), mcp.sessions, catalog)
        return _agent_service


mcp.agent_background_factory = _agents


@mcp.tool(title='Submit a bounded background agent task', annotations=WRITE_DESTRUCTIVE)
def agent_submit(goal: str, workspace: str, allowed_tools: list[str], criteria: list[dict],
                 request_key: str, context: dict | None = None, allowed_hosts: list[str] | None = None,
                 run_at: float | None = None, max_calls: int = 6, max_cycles: int = 16,
                 max_seconds: int = 1200, token_budget: int = 180000,
                 milestones: list[dict] | None = None, browser_recovery: dict | None = None,
                 validators: list[dict] | None = None, priority: int = 0) -> dict:
    """Delegate a goal with frozen read-only completion criteria, explicit tools and workspace. A local Codex planner drives Mission steps without an attached chat. run_at is an optional UTC Unix timestamp; a stable request_key also deduplicates event submissions. No arbitrary shell, purchases or permission expansion."""
    from .tool_policy import current_principal
    service = _agents()
    result = service.submit(current_principal(runtime), goal, workspace, allowed_tools, criteria,
        request_key, context, allowed_hosts, run_at, max_calls, max_cycles, max_seconds, token_budget,
        milestones, browser_recovery, validators, priority)
    service.start()
    return result


@mcp.tool(title='Start a background task from declared file outputs', annotations=WRITE_DESTRUCTIVE)
def agent_start(goal: str, workspace: str, outputs: list[dict], request_key: str,
                stages: list[dict] | None = None, run_at: float | None = None,
                validators: list[dict] | None = None, priority: int = 0,
                wait_for_inputs: list[dict] | None = None, version_outputs: bool = False) -> dict:
    """Prefer this for autonomous file tasks. Translate the user's request into outputs [{path, text}] for exact content or [{path, contains}] for an explicit required fragment; paths may be workspace-relative. Optional ordered stages [{title,outputs}] checkpoint durable intermediate outputs. Derives narrow tools and frozen read-back criteria automatically. No execution steps needed; continues without chat. Preserve the user's intended acceptance requirements; do not invent trivial checks. For browser/other tools use agent_submit with explicit delegation."""
    from .tool_policy import current_principal
    service = _agents()
    result = service.start_task(current_principal(runtime), goal, workspace, outputs, request_key,
                                stages, run_at, validators, priority, wait_for_inputs, version_outputs)
    service.start()
    return result


@mcp.tool(title='Run an explicit deterministic file recipe without inference', annotations=WRITE_DESTRUCTIVE)
def agent_recipe(goal: str, workspace: str, recipe: dict, request_key: str,
                 run_at: float | None = None, wait_for_inputs: list[dict] | None = None,
                 version_outputs: bool = True) -> dict:
    """Render an explicit files recipe (exact path/text outputs), bom_csv (path/expected rows), or json_report (path/document/contract). No model calls or guessed content. All writes use native CAS and Mission evidence. Versioning stages canonical UTF-8 files up to 16 KiB; retained history is owner-scoped. Optional frozen input conditions wait for secured read-back evidence."""
    from .tool_policy import current_principal
    service=_agents()
    result=service.start_recipe(current_principal(runtime),goal,workspace,recipe,request_key,run_at,wait_for_inputs,version_outputs)
    service.start()
    return result


@mcp.tool(title='Run a declared file workflow with branches and dependencies', annotations=WRITE_DESTRUCTIVE)
def agent_workflow(goal: str, workspace: str, nodes: list[dict], outputs: list[dict], request_key: str,
                   wait_for_inputs: list[dict] | None = None, version_outputs: bool = True) -> dict:
    """Declare 1..12 nodes with id,title,needs and exact outputs or recipe. Optional when uses read-only file predicates; needs_policy is all or settled for joins. Output nodes default to exact deterministic recipes; mode=ai explicitly enables planning for text/contains outputs. Freeze all branches, paths and final outputs; completed nodes do not replay after regression. Cycles and shared output ownership are rejected."""
    from .tool_policy import current_principal
    service=_agents()
    result=service.start_workflow(current_principal(runtime),goal,workspace,nodes,outputs,request_key,wait_for_inputs,version_outputs)
    service.start()
    return result


@mcp.tool(title='Inspect owned staged and verified file versions', annotations=READ_ONLY)
def agent_artifacts(job_id: str) -> dict:
    """Read up to 50 owner-scoped artifact versions, native hashes and bounded text diffs. Staged/uncertain versions cannot be restored. Details may expire after 90 days; metadata remains. No current file is read or changed."""
    from .agents.artifacts import history
    from .tool_policy import current_principal
    return history(_agents().queue,current_principal(runtime).owner,job_id)


@mcp.tool(title='Queue restoration of a verified previous text version', annotations=WRITE_DESTRUCTIVE)
def agent_restore_artifact(job_id: str, version_id: int, request_key: str) -> dict:
    """Queue a deterministic restoration within the original retained delegation and current policy. The current file must still match the recorded published SHA256 at execution; changed user content blocks the task. Does not delete newly created files, undo external actions, bypass approval or grant permissions."""
    from .agents.artifacts import restore
    from .tool_policy import current_principal
    service=_agents()
    result=restore(service,current_principal(runtime),job_id,version_id,request_key)
    service.start()
    return result


@mcp.tool(title='Archive a completed agent task without losing deduplication', annotations=WRITE_STATEFUL)
def agent_archive(job_id: str) -> dict:
    """Compress owned terminal task/Mission evidence, retaining permanent ID/key/digest summaries. Refuses active or unresolved mutation outcomes. Full history is bounded, expires after 90 days on retention maintenance; idempotency keys never expire. Read retained details using agent_status(include_archived=true)."""
    from .tool_policy import current_principal
    service = _agents()
    return service.queue.archive.archive(current_principal(runtime).owner, job_id)


@mcp.tool(title='Check BOM or JSON report quality with read-only evidence', annotations=READ_ONLY)
async def agent_validate(workspace: str, validators: list[dict]) -> dict:
    """Evaluate explicit deterministic bom_csv or json_report contracts through secured file reads. Does not call a model or create a task. BOM CSV headers: part,quantity,unit; expected rows use positive decimal quantity strings. JSON contracts declare fields/types, nonempty fields and exact expected values. This certifies the supplied contract, not unspecified business suitability."""
    from .tool_policy import current_principal
    service = _agents()
    return await service.validate(current_principal(runtime), workspace, validators)


@mcp.tool(title='Read private historical project memory', annotations=READ_ONLY)
def agent_memory_list(workspace: str) -> dict:
    """Read up to eight unexpired owner/project hints with provenance. These are historical hints, never fresh evidence or authorization."""
    from .tool_policy import current_principal
    return _agents().memory(current_principal(runtime), workspace, 'list')


@mcp.tool(title='Remember or forget a project hint', annotations=WRITE_STATEFUL)
def agent_memory_update(workspace: str, action: str, key: str, note: str | None = None,
                        ttl_days: int = 30) -> dict:
    """Remember an explicit owner note or forget an exact memory key. Notes expire in 1..90 days, stay private to owner/workspace and cannot change execution policy."""
    from .tool_policy import current_principal
    return _agents().memory(current_principal(runtime), workspace, action, key, note, ttl_days)


@mcp.tool(title='Read verified background execution metrics', annotations=READ_ONLY)
def agent_metrics() -> dict:
    """Read owner-scoped completion, blocking, recovery, usage and milestone metrics from persisted execution records. Does not call the model."""
    from .agents.evaluation import metrics
    from .tool_policy import current_principal
    service = _agents()
    return metrics(service.queue.store, current_principal(runtime).owner)


@mcp.tool(title='Inspect local background service health', annotations=READ_ONLY)
def agent_health() -> dict:
    """Probe local backend, gateway and worker, with bounded diagnostic history. Tunnel PID changes are observations; external tunnel connectivity is not certified. No commands or credentials are returned."""
    from .agents.diagnostics import collect
    return collect(runtime.config_path.parent.parent)


@mcp.tool(title='Read owned background agent tasks', annotations=READ_ONLY)
def agent_status(job_id: str | None = None, include_archived: bool = False) -> dict:
    """Read this authenticated owner's task state, frozen delegation, bounded memory, evidence and usage reservations. Omit job_id for the latest 50 summaries."""
    from .tool_policy import current_principal
    service = _agents()
    from .agents.coordination import health
    return {**service.queue.status(current_principal(runtime).owner, job_id, include_archived),
        'worker_alive': bool(service.thread and service.thread.is_alive()),
        'planner': 'codex_cli', 'worker_last_error': service.last_error,
        'worker_health': health(service.queue.store, service.worker_id), 'watchdog_started': service.watchdog_started,
        'last_backup_error': service.last_backup_error,'input_monitor':service.input_watcher.status()}


@mcp.tool(title='Recheck blocked requirements without replaying mutations', annotations=WRITE_STATEFUL)
async def agent_recheck(job_id: str, resume_if_verified: bool = True) -> dict:
    """Read frozen goals and quality evidence for an owned BLOCKED task. Reconcile UNKNOWN delivery only by Mission verification. Optionally queue already-verified requirements without resetting budgets. No model call, mutation replay or approval bypass. Missing requirements remain blocked."""
    from .agents.recheck import inspect
    from .tool_policy import current_principal
    service = _agents()
    result = await inspect(service, current_principal(runtime).owner, job_id, resume_if_verified)
    if result['resumed']:
        service.start()
    return result


@mcp.tool(title='Control an owned background agent task', annotations=WRITE_STATEFUL)
def agent_control(job_id: str, action: str) -> dict:
    """Pause, resume or cancel a delegated task. Active actions settle first; cancellation cannot undo delivered effects. Resume retains all usage and time budgets and cannot replay uncertain Mission mutations."""
    from .tool_policy import current_principal
    service = _agents()
    result = service.queue.control(current_principal(runtime).owner, job_id, action)
    if action == 'resume':
        service.start()
    return result


# Shared on-demand test runner; construction starts no processes or services.
from threading import RLock as _TestRunnerLock
_test_runner_lock = _TestRunnerLock()
_test_runner_service = None


def _test_runner():
    global _test_runner_service
    from .test_runner import TestRunner
    with _test_runner_lock:
        if _test_runner_service is None:
            _test_runner_service = TestRunner(runtime, mcp.sessions)
        return _test_runner_service


@mcp.tool(title='Check test runner prerequisites', annotations=READ_ONLY)
def test_preflight(profile: str) -> dict:
    """Check an owner-approved Maven profile without starting processes or containers."""
    return _test_runner().preflight(profile)


@mcp.tool(title='Start an owned Maven test run', annotations=WRITE_ADDITIVE)
def test_run(profile: str, attempt_key: str) -> dict:
    """Start one approved background Maven clean verify job and optional isolated services. Reuse attempt_key only to recover the same run."""
    return _test_runner().run(profile, attempt_key)


@mcp.tool(title='Read owned test run status', annotations=READ_ONLY)
def test_status(run_id: str) -> dict:
    """Read owned run state and cleanup obligation; cancellation request alone does not confirm stop."""
    return _test_runner().status(run_id)


@mcp.tool(title='Read owned test report', annotations=READ_ONLY)
def test_report(run_id: str) -> dict:
    """Read bounded, redacted command evidence and fresh Surefire/Failsafe test counts."""
    return _test_runner().report(run_id)


@mcp.tool(title='Cancel an owned test run', annotations=WRITE_STATEFUL)
def test_stop(run_id: str) -> dict:
    """Request cancellation. Poll test_status until finished and cleanup_pending=false."""
    return _test_runner().stop(run_id)


@mcp.tool(title='Retry owned test environment cleanup', annotations=WRITE_STATEFUL)
def test_cleanup(run_id: str) -> dict:
    """Retry removal of this run's Compose containers and volumes after failed or interrupted cleanup."""
    return _test_runner().cleanup(run_id)


from .automation_schema import validate_server
validate_server(mcp,globals())

def main() -> None:
    dom_host.start()
    admin = AdminServer(runtime, dom_host=dom_host)
    admin.start()
    try:
        options = run_options()
        if options["transport"] == "streamable-http":
            hostname = os.getenv("ACTUATOR_MCP_HOSTNAME")
            allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
            allowed_origins = []
            if hostname:
                allowed_hosts.extend([hostname, f"{hostname}:*"])
                allowed_origins.append(f"https://{hostname}")
            options["transport_security"] = TransportSecuritySettings(
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
            )
        mcp.run(**options)
    finally:
        admin.stop()
        dom_host.stop()


if __name__ == "__main__":
    main()
