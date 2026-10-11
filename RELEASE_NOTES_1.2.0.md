# ChatGPT-Actuator 1.2.0

Tailscale Funnel is now an optional connection method alongside OpenAI Secure MCP Tunnel. A local OAuth gateway proxies the existing Actuator tools over loopback HTTP; it does not require an OpenAI tunnel client, API key or tunnel profile.

On Windows, install Python 3.12 x64 and Tailscale, sign in to Tailscale, then run `scripts/setup.ps1 -Tailscale`. Follow the connection-profile steps in README.vi.md or README.md. Approve OAuth from the local owner console. The example requires approval for calls outside the core read-only group; backend permissions continue to apply.

The bootstrap ZIP includes the gateway, profile example and launchers. It is not a standalone executable. The Python wheel provides the core Actuator; use the ZIP or repository for gateway launchers and profiles. OpenAI Tunnel remains available and is not stopped by the Tailscale launchers.

PR CI covers OAuth/PKCE, resource binding, one-time call approval, rotation/revocation, malformed requests, a real Windows loopback HTTP backend and reproducible ZIP/wheel packages. Public ChatGPT/Funnel setup still requires the installing owner's Tailscale configuration and explicit OAuth approval.
