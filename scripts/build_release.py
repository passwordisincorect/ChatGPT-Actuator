from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
SOURCE_DATE_EPOCH = "1767225600"
PLACEHOLDER_ROOT = "__CHATGPT_ACTUATOR_ROOT__"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_version() -> str:
    raw = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(raw["project"]["version"])


def copy_release_tree(stage: Path) -> None:
    for name in ("src", "config", "tests"):
        src = ROOT / name
        if src.exists():
            shutil.copytree(
                src,
                stage / name,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.egg-info"),
            )

    release_scripts = (
        "autostart-entry.ps1",
        "autostart-status.ps1",
        "build_release.py",
        "build-portable.ps1",
        "build-release.ps1",
        "configure-tunnel.ps1",
        "deployment-status.ps1",
        "doctor-tunnel.ps1",
        "install.ps1",
        "install-autostart.ps1",
        "open-admin.ps1",
        "remove-autostart.ps1",
        "remove-runtime-key.ps1",
        "save-runtime-key.ps1",
        "setup.ps1",
        "start.ps1",
        "start-tunnel.ps1",
        "test.ps1",
        "tunnel-manager.ps1",
        "uninstall.ps1",
        "verify-release.ps1",
        "verify_runtime.py",
    )
    scripts_stage = stage / "scripts"
    scripts_stage.mkdir(parents=True, exist_ok=True)
    for name in release_scripts:
        src = ROOT / "scripts" / name
        if src.exists():
            shutil.copy2(src, scripts_stage / name)

    for name in ("pyproject.toml", "README.md", "README.vi.md", "LICENSE"):
        shutil.copy2(ROOT / name, stage / name)


