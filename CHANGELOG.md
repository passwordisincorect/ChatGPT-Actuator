# Changelog

## [1.2.0] - 2026-10-11

- Add optional named Tailscale Funnel/OAuth profiles alongside OpenAI Tunnel.
- Run the same Actuator tools over loopback stateless JSON HTTP; choose backend and Admin ports from the profile.
- Add `setup.ps1 -Tailscale`, local owner consent, per-call approval and connection revocation.
- Fix the pending approval response and reject duplicate security headers/OAuth parameters.
- Add isolated OAuth/security tests, a real Windows HTTP gateway integration test and PR CI.
- Package the optional gateway in the bootstrap ZIP, excluding local OAuth state and credentials.


## [1.1.0] - 2026-10-05

### Added

- batch_execute for up to 25 local actuator actions in one MCP round trip.
- Sequential batch dependencies through explicit $ref paths.
- batch_readonly with conservative parallel read-only execution.
- Background-first semantic UI Automation actions for scroll, range values, text selection, and WindowPattern operations.
- 14 Browser DOM tools backed by Playwright + loopback-only CDP, including tab open, navigate, and close lifecycle actions.
- Browser account/profile management and account bootstrap helpers.
- Local MAIN/Actuator/Orchestrator routing policy with loop guards.
- EDA project inspection plus guarded KiCad ERC/DRC and Gerber export capabilities.
- Local Control Center and Orchestrator management helpers.

### Changed

- UIA mutation actions are background-first and keep target-window isolation.
- Browser DOM uses a persistent async Playwright/CDP connection.
- Dedicated Edge worker launch/reconnect behavior is hardened against duplicate workers and foreground focus stealing.
- Public/release configuration resolves __CHATGPT_ACTUATOR_ROOT__ safely at runtime.
- Release artifacts disable Browser DOM navigation/auto-launch/auto-Allow by default.
- EDA release defaults are read-only, project-root scoped, with edit/manufacturing disabled and external executable paths unset.
- Stable v1.1.0 MCP tool count is 73.
- Regression suite is 104 tests.

### Verified

- 104/104 unit/regression tests passed.
- Isolated MCP/Admin runtime verification passed with 73 tools.
- Reproducible bootstrap ZIP and wheel builds passed.
- Public staging checkout passed the complete 7-step release verifier with safe-default configuration.
- Secret and machine-specific path scans passed before commit.

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
