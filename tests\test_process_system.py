import os
import sys
import time
import unittest

import psutil

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import ProcessConfig
from chatgpt_actuator.tools import ProcessSystemService


class ProcessSystemTests(unittest.TestCase):
    def make_service(self) -> ProcessSystemService:
        cfg = ProcessConfig(
            enabled=True,
            allow_start=True,
            allow_stop=True,
            allow_force_kill=True,
            protect_system_processes=True,
            max_list_items=200,
            stop_timeout_seconds=2.0,
        )
        return ProcessSystemService(cfg, AuditLogger(False, "unused.log"))

    def test_system_info_and_snapshot(self):
        service = self.make_service()
        info = service.system_info()
        self.assertIn("hostname", info)
        self.assertGreaterEqual(info["cpu_logical_cores"], 1)

        snap = service.resource_snapshot()
        self.assertIn("cpu_percent", snap)
        self.assertIn("memory", snap)
        self.assertIn("disks", snap)

    def test_process_list_and_details(self):
        service = self.make_service()
        listed = service.list_processes(name_filter="python", max_items=50)
        self.assertIn("processes", listed)

        details = service.process_details(os.getpid())
        self.assertEqual(details["pid"], os.getpid())
        self.assertTrue(details["name"])

    def test_protects_own_process(self):
        service = self.make_service()
        with self.assertRaises(PermissionError):
            service.stop_process(os.getpid())

    def test_start_and_stop_safe_child(self):
        service = self.make_service()
        started = service.start_process(
            executable=sys.executable,
            arguments=["-c", "import time; time.sleep(60)"],
        )
        pid = started["pid"]
        self.assertTrue(psutil.pid_exists(pid))

        details = service.process_details(pid)
        self.assertEqual(details["pid"], pid)

        stopped = service.stop_process(pid, force=False, include_children=False)
        self.assertIn(pid, stopped["stop_requested_for"])

        deadline = time.time() + 4
        while time.time() < deadline and psutil.pid_exists(pid):
            time.sleep(0.05)

        self.assertFalse(psutil.pid_exists(pid))

    def test_stop_already_exited_process_is_clean(self):
        service = self.make_service()
        started = service.start_process(
            executable=sys.executable,
            arguments=["-c", "pass"],
        )
        pid = started["pid"]

        deadline = time.time() + 3
        while time.time() < deadline and psutil.pid_exists(pid):
            time.sleep(0.05)

        result = service.stop_process(pid, force=False, include_children=False)
        self.assertEqual(result["stop_requested_for"], [])
        self.assertEqual(result["already_missing"], [pid])
        self.assertEqual(result["timed_out"], [])


if __name__ == "__main__":
    unittest.main()
