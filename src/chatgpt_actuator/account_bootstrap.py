from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tomllib
from typing import Any


SCHEMA_VERSION = 2


class AccountBootstrap:
    """Create a portable, non-secret bootstrap pack for a new ChatGPT account.

    The pack deliberately excludes cookies, OAuth tokens, session data, API keys,
    DPAPI blobs, and chat/account credentials. Account-bound plugins still need
    authorization in the destination ChatGPT account.
    """

    def __init__(self, actuator_root: Path) -> None:
        self.actuator_root = actuator_root.resolve()
        self.workspace_root = self.actuator_root.parent
        configured = os.environ.get("CHATGPT_ACCOUNT_PROFILE_ROOT", "").strip()
        self.profile_root = (
            Path(configured).expanduser().resolve()
            if configured
            else (self.workspace_root / "ChatGPT-Profile")
        )
        self.orchestrator_root = self.workspace_root / "ChatGPT-Orchestrator"
        self.profile_path = self.profile_root / "profile.json"
        self.plugins_path = self.profile_root / "plugins.json"
        self.projects_path = self.profile_root / "projects.json"
        self.preferences_path = self.profile_root / "preferences.json"
        self.bootstrap_path = self.profile_root / "bootstrap.md"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)

    @staticmethod
    def _project_version(root: Path) -> str:
        path = root / "pyproject.toml"
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            return str(data.get("project", {}).get("version", "unknown"))
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
            return "unknown"

    def _default_plugins(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "ChatGPT-Actuator",
                "kind": "tunnel_connector",
                "purpose": "Local Windows execution plane",
                "account_bound": True,
                "portable_identity": False,
                "source_account_app_id": "asdk_app_6ac0e5b1a9008191abc12997e4776085",
                "tunnel_profile": "chatgpt-actuator",
                "local_backend": str(self.actuator_root),
                "destination_action": (
                    "Recreate/connect the ChatGPT-Actuator Tunnel connector in the new "
                    "ChatGPT account using tunnel profile chatgpt-actuator, then approve access. "
                    "The source-account app id is reference-only and is not portable."
                ),
                "authorization": "required",
            },
            {
                "name": "chatgpt-orchestrator",
                "kind": "tunnel_connector",
                "purpose": "Delegation, multi-worker review, recovery, and complex reasoning",
                "account_bound": True,
                "portable_identity": False,
                "source_account_app_id": "asdk_app_6abf9a6d904c8191a35a454ca631951d",
                "tunnel_profile": "chatgpt-orchestrator",
                "local_backend": str(self.orchestrator_root),
                "destination_action": (
                    "Recreate/connect the chatgpt-orchestrator Tunnel connector in the new "
                    "ChatGPT account using tunnel profile chatgpt-orchestrator, then approve access. "
                    "The source-account app id is reference-only and is not portable."
                ),
                "authorization": "required",
            },
            {
                "name": "Google Drive",
                "kind": "public_plugin",
                "purpose": "Connected Drive/Docs/Sheets/Slides access when used",
                "account_bound": True,
                "portable_identity": True,
                "plugin_id": "plugin_connector_1p_ab21a553bfbc81919ea8fd1858e3ffa7",
                "installed_in_source_account": True,
                "local_backend": None,
                "destination_action": (
                    "Install Google Drive by canonical plugin id "
                    "plugin_connector_1p_ab21a553bfbc81919ea8fd1858e3ffa7 "
                    "and authorize the Google account in the destination ChatGPT account."
                ),
                "authorization": "required_if_used",
            },
        ]

    def _plugins(self) -> list[dict[str, Any]]:
        current = self._read_json(self.plugins_path, None)
        if isinstance(current, dict) and isinstance(current.get("plugins"), list):
            items = current["plugins"]
        elif isinstance(current, list):
            items = current
        else:
            return self._default_plugins()

        # Enrich known entries with portable metadata without overwriting
        # user-authored fields. Unknown/custom entries are preserved unchanged.
        defaults = {item["name"]: item for item in self._default_plugins()}
        legacy_actions = {
            "ChatGPT-Actuator": "Install/connect in the new ChatGPT account and approve access.",
            "chatgpt-orchestrator": "Install/connect in the new ChatGPT account and approve access.",
            "Google Drive": "Reconnect Google authorization only if needed on the new account.",
        }
        enriched: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            base = dict(defaults.get(name, {}))
            preserved = dict(item)
            # Migrate only exact strings emitted by schema v1. Any human-edited
            # destination_action remains authoritative.
            if preserved.get("destination_action") == legacy_actions.get(name):
                preserved.pop("destination_action", None)
            base.update(preserved)
            enriched.append(base)
        return enriched

    def _projects(self) -> dict[str, Any]:
        existing = self._read_json(self.projects_path, {})
        projects = existing.get("projects") if isinstance(existing, dict) else None
        if not isinstance(projects, list):
            projects = [
                {
                    "name": "ChatGPT-Actuator",
                    "path": str(self.actuator_root),
                    "portable": True,
                },
                {
                    "name": "ChatGPT-Orchestrator",
                    "path": str(self.orchestrator_root),
                    "portable": True,
                },
            ]
        return {
            "schema_version": SCHEMA_VERSION,
            "workspace_root": str(self.workspace_root),
            "projects": projects,
        }

    def _preferences(self) -> dict[str, Any]:
        existing = self._read_json(self.preferences_path, {})
        preferences = existing.get("preferences") if isinstance(existing, dict) else None
        if not isinstance(preferences, dict):
            preferences = {
                "architecture": "ChatGPT MAIN -> Actuator for direct actions; Orchestrator for complex delegated work",
                "execution_policy": "background-first, semantic automation preferred; avoid copying account credentials",
                "account_switch_goal": "restore local project context with minimum manual setup",
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "preferences": preferences,
        }

    def _profile(self, plugins: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self._now(),
            "profile_root": str(self.profile_root),
            "workspace_root": str(self.workspace_root),
            "services": {
                "actuator": {
                    "path": str(self.actuator_root),
                    "version": self._project_version(self.actuator_root),
                },
                "orchestrator": {
                    "path": str(self.orchestrator_root),
                    "version": self._project_version(self.orchestrator_root),
                    "present": self.orchestrator_root.is_dir(),
                },
                "control_center": {
                    "url": "http://127.0.0.1:8764/",
                    "loopback_only": True,
                },
                "tunnel": {
                    "profile": os.environ.get(
                        "CHATGPT_ACTUATOR_TUNNEL_PROFILE",
                        "chatgpt-actuator",
                    ),
                },
            },
            "plugins": [
                {
                    "name": item.get("name", "unknown"),
                    "account_bound": bool(item.get("account_bound", True)),
                    "authorization": item.get("authorization", "required"),
                }
                for item in plugins
            ],
            "secrets_policy": {
                "copy_cookies": False,
                "copy_oauth_tokens": False,
                "copy_chatgpt_session": False,
                "copy_api_keys": False,
                "copy_dpapi_secrets": False,
                "reason": "Credentials remain account/device scoped and must not be migrated by the bootstrap pack.",
            },
        }

    def _bootstrap_markdown(
        self,
        profile: dict[str, Any],
        plugins: list[dict[str, Any]],
    ) -> str:
        lines = [
            "# ChatGPT Account Bootstrap",
            "",
            f"Generated: {profile['generated_at']}",
            "",
            "Use this pack after signing in to the destination ChatGPT account.",
            "It restores local Agent OS context; it does not copy authentication secrets.",
            "",
            "## Local services",
            f"- ChatGPT-Actuator: {self.actuator_root}",
            f"- ChatGPT-Orchestrator: {self.orchestrator_root}",
            "- Control Center: http://127.0.0.1:8764/",
            "",
            "## Plugins/connectors to restore",
        ]
        for item in plugins:
            details: list[str] = []
            if item.get("kind"):
                details.append(f"kind={item['kind']}")
            if item.get("plugin_id"):
                details.append(f"plugin_id={item['plugin_id']}")
            if item.get("tunnel_profile"):
                details.append(f"tunnel_profile={item['tunnel_profile']}")
            if item.get("source_account_app_id"):
                details.append(
                    f"source_account_app_id={item['source_account_app_id']} (reference only)"
                )
            suffix = f" [{'; '.join(details)}]" if details else ""
            lines.append(
                f"- {item.get('name', 'unknown')}{suffix}: "
                f"{item.get('destination_action', 'Reconnect in the new account.')}"
            )
        lines.extend(
            [
                "",
                "## Bootstrap prompt for the new account",
                "",
                "I use a local Agent OS with ChatGPT MAIN as the interface. "
                "Use ChatGPT-Actuator for direct local actions and "
                "chatgpt-orchestrator for complex delegated/review work. "
                f"The local workspace root is {self.workspace_root}. "
                "Do not copy or request old ChatGPT cookies, OAuth tokens, API keys, "
                "session data, or DPAPI secrets. Read plugins.json from this bootstrap pack. "
                "For public plugins, use the canonical plugin_id when available. For private "
                "Tunnel connectors, recreate/connect them with the recorded tunnel_profile; "
                "source_account_app_id values are references only and must not be treated as "
                "portable credentials. Ask only for authorizations that the new account must grant.",
                "",
                "## What still requires account authorization",
                "",
                "ChatGPT plugins/connectors, OAuth-backed services, workspace membership, "
                "subscription, ChatGPT Memory, and chat history remain account-scoped.",
                "",
            ]
        )
        return "\n".join(lines)

    def export(self) -> dict[str, Any]:
        self.profile_root.mkdir(parents=True, exist_ok=True)
        plugins = self._plugins()
        projects = self._projects()
        preferences = self._preferences()
        profile = self._profile(plugins)

        self._write_json(self.profile_path, profile)
        self._write_json(
            self.plugins_path,
            {
                "schema_version": SCHEMA_VERSION,
                "plugins": plugins,
            },
        )
        self._write_json(self.projects_path, projects)
        self._write_json(self.preferences_path, preferences)
        self.bootstrap_path.write_text(
            self._bootstrap_markdown(profile, plugins),
            encoding="utf-8",
        )

        return self.status()

    def bootstrap_text(self) -> str:
        if not self.bootstrap_path.is_file():
            self.export()
        return self.bootstrap_path.read_text(encoding="utf-8")

    def prepare_destination(self) -> dict[str, Any]:
        current = self.export()
        plugins = self._plugins()
        manual = [
            item
            for item in plugins
            if item.get("account_bound", True)
            and item.get("authorization") not in {"none", "not_required"}
        ]
        return {
            "ok": True,
            "local_profile_ready": True,
            "profile_root": str(self.profile_root),
            "portable_files": current["files"],
            "manual_authorizations": [
                {
                    "name": item.get("name", "unknown"),
                    "kind": item.get("kind"),
                    "plugin_id": item.get("plugin_id"),
                    "tunnel_profile": item.get("tunnel_profile"),
                    "action": item.get(
                        "destination_action",
                        "Reconnect/authorize in the destination account.",
                    ),
                }
                for item in manual
            ],
            "message": (
                "Local bootstrap pack is ready. Account-bound plugins/connectors "
                "still require destination-account authorization."
            ),
            "bootstrap_text": self.bootstrap_text(),
        }

    def status(self) -> dict[str, Any]:
        files = {
            "profile": str(self.profile_path),
            "plugins": str(self.plugins_path),
            "projects": str(self.projects_path),
            "preferences": str(self.preferences_path),
            "bootstrap": str(self.bootstrap_path),
        }
        profile = self._read_json(self.profile_path, {})
        plugins = self._plugins()
        manual_count = sum(
            1
            for item in plugins
            if item.get("account_bound", True)
            and item.get("authorization") not in {"none", "not_required"}
        )
        return {
            "profile_root": str(self.profile_root),
            "exists": self.profile_path.is_file(),
            "generated_at": profile.get("generated_at"),
            "files": files,
            "portable_file_count": sum(Path(path).is_file() for path in files.values()),
            "plugin_count": len(plugins),
            "manual_authorization_count": manual_count,
            "secrets_included": False,
        }
