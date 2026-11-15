"""Offline regressions for commit recovery and structural topology contracts."""
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from chatgpt_actuator.tool_policy import Principal, principal_context


_WORKFLOW_PATH = __import__("pathlib").Path(__file__).with_name("test_altium_edit_workflow.py")
_WORKFLOW_SPEC = importlib.util.spec_from_file_location("altium_recovery_workflow_fixture", _WORKFLOW_PATH)
_WORKFLOW = importlib.util.module_from_spec(_WORKFLOW_SPEC)
sys.modules[_WORKFLOW_SPEC.name] = _WORKFLOW
_WORKFLOW_SPEC.loader.exec_module(_WORKFLOW)
_COMMIT_PATH = Path(__file__).with_name("test_altium_commit.py")
_COMMIT_SPEC = importlib.util.spec_from_file_location("altium_commit_recovery_fixture", _COMMIT_PATH)
_COMMIT = importlib.util.module_from_spec(_COMMIT_SPEC)
sys.modules[_COMMIT_SPEC.name] = _COMMIT
_COMMIT_SPEC.loader.exec_module(_COMMIT)


class AltiumRecoveryRegressions(unittest.TestCase):
    def setUp(self):
        self.fixture = _WORKFLOW.AltiumEditWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        principal_context.set(Principal(owner="recovery-review"))

    def _run_commit_with_native(self, harness, output, key, intercept=None, replace_count=None):
        import os
        import chatgpt_actuator.tools.eda_altium_commit as commit_module
        real_replace = os.replace
        original_inspect = harness.editor._inspect_rows

        def replace(source, target):
            real_replace(source, target)
            if Path(target).resolve() == harness.sheet.resolve():
                harness.workflow.current_rows = json.loads(json.dumps(harness._output_rows))
                if replace_count is not None:
                    replace_count.append(str(target))

        def inspect(project, sheet, request_key=None, **options):
            result = original_inspect(project, sheet, request_key, **options)
            if intercept is not None:
                return intercept(result, request_key)
            return result

        with patch.object(commit_module.os, "replace", side_effect=replace), \
             patch.object(harness.editor, "_inspect_rows", side_effect=inspect):
            return harness.editor.commit(str(output), key)

    def _commit_harness(self, output_name, edit_key, commit_key):
        harness = _COMMIT.AltiumCommitTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.addCleanup(harness.tearDown)
        _, output, _, _ = harness._verified_output(key=edit_key, output_name=output_name)
        return harness, output, commit_key

    def test_structural_preview_rejects_target_table_omitting_unchanged_source_pin(self):
        """A target table must retain every physical pin not removed by the edit."""
        second_pin = {"kind": "edit_pin", "reference": "R1", "pin": "2", "name": "B",
                      "electrical_type": "passive", "x": 2_000_000, "y": 1_000_000,
                      "part": 1, "uid": "pin-uid-2"}
        self.fixture.current_rows.append(second_pin)
        self.fixture.current_rows.append({"kind": "net_pin", "net": "DATA", "reference": "R1", "pin": "2"})

        inspected = self.fixture.editor.inspect(str(self.fixture.project))
        expected = [{"component": "R1", "pin": "1", "net": "DATA"}]
        change = {"op": "add_junction", "uid": "topology-test", "x": 500, "y": 600}

        with self.assertRaisesRegex(ValueError, "complete target pin-to-net table"):
            self.fixture.editor.preview(
                inspected["snapshot_id"], [change], str(self.fixture.projects / "OmittedPin"),
                expected_connectivity=expected,
                intent={"contracts": expected, "decoupling": [], "pullups": []},
            )

    def test_structural_preview_accepts_exact_table_after_removing_one_component(self):
        self.fixture.current_rows.extend([
            {"kind": "edit_component", "reference": "R2", "symbol": "R", "part": 1,
             "part_count": 1, "value": "1k", "uid": "component-uid-2", "x": 800, "y": 900,
             "rotation": 0, "mirrored": False},
            {"kind": "edit_pin", "reference": "R2", "pin": "1", "name": "A",
             "electrical_type": "passive", "x": 8_000_000, "y": 9_000_000,
             "part": 1, "uid": "pin-uid-2", "component_uid": "component-uid-2"},
            {"kind": "net", "name": "AUX"},
            {"kind": "net_pin", "net": "AUX", "reference": "R2", "pin": "1"},
        ])
        inspected = self.fixture.editor.inspect(str(self.fixture.project))
        original_component = next(row for row in self.fixture.current_rows
                                  if row.get("kind") == "edit_component" and row.get("reference") == "R1")
        expected_after = [{"component": "R2", "pin": "1", "net": "AUX"}]
        preview = self.fixture.editor.preview(
            inspected["snapshot_id"],
            [{"op": "remove_object", "uid": original_component["uid"], "expected": dict(original_component)}],
            str(self.fixture.projects / "RemoveOneComponent"),
            expected_connectivity=expected_after,
            intent={"contracts": expected_after, "decoupling": [], "pullups": []},
        )
        self.assertEqual(preview["status"], "preview")

    def test_structural_target_table_rejects_extra_pin_and_invalid_identity_types(self):
        inspected = self.fixture.editor.inspect(str(self.fixture.project))
        change = {"op": "add_junction", "uid": "target-table-check", "x": 500, "y": 600}
        valid = [{"component": "R1", "pin": "1", "net": "DATA"}]
        cases = [
            ([*valid, {"component": "R9", "pin": "1", "net": "GHOST"}], "complete target pin-to-net table"),
            ([{"component": 7, "pin": "1", "net": "DATA"}], "component and pin"),
            ([{"component": "R1", "pin": None, "net": "DATA"}], "component and pin"),
        ]
        for index, (contracts, message) in enumerate(cases):
            with self.subTest(contracts=contracts), self.assertRaisesRegex(ValueError, message):
                self.fixture.editor.preview(
                    inspected["snapshot_id"], [change], str(self.fixture.projects / f"BadTarget{index}"),
                    expected_connectivity=contracts,
                    intent={"contracts": contracts, "decoupling": [], "pullups": []},
                )

    def test_structural_preview_requires_new_component_full_verified_library_pin_table(self):
        library = self.fixture.project_dir / "Cached.SchLib"
        library.write_bytes(b"cached-only fake symbol source")
        library_hash = hashlib.sha256(library.read_bytes()).hexdigest()
        self.fixture.adapter.state_root.joinpath("libraries").mkdir(parents=True, exist_ok=True)
        catalog = {
            "path": str(library), "sha256": library_hash, "catalog_version": 1,
            "source_hashes": {str(library): library_hash},
            "symbols": [{"name": "DUAL", "parts": 1, "source_path": str(library), "pins": [
                {"number": "1", "name": "A", "part": 1, "x": 0, "y": 0, "length": 0, "orientation": 0},
                {"number": "2", "name": "B", "part": 1, "x": 100, "y": 0, "length": 0, "orientation": 0},
            ]}],
        }
        catalog_path = self.fixture.adapter.state_root / "libraries" / f"{library_hash}.json"
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        inspected = self.fixture.editor.inspect(str(self.fixture.project))
        changes = [{"op": "add_component", "uid": "added-dual", "library_path": str(library),
                    "symbol": "DUAL", "reference": "U1", "value": "DUAL", "x": 1000, "y": 1000,
                    "part": 1, "rotation": 0}]
        incomplete_target = [{"component": "R1", "pin": "1", "net": "DATA"},
                             {"component": "U1", "pin": "1", "net": "VCC"}]
        native_before = len(self.fixture.native_operations)
        with self.assertRaisesRegex(ValueError, "complete target pin-to-net table"):
            self.fixture.editor.preview(
                inspected["snapshot_id"], changes, str(self.fixture.projects / "MissingAddedPin"),
                expected_connectivity=incomplete_target,
                intent={"contracts": incomplete_target, "decoupling": [], "pullups": []},
            )
        self.assertEqual(len(self.fixture.native_operations), native_before,
                         "cached library preview must not dispatch native library inspection")

    def test_structural_preview_accepts_unchanged_complete_target_table(self):
        inspected = self.fixture.editor.inspect(str(self.fixture.project))
        full_target = [{"component": "R1", "pin": "1", "net": "DATA"}]
        preview = self.fixture.editor.preview(
            inspected["snapshot_id"], [{"op": "add_junction", "uid": "full-target", "x": 500, "y": 600}],
            str(self.fixture.projects / "FullTarget"), expected_connectivity=full_target,
            intent={"contracts": full_target, "decoupling": [], "pullups": []},
        )
        self.assertEqual(preview["status"], "preview")

    def test_commit_crash_after_backup_before_record_can_be_retried(self):
        harness = _COMMIT.AltiumCommitTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.addCleanup(harness.tearDown)
        _, output, _, _ = harness._verified_output(key="backup-window-edit", output_name="BackupWindow")
        import chatgpt_actuator.tools.eda_altium_commit as commit_module
        original_write = commit_module._write
        intercepted = {"path": None}

        def interrupt_before_durable_record(path, data):
            if Path(path).name == "record.json" and data.get("status") == "replacing":
                intercepted["path"] = Path(path)
                raise RuntimeError("simulated crash before commit record")
            return original_write(path, data)

        with patch.object(commit_module, "_write", side_effect=interrupt_before_durable_record):
            with self.assertRaisesRegex(RuntimeError, "before commit record"):
                harness.editor.commit(str(output), "backup-window-commit")
        self.assertIsNotNone(intercepted["path"])
        self.assertEqual(json.loads(intercepted["path"].read_text(encoding="utf-8"))["status"], "preparing")
        record_folder = intercepted["path"].parent
        self.assertTrue((record_folder / "before.PrjPcb").is_file())
        self.assertTrue((record_folder / "before.SchDoc").is_file())
        retry = harness._commit(output, "backup-window-commit")
        self.assertEqual(retry["status"], "succeeded", retry)

    def test_unknown_commit_retry_polls_then_verifies_without_second_replacement(self):
        harness, output, commit_key = self._commit_harness("UnknownResume", "unknown-resume-edit", "unknown-resume")
        replace_count = []
        first_unknown = {"pending": True}

        def unknown_once(result, request_key):
            if request_key and request_key.endswith(":verify") and first_unknown["pending"]:
                first_unknown["pending"] = False
                return {"status": "unknown", "operation_id": "b" * 32}
            return result

        first = self._run_commit_with_native(harness, output, commit_key, unknown_once, replace_count)
        self.assertEqual(first["status"], "unknown")
        status_keys = []

        def status(operation_id):
            status_keys.append(operation_id)
            return {"status": "running" if len(status_keys) == 1 else "succeeded", "operation_id": operation_id}

        with patch.object(harness.adapter, "operation_status", side_effect=status):
            pending = self._run_commit_with_native(harness, output, commit_key, replace_count=replace_count)
            self.assertEqual(pending["status"], "unknown", pending)
            complete = self._run_commit_with_native(harness, output, commit_key, replace_count=replace_count)
        self.assertEqual(complete["status"], "succeeded", complete)
        self.assertEqual(status_keys, ["b" * 32, "b" * 32])
        self.assertEqual(len(replace_count), 1, "unknown verification recovery must not replace the source twice")

    def test_fresh_commit_readback_rejects_erc_and_graph_changes(self):
        for mode in ("erc", "graph"):
            with self.subTest(mode=mode):
                harness, output, commit_key = self._commit_harness("Fresh" + mode, "fresh-" + mode, "fresh-" + mode)

                def damage(result, request_key):
                    if not request_key or not request_key.endswith(":verify"):
                        return result
                    rows = result["rows"]
                    if mode == "erc":
                        next(row for row in rows if row.get("kind") == "erc_summary")["violation_count"] = 1
                        rows.append({"kind": "erc_violation", "message": "fresh native error", "severity": "error",
                                     "source": "fixture", "suppressed": False})
                    else:
                        next(row for row in rows if row.get("kind") == "net_pin")["net"] = "WRONG"
                        rows.append({"kind": "net", "name": "WRONG"})
                        next(row for row in rows if row.get("kind") == "compiled")["nets"] = 2
                        next(row for row in rows if row.get("kind") == "document")["nets"] = 2
                    return result

                result = self._run_commit_with_native(harness, output, commit_key, intercept=damage)
                self.assertEqual(result["status"], "verification_failed", result)
                if mode == "erc":
                    self.assertEqual(result["electrical_checks"]["erc"]["status"], "failed")
                else:
                    self.assertTrue(result["electrical_checks"]["connectivity"]["complete"])
                    self.assertEqual(result["electrical_checks"]["connectivity"]["pins"][0]["net"], "WRONG")

    def test_restore_of_diagnostic_compilefailed_baseline_reports_limited_scope(self):
        harness, output, commit_key = self._commit_harness("DiagnosticRestore", "diagnostic-edit", "diagnostic-commit")
        original_bytes = harness.sheet.read_bytes()
        committed = harness._commit(output, commit_key)
        self.assertEqual(committed["status"], "succeeded", committed)
        # The report's snapshot ID is kept in the edit publication record.
        import json
        request_record = next(json.loads(path.read_text(encoding="utf-8")) for path in
                               (harness.adapter.state_root / "edit-requests").glob("*.json")
                               if json.loads(path.read_text(encoding="utf-8")).get("destination") == str(output))
        snapshot_path = harness.adapter.state_root / "edit-snapshots" / (request_record["snapshot_id"] + ".json")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        diagnostic_rows = [row for row in json.loads(json.dumps(snapshot["rows"]))
                           if row.get("kind") not in {"project", "document", "compile_summary", "erc_violation"}
                           and "operation_id" not in row]
        diagnostic_rows.append({"kind": "compile_summary", "complete": True, "success": False,
                                "message": "baseline compile diagnostic"})
        summary = next(row for row in diagnostic_rows if row.get("kind") == "erc_summary")
        summary.update(complete=False, compile_success=False, connectivity_complete=False,
                       configuration_complete=False, violations_available=False,
                       violation_count=None, report_suppressed=None)
        snapshot["rows"] = diagnostic_rows
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        harness._original_rows = json.loads(json.dumps(diagnostic_rows))
        # Simulate the native restore inspection returning the original incomplete baseline.
        original_inspect = harness.editor._inspect_rows

        def diagnostic_native_inspect(project, sheet, key=None, **options):
            if key and key.endswith(":restore:verify"):
                from chatgpt_actuator.tools.eda_altium import _pas
                return harness.adapter._run(
                    f"InspectExisting({_pas(project)}, {_pas(sheet)});", kind="existing_inspect",
                    request_key=key, payload={"project": str(project), "sheet": str(sheet)},
                )
            return original_inspect(project, sheet, key, **options)

        with patch.object(harness.editor, "_inspect_rows", side_effect=diagnostic_native_inspect):
            restore_result = harness._restore(committed["commit_id"], "diagnostic-restore")
        self.assertEqual(restore_result["status"], "succeeded", restore_result)
        self.assertEqual(harness.sheet.read_bytes(), original_bytes)
        self.assertEqual(restore_result["restored_hashes"]["sheet"], hashlib.sha256(original_bytes).hexdigest())
        self.assertFalse(restore_result["connectivity"]["complete"])
        self.assertIn("connectivity checked only when the baseline was compiled",
                      restore_result["verification_scope"])
        self.assertEqual(harness.workflow.current_rows, diagnostic_rows)


if __name__ == "__main__":
    unittest.main()
