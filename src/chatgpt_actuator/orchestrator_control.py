from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import time
import tomllib


class OrchestratorControl:
    def __init__(self, project_root: Path) -> None:
        self.actuator_root = Path(project_root)
        self.root = Path(
            __import__("os").environ.get(
                "CHATGPT_ORCHESTRATOR_ROOT",
                str(self.actuator_root.parent / "ChatGPT-Orchestrator"),
            )
        )
        self.manager = self.actuator_root / "scripts" / "orchestrator-manager.ps1"
        self.health_url = __import__("os").environ.get(
            "CHATGPT_ORCHESTRATOR_HEALTH",
            "http://127.0.0.1:8082/healthz",
        )

    @staticmethod
    def _uptime_human(seconds: int) -> str:
        seconds = max(0, int(seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}h {minutes}m {secs}s"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    def _command(self, action: str, *, json_output: bool = False) -> subprocess.CompletedProcess[str]:
        if action not in {"Status", "Start", "Stop", "Restart"}:
            raise ValueError("Unsupported Orchestrator action.")
        if not self.manager.is_file():
            raise FileNotFoundError(f"Orchestrator manager not found: {self.manager}")
        args = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.manager),
            "-Action",
            action,
        ]
        if json_output:
            args.append("-Json")
        return subprocess.run(
            args,
            cwd=str(self.actuator_root),
            text=True,
            capture_output=True,
            timeout=35,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )

    def process_status(self) -> dict:
        result = self._command("Status", json_output=True)
        if result.returncode != 0:
            return {
                "running": False,
                "pids": [],
                "health_ok": False,
                "health_url": self.health_url,
                "uptime_seconds": 0,
                "uptime_human": "0s",
                "error": (result.stderr or result.stdout).strip(),
            }
        try:
            data = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            data = {
                "running": False,
                "pids": [],
                "health_ok": False,
                "health_url": self.health_url,
                "uptime_seconds": 0,
                "error": "Unable to parse Orchestrator process status.",
            }
        data["uptime_human"] = self._uptime_human(int(data.get("uptime_seconds") or 0))
        return data

    def _static_info(self) -> dict:
        info = {
            "version": "unknown",
            "backend": "unknown",
            "hybrid_cdp_enabled": False,
            "max_active_workers": None,
        }
        pyproject = self.root / "pyproject.toml"
        try:
            raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            info["version"] = str(raw.get("project", {}).get("version", "unknown"))
        except Exception:
            pass
        config_path = self.root / "config" / "config.json"
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            info["backend"] = str(raw.get("backend", "unknown"))
            info["hybrid_cdp_enabled"] = bool(raw.get("hybrid_cdp_enabled", False))
            info["max_active_workers"] = raw.get("max_active_workers")
        except Exception:
            pass
        return info

    def _runtime_db(self) -> dict:
        db_path = self.root / "data" / "orchestrator.db"
        empty = {
            "active_worker_count": 0,
            "active_job_count": 0,
            "active_task_count": 0,
            "workers": [],
            "jobs": [],
        }
        if not db_path.is_file():
            return empty
        try:
            uri = f"{db_path.as_uri()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=0.5)
            con.row_factory = sqlite3.Row
            try:
                workers = con.execute(
                    """SELECT id, task_id, role, status, updated_at
                       FROM workers
                       WHERE status != 'CLOSED'
                       ORDER BY updated_at DESC, rowid DESC
                       LIMIT 8"""
                ).fetchall()
                jobs = con.execute(
                    """SELECT id, worker_id, task_id, kind, status, updated_at
                       FROM jobs
                       WHERE status IN ('QUEUED','RUNNING')
                       ORDER BY updated_at DESC, rowid DESC
                       LIMIT 8"""
                ).fetchall()
                task_row = con.execute(
                    """SELECT COUNT(*) AS n FROM tasks
                       WHERE status IN ('PENDING','RUNNING','REVIEWING')"""
                ).fetchone()
            finally:
                con.close()
            return {
                "active_worker_count": len(workers),
                "active_job_count": len(jobs),
                "active_task_count": int(task_row["n"]) if task_row else 0,
                "workers": [dict(x) for x in workers],
                "jobs": [dict(x) for x in jobs],
            }
        except Exception as exc:
            empty["db_error"] = f"{type(exc).__name__}: {exc}"
            return empty

    def _runtime_backend(self, static: dict, runtime: dict) -> dict:
        backend = static.get("backend")
        if backend == "edge_tabs":
            selected = "uia" if runtime["active_worker_count"] else "none"
            return {"selected_backend": selected, "source": "configured"}
        if backend == "edge_cdp":
            selected = "cdp" if runtime["active_worker_count"] else "none"
            return {"selected_backend": selected, "source": "configured"}
        if backend != "edge_hybrid":
            return {"selected_backend": backend or "unknown", "source": "configured"}

        status_path = self.root / "data" / "runtime-status.json"
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
            data["source"] = "runtime"
            return data
        except Exception:
            return {
                "selected_backend": "none" if not runtime["active_worker_count"] else "unknown",
                "active_cdp_sessions": 0,
                "active_uia_sessions": 0,
                "source": "unavailable",
            }

    def status(self) -> dict:
        static = self._static_info()
        runtime = self._runtime_db()
        process = self.process_status()
        backend_runtime = self._runtime_backend(static, runtime)
        return {
            **process,
            **static,
            **runtime,
            "backend_runtime": backend_runtime,
        }

    def action(self, action: str) -> dict:
        started = time.monotonic()
        result = self._command(action)
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "Orchestrator command failed.").strip()
            )
        return {
            "action": action,
            "duration_seconds": round(time.monotonic() - started, 3),
            "output": result.stdout.strip(),
            "status": self.status(),
        }

    def logs(self, lines: int = 80) -> dict:
        lines = max(1, min(int(lines), 500))

        def tail(path: Path) -> str:
            if not path.exists():
                return "(log file not found)"
            try:
                content = path.read_text(encoding="utf-8", errors="replace").splitlines()
                return "\n".join(content[-lines:])
            except Exception as exc:
                return f"(unable to read log: {exc})"

        logs = self.root / "logs"
        return {
            "stdout": tail(logs / "tunnel.stdout.log"),
            "stderr": tail(logs / "tunnel.stderr.log"),
        }
