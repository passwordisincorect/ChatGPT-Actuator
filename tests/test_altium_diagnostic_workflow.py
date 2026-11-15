"""Inspect failed compilation without certifying stale connectivity."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

from chatgpt_actuator.tool_policy import Principal, principal_context

_spec = importlib.util.spec_from_file_location("altium_diagnostic_fixture", Path(__file__).with_name("test_altium_edit_workflow.py"))
_fixture = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _fixture
_spec.loader.exec_module(_fixture)


class DiagnosticWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _fixture.AltiumEditWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        principal_context.set(Principal(owner="diagnostic-tester"))
        self.good_rows = json.loads(json.dumps(self.fixture.current_rows))
        rows = [r for r in self.fixture.current_rows if r.get("kind") not in {
            "compiled", "net", "net_pin", "unconnected_pin", "compile_summary", "erc_violation"}]
        rows.append({"kind": "compile_summary", "complete": True, "success": False})
        summary = next(r for r in rows if r.get("kind") == "erc_summary")
        summary.update(complete=False, compile_success=False, connectivity_complete=False,
                       configuration_complete=False, violations_available=False,
                       violation_count=None, report_suppressed=None)
        self.fixture.current_rows = rows
        self.editor = self.fixture.editor
        self.expected = [{"component": "R1", "pin": "1", "net": "DATA"}]

    def inspect(self):
        return self.editor.inspect(str(self.fixture.project))

    def test_inspection_reports_physical_pins_and_erc_with_incomplete_graph(self):
        result = self.inspect()
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["compile_evidence"]["success"])
        self.assertFalse(result["connectivity"]["complete"])
        self.assertEqual(result["connectivity"]["physical_pin_count"], 1)
        self.assertEqual(result["erc"]["status"], "incomplete")
        self.assertEqual(len(result["pins"]), 1)
        with self.assertRaises(ValueError):
            self.editor.check_pin_contracts(result["snapshot_id"], self.expected)

    def test_geometry_lint_still_finds_off_grid_component(self):
        next(r for r in self.fixture.current_rows if r.get("kind") == "edit_sheet")["grid"] = 1000
        result = self.inspect()
        lint = self.editor.lint(result["snapshot_id"], {"decoupling": [], "pullups": []})
        codes = {finding["code"] for finding in lint["findings"]}
        self.assertEqual(lint["status"], "incomplete")
        self.assertTrue({"INCOMPLETE_CONNECTIVITY", "OFF_GRID_COMPONENT"} <= codes)

    def test_repair_requires_complete_target_even_for_value_change(self):
        result = self.inspect()
        change = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"}]
        with self.assertRaisesRegex(ValueError, "complete expected"):
            self.editor.preview(result["snapshot_id"], change, str(self.fixture.projects / "Repair"))
        preview = self.editor.preview(result["snapshot_id"], change, str(self.fixture.projects / "Repair"), expected_connectivity=self.expected)
        self.assertEqual(preview["status"], "preview")

    def test_failed_compile_after_edit_retains_diagnostic_report_without_output(self):
        result = self.inspect()
        change = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"}]
        output = self.fixture.projects / "FailedRepair"
        preview = self.editor.preview(result["snapshot_id"], change, str(output), expected_connectivity=self.expected)
        applied = self.editor.apply(result["snapshot_id"], change, str(output), preview["preview_digest"], "failed-repair", expected_connectivity=self.expected)
        self.assertEqual(applied["status"], "verification_failed")
        self.assertFalse(output.exists())
        report = json.loads(Path(applied["report_path"]).read_text(encoding="utf-8"))
        self.assertFalse(report["electrical_checks"]["connectivity"]["complete"])
        self.assertEqual(report["electrical_checks"]["erc"]["status"], "incomplete")
        self.assertTrue(any(r.get("kind") == "compile_summary" and r["success"] is False for r in report["after"]))
        self.assertEqual(self.fixture.sheet.read_bytes(), b"mock schematic source")

    def test_repaired_compilation_and_full_contract_allow_verified_copy(self):
        original_run = self.fixture.adapter._run
        def compiled_stage(command, **kwargs):
            if command.startswith("InspectExisting(") and "actuator-stage" in command:
                import re
                path = Path(re.search(r"InspectExisting\('([^']+)',\s*'([^']+)'\)", command).group(2))
                good = json.loads(json.dumps(self.good_rows))
                next(r for r in good if r.get("kind") == "edit_component")["value"] = "22k"
                good = [r for r in good if r.get("kind") != "compile_summary"]
                good.append({"kind": "compile_summary", "complete": True, "success": True})
                self.fixture.staged_rows[str(path)] = good
            return original_run(command, **kwargs)
        self.fixture.adapter._run = compiled_stage
        result = self.inspect()
        changes = [{"op": "set_component_value", "reference": "R1", "expected_value": "10k", "value": "22k"}]
        output = self.fixture.projects / "Repaired"
        preview = self.editor.preview(result["snapshot_id"], changes, str(output), expected_connectivity=self.expected)
        applied = self.editor.apply(result["snapshot_id"], changes, str(output), preview["preview_digest"], "repaired", expected_connectivity=self.expected)
        self.assertEqual(applied["status"], "succeeded", applied)
        self.assertTrue(applied["electrical_checks"]["connectivity"]["complete"])
        self.assertEqual(self.fixture.sheet.read_bytes(), b"mock schematic source")


if __name__ == "__main__":
    unittest.main()
