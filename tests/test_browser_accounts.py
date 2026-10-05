from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_actuator.browser_accounts import BrowserAccountManager
from chatgpt_actuator.chatgpt_identity import ChatGPTIdentityDetector


class BrowserAccountManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.actuator_root = self.workspace / "ChatGPT-Actuator"
        self.worker_root = self.workspace / "ChatGPT-Worker-Edge"
        (self.actuator_root / "config").mkdir(parents=True)
        self.worker_root.mkdir(parents=True)

        config = {
            "browser_dom": {
                "user_data_dir": str(self.worker_root / "User Data"),
                "edge_executable": str(self.workspace / "msedge.exe"),
                "background_mode": "minimized",
            }
        }
        (self.actuator_root / "config" / "config.json").write_text(
            json.dumps(config),
            encoding="utf-8",
        )
        self.manager = BrowserAccountManager(
            self.actuator_root,
            worker_root=self.worker_root,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_initial_profile_is_registered_and_main_edge_is_out_of_scope(self) -> None:
        status = self.manager.status()
        self.assertEqual(status["active_account_id"], "default")
        self.assertTrue(status["config_matches"])
        self.assertFalse(status["main_edge_touched"])
        self.assertIsNone(status["main_confirmed_account_id"])
        self.assertFalse(status["same_account_confirmed"])
        self.assertEqual(status["password_storage"], "none")
        self.assertEqual(len(status["accounts"]), 1)
        self.assertTrue(status["accounts"][0]["active"])

    def test_create_and_switch_parks_profiles_without_copying(self) -> None:
        active_marker = self.manager.active_dir / "default-marker.txt"
        active_marker.write_text("default", encoding="utf-8")

        created = self.manager.create_account("Account Two")
        account_id = created["created_account_id"]
        target_slot = self.worker_root / "Accounts" / account_id / "User Data"
        (target_slot / "second-marker.txt").write_text("second", encoding="utf-8")

        with (
            patch.object(self.manager, "stop_worker_edge", return_value=[]),
            patch.object(
                self.manager,
                "_launch_edge",
                return_value={"pid": 123, "visible": False},
            ),
        ):
            switched = self.manager.switch_account(account_id, launch_background=True)

        self.assertTrue(switched["changed"])
        self.assertEqual(switched["active_account_id"], account_id)
        self.assertEqual(
            (self.manager.active_dir / "second-marker.txt").read_text(encoding="utf-8"),
            "second",
        )
        parked_default = self.worker_root / "Accounts" / "default" / "User Data"
        self.assertEqual(
            (parked_default / "default-marker.txt").read_text(encoding="utf-8"),
            "default",
        )

        with (
            patch.object(self.manager, "stop_worker_edge", return_value=[]),
            patch.object(
                self.manager,
                "_launch_edge",
                return_value={"pid": 124, "visible": False},
            ),
        ):
            restored = self.manager.switch_account("default", launch_background=True)

        self.assertEqual(restored["active_account_id"], "default")
        self.assertTrue((self.manager.active_dir / "default-marker.txt").is_file())
        parked_second = self.worker_root / "Accounts" / account_id / "User Data"
        self.assertTrue((parked_second / "second-marker.txt").is_file())

    def test_duplicate_labels_get_unique_ids_and_no_credentials_are_recorded(self) -> None:
        first = self.manager.create_account("Nam")
        second = self.manager.create_account("Nam")
        self.assertNotEqual(first["created_account_id"], second["created_account_id"])

        manifest_text = self.manager.manifest_path.read_text(encoding="utf-8").casefold()
        self.assertNotIn("password", manifest_text)
        self.assertNotIn("cookie", manifest_text)
        self.assertNotIn("token", manifest_text)

    def test_main_confirmation_tracks_same_logical_account_without_copying_session(self) -> None:
        confirmed = self.manager.confirm_main_account("default")
        self.assertEqual(confirmed["main_confirmed_account_id"], "default")
        self.assertTrue(confirmed["same_account_confirmed"])
        self.assertFalse(confirmed["main_edge_touched"])

        created = self.manager.create_account("Second")
        second_id = created["created_account_id"]
        confirmed_second = self.manager.confirm_main_account(second_id)
        self.assertEqual(confirmed_second["main_confirmed_account_id"], second_id)
        self.assertFalse(confirmed_second["same_account_confirmed"])

    def test_sync_worker_to_confirmed_main_switches_worker_profile(self) -> None:
        created = self.manager.create_account("Second")
        second_id = created["created_account_id"]
        self.manager.confirm_main_account(second_id)

        with patch.object(
            self.manager,
            "switch_account",
            return_value={
                "active_account_id": second_id,
                "same_account_confirmed": True,
            },
        ) as switch:
            result = self.manager.sync_worker_to_main()

        switch.assert_called_once_with(second_id, launch_background=True)
        self.assertTrue(result["synced_to_main"])

    def test_sync_worker_requires_main_confirmation(self) -> None:
        with self.assertRaises(RuntimeError):
            self.manager.sync_worker_to_main()

    def test_identity_detector_strips_session_secrets(self) -> None:
        result = ChatGPTIdentityDetector._safe_identity(
            {
                "authenticated": True,
                "name": "Test User",
                "email": "test@example.com",
                "http_status": 200,
                "accessToken": "must-not-leak",
                "sessionToken": "must-not-leak",
            },
            state="session_unavailable",
            detected_at="2026-10-05T00:00:00+00:00",
        )
        self.assertEqual(result["name"], "Test User")
        self.assertEqual(result["email"], "test@example.com")
        self.assertNotIn("accessToken", result)
        self.assertNotIn("sessionToken", result)

    def test_detect_worker_persists_nonsecret_identity_for_active_profile(self) -> None:
        identity = {
            "state": "signed_in",
            "authenticated": True,
            "name": "Worker User",
            "email": "worker@example.com",
            "detected_at": "2026-10-05T00:00:00+00:00",
            "detail": None,
        }

        def detect_worker(_path):
            self.manager.identity_detector._cache["worker"] = dict(identity)
            return dict(identity)

        with patch.object(
            self.manager.identity_detector,
            "detect_worker",
            side_effect=detect_worker,
        ):
            result = self.manager.detect_identities("worker")

        self.assertEqual(
            result["identity_detection"]["worker"]["email"],
            "worker@example.com",
        )
        active = next(item for item in result["accounts"] if item["active"])
        self.assertEqual(
            active["last_detected_identity"]["email"],
            "worker@example.com",
        )

    def test_detect_scope_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.detect_identities("everything")

    def test_same_login_detection_compares_email_case_insensitively(self) -> None:
        self.manager.identity_detector._cache["main"] = {
            "state": "signed_in",
            "authenticated": True,
            "name": "Main",
            "email": "Same@Example.com",
            "detected_at": "x",
            "detail": None,
        }
        self.manager.identity_detector._cache["worker"] = {
            "state": "signed_in",
            "authenticated": True,
            "name": "Worker",
            "email": "same@example.com",
            "detected_at": "x",
            "detail": None,
        }
        snapshot = self.manager.identity_detector.snapshot()
        self.assertTrue(snapshot["same_login_detected"])

    def test_sync_worker_prefers_detected_main_email_mapping(self) -> None:
        created = self.manager.create_account("Second")
        second_id = created["created_account_id"]
        manifest = self.manager._load_manifest()
        second = self.manager._account(manifest, second_id)
        second["last_detected_identity"] = {
            "name": "Second",
            "email": "second@example.com",
            "detected_at": "2026-10-05T00:00:00+00:00",
        }
        self.manager._write_json(self.manager.manifest_path, manifest)
        self.manager.identity_detector._cache["main"] = {
            "state": "signed_in",
            "authenticated": True,
            "name": "Second",
            "email": "SECOND@example.com",
            "detected_at": "x",
            "detail": None,
        }

        with patch.object(
            self.manager,
            "switch_account",
            return_value={"active_account_id": second_id},
        ) as switch:
            result = self.manager.sync_worker_to_main()

        switch.assert_called_once_with(second_id, launch_background=True)
        self.assertEqual(result["sync_source"], "detected_email")

    def test_open_login_uses_visible_worker_edge(self) -> None:
        created = self.manager.create_account("Login Test")
        account_id = created["created_account_id"]

        with (
            patch.object(self.manager, "stop_worker_edge", return_value=[]),
            patch.object(
                self.manager,
                "_launch_edge",
                return_value={
                    "pid": 222,
                    "visible": True,
                    "url": "https://chatgpt.com/",
                    "background_mode": "minimized",
                },
            ) as launch,
        ):
            result = self.manager.open_login(account_id)

        launch.assert_called_once_with(visible=True, url="https://chatgpt.com/")
        self.assertEqual(result["active_account_id"], account_id)
        self.assertTrue(result["launch"]["visible"])


if __name__ == "__main__":
    unittest.main()
