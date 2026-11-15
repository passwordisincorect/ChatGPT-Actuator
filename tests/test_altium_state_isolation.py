"""Security regressions for private Altium state and caller-controlled roots."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import EDAConfig, FileSystemConfig
from chatgpt_actuator.errors import AccessDeniedError
from chatgpt_actuator.tool_policy import Principal, principal_context
from chatgpt_actuator.tools.eda import EDAService
from chatgpt_actuator.tools.eda_altium import _write
from chatgpt_actuator.tools.filesystem import FileSystemService


class AltiumStateIsolationTests(unittest.TestCase):
    def make_service(self, base, *, project_root=None, library_root=None, state_dir=None, excluded_roots=()):
        base = Path(base)
        project_root = Path(project_root) if project_root is not None else base / "project"
        library_root = Path(library_root) if library_root is not None else base / "libraries"
        project_root.mkdir(parents=True, exist_ok=True)
        library_root.mkdir(parents=True, exist_ok=True)
        library = library_root / "parts.SchLib"
        if not library.exists():
            library.write_bytes(b"library")
        state_dir = Path(state_dir) if state_dir is not None else base / "private-state"
        config = EDAConfig(True, True, True, False, (str(project_root),), "", "", .02, 1, 200000,
                           altium_library_roots=(str(library_root),))
        audit = AuditLogger(False, str(base / "audit.log"))
        service = EDAService(config, audit, state_dir=state_dir,
                             excluded_roots=excluded_roots or (project_root, library_root))
        return service, project_root, library_root, library, state_dir, audit

    def make_filesystem(self, roots, audit):
        config = FileSystemConfig(True, False, True, True, True, True,
                                  tuple(str(root) for root in roots), 100, 100000, 100)
        return FileSystemService(config, audit)

    def test_generic_filesystem_cannot_read_or_write_private_records_or_caches(self):
        with tempfile.TemporaryDirectory(prefix="actuator-altium-state-") as tmp:
            tmp = str(Path(tmp).resolve())
            service, project, library_root, _, state_dir, audit = self.make_service(tmp)
            api = service.altium
            operation = api._op_path("1" * 32)
            operation_file = operation / "operation.json"
            request_file = api.state_guard.resolve_target(state_dir / "requests" / "request.json")
            cache_file = api.state_guard.resolve_target(state_dir / "libraries" / "catalog.json")
            for path, data in ((operation_file, {"owner": "owner-a"}),
                               (request_file, {"operation_id": "1" * 32}),
                               (cache_file, {"symbols": []})):
                _write(path, data)

            filesystem = self.make_filesystem((project, library_root), audit)
            for path in (operation_file, request_file, cache_file):
                with self.subTest(path=path, action="read"):
                    with self.assertRaises(AccessDeniedError):
                        filesystem.read_text(str(path))
                with self.subTest(path=path, action="write"):
                    with self.assertRaises(AccessDeniedError):
                        filesystem.write_text(str(path), "tampered", overwrite=True, expected_sha256="0" * 64)

    def test_original_project_and_library_guards_cannot_read_private_extracted_sources(self):
        with tempfile.TemporaryDirectory(prefix="actuator-altium-cache-") as tmp:
            tmp = str(Path(tmp).resolve())
            service, _, _, _, state_dir, _ = self.make_service(tmp)
            extracted = service.altium.state_guard.resolve_target(
                state_dir / "libraries" / ("f" * 64) / "parts.SchLib")
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(b"private extracted source")

            with self.assertRaises(AccessDeniedError):
                service.guard.resolve_existing(extracted)
            with self.assertRaises(AccessDeniedError):
                service.altium.library_guard.resolve_existing(extracted)

    def test_private_state_works_under_delegation_but_project_and_library_roots_remain_scoped(self):
        with tempfile.TemporaryDirectory(prefix="actuator-altium-delegation-") as tmp:
            tmp = str(Path(tmp).resolve())
            service, project, _, library, state_dir, _ = self.make_service(tmp)
            workspace = Path(tmp) / "delegated-workspace"
            workspace.mkdir()
            operation_id = "2" * 32
            principal = Principal(owner="delegated-owner", autonomous_workspace=str(workspace))
            token = principal_context.set(principal)
            try:
                operation_path = service.altium._op_path(operation_id) / "operation.json"
                _write(operation_path, {"operation_id": operation_id, "owner": principal.owner,
                                        "kind": "probe", "status": "succeeded"})
                result = service.altium.operation_status(operation_id)
                self.assertEqual(result["status"], "succeeded")
                self.assertTrue(operation_path.is_file())
                self.assertTrue(str(operation_path).startswith(str(state_dir)))

                with self.assertRaises(AccessDeniedError):
                    service.guard.resolve_existing(project)
                with self.assertRaises(AccessDeniedError):
                    service.altium.library_guard.resolve_existing(library)
            finally:
                principal_context.reset(token)

    def test_overlapping_private_state_is_rejected_lazily_without_disabling_info(self):
        cases = (
            "state_below_root",
            "state_above_roots",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix="actuator-altium-overlap-") as tmp:
                base = Path(tmp).resolve()
                if case == "state_below_root":
                    project = base / "project"
                    library_root = base / "library"
                    state_dir = project / "private-state"
                else:
                    state_dir = base / "shared"
                    project = state_dir / "project"
                    library_root = state_dir / "library"
                project.mkdir(parents=True)
                library_root.mkdir(parents=True)
                library = library_root / "parts.SchLib"
                library.write_bytes(b"library")
                audit = AuditLogger(False, str(base / "audit.log"))
                config = EDAConfig(True, True, True, False, (str(project),), "", "", .02, 1, 200000,
                                   altium_library_roots=(str(library_root),))
                service = EDAService(config, audit, state_dir=state_dir,
                                     excluded_roots=(project, library_root))

                with patch.object(service, "_discover_kicad_cli", return_value=Path("kicad-cli.exe")), \
                        patch.object(service, "_discover_altium", return_value=None):
                    info = service.info()
                self.assertTrue(info["backends"]["kicad"]["detected"])
                self.assertFalse(info["backends"]["altium"]["adapter"]["state_available"])

                operation_id = "3" * 32
                operation = state_dir / "operations" / operation_id / "operation.json"
                operation.parent.mkdir(parents=True, exist_ok=True)
                operation.write_text('{"operation_id":"' + operation_id + '","owner":"local","kind":"probe","status":"succeeded"}', encoding="utf-8")
                with self.assertRaisesRegex(PermissionError, "overlaps a configured user-accessible root"):
                    service.altium.operation_status(operation_id)


if __name__ == "__main__":
    unittest.main()
