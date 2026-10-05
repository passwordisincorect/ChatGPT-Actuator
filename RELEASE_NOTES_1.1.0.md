# ChatGPT-Actuator v1.1.0

Stable baseline verified on 2026-10-05.

## Highlights

- 73 MCP tools in the isolated server baseline.
- 104/104 regression tests passing.
- Persistent Playwright/CDP browser broker with background-first worker operation.
- DOM tab lifecycle/navigation tools and host/permission guards.
- Expanded semantic Windows UI Automation actions with target-window isolation.
- Direct batch execution and read-only batching with routing/loop guards.
- Browser account/profile support and local account bootstrap helpers.
- EDA inspection plus KiCad ERC/DRC and guarded Gerber export capabilities.
- Version metadata synchronized to 1.1.0 across source, runtime and installed editable package.

## Safety

Public/release configuration remains conservative by default. Local credentials, logs, browser profiles, DPAPI material, backups and machine-specific enabled permissions are not intended for source control or release artifacts.
