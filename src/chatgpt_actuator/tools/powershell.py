from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import time
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import psutil

from ..audit import AuditLogger
from ..config import PowerShellConfig
from ..errors import ConfigError


class PowerShellService:
    def __init__(self, config: PowerShellConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit
        self.executable = self._resolve_executable(config.executable)
        self.allowed_working_roots = tuple(
            Path(root).expanduser().resolve(strict=True)
            for root in config.allowed_working_roots
        )
        self.default_cwd = self._resolve_cwd(config.default_cwd)

    def info(self) -> dict:
        script = (
            "$ErrorActionPreference='Stop'; "
            "[pscustomobject]@{"
            "PSVersion=$PSVersionTable.PSVersion.ToString();"
            "PSEdition=$PSVersionTable.PSEdition;"
            "OS=[System.Environment]::OSVersion.VersionString;"
            "Platform=[System.Environment]::OSVersion.Platform.ToString()"
            "} | ConvertTo-Json -Compress"
        )
        timeout = min(self.config.max_timeout_seconds, max(10.0, self.config.default_timeout_seconds))
        result = self._execute(script, cwd=str(self.default_cwd), timeout_seconds=timeout, audit_action="powershell_info")
        return {
            "executable": self.executable,
            "default_cwd": str(self.default_cwd),
            "allowed_working_roots": [str(p) for p in self.allowed_working_roots],
            "execution_mode": "background_private_desktop",
            "background_only":True,
            "child_tree_bounded":True,
            "filesystem_sandbox_applies": False,
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
            "cancelled": result["cancelled"],
            "duration_seconds": result["duration_seconds"],
            "output_truncated": result["output_truncated"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        }

    def run(
        self,
        script: str,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        *, resource_limits=None, cancel=lambda:False,
    ) -> dict:
        if not self.config.enabled:
            raise PermissionError("PowerShell tools are disabled.")
        if not self.config.allow_execute:
            raise PermissionError("PowerShell execution is disabled.")

        if not isinstance(script, str) or not script.strip():
            raise ValueError("script must not be empty.")
        if len(script) > self.config.max_script_chars:
            raise ValueError(
                f"script is too large: {len(script)} chars; limit is {self.config.max_script_chars}."
            )

        resolved_cwd = self._resolve_cwd(cwd or str(self.default_cwd))
        timeout = self.config.default_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be greater than 0.")
        timeout = min(timeout, self.config.max_timeout_seconds)

        return self._execute(
            script,
            cwd=str(resolved_cwd),
            timeout_seconds=timeout,
            audit_action="powershell_run",resource_limits=resource_limits,cancel=cancel,
        )

    def _execute(
        self,
        script: str,
        cwd: str,
        timeout_seconds: float,
        audit_action: str,
        *, resource_limits=None, cancel=lambda:False,
    ) -> dict:
        # Force UTF-8 for redirected stdout/stderr, then run the caller's script.
        wrapped = (
            "$utf8 = New-Object System.Text.UTF8Encoding($false); "
            "[Console]::OutputEncoding = $utf8; "
            "$OutputEncoding = $utf8; "
            "$ProgressPreference = 'SilentlyContinue'; "
            + script
        )
        encoded = base64.b64encode(wrapped.encode("utf-16le")).decode("ascii")
        argv = [
            self.executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]

        from ..background_process import run_bounded
        # Credentials and arbitrary inherited environment are not passed to caller scripts.
        env={k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA','ProgramFiles','ProgramFiles(x86)','ProgramW6432','ComSpec') if k in os.environ}
        native=Path(self.executable).parent
        env['PATH']=os.pathsep.join([str(native),str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32'),os.environ.get('PATH','')])
        env['PSModulePath']=str(native/'Modules')
        result=run_bounded(argv,cwd,env,timeout_seconds,self.config.max_output_bytes,cancel,limits=resource_limits)
        stdout=result['stdout'];stderr=self._strip_progress_only_clixml(result['stderr'])
        timed_out=result['timed_out'];exit_code=result['exit_code'];truncated=result['output_truncated'];duration_seconds=result['duration_seconds']
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()

        self.audit.record(
            audit_action,
            script_sha256=digest,
            script_chars=len(script),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            timed_out=timed_out,
            exit_code=exit_code,
            output_truncated=truncated,
        )

        return {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "cancelled":result["cancelled"],
            "background_only":True,
            "duration_seconds": duration_seconds,
            "cwd": cwd,
            "stdout": stdout,
            "stderr": stderr,
            "output_truncated": truncated,
        }

    def _resolve_executable(self, raw: str) -> str:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ConfigError(f"PowerShell executable not found: {candidate}") from exc
            if not resolved.is_file():
                raise ConfigError(f"PowerShell executable is not a file: {resolved}")
            return str(resolved)

        found = shutil.which(raw)
        if not found and os.name == 'nt' and raw.casefold() == 'powershell.exe':
            windows_root=os.environ.get('SystemRoot') or os.environ.get('WINDIR')
            if windows_root:
                native=Path(windows_root)/'System32/WindowsPowerShell/v1.0/powershell.exe'
                if native.is_file():found=str(native)
        if not found:
            raise ConfigError(f"PowerShell executable was not found on PATH: {raw}")
        return str(Path(found).resolve(strict=True))

    def _resolve_cwd(self, raw: str) -> Path:
        path = Path(raw).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise NotADirectoryError(str(path))

        for root in self.allowed_working_roots:
            try:
                path.relative_to(root)
                return path
            except ValueError:
                continue

        raise PermissionError(
            f"PowerShell working directory '{path}' is outside allowed_working_roots."
        )

    def _decode_and_limit(self, data: bytes) -> tuple[str, bool]:
        limit = self.config.max_output_bytes
        truncated = len(data) > limit
        selected = data[:limit]
        return selected.decode("utf-8-sig", errors="replace"), truncated

    @staticmethod
    def _strip_progress_only_clixml(text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("#< CLIXML"):
            return text

        parts = stripped.splitlines()
        if len(parts) < 2:
            return text

        xml_text = "\n".join(parts[1:])
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return text

        streams = [
            elem.attrib.get("S")
            for elem in root.iter()
            if elem.attrib.get("S") is not None
        ]
        if streams and all(stream == "progress" for stream in streams):
            return ""
        return text

    @staticmethod
    def _kill_process_tree(pid: int) -> None:
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return

        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        try:
            parent.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        psutil.wait_procs(children + [parent], timeout=2.0)
