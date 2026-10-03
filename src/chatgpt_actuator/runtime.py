from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import re
from threading import RLock

from .audit import AuditLogger
from .config import AppConfig, default_config_path, load_config


@dataclass(frozen=True)
class RuntimeBundle:
    config: AppConfig
    audit: AuditLogger
    filesystem: object
    process_system: object
    powershell: object
    screen: object
    window: object
    mouse: object
    keyboard: object
    clipboard: object
    verified_input: object
    uia: object


class DynamicServiceProxy:
    def __init__(self, manager: "RuntimeManager", service_name: str) -> None:
        self._manager = manager
        self._service_name = service_name

    def __getattr__(self, name: str):
        service = self._manager.get_service(self._service_name)
        return getattr(service, name)


class RuntimeManager:
    MUTABLE_SECTIONS = {
        "filesystem",
        "process",
        "system",
        "powershell",
        "screen",
        "window",
        "mouse",
        "keyboard",
        "clipboard",
        "verified_input",
        "ui_automation",
    }

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path else default_config_path()
        self._lock = RLock()
        self._apply_lock = RLock()
        self._generation = 1
        initial = load_config(self.config_path)
        self._bundle = self._build_bundle(initial)

    @staticmethod
    def _build_bundle(config: AppConfig) -> RuntimeBundle:
        from .tools import (
            ClipboardService,
            FileSystemService,
            KeyboardService,
            MouseService,
            PowerShellService,
            ProcessSystemService,
            ScreenService,
            UIAutomationService,
            VerifiedInputService,
            WindowService,
        )

        audit = AuditLogger(config.logging_enabled, config.log_file)
        return RuntimeBundle(
            config=config,
            audit=audit,
            filesystem=FileSystemService(config.filesystem, audit),
            process_system=ProcessSystemService(config.process, audit),
            powershell=PowerShellService(config.powershell, audit),
            screen=ScreenService(config.screen, audit),
            window=WindowService(config.window, audit),
            mouse=MouseService(config.mouse, audit),
            keyboard=KeyboardService(config.keyboard, audit),
            clipboard=ClipboardService(config.clipboard, audit),
            verified_input=VerifiedInputService(config.verified_input, audit),
            uia=UIAutomationService(config.ui_automation, audit),
        )

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return self._bundle.config

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def audit(self) -> AuditLogger:
        with self._lock:
            return self._bundle.audit

    def get_service(self, name: str):
        with self._lock:
            return getattr(self._bundle, name)

    def proxy(self, name: str) -> DynamicServiceProxy:
        return DynamicServiceProxy(self, name)

    def raw_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def apply_patch(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise TypeError("Configuration patch must be a JSON object.")

        unknown = set(patch) - self.MUTABLE_SECTIONS
        if unknown:
            raise ValueError(
                "Admin UI cannot modify these top-level sections: "
                + ", ".join(sorted(unknown))
            )

        with self._apply_lock:
            current_raw = self.raw_config()
            candidate_raw = deepcopy(current_raw)

            for section, value in patch.items():
                if not isinstance(value, dict):
                    raise TypeError(f"Section '{section}' must be a JSON object.")
                existing = candidate_raw.get(section)
                if not isinstance(existing, dict):
                    existing = {}
                    candidate_raw[section] = existing
                self._deep_merge(existing, value)

            candidate_raw["server"] = deepcopy(current_raw.get("server", {}))
            candidate_raw["admin"] = deepcopy(current_raw.get("admin", {}))

            pending = self.config_path.with_name(
                self.config_path.name + f".pending.{os.getpid()}"
            )
            pending.write_text(
                json.dumps(candidate_raw, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            try:
                candidate_config = load_config(pending)
                candidate_bundle = self._build_bundle(candidate_config)
            except Exception:
                try:
                    pending.unlink(missing_ok=True)
                finally:
                    raise

            backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
            shutil.copy2(self.config_path, backup)
            os.replace(pending, self.config_path)

            with self._lock:
                old_generation = self._generation
                self._bundle = candidate_bundle
                self._generation += 1
                generation = self._generation

            candidate_bundle.audit.record(
                "admin_config_applied",
                generation=generation,
                previous_generation=old_generation,
                changed_sections=sorted(patch.keys()),
                backup=str(backup),
            )

            return self.status()

    def reload_from_disk(self) -> dict:
        with self._apply_lock:
            candidate_config = load_config(self.config_path)
            candidate_bundle = self._build_bundle(candidate_config)
            with self._lock:
                old_generation = self._generation
                self._bundle = candidate_bundle
                self._generation += 1
                generation = self._generation

            candidate_bundle.audit.record(
                "admin_config_reloaded",
                generation=generation,
                previous_generation=old_generation,
            )
            return self.status()

    def status(self) -> dict:
        cfg = self.config
        return {
            "name": cfg.name,
            "version": cfg.version,
            "generation": self.generation,
            "capabilities": {
                "filesystem": {
                    "enabled": cfg.filesystem.enabled,
                    "read_only": cfg.filesystem.read_only,
                    "allow_create": cfg.filesystem.allow_create,
                    "allow_edit": cfg.filesystem.allow_edit,
                    "allow_move": cfg.filesystem.allow_move,
                    "allow_delete": cfg.filesystem.allow_delete,
                    "allowed_roots": list(cfg.filesystem.allowed_roots),
                },
                "system": {"enabled": cfg.system.enabled},
                "process": {
                    "enabled": cfg.process.enabled,
                    "allow_start": cfg.process.allow_start,
                    "allow_stop": cfg.process.allow_stop,
                    "allow_force_kill": cfg.process.allow_force_kill,
                },
                "powershell": {
                    "enabled": cfg.powershell.enabled,
                    "allow_execute": cfg.powershell.allow_execute,
                    "default_cwd": cfg.powershell.default_cwd,
                    "allowed_working_roots": list(cfg.powershell.allowed_working_roots),
                },
                "screen": {"enabled": cfg.screen.enabled},
                "window": {
                    "enabled": cfg.window.enabled,
                    "allow_focus": cfg.window.allow_focus,
                    "allow_state_change": cfg.window.allow_state_change,
                    "allow_close": cfg.window.allow_close,
                },
                "mouse": {
                    "enabled": cfg.mouse.enabled,
                    "allow_move": cfg.mouse.allow_move,
                    "allow_click": cfg.mouse.allow_click,
                    "allow_scroll": cfg.mouse.allow_scroll,
                },
                "keyboard": {
                    "enabled": cfg.keyboard.enabled,
                    "allow_write": cfg.keyboard.allow_write,
                    "allow_press": cfg.keyboard.allow_press,
                    "allow_hotkey": cfg.keyboard.allow_hotkey,
                },
                "clipboard": {
                    "enabled": cfg.clipboard.enabled,
                    "allow_read": cfg.clipboard.allow_read,
                    "allow_write": cfg.clipboard.allow_write,
                },
                "verified_input": {
                    "enabled": cfg.verified_input.enabled,
                    "verify_keyboard_write": cfg.verified_input.verify_keyboard_write,
                    "repair_full_selection_failure": cfg.verified_input.repair_full_selection_failure,
                    "rollback_partial_failure": cfg.verified_input.rollback_partial_failure,
                },
                "ui_automation": {
                    "enabled": cfg.ui_automation.enabled,
                    "allow_focus": cfg.ui_automation.allow_focus,
                    "allow_invoke": cfg.ui_automation.allow_invoke,
                    "allow_set_value": cfg.ui_automation.allow_set_value,
                    "allow_toggle": cfg.ui_automation.allow_toggle,
                    "allow_select": cfg.ui_automation.allow_select,
                    "allow_expand_collapse": cfg.ui_automation.allow_expand_collapse,
                    "allow_scroll": cfg.ui_automation.allow_scroll,
                    "allow_scroll_into_view": cfg.ui_automation.allow_scroll_into_view,
                    "allow_range_value": cfg.ui_automation.allow_range_value,
                    "allow_text_selection": cfg.ui_automation.allow_text_selection,
                    "allow_window_action": cfg.ui_automation.allow_window_action,
                },
            },
            "admin": {
                "enabled": cfg.admin.enabled,
                "host": cfg.admin.host,
                "port": cfg.admin.port,
                "allow_config_write": cfg.admin.allow_config_write,
            },
        }

    def audit_tail(self, limit: int | None = None) -> list[dict]:
        cfg = self.config
        requested = cfg.admin.audit_tail_lines if limit is None else int(limit)
        requested = max(1, min(requested, 5000))
        path = self.audit.path

        if not path.exists():
            return []

        lines: deque[str] = deque(maxlen=requested)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for physical_line in handle:
                # v0.1-v0.7 accidentally wrote the two characters "\\n"
                # between events. Preserve compatibility with those logs while
                # using real newlines for all new events.
                legacy_line = physical_line.rstrip("\n")
                for line in re.split(r"(?<=})\\n(?={)", legacy_line):
                    if line:
                        lines.append(line)

        events = []
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
                else:
                    events.append({"raw": line})
            except json.JSONDecodeError:
                events.append({"raw": line})
        return events

    @staticmethod
    def _deep_merge(target: dict, patch: dict) -> None:
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                RuntimeManager._deep_merge(target[key], value)
            else:
                target[key] = deepcopy(value)
