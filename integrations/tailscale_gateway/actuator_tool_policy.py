"""Explicit reviewed tool permissions; unknown names are denied."""
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import inspect

READ_TOOLS = frozenset("""
test_preflight test_status test_report
eda_altium_operation_status
eda_altium_sch_check_pin_contracts eda_altium_sch_diff_connectivity eda_altium_sch_lint
mission_status mission_next agent_status agent_memory_list agent_metrics agent_validate agent_health agent_artifacts
dom_operation_status dom_review_status dom_browser_choices dom_observe
uia_desktop_windows dom_browser_sessions
batch_preview
uia_control_find uia_control_state
automation_schema_info
uia_workspace_profiles uia_workspace_windows uia_control_snapshot routing_policy_info routing_decide filesystem_list_roots filesystem_list
filesystem_read_text filesystem_stat filesystem_search system_info
system_resource_snapshot process_list process_details powershell_info screen_info
screen_capture window_list window_active window_details mouse_position uia_list
uia_find uia_details uia_snapshot uia_snapshot_diff dom_snapshot dom_snapshot_diff automation_status dom_accessibility_snapshot dom_list_tabs dom_find dom_details dom_read_text dom_wait
clipboard_read_text eda_info eda_project_inspect eda_sch_plan_get eda_sch_validate
eda_sch_inspect eda_sch_compare_netlist eda_sch_worker_status batch_readonly
security_policy_info command_profile_list work_session_status connection_diagnostics
work_session_close_preview task_attempt_status capability_readiness
""".split())
WRITE_TOOLS = frozenset("""
eda_altium_sch_plan_create
eda_altium_sch_edit_preview
mission_create mission_verify mission_control agent_submit agent_control agent_start agent_memory_update agent_archive agent_recheck agent_recipe agent_workflow agent_restore_artifact
dom_operation_recover dom_review_run dom_browser_choice_manage dom_variant_select
dom_operation_cancel dom_review_record dom_browser_remember dom_browser_resume
uia_control_focus
uia_control_window uia_control_scroll uia_control_reveal uia_desktop_recover dom_browser_recover
uia_desktop_attach uia_desktop_release dom_browser_attach dom_browser_detach
uia_control_select uia_control_expand_collapse uia_control_set_range_value
uia_workspace_close uia_control_bind filesystem_create_directory filesystem_write_text filesystem_replace_in_file
filesystem_move filesystem_delete window_focus window_set_state window_close
mouse_move mouse_click mouse_scroll keyboard_write keyboard_press keyboard_hotkey
text_replace_all uia_focus uia_invoke uia_set_value uia_toggle uia_select
uia_expand_collapse uia_scroll uia_scroll_into_view uia_set_range_value
uia_text_select_all uia_window_action dom_open_tab dom_navigate dom_close_tab
dom_click dom_set_value dom_select dom_toggle dom_scroll_into_view dom_scroll
clipboard_write_text eda_sch_plan_create eda_sch_component eda_sch_power
eda_sch_auto_layout eda_sch_connection eda_sch_marker eda_sch_rollback
eda_sch_materialize eda_sch_freeze eda_sch_worker_window eda_sch_worker_stop
work_session_open work_session_close worktree_create task_attempt_cancel
""".split())
EXECUTE_TOOLS = frozenset("""
test_run test_stop test_cleanup
eda_altium_library_inspect eda_altium_sch_validate eda_altium_sch_materialize eda_altium_sch_verify
eda_altium_sch_inspect eda_altium_sch_edit_apply
eda_altium_sch_erc eda_altium_sch_edit_commit eda_altium_sch_edit_restore
mission_execute
uia_app_open
uia_workspace_open process_start process_stop powershell_run batch_execute command_profile_run
eda_kicad_erc eda_kicad_drc eda_kicad_export_gerbers eda_sch_export_netlist
eda_sch_verify eda_sch_worker_start eda_sch_worker_apply
""".split())
# Immutable restriction: Full Access and owner policy cannot re-enable desktop input.
BACKGROUND_BLOCKED_TOOLS = frozenset("""
mouse_position mouse_move mouse_click mouse_scroll keyboard_write keyboard_press
keyboard_hotkey text_replace_all window_focus window_set_state window_close
uia_focus uia_select uia_expand_collapse
uia_scroll uia_scroll_into_view uia_set_range_value uia_text_select_all
uia_window_action clipboard_write_text process_start
eda_sch_worker_start eda_sch_worker_window eda_sch_worker_apply
""".split())

FULL_ACCESS_TOOLS = frozenset({"process_start"})
TOOL_SCOPES = {**dict.fromkeys(READ_TOOLS, "mcp:read"),
               **dict.fromkeys(WRITE_TOOLS, "mcp:write"),
               **dict.fromkeys(EXECUTE_TOOLS, "mcp:execute")}
