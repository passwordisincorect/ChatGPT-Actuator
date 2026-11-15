from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from chatgpt_actuator.tools.eda_altium_changes import command, normalize, verify_objects


class Guard:
    def __init__(self, path):
        self.path = Path(path)

    def resolve_existing(self, _path):
        return self.path


class Adapter:
    def __init__(self, path):
        self.library_guard = Guard(path)

    def library_inspect(self, _path, *, cached_only=False):
        assert cached_only is True, "Structural preview must not dispatch native library reads"
        return {"symbols": [{"name": "R", "parts": 1, "source_path": str(self.library_guard.path),
                              "pins": [{"number": "1", "name": "A", "part": 1, "x": 100, "y": 0,
                                        "length": 50, "orientation": 0, "electrical_type": "Passive"},
                                       {"number": "2", "name": "B", "part": 1, "x": 100, "y": 0,
                                        "length": 50, "orientation": 2, "electrical_type": "Passive"}]}]}


def legacy(changes, _rows):
    return changes


class AltiumChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Path(self.temp.name) / "parts.SchLib"
        self.library.write_bytes(b"test library")
        self.adapter = Adapter(self.library)
        self.component = {
            "kind": "edit_component", "uid": "COMP0001", "reference": "R1", "symbol": "R",
            "part": 1, "part_count": 1, "value": "10k", "x": 1000000, "y": 2000000,
            "rotation": 0, "parameters": [{"name": "MPN", "text": "old"}, {"name": "Note", "text": "keep"}],
        }
        self.wire = {"kind": "edit_wire", "uid": "WIRE0001", "vertices": [[0, 0], [1000000, 0]]}
        self.label = {"kind": "edit_label", "uid": "LABEL001", "name": "DATA", "x": 0, "y": 0, "rotation": 0}
        self.rows = [self.component, self.wire, self.label]

    def normalize(self, changes, rows=None):
        return normalize(changes, self.rows if rows is None else rows, self.adapter, legacy)

    @staticmethod
    def captured_component_pins(change):
        from chatgpt_actuator.tools.eda_altium import AltiumAdapter
        pins = []
        for index, pin in enumerate(change["expected_pins"], 1):
            x, y, orientation = AltiumAdapter._pin_position(change, pin)
            pins.append({"kind": "edit_pin", "uid": f"PINNEW{index:03}",
                         "reference": change["reference"], "pin": pin["number"], "name": pin["name"],
                         "part": pin["part"], "electrical_type": pin["electrical_type"],
                         "orientation": orientation, "x": x * 10000, "y": y * 10000})
        return pins

    def test_add_component_validates_library_reference_and_duplicate_designators(self):
        change = self.normalize([{"op": "add_component", "library_path": str(self.library),
                                 "symbol": "R", "reference": "C1", "value": "100nF", "x": 10, "y": 20}])[0]
        self.assertEqual("C1", change["reference"])
        self.assertTrue(change["uid"].startswith("AC"))
        with self.assertRaisesRegex(ValueError, "Duplicate component reference"):
            self.normalize([{"op": "add_component", "library_path": str(self.library),
                             "symbol": "R", "reference": "r1", "value": "1k", "x": 0, "y": 0}])
        with self.assertRaisesRegex(ValueError, "symbol was not verified"):
            self.normalize([{"op": "add_component", "library_path": str(self.library),
                             "symbol": "UNKNOWN", "reference": "C1", "value": "1n", "x": 0, "y": 0}])

    def test_add_change_rejects_malformed_coordinates_and_unknown_fields(self):
        for change in (
            {"op": "add_label", "text": "DATA", "x": True, "y": 0},
            {"op": "add_wire", "vertices": [[0, 0], [0, 0]]},
            {"op": "add_wire", "vertices": [[0, 0], [1, 1]], "force": True},
            {"op": "add_junction", "x": 100001, "y": 0},
            {"op": "add_power", "text": "GND", "x": 0, "y": 0, "style": 16},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.normalize([change])

    def test_change_lists_and_uids_are_exact_and_validated(self):
        for changes in ([], None, [None], [{"op": "add_label", "text": "SDA", "x": 0, "y": 0,
                                           "uid": "bad'uid"}],
                        [{"op": "add_label", "text": "SDA", "x": 0, "y": 0, "extra": "unbound"}]):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.normalize(changes)

    def test_control_characters_are_rejected_in_electrical_names_and_values(self):
        for text in ("SDA\x7f", "GND\x85", "Name\x00"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "printable text"):
                self.normalize([{"op": "add_label", "text": text, "x": 0, "y": 0}])

    def test_new_uids_are_unique_and_do_not_overwrite_existing_objects(self):
        with self.assertRaisesRegex(ValueError, "UID already exists"):
            self.normalize([{"op": "add_label", "uid": "WIRE0001", "text": "X", "x": 0, "y": 0}])
        with self.assertRaisesRegex(ValueError, "one operation per object UID"):
            self.normalize([
                {"op": "add_label", "uid": "NEW00001", "text": "X", "x": 0, "y": 0},
                {"op": "add_junction", "uid": "NEW00001", "x": 1, "y": 1},
            ])

    def test_existing_object_requires_unique_uid_and_exact_fresh_properties(self):
        expected = dict(self.component)
        move = {"op": "move_component", "uid": "COMP0001", "expected": expected,
                "x": 300, "y": 400, "rotation": 90}
        normalized = self.normalize([move])[0]
        self.assertEqual("component", normalized["object_type"])
        with self.assertRaisesRegex(ValueError, "missing, ambiguous or stale"):
            self.normalize([{**move, "expected": {**expected, "value": "stale"}}])
        with self.assertRaisesRegex(ValueError, "missing, ambiguous or stale"):
            self.normalize([move], self.rows + [dict(self.component)])
        with self.assertRaisesRegex(ValueError, "requires a component UID"):
            self.normalize([{"op": "move_component", "uid": "WIRE0001", "expected": dict(self.wire),
                             "x": 3, "y": 4}])

    def test_parameter_change_requires_expected_value_and_component_selector(self):
        result = self.normalize([{"op": "set_component_parameter", "uid": "COMP0001",
                                  "expected": dict(self.component), "name": "MPN",
                                  "expected_value": "old", "value": "new"}])[0]
        self.assertEqual("component", result["object_type"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.normalize([{"op": "set_component_parameter", "uid": "COMP0001",
                             "expected": dict(self.component), "name": "MPN",
                             "expected_value": "wrong", "value": "new"}])

    def test_update_keeps_object_type_and_binds_replacement_to_same_uid(self):
        change = self.normalize([{"op": "update_object", "uid": "LABEL001", "expected": dict(self.label),
                                  "replacement": {"op": "add_label", "text": "CLOCK", "x": 10, "y": 20}}])[0]
        self.assertEqual("LABEL001", change["replacement"]["uid"])
        self.assertEqual("add_label", change["replacement"]["op"])
        self.assertEqual("AddLabel('LABEL001', 'CLOCK', 10, 20, 0);", command(change["replacement"]))
        with self.assertRaisesRegex(ValueError, "same primitive type"):
            self.normalize([{"op": "update_object", "uid": "LABEL001", "expected": dict(self.label),
                             "replacement": {"op": "add_junction", "x": 10, "y": 20}}])

    def test_commands_escape_string_arguments_and_preserve_mil_units(self):
        label = self.normalize([{"op": "add_label", "uid": "NEWLABEL1", "text": "SDA'測",
                                 "x": -12, "y": 42, "rotation": 270}])[0]
        self.assertEqual("AddLabel('NEWLABEL1', 'SDA'''#28204, -12, 42, 270);", command(label))
        moved = self.normalize([{"op": "move_component", "uid": "COMP0001",
                                 "expected": dict(self.component), "x": 12, "y": 43, "rotation": 180}])[0]
        self.assertEqual("MoveComponent('COMP0001', 12, 43, 180);", command(moved))
        wire = self.normalize([{"op": "add_wire", "uid": "NEW_WIRE1",
                                "vertices": [[0, 1], [2, 1], [2, 5]]}])[0]
        self.assertEqual("BeginWire('NEW_WIRE1');\nWireVertex(0, 1);\nWireVertex(2, 1);\nWireVertex(2, 5);\nEndWire;",
                         command(wire))

    def test_verify_add_move_remove_and_parameter_edits(self):
        add = self.normalize([{"op": "add_junction", "uid": "NEWJUNC01", "x": 10, "y": 20}])
        verify_objects(self.rows, self.rows + [{"kind": "edit_junction", "uid": "NEWJUNC01",
                                               "x": 100000, "y": 200000}], add)
        with self.assertRaisesRegex(RuntimeError, "position differs"):
            verify_objects(self.rows, self.rows + [{"kind": "edit_junction", "uid": "NEWJUNC01",
                                                    "x": 100001, "y": 200000}], add)

        move = self.normalize([{"op": "move_component", "uid": "COMP0001",
                                "expected": dict(self.component), "x": 30, "y": 40, "rotation": 90}])
        changed = dict(self.component, x=300000, y=400000, rotation=1)
        verify_objects(self.rows, [changed, self.wire, self.label], move)
        with self.assertRaisesRegex(RuntimeError, "Unrelated schematic objects"):
            verify_objects(self.rows, [changed, dict(self.wire, vertices=[[0, 0], [5, 0]]), self.label], move)

        remove = self.normalize([{"op": "remove_object", "uid": "WIRE0001", "expected": dict(self.wire)}])
        verify_objects(self.rows, [self.component, self.label], remove)
        with self.assertRaisesRegex(RuntimeError, "still exists"):
            verify_objects(self.rows, self.rows, remove)

        change_value = self.normalize([{"op": "set_component_parameter", "uid": "COMP0001",
                                        "expected": dict(self.component), "name": "MPN",
                                        "expected_value": "old", "value": "new"}])
        changed = dict(self.component, parameters=[{"name": "MPN", "text": "new"}, {"name": "Note", "text": "keep"}])
        verify_objects(self.rows, [changed, self.wire, self.label], change_value)
        changed["parameters"][1]["text"] = "tampered"
        with self.assertRaisesRegex(RuntimeError, "unrelated parameter"):
            verify_objects(self.rows, [changed, self.wire, self.label], change_value)

    def test_move_preserves_translated_pin_geometry(self):
        pin = {"kind": "edit_pin", "uid": "PIN00001", "reference": "R1", "pin": "1",
               "name": "A", "electrical_type": "Passive", "orientation": 0,
               "x": 1010000, "y": 2020000, "part": 1}
        before = self.rows + [pin]
        change = self.normalize([{"op": "move_component", "uid": "COMP0001",
                                  "expected": dict(self.component), "x": 300, "y": 400, "rotation": 0}], before)
        moved = dict(self.component, x=3000000, y=4000000)
        translated_pin = dict(pin, x=3010000, y=4020000)
        verify_objects(before, [moved, self.wire, self.label, translated_pin], change)
        with self.assertRaisesRegex(RuntimeError, "pin"):
            verify_objects(before, [moved, self.wire, self.label, dict(translated_pin, x=3500000)], change)
        rotated = self.normalize([{"op": "move_component", "uid": "COMP0001",
                                   "expected": dict(self.component), "x": 300, "y": 400, "rotation": 90}], before)
        rotated_pin = dict(pin, x=2980000, y=4010000, orientation=1)
        verify_objects(before, [dict(self.component, x=3000000, y=4000000, rotation=1),
                                self.wire, self.label, rotated_pin], rotated)
        with self.assertRaisesRegex(RuntimeError, "pin geometry"):
            verify_objects(before, [dict(self.component, x=3000000, y=4000000, rotation=1), self.wire,
                                    self.label, dict(rotated_pin, orientation=0)], rotated)

    def test_verify_add_component_rejects_wrong_reference_geometry_and_unrelated_changes(self):
        change = self.normalize([{"op": "add_component", "uid": "NEWCOMP01", "library_path": str(self.library),
                                  "symbol": "R", "reference": "R2", "value": "2k", "x": 12, "y": 15}])
        component = {"kind": "edit_component", "uid": "NEWCOMP01", "reference": "R2", "symbol": "R",
                     "part": 1, "part_count": 1, "value": "2k", "x": 120000, "y": 150000, "rotation": 0}
        pins = self.captured_component_pins(change[0])
        verify_objects(self.rows, self.rows + [component] + pins, change)
        with self.assertRaisesRegex(RuntimeError, "field readback mismatch"):
            verify_objects(self.rows, self.rows + [dict(component, reference="R3")] + pins, change)
        with self.assertRaisesRegex(RuntimeError, "pin inventory"):
            verify_objects(self.rows, self.rows + [component, pins[0]], change)
        with self.assertRaisesRegex(RuntimeError, "pin geometry or identity"):
            tampered = [dict(pin, name="wrong") if pin["pin"] == "1" else pin for pin in pins]
            verify_objects(self.rows, self.rows + [component] + tampered, change)
        with self.assertRaisesRegex(RuntimeError, "pin geometry or identity"):
            tampered = [dict(pin, x=pin["x"] + 1) if pin["pin"] == "1" else pin for pin in pins]
            verify_objects(self.rows, self.rows + [component] + tampered, change)

    def test_verify_wire_geometry_and_updated_label_without_unrelated_mutations(self):
        wire_change = self.normalize([{"op": "add_wire", "uid": "NEWWIRE01",
                                       "vertices": [[0, 1], [2, 1], [2, 5]]}])
        wire = {"kind": "edit_wire", "uid": "NEWWIRE01",
                "vertices": [[0, 10000], [20000, 10000], [20000, 50000]]}
        verify_objects(self.rows, self.rows + [wire], wire_change)
        with self.assertRaisesRegex(RuntimeError, "vertices differ"):
            verify_objects(self.rows, self.rows + [dict(wire, vertices=[[0, 10000], [5, 10000]])], wire_change)

        update = self.normalize([{"op": "update_object", "uid": "LABEL001", "expected": dict(self.label),
                                  "replacement": {"op": "add_label", "text": "CLOCK", "x": 25, "y": -4,
                                                  "rotation": 180}}])
        edited_label = dict(self.label, name="CLOCK", x=250000, y=-40000, rotation=2)
        verify_objects(self.rows, [self.component, self.wire, edited_label], update)
        with self.assertRaisesRegex(RuntimeError, "Object position differs"):
            verify_objects(self.rows, [self.component, self.wire, dict(edited_label, y=-39999)], update)


if __name__ == "__main__":
    unittest.main()
