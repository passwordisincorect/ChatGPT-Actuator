"""Pure checks over complete, compiled AD26 schematic inspection rows."""
from __future__ import annotations

import hashlib
import json
import math
import re


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _pin_index(pins):
    if not isinstance(pins, list) or not pins:
        raise ValueError("pins must be a non-empty list")
    index = {}
    for row in pins:
        if not isinstance(row, dict):
            raise ValueError("each pin assignment must be an object")
        component, pin, net = row.get("component"), row.get("pin"), row.get("net")
        if not all(isinstance(v, str) and v.strip() for v in (component, pin)) or not isinstance(net, str):
            raise ValueError("component and pin must be non-empty strings and net must be a string")
        key = (component, pin)
        value = "" if net == "?" else net
        if key in index and index[key] != value:
            raise ValueError(f"conflicting assignments for {component}.{pin}")
        index[key] = value
    return index


def validate_physical_capture(rows):
    """Validate complete raw graphical inventory without requiring compile evidence."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("native inspection rows must be non-empty")
    summaries = [r for r in rows if isinstance(r, dict) and r.get("kind") == "capture_summary"]
    if len(summaries) != 1 or summaries[0].get("complete") is not True:
        raise ValueError("complete native capture summary is required")
    summary = summaries[0]
    pins = [r for r in rows if isinstance(r, dict) and r.get("kind") == "edit_pin"]
    components = [r for r in rows if isinstance(r, dict) and r.get("kind") == "edit_component"]
    labels = [r for r in rows if isinstance(r, dict) and r.get("kind") == "edit_label"]
    wires = [r for r in rows if isinstance(r, dict) and r.get("kind") == "edit_wire"]
    junctions = [r for r in rows if isinstance(r, dict) and r.get("kind") == "edit_junction"]
    powers = [r for r in rows if isinstance(r, dict) and r.get("kind") == "edit_power"]
    noerc = [r for r in rows if isinstance(r, dict) and r.get("kind") == "edit_noerc"]
    explicit_nc = [r for r in rows if isinstance(r, dict) and r.get("kind") == "unconnected_pin"]
    count_keys = {"pins": len(pins), "components": len(components), "labels": len(labels),
                  "wires": len(wires), "junctions": len(junctions), "powers": len(powers),
                  "noerc": len(noerc), "unconnected_pins": len(explicit_nc)}
    if any(type(summary.get(k)) is not int or summary[k] != count for k, count in count_keys.items()):
        raise ValueError("native capture summary counts are missing or inconsistent")
    if not pins:
        raise ValueError("native physical pin capture is empty")
    for row in pins:
        if (not isinstance(row.get("reference"), str) or not row["reference"] or
                not isinstance(row.get("pin"), str) or not row["pin"]):
            raise ValueError("native physical pin identity is incomplete")
    return {"summary": summary, "pins": pins, "components": components, "labels": labels,
            "wires": wires, "junctions": junctions, "powers": powers, "noerc": noerc,
            "unconnected_pins": explicit_nc}


def connectivity(rows, *, source_hashes=None, adapter_sha256=None):
    """Canonical pin map from complete, successful native compile rows; omission is never NC."""
    physical = validate_physical_capture(rows)
    summary, pins = physical["summary"], physical["pins"]
    assignments = [r for r in rows if isinstance(r, dict) and r.get("kind") == "net_pin"]
    explicit_nc = physical["unconnected_pins"]
    compiled = [r for r in rows if isinstance(r, dict) and r.get("kind") == "compiled"]
    if len(compiled) != 1:
        raise ValueError("exactly one compiled connectivity row is required")
    compile_summaries = [r for r in rows if isinstance(r, dict) and r.get("kind") == "compile_summary"]
    if len(compile_summaries) > 1:
        raise ValueError("compile summary is ambiguous")
    if compile_summaries and (compile_summaries[0].get("success") is not True or
                              compile_summaries[0].get("complete") is not True):
        raise ValueError("native compile failed or is incomplete")
    if any(key in compiled[0] and compiled[0][key] is not True for key in ("success", "complete", "compile_success")):
        raise ValueError("native compile failed or is incomplete")
    net_rows = [r for r in rows if isinstance(r, dict) and r.get("kind") == "net"]
    net_names = [r.get("name") for r in net_rows]
    if any(not isinstance(name, str) or not name for name in net_names) or len(set(net_names)) != len(net_names):
        raise ValueError("compiled net inventory is invalid or contains duplicates")
    if type(compiled[0].get("nets")) is not int or compiled[0]["nets"] != len(net_names):
        raise ValueError("compiled net count does not match the captured net inventory")
    if any(r.get("net") not in set(net_names) for r in assignments):
        raise ValueError("compiled pin assignment references a net missing from the captured net inventory")
    if summary.get("unconnected_pins") == 0 and not explicit_nc and len(assignments) < len(set(
            (p.get("reference"), p.get("pin")) for p in pins)):
        raise ValueError("compiled pin membership is incomplete; explicit NC evidence is required")
    declared = {}
    for row in pins:
        component, pin = row.get("reference"), row.get("pin")
        if not isinstance(component, str) or not component or not isinstance(pin, str) or not pin:
            raise ValueError("native physical pin identity is incomplete")
        key = (component, pin)
        declared.setdefault(key, row)
    actual = {}
    for row in assignments:
        key = (row.get("reference"), str(row.get("pin", "")))
        if key not in declared:
            raise ValueError("compiled net references a pin missing from physical schematic capture")
        net = row.get("net")
        if not isinstance(net, str) or not net:
            raise ValueError("compiled net pin has no net name")
        prior = actual.get(key)
        if prior is not None:
            if prior != net:
                raise ValueError("a physical pin has conflicting compiled net assignments")
            continue
        actual[key] = net
    for row in explicit_nc:
        key = (row.get("reference"), str(row.get("pin", "")))
        if key not in declared or key in actual:
            raise ValueError("explicit unconnected pin evidence is missing, duplicated, or conflicts with netlist")
        actual[key] = ""
    if set(actual) != set(declared):
        raise ValueError("compiled pin membership is incomplete; each pin needs a net or explicit NC row")
    canonical = [{"component": component, "pin": pin, "net": actual.get((component, pin), "")}
                 for component, pin in sorted(declared)]
    digest = hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()
    result = {"schema": "chatgpt-actuator/altium-connectivity/v1", "pins": canonical,
              "count": len(canonical), "sha256": digest, "complete": True}
    if source_hashes is not None:
        if not isinstance(source_hashes, dict) or set(source_hashes) != {"project", "sheet"} or any(
                not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in source_hashes.values()):
            raise ValueError("source_hashes must contain project and sheet SHA-256 values")
        result["source_hashes"] = dict(source_hashes)
    if adapter_sha256 is not None:
        if not isinstance(adapter_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", adapter_sha256):
            raise ValueError("adapter_sha256 must be a SHA-256 value")
        result["adapter_sha256"] = adapter_sha256
    return result


def check_contracts(actual, contracts, require_all_pins=True):
    if not isinstance(actual, dict) or actual.get("complete") is not True:
        raise ValueError("a complete connectivity snapshot is required")
    pins = _pin_index(actual.get("pins"))
    if not isinstance(contracts, list):
        raise ValueError("contracts must be a list")
    if type(require_all_pins) is not bool:
        raise ValueError("require_all_pins must be a boolean")
    findings, seen = [], set()
    for contract in contracts:
        if not isinstance(contract, dict):
            raise ValueError("each contract must be an object")
        component, pin = contract.get("component"), contract.get("pin")
        if not isinstance(component, str) or not component or not isinstance(pin, str) or not pin:
            raise ValueError("contract component and pin are required strings")
        key = (component, pin)
        if key in seen:
            raise ValueError(f"duplicate contract for {component}.{pin}")
        seen.add(key)
        nc = contract.get("nc", False)
        if type(nc) is not bool:
            raise ValueError("nc must be a boolean")
        if nc:
            if "net" in contract or not isinstance(contract.get("reason"), str) or not contract["reason"].strip():
                raise ValueError("intentional NC requires a reason and cannot specify a net")
            expected = ""
        else:
            expected = contract.get("net")
            if not isinstance(expected, str) or not expected.strip() or expected == "?":
                raise ValueError("connected pin contract requires an exact net name")
        if key not in pins:
            findings.append({"code": "MISSING_PIN", "component": component, "pin": pin})
        elif pins[key] != expected:
            findings.append({"code": "NET_MISMATCH", "component": component, "pin": pin,
                             "expected": expected, "actual": pins[key]})
    if require_all_pins:
        findings.extend({"code": "UNSPECIFIED_PIN", "component": c, "pin": p}
                        for c, p in sorted(set(pins) - seen))
    return {"ok": not findings, "status": "failed" if findings else "passed",
            "complete": True, "checked": len(contracts), "findings": findings}


def diff(before, after):
    for snapshot in (before, after):
        if not isinstance(snapshot, dict) or snapshot.get("complete") is not True:
            raise ValueError("both connectivity snapshots must be complete")
    old, new = _pin_index(before.get("pins")), _pin_index(after.get("pins"))
    changes = []
    for component, pin in sorted(set(old) | set(new)):
        key = (component, pin)
        if key not in old:
            changes.append({"code": "PIN_ADDED", "component": component, "pin": pin, "after": new[key]})
        elif key not in new:
            changes.append({"code": "PIN_REMOVED", "component": component, "pin": pin, "before": old[key]})
        elif old[key] != new[key]:
            changes.append({"code": "NET_CHANGED", "component": component, "pin": pin,
                            "before": old[key], "after": new[key]})
    return {"ok": True, "status": "compared", "complete": True, "changes": changes,
            "change_count": len(changes), "unchanged_count": sum(old[k] == new[k] for k in set(old) & set(new))}


_PLACEHOLDERS = {"?", "value", "device", "component", "part", "r?", "c?", "l?", "u?", "j?"}
_ENG = re.compile(r"^\s*(\d+(?:\.\d+)?|\.\d+)\s*([pnumkMGTµu]?)\s*(Ω|ohms?|F|R)?\s*$", re.I)
_EMBEDDED_RESISTANCE = re.compile(r"^\s*(\d+)([RrKkMmGgTt])(\d+)\s*$")
_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
           "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def _value_number(value, quantity=None):
    text = str(value)
    if quantity == "RESISTANCE":
        embedded = _EMBEDDED_RESISTANCE.fullmatch(text)
        if embedded:
            whole, prefix, fraction = embedded.groups()
            multiplier = 1 if prefix.casefold() == "r" else _PREFIX.get(prefix)
            if multiplier is not None:
                return float(whole + "." + fraction) * multiplier, "ohm"
    match = _ENG.fullmatch(str(value))
    if not match:
        return None, None
    number, prefix, explicit_unit = match.groups()
    # Preserve engineering-symbol case: m=10^-3 and M=10^6.
    unit = "f" if explicit_unit and explicit_unit.casefold() == "f" else (
        "ohm" if explicit_unit else None)
    return float(number) * _PREFIX.get(prefix, 1), unit


def _on_segment(point, start, end):
    return ((end[0] - start[0]) * (point[1] - start[1]) ==
            (end[1] - start[1]) * (point[0] - start[0]) and
            min(start[0], end[0]) <= point[0] <= max(start[0], end[0]) and
            min(start[1], end[1]) <= point[1] <= max(start[1], end[1]))


def _within_one_mil_of_segment(point, start, end):
    """Exact integer test for a point no farther than 10,000 internal units (1 mil)."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    px, py = point[0] - start[0], point[1] - start[1]
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return px * px + py * py <= 10000 * 10000
    projection = px * dx + py * dy
    if projection <= 0:
        distance2_num, distance2_den = px * px + py * py, 1
    elif projection >= length2:
        qx, qy = point[0] - end[0], point[1] - end[1]
        distance2_num, distance2_den = qx * qx + qy * qy, 1
    else:
        cross = dx * py - dy * px
        distance2_num, distance2_den = cross * cross, length2
    return distance2_num <= 10000 * 10000 * distance2_den


