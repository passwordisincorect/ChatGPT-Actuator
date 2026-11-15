from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tomllib
from typing import Any


SCHEMA_VERSION = 3
CONTEXT_SCHEMA_VERSION = 1
TRANSFER_MARKER = "CHATGPT_PROFILE_MIGRATION_V3"


class AccountBootstrap:
    """Create a portable, non-secret bootstrap pack for a new ChatGPT account.

    The pack deliberately excludes cookies, OAuth tokens, session data, API keys,
    DPAPI blobs, and ChatGPT account credentials. It also does not claim to copy
    OpenAI's account-scoped Memory. Instead, it preserves an explicit portable
    working-context layer: identity preferences, workflow rules, and project state.
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
        self.context_path = self.profile_root / "context.json"
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

    @classmethod
    def _deep_merge_defaults(cls, defaults: Any, existing: Any) -> Any:
        """Merge defaults underneath existing user-authored content.

        Existing values always win. Missing nested keys are filled from defaults.
        Lists are treated as authored values and are not merged item-by-item.
        """
        if isinstance(defaults, dict) and isinstance(existing, dict):
            merged = dict(defaults)
            for key, value in existing.items():
                if key in merged:
                    merged[key] = cls._deep_merge_defaults(merged[key], value)
                else:
                    merged[key] = value
            return merged
        return existing

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
            if preserved.get("destination_action") == legacy_actions.get(name):
                preserved.pop("destination_action", None)
            base.update(preserved)
            enriched.append(base)
        return enriched

    def _default_projects(self) -> list[dict[str, Any]]:
        return [
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

    def _projects(self) -> dict[str, Any]:
        existing = self._read_json(self.projects_path, {})
        projects = existing.get("projects") if isinstance(existing, dict) else None
        if not isinstance(projects, list):
            projects = self._default_projects()
        return {
            "schema_version": SCHEMA_VERSION,
            "workspace_root": str(self.workspace_root),
            "projects": projects,
        }

    def _default_preferences(self) -> dict[str, Any]:
        return {
            "architecture": (
                "ChatGPT MAIN -> Actuator for direct local actions; "
                "Orchestrator for complex delegated/review work"
            ),
            "execution_policy": (
                "background-first; prefer DOM/UIA/native structured interfaces; "
                "avoid physical mouse/keyboard when a semantic path exists"
            ),
            "verification_policy": (
                "inspect before changing; preserve a rollback path; verify meaningful edits"
            ),
            "restart_policy": (
                "do not restart the ChatGPT-Actuator tunnel automatically; "
                "request explicit approval when a tunnel restart is actually required"
            ),
            "account_switch_goal": (
                "restore local project context, habits, and workflow with minimum manual setup"
            ),
        }

    def _preferences(self) -> dict[str, Any]:
        existing = self._read_json(self.preferences_path, {})
        preferences = existing.get("preferences") if isinstance(existing, dict) else None
        if not isinstance(preferences, dict):
            preferences = {}
        preferences = self._deep_merge_defaults(
            self._default_preferences(),
            preferences,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "preferences": preferences,
        }

    def _default_context(self) -> dict[str, Any]:
        return {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "kind": "portable_working_context",
            "official_chatgpt_memory": False,
            "identity_context": {
                "preferred_language": "vi",
                "role_context": (
                    "Sinh viên công nghệ điều khiển và tự động hóa, "
                    "chuyên ngành Robot và AI"
                ),
                "response_preferences": [
                    "Ưu tiên trả lời trực tiếp, thực dụng và có thể triển khai.",
                    "Khi có thao tác máy tính, ưu tiên thực hiện bằng công cụ thay vì chỉ hướng dẫn.",
                    "Giải thích kỹ thuật theo hướng dễ kiểm chứng, nêu rõ giới hạn nếu có.",
                ],
            },
            "workflow_memory": {
                "routing": [
                    "Dùng ChatGPT-Actuator cho thao tác local trực tiếp và workflow ngắn, xác định.",
                    "Dùng ChatGPT-Orchestrator cho công việc phức tạp cần phân vai, review/rework hoặc recovery.",
                ],
                "automation": [
                    "Background-first; tránh chiếm focus.",
                    "Ưu tiên DOM/CDP, API hoặc UI Automation có ngữ nghĩa.",
                    "Không giả lập chuột/bàn phím nếu có đường structured/semantic tương đương.",
                ],
                "safety_and_change_management": [
                    "Kiểm tra trạng thái hiện tại trước khi sửa.",
                    "Tạo rollback/backup phù hợp trước thay đổi đáng kể.",
                    "Patch nhỏ, sau đó test/verify trước khi tuyên bố hoàn tất.",
                    "Không tự restart tunnel ChatGPT-Actuator nếu chưa có yêu cầu/đồng ý rõ ràng.",
                    "Không sao chép cookie ChatGPT, OAuth token, API key, session token hoặc DPAPI secret.",
                ],
                "browser": [
                    "Worker dùng cửa sổ Edge riêng, chạy nền; không dùng Main Edge cho worker automation.",
                    "Ưu tiên một worker window dùng chung với các tab pooled/background.",
                    "Main và Worker nên dùng cùng tài khoản ChatGPT sau khi đổi tài khoản.",
                ],
                "eda": [
                    "Ưu tiên hoàn thiện schematic trước PCB.",
                    "PCB giai đoạn đầu chỉ cần sắp xếp linh kiện, chưa route trace.",
                    "Schematic: đúng điện trước, bố trí trước wiring, dây trực giao và verify ERC sau thay đổi.",
                    "Không đoán kết nối điện khi ý định mơ hồ.",
                ],
                "learning": [
                    "C++ cơ bản ưu tiên thực hành, đọc code, tự viết và debug.",
                    "Ví dụ có thể gắn với STM32/robotics khi phù hợp.",
                ],
            },
            "project_memory": {
                "ChatGPT-Actuator": {
                    "path": str(self.actuator_root),
                    "state": "active",
                    "notes": [
                        "Local Windows execution plane with permission gates.",
                        "Background-safe automation is preferred.",
                        "Experimental EDA/KiCad schematic tooling is integrated.",
                    ],
                },
                "ChatGPT-Orchestrator": {
                    "path": str(self.orchestrator_root),
                    "state": "active",
                    "notes": [
                        "Multi-worker control plane for complex work.",
                        "Dedicated Worker Edge with background DOM/CDP is preferred.",
                        "Persistent project/task state is used when durable context is needed.",
                    ],
                },
                "KiCad-Schematic": {
                    "path_hint": str(self.workspace_root),
                    "state": "active",
                    "notes": [
                        "Schematic-first workflow.",
                        "Use structured/file-backed KiCad editing and verification instead of mouse/keyboard automation.",
                    ],
                },
                "C++-Training": {
                    "state": "active",
                    "notes": [
                        "10-session beginner curriculum for Robotics & Technology club.",
                        "Target balance: 30% theory, 70% practice.",
                    ],
                },
            },
        }

    def _context(self) -> dict[str, Any]:
        existing = self._read_json(self.context_path, {})
        if not isinstance(existing, dict):
            existing = {}
        merged = self._deep_merge_defaults(self._default_context(), existing)
        merged["schema_version"] = CONTEXT_SCHEMA_VERSION
        merged["kind"] = "portable_working_context"
        merged["official_chatgpt_memory"] = False
        return merged

    def _profile(
        self,
        plugins: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> dict[str, Any]:
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
            "portable_context": {
                "file": str(self.context_path),
                "layers": [
                    "identity_context",
                    "workflow_memory",
                    "project_memory",
                ],
                "official_chatgpt_memory": bool(
                    context.get("official_chatgpt_memory", False)
                ),
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
                "copy_official_chatgpt_memory": False,
                "reason": (
                    "Credentials and OpenAI account-scoped state remain account/device "
                    "scoped and are not migrated by the local bootstrap pack."
                ),
            },
        }

    @staticmethod
    def _markdown_value(value: Any, indent: int = 0) -> list[str]:
        prefix = "  " * indent
        lines: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    lines.append(f"{prefix}- {key}:")
                    lines.extend(AccountBootstrap._markdown_value(item, indent + 1))
                else:
                    lines.append(f"{prefix}- {key}: {item}")
            return lines
        if isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    lines.append(f"{prefix}-")
                    lines.extend(AccountBootstrap._markdown_value(item, indent + 1))
                else:
                    lines.append(f"{prefix}- {item}")
            return lines
        return [f"{prefix}- {value}"]

    def _bootstrap_markdown(
        self,
        profile: dict[str, Any],
        plugins: list[dict[str, Any]],
        projects: dict[str, Any],
        preferences: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        lines = [
            "# ChatGPT Account Bootstrap",
            "",
            f"Generated: {profile['generated_at']}",
            "",
            "This is a portable working-context handoff, not an import of OpenAI account Memory.",
            "Use it after signing in to the destination ChatGPT account.",
            "It restores explicit local workflow/project context and never copies authentication secrets.",
            "",
            "## Local services",
            f"- ChatGPT-Actuator: {self.actuator_root}",
            f"- ChatGPT-Orchestrator: {self.orchestrator_root}",
            "- Control Center: http://127.0.0.1:8764/",
            "",
            "## Portable preferences",
        ]
        lines.extend(self._markdown_value(preferences.get("preferences", {})))

        lines.extend(["", "## Identity context"])
        lines.extend(self._markdown_value(context.get("identity_context", {})))

        lines.extend(["", "## Workflow memory"])
        lines.extend(self._markdown_value(context.get("workflow_memory", {})))

        lines.extend(["", "## Project memory"])
        lines.extend(self._markdown_value(context.get("project_memory", {})))

        lines.extend(["", "## Portable project paths"])
        for item in projects.get("projects", []):
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('name', 'unknown')}: "
                f"{item.get('path', '-')}; portable={bool(item.get('portable', True))}"
            )

        lines.extend(["", "## Plugins/connectors to restore"])
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
                "## Bootstrap instructions for the destination chat",
                "",
                "Treat the sections above as explicit portable working context for this user.",
                "Use identity_context for communication preferences, workflow_memory for default "
                "operating rules, and project_memory only when the current request is relevant.",
                "Do not claim this imported OpenAI Memory or reconstructed hidden personalization.",
                "Use ChatGPT-Actuator for direct local actions and chatgpt-orchestrator for "
                "complex delegated/review work when available.",
                f"The local workspace root is {self.workspace_root}.",
                "Do not copy or request old ChatGPT cookies, OAuth tokens, API keys, session "
                "data, or DPAPI secrets.",
                "Read plugins.json from this bootstrap pack when local file access is available.",
                "For public plugins, use the canonical plugin_id when available. For private "
                "Tunnel connectors, recreate/connect them with the recorded tunnel_profile; "
                "source_account_app_id values are references only and must not be treated as "
                "portable credentials.",
                "Ask only for authorizations that the destination account must grant.",
                "",
                "## What still remains account-scoped",
                "",
                "Official ChatGPT Memory, chat history, subscription, workspace membership, "
                "and OAuth-backed authorizations are not cloned by this local migration.",
                "",
            ]
        )
        return "\n".join(lines)

    def export(self) -> dict[str, Any]:
        self.profile_root.mkdir(parents=True, exist_ok=True)
        plugins = self._plugins()
        projects = self._projects()
        preferences = self._preferences()
        context = self._context()
        profile = self._profile(plugins, context)

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
        self._write_json(self.context_path, context)
        self.bootstrap_path.write_text(
            self._bootstrap_markdown(
                profile,
                plugins,
                projects,
                preferences,
                context,
            ),
            encoding="utf-8",
        )

        return self.status()

    def bootstrap_text(self) -> str:
        if not self.bootstrap_path.is_file():
            self.export()
        return self.bootstrap_path.read_text(encoding="utf-8")

    def transfer_prompt(self) -> str:
        """Return the single message intended to be sent to a destination chat."""
        self.export()
        return (
            f"{TRANSFER_MARKER}\n"
            "This message transfers my explicit portable working context from my previous "
            "local ChatGPT setup. It is not an import of official ChatGPT Memory.\n\n"
            + self.bootstrap_text()
        )

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
            "context_layers": current["context_layers"],
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
                "Portable working context is ready. Account-bound plugins/connectors "
                "still require destination-account authorization."
            ),
            "bootstrap_text": self.transfer_prompt(),
        }

    def status(self) -> dict[str, Any]:
        files = {
            "profile": str(self.profile_path),
            "plugins": str(self.plugins_path),
            "projects": str(self.projects_path),
            "preferences": str(self.preferences_path),
            "context": str(self.context_path),
            "bootstrap": str(self.bootstrap_path),
        }
        profile = self._read_json(self.profile_path, {})
        context = self._read_json(self.context_path, {})
        plugins = self._plugins()
        manual_count = sum(
            1
            for item in plugins
            if item.get("account_bound", True)
            and item.get("authorization") not in {"none", "not_required"}
        )
        layers = [
            key
            for key in ("identity_context", "workflow_memory", "project_memory")
            if isinstance(context.get(key), dict)
        ]
        return {
            "profile_root": str(self.profile_root),
            "exists": self.profile_path.is_file(),
            "generated_at": profile.get("generated_at"),
            "schema_version": profile.get("schema_version"),
            "files": files,
            "portable_file_count": sum(Path(path).is_file() for path in files.values()),
            "portable_file_total": len(files),
            "plugin_count": len(plugins),
            "manual_authorization_count": manual_count,
            "context_layers": layers,
            "context_layer_count": len(layers),
            "transfer_marker": TRANSFER_MARKER,
            "secrets_included": False,
            "official_chatgpt_memory_included": False,
        }
