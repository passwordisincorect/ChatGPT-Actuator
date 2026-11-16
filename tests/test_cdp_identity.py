from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chatgpt_actuator import cdp_identity as identity
from chatgpt_actuator.automation_errors import BrowserConnectionUnavailable, PolicyDenied


class FakeProcess:
    pid = 4312

    def __init__(self, info, started=1000.0):
        self.info = info
        self.started = started

    def create_time(self):
        return self.started


class CdpIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "Microsoft" / "Edge" / "User Data"
        (self.root / "Default").mkdir(parents=True)
        self.exe = base / "Program Files (x86)" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_text("", encoding="utf-8")
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": str(base), "ProgramFiles(x86)": str(self.exe.parents[3]),
                                           "ProgramFiles": str(base / "Program Files")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.proc = FakeProcess({"name": "msedge.exe", "cmdline": [str(self.exe), "--profile-directory=Default"],
                                 "exe": str(self.exe), "create_time": 1000.0, "username": "DOMAIN\\alice"})
        self.patches = [
            patch.object(identity.psutil, "Process", lambda pid: SimpleNamespace(username=lambda: "DOMAIN\\alice")),
            patch.object(identity.psutil, "process_iter", lambda attrs: iter([self.proc])),
            patch.object(identity, "_local_policy_denied", lambda: False),
            patch.object(identity, "_listeners", lambda port: [(self.proc.pid, "127.0.0.1")]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.active = self.root / "DevToolsActivePort"
        self.write_active("9222\n/devtools/browser/abc-123\n", 1100)
        self.policy = {"enabled": True, "owner": "alice", "user_data_dir": str(self.root), "profile_directory": "Default"}

    def write_active(self, contents, timestamp):
        self.active.write_text(contents, encoding="utf-8")
        os.utime(self.active, (timestamp, timestamp))

    def test_resolve_returns_json_serializable_binding(self):
        import json
        result = identity.resolve(self.policy, "alice")
        self.assertEqual(result, {"owner": "alice", "pid": self.proc.pid, "started": 1000.0,
                                  "exe": os.path.normcase(str(self.exe.resolve())), "user_data_dir": os.path.normcase(str(self.root.resolve())),
                                  "profile_directory": "Default", "endpoint": "ws://127.0.0.1:9222/devtools/browser/abc-123",
                                  "allow_temporary_focus": False})
        json.dumps(result)

    def test_temporary_focus_grant_is_bound_and_preserved_by_refresh(self):
        policy = {**self.policy, "allow_temporary_focus": True}
        binding = identity.resolve(policy, "alice")
        self.assertIs(binding["allow_temporary_focus"], True)
        self.assertIs(identity.refresh(binding)["allow_temporary_focus"], True)

    def test_temporary_focus_defaults_false_and_rejects_non_boolean(self):
        binding = identity.resolve({**self.policy, "allow_temporary_focus": False}, "alice")
        self.assertIs(binding["allow_temporary_focus"], False)
        for value in (None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(PolicyDenied):
                identity.resolve({**self.policy, "allow_temporary_focus": value}, "alice")

    def test_disabled_wrong_owner_and_legacy_flags_are_not_grants(self):
        cases = [({"enabled": False, "owner": "alice"}, "alice"),
                 ({"enabled": True, "owner": "bob"}, "alice"),
                 ({"enabled": True, "owner": "alice", "auto_connect": True}, "alice")]
        for policy, owner in cases:
            with self.subTest(policy=policy), self.assertRaises(PolicyDenied):
                identity.resolve(policy, owner)

    def test_profile_mismatch_and_no_matching_process_are_denied(self):
        self.proc.info["cmdline"] = [self.proc.info["exe"], "--profile-directory=Other"]
        with self.assertRaises(BrowserConnectionUnavailable):
            identity.resolve(self.policy, "alice")

    def test_multiple_matching_processes_are_denied(self):
        duplicate = FakeProcess(dict(self.proc.info), started=1002.0)
        with patch.object(identity.psutil, "process_iter", lambda attrs: iter([self.proc, duplicate])):
            with self.assertRaises(PolicyDenied):
                identity.resolve(self.policy, "alice")

    def test_machine_policy_denial_blocks_discovery(self):
        with patch.object(identity, "_local_policy_denied", lambda: True):
            with self.assertRaises(PolicyDenied):
                identity.resolve(self.policy, "alice")

    def test_pid_reuse_fails_refresh(self):
        binding = identity.resolve(self.policy, "alice")
        self.proc.started = 1001.0
        with self.assertRaises(BrowserConnectionUnavailable):
            identity.refresh(binding)

    def test_malformed_port_data_never_yields_websocket(self):
        for contents in ("", "oops\n", "70000\n", "9222\nnot-a-browser-path\n"):
            self.write_active(contents, 1100)
            with self.subTest(contents=contents):
                self.assertIsNone(identity._endpoint(self.root, 4312, 1000.0))

    def test_symlink_active_port_is_denied(self):
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == self.active or original(path)):
            with self.assertRaises(PolicyDenied):
                identity._endpoint(self.root, self.proc.pid, 1000.0)

    def test_missing_stale_and_unowned_listeners_return_no_endpoint(self):
        self.active.unlink()
        self.assertIsNone(identity._endpoint(self.root, self.proc.pid, 1000.0))
        self.write_active("9222\n", 900)
        self.assertIsNone(identity._endpoint(self.root, self.proc.pid, 1000.0))
        self.write_active("9222\n", 1100)
        with patch.object(identity, "_listeners", lambda port: []):
            self.assertIsNone(identity._endpoint(self.root, self.proc.pid, 1000.0))

    def test_foreign_or_nonloopback_listener_is_denied(self):
        for listener in ([(9999, "127.0.0.1")], [(4312, "0.0.0.0")]):
            with self.subTest(listener=listener), patch.object(identity, "_listeners", lambda port: listener):
                with self.assertRaises(PolicyDenied):
                    identity._endpoint(self.root, self.proc.pid, 1000.0)

    def test_refresh_tracks_endpoint_rotation(self):
        binding = identity.resolve(self.policy, "alice")
        self.write_active("9333\n/devtools/browser/next\n", 1100)
        refreshed = identity.refresh(binding)
        self.assertEqual(refreshed["pid"], binding["pid"])
        self.assertEqual(refreshed["started"], binding["started"])
        self.assertEqual(refreshed["endpoint"], "ws://127.0.0.1:9333/devtools/browser/next")
