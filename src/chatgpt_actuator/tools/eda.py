from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from ..audit import AuditLogger
from ..config import EDAConfig
from ..security import PathGuard


_KICAD_EXTENSIONS = {".kicad_pro", ".kicad_sch", ".kicad_pcb", ".kicad_prl", ".kicad_dru"}
_ALTIUM_EXTENSIONS = {".prjpcb", ".schdoc", ".pcbdoc"}
_EDA_EXTENSIONS = _KICAD_EXTENSIONS | _ALTIUM_EXTENSIONS


class EDAService:
    """Permission-gated EDA gateway.

    Phase 1 intentionally keeps the core independent from KiCad/Altium Python APIs.
    Read/verification/export operations use vendor CLI entry points when available.
    Native editing is reserved behind allow_edit for a future IPC/script adapter.
    """

    def __init__(self, config: EDAConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit
        self.guard = PathGuard(config.allowed_project_roots)

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("EDA tools are disabled.")

    def _require_read(self) -> None:
        self._require_enabled()
        if not self.config.allow_read:
            raise PermissionError("EDA read/analysis operations are disabled.")

    def _require_manufacturing(self) -> None:
        self._require_enabled()
        if not self.config.allow_manufacturing:
            raise PermissionError(
                "EDA manufacturing export is disabled. Enable eda.allow_manufacturing explicitly."
            )

    def _discover_kicad_cli(self) -> Path | None:
        configured = self.config.kicad_cli.strip()
        if configured:
            candidate = Path(os.path.expandvars(configured)).expanduser()
            if candidate.is_file():
                return candidate.resolve()

        found = shutil.which("kicad-cli.exe") or shutil.which("kicad-cli")
        if found:
            return Path(found).resolve()

        roots = [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramW6432"),
            os.environ.get("ProgramFiles(x86)"),
        ]
        candidates: list[Path] = []
        for raw_root in roots:
            if not raw_root:
                continue
            base = Path(raw_root) / "KiCad"
            if not base.exists():
                continue
            candidates.extend(base.glob("*/bin/kicad-cli.exe"))
            direct = base / "bin" / "kicad-cli.exe"
            if direct.exists():
                candidates.append(direct)

        existing = [p.resolve() for p in candidates if p.is_file()]
        if not existing:
            return None
        return sorted(existing, key=lambda p: str(p).casefold(), reverse=True)[0]

    def _discover_altium(self) -> Path | None:
        configured = self.config.altium_executable.strip()
        if configured:
            candidate = Path(os.path.expandvars(configured)).expanduser()
            if candidate.is_file():
                return candidate.resolve()

        roots = [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramW6432"),
            os.environ.get("ProgramFiles(x86)"),
        ]
        patterns = (
            "Altium*/**/X2.EXE",
            "Altium*/X2.EXE",
        )
        candidates: list[Path] = []
        for raw_root in roots:
            if not raw_root:
                continue
            base = Path(raw_root)
            for pattern in patterns:
                try:
                    candidates.extend(base.glob(pattern))
                except OSError:
                    continue
        existing = [p.resolve() for p in candidates if p.is_file()]
        if not existing:
            return None
        return sorted(existing, key=lambda p: str(p).casefold(), reverse=True)[0]

    def info(self) -> dict:
        self._require_enabled()
        kicad = self._discover_kicad_cli()
        altium = self._discover_altium()
        result = {
            "enabled": True,
            "permissions": {
                "read": self.config.allow_read,
                "edit": self.config.allow_edit,
                "manufacturing": self.config.allow_manufacturing,
            },
            "allowed_project_roots": [str(p) for p in self.guard.roots],
            "backends": {
                "kicad": {
                    "detected": kicad is not None,
                    "cli": str(kicad) if kicad else None,
                    "analysis": kicad is not None and self.config.allow_read,
                    "native_edit": False,
                    "native_edit_reason": (
                        "KiCad IPC adapter is not installed in this Actuator phase."
                        if self.config.allow_edit
                        else "eda.allow_edit is disabled."
                    ),
                },
                "altium": {
                    "detected": altium is not None,
                    "executable": str(altium) if altium else None,
                    "analysis": False,
                    "native_edit": False,
                    "native_edit_reason": (
                        "Altium scripting adapter is not installed in this Actuator phase."
                        if self.config.allow_edit
                        else "eda.allow_edit is disabled."
                    ),
                },
            },
        }
        self.audit.record(
            "eda_info",
            kicad_detected=kicad is not None,
            altium_detected=altium is not None,
            allow_edit=self.config.allow_edit,
            allow_manufacturing=self.config.allow_manufacturing,
        )
        return result

    def project_inspect(self, path: str) -> dict:
        self._require_read()
        resolved = self.guard.resolve_existing(path)
        root = resolved if resolved.is_dir() else resolved.parent
        if not root.is_dir():
            raise NotADirectoryError(str(root))

        files = []
        for item in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
            if not item.is_file() or item.suffix.casefold() not in _EDA_EXTENSIONS:
                continue
            safe = self.guard.resolve_existing(item)
            stat = safe.stat()
            suffix = safe.suffix.casefold()
            backend = "kicad" if suffix in _KICAD_EXTENSIONS else "altium"
            files.append(
                {
                    "name": safe.name,
                    "path": str(safe),
                    "backend": backend,
                    "kind": self._kind_for_suffix(suffix),
                    "size_bytes": stat.st_size,
                    "modified_unix": stat.st_mtime,
                }
            )

        requested = str(resolved)
        result = {
            "requested_path": requested,
            "project_root": str(root),
            "files": files,
            "detected_backends": sorted({item["backend"] for item in files}),
        }
        self.audit.record("eda_project_inspect", path=requested, files=len(files))
        return result

    @staticmethod
    def _kind_for_suffix(suffix: str) -> str:
        mapping = {
            ".kicad_pro": "project",
            ".kicad_sch": "schematic",
            ".kicad_pcb": "pcb",
            ".kicad_prl": "project_local_settings",
            ".kicad_dru": "design_rules",
            ".prjpcb": "project",
            ".schdoc": "schematic",
            ".pcbdoc": "pcb",
        }
        return mapping.get(suffix, "eda_file")

    def kicad_erc(self, schematic_path: str, report_path: str | None = None) -> dict:
        self._require_read()
        schematic = self._resolve_file(schematic_path, {".kicad_sch"}, "KiCad schematic")
        report = self._resolve_report_target(
            report_path,
            schematic.with_suffix(".erc.rpt"),
        )
        return self._run_kicad(
            ["sch", "erc", "--output", str(report), str(schematic)],
            cwd=schematic.parent,
            operation="eda_kicad_erc",
            artifacts=[report],
        )

    def kicad_drc(self, pcb_path: str, report_path: str | None = None) -> dict:
        self._require_read()
        pcb = self._resolve_file(pcb_path, {".kicad_pcb"}, "KiCad PCB")
        report = self._resolve_report_target(
            report_path,
            pcb.with_suffix(".drc.rpt"),
        )
        return self._run_kicad(
            ["pcb", "drc", "--output", str(report), str(pcb)],
            cwd=pcb.parent,
            operation="eda_kicad_drc",
            artifacts=[report],
        )

    def kicad_export_gerbers(self, pcb_path: str, output_dir: str) -> dict:
        self._require_manufacturing()
        pcb = self._resolve_file(pcb_path, {".kicad_pcb"}, "KiCad PCB")
        target = self.guard.resolve_target(output_dir)
        if target.exists():
            target = self.guard.resolve_existing(target)
            if not target.is_dir():
                raise NotADirectoryError(str(target))
            if any(target.iterdir()):
                raise FileExistsError(
                    "Gerber output directory must be empty to avoid overwriting manufacturing artifacts."
                )
        else:
            parent = self.guard.resolve_target(target.parent)
            parent.mkdir(parents=True, exist_ok=True)
            self.guard.resolve_existing(parent)
            target.mkdir()
            target = self.guard.resolve_existing(target)

        return self._run_kicad(
            ["pcb", "export", "gerbers", "--output", str(target), str(pcb)],
            cwd=pcb.parent,
            operation="eda_kicad_export_gerbers",
            artifact_directory=target,
        )

    def _resolve_file(self, raw_path: str, extensions: Iterable[str], label: str) -> Path:
        resolved = self.guard.resolve_existing(raw_path)
        if not resolved.is_file():
            raise IsADirectoryError(str(resolved))
        allowed = {value.casefold() for value in extensions}
        if resolved.suffix.casefold() not in allowed:
            raise ValueError(f"{label} must use one of: {', '.join(sorted(allowed))}")
        return resolved

    def _resolve_report_target(self, raw_path: str | None, default: Path) -> Path:
        target = self.guard.resolve_target(raw_path or str(default))
        parent = self.guard.resolve_target(target.parent)
        parent.mkdir(parents=True, exist_ok=True)
        self.guard.resolve_existing(parent)
        if target.exists():
            existing = self.guard.resolve_existing(target)
            if not existing.is_file():
                raise IsADirectoryError(str(existing))
            return existing
        return target

    def _run_kicad(
        self,
        args: list[str],
        *,
        cwd: Path,
        operation: str,
        artifacts: list[Path] | None = None,
        artifact_directory: Path | None = None,
    ) -> dict:
        executable = self._discover_kicad_cli()
        if executable is None:
            raise FileNotFoundError(
                "kicad-cli was not found. Install KiCad or set eda.kicad_cli to the executable path."
            )

        timeout = min(
            max(1.0, float(self.config.default_timeout_seconds)),
            float(self.config.max_timeout_seconds),
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [str(executable), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            creationflags=creationflags,
        )
        stdout = self._limit_text(completed.stdout)
        stderr = self._limit_text(completed.stderr)

        artifact_items = []
        for artifact in artifacts or []:
            if artifact.exists():
                safe = self.guard.resolve_existing(artifact)
                artifact_items.append(
                    {"path": str(safe), "size_bytes": safe.stat().st_size}
                )
        if artifact_directory is not None and artifact_directory.exists():
            safe_dir = self.guard.resolve_existing(artifact_directory)
            for item in sorted(safe_dir.iterdir(), key=lambda p: p.name.casefold()):
                if item.is_file():
                    safe = self.guard.resolve_existing(item)
                    artifact_items.append(
                        {"path": str(safe), "size_bytes": safe.stat().st_size}
                    )

        result = {
            "backend": "kicad",
            "operation": operation,
            "executable": str(executable),
            "exit_code": completed.returncode,
            "ok": completed.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "artifacts": artifact_items,
        }
        self.audit.record(
            operation,
            exit_code=completed.returncode,
            ok=completed.returncode == 0,
            artifact_count=len(artifact_items),
        )
        return result

    def _limit_text(self, value: str) -> str:
        data = (value or "").encode("utf-8", errors="replace")
        limit = max(1, int(self.config.max_output_bytes))
        if len(data) <= limit:
            return value or ""
        clipped = data[:limit].decode("utf-8", errors="replace")
        return clipped + "\n...[truncated]"
