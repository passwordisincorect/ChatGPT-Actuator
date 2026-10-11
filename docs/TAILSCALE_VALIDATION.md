# Tailscale gateway validation — 2026-10-11

The optional gateway lets the existing Windows Actuator backend run over loopback HTTP and connect through Tailscale Funnel with local OAuth consent. No OpenAI Tunnel client/profile or API key is required for this connection.

Validated in a fresh virtual environment and an isolated staging directory on the user's Windows PC, without replacing the running gateway or restarting the OpenAI Tunnel:

- Python 3.12; freshly resolved MCP SDK 2.3.0.
- PowerShell syntax, Python compilation and dependency consistency: passed.
- Full regression suite: **113 tests passed**, including the real loopback HTTP gateway integration test.
- Real OAuth authorization with synthetic client credentials, MCP initialize/notification, 73 tool schemas, `system_info`, pending write approval and connection revocation: passed.
- Default stdio MCP/Admin runtime smoke test: passed, 73 tools.
- ZIP and wheel built twice with matching hashes within the Windows environment: passed.
- Final bootstrap ZIP and wheel build: passed. Local profile, OAuth state, credentials and runtime logs are excluded.
- Linux isolated gateway/transport suite: 8 passed; Windows integration skipped as expected on Linux.

These results do not claim a test on a second Windows PC or an end-to-end public ChatGPT/Funnel connection using the new packaged gateway. The existing live Funnel remained in place. CI workflow files are prepared; GitHub Actions has not run for this local revision because publication is awaiting explicit approval.

Before release, push the tested branch, wait for GitHub CI and merge PR #1. Tagged releases use the project version rather than a hardcoded v1.0.0 tag. Production replacement, if later requested, should retain the private profile/state and a rollback copy.
