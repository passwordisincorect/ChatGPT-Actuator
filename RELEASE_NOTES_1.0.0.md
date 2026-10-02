# ChatGPT-Actuator v1.0.0

First stable release of the current permission-gated Windows MCP actuator.

Highlights:
- 45 MCP tools for Windows/filesystem/UI automation workflows.
- Local Admin UI with hot permission apply.
- Windows UI Automation semantic actions and verified text input.
- Secure tunnel lifecycle and Task Scheduler autostart.
- DPAPI-protected runtime credential storage.
- Deterministic bootstrap ZIP + Python wheel + SHA-256 manifest.
- Full 39/39 regression suite passed on the target Windows host.
- Live ChatGPT -> Secure MCP Tunnel -> Windows end-to-end passed.
- Autostart after a real Windows reboot passed.

The bootstrap ZIP is not a standalone executable. It requires Windows, Python 3.12 x64, OpenAI tunnel-client and an authorized tunnel profile.