def _segments(rows):
    segments, points = [], set()
    for row in rows:
        if row.get("kind") != "edit_wire":
            continue
        vertices = row.get("vertices", [])
        for a, b in zip(vertices, vertices[1:]):
            if a == b:
                continue
            segments.append((tuple(a), tuple(b)))
            points.update((tuple(a), tuple(b)))
    for row in rows:
        if row.get("kind") == "edit_junction":
            if isinstance(row.get("x"), int) and isinstance(row.get("y"), int):
                points.add((row["x"], row["y"]))
    nodes = {}
    for segment in segments:
        a, b = segment
        on = [p for p in points if _on_segment(p, a, b)]
        on.sort(key=lambda p: (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2)
        for p in on:
            nodes.setdefault(p, set())
        for p, q in zip(on, on[1:]):
            nodes[p].add(q)
            nodes[q].add(p)
    junctions = {(r.get("x"), r.get("y")) for r in rows if r.get("kind") == "edit_junction"}
    # Crossings conduct only when an explicit junction split both wires.
    return segments, nodes


def _connected_coordinates(rows):
    segments, graph = _segments(rows)
    parent = {point: point for point in graph}
    def find(point):
        while parent[point] != point:
            parent[point] = parent[parent[point]]
            point = parent[point]
        return point
    def union(a, b):
        parent[find(a)] = find(b)
    for point, neighbors in graph.items():
        for neighbor in neighbors:
            union(point, neighbor)
    attached = {}
    for row in rows:
        if row.get("kind") not in {"edit_label", "edit_power", "edit_pin"}:
            continue
        x, y = row.get("x"), row.get("y")
        if type(x) is int and type(y) is int:
            coordinate = (x, y)
            if coordinate in parent:
                root = find(coordinate)
            else:
                on_segments = [(a, b) for a, b in segments if _on_segment(coordinate, a, b)]
                # A label on a bare crossing does not prove which wire it names.
                if len(on_segments) != 1:
                    if any(r.get("kind") == "edit_pin" and (r.get("x"), r.get("y")) == coordinate for r in rows):
                        parent[coordinate] = coordinate
                        root = coordinate
                    else:
                        continue
                else:
                    root = find(on_segments[0][0])
            attached.setdefault(root, []).append(row)
    return attached, segments, {point: find(point) for point in parent}


def _geometry_is_complete(rows):
    sheets = [r for r in rows if r.get("kind") == "edit_sheet"]
    if len(sheets) != 1 or any(type(sheets[0].get(k)) is not int or sheets[0][k] <= 0
                                for k in ("width", "height", "grid")):
        return False, "sheet dimensions or snap grid are missing"
    for row in rows:
        kind = row.get("kind")
        if kind == "edit_component" and (not isinstance(row.get("reference"), str) or
                not isinstance(row.get("value"), str) or any(type(row.get(k)) is not int for k in ("x", "y"))):
            return False, "component geometry or identity is incomplete"
        if kind == "edit_pin" and (not isinstance(row.get("reference"), str) or not row["reference"] or
                not isinstance(row.get("pin"), str) or not row["pin"] or
                not isinstance(row.get("electrical_type"), str) or
                any(type(row.get(k)) is not int for k in ("x", "y"))):
            return False, "physical pin geometry or identity is incomplete"
        if kind in {"edit_label", "edit_power", "edit_junction"} and any(
                type(row.get(k)) is not int for k in ("x", "y")):
            return False, "label, power-port, or junction geometry is incomplete"
        if kind in {"edit_label", "edit_power"} and not isinstance(row.get("name"), str):
            return False, "named schematic object has no text"
        if kind == "edit_wire":
            vertices = row.get("vertices")
            if not isinstance(vertices, list) or len(vertices) < 2 or any(
                    not isinstance(v, list) or len(v) != 2 or any(type(c) is not int for c in v) for v in vertices):
                return False, "wire vertices are incomplete"
    return True, None


def lint(rows, intent=None):
    if not isinstance(rows, list) or not rows:
        raise ValueError("native inspection rows must be non-empty")
    findings, incomplete = [], False
    if any(not isinstance(r, dict) for r in rows):
        return {"ok": False, "status": "incomplete", "findings": [{"code": "INCOMPLETE_CAPTURE",
                                                                         "detail": "inspection rows must be objects"}]}
    if any(r.get("kind") == "edit_blocker" for r in rows):
        return {"ok": False, "status": "incomplete", "complete": False,
                "findings": [{"code": "UNSUPPORTED_GEOMETRY", "detail": "Native geometric analysis cannot certify edit_blocker objects"}]}
    try:
        validate_physical_capture(rows)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "status": "incomplete", "findings": [{"code": "INCOMPLETE_CAPTURE", "detail": str(exc)}]}
    geometry_complete, geometry_error = _geometry_is_complete(rows)
    if not geometry_complete:
        return {"ok": False, "status": "incomplete", "findings": [{"code": "INCOMPLETE_CAPTURE", "detail": geometry_error}]}
    def finding(code, severity="error", **data):
        findings.append({"code": code, "severity": severity, **data})
    snapshot = None
    try:
        snapshot = connectivity(rows)
    except (TypeError, ValueError) as exc:
        finding("INCOMPLETE_CONNECTIVITY", severity="incomplete", detail=str(exc))
        incomplete = True
    components = [r for r in rows if r.get("kind") == "edit_component"]
    by_ref = {}
    for row in components:
        reference = str(row.get("reference", ""))
        by_ref.setdefault(reference.casefold(), []).append(row)
        if str(row.get("value", "")).strip().casefold() in _PLACEHOLDERS:
            finding("PLACEHOLDER_VALUE", reference=reference)
    for references in by_ref.values():
        if len(references) > 1:
            finding("DUPLICATE_DESIGNATOR", reference=references[0].get("reference"))
    sheet = next((r for r in rows if r.get("kind") == "edit_sheet"), {})
    grid = sheet.get("grid")
    if type(grid) is int and grid > 0:
        for row in rows:
            if row.get("kind") == "edit_component" and all(type(row.get(k)) is int for k in ("x", "y")):
                if row["x"] % grid or row["y"] % grid:
                    finding("OFF_GRID_COMPONENT", reference=row.get("reference"), x=row["x"], y=row["y"])
    elif any(r.get("kind") == "edit_component" for r in rows):
        finding("GRID_UNKNOWN", severity="incomplete")
        incomplete = True
    attached, segments, graph_roots = _connected_coordinates(rows)
    pin_rows = [r for r in rows if r.get("kind") == "edit_pin"]
    for root, objects in attached.items():
        names = {str(r.get("name", "")).strip().casefold(): r for r in objects
                 if r.get("kind") in {"edit_label", "edit_power"} and str(r.get("name", "")).strip()}
        if len(names) > 1:
            finding("CONFLICTING_NET_NAMES", names=sorted(names))
    for row in rows:
        if row.get("kind") in {"edit_label", "edit_power"}:
            name = str(row.get("name", "")).strip()
            coordinate = (row.get("x"), row.get("y"))
            directly_pinned = coordinate in {(r.get("x"), r.get("y")) for r in pin_rows}
            crossing = (not any(r.get("kind") == "edit_junction" and
                                 (r.get("x"), r.get("y")) == coordinate for r in rows) and
                        not any(coordinate in (a, b) for a, b in segments) and
                        sum(_on_segment(coordinate, a, b) for a, b in segments) > 1)
            if name and crossing:
                finding("AMBIGUOUS_NET_LABEL_ATTACHMENT", name=name, x=coordinate[0], y=coordinate[1])
            elif name and not directly_pinned and not any(_on_segment(coordinate, a, b) for a, b in segments):
                finding("ORPHAN_NET_LABEL" if row["kind"] == "edit_label" else "ORPHAN_POWER_PORT", name=name)
    # Catch near misses against entire segments, not just their endpoints.
    for pin in pin_rows:
        x, y = pin.get("x"), pin.get("y")
        on_wire = type(x) is int and type(y) is int and any(_on_segment((x, y), a, b) for a, b in segments)
        if type(x) is int and type(y) is int and not on_wire:
            if any(_within_one_mil_of_segment((x, y), a, b) for a, b in segments):
                finding("PIN_NEAR_MISS", component=pin.get("reference"), pin=pin.get("pin"))
    wire_endpoints = {point for segment in segments for point in segment}
    for endpoint in wire_endpoints:
        if any(not _on_segment(endpoint, a, b) and _within_one_mil_of_segment(endpoint, a, b) and
               graph_roots.get(endpoint) != graph_roots.get(a)
               for a, b in segments):
            finding("WIRE_NEAR_MISS", x=endpoint[0], y=endpoint[1])
    pins_by_net = {}
    if snapshot is not None:
        assignments = [r for r in rows if r.get("kind") == "net_pin"]
        for row in assignments:
            pins_by_net.setdefault(row.get("net"), set()).add((row.get("reference"), str(row.get("pin"))))
        for name, pins in pins_by_net.items():
            if len(pins) == 1:
                finding("SINGLE_PIN_NET", severity="warning", net=name)
    if intent is None:
        intent = {}
    if not isinstance(intent, dict):
        raise ValueError("intent must be an object")
    unsupported_intent = set(intent) - {"contracts", "decoupling", "pullups"}
    if unsupported_intent:
        raise ValueError("unsupported intent fields: " + ", ".join(sorted(unsupported_intent)))
    if "contracts" in intent and snapshot is not None:
        try:
            contracts = check_contracts(snapshot, intent["contracts"], require_all_pins=False)
            findings.extend({"code": f["code"], "severity": "error", **{k: v for k, v in f.items() if k != "code"}}
                            for f in contracts["findings"])
        except (TypeError, ValueError) as exc:
            finding("INTENT_INVALID", severity="incomplete", check="contracts", detail=str(exc))
            incomplete = True
    # A No-ERC object is safe only when it sits on physical pin(s) explicitly
    # declared NC. Matching by location also catches legacy/unbound directives.
    nc_contracts = set()
    contract_rows = intent.get("contracts", [])
    if isinstance(contract_rows, list):
        for contract in contract_rows:
            if (isinstance(contract, dict) and contract.get("nc") is True and
                    isinstance(contract.get("component"), str) and isinstance(contract.get("pin"), str) and
                    isinstance(contract.get("reason"), str) and contract["reason"].strip()):
                nc_contracts.add((contract["component"], contract["pin"]))
    physical_pins = {}
    for pin in pin_rows:
        x, y = pin.get("x"), pin.get("y")
        if type(x) is int and type(y) is int:
            physical_pins.setdefault((x, y), []).append(pin)
    for directive in (r for r in rows if r.get("kind") == "edit_noerc"):
        x, y = directive.get("x"), directive.get("y")
        if type(x) is not int or type(y) is not int:
            finding("UNBOUND_NOERC", severity="incomplete", uid=directive.get("uid"),
                    detail="No-ERC position is missing or malformed")
            incomplete = True
            continue
        attached = physical_pins.get((x, y), [])
        if not attached:
            finding("UNBOUND_NOERC", uid=directive.get("uid"), x=x, y=y,
                    detail="No physical pin is located at this No-ERC directive")
            continue
        uncontracted = [pin for pin in attached
                        if (pin.get("reference"), str(pin.get("pin", ""))) not in nc_contracts]
        if uncontracted:
            finding("UNBOUND_NOERC", uid=directive.get("uid"), x=x, y=y,
                    pins=[{"component": pin.get("reference"), "pin": str(pin.get("pin", ""))}
                          for pin in uncontracted],
                    detail="Every physical pin at a No-ERC directive must have an explicit NC contract with a reason")
    for family, check in (("decoupling", _check_decoupling), ("pullups", _check_pullups)) if snapshot is not None else ():
        if family not in intent:
            finding("INTENT_REQUIRED", severity="incomplete", check=family)
            incomplete = True
            continue
        rules = intent[family]
        if not isinstance(rules, list):
            finding("INTENT_INVALID", severity="incomplete", check=family, detail="rule family must be a list")
            incomplete = True
        else:
            for rule in rules:
                try:
                    check(rule, pins_by_net, components, finding)
                except (KeyError, TypeError, ValueError) as exc:
                    finding("INTENT_INVALID", severity="incomplete", check=family, detail=str(exc))
                    incomplete = True
    status = "incomplete" if incomplete else ("failed" if any(f["severity"] == "error" for f in findings) else "passed")
    return {"ok": status == "passed", "status": status,
            "complete": not incomplete, "finding_count": len(findings), "findings": findings}


