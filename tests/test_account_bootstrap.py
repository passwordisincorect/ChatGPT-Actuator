from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from chatgpt_actuator.account_bootstrap import AccountBootstrap


class AccountBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.actuator = root / "ChatGPT-Actuator"
        self.orchestrator = root / "ChatGPT-Orchestrator"
        self.actuator.mkdir(parents=True)
        self.orchestrator.mkdir(parents=True)
        (self.actuator / "pyproject.toml").write_text(
            '[project]\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        (self.orchestrator / "pyproject.toml").write_text(
            '[project]\nversion = "0.6.3"\n',
            encoding="utf-8",
        )
        self.bootstrap = AccountBootstrap(self.actuator)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_export_creates_portable_non_secret_pack(self) -> None:
        result = self.bootstrap.export()
        self.assertTrue(result["exists"])
        self.assertEqual(result["portable_file_count"], 5)
        self.assertFalse(result["secrets_included"])

        profile = json.loads(self.bootstrap.profile_path.read_text(encoding="utf-8"))
        self.assertEqual(profile["services"]["actuator"]["version"], "1.0.0")
        self.assertEqual(profile["services"]["orchestrator"]["version"], "0.6.3")
        self.assertFalse(profile["secrets_policy"]["copy_cookies"])
        self.assertFalse(profile["secrets_policy"]["copy_oauth_tokens"])
        self.assertFalse(profile["secrets_policy"]["copy_api_keys"])

    def test_prepare_destination_reports_account_bound_authorizations(self) -> None:
        result = self.bootstrap.prepare_destination()
        self.assertTrue(result["local_profile_ready"])
        names = {item["name"] for item in result["manual_authorizations"]}
        self.assertIn("ChatGPT-Actuator", names)
        self.assertIn("chatgpt-orchestrator", names)
        self.assertGreaterEqual(len(result["bootstrap_text"]), 100)

    def test_existing_plugin_manifest_is_preserved(self) -> None:
        self.bootstrap.profile_root.mkdir(parents=True)
        self.bootstrap.plugins_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "plugins": [
                        {
                            "name": "Custom Plugin",
                            "account_bound": True,
                            "authorization": "required",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.bootstrap.export()
        saved = json.loads(self.bootstrap.plugins_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["plugins"][0]["name"], "Custom Plugin")


if __name__ == "__main__":
    unittest.main()
