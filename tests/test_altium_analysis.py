import unittest

from chatgpt_actuator.tools.eda_altium_analysis import (check_contracts, connectivity, diff, erc, lint,
                                                        validate_physical_capture, _value_number)


def rows(value="4.7kΩ"):
    result = [
        {"kind": "edit_sheet", "width": 20000000, "height": 12000000, "grid": 1000000},
        {"kind": "edit_component", "reference": "R1", "value": value, "x": 2000000, "y": 2000000},
        {"kind": "edit_component", "reference": "C1", "value": "100nF", "x": 3000000, "y": 2000000},
        {"kind": "edit_component", "reference": "U1", "value": "MCU", "x": 4000000, "y": 2000000},
        {"kind": "edit_label", "name": "SDA", "x": 10000000, "y": 2000000},
        {"kind": "edit_power", "name": "GND", "x": 10000000, "y": 5000000},
        {"kind": "edit_wire", "vertices": [[0, 2000000], [10000000, 2000000]]},
        {"kind": "edit_wire", "vertices": [[0, 5000000], [10000000, 5000000]]},
        {"kind": "net", "name": "SDA"},
        {"kind": "net", "name": "VCC"},
        {"kind": "net", "name": "GND"},
        {"kind": "net_pin", "net": "SDA", "reference": "R1", "pin": "1"},
        {"kind": "net_pin", "net": "VCC", "reference": "R1", "pin": "2"},
        {"kind": "net_pin", "net": "VCC", "reference": "C1", "pin": "1"},
        {"kind": "net_pin", "net": "GND", "reference": "C1", "pin": "2"},
        {"kind": "net_pin", "net": "SDA", "reference": "U1", "pin": "1"},
        {"kind": "net_pin", "net": "VCC", "reference": "U1", "pin": "2"},
        {"kind": "unconnected_pin", "reference": "U1", "pin": "3"},
        {"kind": "compiled", "nets": 3},
        {"kind": "capture_summary", "complete": True, "pins": 7, "unconnected_pins": 1,
         "components": 3, "labels": 1, "wires": 2, "junctions": 0, "powers": 1, "noerc": 0},
    ]
    for ref, pin, x, y in (("R1", "1", 0, 2000000), ("R1", "2", 0, 2000000),
                            ("C1", "1", 10000000, 2000000), ("C1", "2", 10000000, 5000000),
                            ("U1", "1", 10000000, 2000000), ("U1", "2", 10000000, 5000000),
                            ("U1", "3", 12000000, 8000000)):
        result.append({"kind": "edit_pin", "reference": ref, "pin": pin, "name": "P" + pin,
                       "electrical_type": "Passive", "x": x, "y": y, "uid": ref + pin, "part": 1})
    return result


def intent():
    return {"decoupling": [{"power": {"component": "U1", "pin": "2"}, "power_net": "VCC",
                            "ground_net": "GND", "capacitor": {"component": "C1", "pins": ["1", "2"]},
                            "min_value": 9e-8, "max_value": 1.1e-7}],
            "pullups": [{"signal_net": "SDA", "power_net": "VCC",
                         "resistor": {"component": "R1", "pins": ["1", "2"]},
                         "min_value": 4e3, "max_value": 5e3}]}


def refresh_summary(source):
    summary = next(r for r in source if r.get("kind") == "capture_summary")
    for key, kind in (("pins", "edit_pin"), ("components", "edit_component"), ("labels", "edit_label"),
                      ("wires", "edit_wire"), ("junctions", "edit_junction"), ("powers", "edit_power"),
                      ("noerc", "edit_noerc"), ("unconnected_pins", "unconnected_pin")):
        summary[key] = sum(r.get("kind") == kind for r in source)


