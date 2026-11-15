from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from threading import RLock
import subprocess
import time

import psutil
import win32con
import win32gui
import win32process

from ..audit import AuditLogger
from ..config import EDAConfig
from ..security import PathGuard


class SchematicWorkerManager:
    """Background KiCad Schematic Editor lifecycle for file-backed SCH editing.

    KiCad 10 does not expose the schematic editor through the IPC API, so this
    worker deliberately keeps the GUI as a viewer while Actuator edits the
    .kicad_sch file through its verified materialization pipeline.

    No mouse or keyboard input is used. Window lifecycle uses Win32 messages.
    """

    def __init__(self, config: EDAConfig, audit: AuditLogger, guard: PathGuard) -> None:
        self.config = config
        self.audit = audit
        self.guard = guard
        self._lock = RLock()
        self._state: dict | None = None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _require_edit(self) -> None:
        if not self.config.enabled:
            raise PermissionError("EDA tools are disabled.")
        if not self.config.allow_edit:
            raise PermissionError("EDA edit operations are disabled.")

    def _resolve_schematic(self, schematic_path: str) -> Path:
        path = self.guard.resolve_existing(schematic_path)
        if not path.is_file():
            raise IsADirectoryError(str(path))
        if path.suffix.casefold() != ".kicad_sch":
            raise ValueError("schematic_path must end with .kicad_sch")
        return path

    @staticmethod
    def _editor_from_cli(kicad_cli: str | Path) -> Path:
        cli = Path(kicad_cli).expanduser().resolve(strict=True)
        editor = cli.parent / "eeschema.exe"
        if not editor.is_file():
            raise FileNotFoundError(f"KiCad Schematic Editor not found next to kicad-cli: {editor}")
        return editor.resolve()

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            return psutil.Process(int(pid)).is_running()
        except (psutil.Error, OSError, ValueError):
            return False

    @staticmethod
    def _window_for_pid(pid: int) -> int | None:
        matches: list[tuple[int, str, str]] = []

        def callback(hwnd: int, _extra) -> bool:
            try:
                _thread_id, window_pid = win32process.GetWindowThreadProcessId(hwnd)
                if window_pid == pid and win32gui.IsWindow(hwnd):
                    title = str(win32gui.GetWindowText(hwnd) or "")
                    class_name = str(win32gui.GetClassName(hwnd) or "")
                    if title:
                        matches.append((hwnd, title, class_name))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(callback, None)
        if not matches:
            return None

        # Prefer the actual KiCad top-level editor window. Windows can expose
        # helper top-level windows such as "Default IME" for the same PID.
        editor = [
            item for item in matches
            if item[2] == "wxWindowNR" and "schematic editor" in item[1].casefold()
        ]
        if editor:
            visible_editor = [item for item in editor if win32gui.IsWindowVisible(item[0])]
            return int((visible_editor or editor)[0][0])

        visible = [item for item in matches if win32gui.IsWindowVisible(item[0])]
        return int((visible or matches)[0][0])

    @staticmethod
    def _window_title(hwnd: int | None) -> str:
        if not hwnd:
            return ""
        try:
            return str(win32gui.GetWindowText(int(hwnd)) or "")
        except Exception:
            return ""

    @staticmethod
    def _is_minimized(hwnd: int | None) -> bool:
        if not hwnd:
            return False
        try:
            return bool(win32gui.IsIconic(int(hwnd)))
        except Exception:
            return False

    @staticmethod
    def _dirty_from_title(title: str) -> bool:
        # KiCad marks unsaved documents with an asterisk in the top-level title.
        return "*" in str(title)

    def _status_locked(self) -> dict:
        if self._state is None:
            return {
                "status": "stopped",
                "running": False,
                "mode": "kicad10-file-backed",
                "background_safe": True,
                "input_emulation": False,
            }

        state = dict(self._state)
        pid = state.get("pid")
        alive = self._pid_alive(pid)
        hwnd = state.get("hwnd")
        if alive and (not hwnd or not win32gui.IsWindow(int(hwnd))):
            hwnd = self._window_for_pid(int(pid))
            self._state["hwnd"] = hwnd

        title = self._window_title(hwnd) if alive else ""
        dirty = self._dirty_from_title(title)
        foreground = False
        if alive and hwnd:
            try:
                foreground = int(win32gui.GetForegroundWindow()) == int(hwnd)
            except Exception:
                foreground = False

        schematic = Path(str(state["schematic_path"]))
        current_hash = self._hash(schematic) if schematic.is_file() else None
        baseline_hash = state.get("baseline_sha256")
        external_change = bool(
            current_hash
            and baseline_hash
            and str(current_hash).casefold() != str(baseline_hash).casefold()
        )

        if dirty:
            paused_reason = "unsaved_user_edits"
        elif foreground:
            paused_reason = "user_viewing"
        elif external_change:
            paused_reason = "schematic_changed_outside_worker"
        else:
            paused_reason = None

        result = {
            **state,
            "status": "paused" if paused_reason else ("running" if alive else "stopped"),
            "running": alive,
            "hwnd": int(hwnd) if hwnd else None,
            "title": title,
            "dirty": dirty,
            "foreground": foreground,
            "minimized": self._is_minimized(hwnd) if alive else False,
            "external_change": external_change,
            "paused_reason": paused_reason,
            "current_sha256": current_hash,
            "mode": "kicad10-file-backed",
            "background_safe": True,
            "input_emulation": False,
        }
        return result

    def status(self) -> dict:
        with self._lock:
            result = self._status_locked()
        self.audit.record(
            "eda_sch_worker_status",
            running=result.get("running", False),
            status=result.get("status"),
            paused_reason=result.get("paused_reason"),
        )
        return result

    def start(
        self,
        schematic_path: str,
        kicad_cli: str | Path,
        *,
        minimize: bool = True,
    ) -> dict:
        self._require_edit()
        schematic = self._resolve_schematic(schematic_path)
        editor = self._editor_from_cli(kicad_cli)

        with self._lock:
            current = self._status_locked()
            if current.get("running"):
                same = (
                    Path(str(current.get("schematic_path", ""))).resolve(strict=False)
                    == schematic.resolve(strict=False)
                )
                if same:
                    return current
                raise RuntimeError(
                    "A KiCad SCH worker is already running for another schematic. "
                    "Stop it before starting a different schematic."
                )

            foreground_before = None
            try:
                current_foreground = int(win32gui.GetForegroundWindow())
                if current_foreground and win32gui.IsWindow(current_foreground):
                    foreground_before = current_foreground
            except Exception:
                foreground_before = None

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            # Do not let KiCad paint its first normal frame. KiCad 10 may ignore
            # SW_SHOWMINNOACTIVE later during document hydration, so startup begins
            # fully hidden and a watchdog keeps the editor hidden until loaded.
            startupinfo.wShowWindow = win32con.SW_HIDE if minimize else win32con.SW_SHOWNORMAL
            proc = subprocess.Popen(
                [str(editor), str(schematic)],
                cwd=str(schematic.parent),
                startupinfo=startupinfo,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )

            hwnd = None
            deadline = time.monotonic() + 8.0
            loaded_stable = 0
            hidden_interventions = 0
            foreground_interventions = 0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break

                candidate = self._window_for_pid(proc.pid)
                if candidate:
                    hwnd = candidate
                    if minimize:
                        try:
                            # Hide without activation. This closes the small race where
                            # KiCad restores itself after startupinfo has been consumed.
                            win32gui.SetWindowPos(
                                hwnd,
                                win32con.HWND_BOTTOM,
                                0,
                                0,
                                0,
                                0,
                                win32con.SWP_NOMOVE
                                | win32con.SWP_NOSIZE
                                | win32con.SWP_NOACTIVATE
                                | win32con.SWP_HIDEWINDOW,
                            )
                            hidden_interventions += 1
                        except Exception:
                            try:
                                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                                hidden_interventions += 1
                            except Exception:
                                pass

                        try:
                            if int(win32gui.GetForegroundWindow()) == int(hwnd):
                                foreground_interventions += 1
                                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                                if (
                                    foreground_before
                                    and win32gui.IsWindow(int(foreground_before))
                                ):
                                    try:
                                        win32gui.SetForegroundWindow(
                                            int(foreground_before)
                                        )
                                    except Exception:
                                        pass
                        except Exception:
                            pass

                    title = self._window_title(hwnd)
                    loaded = schematic.stem.casefold() in title.casefold()
                    if loaded:
                        loaded_stable += 1
                        if loaded_stable >= 5:
                            break
                    else:
                        loaded_stable = 0
                time.sleep(0.002)

            if proc.poll() is not None:
                raise RuntimeError(
                    f"KiCad Schematic Editor exited during worker startup with code {proc.returncode}."
                )

            if hwnd and minimize:
                # Now expose only a minimized taskbar entry, never a normal window.
                # Continue guarding briefly because KiCad 10 can issue one final
                # restore while finishing document initialization.
                settle_deadline = time.monotonic() + 1.5
                stable_minimized = 0
                while time.monotonic() < settle_deadline and proc.poll() is None:
                    try:
                        if not win32gui.IsIconic(hwnd):
                            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                            win32gui.ShowWindow(hwnd, win32con.SW_SHOWMINNOACTIVE)
                            hidden_interventions += 1
                        title = self._window_title(hwnd)
                        loaded = schematic.stem.casefold() in title.casefold()
                        foreground = int(win32gui.GetForegroundWindow()) == int(hwnd)
                        if foreground:
                            foreground_interventions += 1
                            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                            win32gui.ShowWindow(hwnd, win32con.SW_SHOWMINNOACTIVE)
                            if (
                                foreground_before
                                and win32gui.IsWindow(int(foreground_before))
                            ):
                                try:
                                    win32gui.SetForegroundWindow(
                                        int(foreground_before)
                                    )
                                except Exception:
                                    pass
                            stable_minimized = 0
                        elif win32gui.IsIconic(hwnd) and loaded:
                            stable_minimized += 1
                            if stable_minimized >= 10:
                                break
                        else:
                            stable_minimized = 0
                    except Exception:
                        break
                    time.sleep(0.002)

            self._state = {
                "schematic_path": str(schematic),
                "pid": int(proc.pid),
                "hwnd": int(hwnd) if hwnd else None,
                "started_at": self._now(),
                "baseline_sha256": self._hash(schematic),
                "minimize_on_resume": bool(minimize),
                "managed_by_actuator": True,
                "startup_hidden": bool(minimize),
                "startup_hidden_interventions": int(hidden_interventions if minimize else 0),
                "startup_foreground_interventions": int(
                    foreground_interventions if minimize else 0
                ),
                "startup_foreground_before": int(foreground_before) if foreground_before else None,
            }
            result = self._status_locked()

        self.audit.record(
            "eda_sch_worker_start",
            schematic_path=str(schematic),
            pid=result.get("pid"),
            hwnd=result.get("hwnd"),
            minimize=minimize,
        )
        return result

    def window(self, action: str) -> dict:
        self._require_edit()
        normalized = str(action).strip().casefold()
        if normalized not in {"show", "restore", "minimize"}:
            raise ValueError("action must be show, restore, or minimize.")

        with self._lock:
            status = self._status_locked()
            if not status.get("running") or not status.get("hwnd"):
                raise RuntimeError("KiCad SCH worker window is not running.")
            hwnd = int(status["hwnd"])
            if normalized == "minimize":
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            result = self._status_locked()

        self.audit.record(
            "eda_sch_worker_window",
            action=normalized,
            schematic_path=result.get("schematic_path"),
            hwnd=result.get("hwnd"),
        )
        return result

    def _close_clean_locked(self, *, timeout_seconds: float = 4.0) -> dict:
        status = self._status_locked()
        if not status.get("running"):
            return status
        if status.get("dirty"):
            raise PermissionError(
                "KiCad Schematic Editor has unsaved user edits. "
                "Worker refuses to close or overwrite the document."
            )
        hwnd = status.get("hwnd")
        pid = status.get("pid")
        if hwnd:
            win32gui.PostMessage(int(hwnd), win32con.WM_CLOSE, 0, 0)
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        closed = False
        while time.monotonic() < deadline:
            if not self._pid_alive(pid):
                closed = True
                break
            remaining_hwnd = self._window_for_pid(int(pid)) if pid else None
            if not remaining_hwnd:
                # KiCad may keep the process alive briefly after its top-level
                # editor window is gone. Treat window closure as a clean stop.
                closed = True
                break
            time.sleep(0.05)
        if not closed:
            raise RuntimeError(
                "KiCad Schematic Editor window did not close cleanly; "
                "worker will not force-kill it."
            )

        # KiCad 10 can leave a headless eeschema process behind after the
        # top-level window closes. Reap only this managed worker process using
        # a normal terminate request; never escalate to force-kill here.
        if self._pid_alive(pid):
            try:
                process = psutil.Process(int(pid))
                process.terminate()
                process.wait(timeout=2.0)
            except psutil.NoSuchProcess:
                pass
            except psutil.TimeoutExpired:
                self.audit.record(
                    "eda_sch_worker_reap_timeout",
                    pid=int(pid),
                    schematic_path=self._state.get("schematic_path"),
                )

        self._state["pid"] = None
        self._state["hwnd"] = None
        return self._status_locked()

    def stop(self) -> dict:
        self._require_edit()
        with self._lock:
            if self._state is None:
                return self._status_locked()
            previous_path = self._state.get("schematic_path")
            self._close_clean_locked()
            self._state = None
            result = self._status_locked()
        self.audit.record("eda_sch_worker_stop", schematic_path=previous_path)
        return result

    def begin_batch(self, schematic_path: str) -> dict:
        """Pause a managed viewer before replacing the on-disk schematic."""
        self._require_edit()
        schematic = self._resolve_schematic(schematic_path)
        with self._lock:
            status = self._status_locked()
            if not status.get("running"):
                return {"had_worker": False, "status": status}

            worker_path = Path(str(status["schematic_path"])).resolve(strict=False)
            if worker_path != schematic.resolve(strict=False):
                raise RuntimeError("The running SCH worker is attached to a different schematic.")
            if status.get("dirty"):
                raise PermissionError(
                    "KiCad Schematic Editor has unsaved user edits. "
                    "Save or discard them before the worker applies a new batch."
                )
            if status.get("foreground"):
                raise PermissionError(
                    "KiCad SCH worker is currently in the foreground. "
                    "Minimize/switch away before applying a background batch."
                )
            if status.get("external_change"):
                raise PermissionError(
                    "The schematic changed outside the worker since it was opened. "
                    "Refusing to overwrite a manually saved change."
                )

            minimize_on_resume = bool(self._state.get("minimize_on_resume", True))
            self._close_clean_locked()
            token = {
                "had_worker": True,
                "schematic_path": str(schematic),
                "minimize_on_resume": minimize_on_resume,
                "paused_at": self._now(),
            }

        self.audit.record(
            "eda_sch_worker_begin_batch",
            schematic_path=str(schematic),
            had_worker=token["had_worker"],
        )
        return token

    def end_batch(
        self,
        token: dict,
        schematic_path: str,
        kicad_cli: str | Path,
        *,
        restart: bool = True,
    ) -> dict:
        """Refresh baseline and reopen the managed viewer after a verified batch."""
        self._require_edit()
        schematic = self._resolve_schematic(schematic_path)
        had_worker = bool((token or {}).get("had_worker"))

        with self._lock:
            if self._state is not None:
                self._state["baseline_sha256"] = self._hash(schematic)

        if had_worker and restart:
            with self._lock:
                self._state = None
            result = self.start(
                str(schematic),
                kicad_cli,
                minimize=bool((token or {}).get("minimize_on_resume", True)),
            )
        else:
            result = self.status()

        self.audit.record(
            "eda_sch_worker_end_batch",
            schematic_path=str(schematic),
            restarted=bool(had_worker and restart),
        )
        return result
