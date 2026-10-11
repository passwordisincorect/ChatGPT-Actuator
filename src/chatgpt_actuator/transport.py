"""Transport selection; HTTP backends always bind to loopback."""
import os


def run_options(environ=None):
    env = os.environ if environ is None else environ
    transport = env.get("ACTUATOR_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        return {"transport": "stdio"}
    if transport not in {"http", "streamable-http"}:
        raise ValueError("ACTUATOR_MCP_TRANSPORT must be stdio or http")
    host = env.get("ACTUATOR_MCP_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Actuator HTTP must bind to loopback")
    port = int(env.get("ACTUATOR_MCP_PORT", "8090"))
    if not 1 <= port <= 65535:
        raise ValueError("ACTUATOR_MCP_PORT must be a TCP port")
    return {"transport": "streamable-http", "host": host, "port": port,
            "streamable_http_path": "/mcp", "json_response": True,
            "stateless_http": True}
