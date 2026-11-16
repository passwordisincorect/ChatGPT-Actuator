# ChatGPT-Actuator 1.3.0rc2

This is an **experimental prerelease**, not a stable upgrade. The stable release remains v1.0.0. Install this candidate separately; it does not update an existing runtime automatically.

## Changes

- Add `dom_browser_attach(cdp_url="auto")` for owner-authorized connection to the currently running personal Edge profile. It discovers the current loopback CDP endpoint, handles only a newly correlated native consent dialog, and supports selecting a unique existing tab from returned metadata.
- If remote debugging is off, an explicitly granted UIA helper opens the exact Edge settings page in a temporary window, enables the remote-debugging checkbox, closes its window, and restores the prior foreground. It does not restart Edge or use physical keyboard or mouse input. Avoid interacting with Edge during this brief setup.
- Keep the connection scoped to the authenticated owner and Work Session. Existing tabs stay open. Attach reuses its connection; after a disconnect it validates Edge and the endpoint again, retires old references, and does not replay actions.
- Public defaults keep `security.browser_auto_connect` disabled. Automatic access requires a private local grant bound to the exact authenticated owner, the verified personal Edge user-data directory, and profile `Default`. Temporary settings-window focus requires `allow_temporary_focus: true`. Legacy Worker consent settings do not authorize a personal Edge profile.

The UIA consent and off-to-on setup were exercised locally on Microsoft Edge 157. A signed-in Drive tab was selected and read, then the connection was reused and re-established after disconnect. Edge kept the same process. This is local evidence for the tested Windows environment, not a guarantee across all Edge versions or machines.

## Install separately on Windows

Use Windows with Python 3.12 x64. Extract `ChatGPT-Actuator-1.3.0rc2-bootstrap-portable.zip` into a new directory, then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -RecreateVenv
```

For the optional gateway dependencies, add `-Tailscale`. Configure the chosen connection separately using the README. Setup does not start a tunnel or enable autostart unless explicitly requested. The bootstrap ZIP is not a standalone executable and downloads Python dependencies during installation.

For the core Python package, use a new virtual environment and install the downloaded wheel:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .\chatgpt_actuator-1.3.0rc2-py3-none-any.whl
```

The wheel contains the core server and Altium bridge; gateway launchers, example configuration, and setup scripts are supplied in the ZIP. Public package defaults keep automatic personal-browser access disabled. Configure the exact local owner grant only when using auto attach. Never copy private credentials into release files.

Verify each downloaded ZIP/wheel against `SHA256SUMS.txt`. `release-manifest.json` records their SHA-256 values and marks this version as a release candidate. Keep the current runtime as the rollback option for a separately installed RC.

## Verification and limits

The tagged-release workflow runs dependency and PowerShell syntax checks, Windows regression tests with `ResourceWarning` treated as an error, isolated MCP/Admin checks, and repeated ZIP/wheel builds. Linux PR CI also runs mocked CDP identity, consent, and session tests alongside gateway security, profiles, and transport tests. These are the configured checks; consult the PR and tag's Actions runs for their actual results.

The seven Altium runtime files retain the existing qualified fingerprint `637f9aa98b46cf1a950ebc2f2817f944ceebe820e700c3dd8922a2f3241f7312`. No new native Altium qualification is claimed; Altium was not rerun for this prerelease. Prior evidence is limited to AD26.10.1, a private standalone single-sheet fixture, and the exercised operations/settings. Native harnesses and fixture designs are not shipped. See [SCH3 validation](docs/ALTIUM-SCH3-VALIDATION.md) and [SCH3 usage](README-SCH3.md).

Multi-sheet hierarchy, PCB routing, all electrical rules, arbitrary project configurations, and exhaustive rotations are outside that evidence. The CDP UIA bootstrap was observed on Edge 157 only. A 60-second test budget does not guarantee PowerShell completes within the product's default 20 seconds on every machine.
