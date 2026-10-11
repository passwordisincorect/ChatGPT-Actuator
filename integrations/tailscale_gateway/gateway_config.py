"""Validated connection profiles for the optional OAuth gateway."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = Path(os.environ.get("CHATGPT_ACTUATOR_PROFILE_FILE", str(PROJECT_ROOT / "config" / "connection-profiles.local.json")))
PROFILE_NAME = os.environ.get("CHATGPT_ACTUATOR_PROFILE", "tailscale")

def _loopback_url(value, field):
    u = urlsplit(value)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1") or not u.port or u.username or u.password or u.query or u.fragment or u.path != "/mcp":
        raise ValueError(f"{field} must be an HTTP loopback MCP URL")
    return value

def load_profile(path=PROFILE_FILE, name=PROFILE_NAME):
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    profile = raw["profiles"][name]
    if profile.get("transport") != "tailscale_funnel":
        raise ValueError("Profile transport must be tailscale_funnel")
    issuer = profile["issuer"].rstrip("/")
    u = urlsplit(issuer)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.path or u.query or u.fragment:
        raise ValueError("issuer must be an HTTPS origin")
    if not u.hostname.endswith(".ts.net") or u.port not in (None, 443, 8443, 10000):
        raise ValueError("issuer must use a Tailscale hostname and Funnel HTTPS port (443, 8443 or 10000)")
    if u.hostname == "your-tailnet-name.ts.net":
        raise ValueError("Set the real Tailscale Funnel issuer in the local profile")
    for field in ("public_port", "owner_port"):
        value=profile[field]
        if type(value) is not int or not 1 <= value <= 65535:
            raise ValueError(f"{field} must be a TCP port")
    admin_port = profile.get("actuator_admin_port", 8766)
    if type(admin_port) is not int or not 1 <= admin_port <= 65535:
        raise ValueError("actuator_admin_port must be a TCP port")
    ports = [profile["public_port"], profile["owner_port"], admin_port,
             urlsplit(_loopback_url(profile["actuator_backend"], "actuator_backend")).port]
    if len(ports) != len(set(ports)):
        raise ValueError("Gateway, owner, backend and Actuator admin ports must differ")
    for field in ("actuator_backend", "orchestrator_backend"):
        _loopback_url(profile[field],field)
    for field in ("per_call_approval", "direct_local_approval"):
        if type(profile[field]) is not bool:
            raise ValueError(f"{field} must be Boolean")
    state=Path(profile["state_dir"])
    if not state.is_absolute():
        state=PROJECT_ROOT/state
    profile=dict(profile)
    profile["actuator_admin_port"] = admin_port
    profile["issuer"]=issuer
    profile["state_dir"]=state.resolve()
    return profile

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", required=True, type=Path)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    print(json.dumps(load_profile(args.profiles, args.profile), default=str))