if len(TOOL_SCOPES) != len(READ_TOOLS) + len(WRITE_TOOLS) + len(EXECUTE_TOOLS):
    raise RuntimeError('Tool scopes overlap')

_ALTIUM_SCOPES = {
    "eda_altium_library_inspect": ("mcp:read", "mcp:execute"),
    "eda_altium_sch_validate": ("mcp:read", "mcp:execute"),
    "eda_altium_sch_verify": ("mcp:read", "mcp:execute"),
    "eda_altium_sch_plan_create": ("mcp:read", "mcp:write", "mcp:execute"),
    "eda_altium_sch_materialize": ("mcp:read", "mcp:write", "mcp:execute"),
    "eda_altium_sch_inspect": ("mcp:read", "mcp:execute"),
    "eda_altium_sch_edit_preview": ("mcp:read", "mcp:write"),
    "eda_altium_sch_edit_apply": ("mcp:read", "mcp:write", "mcp:execute"),
    "eda_altium_sch_check_pin_contracts": ("mcp:read",),
    "eda_altium_sch_diff_connectivity": ("mcp:read",),
    "eda_altium_sch_lint": ("mcp:read",),
    "eda_altium_sch_erc": ("mcp:read", "mcp:execute"),
    "eda_altium_sch_edit_commit": ("mcp:read", "mcp:write", "mcp:execute"),
    "eda_altium_sch_edit_restore": ("mcp:read", "mcp:write", "mcp:execute"),
    "eda_altium_operation_status": ("mcp:read",),
}
_SCOPE_ORDER = ("mcp:read", "mcp:write", "mcp:execute")

@dataclass(frozen=True)
class Principal:
    owner: str = "local"
    scopes: frozenset = frozenset({"mcp:read", "mcp:write", "mcp:execute"})
    full_access: bool = False
    session: str = "default"
    background_powershell: bool = False
    connection_kind: str = 'unidentified'
    autonomous_hosts: tuple[str, ...] | None = None
    autonomous_workspace: str | None = None

principal_context = ContextVar("actuator_principal", default=None)

def scope_for(name):
    prefix, sep, original = name.partition("__")
    if sep and prefix != "actuator":
        raise PermissionError("Unclassified backend namespace")
    key = original if sep else name
    if key not in TOOL_SCOPES:
        raise PermissionError("Unclassified tool: " + key)
    return TOOL_SCOPES[key]

def required_scopes(name):
    """Return every grant a tool needs, preserving scope_for's primary mapping."""
    prefix, sep, original = name.partition("__")
    if sep and prefix != "actuator":
        raise PermissionError("Unclassified backend namespace")
    key = original if sep else name
    primary = scope_for(name)
    requirements = _ALTIUM_SCOPES.get(key, (primary,))
    return tuple(scope for scope in _SCOPE_ORDER if scope in requirements)

def authorize(name, principal, arguments=None):
    from .automation_errors import PolicyDenied
    key = name.removeprefix("actuator__")
    if key in BACKGROUND_BLOCKED_TOOLS:
        raise PolicyDenied('local_policy')
    name = key
    required = scope_for(name)
    missing = [scope for scope in required_scopes(name) if scope not in principal.scopes]
    if missing:
        raise PolicyDenied('missing_scope',missing[0])
    if name == 'powershell_run' and not principal.background_powershell:
        raise PolicyDenied('local_policy')
    if name in FULL_ACCESS_TOOLS and not principal.full_access:
        raise PolicyDenied('local_policy')
    if name in {"batch_execute", "batch_readonly"}:
        actions = (arguments or {}).get("actions", [])
        if not isinstance(actions, list) or len(actions) > 25:
            raise ValueError("Invalid batch actions")
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError("Invalid batch action")
            child = action.get("tool", "")
            if child in {"batch_execute", "batch_readonly"}:
                raise PermissionError("Nested batch denied")
            if name == "batch_readonly" and child not in READ_TOOLS:
                raise PermissionError("Write in read-only batch")
            authorize(child, principal, action.get("arguments", {}))
    return required

def background_powershell_approved(runtime):
    if not hasattr(runtime,'config_path'):return False
    import json
    from pathlib import Path
    path=Path(runtime.config_path).parent.parent/'deployment/powershell-policy.json'
    try:return json.loads(path.read_text(encoding='utf-8')).get('background_execute') is True
    except (OSError,ValueError,AttributeError):return False

def current_principal(runtime):
    existing = principal_context.get()
    if existing is not None:
        return existing
    from mcp.server.auth.middleware.auth_context import get_access_token
    policy = runtime.raw_config().get("security", {})
    configured = frozenset(policy.get("scopes", ["mcp:read", "mcp:write", "mcp:execute"]))
    token = get_access_token()
    scopes = configured & frozenset(token.scopes) if token else configured
    owner = token.client_id if token else "local"
    return Principal(owner=owner, scopes=scopes,
                     full_access=policy.get("mode", "project") == "full_access",
                     background_powershell=background_powershell_approved(runtime))