def _component_pins(pins_by_net, net, resistor):
    reference = resistor["component"]
    numbers = set(map(str, resistor["pins"]))
    return any(ref == reference and pin in numbers for ref, pin in pins_by_net.get(net, set()))


def _check_decoupling(rule, pins_by_net, components, finding):
    power, cap = rule["power"], rule["capacitor"]
    power_net, ground_net = rule["power_net"], rule["ground_net"]
    if (power["component"], str(power["pin"])) not in pins_by_net.get(power_net, set()):
        finding("REQUIRED_POWER_PIN_NET", check="decoupling", net=power_net, **power)
    pins = list(map(str, cap["pins"]))
    pwr_pins = {p for p in pins if (cap["component"], p) in pins_by_net.get(power_net, set())}
    gnd_pins = {p for p in pins if (cap["component"], p) in pins_by_net.get(ground_net, set())}
    if len(pins) != 2 or len(set(pins)) != 2 or not pwr_pins or not gnd_pins or pwr_pins == gnd_pins:
        finding("MISSING_DECOUPLING_CAP", check="decoupling", component=cap["component"], power_net=power_net, ground_net=ground_net)
        return
    _required_value(cap["component"], rule, components, "CAPACITANCE", finding)


def _check_pullups(rule, pins_by_net, components, finding):
    resistor, signal, power = rule["resistor"], rule["signal_net"], rule["power_net"]
    pins = list(map(str, resistor["pins"]))
    on_signal = [p for p in pins if (resistor["component"], p) in pins_by_net.get(signal, set())]
    on_power = [p for p in pins if (resistor["component"], p) in pins_by_net.get(power, set())]
    if len(pins) != 2 or len(set(pins)) != 2 or not on_signal or not on_power or set(on_signal) == set(on_power):
        finding("MISSING_PULLUP", check="pullups", component=resistor["component"], signal_net=signal, power_net=power)
        return
    _required_value(resistor["component"], rule, components, "RESISTANCE", finding)


