[English](README.md) | [Tiếng Việt](README.vi.md)

# ChatGPT-Actuator 1.2.0

ChatGPT-Actuator is a permission-gated Windows MCP actuator for ChatGPT.

Version 1.2.0 adds an optional Tailscale Funnel connection with owner-approved OAuth, alongside the existing OpenAI Tunnel connection.

## Runtime

- Windows desktop actuator
- MCP over stdio behind OpenAI Secure MCP Tunnel
- 73 MCP tools in the current v1.1.0 baseline (the published v1.0.0 release had 45)
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
- Browser DOM automation via Playwright + loopback-only CDP
- EDA project inspection plus guarded KiCad ERC/DRC and Gerber export

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
- browser DOM host allowlist and loopback-only CDP endpoint
- password controls excluded from generic value read/set
- audit logging without plaintext keyboard/clipboard secrets
- loopback-only Admin UI with a random per-process session token

A stale Admin browser tab automatically reloads after a process restart and restores unsaved permission choices for review.

### v1.1.0 background-first UIA, Browser DOM, routing and EDA update

The current local source adds five semantic UI Automation actions: uia_scroll, uia_scroll_into_view, uia_set_range_value, uia_text_select_all, and uia_window_action. Existing UIA mutation tools now default to auto_focus_window=false, so they attempt background semantic actions first. These UIA actions do not synthesize physical mouse or keyboard input. Mouse and keyboard tools remain available only as explicit fallback capabilities.

The v1.1.0 baseline includes 14 browser DOM tools backed by Playwright attached to Microsoft Edge through CDP: dom_list_tabs, dom_find, dom_details, dom_read_text, dom_click, dom_set_value, dom_select, dom_toggle, dom_scroll_into_view, dom_scroll, dom_wait, dom_open_tab, dom_navigate, and dom_close_tab. CDP supports auto-discovery through Edge's local DevToolsActivePort file (for an edge://inspect-enabled existing browser) or an explicit loopback endpoint; DOM access is further restricted by an allowed-host list. DOM actions do not use the physical OS mouse or keyboard.

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

The generated bootstrap ZIP is not a standalone executable. It requires Windows and Python 3.12 x64. OpenAI connections need tunnel-client/profile; Tailscale connections need Tailscale and the optional gateway dependencies.

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
- Browser DOM is present but read/mutation/navigation permissions are disabled, auto-launch is disabled, and allowed hosts are empty by default in release artifacts
- EDA inspection is read-only and rooted to the extracted project; edit/manufacturing actions are disabled and external tool paths are unset
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
4. full regression suite, including isolated OAuth/security and Windows HTTP gateway integration tests with `ResourceWarning` treated as an error
5. isolated MCP/Admin runtime smoke test
6. reproducible ZIP and wheel builds
7. final release artifact build

Expected MCP tool count for the current v1.1.0 baseline: **73**.

Before v1.0.0 was published, the release candidate also passed live end-to-end testing through ChatGPT → Secure MCP Tunnel → ChatGPT-Actuator → Windows, including UI Automation tab selection, verified text replacement/read-back, dialog handling and autostart-after-reboot.

## License

This project is licensed under the [MIT License](LICENSE).

## Contribution and safety

Keep Windows-control permissions conservative by default. Do not commit local credentials, DPAPI files, logs, deployment state or tunnel secrets.


## Optional connection profiles

The actuator tools in src/chatgpt_actuator serve both OpenAI Secure MCP Tunnel and Tailscale Funnel. The optional OAuth gateway is in integrations/tailscale_gateway; it does not duplicate tools or alter the existing OpenAI Tunnel setup.

| Transport | ChatGPT endpoint | Local runtime |
| --- | --- | --- |
| openai_tunnel | OpenAI Secure MCP Tunnel | Existing stdio profile |
| tailscale_funnel | https://HOST/full/mcp | HTTP actuator 127.0.0.1:8090, OAuth gateway 127.0.0.1:8188, owner page 127.0.0.1:8189, Actuator Admin 127.0.0.1:8766 |

To set up a different Windows PC:

1. Install Python 3.12 x64 and Tailscale, sign in to Tailscale, then run `scripts/setup.ps1 -Tailscale`. No OpenAI Tunnel client, API key or tunnel profile is required for this connection.
2. Copy config/connection-profiles.example.json to config/connection-profiles.local.json. Set issuer to your actual HTTPS Tailscale Funnel hostname. Leave backend and owner page on loopback. Give concurrent gateway profiles separate ports and state directories.
3. In one PowerShell terminal run .\scripts\start-actuator-http.ps1 -Profile tailscale. It starts the existing actuator in HTTP mode on port 8090. If that port is occupied, use the already running HTTP actuator or stop it first.
4. In another terminal run .\scripts\gateway-profile.ps1 -Action Validate -Profile tailscale, then .\scripts\gateway-profile.ps1 -Action Run -Profile tailscale. Check -Action Status. Review OAuth requests at http://127.0.0.1:8189/ on that PC.
5. Explicitly publish the gateway with .\scripts\gateway-profile.ps1 -Action EnableFunnel -Profile tailscale. Connect ChatGPT to the /full/mcp resource with scopes mcp:read mcp:write mcp:execute. Only port 8188 is published.

To start an existing OpenAI tunnel profile, use .\scripts\gateway-profile.ps1 -Action Run -Profile openai, which delegates to tunnel-manager.ps1 after normal OpenAI setup. No gateway is involved in this mode.

OAuth always needs local owner consent. The sample profile also requires local approval for non-read tool calls. A local profile can set per_call_approval to false after review. Actuator commands run under the Windows user account; the gateway does not sandbox them. The tool count comes from the running actuator backend and its capability settings. The optional /orch/mcp route needs a separate Orchestrator HTTP backend and is not started by these scripts. Local profile and OAuth state are excluded from Git.

Publish only the OAuth gateway through Funnel; never publish the backend or owner console. The backend port comes from the profile; `actuator_admin_port` defaults to 8766 so it can coexist with OpenAI Tunnel. HTTP environment variables affect only the new process. Backend permission settings still apply.

Stop: Ctrl+C in the backend/gateway consoles. To disable Funnel, use `tailscale funnel --https=443 off` (or your chosen HTTPS port); this affects all paths on that port. Avoid `funnel reset` when other services exist. Roll back by checking out the preceding tag/commit and retaining private profile/state directories; do not restore revoked tokens.

CI runs on every PR: Linux security regressions, Windows regressions and a real loopback HTTP gateway, then reproducible ZIP/wheel builds. Loopback tests do not replace ChatGPT OAuth consent and a public Funnel connection on the installing PC.
