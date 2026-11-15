"""Regression coverage for the guarded native source commit/restore lifecycle."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from chatgpt_actuator.tool_policy import Principal, principal_context


_FIXTURE_PATH = Path(__file__).with_name("test_altium_edit_workflow.py")
_SPEC = importlib.util.spec_from_file_location("altium_commit_workflow_fixture", _FIXTURE_PATH)
_FIXTURE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _FIXTURE
_SPEC.loader.exec_module(_FIXTURE)


class AltiumCommitTests(unittest.TestCase):
    def setUp(self):
        self.workflow = _FIXTURE.AltiumEditWorkflowTests()
        self.workflow.setUp()
        self.addCleanup(self.workflow.doCleanups)
        self.addCleanup(self.workflow.tearDown)
        fingerprint = patch.object(self.workflow.adapter, "_adapter_fingerprint", return_value="a" * 64)
        fingerprint.start()
        self.addCleanup(fingerprint.stop)
        original_run = self.workflow.adapter._run

        def with_clean_close(command, *, kind, request_key=None, payload=None):
            if command.startswith("CloseCleanSheet("):
                return {"status": "succeeded", "operation_id": f"{len(self.native_operations) + 1:032x}", "rows": []}
            result = original_run(command, kind=kind, request_key=request_key, payload=payload)
            if result.get("status") == "succeeded":
                result.setdefault("adapter_sha256", self.adapter._adapter_fingerprint())
            return result

        self.workflow.adapter._run = with_clean_close

    def __getattr__(self, name):
        workflow = self.__dict__.get("workflow")
        if workflow is not None and hasattr(workflow, name):
            return getattr(workflow, name)
        raise AttributeError(name)

    def _verified_output(self, *, key="edit-commit", output_name="CommitCopy", new_value="22k"):
        principal_context.set(Principal(owner="alice"))
        snapshot = self.editor.inspect(str(self.project))
        source_rows = self.editor._snapshot(snapshot["snapshot_id"])["rows"]
        current_value = next(row["value"] for row in source_rows if row.get("kind") == "edit_component")
        changes = [{"op": "set_component_value", "reference": "R1", "expected_value": current_value, "value": new_value}]
        output = self.projects / output_name
        preview = self.editor.preview(snapshot["snapshot_id"], changes, str(output))
        applied = self.editor.apply(snapshot["snapshot_id"], changes, str(output), preview["preview_digest"], key)
        self.assertEqual(applied["status"], "succeeded", applied)
        report = __import__("json").loads(Path(applied["report_path"]).read_text(encoding="utf-8"))
        # Keep the source baseline intact until the atomic source replacement happens.
        self._expected_after = report["after"]
        self._output_rows = [row for row in report["after"]
                             if row.get("kind") not in {"project", "document"} and "operation_id" not in row]
        self._original_rows = json.loads(json.dumps(self.workflow.current_rows))
        return snapshot, output, applied, report

    def _commit(self, output, key):
        import os
        real_replace = os.replace

        def install_rows(source, target):
            real_replace(source, target)
            if Path(target).resolve() == self.sheet.resolve():
                self.workflow.current_rows = json.loads(json.dumps(self._output_rows))

        with patch("chatgpt_actuator.tools.eda_altium_commit.os.replace", side_effect=install_rows):
            return self.editor.commit(str(output), key)

    def _restore(self, commit_id, key):
        import os
        real_replace = os.replace

        def restore_rows(source, target):
            real_replace(source, target)
            if Path(target).resolve() == self.sheet.resolve():
                self.workflow.current_rows = json.loads(json.dumps(self._original_rows))

        with patch("chatgpt_actuator.tools.eda_altium_commit.os.replace", side_effect=restore_rows):
            return self.editor.restore(commit_id, key)

    def test_commit_and_restore_backup_exact_source(self):
        original_project = self.project.read_bytes()
        original_sheet = self.sheet.read_bytes()
        _, output, _, _ = self._verified_output()
        original_inspect = self.editor._inspect_rows
        observed = {}

        def capture(project, sheet, key=None, **kwargs):
            result = original_inspect(project, sheet, key, **kwargs)
            if key and key.endswith(":verify"):
                observed["rows"] = self.adapter._rows(result)
            return result

        self.editor._inspect_rows = capture
        committed = self._commit(output, "install-1")
        if committed["status"] != "succeeded":
            self.fail(f"commit mismatch: native={observed.get('rows')!r}; expected={self._expected_after!r}")
        self.assertEqual(committed["status"], "succeeded", committed)
        self.assertNotEqual(self.sheet.read_bytes(), original_sheet)
        self.assertEqual(self.project.read_bytes(), original_project)
        self.assertTrue(Path(committed["backup_record"]).is_file())
        # Native restore readback uses original captured rows.
        restored = self._restore(committed["commit_id"], "restore-1")
        self.assertEqual(restored["status"], "succeeded", restored)
        self.assertEqual(self.sheet.read_bytes(), original_sheet)

    def test_source_hash_change_refuses_commit(self):
        _, output, _, _ = self._verified_output()
        self.sheet.write_bytes(self.sheet.read_bytes() + b"external")
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            self._commit(output, "source-changed")

    def test_report_tamper_blocks_commit(self):
        _, output, applied, _ = self._verified_output()
        report_path = Path(applied["report_path"])
        report_path.write_bytes(report_path.read_bytes() + b" ")
        with self.assertRaises(Exception):
            self._commit(output, "report-tamper")

    def test_foreign_owner_cannot_restore(self):
        _, output, _, _ = self._verified_output()
        committed = self._commit(output, "owner-commit")
        principal_context.set(Principal(owner="bob"))
        with self.assertRaises(PermissionError):
            self.editor.restore(committed["commit_id"], "foreign-restore")

    def test_backup_tamper_blocks_restore(self):
        _, output, _, _ = self._verified_output()
        committed = self._commit(output, "backup-commit")
        import json
        record = json.loads(Path(committed["backup_record"]).read_text(encoding="utf-8"))
        Path(record["sheet_backup"]).write_bytes(b"tampered backup")
        with self.assertRaisesRegex(RuntimeError, "Backup hash mismatch"):
            self.editor.restore(committed["commit_id"], "backup-restore")

    def test_request_key_cannot_be_rebound_to_other_verified_copy(self):
        _, output, _, _ = self._verified_output(key="bind-edit-1", output_name="BindOne")
        self._commit(output, "same-commit-key")
        # A second verified output has a distinct report/output binding.
        _, second, _, _ = self._verified_output(key="bind-edit-2", output_name="BindTwo", new_value="33k")
        with self.assertRaisesRegex(ValueError, "already bound"):
            self._commit(second, "same-commit-key")

    def test_dirty_native_source_guard_prevents_publication(self):
        _, output, _, _ = self._verified_output()
        self.workflow.native_dirty = True
        result = self._commit(output, "dirty-commit")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.sheet.read_bytes(), b"mock schematic source")

    def test_commit_unknown_is_not_replayed_on_retry(self):
        _, output, _, _ = self._verified_output()
        original_inspect = self.editor._inspect_rows
        first = {"pending": True}

        def uncertain(project, sheet, key=None, **kwargs):
            if key and key.endswith(":verify") and first["pending"]:
                first["pending"] = False
                return {"status": "unknown", "operation_id": "a" * 32}
            return original_inspect(project, sheet, key, **kwargs)

        with patch.object(self.editor, "_inspect_rows", side_effect=uncertain):
            result = self._commit(output, "unknown-commit")
            self.assertEqual(result["status"], "unknown")
            before = len(self.native_operations)
            retry = self._commit(output, "unknown-commit")
        self.assertEqual(retry["status"], "unknown")
        self.assertEqual(len(self.native_operations), before)

    def test_interrupted_atomic_replacement_reconciles_without_replay(self):
        _, output, _, _ = self._verified_output()
        import os
        real_replace = os.replace
        interrupted = {"pending": True}

        def replace_then_interrupt(source, target):
            real_replace(source, target)
            if Path(target).resolve() == self.sheet.resolve():
                self.workflow.current_rows = json.loads(json.dumps(self._output_rows))
            if interrupted["pending"]:
                interrupted["pending"] = False
                raise RuntimeError("simulated interruption after atomic replace")

        with patch("chatgpt_actuator.tools.eda_altium_commit.os.replace", side_effect=replace_then_interrupt):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                self.editor.commit(str(output), "interrupt-after-replace")
        before = len(self.native_operations)
        result = self._commit(output, "interrupt-after-replace")
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len([op for op in self.native_operations[before:] if "CloseCleanSheet" in op[1]]), 0)

    def test_interruption_before_commit_replace_is_marked_not_applied(self):
        _, output, _, _ = self._verified_output()
        source_before = self.sheet.read_bytes()
        import os
        real_replace = os.replace
        interrupted = {"pending": True}

        def interrupt_before_sheet_replace(source, target):
            if Path(target).resolve() == self.sheet.resolve() and interrupted["pending"]:
                interrupted["pending"] = False
                raise RuntimeError("simulated interruption before commit replacement")
            return real_replace(source, target)

        with patch("chatgpt_actuator.tools.eda_altium_commit.os.replace", side_effect=interrupt_before_sheet_replace):
            with self.assertRaisesRegex(RuntimeError, "before commit replacement"):
                self.editor.commit(str(output), "commit-before-replace")
        self.assertEqual(self.sheet.read_bytes(), source_before)
        calls_before_retry = len(self.native_operations)
        retry = self.editor.commit(str(output), "commit-before-replace")
        self.assertEqual(retry["status"], "not_applied", retry)
        self.assertEqual(self.sheet.read_bytes(), source_before)
        self.assertEqual(len(self.native_operations), calls_before_retry)

    def test_interruption_before_restore_replace_is_marked_not_applied(self):
        _, output, _, _ = self._verified_output()
        committed = self._commit(output, "restore-window-commit")
        committed_source = self.sheet.read_bytes()
        import os
        real_replace = os.replace
        interrupted = {"pending": True}

        def interrupt_before_sheet_restore(source, target):
            if Path(target).resolve() == self.sheet.resolve() and interrupted["pending"]:
                interrupted["pending"] = False
                raise RuntimeError("simulated interruption before restore replacement")
            return real_replace(source, target)

        with patch("chatgpt_actuator.tools.eda_altium_commit.os.replace", side_effect=interrupt_before_sheet_restore):
            with self.assertRaisesRegex(RuntimeError, "before restore replacement"):
                self.editor.restore(committed["commit_id"], "restore-before-replace")
        self.assertEqual(self.sheet.read_bytes(), committed_source)
        calls_before_retry = len(self.native_operations)
        retry = self.editor.restore(committed["commit_id"], "restore-before-replace")
        self.assertEqual(retry["status"], "not_applied", retry)
        self.assertEqual(self.sheet.read_bytes(), committed_source)
        self.assertEqual(len(self.native_operations), calls_before_retry)


if __name__ == "__main__":
    unittest.main()