def _required_value(reference, rule, components, quantity, finding):
    component = next((r for r in components if r.get("reference") == reference), None)
    if component is None:
        raise ValueError("intent component is not present: " + reference)
    value, unit = _value_number(component.get("value", ""), quantity)
    expected_unit = "f" if quantity == "CAPACITANCE" else "ohm"
    if unit is not None and unit != expected_unit:
        finding("REQUIRED_COMPONENT_VALUE", check=quantity.casefold(), component=reference,
                actual=component.get("value", ""), min_value=rule.get("min_value"), max_value=rule.get("max_value"))
        return
    minimum, maximum = rule.get("min_value"), rule.get("max_value")
    if type(minimum) not in (int, float) or type(maximum) not in (int, float) or not (
            math.isfinite(minimum) and math.isfinite(maximum) and 0 < minimum <= maximum):
        raise ValueError("min_value and max_value must be positive finite base-unit values")
    if value is None or not minimum <= value <= maximum:
        finding("REQUIRED_COMPONENT_VALUE", check=quantity.casefold(), component=reference,
                actual=component.get("value", ""), min_value=minimum, max_value=maximum)


def erc(rows):
    if not isinstance(rows, list):
        raise ValueError("native ERC rows must be a list")
    summaries = [r for r in rows if isinstance(r, dict) and r.get("kind") == "erc_summary"]
    if len(summaries) != 1 or summaries[0].get("complete") is not True or \
       summaries[0].get("configuration_complete") is not True or \
       not isinstance(summaries[0].get("config"), dict) or not summaries[0]["config"]:
        return {"ok": False, "status": "incomplete", "complete": False, "violations": [],
                "message": "Native ERC evidence is unavailable or incomplete"}
    summary = summaries[0]
    violations = [r for r in rows if isinstance(r, dict) and r.get("kind") == "erc_violation"]
    if type(summary.get("violation_count")) is not int or summary["violation_count"] != len(violations):
        return {"ok": False, "status": "incomplete", "complete": False, "violations": [],
                "message": "Native ERC violation count is inconsistent"}
    normalized = []
    for row in violations:
        if not all(isinstance(row.get(k), str) and row[k] for k in ("message", "severity", "source")) or type(row.get("suppressed")) is not bool:
            return {"ok": False, "status": "incomplete", "complete": False, "violations": [],
                    "message": "Native ERC violation fields are incomplete"}
        if row["severity"].casefold() not in {"warning", "error", "fatal", "no_report"}:
            return {"ok": False, "status": "incomplete", "complete": False, "violations": [],
                    "message": "Native ERC severity is unknown"}
        normalized.append({k: row[k] for k in ("message", "severity", "source", "suppressed")})
    errors = [v for v in normalized if not v["suppressed"] and v["severity"].casefold() in {"error", "fatal"}]
    compile_failed = any(
        (r.get("kind") == "compile_summary" and
         (r.get("success") is not True or r.get("complete") is not True)) or
        (r.get("kind") in {"compiled", "erc_summary"} and
         any(key in r and r[key] is not True for key in ("success", "complete", "compile_success")))
        for r in rows if isinstance(r, dict))
    if compile_failed:
        captured_errors = [v for v in normalized if v["severity"].casefold() in {"error", "fatal"}]
        return {"ok": False, "status": "failed" if captured_errors else "incomplete",
                "complete": bool(captured_errors), "violation_count": len(normalized),
                "suppressed_count": sum(v["suppressed"] for v in normalized), "violations": normalized,
                "configuration": summary.get("config"),
                "message": "Native compile failed; ERC errors were captured" if captured_errors else
                           "Native compile failed without captured ERC errors"}
    return {"ok": not errors, "status": "failed" if errors else "passed", "complete": True,
            "violation_count": len(normalized), "suppressed_count": sum(v["suppressed"] for v in normalized),
            "violations": normalized, "configuration": summary.get("config")}
