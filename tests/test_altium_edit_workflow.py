import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import multiprocessing
from unittest.mock import patch

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.security import PathGuard
from chatgpt_actuator.tools.eda_altium import AltiumAdapter
from chatgpt_actuator.tools.eda_altium_edit import ExistingSheetEditor
import chatgpt_actuator.tools.eda_altium_edit as edit_module
from chatgpt_actuator.atomic_files import file_lock
from chatgpt_actuator.tool_policy import Principal, principal_context, required_scopes

RULE_NUMBERS = tuple(i for i in range(1, 166) if i not in {120, 121})


def rows(value="10k", net="DATA"):
    return [
        {"kind": "edit_sheet", "width": 12000, "height": 9000, "grid": 1},
        {"kind": "edit_sheet_parameter", "name": "Title", "text": "Sensor", "x": 10, "y": 20, "rotation": 0, "hidden": False},
        {"kind": "edit_component", "reference": "R1", "symbol": "R", "part": 1,
         "part_count": 1, "value": value, "uid": "component-uid", "x": 100, "y": 200,
         "rotation": 0, "mirrored": False},
        {"kind": "edit_label", "name": net, "x": 300, "y": 400, "rotation": 0,
         "justification": 0, "font_id": 1},
        {"kind": "edit_wire", "vertices": [[100, 100], [300, 100], [300, 400]], "line_width": 1, "color": 0},
        {"kind": "edit_junction", "x": 300, "y": 100},
        {"kind": "net", "name": net},
        {"kind": "net_pin", "net": net, "reference": "R1", "pin": "1"},
        {"kind": "compiled", "nets": 1},
        {"kind": "edit_pin", "reference": "R1", "pin": "1", "name": "A", "electrical_type": "passive", "x": 300, "y": 400, "part": 1, "uid": "pin-uid"},
        {"kind": "capture_summary", "complete": True, "pins": 1, "components": 1, "labels": 1, "wires": 1, "junctions": 1, "powers": 0, "noerc": 0, "unconnected_pins": 0},
        *[{"kind": "erc_error_setting", "code": f"Type{i}", "project_type": f"Type{i}",
           "project_level": 1, "severity": "warning", "mapping_verified": False} for i in RULE_NUMBERS],
        {"kind": "erc_summary", "complete": True, "configuration_complete": True, "error_reporting_complete": True,
         "error_reporting_setting_count": len(RULE_NUMBERS),
         "effective_error_reporting": {f"Type{i}": 1 for i in RULE_NUMBERS}, "violation_count": 0,
         "report_suppressed": False, "config": {"fixture": "explicit native settings fixture"}},
        {"kind": "compile_summary", "success": True, "complete": True},
    ]


class DeclaredIntentEditor(ExistingSheetEditor):
    """This fixture explicitly has no bypass/pull-up requirements."""
    def preview(self, *args, **kwargs):
        kwargs.setdefault("intent", {"decoupling": [], "pullups": []})
        return super().preview(*args, **kwargs)

    def apply(self, *args, **kwargs):
        kwargs.setdefault("intent", {"decoupling": [], "pullups": []})
        return super().apply(*args, **kwargs)


def _cross_process_publish(destination, stage, start, results):
    start.wait(10)
    with file_lock(Path(destination)):
        if Path(destination).exists():
            results.put("exists")
            return
        Path(stage).mkdir()
        (Path(stage) / "owner.txt").write_text(Path(stage).name, encoding="utf-8")
        import os
        os.rename(stage, destination)
        results.put("published")


class AltiumEditWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.projects = root / "projects"
        self.projects.mkdir()
        self.state = root / "private-state"
        self.project_dir = self.projects / "Board"
        self.project_dir.mkdir()
        self.project = self.project_dir / "Board.PrjPcb"
        self.sheet = self.project_dir / "Board.SchDoc"
        matrix = "\n".join("L" + str(index) + "=" + "N" * 17 for index in range(1, 18))
        types = "\n".join(f"Type{i}=1" for i in RULE_NUMBERS)
        self.project.write_text("[Design]\nVersion=1.0\nReportSuppressedErrorsInMessages=0\n[Document1]\nDocumentPath=Board.SchDoc\n[Electrical Rules Check]\n" + types + "\n[ERC Connection Matrix]\n" + matrix + "\n", encoding="utf-8")
        self.sheet.write_bytes(b"mock schematic source")
        config = SimpleNamespace(enabled=True, allow_read=True, allow_edit=True,
                                 altium_library_roots=(), allowed_project_roots=(str(self.projects),),
                                 max_output_bytes=1024 * 1024, default_timeout_seconds=1, max_timeout_seconds=1)
        audit_path = root / "audit.jsonl"
        audit_path.write_text("", encoding="utf-8")
        audit = AuditLogger(False, str(audit_path))
        adapter = AltiumAdapter(config, audit, PathGuard([self.projects]), lambda: None,
                                state_dir=self.state)
        self.adapter = adapter
        self.editor = DeclaredIntentEditor(adapter)
        self.current_rows = rows()
        self.native_operations = []
        self.native_results = {}
        self.native_dirty = False
        self.staged_rows = {}

        def native(command, *, kind, request_key=None, payload=None):
            if request_key in self.native_results:
                return self.native_results[request_key]
            operation_id = f"{len(self.native_operations) + 1:032x}"
            self.native_operations.append((kind, command, request_key))
            if command.startswith("PrepareExisting"):
                if self.native_dirty:
                    return {"status": "failed", "operation_id": operation_id,
                            "message": "document has unsaved changes"}
                import re
                match = re.search(r"PrepareExisting\('((?:''|[^'])*)',\s*'((?:''|[^'])*)'\)", command)
                project_path, sheet_path = [value.replace("''", "'") for value in match.groups()]
                result_rows = [{"kind": "project_configuration_reload", "complete": True,
                                "previously_open": True, "project_path": project_path, "sheet_path": sheet_path}]
            elif command.startswith("PrepareProject("):
                result_rows = [{"kind": "prepared_project"}]
            elif command.startswith("InspectExisting"):
                if self.native_dirty:
                    return {"status": "failed", "operation_id": operation_id,
                            "message": "project has unsaved changes before inspection"}
                import re
                sheet_path = Path(re.search(r"InspectExisting\('([^']+)',\s*'([^']+)'\)", command).group(2))
                if sheet_path.resolve() == self.sheet.resolve():
                    detail_rows = json.loads(json.dumps(self.current_rows))
                else:
                    detail_rows = self.staged_rows.setdefault(str(sheet_path), json.loads(json.dumps(self.current_rows)))
                count = len([r for r in detail_rows if r.get("kind") == "edit_component"])
                project_path = Path(re.search(r"InspectExisting\('([^']+)',\s*'([^']+)'\)", command).group(1))
                net_count = len([r for r in detail_rows if r.get("kind") == "net"])
                compiled = next((r for r in detail_rows if r.get("kind") == "compiled"), None)
                if compiled is not None:
                    compiled["nets"] = net_count
                capture = next(r for r in detail_rows if r.get("kind") == "capture_summary")
                for field, kind_name in (("pins", "edit_pin"), ("components", "edit_component"), ("labels", "edit_label"), ("wires", "edit_wire"), ("junctions", "edit_junction"), ("powers", "edit_power"), ("noerc", "edit_noerc"), ("unconnected_pins", "unconnected_pin")):
                    capture[field] = sum(r.get("kind") == kind_name for r in detail_rows)
                result_rows = [{"kind": "project", "documents": 1, "path": str(project_path)},
                               {"kind": "document", "path": str(sheet_path), "components": count, "nets": net_count},
                               *detail_rows]
            elif command.startswith("NormalizeOwnedSheet("):
                # The native operation is part of the owned stage workflow;
                # the fake records dispatch without changing the source file.
                result_rows = [{"kind": "normalized_owned_sheet"}]
            elif "SetComponentValue(" in command:
                import re
                match = re.search(r"SetComponentValue\('((?:''|[^'])*)',\s*'((?:''|[^'])*)',\s*'((?:''|[^'])*)'\)", command)
                self.assertIsNotNone(match)
                ref, _old, new = [value.replace("''", "'") for value in match.groups()]
                sheet_path = Path(re.search(r"OpenSheet\('([^']+)'\)", command).group(1))
                sheet_rows = self.current_rows if sheet_path.resolve() == self.sheet.resolve() else self.staged_rows.setdefault(str(sheet_path), json.loads(json.dumps(self.current_rows)))
                next(r for r in sheet_rows if r["kind"] == "edit_component" and r["reference"] == ref)["value"] = new
                sheet_path.write_bytes(sheet_path.read_bytes() + b" edited")
                result_rows = [{"kind": "saved", "path": str(sheet_path)}]
            elif "RenameNetLabels(" in command:
                import re
                match = re.search(r"RenameNetLabels\('((?:''|[^'])*)',\s*'((?:''|[^'])*)'\)", command)
                old, new = [value.replace("''", "'") for value in match.groups()]
                sheet_path = Path(re.search(r"OpenSheet\('([^']+)'\)", command).group(1))
                sheet_rows = self.current_rows if sheet_path.resolve() == self.sheet.resolve() else self.staged_rows.setdefault(str(sheet_path), json.loads(json.dumps(self.current_rows)))
                for row in sheet_rows:
                    if row.get("kind") == "edit_label" and row["name"].casefold() == old.casefold():
                        row["name"] = new
                    if row.get("kind") == "net" and row["name"].casefold() == old.casefold():
                        row["name"] = new
                    if row.get("kind") == "net_pin" and row["net"].casefold() == old.casefold():
                        row["net"] = new
                sheet_path = Path(re.search(r"OpenSheet\('([^']+)'\)", command).group(1))
                sheet_path.write_bytes(sheet_path.read_bytes() + b" renamed")
                result_rows = [{"kind": "saved", "path": str(sheet_path)}]
            else:
                raise AssertionError(command)
            result = {"status": "succeeded", "operation_id": operation_id,
                      "adapter_sha256": self.adapter._adapter_fingerprint(),
                      "rows": [{"operation_id": operation_id}, *result_rows]}
            if request_key:
                self.native_results[request_key] = result
                digest = hashlib.sha256((self.adapter._owner() + "\0" + kind + "\0" + request_key).encode()).hexdigest()
                request_path = self.adapter.state_root / "requests" / (digest + ".json")
                request_path.parent.mkdir(parents=True, exist_ok=True)
                request_path.write_text(json.dumps({"operation_id": operation_id, "digest": "fake"}), encoding="utf-8")
            return result

        adapter._run = native
        self.addCleanup(self.temp.cleanup)

    def tearDown(self):
        principal_context.set(None)

    def test_tool_scope_contract(self):
        self.assertEqual(required_scopes("eda_altium_sch_inspect"), ("mcp:read", "mcp:execute"))
        self.assertEqual(required_scopes("eda_altium_sch_edit_preview"), ("mcp:read", "mcp:write"))
        self.assertEqual(required_scopes("eda_altium_sch_edit_apply"), ("mcp:read", "mcp:write", "mcp:execute"))
        gateway_policy = Path(__file__).resolve().parents[1] / "integrations" / "tailscale_gateway" / "actuator_tool_policy.py"
        backend_policy = Path(__file__).resolve().parents[1] / "src" / "chatgpt_actuator" / "tool_policy.py"
        self.assertEqual(gateway_policy.read_bytes(), backend_policy.read_bytes())

    def test_project_definition_accepts_native_utf16_and_utf8_bom(self):
        original = self.project.read_text(encoding="utf-8")
        for encoding in ("utf-16", "utf-8-sig"):
            with self.subTest(encoding=encoding):
                self.project.write_text(original, encoding=encoding)
                project, sheet = self.editor._project(str(self.project))
                self.assertEqual((project, sheet), (self.project, self.sheet))

    def test_reload_provenance_requires_current_native_receipts(self):
        native = self.adapter._run
        fingerprint = self.adapter._adapter_fingerprint()
        wrong_prepare_fingerprint = {"enabled": False}

        def reload_native(command, *, kind, request_key=None, payload=None):
            result = native(command, kind=kind, request_key=request_key, payload=payload)
            result["adapter_sha256"] = fingerprint
            if kind == "existing_prepare":
                if wrong_prepare_fingerprint["enabled"]:
                    result["adapter_sha256"] = "0" * 64
            elif kind == "existing_inspect":
                next(row for row in result["rows"] if row.get("kind") == "erc_summary").pop("effective_error_reporting")
            return result

        with patch.object(self.adapter, "_run", side_effect=reload_native):
            result = self.editor._inspect_rows(self.project, self.sheet)
            summary = next(row for row in result["rows"] if row.get("kind") == "erc_summary")
            self.assertTrue(summary["configuration_complete"])
            self.assertEqual("saved_project_natively_reloaded", summary["config"]["method"])
            proof = result["configuration_reload_evidence"]
            self.assertEqual(hashlib.sha256(self.project.read_bytes()).hexdigest(), proof["project_sha256"])
            self.assertEqual(hashlib.sha256(self.sheet.read_bytes()).hexdigest(), proof["sheet_sha256"])
            self.assertNotEqual(proof["prepare_operation_id"], proof["inspection_operation_id"])
            wrong_prepare_fingerprint["enabled"] = True
            result = self.editor._inspect_rows(self.project, self.sheet)
            summary = next(row for row in result["rows"] if row.get("kind") == "erc_summary")
            self.assertFalse(summary["configuration_complete"])
            self.assertIsNone(result["configuration_reload_evidence"])

    def test_changed_source_after_prepare_blocks_inspection(self):
        native = self.adapter._run

        def change_after_prepare(command, *, kind, request_key=None, payload=None):
            result = native(command, kind=kind, request_key=request_key, payload=payload)
            if kind == "existing_prepare":
                self.project.write_bytes(self.project.read_bytes() + b" changed externally")
            return result

        with patch.object(self.adapter, "_run", side_effect=change_after_prepare):
            with self.assertRaises(RuntimeError):
                self.editor._inspect_rows(self.project, self.sheet)
        self.assertFalse(any(kind == "existing_inspect" for kind, _, _ in self.native_operations))

    def test_dirty_options_between_prepare_and_inspect_never_make_snapshot(self):
        native = self.adapter._run
        before = (self.project.read_bytes(), self.sheet.read_bytes())

        def dirty_after_prepare(command, *, kind, request_key=None, payload=None):
            result = native(command, kind=kind, request_key=request_key, payload=payload)
            if kind == "existing_prepare":
                self.native_dirty = True
            return result

        with patch.object(self.adapter, "_run", side_effect=dirty_after_prepare):
            result = self.editor.inspect(str(self.project))
        self.assertEqual("failed", result["status"])
        self.assertNotIn("snapshot_id", result)
        self.assertEqual(before, (self.project.read_bytes(), self.sheet.read_bytes()))

    def test_snapshot_is_private_and_preview_binds_normalized_intent(self):
        principal_context.set(Principal(owner="alice"))
        inspected = self.editor.inspect(str(self.project))
        self.assertEqual(inspected["sheet_parameters"][0]["text"], "Sensor")
        preview = self.editor.preview(inspected["snapshot_id"], [
            {"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"},
            {"op": "rename_net", "old_name": "DATA", "new_name": "DATA_新"},
        ], str(self.project_dir.parent / "Copy"))
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(next(r for r in preview["after"] if r.get("kind") == "edit_component")["value"], "22k")
        self.assertEqual(preview["preview_digest"], self.editor.preview(inspected["snapshot_id"], preview["changes"], preview["output_dir"])["preview_digest"])
        principal_context.set(Principal(owner="bob"))
        with self.assertRaises(PermissionError):
            self.editor.preview(inspected["snapshot_id"], preview["changes"], preview["output_dir"])

    def test_dirty_native_document_blocks_inspect_and_apply_before_staging(self):
        self.native_dirty = True
        dirty_inspection = self.editor.inspect(str(self.project))
        self.assertEqual(dirty_inspection["status"], "failed")
        snapshots = self.state / "edit-snapshots"
        self.assertFalse(snapshots.exists())
        self.assertFalse(any("InspectExisting" in command for _, command, _ in self.native_operations))

        self.native_dirty = False
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"}]
        output = self.projects / "DirtySource"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        operation_count = len(self.native_operations)
        self.native_dirty = True
        result = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "dirty-source")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(output.exists())
        self.assertFalse(list(self.projects.glob(".DirtySource.actuator-stage-*")))
        self.assertFalse(any(kind == "existing_edit" for kind, _, _ in self.native_operations[operation_count:]))
        self.assertFalse(any("InspectExisting" in command for _, command, _ in self.native_operations[operation_count:]))

    def test_retry_uses_fresh_dirty_preflight_and_returns_native_reason(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"}]
        output = self.projects / "PreflightRetry"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        original_resolve = self.adapter.guard.resolve_target
        output_resolutions = {"count": 0}

        def interrupt_before_stage(raw_path):
            resolved = original_resolve(raw_path)
            if Path(raw_path) == output:
                output_resolutions["count"] += 1
                if output_resolutions["count"] == 3:
                    raise RuntimeError("simulated interruption after source preflight")
            return resolved

        with patch.object(self.adapter.guard, "resolve_target", side_effect=interrupt_before_stage):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "preflight-retry")
        prepared_before_retry = len([1 for _, command, _ in self.native_operations if command.startswith("PrepareExisting")])
        self.assertEqual(prepared_before_retry, 2)  # snapshot inspect plus first apply preflight

        self.native_dirty = True
        result = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "preflight-retry")
        self.assertEqual(result["status"], "failed")
        self.assertIn("unsaved changes", result["message"])
        self.assertEqual(len([1 for _, command, _ in self.native_operations if command.startswith("PrepareExisting")]), 3)
        self.assertFalse(any("InspectExisting" in command for _, command, _ in self.native_operations[-1:]))
        self.assertFalse(any(kind == "existing_edit" for kind, _, _ in self.native_operations))
        self.assertFalse(output.exists())
        self.assertFalse(list(self.projects.glob(".PreflightRetry.actuator-stage-*")))

    def test_rejects_stale_values_multipart_and_existing_net_collision(self):
        inspected = self.editor.inspect(str(self.project))
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            self.editor.preview(inspected["snapshot_id"], [{"op": "set_component_value", "reference": "R1", "expected_value": "old", "value": "new"}], str(self.projects / "Copy"))
        next(r for r in self.current_rows if r["kind"] == "edit_component")["part_count"] = 2
        inspected_multi = self.editor.inspect(str(self.project))
        with self.assertRaisesRegex(ValueError, "multipart"):
            self.editor.preview(inspected_multi["snapshot_id"], [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "new"}], str(self.projects / "Copy"))
        next(r for r in self.current_rows if r["kind"] == "edit_component")["part_count"] = 1
        self.current_rows.append({"kind": "edit_label", "name": "OTHER", "x": 0, "y": 0})
        self.current_rows.append({"kind": "net", "name": "OTHER"})
        inspected_collision = self.editor.inspect(str(self.project))
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.editor.preview(inspected_collision["snapshot_id"], [{"op": "rename_net", "old_name": "DATA", "new_name": "OTHER"}], str(self.projects / "Copy"))

    def test_rejects_unknown_fields_controls_and_overlapping_rename_targets(self):
        self.current_rows.extend([
            {"kind": "edit_label", "name": "OTHER", "x": 10, "y": 20, "rotation": 0, "justification": 0, "font_id": 1},
            {"kind": "net", "name": "OTHER"},
        ])
        # Keep native compile count evidence aligned with fixture rows.
        inspected = self.editor.inspect(str(self.project))
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.editor.preview(inspected["snapshot_id"], [{"op": "rename_net", "old_name": "DATA", "new_name": "GOOD", "extra": True}], str(self.projects / "Copy"))
        with self.assertRaisesRegex(ValueError, "control"):
            self.editor.preview(inspected["snapshot_id"], [{"op": "rename_net", "old_name": "DATA", "new_name": "BAD\nNAME"}], str(self.projects / "Copy"))
        with self.assertRaisesRegex(ValueError, "duplicate rename targets"):
            self.editor.preview(inspected["snapshot_id"], [
                {"op": "rename_net", "old_name": "DATA", "new_name": "MERGED"},
                {"op": "rename_net", "old_name": "OTHER", "new_name": "MERGED"},
            ], str(self.projects / "Copy"))

    def test_apply_publishes_verified_copy_and_preserves_source(self):
        source_hashes = {"project": __import__("hashlib").sha256(self.project.read_bytes()).hexdigest(),
                         "sheet": __import__("hashlib").sha256(self.sheet.read_bytes()).hexdigest()}
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"},
                   {"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        output = self.projects / "VerifiedCopy"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        result = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-1")
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["compile_verified"])
        inspections = [command for kind, command, _ in self.native_operations if kind == "existing_inspect"]
        self.assertNotIn("CloseOwnedProject", inspections[0])
        self.assertIn("CloseOwnedProject", inspections[-1])
        self.assertTrue((output / self.project.name).is_file())
        self.assertEqual(source_hashes, {"project": __import__("hashlib").sha256(self.project.read_bytes()).hexdigest(),
                                         "sheet": __import__("hashlib").sha256(self.sheet.read_bytes()).hexdigest()})
        self.assertEqual([op[0] for op in self.native_operations].count("existing_edit"), 2)
        self.assertTrue(Path(result["report_path"]).is_file())
        count = len(self.native_operations)
        repeated = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-1")
        self.assertEqual(repeated, result)
        self.assertEqual(len(self.native_operations), count)
        changed = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "33k"},
                   {"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        with self.assertRaisesRegex(ValueError, "preview_digest"):
            self.editor.apply(inspected["snapshot_id"], changed, str(output), preview["preview_digest"], "request-1")

    def test_native_target_contract_mismatch_returns_durable_failure_without_publication(self):
        before = (self.project.read_bytes(), self.sheet.read_bytes())
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"}]
        output = self.projects / "WrongNativeContract"
        target = [{"component": "R1", "pin": "1", "net": "WRONG"}]
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output), expected_connectivity=target)
        result = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"],
                                   "wrong-native-contract", expected_connectivity=target)
        self.assertEqual(result["status"], "verification_failed", result)
        self.assertFalse(output.exists())
        self.assertTrue(Path(result["report_path"]).is_file())
        report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(report["electrical_checks"]["contracts"]["status"], "failed")
        self.assertEqual((self.project.read_bytes(), self.sheet.read_bytes()), before)

    def test_normalization_runs_after_edits_before_final_inspection(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"}]
        output = self.projects / "NormalizeOrder"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        result = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "normalize-order")
        self.assertEqual(result["status"], "succeeded")
        ordered = [(kind, command) for kind, command, _ in self.native_operations
                   if kind in {"existing_edit", "existing_normalize_prepare", "existing_normalize", "existing_inspect"}]
        edit_index = next(i for i, row in enumerate(ordered) if row[0] == "existing_edit")
        prepare_index = next(i for i, row in enumerate(ordered) if row[0] == "existing_normalize_prepare")
        normalize_index = next(i for i, row in enumerate(ordered) if row[0] == "existing_normalize")
        final_inspect_index = max(i for i, row in enumerate(ordered) if row[0] == "existing_inspect")
        self.assertLess(edit_index, normalize_index)
        self.assertLess(edit_index, prepare_index)
        self.assertLess(prepare_index, normalize_index)
        self.assertLess(normalize_index, final_inspect_index)
        self.assertIn("NormalizeOwnedSheet(", ordered[normalize_index][1])

    def test_unknown_normalization_resumes_without_replay_or_publication(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        output = self.projects / "NormalizeRetry"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        native = self.adapter._run
        first = {"pending": True}

        def uncertain(command, *, kind, request_key=None, payload=None):
            result = native(command, kind=kind, request_key=request_key, payload=payload)
            if kind == "existing_normalize" and first["pending"]:
                first["pending"] = False
                return dict(result, status="unknown")
            return result

        self.adapter._run = uncertain
        pending = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "normalize-retry")
        self.assertEqual(pending["status"], "unknown")
        self.assertFalse(output.exists())
        self.assertFalse(any(row[0] == "existing_inspect" and "actuator-stage" in row[1]
                             for row in self.native_operations[-1:]))
        normalization_calls = [row for row in self.native_operations if row[0] == "existing_normalize"]
        self.assertEqual(len(normalization_calls), 1)
        normalization_key = normalization_calls[0][2]
        self.assertTrue(normalization_key.endswith(":normalize"))
        prepare_calls = [row for row in self.native_operations if row[0] == "existing_normalize_prepare"]
        self.assertEqual(len(prepare_calls), 1)
        prepare_key = prepare_calls[0][2]
        self.assertTrue(prepare_key.endswith(":normalize_prepare"))

        resumed = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "normalize-retry")
        self.assertEqual(resumed["status"], "succeeded")
        self.assertEqual([row for row in self.native_operations if row[0] == "existing_normalize"], normalization_calls)
        self.assertEqual([row for row in self.native_operations if row[0] == "existing_normalize_prepare"], prepare_calls)
        self.assertTrue(output.exists())

    def test_resume_after_native_edits_before_verification(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"}]
        output = self.projects / "RecoverEdit"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        original_inspect = self.editor._inspect_rows
        failed_once = {"value": False}

        def uncertain(project, sheet, key=None, **options):
            if key and key.endswith(":verify") and not failed_once["value"]:
                failed_once["value"] = True
                return {"status": "unknown", "operation_id": "f" * 32}
            return original_inspect(project, sheet, key, **options)

        with patch.object(self.editor, "_inspect_rows", side_effect=uncertain):
            first = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-recover")
        self.assertEqual(first["status"], "unknown")
        edit_dispatches = len([op for op in self.native_operations if op[0] == "existing_edit"])
        second = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-recover")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(len([op for op in self.native_operations if op[0] == "existing_edit"]), edit_dispatches)

    def test_staged_mutation_after_native_inspection_blocks_publication(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        output = self.projects / "MutatedAfterInspect"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        original_inspect = self.editor._inspect_rows

        def mutate_after_inspection(project, sheet, key=None, **options):
            result = original_inspect(project, sheet, key, **options)
            if key and key.endswith(":verify") and result.get("status") == "succeeded":
                Path(sheet).write_bytes(Path(sheet).read_bytes() + b" external mutation")
            return result

        with patch.object(self.editor, "_inspect_rows", side_effect=mutate_after_inspection):
            with self.assertRaisesRegex(RuntimeError, "changed after successful native inspection"):
                self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "mutate-after-inspect")
        self.assertFalse(output.exists())

    def test_mutation_after_verification_report_write_blocks_rename(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        output = self.projects / "MutatedAfterReport"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        original_write = edit_module._write

        def mutate_after_report(path, value):
            original_write(path, value)
            if Path(path).name == "verification.json":
                stage_sheet = Path(path).parent / self.sheet.name
                stage_sheet.write_bytes(stage_sheet.read_bytes() + b" post-report mutation")

        with patch.object(edit_module, "_write", side_effect=mutate_after_report):
            with self.assertRaisesRegex(RuntimeError, "changed after successful native inspection"):
                self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "mutate-after-report")
        self.assertFalse(output.exists())

    def test_unknown_native_delivery_reconciles_without_replaying_edit(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        output = self.projects / "UnknownNative"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        native = self.adapter._run
        uncertain = {"sent": False}

        def one_uncertain_dispatch(command, *, kind, request_key=None, payload=None):
            result = native(command, kind=kind, request_key=request_key, payload=payload)
            if kind == "existing_edit" and not uncertain["sent"]:
                uncertain["sent"] = True
                return {**result, "status": "unknown"}
            return result

        self.adapter._run = one_uncertain_dispatch
        first = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-unknown")
        self.assertEqual(first["status"], "unknown")
        edit_count = len([op for op in self.native_operations if op[0] == "existing_edit"])
        second = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-unknown")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(len([op for op in self.native_operations if op[0] == "existing_edit"]), edit_count)

    def test_recover_after_rename_before_request_receipt(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        output = self.projects / "RecoverPublish"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        result = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-publish")
        request_id = __import__("hashlib").sha256(("local\0request-publish").encode()).hexdigest()
        request_path = self.state / "edit-requests" / (request_id + ".json")
        record = json.loads(request_path.read_text(encoding="utf-8"))
        record.pop("result", None)
        record["status"] = "publishing"
        request_path.write_text(json.dumps(record), encoding="utf-8")
        self.project.write_bytes(self.project.read_bytes() + b" later source edit")
        recovered = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-publish")
        self.assertEqual(recovered["result_hashes"], result["result_hashes"])
        self.assertEqual(recovered["status"], "succeeded")

    def test_completed_report_tampering_and_path_traversal_are_rejected(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        output = self.projects / "TamperReport"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        result = self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-tamper")
        report_path = Path(result["report_path"])
        original_report = json.loads(report_path.read_text(encoding="utf-8"))
        request_id = __import__("hashlib").sha256(("local\0request-tamper").encode()).hexdigest()
        request_path = self.state / "edit-requests" / (request_id + ".json")
        original_record = json.loads(request_path.read_text(encoding="utf-8"))
        report = dict(original_report, compile_verified=False)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "durable publication intent"):
            self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-tamper")
        report = dict(original_report, project_relative="../Board.PrjPcb")
        report_path.write_text(json.dumps(report), encoding="utf-8")
        original_record["publication"]["report_sha256"] = __import__("hashlib").sha256(report_path.read_bytes()).hexdigest()
        request_path.write_text(json.dumps(original_record), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unsafe or unexpected"):
            self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], "request-tamper")

    def test_declared_sheet_can_have_different_name_and_ignore_sibling(self):
        main_sheet = self.project_dir / "Main.SchDoc"
        main_sheet.write_bytes(b"main sheet")
        self.project.write_text("[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Main.SchDoc\n", encoding="utf-8")
        inspected = self.editor.inspect(str(self.project))
        self.assertEqual(inspected["sheet_path"], str(main_sheet.resolve()))

    def test_concurrent_distinct_requests_cannot_publish_to_same_destination(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        output = self.projects / "OneDestination"
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(output))
        barrier = Barrier(2)

        def apply(key):
            barrier.wait()
            try:
                return self.editor.apply(inspected["snapshot_id"], changes, str(output), preview["preview_digest"], key)["status"]
            except FileExistsError:
                return "exists"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(apply, ("parallel-a", "parallel-b")))
        self.assertCountEqual(outcomes, ["succeeded", "exists"])
        self.assertTrue((output / "verification.json").is_file())

    def test_destination_publication_lock_serializes_two_processes(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory(prefix="altium-edit-publish-race-") as directory:
            root = Path(directory).resolve()
            destination = root / "output"
            start = context.Event()
            results = context.Queue()
            stages = [root / "stage-a", root / "stage-b"]
            processes = [context.Process(target=_cross_process_publish,
                                         args=(str(destination), str(stage), start, results)) for stage in stages]
            for process in processes:
                process.start()
            try:
                start.set()
                for process in processes:
                    process.join(10)
                self.assertEqual([process.exitcode for process in processes], [0, 0])
                self.assertCountEqual([results.get(timeout=2), results.get(timeout=2)], ["published", "exists"])
                self.assertTrue((destination / "owner.txt").is_file())
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)
                results.close()
                results.join_thread()

    def test_rejects_changed_source_and_unconfirmed_digest_before_dispatch(self):
        inspected = self.editor.inspect(str(self.project))
        changes = [{"op": "rename_net", "old_name": "DATA", "new_name": "SENSE"}]
        preview = self.editor.preview(inspected["snapshot_id"], changes, str(self.projects / "Copy"))
        self.sheet.write_bytes(self.sheet.read_bytes() + b" external")
        dispatch_count = len(self.native_operations)
        with self.assertRaisesRegex(RuntimeError, "changed since snapshot"):
            self.editor.apply(inspected["snapshot_id"], changes, str(self.projects / "Copy"), preview["preview_digest"], "request-stale")
        self.assertEqual(dispatch_count, len(self.native_operations))


if __name__ == "__main__":
    unittest.main()
