from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

from .errors import ConfigError


@dataclass(frozen=True)
class FileSystemConfig:
    enabled: bool
    read_only: bool
    allow_create: bool
    allow_edit: bool
    allow_move: bool
    allow_delete: bool
    allowed_roots: tuple[str, ...]
    max_list_items: int
    max_read_bytes: int
    max_search_results: int


@dataclass(frozen=True)
class ProcessConfig:
    enabled: bool
    allow_start: bool
    allow_stop: bool
    allow_force_kill: bool
    protect_system_processes: bool
    max_list_items: int
    stop_timeout_seconds: float


@dataclass(frozen=True)
class SystemConfig:
    enabled: bool


@dataclass(frozen=True)
class PowerShellConfig:
    enabled: bool
    allow_execute: bool
    executable: str
    default_cwd: str
    allowed_working_roots: tuple[str, ...]
    max_timeout_seconds: float
    default_timeout_seconds: float
    max_output_bytes: int
    max_script_chars: int


@dataclass(frozen=True)
class ScreenConfig:
    enabled: bool
    default_max_dimension: int
    max_allowed_dimension: int


@dataclass(frozen=True)
class WindowConfig:
    enabled: bool
    allow_focus: bool
    allow_state_change: bool
    allow_close: bool
    protect_actuator_chain: bool
    max_list_items: int
    close_wait_seconds: float


@dataclass(frozen=True)
class MouseConfig:
    enabled: bool
    allow_move: bool
    allow_click: bool
    allow_scroll: bool
    max_clicks: int
    max_move_duration_ms: int
    max_scroll_steps: int


@dataclass(frozen=True)
class KeyboardConfig:
    enabled: bool
    allow_write: bool
    allow_press: bool
    allow_hotkey: bool
    max_text_chars: int
    max_hotkey_keys: int
    max_press_count: int


@dataclass(frozen=True)
class ClipboardConfig:
    enabled: bool
    allow_read: bool
    allow_write: bool
    max_chars: int


@dataclass(frozen=True)
class VerifiedInputConfig:
    enabled: bool
    focus_text_control: bool
    verify_keyboard_write: bool
    repair_full_selection_failure: bool
    rollback_partial_failure: bool
    verify_wait_ms: int
    max_read_chars: int


@dataclass(frozen=True)
class UIAutomationConfig:
    enabled: bool
    allow_focus: bool
    allow_invoke: bool
    allow_set_value: bool
    allow_toggle: bool
    allow_select: bool
    allow_expand_collapse: bool
    max_depth: int
    max_items: int
    max_value_chars: int
    verify_wait_ms: int


@dataclass(frozen=True)
class AdminConfig:
    enabled: bool
    host: str
    port: int
    allow_config_write: bool
    audit_tail_lines: int


