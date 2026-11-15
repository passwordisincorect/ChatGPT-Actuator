from __future__ import annotations

import os
import importlib.util
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from ..audit import AuditLogger
from ..config import EDAConfig
from ..security import PathGuard
from .eda_schematic import SchematicEngine
from .eda_sch_worker import SchematicWorkerManager
from .eda_altium import AltiumAdapter


_KICAD_EXTENSIONS = {".kicad_pro", ".kicad_sch", ".kicad_pcb", ".kicad_prl", ".kicad_dru"}
_ALTIUM_EXTENSIONS = {".prjpcb", ".schdoc", ".pcbdoc"}
_EDA_EXTENSIONS = _KICAD_EXTENSIONS | _ALTIUM_EXTENSIONS


class EDAService:
    """Permission-gated EDA gateway.

    Phase 1 intentionally keeps the core independent from KiCad/Altium Python APIs.
    Read/verification/export operations use vendor CLI entry points when available.
    Native editing is reserved behind allow_edit for a future IPC/script adapter.
    """

    def __init__(self, config: EDAConfig, audit: AuditLogger, *, state_dir=None, excluded_roots=()) -> None:
        self.config = config
        self.audit = audit
        self.guard = PathGuard(config.allowed_project_roots)
        self.schematic = SchematicEngine(config, audit, self.guard)
        self.sch_worker = SchematicWorkerManager(config, audit, self.guard)
        self.altium = AltiumAdapter(config, audit, self.guard, self._discover_altium,
                                    state_dir=state_dir, excluded_roots=excluded_roots)
        from .eda_altium_edit import ExistingSheetEditor
        self.altium_existing = ExistingSheetEditor(self.altium)

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("EDA tools are disabled.")

    def altium_library_inspect(self, library_path):
        return self.altium.library_inspect(library_path)

    def altium_plan_create(self, project_dir, name, components, nets, title="", expected_revision=None):
        return self.altium.plan_create(project_dir, name, components, nets, title, expected_revision)

    def altium_validate(self, ir_path):
        return self.altium.validate(ir_path)

    def altium_materialize(self, ir_path, request_key, output_dir=None):
        return self.altium.materialize(ir_path, request_key, output_dir)

    def altium_verify(self, ir_path, project_path):
        return self.altium.verify(ir_path, project_path)

    def altium_operation_status(self, operation_id):
        return self.altium.operation_status(operation_id)

    def altium_sch_inspect(self, project_path):
        return self.altium_existing.inspect(project_path)

    def altium_sch_edit_preview(self, snapshot_id, changes, output_dir, expected_connectivity=None, intent=None):
        return self.altium_existing.preview(snapshot_id, changes, output_dir,
                                            expected_connectivity=expected_connectivity, intent=intent)

    def altium_sch_edit_apply(self, snapshot_id, changes, output_dir, preview_digest, request_key,
                              expected_connectivity=None, intent=None):
        return self.altium_existing.apply(snapshot_id, changes, output_dir, preview_digest, request_key,
                                          expected_connectivity=expected_connectivity, intent=intent)

    def altium_sch_check_pin_contracts(self, snapshot_id, contracts, require_all_pins=True):
        return self.altium_existing.check_pin_contracts(snapshot_id, contracts, require_all_pins)

    def altium_sch_diff_connectivity(self, before_snapshot_id, after_snapshot_id):
        return self.altium_existing.diff_connectivity(before_snapshot_id, after_snapshot_id)

    def altium_sch_lint(self, snapshot_id, intent=None):
        return self.altium_existing.lint(snapshot_id, intent)

    def altium_sch_erc(self, project_path):
        return self.altium_existing.erc(project_path)

    def altium_sch_edit_commit(self, output_dir, request_key):
        return self.altium_existing.commit(output_dir, request_key)

    def altium_sch_edit_restore(self, commit_id, request_key):
        return self.altium_existing.restore(commit_id, request_key)

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
        altium_adapter = self.altium.capabilities()
        altium_ready = bool(altium and self.config.allow_edit and altium_adapter
                            and altium_adapter.get("connectivity_verified"))
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
                        "KiCad 10 schematic IPC is unavailable; Actuator uses a verified "
                        "file-backed SCH worker instead."
                        if self.config.allow_edit
                        else "eda.allow_edit is disabled."
                    ),
                    "sch_worker_mode": "kicad10-file-backed",
                },
                "altium": {
                    "detected": altium is not None,
                    "executable": str(altium) if altium else None,
                    "analysis": bool(altium and self.config.allow_read),
                    "native_edit": altium_ready,
                    "adapter": altium_adapter,
                    "native_edit_reason": (
                        None if altium_ready else (
                            "eda.allow_edit is disabled."
                            if not self.config.allow_edit else (
                                "Altium Designer executable is unavailable."
                                if not altium else (
                                    altium_adapter.get("state_error")
                                    or "AD26 scripting adapter requires successful live connectivity verification."
                                )
                            )
                        )
                    ),
                },
            },
            "schematic": {
                "priority": True,
                "ir_edit": bool(self.config.allow_edit),
                "kicad_file_inspect": bool(self.config.allow_read),
                "erc_and_netlist": kicad is not None and bool(self.config.allow_read),
                "materialize_kicad_sch": bool(
                    kicad is not None
                    and self.config.allow_edit
                    and importlib.util.find_spec("kicad_sch_api") is not None
                ),
                "materialize_blocker": (
                    None
                    if (
                        kicad is not None
                        and self.config.allow_edit
                        and importlib.util.find_spec("kicad_sch_api") is not None
                    )
                    else "KiCad, EDA edit permission, and kicad-sch-api are all required."
                ),
                "power_symbols": True,
                "multi_unit_symbols": True,
                "auto_layout": "connectivity-v3.3",
                "background_worker": bool(kicad is not None and self.config.allow_edit),
                "worker_mode": "kicad10-file-backed",
                "worker_background_safe": True,
                "worker_input_emulation": False,
                "pcb_scope": "placement-only; routing is intentionally out of scope",
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

    # ----------------------------
    # SCH-first tools
    # ----------------------------

    def sch_plan_create(self, project_dir: str, name: str, title: str = "") -> dict:
        return self.schematic.plan_create(project_dir, name, title=title)

    def sch_plan_get(self, ir_path: str) -> dict:
        return self.schematic.plan_get(ir_path)

    def sch_component(
        self,
        ir_path: str,
        action: str,
        reference: str,
        *,
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
        return self.schematic.component(
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

    def sch_power(
        self,
        ir_path: str,
        action: str,
        power_name: str,
        *,
        reference: str | None = None,
        x: float | None = None,
        y: float | None = None,
    ) -> dict:
        return self.schematic.power(
            ir_path,
            action,
            power_name,
            reference=reference,
            x=x,
            y=y,
        )

    def sch_auto_layout(
        self,
        ir_path: str,
        *,
        start_x: float = 50.0,
        start_y: float = 50.0,
        column_spacing: float = 35.0,
        row_spacing: float = 25.0,
        power_offset: float = 15.0,
    ) -> dict:
        return self.schematic.auto_layout(
            ir_path,
            start_x=start_x,
            start_y=start_y,
            column_spacing=column_spacing,
            row_spacing=row_spacing,
            power_offset=power_offset,
        )

    def sch_connection(
        self,
        ir_path: str,
        action: str,
        *,
        connection_id: str | None = None,
        from_reference: str | None = None,
        from_pin: str | None = None,
        to_reference: str | None = None,
        to_pin: str | None = None,
        net_name: str = "",
    ) -> dict:
        return self.schematic.connection(
            ir_path,
            action,
            connection_id=connection_id,
            from_reference=from_reference,
            from_pin=from_pin,
            to_reference=to_reference,
            to_pin=to_pin,
            net_name=net_name,
        )

    def sch_marker(
        self,
        ir_path: str,
        action: str,
        kind: str,
        *,
        marker_id: str | None = None,
        name: str | None = None,
        label_kind: str = "local",
        x: float | None = None,
        y: float | None = None,
        reference: str | None = None,
        pin: str | None = None,
    ) -> dict:
        return self.schematic.marker(
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

    def sch_validate(self, ir_path: str) -> dict:
        return self.schematic.validate(ir_path)

    def sch_rollback(self, ir_path: str, revision: int) -> dict:
        return self.schematic.rollback(ir_path, revision)

    def sch_inspect(self, schematic_path: str) -> dict:
        return self.schematic.inspect_kicad(schematic_path)

    def sch_worker_start(self, schematic_path: str, minimize: bool = True) -> dict:
        self._require_enabled()
        if not self.config.allow_edit:
            raise PermissionError("EDA edit operations are disabled.")
        executable = self._discover_kicad_cli()
        if executable is None:
            raise FileNotFoundError("kicad-cli was not found; SCH worker cannot locate eeschema.exe.")
        return self.sch_worker.start(schematic_path, executable, minimize=minimize)

    def sch_worker_status(self) -> dict:
        self._require_enabled()
        return self.sch_worker.status()

    def sch_worker_window(self, action: str) -> dict:
        self._require_enabled()
        if not self.config.allow_edit:
            raise PermissionError("EDA edit operations are disabled.")
        return self.sch_worker.window(action)

    def sch_worker_stop(self) -> dict:
        self._require_enabled()
        if not self.config.allow_edit:
            raise PermissionError("EDA edit operations are disabled.")
        return self.sch_worker.stop()

    def sch_worker_apply(
        self,
        ir_path: str,
        schematic_path: str,
        *,
        overwrite: bool = False,
        force: bool = False,
    ) -> dict:
        """Apply one verified SCH batch while keeping the KiCad viewer background-safe."""
        self._require_enabled()
        if not self.config.allow_edit:
            raise PermissionError("EDA edit operations are disabled.")
        executable = self._discover_kicad_cli()
        if executable is None:
            raise FileNotFoundError("kicad-cli was not found; SCH worker cannot apply a batch.")

        token = self.sch_worker.begin_batch(schematic_path)
        try:
            materialized = self.sch_materialize(
                ir_path,
                schematic_path,
                overwrite=overwrite,
                force=force,
            )
        except Exception:
            if token.get("had_worker"):
                self.sch_worker.end_batch(token, schematic_path, executable, restart=True)
            raise

        worker = self.sch_worker.end_batch(
            token,
            schematic_path,
            executable,
            restart=True,
        )
        result = {
            "batch": materialized,
            "worker": worker,
            "background_safe": True,
            "input_emulation": False,
        }
        self.audit.record(
            "eda_sch_worker_apply",
            ir_path=str(ir_path),
            schematic_path=str(schematic_path),
            ready_to_freeze=bool(materialized.get("ready_to_freeze", False)),
            worker_running=bool(worker.get("running", False)),
        )
        return result

    def sch_materialize(
        self,
        ir_path: str,
        schematic_path: str,
        *,
        overwrite: bool = False,
        force: bool = False,
    ) -> dict:
        self._require_enabled()
        if not self.config.allow_edit:
            raise PermissionError("EDA edit operations are disabled.")
        executable = self._discover_kicad_cli()
        if executable is None:
            raise FileNotFoundError(
                "kicad-cli was not found. Configure eda.kicad_cli before materializing a schematic."
            )
        if importlib.util.find_spec("kicad_sch_api") is None:
            raise RuntimeError("kicad-sch-api is not installed in the Actuator environment.")

        materialized = self.schematic.materialize_kicad(
            ir_path,
            schematic_path,
            str(executable),
            overwrite=overwrite,
            force=force,
        )
        verification = self.sch_verify(materialized["path"])
        semantic = materialized["semantic_check"]

        if verification.get("netlist", {}).get("ok"):
            artifacts = verification.get("netlist", {}).get("artifacts", [])
            if artifacts:
                semantic = self.schematic.compare_ir_netlist(
                    ir_path,
                    artifacts[0]["path"],
                )

        result = {
            "materialized": materialized,
            "verification": verification,
            "semantic": semantic,
            "ready_to_freeze": bool(
                verification.get("ok", False) and semantic.get("ok", False)
            ),
        }
        self.audit.record(
            "eda_sch_materialize_pipeline",
            ir_path=str(ir_path),
            schematic_path=materialized["path"],
            ready_to_freeze=result["ready_to_freeze"],
        )
        return result

    def sch_compare_netlist(self, ir_path: str, netlist_path: str) -> dict:
        return self.schematic.compare_ir_netlist(ir_path, netlist_path)

    def sch_freeze(self, ir_path: str, schematic_path: str) -> dict:
        verification = self.sch_verify(schematic_path)
        if not verification.get("netlist", {}).get("ok"):
            raise ValueError("Schematic netlist export failed; cannot freeze.")

        artifacts = verification.get("netlist", {}).get("artifacts", [])
        if not artifacts:
            raise ValueError("Schematic netlist artifact is missing; cannot freeze.")
        semantic = self.schematic.compare_ir_netlist(
            ir_path,
            artifacts[0]["path"],
        )
        return self.schematic.freeze(
            ir_path,
            schematic_path,
            verification,
            semantic,
        )

    def sch_export_netlist(
        self,
        schematic_path: str,
        output_path: str | None = None,
        netlist_format: str = "kicadsexpr",
    ) -> dict:
        self._require_read()
        schematic = self._resolve_file(schematic_path, {".kicad_sch"}, "KiCad schematic")
        allowed_formats = {
            "kicadsexpr", "kicadxml", "cadstar", "orcadpcb2",
            "spice", "spicemodel", "pads", "allegro",
        }
        fmt = str(netlist_format).strip().casefold()
        if fmt not in allowed_formats:
            raise ValueError("Unsupported KiCad netlist format.")
        default_suffix = ".net" if fmt == "kicadsexpr" else f".{fmt}.net"
        target = self._resolve_report_target(
            output_path,
            schematic.with_suffix(default_suffix),
        )
        return self._run_kicad(
            ["sch", "export", "netlist", "--format", fmt, "--output", str(target), str(schematic)],
            cwd=schematic.parent,
            operation="eda_sch_export_netlist",
            artifacts=[target],
        )

    def sch_verify(self, schematic_path: str) -> dict:
        self._require_read()
        inspection = self.schematic.inspect_kicad(schematic_path)
        executable = self._discover_kicad_cli()
        if executable is None:
            result = {
                "status": "blocked",
                "ok": False,
                "inspection": inspection,
                "blockers": [
                    "kicad-cli was not found.",
                    "KiCad ERC and authoritative netlist verification cannot run until KiCad is installed/configured.",
                ],
            }
            self.audit.record("eda_sch_verify", path=inspection["path"], status="blocked")
            return result

        schematic = self._resolve_file(schematic_path, {".kicad_sch"}, "KiCad schematic")
        verify_dir = self.guard.resolve_target(schematic.parent / ".actuator" / "verification")
        verify_dir.mkdir(parents=True, exist_ok=True)
        verify_dir = self.guard.resolve_existing(verify_dir)
        report = self.guard.resolve_target(verify_dir / f"{schematic.stem}.erc.rpt")
        netlist = self.guard.resolve_target(verify_dir / f"{schematic.stem}.net")

        erc = self._run_kicad(
            ["sch", "erc", "--exit-code-violations", "--output", str(report), str(schematic)],
            cwd=schematic.parent,
            operation="eda_sch_verify_erc",
            artifacts=[report],
        )
        net = self._run_kicad(
            ["sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(netlist), str(schematic)],
            cwd=schematic.parent,
            operation="eda_sch_verify_netlist",
            artifacts=[netlist],
        )
        ok = bool(erc["ok"] and net["ok"])
        result = {
            "status": "pass" if ok else "fail",
            "ok": ok,
            "inspection": inspection,
            "erc": erc,
            "netlist": net,
            "blockers": [] if ok else ["KiCad ERC and/or netlist export failed."],
        }
        self.audit.record("eda_sch_verify", path=str(schematic), status=result["status"])
        return result

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