def write_safe_release_config(stage: Path, version: str) -> None:
    path = stage / "config" / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))

    raw["server"]["version"] = version

    fs = raw["filesystem"]
    fs["enabled"] = True
    fs["read_only"] = True
    fs["allow_create"] = False
    fs["allow_edit"] = False
    fs["allow_move"] = False
    fs["allow_delete"] = False
    fs["allowed_roots"] = [PLACEHOLDER_ROOT]

    proc = raw["process"]
    proc["enabled"] = True
    proc["allow_start"] = False
    proc["allow_stop"] = False
    proc["allow_force_kill"] = False

    ps = raw["powershell"]
    ps["enabled"] = False
    ps["allow_execute"] = False
    ps["default_cwd"] = PLACEHOLDER_ROOT
    ps["allowed_working_roots"] = [PLACEHOLDER_ROOT]

    raw["screen"]["enabled"] = False

    win = raw["window"]
    win["enabled"] = True
    win["allow_focus"] = False
    win["allow_state_change"] = False
    win["allow_close"] = False

    mouse = raw["mouse"]
    mouse["enabled"] = False
    mouse["allow_move"] = False
    mouse["allow_click"] = False
    mouse["allow_scroll"] = False

    keyboard = raw["keyboard"]
    keyboard["enabled"] = False
    keyboard["allow_write"] = False
    keyboard["allow_press"] = False
    keyboard["allow_hotkey"] = False

    clipboard = raw["clipboard"]
    clipboard["enabled"] = False
    clipboard["allow_read"] = False
    clipboard["allow_write"] = False

    verified = raw["verified_input"]
    verified["enabled"] = False

    uia = raw["ui_automation"]
    uia["enabled"] = True
    uia["allow_focus"] = False
    uia["allow_invoke"] = False
    uia["allow_set_value"] = False
    uia["allow_toggle"] = False
    uia["allow_select"] = False
    uia["allow_expand_collapse"] = False
    uia["allow_scroll"] = False
    uia["allow_scroll_into_view"] = False
    uia["allow_range_value"] = False
    uia["allow_text_selection"] = False
    uia["allow_window_action"] = False

    raw["admin"]["enabled"] = True
    raw["admin"]["host"] = "127.0.0.1"
    raw["admin"]["port"] = 8765

    path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def validate_release_tree(stage: Path) -> None:
    forbidden_parts = {".venv", "logs", "deployment", "dist", "build", "__pycache__"}
    forbidden_names = {"control-plane-key.dpapi"}

    for path in stage.rglob("*"):
        rel = path.relative_to(stage)
        if any(part in forbidden_parts or part.endswith(".egg-info") for part in rel.parts):
            raise RuntimeError(f"Forbidden release path included: {rel}")
        if path.name in forbidden_names or path.suffix.casefold() in {".pyc", ".pyo"}:
            raise RuntimeError(f"Forbidden release file included: {rel}")

    config_path = stage / "config" / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    checks = {
        "filesystem.read_only": raw["filesystem"]["read_only"] is True,
        "filesystem.write_disabled": all(
            raw["filesystem"][key] is False
            for key in ("allow_create", "allow_edit", "allow_move", "allow_delete")
        ),
        "filesystem.placeholder_root": raw["filesystem"]["allowed_roots"] == [PLACEHOLDER_ROOT],
        "process.start_stop_disabled": (
            raw["process"]["allow_start"] is False
            and raw["process"]["allow_stop"] is False
            and raw["process"]["allow_force_kill"] is False
        ),
        "powershell.disabled": (
            raw["powershell"]["enabled"] is False
            and raw["powershell"]["allow_execute"] is False
        ),
        "screen.disabled": raw["screen"]["enabled"] is False,
        "window.mutation_disabled": (
            raw["window"]["allow_focus"] is False
            and raw["window"]["allow_state_change"] is False
            and raw["window"]["allow_close"] is False
        ),
        "mouse.disabled": raw["mouse"]["enabled"] is False,
        "keyboard.disabled": raw["keyboard"]["enabled"] is False,
        "clipboard.disabled": raw["clipboard"]["enabled"] is False,
        "verified_input.disabled": raw["verified_input"]["enabled"] is False,
        "uia.actions_disabled": (
            raw["ui_automation"]["allow_focus"] is False
            and raw["ui_automation"]["allow_invoke"] is False
            and raw["ui_automation"]["allow_set_value"] is False
            and raw["ui_automation"]["allow_toggle"] is False
            and raw["ui_automation"]["allow_select"] is False
            and raw["ui_automation"]["allow_expand_collapse"] is False
            and raw["ui_automation"]["allow_scroll"] is False
            and raw["ui_automation"]["allow_scroll_into_view"] is False
            and raw["ui_automation"]["allow_range_value"] is False
            and raw["ui_automation"]["allow_text_selection"] is False
            and raw["ui_automation"]["allow_window_action"] is False
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError("Release safe-default validation failed: " + ", ".join(failed))

    legacy_verifiers = [
        path.name
        for path in (stage / "scripts").glob("verify_v*.py")
    ]
    if legacy_verifiers:
        raise RuntimeError(
            "Legacy version-specific verifiers leaked into release: "
            + ", ".join(sorted(legacy_verifiers))
        )


def write_install_text(stage: Path, version: str) -> None:
    text = f"""ChatGPT-Actuator {version} bootstrap release package.

Safe defaults:
- filesystem read-only, limited to the extracted project folder after setup
- process listing enabled, process start/stop disabled
- PowerShell disabled
- screenshots disabled
- window inspection enabled, window mutation disabled
- mouse/keyboard/clipboard disabled
- UI Automation discovery enabled, UIA actions disabled

Requirements:
- Windows
- Python 3.12 x64
- OpenAI tunnel-client installed
- a configured/authorized tunnel profile

Install:
  powershell -ExecutionPolicy Bypass -File .\\scripts\\setup.ps1 -RecreateVenv

Enable additional permissions from the local Admin UI after installation:
  http://127.0.0.1:8765/

This package is not a standalone executable.
No API key, DPAPI credential, logs, .venv, build directory, or deployment state is included.
"""
    (stage / "INSTALL.txt").write_text(text, encoding="utf-8")


def build_wheel(stage: Path) -> Path:
    wheel_dir = stage / "wheel"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(ROOT),
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected exactly one wheel, found {len(wheels)}.")
    return wheels[0]


def deterministic_zip(stage: Path, output: Path) -> None:
    if output.exists():
        output.unlink()

    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            rel = path.relative_to(stage).as_posix()
            info = zipfile.ZipInfo(rel, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "dist"))
    parser.add_argument("--skip-wheel", action="store_true")
    args = parser.parse_args()

    version = project_version()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    stage = output_dir / f"ChatGPT-Actuator-{version}"
    archive = output_dir / f"ChatGPT-Actuator-{version}-bootstrap-portable.zip"

    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    copy_release_tree(stage)
    write_safe_release_config(stage, version)
    write_install_text(stage, version)

    wheel = None
    if not args.skip_wheel:
        wheel = build_wheel(stage)
        shutil.copy2(wheel, output_dir / wheel.name)

    validate_release_tree(stage)
    deterministic_zip(stage, archive)

    artifacts = [
        {
            "kind": "bootstrap_zip",
            "path": str(archive),
            "size_bytes": archive.stat().st_size,
            "sha256": sha256(archive),
            "standalone": False,
        }
    ]
    if wheel is not None:
        copied_wheel = output_dir / wheel.name
        artifacts.append(
            {
                "kind": "wheel",
                "path": str(copied_wheel),
                "size_bytes": copied_wheel.stat().st_size,
                "sha256": sha256(copied_wheel),
            }
        )

    sums = output_dir / "SHA256SUMS.txt"
    sums.write_text(
        "".join(
            f"{item['sha256']}  {Path(item['path']).name}\n"
            for item in artifacts
        ),
        encoding="ascii",
    )

    manifest = {
        "name": "ChatGPT-Actuator",
        "version": version,
        "release_candidate": "rc" in version.casefold(),
        "standalone": False,
        "safe_default_config": True,
        "artifacts": artifacts,
        "sha256sums": str(sums),
    }
    manifest_path = output_dir / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
