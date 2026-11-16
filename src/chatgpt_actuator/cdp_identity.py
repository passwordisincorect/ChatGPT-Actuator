"""Read-only discovery of the owner's explicitly selected Microsoft Edge process."""
from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path
from typing import Any

import psutil

from .automation_errors import BrowserConnectionUnavailable, PolicyDenied

_PROFILE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_BROWSER_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9-]{1,128}\Z")


def _canonical(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=True)))


def _edge_executables() -> set[str]:
    roots = {os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")}
    paths = set()
    for root in roots - {""}:
        candidate = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        try:
            paths.add(_canonical(candidate))
        except OSError:
            pass
    return paths


def _local_policy_denied() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    key_path = r"SOFTWARE\Policies\Microsoft\Edge"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                for name, blocked in (("RemoteDebuggingAllowed", 0), ("DeveloperToolsAvailability", 2)):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        continue
                    if value == blocked:
                        return True
        except FileNotFoundError:
            continue
    return False


def _arguments(cmdline: list[str]) -> tuple[str | None, str]:
    root = None
    profile = "Default"
    for index, arg in enumerate(cmdline):
        if arg.startswith("--user-data-dir="):
            root = arg.split("=", 1)[1]
        elif arg == "--user-data-dir" and index + 1 < len(cmdline):
            root = cmdline[index + 1]
        elif arg.startswith("--profile-directory="):
            profile = arg.split("=", 1)[1]
        elif arg == "--profile-directory" and index + 1 < len(cmdline):
            profile = cmdline[index + 1]
    return root, profile


def _listeners(port: int) -> list[tuple[int | None, str]]:
    found = []
    for conn in psutil.net_connections(kind="tcp"):
        address = getattr(conn, "laddr", None)
        if getattr(conn, "status", None) != psutil.CONN_LISTEN or not address or address.port != port:
            continue
        found.append((getattr(conn, "pid", None), str(address.ip)))
    return found


def _endpoint(root: Path, pid: int, started: float) -> str | None:
    active = root / "DevToolsActivePort"
    if active.is_symlink():
        raise PolicyDenied("local_policy")
    try:
        stat = active.stat()
        # Compare at whole-second precision because some Windows filesystems round
        # file timestamps while process creation times retain fractions.
        if int(stat.st_mtime) < int(started):
            return None
        lines = active.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return None
    if not lines or not lines[0].strip().isdigit():
        return None
    port = int(lines[0].strip())
    if not 1 <= port <= 65535:
        return None
    if len(lines) > 1 and not _BROWSER_PATH.fullmatch(lines[1].strip()):
        return None
    listeners = _listeners(port)
    if not listeners:
        return None
    if any(owner != pid for owner, _ in listeners):
        raise PolicyDenied("local_policy")
    if any(not ipaddress.ip_address(ip).is_loopback for _, ip in listeners):
        raise PolicyDenied("local_policy")
    if len(lines) > 1:
        return f"ws://127.0.0.1:{port}{lines[1].strip()}"
    return f"http://127.0.0.1:{port}"


def _process_binding(policy: dict[str, Any], owner: str, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    if _local_policy_denied():
        raise PolicyDenied("local_policy")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise PolicyDenied("local_policy")
    standard_root = Path(local_app_data) / "Microsoft" / "Edge" / "User Data"
    raw_root = policy.get("user_data_dir")
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        raise PolicyDenied("local_policy")
    try:
        root = Path(_canonical(raw_root))
        if _canonical(root) != _canonical(standard_root):
            raise PolicyDenied("local_policy")
    except OSError as exc:
        raise PolicyDenied("local_policy") from exc
    profile = policy.get("profile_directory") or "Default"
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile) or not (root / profile).is_dir():
        raise PolicyDenied("local_policy")
    try:
        current_user = psutil.Process(os.getpid()).username()
    except (psutil.Error, OSError) as exc:
        raise PolicyDenied("local_policy") from exc
    trusted = _edge_executables()
    if not trusted:
        raise PolicyDenied("local_policy")
    matches = []
    for proc in psutil.process_iter(["name", "cmdline", "exe", "create_time", "username"]):
        try:
            info = proc.info
            if (str(info.get("name", "")).casefold() != "msedge.exe"
                    or any(arg.startswith("--type=") for arg in (info.get("cmdline") or [])[1:])):
                continue
            exe = _canonical(info["exe"])
            if exe not in trusted or info.get("username") != current_user:
                continue
            argroot, argprofile = _arguments(info.get("cmdline") or [])
            actual_root = Path(argroot) if argroot else standard_root
            if _canonical(actual_root) != str(root) or argprofile != profile:
                continue
            matches.append((proc, float(info["create_time"]), exe))
        except (psutil.Error, OSError, KeyError, TypeError, ValueError):
            continue
    if len(matches) != 1:
        if expected is not None:
            raise BrowserConnectionUnavailable("The selected Edge process identity changed.")
        if len(matches) > 1:
            raise PolicyDenied("ownership_lost")
        raise BrowserConnectionUnavailable("No matching Edge process is running.")
    proc, started, exe = matches[0]
    if expected is not None and (proc.pid != expected["pid"] or started != expected["started"] or exe != expected["exe"]):
        raise BrowserConnectionUnavailable("The selected Edge process identity changed.")
    # Recheck process creation time immediately before trusting the active port file.
    try:
        if float(proc.create_time()) != started:
            raise BrowserConnectionUnavailable("The selected Edge process identity changed.")
    except psutil.Error as exc:
        raise BrowserConnectionUnavailable("The selected Edge process exited.") from exc
    endpoint = _endpoint(root, int(proc.pid), started)
    try:
        if float(proc.create_time()) != started:
            raise BrowserConnectionUnavailable("The selected Edge process identity changed.")
    except psutil.Error as exc:
        raise BrowserConnectionUnavailable("The selected Edge process exited.") from exc
    return {"owner": owner, "pid": int(proc.pid), "started": started, "exe": exe,
            "user_data_dir": str(root), "profile_directory": profile,
            "endpoint": endpoint,
            "allow_temporary_focus": policy.get("allow_temporary_focus", False)}


def resolve(policy: dict[str, Any] | None, owner: str) -> dict[str, Any]:
    """Resolve the single owner-approved Edge identity without changing local state."""
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        raise PolicyDenied("wrong_owner")
    if set(policy) - {"enabled", "owner", "user_data_dir", "profile_directory", "allow_temporary_focus"}:
        raise PolicyDenied("wrong_owner")
    focus = policy.get("allow_temporary_focus", False)
    if not isinstance(focus, bool):
        raise PolicyDenied("wrong_owner")
    if not owner or policy.get("owner") != owner:
        raise PolicyDenied("wrong_owner")
    return _process_binding(policy, owner)


def refresh(binding: dict[str, Any]) -> dict[str, Any]:
    """Revalidate a prior binding and return its current endpoint, if available."""
    if not isinstance(binding, dict) or not isinstance(binding.get("owner"), str):
        raise PolicyDenied("wrong_owner")
    focus = binding.get("allow_temporary_focus", False)
    if not isinstance(focus, bool):
        raise PolicyDenied("wrong_owner")
    expected = {key: binding.get(key) for key in ("pid", "started", "exe")}
    policy = {"enabled": True, "owner": binding["owner"],
              "user_data_dir": binding.get("user_data_dir"),
              "profile_directory": binding.get("profile_directory"),
              "allow_temporary_focus": focus}
    result = _process_binding(policy, binding["owner"], expected)
    if result["user_data_dir"] != binding.get("user_data_dir") or result["profile_directory"] != binding.get("profile_directory"):
        raise BrowserConnectionUnavailable("The selected Edge profile changed.")
    return result