@dataclass(frozen=True)
class AppConfig:
    name: str
    version: str
    filesystem: FileSystemConfig
    process: ProcessConfig
    system: SystemConfig
    powershell: PowerShellConfig
    screen: ScreenConfig
    window: WindowConfig
    mouse: MouseConfig
    keyboard: KeyboardConfig
    clipboard: ClipboardConfig
    verified_input: VerifiedInputConfig
    ui_automation: UIAutomationConfig
    admin: AdminConfig
    logging_enabled: bool
    log_file: str


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    override = os.environ.get("CHATGPT_ACTUATOR_CONFIG")
    if override:
        return Path(override).expanduser()
    return project_root() / "config" / "config.json"


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path is not None else default_config_path()

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in configuration file: {config_path}") from exc

    try:
        server = raw["server"]
        fs = raw["filesystem"]
        logging_cfg = raw.get("logging", {})
        process_cfg = raw.get("process", {})
        system_cfg = raw.get("system", {})
        powershell_cfg = raw.get("powershell", {})
        screen_cfg = raw.get("screen", {})
        window_cfg = raw.get("window", {})
        mouse_cfg = raw.get("mouse", {})
        keyboard_cfg = raw.get("keyboard", {})
        clipboard_cfg = raw.get("clipboard", {})
        verified_input_cfg = raw.get("verified_input", {})
        ui_automation_cfg = raw.get("ui_automation", {})
        admin_cfg = raw.get("admin", {})
        allowed_roots = tuple(str(x) for x in fs["allowed_roots"])
    except (KeyError, TypeError) as exc:
        raise ConfigError("Configuration is missing required fields.") from exc

    if not allowed_roots:
        raise ConfigError("filesystem.allowed_roots must contain at least one path.")

    ps_working_roots = tuple(
        str(x) for x in powershell_cfg.get("allowed_working_roots", allowed_roots)
    )
    if not ps_working_roots:
        raise ConfigError("powershell.allowed_working_roots must contain at least one path.")

    default_cwd = str(
        powershell_cfg.get(
            "default_cwd",
            ps_working_roots[0],
        )
    )

    admin_host = str(admin_cfg.get("host", "127.0.0.1")).strip()
    if admin_host not in {"127.0.0.1", "localhost"}:
        raise ConfigError("admin.host must be loopback-only: 127.0.0.1 or localhost.")

    admin_port = int(admin_cfg.get("port", 8765))
    if not 1 <= admin_port <= 65535:
        raise ConfigError("admin.port must be between 1 and 65535.")

    admin_tail_lines = int(admin_cfg.get("audit_tail_lines", 200))
    if not 1 <= admin_tail_lines <= 5000:
        raise ConfigError("admin.audit_tail_lines must be between 1 and 5000.")

    return AppConfig(
        name=str(server.get("name", "ChatGPT-Actuator")),
        version=str(server.get("version", "1.0.0")),
        filesystem=FileSystemConfig(
            enabled=bool(fs.get("enabled", True)),
            read_only=bool(fs.get("read_only", False)),
            allow_create=bool(fs.get("allow_create", True)),
            allow_edit=bool(fs.get("allow_edit", True)),
            allow_move=bool(fs.get("allow_move", True)),
            allow_delete=bool(fs.get("allow_delete", True)),
            allowed_roots=allowed_roots,
            max_list_items=int(fs.get("max_list_items", 500)),
            max_read_bytes=int(fs.get("max_read_bytes", 1048576)),
            max_search_results=int(fs.get("max_search_results", 200)),
        ),
        process=ProcessConfig(
            enabled=bool(process_cfg.get("enabled", True)),
            allow_start=bool(process_cfg.get("allow_start", True)),
            allow_stop=bool(process_cfg.get("allow_stop", True)),
            allow_force_kill=bool(process_cfg.get("allow_force_kill", True)),
            protect_system_processes=bool(process_cfg.get("protect_system_processes", True)),
            max_list_items=int(process_cfg.get("max_list_items", 300)),
            stop_timeout_seconds=float(process_cfg.get("stop_timeout_seconds", 5.0)),
        ),
        system=SystemConfig(
            enabled=bool(system_cfg.get("enabled", True)),
        ),
        powershell=PowerShellConfig(
            enabled=bool(powershell_cfg.get("enabled", True)),
            allow_execute=bool(powershell_cfg.get("allow_execute", True)),
            executable=str(powershell_cfg.get("executable", "powershell.exe")),
            default_cwd=default_cwd,
            allowed_working_roots=ps_working_roots,
            max_timeout_seconds=float(powershell_cfg.get("max_timeout_seconds", 60.0)),
            default_timeout_seconds=float(powershell_cfg.get("default_timeout_seconds", 20.0)),
            max_output_bytes=int(powershell_cfg.get("max_output_bytes", 1048576)),
            max_script_chars=int(powershell_cfg.get("max_script_chars", 100000)),
        ),
        screen=ScreenConfig(
            enabled=bool(screen_cfg.get("enabled", True)),
            default_max_dimension=int(screen_cfg.get("default_max_dimension", 1920)),
            max_allowed_dimension=int(screen_cfg.get("max_allowed_dimension", 7680)),
        ),
        window=WindowConfig(
            enabled=bool(window_cfg.get("enabled", True)),
            allow_focus=bool(window_cfg.get("allow_focus", True)),
            allow_state_change=bool(window_cfg.get("allow_state_change", True)),
            allow_close=bool(window_cfg.get("allow_close", True)),
            protect_actuator_chain=bool(window_cfg.get("protect_actuator_chain", True)),
            max_list_items=int(window_cfg.get("max_list_items", 300)),
            close_wait_seconds=float(window_cfg.get("close_wait_seconds", 2.0)),
        ),
        mouse=MouseConfig(
            enabled=bool(mouse_cfg.get("enabled", True)),
            allow_move=bool(mouse_cfg.get("allow_move", True)),
            allow_click=bool(mouse_cfg.get("allow_click", True)),
            allow_scroll=bool(mouse_cfg.get("allow_scroll", True)),
            max_clicks=int(mouse_cfg.get("max_clicks", 5)),
            max_move_duration_ms=int(mouse_cfg.get("max_move_duration_ms", 5000)),
            max_scroll_steps=int(mouse_cfg.get("max_scroll_steps", 100)),
        ),
        keyboard=KeyboardConfig(
            enabled=bool(keyboard_cfg.get("enabled", True)),
            allow_write=bool(keyboard_cfg.get("allow_write", True)),
            allow_press=bool(keyboard_cfg.get("allow_press", True)),
            allow_hotkey=bool(keyboard_cfg.get("allow_hotkey", True)),
            max_text_chars=int(keyboard_cfg.get("max_text_chars", 10000)),
            max_hotkey_keys=int(keyboard_cfg.get("max_hotkey_keys", 8)),
            max_press_count=int(keyboard_cfg.get("max_press_count", 100)),
        ),
        clipboard=ClipboardConfig(
            enabled=bool(clipboard_cfg.get("enabled", True)),
            allow_read=bool(clipboard_cfg.get("allow_read", True)),
            allow_write=bool(clipboard_cfg.get("allow_write", True)),
            max_chars=int(clipboard_cfg.get("max_chars", 1000000)),
        ),
        verified_input=VerifiedInputConfig(
            enabled=bool(verified_input_cfg.get("enabled", True)),
            focus_text_control=bool(verified_input_cfg.get("focus_text_control", True)),
            verify_keyboard_write=bool(verified_input_cfg.get("verify_keyboard_write", True)),
            repair_full_selection_failure=bool(verified_input_cfg.get("repair_full_selection_failure", True)),
            rollback_partial_failure=bool(verified_input_cfg.get("rollback_partial_failure", True)),
            verify_wait_ms=int(verified_input_cfg.get("verify_wait_ms", 500)),
            max_read_chars=int(verified_input_cfg.get("max_read_chars", 1000000)),
        ),
        ui_automation=UIAutomationConfig(
            enabled=bool(ui_automation_cfg.get("enabled", True)),
            allow_focus=bool(ui_automation_cfg.get("allow_focus", True)),
            allow_invoke=bool(ui_automation_cfg.get("allow_invoke", True)),
            allow_set_value=bool(ui_automation_cfg.get("allow_set_value", True)),
            allow_toggle=bool(ui_automation_cfg.get("allow_toggle", True)),
            allow_select=bool(ui_automation_cfg.get("allow_select", True)),
            allow_expand_collapse=bool(ui_automation_cfg.get("allow_expand_collapse", True)),
            max_depth=int(ui_automation_cfg.get("max_depth", 10)),
            max_items=int(ui_automation_cfg.get("max_items", 500)),
            max_value_chars=int(ui_automation_cfg.get("max_value_chars", 1000000)),
            verify_wait_ms=int(ui_automation_cfg.get("verify_wait_ms", 500)),
        ),
        admin=AdminConfig(
            enabled=bool(admin_cfg.get("enabled", True)),
            host=admin_host,
            port=admin_port,
            allow_config_write=bool(admin_cfg.get("allow_config_write", True)),
            audit_tail_lines=admin_tail_lines,
        ),
        logging_enabled=bool(logging_cfg.get("enabled", True)),
        log_file=str(logging_cfg.get("file", "logs/audit.log")),
    )
