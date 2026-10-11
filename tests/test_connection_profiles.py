"""Isolated OAuth profile regression checks; no listener or network access."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


class ConnectionProfilesTest(unittest.TestCase):
    def test_tailscale_profile_oauth_and_loopback(self):
        sample = json.loads((ROOT / "config" / "connection-profiles.example.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            sample["profiles"]["tailscale"]["issuer"] = "https://test-node.example.ts.net"
            sample["profiles"]["tailscale"]["state_dir"] = str(Path(tmp) / "state")
            path.write_text(json.dumps(sample), encoding="utf-8")
            old_file = os.environ.get("CHATGPT_ACTUATOR_PROFILE_FILE")
            old_name = os.environ.get("CHATGPT_ACTUATOR_PROFILE")
            os.environ["CHATGPT_ACTUATOR_PROFILE_FILE"] = str(path)
            os.environ["CHATGPT_ACTUATOR_PROFILE"] = "tailscale"
            sys.path.insert(0, str(ROOT / "integrations" / "tailscale_gateway"))
            try:
                from gateway_config import load_profile
                import gateway as g
                self.assertTrue(g.full.PER_CALL_APPROVAL_REQUIRED)
                self.assertFalse(load_profile(path, "tailscale")["direct_local_approval"])
                g.initialize_db()
                self.assertTrue(g.DB.exists())

                async def exercise():
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=g.public_app),
                        base_url=g.ISSUER,
                        follow_redirects=False,
                    ) as client:
                        meta = await client.get("/.well-known/oauth-protected-resource/full/mcp")
                        self.assertEqual(meta.json()["resource"], g.FULL_RESOURCE)
                        denied = await client.post("/full/mcp", json={
                            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                        })
                        self.assertEqual(denied.status_code, 401)
                        reg = await client.post("/oauth/register", json={
                            "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
                            "token_endpoint_auth_method": "none",
                            "scope": "mcp:read mcp:write mcp:execute",
                        })
                        self.assertEqual(reg.status_code, 201, reg.text)
                        self.assertEqual(reg.json()["scope"], "mcp:read mcp:write mcp:execute")
                        consent = await client.get("/oauth/authorize", params={
                            "response_type": "code",
                            "client_id": reg.json()["client_id"],
                            "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
                            "code_challenge_method": "S256",
                            "code_challenge": "A" * 43,
                            "resource": g.FULL_RESOURCE,
                            "scope": "mcp:read mcp:write mcp:execute",
                            "state": "test-state",
                        })
                        self.assertEqual(consent.status_code, 200)
                        self.assertIn("Awaiting approval", consent.text)
                        self.assertIn("127.0.0.1:8189", consent.text)
                asyncio.run(exercise())

                sample["profiles"]["tailscale"]["actuator_backend"] = "http://remote.example:8090/mcp"
                path.write_text(json.dumps(sample), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_profile(path, "tailscale")
            finally:
                sys.path.remove(str(ROOT / "integrations" / "tailscale_gateway"))
                if old_file is None:
                    os.environ.pop("CHATGPT_ACTUATOR_PROFILE_FILE", None)
                else:
                    os.environ["CHATGPT_ACTUATOR_PROFILE_FILE"] = old_file
                if old_name is None:
                    os.environ.pop("CHATGPT_ACTUATOR_PROFILE", None)
                else:
                    os.environ["CHATGPT_ACTUATOR_PROFILE"] = old_name


if __name__ == "__main__":
    unittest.main()
