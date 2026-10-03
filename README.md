[English](README.md) | [Tiếng Việt](README.vi.md)

# ChatGPT-Actuator 1.0.0

ChatGPT-Actuator is a permission-gated Windows MCP actuator for ChatGPT.

Version 1.0.0 is the first stable release of the current Windows control-plane architecture.

## Runtime

- Windows desktop actuator
- MCP over stdio behind OpenAI Secure MCP Tunnel
- 50 MCP tools in the current local development state (the published v1.0.0 release had 45)
- local Admin UI on `http://127.0.0.1:8765/`
- tunnel profile: `chatgpt-actuator`
- default tunnel health: `http://127.0.0.1:8081/healthz`

## Capability groups

- Filesystem
- System information
- Process management
- PowerShell
- Screen capture
- Window management
- Mouse
- Keyboard
- Clipboard
- Verified text input
- Windows UI Automation

## Security model

ChatGPT-Actuator keeps execution behind explicit permission gates. The local Admin UI can hot-apply capability changes without restarting the tunnel.

Important protections include:

- allowed filesystem roots
- root deletion/move protection
- path traversal and link/junction escape protection
- protected actuator/tunnel process chain
- protected critical Windows processes
- constrained PowerShell working roots
- target-window verification for keyboard input
- semantic UI Automation actions with window-bound element references
- password controls excluded from generic value read/set
- audit logging without plaintext keyboard/clipboard secrets
- loopback-only Admin UI with a random per-process session token

A stale Admin browser tab automatically reloads after a process restart and restores unsaved permission choices for review.

### Local UIA background-first update (unreleased)

The current local source adds five semantic UI Automation actions: uia_scroll, uia_scroll_into_view, uia_set_range_value, uia_text_select_all, and uia_window_action. Existing UIA mutation tools now default to auto_focus_window=false, so they attempt background semantic actions first. These UIA actions do not synthesize physical mouse or keyboard input. Mouse and keyboard tools remain available only as explicit fallback capabilities.

## Deployment

Setup:

```powershell
.\scripts\setup.ps1
```

Clean/recreate the virtual environment:

```powershell
.\scripts\setup.ps1 -RecreateVenv
```

Deployment status:

```powershell
.\scripts\deployment-status.ps1
```

Tunnel lifecycle:

```powershell
.\scripts\tunnel-manager.ps1 -Action Status
.\scripts\tunnel-manager.ps1 -Action Start
.\scripts\tunnel-manager.ps1 -Action Stop
.\scripts\tunnel-manager.ps1 -Action Restart
```

Open Admin:

```powershell
.\scripts\open-admin.ps1
```

## Autostart

Autostart uses Windows Task Scheduler at interactive user logon. A Windows Service is intentionally not used because mouse, keyboard, window and UI Automation features need the interactive desktop session.

Protect the current runtime API key with Windows DPAPI:

```powershell
.\scripts\save-runtime-key.ps1
```

Install and check autostart:

```powershell
.\scripts\install-autostart.ps1
.\scripts\autostart-status.ps1
```

Remove autostart:

```powershell
.\scripts\remove-autostart.ps1
```

Remove the DPAPI-protected runtime key separately:

```powershell
.\scripts\remove-runtime-key.ps1
```

The plaintext key is not written to disk by `save-runtime-key.ps1`.

## Release package

Build:

```powershell
.\scripts\build-release.ps1
```

The generated bootstrap ZIP is not a standalone executable. It requires Windows, Python 3.12 x64, OpenAI tunnel-client, and an authorized tunnel profile.

The release builder:

- creates a deterministic ZIP
- builds a Python wheel
- creates `SHA256SUMS.txt`
- creates `release-manifest.json`
- validates that sensitive/local runtime state is not included
- uses a safe-default config inside the artifact
- excludes local build metadata such as `*.egg-info`

### Safe-default artifact config

On a fresh extracted package:

- filesystem is read-only and rooted to the extracted project after setup
- process listing is enabled, but start/stop/force-kill are disabled
- PowerShell is disabled
- screen capture is disabled
- window inspection is enabled, but mutations are disabled
- mouse, keyboard and clipboard are disabled
- verified input is disabled
- UI Automation discovery is enabled, but semantic actions are disabled
- Admin UI is enabled on loopback only

Permissions can then be explicitly enabled from the local Admin UI.

## Release verification

Run:

```powershell
.\scripts\verify-release.ps1
```

The verifier checks:

1. PowerShell syntax
2. Python compilation
3. pip dependency consistency
4. full 41-test regression suite with `ResourceWarning` treated as an error
5. isolated MCP/Admin runtime smoke test
6. reproducible ZIP and wheel builds
7. final release artifact build

Expected MCP tool count for the current local development state: **50**.

Before v1.0.0 was published, the release candidate also passed live end-to-end testing through ChatGPT → Secure MCP Tunnel → ChatGPT-Actuator → Windows, including UI Automation tab selection, verified text replacement/read-back, dialog handling and autostart-after-reboot.

## License

This project is licensed under the [MIT License](LICENSE).

## Contribution and safety

Keep Windows-control permissions conservative by default. Do not commit local credentials, DPAPI files, logs, deployment state or tunnel secrets.
