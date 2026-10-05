from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import EDAConfig
from chatgpt_actuator.tools.eda import EDAService


class EDAServiceTests(unittest.TestCase):
    def make_service(self, root: Path, **overrides) -> EDAService:
        values = {
            "enabled": True,
            "allow_read": True,
            "allow_edit": False,
            "allow_manufacturing": False,
            "allowed_project_roots": (str(root),),
            "kicad_cli": "",
            "altium_executable": "",
            "default_timeout_seconds": 10.0,
            "max_timeout_seconds": 60.0,
            "max_output_bytes": 100000,
        }
        values.update(overrides)
        cfg = EDAConfig(**values)
        return EDAService(cfg, AuditLogger(False, str(root / "audit.log")))

    def test_project_inspect_classifies_kicad_and_altium(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo.kicad_pro").write_text("{}", encoding="utf-8")
            (root / "demo.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
            (root / "board.PcbDoc").write_text("x", encoding="utf-8")
            (root / "ignore.txt").write_text("x", encoding="utf-8")
            service = self.make_service(root)
            result = service.project_inspect(str(root))
            self.assertEqual({"kicad", "altium"}, set(result["detected_backends"]))
            self.assertEqual(3, len(result["files"]))

    def test_kicad_drc_fails_closed_when_cli_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pcb = root / "demo.kicad_pcb"
            pcb.write_text("(kicad_pcb)", encoding="utf-8")
            service = self.make_service(root)
            with patch.object(service, "_discover_kicad_cli", return_value=None):
                with self.assertRaises(FileNotFoundError):
                    service.kicad_drc(str(pcb))

    def test_manufacturing_requires_explicit_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pcb = root / "demo.kicad_pcb"
            pcb.write_text("(kicad_pcb)", encoding="utf-8")
            service = self.make_service(root, allow_manufacturing=False)
            with self.assertRaises(PermissionError):
                service.kicad_export_gerbers(str(pcb), str(root / "gerbers"))

    def test_gerber_export_refuses_nonempty_directory_before_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pcb = root / "demo.kicad_pcb"
            pcb.write_text("(kicad_pcb)", encoding="utf-8")
            out = root / "gerbers"
            out.mkdir()
            (out / "old.gbr").write_text("old", encoding="utf-8")
            service = self.make_service(root, allow_manufacturing=True)
            with self.assertRaises(FileExistsError):
                service.kicad_export_gerbers(str(pcb), str(out))


if __name__ == "__main__":
    unittest.main()
