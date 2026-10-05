from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any
from uuid import uuid4

import psutil

from .chatgpt_identity import ChatGPTIdentityDetector


SCHEMA_VERSION = 2
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class BrowserAccountManager:
    """Manage isolated ChatGPT sessions for the dedicated Worker Edge.

    The configured Worker Edge user-data directory stays at a stable path so the
    running Actuator does not need a restart when accounts are switched. Inactive
    browser profiles are parked under Accounts/<id>/User Data and a switch is
    performed with same-volume directory renames after only the dedicated Worker
    Edge process tree has been stopped.

    Passwords are never collected or stored separately. ChatGPT/Edge session data
    remains inside each normal browser profile, exactly as it would in Edge.
    """

    def __init__(self, actuator_root: Path, worker_root: Path | None = None) -> None:
        self.actuator_root = actuator_root.resolve()
        configured = os.environ.get("CHATGPT_WORKER_EDGE_ROOT", "").strip()
        if worker_root is not None:
            self.worker_root = Path(worker_root).expanduser().resolve()
        elif configured:
            self.worker_root = Path(configured).expanduser().resolve()
        else:
            self.worker_root = (self.actuator_root.parent / "ChatGPT-Worker-Edge").resolve()

        self.active_dir = self.worker_root / "User Data"
        self.accounts_dir = self.worker_root / "Accounts"
        self.manifest_path = self.worker_root / "accounts.json"
        self.config_path = self.actuator_root / "config" / "config.json"
        self._lock = threading.RLock()
        self.identity_detector = ChatGPTIdentityDetector()

        self.worker_root.mkdir(parents=True, exist_ok=True)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_manifest()

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

    def _default_manifest(self) -> dict[str, Any]:
        now = self._now()
        return {
            "schema_version": SCHEMA_VERSION,
            "active_account_id": "default",
            "main_confirmed_account_id": None,
            "main_confirmed_at": None,
            "accounts": [
                {
                    "id": "default",
                    "label": "Worker Default",
                    "created_at": now,
                    "last_switched_at": now,
                }
            ],
        }

    def _ensure_manifest(self) -> None:
        with self._lock:
            manifest = self._read_json(self.manifest_path, None)
            if not isinstance(manifest, dict) or not isinstance(manifest.get("accounts"), list):
                manifest = self._default_manifest()
                self._write_json(self.manifest_path, manifest)

            accounts = [
                item for item in manifest.get("accounts", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            if not accounts:
                manifest = self._default_manifest()
                accounts = manifest["accounts"]
                self._write_json(self.manifest_path, manifest)

            ids = {str(item["id"]) for item in accounts}
            active_id = str(manifest.get("active_account_id") or "")
            if active_id not in ids:
                manifest["active_account_id"] = str(accounts[0]["id"])
                self._write_json(self.manifest_path, manifest)

            if not self.active_dir.exists():
                active_id = str(manifest["active_account_id"])
                parked = self._slot_dir(active_id)
                if parked.exists():
                    self.active_dir.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(parked, self.active_dir)
                else:
                    self.active_dir.mkdir(parents=True, exist_ok=True)

    def _load_manifest(self) -> dict[str, Any]:
        self._ensure_manifest()
        manifest = self._read_json(self.manifest_path, self._default_manifest())
        if not isinstance(manifest, dict):
            raise RuntimeError("Browser account manifest is invalid.")
        return manifest

    def _slot_dir(self, account_id: str) -> Path:
        if not _ACCOUNT_ID_RE.fullmatch(account_id):
            raise ValueError("Invalid browser account id.")
        return self.accounts_dir / account_id / "User Data"

    def _account(self, manifest: dict[str, Any], account_id: str) -> dict[str, Any]:
        for item in manifest.get("accounts", []):
            if isinstance(item, dict) and item.get("id") == account_id:
                return item
        raise KeyError(f"Unknown browser account: {account_id}")

    def _browser_config(self) -> dict[str, Any]:
        raw = self._read_json(self.config_path, {})
        cfg = raw.get("browser_dom", {}) if isinstance(raw, dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def _configured_user_data_dir(self) -> Path:
        cfg = self._browser_config()
        raw = str(cfg.get("user_data_dir", "")).strip()
        if not raw:
            raise RuntimeError("browser_dom.user_data_dir is not configured.")
        return Path(os.path.expandvars(raw)).expanduser().resolve()

    def _ensure_config_matches(self) -> None:
        configured = self._configured_user_data_dir()
        if configured != self.active_dir.resolve():
            raise RuntimeError(
                "Browser account switching is disabled because browser_dom.user_data_dir "
                f"does not point to the managed Worker Edge path: {self.active_dir}"
            )

    @staticmethod
    def _slug(label: str) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
        value = value[:40].strip("-")
        return value or f"account-{uuid4().hex[:8]}"

    def _matching_worker_edge_roots(self) -> list[psutil.Process]:
        target = str(self.active_dir.resolve()).casefold()
        roots: list[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if str(proc.info.get("name") or "").casefold() != "msedge.exe":
                    continue
                cmdline = proc.info.get("cmdline") or []
                for arg in cmdline:
                    text = str(arg)
                    if not text.casefold().startswith("--user-data-dir="):
                        continue
                    value = text.split("=", 1)[1].strip().strip('"')
                    try:
                        resolved = str(Path(os.path.expandvars(value)).resolve()).casefold()
                    except OSError:
                        resolved = value.casefold()
                    if resolved == target:
                        roots.append(proc)
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return roots

    def worker_pids(self) -> list[int]:
        return sorted({int(proc.pid) for proc in self._matching_worker_edge_roots()})

    def stop_worker_edge(self, timeout: float = 6.0) -> list[int]:
        roots = self._matching_worker_edge_roots()
        if not roots:
            return []

        targets: dict[int, psutil.Process] = {}
        for root in roots:
            try:
                for child in root.children(recursive=True):
                    targets[int(child.pid)] = child
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            targets[int(root.pid)] = root

        ordered = list(targets.values())
        for proc in reversed(ordered):
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        _gone, alive = psutil.wait_procs(ordered, timeout=max(0.5, timeout))
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=2.0)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not self._matching_worker_edge_roots():
                break
            time.sleep(0.1)

        return sorted(targets)

    def _edge_executable(self) -> Path:
        cfg = self._browser_config()
        raw = str(
            cfg.get(
                "edge_executable",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            )
        ).strip()
        path = Path(os.path.expandvars(raw)).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Edge executable not found: {path}")
        return path

    def _launch_edge(self, *, visible: bool, url: str) -> dict[str, Any]:
        self._ensure_config_matches()
        edge = self._edge_executable()
        self.active_dir.mkdir(parents=True, exist_ok=True)

        active_port = self.active_dir / "DevToolsActivePort"
        try:
            active_port.unlink(missing_ok=True)
        except OSError:
            pass

        cfg = self._browser_config()
        mode = str(cfg.get("background_mode", "offscreen")).strip().casefold()
        if mode not in {"offscreen", "minimized"}:
            mode = "offscreen"

        args = [
            str(edge),
            f"--user-data-dir={self.active_dir}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--new-window",
        ]
        if not visible:
            if mode == "minimized":
                args.append("--start-minimized")
            else:
                args.extend(["--window-position=-32000,-32000", "--window-size=1280,900"])
        args.append(url)

        creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        startupinfo = None
        if not visible:
            creationflags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
            startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
            if startupinfo_cls is not None:
                startupinfo = startupinfo_cls()
                startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
                startupinfo.wShowWindow = 7 if mode == "minimized" else 4

        process = subprocess.Popen(
            args,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )

        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Worker Edge exited early with code {process.returncode}."
                )
            if active_port.is_file():
                try:
                    if active_port.read_text(encoding="utf-8").strip():
                        break
                except OSError:
                    pass
            time.sleep(0.1)

        return {
            "pid": int(process.pid),
            "visible": bool(visible),
            "url": url,
            "background_mode": mode,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            manifest = self._load_manifest()
            active_id = str(manifest["active_account_id"])
            accounts: list[dict[str, Any]] = []
            for item in manifest.get("accounts", []):
                if not isinstance(item, dict):
                    continue
                account_id = str(item.get("id", ""))
                if not _ACCOUNT_ID_RE.fullmatch(account_id):
                    continue
                is_active = account_id == active_id
                profile_dir = self.active_dir if is_active else self._slot_dir(account_id)
                accounts.append(
                    {
                        "id": account_id,
                        "label": str(item.get("label") or account_id),
                        "active": is_active,
                        "profile_exists": profile_dir.is_dir(),
                        "created_at": item.get("created_at"),
                        "last_switched_at": item.get("last_switched_at"),
                        "last_detected_identity": item.get("last_detected_identity"),
                    }
                )

            try:
                configured_path = self._configured_user_data_dir()
                config_matches = configured_path == self.active_dir.resolve()
                configured = str(configured_path)
            except Exception:
                config_matches = False
                configured = ""

            pids = self.worker_pids()
            main_confirmed_id = manifest.get("main_confirmed_account_id")
            if not isinstance(main_confirmed_id, str) or not any(
                item.get("id") == main_confirmed_id for item in accounts
            ):
                main_confirmed_id = None
            same_account_confirmed = main_confirmed_id == active_id
            for item in accounts:
                item["main_confirmed"] = bool(
                    main_confirmed_id and item.get("id") == main_confirmed_id
                )

            identity_detection = self.identity_detector.snapshot()

            return {
                "active_account_id": active_id,
                "system_account_id": active_id,
                "accounts": accounts,
                "main_confirmed_account_id": main_confirmed_id,
                "main_confirmed_at": manifest.get("main_confirmed_at"),
                "same_account_confirmed": same_account_confirmed,
                "identity_detection": identity_detection,
                "worker_edge_running": bool(pids),
                "worker_edge_pids": pids,
                "worker_root": str(self.worker_root),
                "active_user_data_dir": str(self.active_dir),
                "configured_user_data_dir": configured,
                "config_matches": config_matches,
                "main_edge_touched": False,
                "scope": "main_manual_confirmation_plus_worker_edge",
                "password_storage": "none",
            }

    def create_account(self, label: str) -> dict[str, Any]:
        clean = " ".join(str(label or "").split()).strip()
        if not clean:
            raise ValueError("Account label must not be empty.")
        if len(clean) > 60:
            raise ValueError("Account label must be 60 characters or fewer.")

        with self._lock:
            manifest = self._load_manifest()
            existing_ids = {
                str(item.get("id"))
                for item in manifest.get("accounts", [])
                if isinstance(item, dict)
            }
            base = self._slug(clean)
            account_id = base
            counter = 2
            while account_id in existing_ids:
                suffix = f"-{counter}"
                account_id = (base[: 48 - len(suffix)] + suffix).rstrip("-")
                counter += 1

            now = self._now()
            manifest.setdefault("accounts", []).append(
                {
                    "id": account_id,
                    "label": clean,
                    "created_at": now,
                    "last_switched_at": None,
                }
            )
            self._slot_dir(account_id).mkdir(parents=True, exist_ok=False)
            self._write_json(self.manifest_path, manifest)
            result = self.status()
            result["created_account_id"] = account_id
            return result

    def _persist_active_worker_identity(self, identity: dict[str, Any]) -> None:
        if not identity.get("authenticated"):
            return
        email = str(identity.get("email") or "").strip()
        name = str(identity.get("name") or "").strip()
        if not email and not name:
            return
        manifest = self._load_manifest()
        active_id = str(manifest["active_account_id"])
        account = self._account(manifest, active_id)
        account["last_detected_identity"] = {
            "name": name or None,
            "email": email or None,
            "detected_at": identity.get("detected_at") or self._now(),
        }
        self._write_json(self.manifest_path, manifest)

    def detect_identities(self, scope: str = "both") -> dict[str, Any]:
        normalized = str(scope or "both").strip().casefold()
        if normalized not in {"main", "worker", "both"}:
            raise ValueError("scope must be main, worker, or both.")

        with self._lock:
            if normalized in {"worker", "both"}:
                worker = self.identity_detector.detect_worker(self.active_dir)
                self._persist_active_worker_identity(worker)
            if normalized in {"main", "both"}:
                self.identity_detector.detect_main()

            result = self.status()
            result["detection_scope"] = normalized
            return result

    def confirm_main_account(self, account_id: str) -> dict[str, Any]:
        """Record the user's confirmation of which ChatGPT account Main Edge uses.

        This deliberately does not inspect or copy Main Edge cookies/session data.
        It is a user-confirmed association used only to keep Main and Worker on the
        same logical ChatGPT account.
        """
        with self._lock:
            manifest = self._load_manifest()
            self._account(manifest, account_id)
            manifest["main_confirmed_account_id"] = account_id
            manifest["main_confirmed_at"] = self._now()
            self._write_json(self.manifest_path, manifest)
            result = self.status()
            result["main_confirmation_changed"] = True
            return result

    def sync_worker_to_main(self) -> dict[str, Any]:
        """Switch Worker Edge to the detected Main identity or manual fallback."""
        with self._lock:
            manifest = self._load_manifest()
            snapshot = self.identity_detector.snapshot()
            main_identity = snapshot.get("main") or {}
            main_email = str(main_identity.get("email") or "").strip().casefold()
            account_id: str | None = None
            sync_source = "manual_confirmation"

            if main_identity.get("authenticated") and main_email:
                matches: list[str] = []
                for item in manifest.get("accounts", []):
                    if not isinstance(item, dict):
                        continue
                    known = item.get("last_detected_identity")
                    if not isinstance(known, dict):
                        continue
                    email = str(known.get("email") or "").strip().casefold()
                    if email and email == main_email:
                        matches.append(str(item.get("id") or ""))
                matches = [value for value in matches if value]
                if len(matches) == 1:
                    account_id = matches[0]
                    sync_source = "detected_email"
                elif len(matches) > 1:
                    confirmed = manifest.get("main_confirmed_account_id")
                    if isinstance(confirmed, str) and confirmed in matches:
                        account_id = confirmed
                        sync_source = "detected_email_plus_confirmation"
                    else:
                        raise RuntimeError(
                            "More than one Worker profile is associated with the detected Main email. "
                            "Confirm the intended system account manually first."
                        )

            if account_id is None:
                confirmed = manifest.get("main_confirmed_account_id")
                if isinstance(confirmed, str) and confirmed:
                    account_id = confirmed
                    sync_source = "manual_confirmation"

            if not account_id:
                raise RuntimeError(
                    "Main Edge account could not be mapped to a Worker profile. "
                    "Detect the accounts first, or select the intended account and confirm Main manually."
                )
            self._account(manifest, account_id)

        result = self.switch_account(account_id, launch_background=True)
        result["synced_to_main"] = True
        result["sync_source"] = sync_source
        return result

    def switch_account(self, account_id: str, *, launch_background: bool = True) -> dict[str, Any]:
        with self._lock:
            self._ensure_config_matches()
            manifest = self._load_manifest()
            target = self._account(manifest, account_id)
            active_id = str(manifest["active_account_id"])

            if account_id == active_id:
                launch_result = None
                if launch_background and not self.worker_pids():
                    launch_result = self._launch_edge(visible=False, url="about:blank")
                result = self.status()
                result["changed"] = False
                result["launch"] = launch_result
                return result

            self.stop_worker_edge()
            current_slot = self._slot_dir(active_id)
            target_slot = self._slot_dir(account_id)
            current_slot.parent.mkdir(parents=True, exist_ok=True)
            target_slot.parent.mkdir(parents=True, exist_ok=True)

            if current_slot.exists():
                try:
                    current_slot.rmdir()
                except OSError as exc:
                    raise RuntimeError(
                        f"Cannot park active account {active_id}; its inactive slot is not empty."
                    ) from exc

            if not self.active_dir.exists():
                self.active_dir.mkdir(parents=True, exist_ok=True)

            target_existed = target_slot.exists()
            moved_current = False
            try:
                os.replace(self.active_dir, current_slot)
                moved_current = True

                if target_existed:
                    os.replace(target_slot, self.active_dir)
                else:
                    self.active_dir.mkdir(parents=True, exist_ok=False)

                now = self._now()
                manifest["active_account_id"] = account_id
                target["last_switched_at"] = now
                self._write_json(self.manifest_path, manifest)
                self.identity_detector.clear_worker()
            except Exception:
                try:
                    if self.active_dir.exists():
                        if target_existed:
                            target_slot.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(self.active_dir, target_slot)
                        else:
                            self.active_dir.rmdir()
                    if moved_current and current_slot.exists() and not self.active_dir.exists():
                        os.replace(current_slot, self.active_dir)
                except Exception:
                    pass
                raise

            launch_result = None
            launch_error = None
            if launch_background:
                try:
                    launch_result = self._launch_edge(visible=False, url="about:blank")
                except Exception as exc:
                    launch_error = str(exc)

            result = self.status()
            result["changed"] = True
            result["launch"] = launch_result
            result["launch_error"] = launch_error
            return result

    def open_login(self, account_id: str) -> dict[str, Any]:
        with self._lock:
            manifest = self._load_manifest()
            self._account(manifest, account_id)
            if str(manifest["active_account_id"]) != account_id:
                self.switch_account(account_id, launch_background=False)
            else:
                self.stop_worker_edge()

            self.identity_detector.clear_worker()
            launch = self._launch_edge(visible=True, url="https://chatgpt.com/")
            result = self.status()
            result["launch"] = launch
            result["message"] = (
                "A visible dedicated Worker Edge was opened for this account. "
                "Sign in once, then close the window when finished; no password is stored by Web Control."
            )
            return result