class AltiumAnalysisTests(unittest.TestCase):
    def test_snapshot_requires_native_summary_and_explicit_nc(self):
        source = rows()
        snapshot = connectivity(source, source_hashes={"project": "a" * 64, "sheet": "b" * 64})
        self.assertEqual(7, snapshot["count"])
        self.assertEqual("", next(p["net"] for p in snapshot["pins"] if p["pin"] == "3"))
        self.assertEqual(64, len(snapshot["sha256"]))
        incomplete = [row for row in source if row.get("kind") != "unconnected_pin"]
        next(r for r in incomplete if r.get("kind") == "capture_summary")["unconnected_pins"] = 0
        with self.assertRaisesRegex(ValueError, "explicit NC"):
            connectivity(incomplete)
        with self.assertRaisesRegex(ValueError, "summary counts"):
            connectivity([dict(row, pins=8) if row.get("kind") == "capture_summary" else row for row in source])
        conflicting = rows() + [{"kind": "net_pin", "net": "GND", "reference": "R1", "pin": "1"}]
        with self.assertRaisesRegex(ValueError, "conflicting compiled"):
            connectivity(conflicting)
        wrong_net_count = rows()
        next(r for r in wrong_net_count if r.get("kind") == "compiled")["nets"] = 2
        with self.assertRaisesRegex(ValueError, "compiled net count"):
            connectivity(wrong_net_count)
        duplicate_assignment = rows()
        duplicate_assignment.append(next(dict(r) for r in duplicate_assignment if r.get("kind") == "net_pin"))
        self.assertEqual(connectivity(rows())["pins"], connectivity(duplicate_assignment)["pins"])
        stacked_units = rows()
        stacked_units.append(dict(next(r for r in stacked_units if r.get("kind") == "edit_pin" and
                                       r.get("reference") == "R1" and r.get("pin") == "1"),
                                  x=1000000, y=2000000, part=2))
        refresh_summary(stacked_units)
        self.assertEqual(7, connectivity(stacked_units)["count"])

    def test_physical_capture_is_validated_independently_of_compile(self):
        source = rows() + [{"kind": "compile_summary", "success": False, "complete": True}]
        self.assertEqual(7, len(validate_physical_capture(source)["pins"]))
        with self.assertRaisesRegex(ValueError, "compile failed"):
            connectivity(source)
        bad_count = rows()
        next(r for r in bad_count if r.get("kind") == "capture_summary")["pins"] = 6
        with self.assertRaisesRegex(ValueError, "summary counts"):
            validate_physical_capture(bad_count)

    def test_pin_contracts_require_exact_full_coverage(self):
        snapshot = connectivity(rows())
        result = check_contracts(snapshot, [
            {"component": "R1", "pin": "1", "net": "SDA"},
            {"component": "U1", "pin": "3", "nc": True, "reason": "Reserved by device specification"},
        ], require_all_pins=False)
        self.assertTrue(result["ok"])
        self.assertEqual("NET_MISMATCH", check_contracts(snapshot, [
            {"component": "R1", "pin": "1", "net": "GND"}], False)["findings"][0]["code"])
        with self.assertRaisesRegex(ValueError, "requires a reason"):
            check_contracts(snapshot, [{"component": "U1", "pin": "3", "nc": True}], False)
        result = check_contracts(snapshot, [{"component": "U1", "pin": "3", "nc": True, "reason": "unused"}])
        self.assertIn("UNSPECIFIED_PIN", {f["code"] for f in result["findings"]})

    def test_diff_reports_changed_added_removed_and_unchanged_pins(self):
        before = connectivity(rows())
        after = dict(before, pins=[dict(pin) for pin in before["pins"]])
        after["pins"][0]["net"] = "OTHER"
        after["pins"].append({"component": "J1", "pin": "1", "net": "SDA"})
        after["pins"].remove(next(p for p in after["pins"] if p["component"] == "U1" and p["pin"] == "3"))
        report = diff(before, after)
        self.assertEqual({"NET_CHANGED", "PIN_ADDED", "PIN_REMOVED"}, {c["code"] for c in report["changes"]})
        self.assertEqual(5, report["unchanged_count"])

    def test_lint_checks_pullup_decoupling_placeholder_and_incomplete_intent(self):
        report = lint(rows(), intent())
        self.assertEqual("passed", report["status"], report["findings"])
        missing = lint(rows(), {"decoupling": []})
        self.assertEqual("incomplete", missing["status"])
        self.assertEqual("passed", lint(rows(), {"decoupling": [], "pullups": []})["status"])
        bad = rows(value="R?")
        result = lint(bad, intent())
        self.assertEqual("failed", result["status"])
        self.assertIn("PLACEHOLDER_VALUE", {f["code"] for f in result["findings"]})
        mismatch = lint(rows(), {**intent(), "contracts": [{"component": "U1", "pin": "1", "net": "GND"}]})
        self.assertIn("NET_MISMATCH", {f["code"] for f in mismatch["findings"]})
        truncated = rows()
        next(r for r in truncated if r.get("kind") == "capture_summary")["complete"] = False
        self.assertEqual("incomplete", lint(truncated, intent())["status"])

    def test_engineering_values_accept_resistor_notation_and_reject_wrong_units(self):
        for value in ("4.7k", "4k7", "4700R", "4.7kohm"):
            with self.subTest(value=value):
                self.assertEqual("passed", lint(rows(value=value), intent())["status"])
        self.assertEqual((4.7, "ohm"), _value_number("4R7", "RESISTANCE"))
        self.assertEqual((0.0047, "ohm"), _value_number("4m7", "RESISTANCE"))
        self.assertEqual((4_700_000.0, "ohm"), _value_number("4M7", "RESISTANCE"))
        value, unit = _value_number("100nF", "CAPACITANCE")
        self.assertAlmostEqual(100e-9, value)
        self.assertEqual("f", unit)

        wrong_resistor = lint(rows(value="100nF"), intent())
        self.assertIn("REQUIRED_COMPONENT_VALUE", {f["code"] for f in wrong_resistor["findings"]})
        wrong_capacitor = rows()
        next(r for r in wrong_capacitor if r.get("kind") == "edit_component" and r.get("reference") == "C1")["value"] = "4.7kohm"
        wrong_capacitor = lint(wrong_capacitor, intent())
        self.assertIn("REQUIRED_COMPONENT_VALUE", {f["code"] for f in wrong_capacitor["findings"]})
        self.assertIsNone(_value_number("4.7kX", "RESISTANCE")[0])

    def test_lint_reports_grid_orphan_near_miss_and_single_pin(self):
        source = rows()
        source[1]["x"] += 1
        source.append({"kind": "edit_label", "name": "FLOATING", "x": 15000000, "y": 9000000})
        source.append({"kind": "edit_pin", "reference": "U1", "pin": "4", "x": 10000100, "y": 2000000,
                       "electrical_type": "Passive"})
        source.append({"kind": "unconnected_pin", "reference": "U1", "pin": "4"})
        refresh_summary(source)
        result = lint(source, intent())
        codes = {f["code"] for f in result["findings"]}
        self.assertIn("OFF_GRID_COMPONENT", codes)
        self.assertIn("ORPHAN_NET_LABEL", codes)
        self.assertIn("PIN_NEAR_MISS", codes)
        self.assertIn("SINGLE_PIN_NET", codes)

    def test_lint_detects_pin_and_wire_near_misses_to_segment_interiors(self):
        source = rows()
        source.extend([
            {"kind": "edit_pin", "reference": "U1", "pin": "4", "x": 5000000, "y": 2005000,
             "electrical_type": "Passive"},
            {"kind": "unconnected_pin", "reference": "U1", "pin": "4"},
            {"kind": "edit_wire", "vertices": [[5000000, 2009999], [6000000, 2009999]]},
        ])
        refresh_summary(source)
        codes = {f["code"] for f in lint(source, intent())["findings"]}
        self.assertIn("PIN_NEAR_MISS", codes)
        self.assertIn("WIRE_NEAR_MISS", codes)

    def test_lint_does_not_call_connected_tight_l_or_hairpin_a_near_miss(self):
        for vertices in (
                [[100, 100], [300, 100], [300, 400]],
                [[100000, 100000], [300000, 100000], [300000, 109999], [100000, 109999]]):
            with self.subTest(vertices=vertices):
                source = rows() + [{"kind": "edit_wire", "vertices": vertices}]
                refresh_summary(source)
                report = lint(source, intent())
                self.assertNotIn("WIRE_NEAR_MISS", {f["code"] for f in report["findings"]})

    def test_lint_keeps_physical_diagnostics_when_compile_fails(self):
        source = rows() + [
            {"kind": "compile_summary", "success": False, "complete": True},
            {"kind": "edit_label", "name": "FLOATING", "x": 15000000, "y": 9000000},
        ]
        refresh_summary(source)
        report = lint(source, intent())
        codes = {f["code"] for f in report["findings"]}
        self.assertEqual("incomplete", report["status"])
        self.assertIn("INCOMPLETE_CONNECTIVITY", codes)
        self.assertIn("ORPHAN_NET_LABEL", codes)
        self.assertNotIn("REQUIRED_POWER_PIN_NET", codes)

    def test_named_object_at_bare_crossing_is_ambiguous_until_junction(self):
        source = rows() + [
            {"kind": "edit_label", "name": "CROSS", "x": 5000000, "y": 2000000},
            {"kind": "edit_wire", "vertices": [[5000000, 0], [5000000, 10000000]]},
        ]
        refresh_summary(source)
        report = lint(source, intent())
        self.assertIn("AMBIGUOUS_NET_LABEL_ATTACHMENT", {f["code"] for f in report["findings"]})
        source.append({"kind": "edit_junction", "x": 5000000, "y": 2000000})
        refresh_summary(source)
        report = lint(source, intent())
        self.assertNotIn("AMBIGUOUS_NET_LABEL_ATTACHMENT", {f["code"] for f in report["findings"]})

    def test_noerc_requires_physical_pin_and_explicit_nc_contract(self):
        source = rows() + [{"kind": "edit_noerc", "uid": "NOERC001", "x": 12000000, "y": 8000000}]
        refresh_summary(source)
        nc = {**intent(), "contracts": [{"component": "U1", "pin": "3", "nc": True,
                                          "reason": "Reserved by the device"}]}
        self.assertEqual("passed", lint(source, nc)["status"])

        connected = rows() + [{"kind": "edit_noerc", "uid": "NOERC002", "x": 10000000, "y": 2000000}]
        refresh_summary(connected)
        report = lint(connected, intent())
        self.assertIn("UNBOUND_NOERC", {f["code"] for f in report["findings"]})

        co_located = rows()
        co_located.extend([
            {"kind": "edit_pin", "reference": "U1", "pin": "4", "name": "P4",
             "electrical_type": "Passive", "x": 12000000, "y": 8000000, "uid": "U14", "part": 1},
            {"kind": "net_pin", "net": "VCC", "reference": "U1", "pin": "4"},
            {"kind": "edit_noerc", "uid": "NOERC003", "x": 12000000, "y": 8000000},
        ])
        refresh_summary(co_located)
        nc_and_connected = {**nc, "contracts": nc["contracts"] + [
            {"component": "U1", "pin": "4", "net": "VCC"}]}
        report = lint(co_located, nc_and_connected)
        self.assertIn("UNBOUND_NOERC", {f["code"] for f in report["findings"]})

    def test_noerc_without_pin_or_with_malformed_position_is_not_certified(self):
        source = rows() + [{"kind": "edit_noerc", "uid": "NOERC004", "x": 15000000, "y": 9000000}]
        refresh_summary(source)
        report = lint(source, {**intent(), "contracts": []})
        self.assertIn("UNBOUND_NOERC", {f["code"] for f in report["findings"]})

        malformed = rows() + [{"kind": "edit_noerc", "uid": "NOERC005", "x": "bad", "y": 8000000}]
        refresh_summary(malformed)
        report = lint(malformed, {**intent(), "contracts": []})
        self.assertEqual("incomplete", report["status"])
        self.assertIn("UNBOUND_NOERC", {f["code"] for f in report["findings"]})

    def test_lint_refuses_unmodeled_geometry_and_unknown_intent(self):
        blocked = rows() + [{"kind": "edit_blocker", "type": "bus", "x": 1, "y": 2}]
        self.assertEqual("incomplete", lint(blocked, intent())["status"])
        with self.assertRaisesRegex(ValueError, "unsupported intent fields"):
            lint(rows(), {**intent(), "contract": []})

    def test_lint_respects_crossings_unless_junction_connects_them(self):
        source = rows()
        source += [{"kind": "edit_label", "name": "OTHER", "x": 5000000, "y": 4000000},
                   {"kind": "edit_wire", "vertices": [[5000000, 0], [5000000, 10000000]]}]
        refresh_summary(source)
        crossing = lint(source, intent())
        self.assertNotIn("CONFLICTING_NET_NAMES", {f["code"] for f in crossing["findings"]})
        source.append({"kind": "edit_junction", "x": 5000000, "y": 5000000})
        refresh_summary(source)
        connected = lint(source, intent())
        self.assertIn("CONFLICTING_NET_NAMES", {f["code"] for f in connected["findings"]})

    def test_diagonal_wire_attachment_is_supported(self):
        source = rows() + [{"kind": "edit_label", "name": "DIAG", "x": 15000000, "y": 8000000},
                           {"kind": "edit_wire", "vertices": [[12000000, 5000000], [18000000, 11000000]]}]
        refresh_summary(source)
        report = lint(source, intent())
        self.assertNotIn("ORPHAN_NET_LABEL", {f["code"] for f in report["findings"]})

    def test_erc_requires_complete_native_findings_and_configuration(self):
        incomplete = erc([])
        self.assertEqual("incomplete", incomplete["status"])
        blocked = erc([{"kind": "erc_summary", "complete": True, "configuration_complete": True,
                        "violation_count": 0, "config": {}}])
        self.assertEqual("incomplete", blocked["status"])
        clean = erc([{"kind": "erc_summary", "complete": True, "configuration_complete": True, "violation_count": 1,
                       "config": {"rule_set": "fixture"}},
                     {"kind": "erc_violation", "message": "Suppressed warning", "severity": "warning",
                      "source": "Sheet.SchDoc", "suppressed": True}])
        self.assertTrue(clean["ok"])
        self.assertEqual(1, clean["suppressed_count"])
        failed = erc([{"kind": "erc_summary", "complete": True, "configuration_complete": True, "violation_count": 1,
                       "config": {"rule_set": "fixture"}},
                      {"kind": "erc_violation", "message": "Pin not driven", "severity": "error",
                       "source": "Sheet.SchDoc", "suppressed": False}])
        self.assertEqual("failed", failed["status"])
        unknown = erc([{"kind": "erc_summary", "complete": True, "configuration_complete": True,
                        "violation_count": 1, "config": {"rule_set": "fixture"}},
                       {"kind": "erc_violation", "message": "Unknown", "severity": "unexpected",
                        "source": "Sheet.SchDoc", "suppressed": False}])
        self.assertEqual("incomplete", unknown["status"])
        compile_failed = erc([{"kind": "compile_summary", "success": False, "complete": True},
                              {"kind": "erc_summary", "complete": True, "configuration_complete": True,
                               "violation_count": 0, "config": {"rule_set": "fixture"}}])
        self.assertEqual("incomplete", compile_failed["status"])
        compile_failed_with_error = erc([
            {"kind": "compile_summary", "success": False, "complete": True},
            {"kind": "erc_summary", "complete": True, "configuration_complete": True,
             "violation_count": 1, "config": {"rule_set": "fixture"}},
            {"kind": "erc_violation", "message": "Pin conflict", "severity": "error",
             "source": "Sheet.SchDoc", "suppressed": False},
        ])
        self.assertEqual("failed", compile_failed_with_error["status"])


if __name__ == "__main__":
    unittest.main()
