"""Copy-on-write edits for existing, standalone, one-sheet Altium projects."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import uuid
import unicodedata

from ..atomic_files import file_lock
from .eda_altium import _hash, _json, _pas, _write


_REF = re.compile(r"[A-Za-z][A-Za-z0-9_]*[0-9][A-Za-z0-9_]*\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identity(row):
    return _digest(row)


def _copy_new(source, target):
    """Copy bytes into a newly created file; never overwrite staged content."""
    source, target = Path(source), Path(target)
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())


def _canonical(rows):
    """Retain only stable detailed rows; compiled connectivity is checked separately."""
    kinds = {"edit_component", "edit_label", "edit_wire", "edit_junction", "edit_blocker", "edit_sheet", "edit_sheet_parameter", "edit_pin", "edit_power", "edit_noerc"}
    return sorted((row for row in rows if row.get("kind") in kinds), key=lambda r: _json(r))


def _nets(rows):
    nets = {}
    compiled = False
    for row in rows:
        if row.get("kind") == "compiled":
            compiled = True
        elif row.get("kind") == "net":
            nets.setdefault(str(row["name"]).casefold(), set())
        elif row.get("kind") == "net_pin":
            nets.setdefault(str(row["net"]).casefold(), set()).add(
                (str(row["reference"]).casefold(), str(row["pin"])))
    if not compiled:
        raise ValueError("Compiled Altium connectivity evidence is missing")
    return nets


def _diagnostic_compile(rows):
    if not isinstance(rows, list):
        return False
    summaries = [row for row in rows if row.get("kind") == "compile_summary"]
    return len(summaries) == 1 and summaries[0].get("complete") is True and summaries[0].get("success") is False


def _connectivity_evidence(rows, *, source_hashes=None, adapter_sha256=None):
    from .eda_altium_analysis import connectivity, validate_physical_capture
    if not _diagnostic_compile(rows):
        return connectivity(rows, source_hashes=source_hashes, adapter_sha256=adapter_sha256)
    physical = validate_physical_capture(rows)
    result = {"schema": "chatgpt-actuator/altium-connectivity/v1", "status": "incomplete", "complete": False,
              "pins": [], "count": 0, "physical_pin_count": len(physical["pins"]), "reason": "native_compilation_failed"}
    if source_hashes is not None:
        result["source_hashes"] = source_hashes
    if adapter_sha256 is not None:
        result["adapter_sha256"] = adapter_sha256
    return result


class ExistingSheetEditor:
    """API layer over AltiumAdapter's secured AD26 dispatcher and private state."""

    def __init__(self, adapter):
        self.adapter = adapter

    def _project(self, project_path):
        adapter = self.adapter
        adapter._require()
        project = adapter.guard.resolve_existing(project_path)
        if not project.is_file() or project.suffix.casefold() != ".prjpcb":
            raise ValueError("Expected a .PrjPcb file")
        folder = project.parent
        # AD project files list documents in [DocumentN] blocks. Any second
        # document is a hierarchy/dependency we cannot safely copy in v1.
        from .eda_altium_configuration import _decode
        text = _decode(project.read_bytes())
        doc_paths = re.findall(r"(?im)^DocumentPath\s*=\s*(.+?)\s*$", text)
        if len(doc_paths) != 1:
            raise ValueError("Only a standalone project with exactly one declared document is supported")
        raw_sheet = Path(doc_paths[0].strip().strip('"'))
        if raw_sheet.is_absolute() or raw_sheet.suffix.casefold() != ".schdoc":
            raise ValueError("Project document must be one relative .SchDoc")
        expected = (folder / raw_sheet).resolve(strict=True)
        expected = adapter.guard.resolve_existing(expected)
        if not expected.is_file() or folder.resolve() not in expected.parents:
            raise ValueError("Project schematic must be a local document beneath the project folder")
        return project, expected

    def _inspect_rows(self, project, sheet, key=None, *, close_after=False):
        # PrepareExisting rejects a sheet that is already open and dirty before
        # any compile or inspection can observe stale saved bytes.
        hashes = (_hash(project), _hash(sheet))
        try:
            prepared = self.adapter._run(f"PrepareExisting({_pas(project)}, {_pas(sheet)});", kind="existing_prepare",
                                         request_key=(key + ":prepare") if key else None,
                                         payload={"project": str(project), "sheet": str(sheet), "hashes": hashes})
            if prepared["status"] != "succeeded":
                return prepared
            if hashes != (_hash(project), _hash(sheet)):
                raise RuntimeError("Files changed during Altium project preparation")
            command = f"InspectExisting({_pas(project)}, {_pas(sheet)});"
            if close_after:
                command += f"\nCloseOwnedProject({_pas(project)}, {_pas(sheet)});"
            result = self.adapter._run(command, kind="existing_inspect",
                                       request_key=(key + ":inspect") if key else None,
                                       payload={"project": str(project), "sheet": str(sheet), "hashes": hashes,
                                                "close_after": close_after})
            result["prepare_operation_id"] = prepared.get("operation_id")
        finally:
            if hashes != (_hash(project), _hash(sheet)):
                raise RuntimeError("Files changed during Altium inspection")
        rows = self.adapter._rows(result)
        if rows is not None:
            self._validate_native_rows(rows, project, sheet)
            from .eda_altium_configuration import annotate
            loads = [row for row in prepared.get("rows", []) if row.get("kind") == "project_configuration_reload"]
            prepare_headers = [row.get("operation_id") for row in prepared.get("rows", []) if "operation_id" in row]
            inspect_headers = [row.get("operation_id") for row in rows if "operation_id" in row]
            fingerprint = self.adapter._adapter_fingerprint()
            reload_evidence = None
            if (len(loads) == 1 and prepare_headers == [prepared.get("operation_id")]
                    and inspect_headers == [result.get("operation_id")]
                    and prepared.get("adapter_sha256") == result.get("adapter_sha256") == fingerprint):
                reload_evidence = {key: loads[0].get(key) for key in
                                   ("kind", "complete", "previously_open", "project_path", "sheet_path")}
                reload_evidence.update(project_sha256=hashes[0], sheet_sha256=hashes[1],
                    prepare_operation_id=prepared["operation_id"], inspection_operation_id=result["operation_id"],
                    adapter_sha256=fingerprint)
            rows = annotate(rows, project, reload_evidence=reload_evidence)
            result["rows"] = rows
            result["configuration_reload_evidence"] = reload_evidence
            if not any(r.get("kind") == "edit_sheet" for r in rows):
                raise ValueError("Detailed schematic inspection is incomplete (sheet evidence missing)")
            if not _diagnostic_compile(rows):
                _nets(rows)
            inspected_hashes = {"project": _hash(project), "sheet": _hash(sheet)}
            if inspected_hashes != {"project": hashes[0], "sheet": hashes[1]}:
                raise RuntimeError("Files changed while validating Altium inspection")
            result["inspected_hashes"] = inspected_hashes
        return result

    @staticmethod
    def _validate_native_rows(rows, project, sheet):
        project_rows = [r for r in rows if r.get("kind") == "project"]
        documents = [r for r in rows if r.get("kind") == "document"]
        if len(project_rows) != 1 or int(project_rows[0].get("documents", -1)) != 1:
            raise ValueError("Only a compiled project with exactly one logical document is supported")
        if os.path.normcase(str(Path(project_rows[0].get("path", "")).resolve(strict=False))) != os.path.normcase(str(Path(project).resolve(strict=False))):
            raise ValueError("Compiled project evidence refers to a different .PrjPcb file")
        if len(documents) != 1:
            raise ValueError("Compiled project document evidence is missing or ambiguous")
        if os.path.normcase(str(Path(documents[0].get("path", "")).resolve(strict=False))) != os.path.normcase(str(Path(sheet).resolve(strict=False))):
            raise ValueError("Compiled project resolves to a different schematic document")
        if _diagnostic_compile(rows):
            if any(row.get("kind") in {"compiled", "net", "net_pin", "unconnected_pin"} for row in rows):
                raise ValueError("Failed compilation cannot certify a connectivity graph")
            from .eda_altium_analysis import validate_physical_capture
            validate_physical_capture(rows)
            return
        component_count = len([r for r in rows if r.get("kind") == "edit_component"])
        if int(documents[0].get("components", -1)) != component_count:
            raise ValueError("Detailed component inspection does not match compiled project evidence")
        compiled = [r for r in rows if r.get("kind") == "compiled"]
        net_rows = [r for r in rows if r.get("kind") == "net"]
        if len(compiled) != 1 or int(compiled[0].get("nets", -1)) != len(net_rows) or int(documents[0].get("nets", -1)) != len(net_rows):
            raise ValueError("Compiled net count evidence is incomplete or inconsistent")

    def inspect(self, project_path):
        self.adapter._require()
        project, sheet = self._project(project_path)
        baseline = {"project": _hash(project), "sheet": _hash(sheet)}
        operation_key = "edit-inspect:" + uuid.uuid4().hex
        result = self._inspect_rows(project, sheet, operation_key)
        rows = self.adapter._rows(result)
        if rows is None:
            return result
        if baseline != {"project": _hash(project), "sheet": _hash(sheet)}:
            raise RuntimeError("Source files changed during inspection")
        owner = self.adapter._owner()
        snapshot_id = uuid.uuid4().hex
        folder = self.adapter.state_guard.resolve_target(self.adapter.state_root / "edit-snapshots")
        folder.mkdir(parents=True, exist_ok=True)
        path = self.adapter.state_guard.resolve_target(folder / (snapshot_id + ".json"))
        snapshot = {"schema": "chatgpt-actuator/altium-existing-snapshot/v1", "snapshot_id": snapshot_id,
                    "owner": owner, "project": str(project), "sheet": str(sheet), "source_hashes": baseline,
                    "rows": rows, "created_at": __import__("time").time(), "operation_id": result["operation_id"],
                    "operation_ids": [prepared for prepared in (result.get("prepare_operation_id"), result.get("operation_id")) if prepared]}
        from .eda_altium_analysis import erc
        snapshot["adapter_sha256"] = result.get("adapter_sha256")
        snapshot["connectivity"] = _connectivity_evidence(rows, source_hashes=baseline, adapter_sha256=result.get("adapter_sha256"))
        snapshot["erc"] = erc(rows)
        snapshot["configuration_reload_evidence"] = result.get("configuration_reload_evidence")
        _write(path, snapshot)
        return {"status": "succeeded", "snapshot_id": snapshot_id, "project_path": str(project),
                "sheet_path": str(sheet), "source_hashes": baseline, "components": [r for r in rows if r.get("kind") == "edit_component"],
                "labels": [r for r in rows if r.get("kind") == "edit_label"],
                "sheet_parameters": [r for r in rows if r.get("kind") == "edit_sheet_parameter"],
                "wires": [r for r in rows if r.get("kind") == "edit_wire"],
                "junctions": [r for r in rows if r.get("kind") == "edit_junction"],
                "blockers": [r for r in rows if r.get("kind") == "edit_blocker"],
                "nets": [r for r in rows if r.get("kind") in {"net", "net_pin", "compiled"}],
                "pins": [r for r in rows if r.get("kind") == "edit_pin"],
                "power_ports": [r for r in rows if r.get("kind") == "edit_power"],
                "noerc": [r for r in rows if r.get("kind") == "edit_noerc"],
                "grid": next((r.get("grid") for r in rows if r.get("kind") == "edit_sheet"), None),
                "connectivity": snapshot["connectivity"], "erc": snapshot["erc"],
                "compile_evidence": next((row for row in rows if row.get("kind") == "compile_summary"), None),
                "coordinate_units": "Altium internal coordinates (10000 per mil)",
                "operation_id": result["operation_id"], "operation_ids": snapshot["operation_ids"]}

    def check_pin_contracts(self, snapshot_id, contracts, require_all_pins=True):
        from .eda_altium_analysis import connectivity, check_contracts
        snapshot = self._snapshot(snapshot_id)
        return check_contracts(connectivity(snapshot["rows"]), contracts, require_all_pins)

    def diff_connectivity(self, before_snapshot_id, after_snapshot_id):
        from .eda_altium_analysis import connectivity, diff
        before, after = self._snapshot(before_snapshot_id), self._snapshot(after_snapshot_id)
        return diff(connectivity(before["rows"]), connectivity(after["rows"]))

    def lint(self, snapshot_id, intent=None):
        from .eda_altium_analysis import lint
        return lint(self._snapshot(snapshot_id)["rows"], intent)

    def erc(self, project_path):
        result = self.inspect(project_path)
        return result if result.get("status") != "succeeded" else dict(result["erc"], snapshot_id=result["snapshot_id"], operation_ids=result["operation_ids"])

    def _snapshot(self, snapshot_id):
        self.adapter._require()
        if not isinstance(snapshot_id, str) or not _ID.fullmatch(snapshot_id):
            raise ValueError("Invalid snapshot_id")
        path = self.adapter.state_guard.resolve_existing(self.adapter.state_root / "edit-snapshots" / (snapshot_id + ".json"))
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("owner") != self.adapter._owner():
            raise PermissionError("Snapshot belongs to another owner")
        if data.get("snapshot_id") != snapshot_id:
            raise ValueError("Snapshot identity mismatch")
        return data

    @staticmethod
    def _normalize(changes, rows):
        if not isinstance(changes, list) or not changes:
            raise ValueError("changes must be a non-empty list")
        components = [r for r in rows if r.get("kind") == "edit_component"]
        labels = [r for r in rows if r.get("kind") == "edit_label"]
        normalized, touched = [], set()
        rename_sources, rename_targets = set(), set()
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                raise ValueError(f"change {index} must be an object")
            op = change.get("op")
            if op == "set_component_value":
                allowed = {"op", "reference", "expected_value", "value"}
                if set(change) != allowed:
                    raise ValueError("set_component_value requires exactly op, reference, expected_value and value")
                ref, old, value = change.get("reference"), change.get("expected_value"), change.get("value")
                if not isinstance(ref, str) or not _REF.fullmatch(ref):
                    raise ValueError("Invalid component reference")
                if not isinstance(old, str) or not isinstance(value, str) or not value.strip() or len(value) > 256:
                    raise ValueError("expected_value/value must be strings; value must be 1..256 characters")
                matches = [r for r in components if str(r.get("reference", "")).casefold() == ref.casefold()]
                if len(matches) != 1:
                    raise ValueError("Component selector is missing or ambiguous: " + ref)
                row = matches[0]
                if int(row.get("part_count", 0)) != 1:
                    raise ValueError("Editing multipart components is unsupported")
                if row.get("value") != old:
                    raise ValueError("Component value no longer matches the snapshot")
                key = (op, ref.casefold())
                if key in touched:
                    raise ValueError("Duplicate component edit")
                touched.add(key)
                normalized.append({"op": op, "reference": row["reference"], "expected_value": old, "value": value,
                                   "selector": _identity({k: row.get(k) for k in ("reference", "symbol", "part", "part_count", "uid", "x", "y", "rotation", "mirrored", "value")})})
            elif op == "rename_net":
                allowed = {"op", "old_name", "new_name"}
                if set(change) != allowed:
                    raise ValueError("rename_net requires exactly op, old_name and new_name")
                old, new = change.get("old_name"), change.get("new_name")
                if not isinstance(old, str) or not old.strip() or not isinstance(new, str) or not new.strip() or len(old) > 256 or len(new) > 256:
                    raise ValueError("Net names must be non-empty strings up to 256 characters")
                if any(unicodedata.category(char) == "Cc" for char in old + new):
                    raise ValueError("Net names cannot contain control characters")
                if old.casefold() == new.casefold():
                    raise ValueError("New net name must differ from the old name")
                if old.casefold() in rename_targets or new.casefold() in rename_targets or new.casefold() in rename_sources:
                    raise ValueError("Overlapping net rename chains and duplicate rename targets are unsupported")
                blockers = [r for r in rows if r.get("kind") in {"edit_blocker", "edit_power"}]
                if blockers:
                    raise ValueError("Net rename is blocked by ports, power ports, buses, or other named objects")
                selected = [r for r in labels if str(r.get("name", "")).casefold() == old.casefold()]
                if not selected:
                    raise ValueError("No net labels match old_name")
                compiled_names = {str(r.get("name", "")).casefold() for r in rows if r.get("kind") == "net"}
                if old.casefold() not in compiled_names:
                    raise ValueError("Net label name is absent from compiled connectivity")
                if new.casefold() in compiled_names and new.casefold() != old.casefold():
                    raise ValueError("New net name already exists in compiled connectivity")
                key = (op, old.casefold())
                if key in touched:
                    raise ValueError("Duplicate net rename")
                touched.add(key)
                rename_sources.add(old.casefold())
                rename_targets.add(new.casefold())
                normalized.append({"op": op, "old_name": old, "new_name": new,
                                   "label_selectors": sorted(_identity(r) for r in selected)})
            else:
                raise ValueError("Supported edits are set_component_value and rename_net")
        return normalized

    def preview(self, snapshot_id, changes, output_dir, *, _allow_existing=False, expected_connectivity=None, intent=None):
        self.adapter._require(edit=True)
        snapshot = self._snapshot(snapshot_id)
        project, sheet = self._project(snapshot["project"])
        if str(project) != snapshot["project"] or str(sheet) != snapshot["sheet"]:
            raise ValueError("Snapshot project paths no longer resolve identically")
        if snapshot["source_hashes"] != {"project": _hash(project), "sheet": _hash(sheet)}:
            raise RuntimeError("Source project changed since snapshot inspection")
        destination = self.adapter.guard.resolve_target(output_dir)
        if destination.exists() and not _allow_existing:
            raise FileExistsError("Output directory already exists: " + str(destination))
        normalized, body = self._binding(snapshot, changes, destination, expected_connectivity, intent)
        public_changes = [{key: value for key, value in change.items() if key not in {"selector", "label_selectors"}}
                          for change in normalized]
        legacy_only = all(change["op"] in {"set_component_value", "rename_net"} for change in normalized)
        return {"status": "preview", "snapshot_id": snapshot_id, "changes": public_changes,
                "before": snapshot["rows"], "after": self._expected_after(snapshot["rows"], public_changes) if legacy_only else None,
                "after_status": "predicted" if legacy_only else "requires_native_readback",
                "output_dir": str(destination), "preview_digest": _digest(body),
                "expected_connectivity": expected_connectivity, "intent": intent,
                "limitations": ["Single-sheet standalone .PrjPcb only", "No hierarchy/bus edits; moves do not stretch wires"]}

    def _binding(self, snapshot, changes, destination, expected_connectivity=None, intent=None):
        if expected_connectivity is not None and (not isinstance(expected_connectivity, list) or not expected_connectivity
                                                 or any(not isinstance(c, dict) for c in expected_connectivity)):
            raise ValueError("expected_connectivity must be a non-empty list of pin contracts")
        if expected_connectivity is not None and any(not isinstance(row.get(key), str) or not row[key].strip()
                                                     for row in expected_connectivity for key in ("component", "pin")):
            raise ValueError("Every expected pin contract needs a non-empty component and pin string")
        project = Path(snapshot["project"])
        if destination == project.parent or destination in project.parents or project.parent in destination.parents:
            raise ValueError("Output directory must not overlap the source project")
        if destination.drive.casefold() != project.drive.casefold():
            raise ValueError("Staging and output must be on the same volume")
        from .eda_altium_changes import normalize
        normalized = normalize(changes, snapshot["rows"], self.adapter, self._normalize)
        structural = any(c["op"] not in {"set_component_value", "rename_net", "set_component_parameter"} for c in normalized)
        compile_failed = any(row.get("kind") == "compile_summary" and row.get("success") is False for row in snapshot["rows"])
        if compile_failed and expected_connectivity is None:
            raise ValueError("Repairing an uncompiled schematic requires the complete expected pin-to-net table")
        if structural or expected_connectivity is not None:
            from .eda_altium_analysis import validate_physical_capture, check_contracts
            if structural and any(r.get("kind") == "edit_blocker" for r in snapshot["rows"]):
                raise ValueError("Topology edits are blocked by unsupported schematic objects")
            validate_physical_capture(snapshot["rows"])
            if not isinstance(expected_connectivity, list) or not expected_connectivity:
                raise ValueError("Topology edits require the complete expected pin-to-net table")
            target_pins = [dict(row) for row in snapshot["rows"] if row.get("kind") == "edit_pin"]
            components = [row for row in snapshot["rows"] if row.get("kind") == "edit_component"]
            for change in normalized:
                if change["op"] == "remove_object" and change["object_type"] == "component":
                    reference = change["expected"]["reference"]
                    matching = [row for row in target_pins if row["reference"] == reference]
                    if any(not row.get("component_uid") for row in matching):
                        if sum(row.get("reference") == reference for row in components) != 1:
                            raise ValueError("Reinspect multipart components to capture each pin's parent UID")
                        target_pins = [row for row in target_pins if row["reference"] != reference]
                    else:
                        target_pins = [row for row in target_pins if row.get("component_uid") != change["uid"]]
                elif change["op"] == "add_component":
                    target_pins.extend({"reference": change["reference"], "pin": pin["number"], "component_uid": change["uid"]}
                                       for pin in change["expected_pins"])
            identities = {(row["reference"], row["pin"]) for row in target_pins}
            supplied = {(row.get("component"), row.get("pin")) for row in expected_connectivity}
            if identities != supplied:
                raise ValueError("Expected connectivity must contain the complete target pin-to-net table; missing="
                                 + repr(sorted(identities - supplied)) + "; extra=" + repr(sorted(supplied - identities, key=repr)))
            # Inventory comes from native pins and verified library additions, not the contract itself.
            assignments = {(row["component"], row["pin"]): row for row in expected_connectivity}
            intended = {"pins": [{"component": ref, "pin": pin, "net": "" if assignments[(ref, pin)].get("nc") else assignments[(ref, pin)].get("net")}
                                 for ref, pin in sorted(identities)], "complete": True}
            checked = check_contracts(intended, expected_connectivity, True)
            if checked.get("status") != "passed":
                raise ValueError("Invalid expected connectivity contracts")
        if intent is not None and not isinstance(intent, dict):
            raise ValueError("intent must be an object")
        nc_contracts = {(c.get("component"), c.get("pin")): c for c in (expected_connectivity or []) if c.get("nc") is True}
        for change in normalized:
            spec = change.get("replacement", change)
            if spec["op"] == "add_noerc":
                contract = nc_contracts.get((spec["component"], spec["pin"]))
                if contract is None or contract.get("reason") != spec["reason"]:
                    raise ValueError("No-ERC creation requires a matching intentional NC contract and reason")
        body = {"snapshot_id": snapshot["snapshot_id"], "owner": self.adapter._owner(), "changes": normalized,
                "output_dir": str(destination), "source_hashes": snapshot["source_hashes"]}
        if expected_connectivity is not None or intent is not None:
            body.update(expected_connectivity=expected_connectivity, intent=intent)
        return normalized, body

    @staticmethod
    def _expected_after(rows, changes):
        result = json.loads(_json(rows))
        for change in changes:
            if change["op"] == "set_component_value":
                for row in result:
                    if row.get("kind") == "edit_component" and row.get("reference", "").casefold() == change["reference"].casefold():
                        row["value"] = change["value"]
            elif change["op"] == "rename_net":
                for row in result:
                    if row.get("kind") == "edit_label" and row.get("name", "").casefold() == change["old_name"].casefold():
                        row["name"] = change["new_name"]
                    if row.get("kind") == "net" and row.get("name", "").casefold() == change["old_name"].casefold():
                        row["name"] = change["new_name"]
                    if row.get("kind") == "net_pin" and row.get("net", "").casefold() == change["old_name"].casefold():
                        row["net"] = change["new_name"]
        return result

    @staticmethod
    def _verify(before_rows, after_rows, changes):
        expected = ExistingSheetEditor._expected_after(before_rows, changes)
        if _canonical(expected) != _canonical(after_rows):
            raise RuntimeError("Saved schematic detail differs from the requested edit or changed unrelated geometry")
        old_nets, new_nets = _nets(before_rows), _nets(after_rows)
        renamed = {c["old_name"].casefold(): c["new_name"].casefold() for c in changes if c["op"] == "rename_net"}
        mapped = {(renamed.get(name, name), frozenset(pins)) for name, pins in old_nets.items()}
        actual = {(name, frozenset(pins)) for name, pins in new_nets.items()}
        if mapped != actual:
            raise RuntimeError("Compiled net membership changed outside the requested name mapping")

    @staticmethod
    def _require_inspected_hashes(project, sheet, inspected_hashes):
        current = {"project": _hash(project), "sheet": _hash(sheet)}
        if current != inspected_hashes:
            raise RuntimeError("Staged Altium files changed after successful native inspection")
        return current

    def _recover_publication(self, request_path, request_hash, destination, record):
        report_path = self.adapter.guard.resolve_existing(destination / "verification.json")
        if not report_path.is_file():
            raise RuntimeError("Published directory has no durable verification report")
        publication = record.get("publication") or {}
        if publication.get("destination") != str(destination) or publication.get("report_sha256") != _hash(report_path):
            raise RuntimeError("Published verification report does not match its durable publication intent")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("request_digest") != request_hash or report.get("snapshot_id") != record.get("snapshot_id") or report.get("compile_verified") is not True:
            raise RuntimeError("Published verification report does not match this request")
        snapshot = self._snapshot(record["snapshot_id"])
        expected_project = Path(snapshot["project"]).name
        expected_sheet = Path(snapshot["sheet"]).relative_to(Path(snapshot["project"]).parent)
        project_relative, sheet_relative = Path(report.get("project_relative", "")), Path(report.get("sheet_relative", ""))
        if (project_relative.is_absolute() or sheet_relative.is_absolute() or ".." in project_relative.parts or ".." in sheet_relative.parts
                or project_relative != Path(expected_project) or sheet_relative != expected_sheet):
            raise RuntimeError("Verification report contains an unsafe or unexpected project-relative path")
        project_path = self.adapter.guard.resolve_existing(destination / project_relative)
        sheet_path = self.adapter.guard.resolve_existing(destination / sheet_relative)
        if destination.resolve(strict=True) not in project_path.parents or destination.resolve(strict=True) not in sheet_path.parents:
            raise RuntimeError("Published project path escaped its output directory")
        actual_hashes = {"project": _hash(project_path), "sheet": _hash(sheet_path)}
        if actual_hashes != report.get("result_hashes"):
            raise RuntimeError("Published Altium output changed after verification")
        if actual_hashes["project"] != report.get("source_hashes", {}).get("project"):
            raise RuntimeError("Published project file differs from the inspected source project")
        previous_result = record.get("result")
        prior_warning = previous_result.get("diagnostic_warning") if isinstance(previous_result, dict) else None
        result = {"status": "succeeded", "snapshot_id": report["snapshot_id"],
                  "source_hashes": report["source_hashes"], "result_hashes": actual_hashes,
                  "project_path": str(project_path), "sheet_path": str(sheet_path),
                  "changes": report["changes"], "before": report["before"], "after": report["after"],
                  "compile_verified": True, "operation_ids": report["operation_ids"],
                  "publication": str(destination), "report_path": str(report_path)}
        result["electrical_checks"] = report.get("electrical_checks")
        if prior_warning:
            result["diagnostic_warning"] = prior_warning
        record.update(status="succeeded", result=result, result_hashes=actual_hashes,
                      operation_ids=report["operation_ids"])
        _write(request_path, record)
        verification_operation_id = report.get("verification_operation_id")
        verification_fingerprint = report.get("verification_adapter_sha256")
        verification_finished_at = report.get("verification_finished_at")
        current_fingerprint = self.adapter._adapter_fingerprint()
        eligible = (current_fingerprint is not None and verification_fingerprint == current_fingerprint
                    and isinstance(verification_operation_id, str)
                    and re.fullmatch(r"[0-9a-f]{32}", verification_operation_id) is not None
                    and isinstance(verification_finished_at, (int, float))
                    and not isinstance(verification_finished_at, bool)
                    and math.isfinite(verification_finished_at) and verification_finished_at > 0)
        try:
            if eligible:
                self.adapter._update_capability_evidence(
                    operation_id=verification_operation_id,
                    provenance="verified_edit_publication", script_dispatch=True, connectivity=True,
                    checked_at=verification_finished_at)
        except OSError as exc:
            # Publication and its durable report are already proven. Keep retry
            # behavior successful while exposing the ancillary evidence failure.
            result.setdefault("diagnostic_warning", "Edit publication succeeded; capability evidence write failed: " + str(exc))
            record["result"] = result
            try:
                _write(request_path, record)
            except OSError:
                pass
        return result

    def apply(self, snapshot_id, changes, output_dir, preview_digest, request_key, *, expected_connectivity=None, intent=None):
        self.adapter._require(edit=True)
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
            raise ValueError("request_key must be 1..200 characters")
        snapshot = self._snapshot(snapshot_id)
        if not isinstance(preview_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", preview_digest):
            raise ValueError("Invalid preview_digest")
        destination = self.adapter.guard.resolve_target(output_dir)
        normalized, binding = self._binding(snapshot, changes, destination, expected_connectivity, intent)
        computed_preview_digest = _digest(binding)
        if preview_digest != computed_preview_digest:
            raise ValueError("preview_digest does not match the current exact request")
        request_hash = _digest({"snapshot_id": snapshot_id, "preview_digest": preview_digest,
                                "request_key": request_key, "owner": self.adapter._owner(),
                                "changes": normalized, "output_dir": str(destination)})
        request_id = hashlib.sha256((self.adapter._owner() + "\0" + request_key).encode()).hexdigest()
        request_path = self.adapter.state_guard.resolve_target(self.adapter.state_root / "edit-requests" / (request_id + ".json"))
        request_path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(request_path), file_lock(destination):
            resume = False
            if request_path.exists():
                previous = json.loads(request_path.read_text(encoding="utf-8"))
                if previous.get("digest") != request_hash:
                    raise ValueError("request_key was already used with different inputs")
                if previous.get("destination") != str(destination):
                    raise ValueError("request_key was already bound to a different output directory")
                if previous.get("status") == "succeeded" or (previous.get("status") == "publishing" and destination.exists()):
                    return self._recover_publication(request_path, request_hash, destination, previous)
                if previous.get("status") not in {"staging", "editing", "verification_pending", "unknown", "running", "publishing"}:
                    return {"status": previous.get("status", "unknown"), "operation_id": previous.get("operation_id")}
                resume = True
            # Completed requests are recoverable without touching the original
            # source. In-flight requests still need current-source CAS checks.
            preview = self.preview(snapshot_id, changes, output_dir, _allow_existing=True,
                                   expected_connectivity=expected_connectivity, intent=intent)
            if preview_digest != preview["preview_digest"]:
                raise ValueError("preview_digest does not match the current exact request")
            project, sheet = self._project(snapshot["project"])
            if snapshot["source_hashes"] != {"project": _hash(project), "sheet": _hash(sheet)}:
                raise RuntimeError("Source project changed since snapshot inspection")
            # Recheck the live source document before allocating a stage or
            # dispatching edits. This catches a document opened and dirtied
            # after the caller created its snapshot.
            # This read-only guard must not reuse a successful result from a
            # prior attempt: the document can become dirty in memory between
            # retries without changing either source file hash.
            source_check = self._inspect_rows(project, sheet, request_id + ":preflight:" + uuid.uuid4().hex)
            if source_check.get("status") != "succeeded":
                details = source_check.get("message")
                rows = source_check.get("rows")
                if not details and isinstance(rows, list):
                    details = next((row.get("message") for row in reversed(rows)
                                    if isinstance(row, dict) and row.get("message")), None)
                result = {"status": source_check.get("status", "unknown"),
                          "operation_id": source_check.get("operation_id"),
                          "operation_ids": [source_check.get("prepare_operation_id"), source_check.get("operation_id")]}
                if details:
                    result["message"] = str(details)[:1000]
                return result
            source_rows = self.adapter._rows(source_check)
            if _canonical(source_rows) != _canonical(snapshot["rows"]):
                raise RuntimeError("Source schematic changed since snapshot inspection")
            destination = self.adapter.guard.resolve_target(output_dir)
            stage = destination.with_name("." + destination.name + ".actuator-stage-" + request_id[:16])
            if destination.exists():
                raise FileExistsError("Output directory already exists: " + str(destination))
            record = json.loads(request_path.read_text(encoding="utf-8")) if resume else {
                "digest": request_hash, "status": "staging", "snapshot_id": snapshot_id,
                "operation_id": None, "destination": str(destination), "operation_ids": []}
            if not resume and stage.exists():
                raise FileExistsError("An unowned staging directory already exists: " + str(stage))
            if not resume:
                _write(request_path, record)
            if not stage.exists():
                stage.mkdir(parents=True)
            stage = self.adapter.guard.resolve_existing(stage)
            if stage.parent != destination.parent.resolve(strict=True):
                raise PermissionError("Staging directory resolved outside the output parent")
            try:
                staged_project = stage / project.name
                sheet_relative = sheet.relative_to(project.parent)
                staged_sheet = stage / sheet_relative
                staged_project = self.adapter.guard.resolve_target(staged_project)
                staged_sheet = self.adapter.guard.resolve_target(staged_sheet)
                if staged_project.parent != stage or stage not in staged_sheet.parents:
                    raise PermissionError("Staged project documents resolved outside staging")
                if not resume:
                    if staged_project.exists() or staged_sheet.exists():
                        raise FileExistsError("Staging files already exist; refusing to overwrite caller content")
                    _copy_new(project, staged_project)
                    staged_sheet.parent.mkdir(parents=True, exist_ok=True)
                    sheet_parent = staged_sheet.parent.resolve(strict=True)
                    if sheet_parent != staged_sheet.parent or (sheet_parent != stage and stage not in sheet_parent.parents):
                        raise PermissionError("Staged schematic directory resolved outside staging")
                    staged_sheet = sheet_parent / sheet.name
                    _copy_new(sheet, staged_sheet)
                elif resume:
                    first_key = request_id + ":edit:0"
                    dispatch_hash = hashlib.sha256((self.adapter._owner() + "\0existing_edit\0" + first_key).encode()).hexdigest()
                    dispatch_record = self.adapter.state_guard.resolve_target(self.adapter.state_root / "requests" / (dispatch_hash + ".json"))
                    if dispatch_record.exists():
                        if not staged_project.is_file() or not staged_sheet.is_file():
                            raise RuntimeError("Staged files are incomplete while native delivery may have started")
                    else:
                        # Recover interrupted preparation without replacing any
                        # existing staged bytes that do not match the source.
                        for source_file, target_file, hash_key in (
                                (project, staged_project, "project"), (sheet, staged_sheet, "sheet")):
                            if target_file.exists():
                                if _hash(target_file) != snapshot["source_hashes"][hash_key]:
                                    raise RuntimeError("Unrecognized staged content exists before native dispatch")
                                continue
                            target_file.parent.mkdir(parents=True, exist_ok=True)
                            resolved_parent = target_file.parent.resolve(strict=True)
                            if resolved_parent != target_file.parent or (resolved_parent != stage and stage not in resolved_parent.parents):
                                raise PermissionError("Staged document directory resolved outside staging")
                            target_file = resolved_parent / source_file.name
                            _copy_new(source_file, target_file)
                first_key = request_id + ":edit:0"
                dispatch_hash = hashlib.sha256((self.adapter._owner() + "\0existing_edit\0" + first_key).encode()).hexdigest()
                first_dispatch = self.adapter.state_guard.resolve_target(self.adapter.state_root / "requests" / (dispatch_hash + ".json"))
                if not first_dispatch.exists() and {"project": _hash(staged_project), "sheet": _hash(staged_sheet)} != snapshot["source_hashes"]:
                    raise RuntimeError("Staged project did not match the source before native editing")
                if snapshot["source_hashes"] != {"project": _hash(project), "sheet": _hash(sheet)}:
                    raise RuntimeError("Source project changed while staging")
                ops = record.get("operation_ids", [])
                for index, change in enumerate(preview["changes"]):
                    if change["op"] == "set_component_value":
                        command = "SetComponentValue(" + ", ".join((_pas(change["reference"]), _pas(change["expected_value"]), _pas(change["value"]))) + ");"
                    elif change["op"] == "rename_net":
                        command = "RenameNetLabels(" + ", ".join((_pas(change["old_name"]), _pas(change["new_name"]))) + ");"
                    else:
                        from .eda_altium_changes import command as edit_command
                        command = edit_command(change)
                    op = self.adapter._run(f"OpenSheet({_pas(staged_sheet)});\n{command}\nSaveSheet;", kind="existing_edit",
                                           request_key=request_id + ":edit:" + str(index), payload={"stage": str(stage), "change": change})
                    if op.get("operation_id") and op.get("operation_id") not in ops:
                        ops.append(op["operation_id"])
                    phase = "editing" if op.get("status") == "succeeded" else op.get("status", "unknown")
                    record.update(status=phase, operation_id=op.get("operation_id"), operation_ids=ops)
                    _write(request_path, record)
                    if op.get("status") != "succeeded":
                        return {"status": op.get("status", "unknown"), "operation_id": op.get("operation_id"), "operation_ids": ops}
                record.update(status="verification_pending", operation_ids=ops)
                _write(request_path, record)
                for phase, command in (
                        ("normalize_prepare", f"PrepareProject({_pas(staged_project)});"),
                        ("normalize", f"NormalizeOwnedSheet({_pas(staged_project)}, {_pas(staged_sheet)});")):
                    normalized_stage = self.adapter._run(command, kind="existing_" + phase,
                        request_key=request_id + ":" + phase,
                        payload={"stage": str(stage), "project": str(staged_project), "sheet": str(staged_sheet)})
                    if normalized_stage.get("operation_id") and normalized_stage["operation_id"] not in ops:
                        ops.append(normalized_stage["operation_id"])
                    if normalized_stage.get("status") != "succeeded":
                        record.update(status=normalized_stage.get("status", "unknown"), operation_id=normalized_stage.get("operation_id"), operation_ids=ops)
                        _write(request_path, record)
                        return {"status": record["status"], "operation_id": record.get("operation_id"), "operation_ids": ops}
                # Release the verified copy's native project before its directory
                # moves, so AD26 cannot retain editor handles to the staging path.
                saved = self._inspect_rows(staged_project, staged_sheet, request_id + ":verify", close_after=True)
                if saved.get("status") != "succeeded":
                    record.update(status=saved.get("status", "unknown"), operation_id=saved.get("operation_id"), operation_ids=ops)
                    _write(request_path, record)
                    return {"status": record["status"], "operation_id": saved.get("operation_id"), "operation_ids": ops}
                after_rows = self.adapter._rows(saved)
                self._validate_native_rows(after_rows, staged_project, staged_sheet)
                from .eda_altium_analysis import connectivity, check_contracts, lint, erc
                from .eda_altium_changes import verify_objects
                verify_objects(snapshot["rows"], after_rows, preview["changes"])
                try:
                    connection_report = _connectivity_evidence(after_rows)
                except ValueError as exc:
                    raise RuntimeError("Native connectivity verification failed: " + str(exc)) from exc
                if not connection_report["complete"]:
                    contract_report = {"status": "incomplete", "reason": "native_compilation_failed"}
                elif expected_connectivity is not None:
                    contract_report = check_contracts(connection_report, expected_connectivity, True)
                else:
                    contract_report = {"status": "preserved_by_exact_comparison"}
                    before_pins = connectivity(snapshot["rows"])["pins"]
                    renamed = {c["old_name"]: c["new_name"] for c in preview["changes"] if c["op"] == "rename_net"}
                    intended_pins = [{**pin, "net": renamed.get(pin["net"], pin["net"])} for pin in before_pins]
                    if intended_pins != connection_report["pins"]:
                        raise RuntimeError("Pin connectivity changed outside the requested exact net mapping")
                    if any(c["op"] == "set_component_parameter" for c in preview["changes"]):
                        if _nets(snapshot["rows"]) != _nets(after_rows):
                            raise RuntimeError("Parameter edit changed compiled net membership")
                lint_intent = dict(intent or {})
                if expected_connectivity is not None:
                    provided = {(c["component"], c["pin"]): c for c in lint_intent.get("contracts", [])}
                    for contract in expected_connectivity:
                        key = (contract["component"], contract["pin"])
                        if key in provided and provided[key] != contract:
                            raise ValueError("Intent and expected connectivity disagree about a pin")
                        provided[key] = contract
                    lint_intent["contracts"] = list(provided.values())
                electrical_checks = {"connectivity": connection_report, "contracts": contract_report,
                                     "erc": erc(after_rows), "lint": lint(after_rows, lint_intent)}
                if (not connection_report.get("complete")
                        or contract_report.get("status") not in {"passed", "preserved_by_exact_comparison"}
                        or electrical_checks["erc"].get("status") != "passed"
                        or electrical_checks["lint"].get("status") != "passed"):
                    failed_report = self.adapter.guard.resolve_target(stage / "verification-failed.json")
                    _write(failed_report, {"schema": "chatgpt-actuator/altium-edit-failure/v1", "snapshot_id": snapshot_id,
                                          "source_hashes": snapshot["source_hashes"], "before": snapshot["rows"], "after": after_rows,
                                          "electrical_checks": electrical_checks, "operation_ids": ops})
                    record.update(status="verification_failed", electrical_checks=electrical_checks, report_path=str(failed_report))
                    _write(request_path, record)
                    return {"status": "verification_failed", "stage": str(stage), "electrical_checks": electrical_checks,
                            "report_path": str(failed_report), "operation_ids": ops}
                inspected_hashes = saved.get("inspected_hashes")
                if not isinstance(inspected_hashes, dict) or set(inspected_hashes) != {"project", "sheet"}:
                    raise RuntimeError("Successful Altium inspection did not provide file hash evidence")
                self._require_inspected_hashes(staged_project, staged_sheet, inspected_hashes)
                if _hash(staged_project) != snapshot["source_hashes"]["project"]:
                    raise RuntimeError("Altium edit changed the project file outside the requested schematic edit")
                stage = self.adapter.guard.resolve_existing(stage)
                staged_project = self.adapter.guard.resolve_existing(staged_project)
                staged_sheet = self.adapter.guard.resolve_existing(staged_sheet)
                if stage.parent != destination.parent.resolve(strict=True) or stage not in staged_project.parents or stage not in staged_sheet.parents:
                    raise PermissionError("Verified staging files resolved outside the output staging directory")
                if snapshot["source_hashes"] != {"project": _hash(project), "sheet": _hash(sheet)}:
                    raise RuntimeError("Source project changed before publication")
                operation_ids = list(dict.fromkeys(ops + [saved.get("prepare_operation_id"), saved["operation_id"]]))
                staged_project_relative = Path(project.name)
                staged_sheet_relative = sheet_relative
                result_hashes = dict(inspected_hashes)
                report = {"schema": "chatgpt-actuator/altium-edit-verification/v1", "request_digest": request_hash,
                          "snapshot_id": snapshot_id, "source_hashes": snapshot["source_hashes"],
                          "result_hashes": result_hashes, "project_relative": str(staged_project_relative),
                          "sheet_relative": str(staged_sheet_relative), "changes": preview["changes"],
                          "before": snapshot["rows"], "after": after_rows, "compile_verified": True,
                          "verification_operation_id": saved.get("operation_id"),
                          "verification_adapter_sha256": saved.get("adapter_sha256"),
                          "verification_finished_at": saved.get("finished_at"),
                          "operation_ids": operation_ids}
                report["electrical_checks"] = electrical_checks
                _write(stage / "verification.json", report)
                self._require_inspected_hashes(staged_project, staged_sheet, inspected_hashes)
                record.update(status="publishing", operation_ids=operation_ids,
                              publication={"stage": str(stage), "destination": str(destination),
                                           "report_sha256": _hash(stage / "verification.json")})
                _write(request_path, record)
                if destination.exists():
                    raise FileExistsError("Output directory appeared before publication")
                # Last check immediately before the create-only directory rename.
                self._require_inspected_hashes(staged_project, staged_sheet, inspected_hashes)
                os.rename(stage, destination)  # create-only publication; never replace a destination
                return self._recover_publication(request_path, request_hash, destination, record)
            except BaseException as exc:
                if destination.exists() and (destination / "verification.json").is_file():
                    return self._recover_publication(request_path, request_hash, destination, record)
                # Exceptions during delivery/publication can leave the external
                # outcome unresolved. A local verification mismatch is known
                # failure and must not be presented as an unknown delivery.
                record.update(status="unknown" if record.get("status") in {"editing", "publishing"} else "failed",
                              error=str(exc))
                _write(request_path, record)
                raise

    def commit(self, output_dir, request_key):
        from .eda_altium_commit import commit
        return commit(self, output_dir, request_key)

    def restore(self, commit_id, request_key):
        from .eda_altium_commit import restore
        return restore(self, commit_id, request_key)
