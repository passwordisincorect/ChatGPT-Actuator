from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import threading
from pathlib import Path

import psutil

from ..audit import AuditLogger
from ..config import ProcessConfig


_CRITICAL_WINDOWS_PROCESS_NAMES = {
    "system",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "fontdrvhost.exe",
    "dwm.exe",
}


class ProcessSystemService:
    def __init__(self, process_config: ProcessConfig, audit: AuditLogger) -> None:
        self.process_config = process_config
        self.audit = audit
        self._started_lock = threading.RLock()
        self._started_processes: dict[int, subprocess.Popen] = {}

    def system_info(self) -> dict:
        vm = psutil.virtual_memory()
        result = {
            "hostname": socket.gethostname(),
            "platform": platform.system(),
            "platform_release": platform.release(),
            "platform_version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
            "boot_time_unix": psutil.boot_time(),
            "cpu_physical_cores": psutil.cpu_count(logical=False),
            "cpu_logical_cores": psutil.cpu_count(logical=True),
            "memory_total_bytes": vm.total,
        }
        self.audit.record("system_info")
        return result

    def resource_snapshot(self) -> dict:
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        net = psutil.net_io_counters()
        disks = []

        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent": usage.percent,
            })

        result = {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory": {
                "total_bytes": vm.total,
                "available_bytes": vm.available,
                "used_bytes": vm.used,
                "percent": vm.percent,
            },
            "swap": {
                "total_bytes": swap.total,
                "used_bytes": swap.used,
                "free_bytes": swap.free,
                "percent": swap.percent,
            },
            "network": {
                "bytes_sent": net.bytes_sent,
                "bytes_recv": net.bytes_recv,
                "packets_sent": net.packets_sent,
                "packets_recv": net.packets_recv,
            },
            "disks": disks,
        }
        self.audit.record("resource_snapshot")
        return result

    def list_processes(self, name_filter: str | None = None, max_items: int | None = None) -> dict:
        if not self.process_config.enabled:
            raise PermissionError("Process tools are disabled.")

        limit = self.process_config.max_list_items if max_items is None else int(max_items)
        if limit < 1:
            raise ValueError("max_items must be at least 1.")
        limit = min(limit, self.process_config.max_list_items)

        needle = name_filter.casefold().strip() if name_filter else None
        items = []
        truncated = False

        for proc in psutil.process_iter(["pid", "ppid", "name", "username", "status", "exe", "create_time"]):
            try:
                info = proc.info
                name = info.get("name") or ""
                if needle and needle not in name.casefold():
                    continue
                items.append({
                    "pid": info.get("pid"),
                    "ppid": info.get("ppid"),
                    "name": name,
                    "username": info.get("username"),
                    "status": info.get("status"),
                    "exe": info.get("exe"),
                    "create_time": info.get("create_time"),
                })
                if len(items) >= limit:
                    truncated = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        items.sort(key=lambda item: (str(item.get("name") or "").casefold(), int(item.get("pid") or 0)))
        self.audit.record("process_list", name_filter=name_filter, returned=len(items), truncated=truncated)
        return {"processes": items, "truncated": truncated}

    def process_details(self, pid: int) -> dict:
        if not self.process_config.enabled:
            raise PermissionError("Process tools are disabled.")

        proc = psutil.Process(int(pid))
        try:
            with proc.oneshot():
                result = {
                    "pid": proc.pid,
                    "ppid": proc.ppid(),
                    "name": proc.name(),
                    "exe": self._safe_call(proc.exe),
                    "cwd": self._safe_call(proc.cwd),
                    "username": self._safe_call(proc.username),
                    "status": proc.status(),
                    "create_time": proc.create_time(),
                    "cmdline": self._safe_call(proc.cmdline, default=[]),
                    "cpu_percent": proc.cpu_percent(interval=0.05),
                    "memory_info": self._namedtuple_to_dict(self._safe_call(proc.memory_info)),
                    "num_threads": self._safe_call(proc.num_threads),
                }
        except psutil.NoSuchProcess as exc:
            raise ProcessLookupError(f"Process {pid} no longer exists.") from exc

        self.audit.record("process_details", pid=int(pid))
        return result

    def start_process(
        self,
        executable: str,
        arguments: list[str] | None = None,
        cwd: str | None = None,
    ) -> dict:
        if not self.process_config.enabled or not self.process_config.allow_start:
            raise PermissionError("Starting processes is disabled.")

        exe = Path(executable).expanduser().resolve(strict=True)
        if not exe.is_file():
            raise FileNotFoundError(str(exe))

        workdir = None
        if cwd:
            workdir_path = Path(cwd).expanduser().resolve(strict=True)
            if not workdir_path.is_dir():
                raise NotADirectoryError(str(workdir_path))
            workdir = str(workdir_path)

        argv = [str(exe)] + [str(arg) for arg in (arguments or [])]
        proc = subprocess.Popen(
            argv,
            cwd=workdir,
            shell=False,
            close_fds=False,
        )
        with self._started_lock:
            self._started_processes[proc.pid] = proc

        self.audit.record(
            "process_start",
            pid=proc.pid,
            executable=str(exe),
            arguments=[str(arg) for arg in (arguments or [])],
            cwd=workdir,
        )
        return {
            "pid": proc.pid,
            "executable": str(exe),
            "arguments": [str(arg) for arg in (arguments or [])],
            "cwd": workdir,
        }

    def stop_process(
        self,
        pid: int,
        force: bool = False,
        include_children: bool = False,
    ) -> dict:
        if not self.process_config.enabled or not self.process_config.allow_stop:
            raise PermissionError("Stopping processes is disabled.")
        if force and not self.process_config.allow_force_kill:
            raise PermissionError("Force-kill is disabled.")

        requested_pid = int(pid)
        try:
            target = psutil.Process(requested_pid)
        except psutil.NoSuchProcess:
            self._reap_started_process(requested_pid, timeout=0.0)
            self.audit.record(
                "process_stop",
                pid=requested_pid,
                force=bool(force),
                include_children=bool(include_children),
                stopped=[],
                timed_out=[],
                already_missing=[requested_pid],
            )
            return {
                "requested_pid": requested_pid,
                "force": bool(force),
                "include_children": bool(include_children),
                "stop_requested_for": [],
                "already_missing": [requested_pid],
                "timed_out": [],
            }

        self._assert_stoppable(target)

        targets = []
        if include_children:
            targets.extend(reversed(target.children(recursive=True)))
        targets.append(target)

        stopped = []
        missing = []
        timed_out = []

        for proc in targets:
            try:
                self._assert_stoppable(proc)
                if force:
                    proc.kill()
                else:
                    proc.terminate()
                stopped.append(proc.pid)
            except psutil.NoSuchProcess:
                missing.append(proc.pid)

        if stopped:
            alive = []
            proc_objects = []
            for stopped_pid in stopped:
                try:
                    proc_objects.append(psutil.Process(stopped_pid))
                except psutil.NoSuchProcess:
                    pass
            if proc_objects:
                _, alive = psutil.wait_procs(
                    proc_objects,
                    timeout=self.process_config.stop_timeout_seconds,
                )
            timed_out = [proc.pid for proc in alive]

        if requested_pid not in timed_out:
            self._reap_started_process(
                requested_pid,
                timeout=max(0.0, self.process_config.stop_timeout_seconds),
            )

        self.audit.record(
            "process_stop",
            pid=int(pid),
            force=bool(force),
            include_children=bool(include_children),
            stopped=stopped,
            timed_out=timed_out,
        )
        return {
            "requested_pid": int(pid),
            "force": bool(force),
            "include_children": bool(include_children),
            "stop_requested_for": stopped,
            "already_missing": missing,
            "timed_out": timed_out,
        }

    def _reap_started_process(self, pid: int, timeout: float = 0.0) -> None:
        with self._started_lock:
            proc = self._started_processes.get(int(pid))
        if proc is None:
            return

        try:
            if timeout > 0:
                proc.wait(timeout=timeout)
            else:
                proc.poll()
        except subprocess.TimeoutExpired:
            return
        finally:
            if proc.returncode is not None:
                with self._started_lock:
                    self._started_processes.pop(int(pid), None)

    def _assert_stoppable(self, proc: psutil.Process) -> None:
        pid = proc.pid
        protected = self._protected_pids()
        if pid in protected:
            raise PermissionError(f"Refusing to stop protected ChatGPT-Actuator/tunnel process PID {pid}.")

        if self.process_config.protect_system_processes:
            try:
                name = (proc.name() or "").casefold()
            except psutil.NoSuchProcess:
                raise
            if pid in {0, 4} or name in _CRITICAL_WINDOWS_PROCESS_NAMES:
                raise PermissionError(f"Refusing to stop protected Windows process: {name} (PID {pid}).")

    @staticmethod
    def _protected_pids() -> set[int]:
        protected = {os.getpid()}
        try:
            proc = psutil.Process(os.getpid())
            for parent in proc.parents():
                protected.add(parent.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return protected

    @staticmethod
    def _safe_call(func, default=None):
        try:
            return func()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            return default

    @staticmethod
    def _namedtuple_to_dict(value):
        if value is None:
            return None
        if hasattr(value, "_asdict"):
            return dict(value._asdict())
        return value
