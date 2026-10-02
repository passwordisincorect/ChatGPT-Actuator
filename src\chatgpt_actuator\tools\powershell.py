from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import time
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
        result = self._execute(script, cwd=str(self.default_cwd), timeout_seconds=10.0, audit_action="powershell_info")
        return {
            "executable": self.executable,
            "default_cwd": str(self.default_cwd),
            "allowed_working_roots": [str(p) for p in self.allowed_working_roots],
            "execution_mode": "current_user",
            "filesystem_sandbox_applies": False,
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        }

    def run(
        self,
        script: str,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
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
        if timeout <= 0:
            raise ValueError("timeout_seconds must be greater than 0.")
        timeout = min(timeout, self.config.max_timeout_seconds)

        return self._execute(
            script,
            cwd=str(resolved_cwd),
            timeout_seconds=timeout,
            audit_action="powershell_run",
        )

    def _execute(
        self,
        script: str,
        cwd: str,
        timeout_seconds: float,
        audit_action: str,
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

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        started_at = time.monotonic()
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            shell=False,
            creationflags=creationflags,
        )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_process_tree(proc.pid)
            stdout_bytes, stderr_bytes = proc.communicate()

        duration_seconds = round(time.monotonic() - started_at, 3)
        stdout, stdout_truncated = self._decode_and_limit(stdout_bytes or b"")
        stderr, stderr_truncated = self._decode_and_limit(stderr_bytes or b"")
        stderr = self._strip_progress_only_clixml(stderr)
        truncated = stdout_truncated or stderr_truncated

        exit_code = proc.returncode
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
