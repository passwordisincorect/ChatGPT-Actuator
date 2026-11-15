"""Integration checks for topology edits against deterministic fake AD evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

from chatgpt_actuator.tool_policy import Principal, principal_context


_FIXTURE_PATH = Path(__file__).with_name("test_altium_edit_workflow.py")
_SPEC = importlib.util.spec_from_file_location("altium_structural_workflow_fixture", _FIXTURE_PATH)
_FIXTURE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _FIXTURE
_SPEC.loader.exec_module(_FIXTURE)


class StructuralWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = _FIXTURE.AltiumEditWorkflowTests()
        self.workflow.setUp()
        self.addCleanup(self.workflow.doCleanups)
        self.addCleanup(self.workflow.tearDown)
        principal_context.set(Principal(owner="structural-tester"))
        self.expected_connectivity = [{"component": "R1", "pin": "1", "net": "DATA"}]
        self.intent = {"contracts": list(self.expected_connectivity), "decoupling": [], "pullups": []}
        self.generic_after = None
        self.generic_wrong_net = False
        self._install_generic_dispatch()

    def _install_generic_dispatch(self):
        adapter = self.workflow.adapter
        original = adapter._run

        def dispatch(command, *, kind, request_key=None, payload=None):
            if kind == "existing_edit" and "AddJunction(" in command:
                match = re.search(r"AddJunction\('([^']+)',\s*(-?\d+),\s*(-?\d+)\)", command)
                self.assertIsNotNone(match, command)
                uid, x, y = match.group(1), int(match.group(2)), int(match.group(3))
                sheet = Path(re.search(r"OpenSheet\('([^']+)'\)", command).group(1))
                staged = self.workflow.staged_rows.setdefault(str(sheet), json.loads(json.dumps(self.workflow.current_rows)))
                if self.generic_after is None:
                    staged.append({"kind": "edit_junction", "uid": uid, "x": x * 10000, "y": y * 10000})
                else:
                    staged[:] = json.loads(json.dumps(self.generic_after))
                if self.generic_wrong_net:
                    next(row for row in staged if row.get("kind") == "net_pin")["net"] = "WRONG"
                    staged.append({"kind": "net", "name": "WRONG"})
                sheet.write_bytes(sheet.read_bytes() + b" generic AddJunction")
                operation_id = f"{len(self.workflow.native_operations) + 1:032x}"
                self.workflow.native_operations.append((kind, command, request_key))
                result = {"status": "succeeded", "operation_id": operation_id,
                          "rows": [{"operation_id": operation_id}, {"kind": "saved", "path": str(sheet)}]}
                if request_key:
                    self.workflow.native_results[request_key] = result
                    digest = hashlib.sha256((adapter._owner() + "\0" + kind + "\0" + request_key).encode()).hexdigest()
                    receipt = adapter.state_root / "requests" / (digest + ".json")
                    receipt.parent.mkdir(parents=True, exist_ok=True)
                    receipt.write_text(json.dumps({"operation_id": operation_id, "digest": "fake"}), encoding="utf-8")
                return result
            return original(command, kind=kind, request_key=request_key, payload=payload)

        adapter._run = dispatch

    def __getattr__(self, name):
        workflow = self.__dict__.get("workflow")
        if workflow is not None and hasattr(workflow, name):
            return getattr(workflow, name)
        raise AttributeError(name)

    def _snapshot(self):
        inspected = self.editor.inspect(str(self.project))
        self.assertEqual(inspected["status"], "succeeded")
        return inspected["snapshot_id"]

    def _junction(self):
        return {"op": "add_junction", "uid": "junction-new", "x": 500, "y": 600}

    def _preview(self, output="StructuralCopy", *, changes=None, contracts=None, intent=None):
        snapshot_id = self._snapshot()
        changes = changes or [self._junction()]
        contracts = self.expected_connectivity if contracts is None else contracts
        intent = self.intent if intent is None else intent
        preview = self.editor.preview(snapshot_id, changes, str(self.projects / output),
                                      expected_connectivity=contracts, intent=intent)
        return snapshot_id, changes, contracts, intent, preview

    def test_complete_contracts_required_and_preview_digest_binds_contracts_and_intent(self):
        snapshot_id = self._snapshot()
        changes = [self._junction()]
        with self.assertRaisesRegex(ValueError, "complete expected pin-to-net"):
            self.editor.preview(snapshot_id, changes, str(self.projects / "NoContracts"), intent=self.intent)

        for field in ("expected_connectivity", "intent"):
            with self.subTest(field=field):
                snapshot_id, changes, contracts, intent, preview = self._preview(output="Bound" + field)
                altered_contracts = [{"component": "R1", "pin": "1", "net": "OTHER"}]
                altered_intent = {**intent, "pullups": [{"resistor": {"component": "R1", "pins": ["1", "2"]},
                                                            "signal_net": "DATA", "power_net": "VCC", "value": "4.7k"}]}
                supplied_contracts = altered_contracts if field == "expected_connectivity" else contracts
                supplied_intent = intent if field == "expected_connectivity" else altered_intent
                with self.assertRaisesRegex(ValueError, "preview_digest"):
                    self.editor.apply(snapshot_id, changes, preview["output_dir"], preview["preview_digest"],
                                      "digest-" + field, expected_connectivity=supplied_contracts,
                                      intent=supplied_intent)

    def test_generic_add_junction_applies_and_verifies_independent_golden_rows(self):
        snapshot_id, changes, contracts, intent, preview = self._preview()
        # Fixed native readback fixture: preserved rows are copied from the known
        # baseline and the new object is independently declared at native units.
        baseline = json.loads(json.dumps(self.workflow.current_rows))
        golden_after = baseline + [{"kind": "edit_junction", "uid": "junction-new",
                                    "x": 5_000_000, "y": 6_000_000}]
        self.generic_after = golden_after
        result = self.editor.apply(snapshot_id, changes, preview["output_dir"], preview["preview_digest"],
                                   "structural-add-junction", expected_connectivity=contracts, intent=intent)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertTrue(Path(result["publication"]).is_dir())
        edit_commands = [command for kind, command, _ in self.native_operations if kind == "existing_edit"]
        self.assertTrue(any("AddJunction('junction-new', 500, 600);" in command for command in edit_commands))
        self.assertEqual(result["electrical_checks"]["contracts"]["status"], "passed")
        self.assertEqual(result["electrical_checks"]["erc"]["status"], "passed")
        self.assertEqual(result["electrical_checks"]["lint"]["status"], "passed")
        self.assertEqual(result["after"][-1], {"kind": "edit_junction", "uid": "junction-new",
                                                 "x": 5_000_000, "y": 6_000_000})

    def test_wrong_actual_pin_net_fails_contract_before_publication(self):
        snapshot_id, changes, contracts, intent, preview = self._preview(output="WrongNet")
        self.generic_wrong_net = True
        result = self.editor.apply(snapshot_id, changes, preview["output_dir"], preview["preview_digest"],
                                   "structural-wrong-net", expected_connectivity=contracts, intent=intent)
        self.assertEqual(result["status"], "verification_failed")
        report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(report["electrical_checks"]["contracts"]["status"], "failed")
        self.assertFalse(Path(preview["output_dir"]).exists())

    def test_noerc_requires_exact_nc_reason_binding(self):
        original = self.workflow.current_rows
        self.workflow.current_rows = [row for row in original if row.get("kind") != "net_pin"]
        self.workflow.current_rows.append({"kind": "unconnected_pin", "reference": "R1", "pin": "1"})
        next(row for row in self.workflow.current_rows if row.get("kind") == "capture_summary")["unconnected_pins"] = 1
        snapshot_id = self._snapshot()
        change = {"op": "add_noerc", "uid": "noerc-intent", "component": "R1", "pin": "1",
                  "reason": "intentionally unused", "x": 300, "y": 400}
        no_reason_contract = [{"component": "R1", "pin": "1", "nc": True}]
        with self.assertRaises(ValueError):
            self.editor.preview(snapshot_id, [change], str(self.projects / "NoReason"),
                                expected_connectivity=no_reason_contract,
                                intent={"decoupling": [], "pullups": [], "contracts": no_reason_contract})
        nc_contract = [{"component": "R1", "pin": "1", "nc": True,
                        "reason": "intentionally unused"}]
        changed_reason = [{**nc_contract[0], "reason": "different reason"}]
        with self.assertRaisesRegex(ValueError, "matching intentional NC contract and reason"):
            self.editor.preview(snapshot_id, [change], str(self.projects / "DifferentReason"),
                                expected_connectivity=changed_reason,
                                intent={"decoupling": [], "pullups": [], "contracts": changed_reason})

    def test_malformed_change_fields_and_stale_uid_are_rejected(self):
        snapshot_id = self._snapshot()
        with self.assertRaisesRegex(ValueError, "Unknown edit fields"):
            self.editor.preview(snapshot_id, [{**self._junction(), "unexpected": True}],
                                str(self.projects / "Malformed"), expected_connectivity=self.expected_connectivity,
                                intent=self.intent)
        component = next(row for row in self.editor._snapshot(snapshot_id)["rows"]
                         if row.get("kind") == "edit_component")
        stale = dict(component, x=component["x"] + 10_000)
        with self.assertRaisesRegex(ValueError, "UID/expected properties"):
            self.editor.preview(snapshot_id, [{"op": "remove_object", "uid": component["uid"], "expected": stale}],
                                str(self.projects / "StaleUID"), expected_connectivity=self.expected_connectivity,
                                intent=self.intent)

    def test_incomplete_erc_and_missing_lint_intent_block_publication(self):
        for mode in ("erc", "lint"):
            with self.subTest(mode=mode):
                snapshot_id, changes, contracts, intent, preview = self._preview(output="NoPass" + mode)
                baseline = json.loads(json.dumps(self.workflow.current_rows))
                golden_after = baseline + [{"kind": "edit_junction", "uid": "junction-new",
                                            "x": 5_000_000, "y": 6_000_000}]
                if mode == "erc":
                    next(row for row in golden_after if row.get("kind") == "erc_summary")["complete"] = False
                    supplied_intent = intent
                else:
                    supplied_intent = {"contracts": list(contracts), "decoupling": []}
                    preview = self.editor.preview(snapshot_id, changes, preview["output_dir"],
                                                  expected_connectivity=contracts, intent=supplied_intent)
                self.generic_after = golden_after
                result = self.editor.apply(snapshot_id, changes, preview["output_dir"], preview["preview_digest"],
                                           "structural-no-pass-" + mode, expected_connectivity=contracts,
                                           intent=supplied_intent)
                self.assertEqual(result["status"], "verification_failed")
                if mode == "erc":
                    self.assertEqual(result["electrical_checks"]["erc"]["status"], "incomplete")
                else:
                    self.assertEqual(result["electrical_checks"]["lint"]["status"], "incomplete")
                self.assertFalse(Path(preview["output_dir"]).exists())


if __name__ == "__main__":
    unittest.main()
