# Changelog

## [1.0.0] - 2026-10-02

First stable release of the current ChatGPT-Actuator architecture.

### Added

- 45 MCP tools across filesystem, system, process, PowerShell, screen, window, mouse, keyboard, clipboard, verified input and Windows UI Automation.
- Loopback-only Admin UI with hot permission apply and audit visibility.
- Windows UI Automation semantic actions and window-bound element references.
- Verified text replacement with exact read-back verification.
- Secure tunnel lifecycle scripts and deployment status tooling.
- Task Scheduler autostart for the interactive Windows user session.
- DPAPI Current User protection for the tunnel runtime API key.
- Deterministic release ZIP and wheel builder with SHA-256 manifest.
- Release verification pipeline with strict ResourceWarning handling.

### Hardened

- Protected actuator/tunnel process chain and critical Windows processes.
- Filesystem root, traversal, symlink and junction escape protections.
- PowerShell working-root constraints and output/time limits.
- Keyboard foreground-target verification.
- Password-control protection for generic UIA value access.
- Process child lifecycle cleanup to prevent unreaped subprocess warnings.
- Admin stale-session recovery while preserving unsaved permission choices.

### Verified

- 39/39 regression tests passed on the target Windows host.
- 45 MCP tools exposed.
- Reproducible ZIP and wheel builds passed.
- Live ChatGPT -> Secure MCP Tunnel -> Windows end-to-end passed.
- UIA discovery, selection, invoke, verified input and dialog handling passed.
- Autostart-after-reboot passed with DPAPI-protected credential loading.

## Historical releases

Earlier AI_Actuator releases remain available in GitHub Releases and repository history.
