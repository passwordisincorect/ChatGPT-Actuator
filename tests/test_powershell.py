import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import PowerShellConfig
from chatgpt_actuator.tools import PowerShellService


class PowerShellTests(unittest.TestCase):
    def make_service(
        self,
        root: Path,
        *,
        max_output_bytes: int = 1024 * 1024,
    ) -> PowerShellService:
        cfg = PowerShellConfig(
            enabled=True,
            allow_execute=True,
            executable="powershell.exe",
            default_cwd=str(root),
            allowed_working_roots=(str(root),),
            max_timeout_seconds=60.0,
            default_timeout_seconds=20.0,
            max_output_bytes=max_output_bytes,
            max_script_chars=100000,
        )
        return PowerShellService(cfg, AuditLogger(False, "unused.log"))

    def test_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp))
            info = service.info()
            self.assertEqual(info["exit_code"], 0)
            self.assertIn("PSVersion", info["stdout"], info)
            self.assertNotIn('"PSVersion":null', info["stdout"])
            self.assertEqual(info["stderr"], "")
            self.assertFalse(info["filesystem_sandbox_applies"])

    def test_info_honors_configured_timeout_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp))
            for default, maximum, expected in [(20.0, 60.0, 20.0), (3.0, 5.0, 5.0)]:
                with self.subTest(default=default, maximum=maximum):
                    service.config = replace(service.config, default_timeout_seconds=default,
                                             max_timeout_seconds=maximum)
                    with patch.object(service, "_execute", return_value={
                        "exit_code": 0, "stdout": "fixture", "stderr": ""
                    }) as execute:
                        service.info()
                    self.assertEqual(execute.call_args.kwargs["timeout_seconds"], expected)

    def test_run_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp))
            result = service.run("Write-Output 'hello-v04'")
            self.assertEqual(result["exit_code"], 0)
            self.assertFalse(result["timed_out"], result)
            self.assertIn("hello-v04", result["stdout"])

    def test_nonzero_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp))
            result = service.run("Write-Error 'expected-test-error'; exit 7")
            self.assertEqual(result["exit_code"], 7, result)
            self.assertIn("expected-test-error", result["stderr"])

    def test_cwd_outside_allowed_roots_is_denied(self):
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as outside:
            service = self.make_service(Path(allowed))
            with self.assertRaises(PermissionError):
                service.run("Write-Output 'x'", cwd=outside)

    def test_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp))
            result = service.run("Start-Sleep -Seconds 5", timeout_seconds=0.4)
            self.assertTrue(result["timed_out"])

    def test_output_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp), max_output_bytes=64)
            result = service.run("Write-Output ('x' * 1000)")
            self.assertTrue(result["output_truncated"], result)
            self.assertLessEqual(len(result["stdout"].encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
