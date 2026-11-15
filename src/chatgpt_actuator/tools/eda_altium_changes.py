"""Validated SCH primitives; all public coordinates are integer mils."""
from copy import deepcopy
import hashlib
import json
import re
import unicodedata

from .eda_altium import _pas

KINDS = {"add_component": "edit_component", "add_wire": "edit_wire",
         "add_label": "edit_label", "add_junction": "edit_junction",
         "add_power": "edit_power", "add_noerc": "edit_noerc"}
TYPE_NAMES = {"edit_component": "component", "edit_wire": "wire", "edit_label": "label",
              "edit_junction": "junction", "edit_power": "power", "edit_noerc": "noerc"}


def _text(value, name, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > 256 or any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError(name + " must be printable text up to 256 characters")
    return value


def _integer(value, name, low=-100000, high=100000):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(name + " must be an integer within supported sheet bounds")
    return value


def _position(change):
    for key in ("x", "y"):
        _integer(change.get(key), key)
    rotation = change.setdefault("rotation", 0)
    if type(rotation) is not int or rotation not in (0, 90, 180, 270):
        raise ValueError("rotation must be 0, 90, 180 or 270 degrees")


def normalize(changes, rows, adapter, legacy):
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes must be a non-empty list")
    old = [c for c in changes if isinstance(c, dict) and c.get("op") in {"set_component_value", "rename_net"}]
    old_normalized = iter(legacy(old, rows) if old else [])
    result, touched = [], set()
    refs = {r.get("reference", "").casefold() for r in rows if r.get("kind") == "edit_component"}
    for index, raw in enumerate(changes):
        if not isinstance(raw, dict):
            raise ValueError("Every change must be an object")
        change = deepcopy(raw)
        op = change.get("op")
        if op in {"set_component_value", "rename_net"}:
            result.append(next(old_normalized))
            continue
        allowed = {"op", "uid"}
        if op in KINDS:
            uid = change.setdefault("uid", "AC" + hashlib.sha256(json.dumps([index, raw], sort_keys=True).encode()).hexdigest()[:14].upper())
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", str(uid)):
                raise ValueError("Added UID must contain 8..32 letters, digits, _ or -")
            if any(r.get("uid") == uid for r in rows):
                raise ValueError("Added UID already exists")
            if op == "add_wire":
                allowed |= {"vertices"}
                vertices = change.get("vertices")
                if not isinstance(vertices, list) or not 2 <= len(vertices) <= 100:
                    raise ValueError("Wire requires 2..100 vertices")
                for point in vertices:
                    if not isinstance(point, list) or len(point) != 2:
                        raise ValueError("Each wire vertex requires [x,y]")
                    for coordinate in point:
                        _integer(coordinate, "vertex")
                if any(a == b for a, b in zip(vertices, vertices[1:])):
                    raise ValueError("Wire contains a zero-length segment")
            else:
                allowed |= {"x", "y", "rotation"}
                _position(change)
            if op == "add_component":
                allowed |= {"library_path", "symbol", "reference", "value", "part", "source_path", "expected_pins", "source_hashes", "expected_part_count"}
                library = adapter.library_guard.resolve_existing(change.get("library_path", ""))
                library_data = adapter.library_inspect(str(library), cached_only=True)
                symbols = library_data.get("symbols", [])
                symbol = next((s for s in symbols if s.get("name") == change.get("symbol")), None)
                if symbol is None:
                    raise ValueError("Component symbol was not verified in the selected library")
                _text(change.get("reference"), "reference")
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*[0-9][A-Za-z0-9_]*", change["reference"]):
                    raise ValueError("Invalid reference")
                if change["reference"].casefold() in refs:
                    raise ValueError("Duplicate component reference")
                refs.add(change["reference"].casefold())
                _text(change.get("value"), "value")
                _integer(change.setdefault("part", 1), "part", 1, int(symbol.get("parts", 1)))
                change["library_path"] = str(library)
                change["source_path"] = str(symbol["source_path"])
                change["source_hashes"] = library_data.get("source_hashes", {})
                change["expected_part_count"] = int(symbol["parts"])
                change["expected_pins"] = [p for p in symbol.get("pins", []) if int(p.get("part", 0)) in (0, change["part"])]
                if not change["expected_pins"]:
                    raise ValueError("Verified symbol pin inventory is empty")
            elif op in {"add_label", "add_power"}:
                allowed |= {"text"}
                _text(change.get("text"), "text")
            if op == "add_power":
                allowed |= {"style"}
                _integer(change.setdefault("style", 0), "style", 0, 15)
            if op == "add_noerc":
                allowed |= {"component", "pin", "reason"}
                for key in ("component", "pin", "reason"):
                    _text(change.get(key), key)
        elif op in {"remove_object", "move_component", "set_component_parameter", "update_object"}:
            allowed |= {"expected"}
            matches = [r for r in rows if r.get("uid") == change.get("uid") and r.get("kind") in TYPE_NAMES]
            if len(matches) != 1 or change.get("expected") != matches[0]:
                raise ValueError("Object UID/expected properties are missing, ambiguous or stale")
            target = matches[0]
            change["object_type"] = TYPE_NAMES[target["kind"]]
            allowed.add("object_type")
            if op == "move_component":
                if target["kind"] != "edit_component":
                    raise ValueError("move_component requires a component UID")
                allowed |= {"x", "y", "rotation"}
                _position(change)
            elif op == "set_component_parameter":
                if target["kind"] != "edit_component":
                    raise ValueError("Parameter target must be a component")
                allowed |= {"name", "expected_value", "value"}
                for key in ("name", "expected_value", "value"):
                    _text(change.get(key), key, empty=key != "name")
                if change["name"].casefold() in {"comment", "value"}:
                    raise ValueError("Use set_component_value for the component value/comment")
                params = target.get("parameters", [])
                existing = [p for p in params if p.get("name") == change["name"]]
                if len(existing) > 1 or (existing[0].get("text", "") if existing else "") != change["expected_value"]:
                    raise ValueError("Parameter value does not match the snapshot")
            elif op == "update_object":
                allowed |= {"replacement"}
                replacement = change.get("replacement")
                if not isinstance(replacement, dict) or KINDS.get(replacement.get("op")) != target["kind"] or target["kind"] == "edit_component":
                    raise ValueError("Replacement must add the same primitive type")
                replacement = dict(replacement, uid=change["uid"])
                remaining = [r for r in rows if r is not target]
                change["replacement"] = normalize([replacement], remaining, adapter, legacy)[0]
        else:
            raise ValueError("Unsupported schematic edit operation: " + str(op))
        if set(change) - allowed:
            raise ValueError("Unknown edit fields: " + ", ".join(sorted(set(change) - allowed)))
        if change["uid"] in touched:
            raise ValueError("Only one operation per object UID is permitted in a preview")
        touched.add(change["uid"])
        result.append(change)
    # A legacy reference selector and UID selector must not mutate the same object.
    legacy_refs = {c["reference"].casefold() for c in result if c["op"] == "set_component_value"}
    if any(c.get("expected", {}).get("reference", "").casefold() in legacy_refs for c in result if c.get("expected")):
        raise ValueError("Conflicting edits target the same component")
    return result


def command(change):
    op, uid = change["op"], _pas(change.get("uid", ""))
    if op == "remove_object":
        return f"DeleteObject({uid}, {_pas(change['object_type'])});"
    if op == "move_component":
        return f"MoveComponent({uid}, {change['x']}, {change['y']}, {change['rotation']});"
    if op == "set_component_parameter":
        args = ", ".join(_pas(change[k]) for k in ("uid", "name", "expected_value", "value"))
        return f"SetComponentParameter({args});"
    if op == "update_object":
        return f"DeleteObject({uid}, {_pas(change['object_type'])});\n" + command(change["replacement"])
    if op == "add_wire":
        return f"BeginWire({uid});\n" + "\n".join(f"WireVertex({x}, {y});" for x, y in change["vertices"]) + "\nEndWire;"
    if op == "add_component":
        args = ", ".join(_pas(change[k]) for k in ("source_path", "symbol", "reference", "value", "uid"))
        return f"AddComponent({args}, {change['x']}, {change['y']}, {change['rotation']}, {change['part']});"
    function = {"add_label": "AddLabel", "add_power": "AddPower", "add_noerc": "AddNoERC", "add_junction": "AddJunction"}[op]
    args = [uid]
    if op in {"add_label", "add_power"}:
        args.append(_pas(change["text"]))
    args += [str(change["x"]), str(change["y"])]
    if op in {"add_label", "add_power"}:
        args.append(str(change["rotation"]))
    if op == "add_power":
        args.append(str(change["style"]))
    return function + "(" + ", ".join(args) + ");"


def verify_objects(before, after, changes):
    """Verify every untouched object and each declared changed field independently."""
    details = lambda rows: [r for r in rows if r.get("kind", "").startswith("edit_")]
    remaining_before, remaining_after = deepcopy(details(before)), deepcopy(details(after))
    for change in changes:
        op = change["op"]
        if op in {"set_component_value", "rename_net"}:
            continue
        uid = change["uid"]
        old = [r for r in remaining_before if r.get("uid") == uid]
        new = [r for r in remaining_after if r.get("uid") == uid]
        if op == "remove_object":
            if new:
                raise RuntimeError("Removed object still exists")
        else:
            if len(new) != 1:
                raise RuntimeError("Changed object readback is missing or ambiguous")
            row = new[0]
            spec = change["replacement"] if op == "update_object" else change
            expected_kind = KINDS.get(spec["op"], "edit_component")
            if row.get("kind") != expected_kind:
                raise RuntimeError("Object type differs from the declared edit")
            if op == "move_component" or spec["op"] in KINDS:
                if spec["op"] == "add_wire":
                    expected = [[x * 10000, y * 10000] for x, y in spec["vertices"]]
                    if row.get("vertices") != expected:
                        raise RuntimeError("Wire vertices differ from requested geometry")
                else:
                    for axis in ("x", "y"):
                        if row.get(axis) != spec[axis] * 10000:
                            raise RuntimeError("Object position differs from requested geometry")
                    if row.get("rotation", 0) != spec.get("rotation", 0) // 90:
                        raise RuntimeError("Object rotation differs from requested geometry")
                for source, target in (("text", "name"), ("reference", "reference"), ("symbol", "symbol"), ("value", "value"), ("part", "part"), ("style", "style")):
                    if source in spec and row.get(target) != spec[source]:
                        raise RuntimeError("Object field readback mismatch: " + target)
            if op == "set_component_parameter":
                params = [p for p in row.get("parameters", []) if p.get("name") == change["name"]]
                if len(params) != 1 or params[0].get("text") != change["value"]:
                    raise RuntimeError("Component parameter readback differs from request")
            if spec["op"] == "add_noerc":
                pins = [p for p in after if p.get("kind") == "edit_pin" and p.get("reference") == spec["component"] and p.get("pin") == spec["pin"]]
                if not pins or any((p.get("x"), p.get("y")) != (row.get("x"), row.get("y")) for p in pins):
                    raise RuntimeError("No-ERC directive does not match its declared NC pin endpoint")
            if old and op in {"move_component", "set_component_parameter"}:
                allowed = ({"x", "y", "rotation", "comment_x", "comment_y", "comment_rotation", "parameters"}
                           if op == "move_component" else {"parameters"})
                if {k: v for k, v in old[0].items() if k not in allowed} != {k: v for k, v in row.items() if k not in allowed}:
                    raise RuntimeError("An undeclared component property changed")
                if op == "set_component_parameter":
                    untouched = lambda r: [p for p in r.get("parameters", []) if p.get("name") != change["name"]]
                    if untouched(old[0]) != untouched(row):
                        raise RuntimeError("An unrelated parameter changed")
                    previous = next((p for p in old[0].get("parameters", []) if p.get("name") == change["name"]), None)
                    current = next(p for p in row["parameters"] if p.get("name") == change["name"])
                    if previous is not None and {k: v for k, v in previous.items() if k != "text"} != {k: v for k, v in current.items() if k != "text"}:
                        raise RuntimeError("Parameter geometry changed outside the requested value edit")
                    if previous is None and current.get("hidden") is not True:
                        raise RuntimeError("New component parameter is not hidden as specified")
                else:
                    turns = (change["rotation"] // 90 - old[0]["rotation"]) % 4
                    def transform(x, y):
                        px, py = x - old[0]["x"], y - old[0]["y"]
                        for _ in range(turns):
                            px, py = -py, px
                        return change["x"] * 10000 + px, change["y"] * 10000 + py
                    if all(k in old[0] for k in ("comment_x", "comment_y")):
                        if (row.get("comment_x"), row.get("comment_y")) != transform(old[0]["comment_x"], old[0]["comment_y"]):
                            raise RuntimeError("Component comment geometry differs from the requested transform")
                    if "comment_rotation" in old[0] and row.get("comment_rotation") != (old[0]["comment_rotation"] + turns) % 4:
                        raise RuntimeError("Component comment rotation differs from the requested transform")
                    expected_parameters = deepcopy(old[0].get("parameters", []))
                    for parameter in expected_parameters:
                        if "x" in parameter and "y" in parameter:
                            parameter["x"], parameter["y"] = transform(parameter["x"], parameter["y"])
                        if "rotation" in parameter:
                            parameter["rotation"] = (parameter["rotation"] + turns) % 4
                    if row.get("parameters", []) != expected_parameters:
                        raise RuntimeError("Component parameter geometry differs from the requested transform")
        remaining_before = [r for r in remaining_before if r.get("uid") != uid]
        remaining_after = [r for r in remaining_after if r.get("uid") != uid]
        if old and old[0].get("kind") == "edit_component":
            ref = old[0]["reference"]
            old_pins = [r for r in remaining_before if r.get("kind") == "edit_pin" and r.get("reference") == ref]
            new_pins = [r for r in remaining_after if r.get("kind") == "edit_pin" and r.get("reference") == ref]
            if op == "remove_object" and new_pins:
                raise RuntimeError("Removed component still has captured pins")
            if op in {"move_component", "set_component_parameter"}:
                expected_pins = deepcopy(old_pins)
                if op == "move_component":
                    origin = old[0]
                    turns = (change["rotation"] // 90 - origin["rotation"]) % 4
                    for pin in expected_pins:
                        px, py = pin["x"] - origin["x"], pin["y"] - origin["y"]
                        for _ in range(turns):
                            px, py = -py, px
                        pin["x"], pin["y"] = change["x"] * 10000 + px, change["y"] * 10000 + py
                        if "orientation" in pin:
                            pin["orientation"] = (pin["orientation"] + turns) % 4
                if sorted(map(lambda p: json.dumps(p, sort_keys=True), expected_pins)) != sorted(map(lambda p: json.dumps(p, sort_keys=True), new_pins)):
                    raise RuntimeError("Changed component pin geometry or identity differs from the requested transform")
            remaining_before = [r for r in remaining_before if not (r.get("kind") == "edit_pin" and r.get("reference") == ref)]
            remaining_after = [r for r in remaining_after if not (r.get("kind") == "edit_pin" and r.get("reference") == ref)]
        if op == "add_component":
            from .eda_altium import AltiumAdapter
            captured = [r for r in remaining_after if r.get("kind") == "edit_pin" and r.get("reference") == change["reference"]]
            expected = change["expected_pins"]
            if new[0].get("part_count") != change["expected_part_count"] or new[0].get("mirrored", False) is not False:
                raise RuntimeError("Added component unit count or mirroring differs from its library")
            if len(captured) != len(expected):
                raise RuntimeError("Added component pin inventory differs from the verified library")
            for pin in expected:
                match = [p for p in captured if p["pin"] == pin["number"] and p.get("part") == pin["part"]]
                px, py, _ = AltiumAdapter._pin_position(change, pin)
                if (len(match) != 1 or match[0].get("name") != pin["name"]
                        or match[0].get("x") != px * 10000 or match[0].get("y") != py * 10000
                        or ("electrical_type" in pin and match[0].get("electrical_type") != pin["electrical_type"])):
                    raise RuntimeError("Added component pin geometry or identity differs from its library")
            remaining_after = [r for r in remaining_after if not (r.get("kind") == "edit_pin" and r.get("reference") == change["reference"])]
    # Apply the two existing exact edits before comparing all remaining properties.
    for change in changes:
        for row in remaining_before:
            if change["op"] == "set_component_value" and row.get("kind") == "edit_component" and row.get("reference", "").casefold() == change["reference"].casefold():
                row["value"] = change["value"]
            if change["op"] == "rename_net" and row.get("kind") == "edit_label" and row.get("name", "").casefold() == change["old_name"].casefold():
                row["name"] = change["new_name"]
    stable = lambda rows: sorted(json.dumps(r, sort_keys=True) for r in rows)
    if stable(remaining_before) != stable(remaining_after):
        raise RuntimeError("Unrelated schematic objects changed during editing")
