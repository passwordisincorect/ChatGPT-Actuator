# ChatGPT-Actuator 1.3.0rc1

This is an **experimental prerelease**, not a stable upgrade or automatic runtime activation. The stable release remains v1.0.0. Install this candidate separately from an existing runtime.

## Changes

- Add the AD26 single-sheet SCH3 workflow: physical-pin snapshots, pin contracts, connectivity diffs, intent-based lint, native ERC, staged edits, and guarded commit/restore.
- Include the required owner-scoped r2 foundation, authenticated Work Sessions, checksum preconditions for existing-file writes, and background-only execution. Physical mouse/keyboard input remains disabled.
- Preserve configurable stdio and loopback HTTP transport, with optional Tailscale Funnel/OAuth and OpenAI Tunnel connections.
- Preserve product PowerShell timeout and resource limits. Live PowerShell tests receive a 60-second budget; the intentional timeout test stays at 0.4 seconds. `powershell_info` adds `timed_out`, `cancelled`, `duration_seconds`, and `output_truncated` while retaining existing fields. Tests parse JSON, validate `PSVersion`, and cover timeout with exit code 0.
- Include version-matched release notes in the bootstrap ZIP and mark candidate releases as prereleases.
- Write setup JSON as UTF-8 without a BOM so fresh Windows PowerShell installations can load their configuration.

## Install separately on Windows

Use Windows with Python 3.12 x64. Extract `ChatGPT-Actuator-1.3.0rc1-bootstrap-portable.zip` into a new directory, then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -RecreateVenv
```

For the optional gateway dependencies, add `-Tailscale`. Configure the chosen connection separately using the README. Setup does not start a tunnel or enable autostart unless explicitly requested. The bootstrap ZIP is not a standalone executable and downloads Python dependencies during installation.

For the core Python package, use a new virtual environment and install the downloaded wheel:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .\chatgpt_actuator-1.3.0rc1-py3-none-any.whl
```

The wheel contains the core server and Altium bridge; gateway launchers, example configuration, and setup scripts are supplied in the ZIP. Public package defaults disable mutations and PowerShell execution. Configure permitted roots and explicit capabilities in a private local config; never copy credentials into the release files.

Verify each downloaded ZIP/wheel against `SHA256SUMS.txt`. `release-manifest.json` records their SHA-256 values and marks this version as a release candidate. Keeping the current runtime untouched is the rollback path for a separate RC installation.

## Verification and limits

The tagged-release workflow runs dependency checks, PowerShell syntax checks, full Windows regression tests with `ResourceWarning` as an error, real isolated MCP/Admin checks, and repeated ZIP/wheel builds before publishing. MCP verification expects 166 tools and Admin health HTTP 200/ok. Linux PR CI covers gateway security, profiles, and transport. The base integration passed 282 Windows regressions; consult this tag's Actions run for the final release test count and results.

The seven Altium runtime files retain the qualified fingerprint `637f9aa98b46cf1a950ebc2f2817f944ceebe820e700c3dd8922a2f3241f7312`. No new native Altium qualification is claimed. Prior evidence is limited to AD26.10.1, a private standalone single-sheet fixture, and the exercised operations/settings. Native harnesses and fixture designs are not shipped. See [SCH3 validation](docs/ALTIUM-SCH3-VALIDATION.md) and [SCH3 usage](README-SCH3.md).

Multi-sheet hierarchy, PCB routing, all electrical rules, arbitrary project configurations, and exhaustive rotations are outside that evidence. Inspect and validate edits in the target project. A timeout can leave delivery uncertain; preserve operation IDs and reconcile state before retrying. A 60-second test budget does not guarantee PowerShell completes within the product's default 20 seconds on every machine.
