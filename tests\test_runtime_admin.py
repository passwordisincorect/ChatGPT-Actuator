from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from chatgpt_actuator.admin import AdminServer
from chatgpt_actuator.config import ConfigError, load_config
from chatgpt_actuator.runtime import RuntimeManager


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class RuntimeAdminTests(unittest.TestCase):
    def make_config(self, root: Path) -> Path:
        project = Path(__file__).resolve().parents[1]
        raw = json.loads((project / "config" / "config.json").read_text(encoding="utf-8"))
        placeholder = "__CHATGPT_ACTUATOR_ROOT__"
        if raw["filesystem"].get("allowed_roots") == [placeholder]:
            raw["filesystem"]["allowed_roots"] = [str(project)]
        if raw["powershell"].get("allowed_working_roots") == [placeholder]:
            raw["powershell"]["allowed_working_roots"] = [str(project)]
        if raw["powershell"].get("default_cwd") == placeholder:
            raw["powershell"]["default_cwd"] = str(project)
        raw["keyboard"]["enabled"] = True
        raw["keyboard"]["allow_write"] = True
        raw["admin"]["host"] = "127.0.0.1"
        raw["admin"]["port"] = free_port()
        raw["logging"]["enabled"] = True
        raw["logging"]["file"] = str(root / "audit.log")
        path = root / "config.json"
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def request_json(url: str, *, token: str | None = None, method: str = "GET", body: dict | None = None):
        data = None
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["X-Actuator-Admin-Token"] = token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_admin_hot_reload_and_token_protection(self):
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            config_path = self.make_config(temp)
            runtime = RuntimeManager(config_path)
            admin = AdminServer(runtime)
            admin.start()
            try:
                time.sleep(0.1)
                base = admin.url.rstrip("/")

                status, health = self.request_json(base + "/healthz")
                self.assertEqual(status, 200)
                self.assertEqual(health["version"], runtime.config.version)
                self.assertEqual(health["generation"], 1)

                with urllib.request.urlopen(base + "/", timeout=3) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("actuator_pending_patch", html)
                self.assertIn("Admin session expired", html)
                self.assertIn("Your unsaved choices were restored", html)

                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.request_json(base + "/api/status")
                self.assertEqual(ctx.exception.code, 403)

                status, initial = self.request_json(
                    base + "/api/status",
                    token=admin.token,
                )
                self.assertEqual(status, 200)
                self.assertTrue(initial["capabilities"]["keyboard"]["enabled"])
                self.assertEqual(initial["generation"], 1)

                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.request_json(
                        base + "/api/config",
                        method="POST",
                        body={"keyboard": {"enabled": False}},
                    )
                self.assertEqual(ctx.exception.code, 403)

                status, changed = self.request_json(
                    base + "/api/config",
                    token=admin.token,
                    method="POST",
                    body={"keyboard": {"enabled": False}},
                )
                self.assertEqual(status, 200)
                self.assertFalse(changed["capabilities"]["keyboard"]["enabled"])
                self.assertEqual(changed["generation"], 2)

                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertFalse(saved["keyboard"]["enabled"])
                self.assertTrue(config_path.with_suffix(".json.bak").exists())

                keyboard = runtime.proxy("keyboard")
                with self.assertRaises(PermissionError):
                    keyboard.write("blocked", target_hwnd=1)

                generation_before_invalid = runtime.generation
                raw_before_invalid = config_path.read_text(encoding="utf-8")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.request_json(
                        base + "/api/config",
                        token=admin.token,
                        method="POST",
                        body={"filesystem": {"allowed_roots": []}},
                    )
                self.assertEqual(ctx.exception.code, 400)
                self.assertEqual(runtime.generation, generation_before_invalid)
                self.assertEqual(config_path.read_text(encoding="utf-8"), raw_before_invalid)

                status, restored = self.request_json(
                    base + "/api/config",
                    token=admin.token,
                    method="POST",
                    body={"keyboard": {"enabled": True}},
                )
                self.assertEqual(status, 200)
                self.assertTrue(restored["capabilities"]["keyboard"]["enabled"])
                self.assertEqual(restored["generation"], 3)

                status, audit = self.request_json(
                    base + "/api/audit?limit=50",
                    token=admin.token,
                )
                self.assertEqual(status, 200)
                actions = [event.get("action") for event in audit["events"]]
                self.assertIn("admin_config_applied", actions)
                self.assertIn("admin_server_started", actions)
            finally:
                admin.stop()

    def test_admin_host_must_be_loopback(self):
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            config_path = self.make_config(temp)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["admin"]["host"] = "0.0.0.0"
            config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
