"""Explicit, backed-up installation of a verified SCH copy into a clean source."""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from ..atomic_files import file_lock
from .eda_altium import _hash, _pas, _write


def _key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("request_key must contain 1..200 characters")
    return hashlib.sha256(value.encode()).hexdigest()


def _hashes(project, sheet):
    return {"project": _hash(project), "sheet": _hash(sheet)}


def _check_hashes(project, sheet, expected):
    if _hashes(project, sheet) != expected:
        raise RuntimeError("Project/schematic changed outside this operation; refusing to overwrite")


def _checks_pass(report):
    checks = report.get("electrical_checks", {})
    return (report.get("compile_verified") is True and checks.get("connectivity", {}).get("complete") is True
            and checks.get("erc", {}).get("status") == "passed"
            and checks.get("lint", {}).get("status") == "passed"
            and checks.get("contracts", {}).get("status") in {"passed", "preserved_by_exact_comparison"})


def _record_path(editor, commit_id):
    if not isinstance(commit_id, str) or not re.fullmatch(r"[0-9a-f]{32}", commit_id):
        raise ValueError("Invalid commit_id")
    folder = editor.adapter.state_guard.resolve_target(editor.adapter.state_root / "commits" / commit_id)
    return folder, editor.adapter.state_guard.resolve_target(folder / "record.json")


def _close_clean(editor, project, sheet, expected, key):
    _check_hashes(project, sheet, expected)
    result = editor._inspect_rows(project, sheet, key + ":fresh:" + uuid.uuid4().hex)
    if result.get("status") != "succeeded":
        return result
    _check_hashes(project, sheet, expected)
    result = editor.adapter._run(f"CloseCleanSheet({_pas(sheet)});", kind="existing_close_clean",
                                 request_key=key + ":close:" + uuid.uuid4().hex,
                                 payload={"project": str(project), "sheet": str(sheet), "hashes": expected})
    if result.get("status") != "succeeded":
        return result
    _check_hashes(project, sheet, expected)
    return None


def _replace_copy(editor, source, target, expected_hash):
    from .eda_altium_edit import _copy_new
    # The temporary file is on the target volume; os.replace is one atomic rename.
    temporary = editor.adapter.guard.resolve_target(target.with_name("." + target.name + ".actuator-" + uuid.uuid4().hex + ".tmp"))
    _copy_new(source, temporary)
    if _hash(temporary) != expected_hash:
        raise RuntimeError("Replacement bytes changed while copying")
    return temporary


def _read_record(editor, commit_id):
    folder, path = _record_path(editor, commit_id)
    path = editor.adapter.state_guard.resolve_existing(path)
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("owner") != editor.adapter._owner() or record.get("commit_id") != commit_id:
        raise PermissionError("Commit belongs to another owner or has invalid identity")
    return folder, path, record


def commit(editor, output_dir, request_key):
    adapter = editor.adapter
    adapter._require(edit=True)
    destination = adapter.guard.resolve_existing(output_dir)
    matches = []
    request_folder = adapter.state_guard.resolve_target(adapter.state_root / "edit-requests")
    for path in request_folder.glob("*.json"):
        path = adapter.state_guard.resolve_existing(path)
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("destination") == str(destination) and item.get("status") == "succeeded":
            matches.append((path, item))
    if len(matches) != 1:
        raise ValueError("Output must belong to exactly one successful verified edit request")
    request_path, publication = matches[0]
    editor._recover_publication(request_path, publication["digest"], destination, publication)
    report_path = adapter.guard.resolve_existing(destination / "verification.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not _checks_pass(report):
        raise ValueError("Commit requires complete connectivity, ERC and lint evidence")
    if report.get("verification_adapter_sha256") != adapter._adapter_fingerprint():
        raise ValueError("Verified copy belongs to a different adapter revision")
    snapshot = editor._snapshot(report["snapshot_id"])
    project, sheet = editor._project(snapshot["project"])
    edited_sheet = adapter.guard.resolve_existing(destination / report["sheet_relative"])
    commit_id = hashlib.sha256((adapter._owner() + "\0" + _key(request_key)).encode()).hexdigest()[:32]
    folder, path = _record_path(editor, commit_id)
    folder.mkdir(parents=True, exist_ok=True)
    binding = {"output_dir": str(destination), "report_sha256": _hash(report_path), "request_key_sha256": _key(request_key)}
    with file_lock(path), file_lock(sheet):
        if path.exists():
            _, path, record = _read_record(editor, commit_id)
            if record["binding"] != binding:
                raise ValueError("request_key was already bound to different commit inputs")
            if record["status"] == "succeeded":
                _check_hashes(project, sheet, record["after_hashes"])
                return record["result"]
            if record["status"] == "unknown":
                try:
                    pending = adapter.operation_status(record["operation_id"])
                except FileNotFoundError:
                    pending = {"status": "unknown"}
                if pending.get("status") in {"running", "unknown"}:
                    return {"status": "unknown", "commit_id": commit_id, "operation_id": record.get("operation_id"), "record_path": str(path)}
                if pending.get("status") != "succeeded":
                    record.update(status="verification_failed", verification_operation=pending)
                    _write(path, record)
                    return {"status": "verification_failed", "commit_id": commit_id, "record_path": str(path)}
                record["status"] = "verifying"
            if record["status"] not in {"preparing", "replacing", "verifying"}:
                return {"status": record["status"], "commit_id": commit_id, "record_path": str(path)}
            if record["status"] == "replacing" and _hashes(project, sheet) == record["before_hashes"]:
                record["status"] = "not_applied"
                _write(path, record)
                return {"status": "not_applied", "commit_id": commit_id, "record_path": str(path)}
            if record["status"] != "preparing":
                # Reconcile a crash after replacement without replaying the file write.
                _check_hashes(project, sheet, record["after_hashes"])
        else:
            record = {"schema": "chatgpt-actuator/altium-commit/v1", "commit_id": commit_id,
                      "owner": adapter._owner(), "binding": binding, "project": str(project), "sheet": str(sheet),
                      "snapshot_id": snapshot["snapshot_id"], "before_hashes": snapshot["source_hashes"],
                      "after_hashes": {"project": snapshot["source_hashes"]["project"], "sheet": report["result_hashes"]["sheet"]},
                      "project_backup": str(folder / "before.PrjPcb"), "sheet_backup": str(folder / "before.SchDoc"), "status": "preparing"}
            _write(path, record)
        if record["status"] == "preparing":
            expected = record["before_hashes"]
            blocked = _close_clean(editor, project, sheet, expected, commit_id)
            if blocked:
                return dict(blocked, commit_id=commit_id)
            from .eda_altium_edit import _copy_new
            project_backup = adapter.state_guard.resolve_target(record["project_backup"])
            sheet_backup = adapter.state_guard.resolve_target(record["sheet_backup"])
            for source, backup, key in ((project, project_backup, "project"), (sheet, sheet_backup, "sheet")):
                if backup.exists():
                    if not backup.is_file() or _hash(backup) != expected[key]:
                        raise RuntimeError("Existing commit backup does not match its bound source snapshot")
                else:
                    _copy_new(source, backup)
            _check_hashes(project_backup, sheet_backup, expected)
            _check_hashes(project, sheet, expected)
            after_hashes = record["after_hashes"]
            temporary = _replace_copy(editor, edited_sheet, sheet, after_hashes["sheet"])
            record["status"] = "replacing"
            _write(path, record)
            _check_hashes(project, sheet, expected)
            os.replace(temporary, sheet)
        record["status"] = "verifying"
        _write(path, record)
        native = editor._inspect_rows(project, sheet, commit_id + ":verify")
        if native.get("status") != "succeeded":
            record.update(status="unknown", operation_id=native.get("operation_id"))
            _write(path, record)
            return {"status": "unknown", "commit_id": commit_id, "operation_id": native.get("operation_id"), "record_path": str(path)}
        from .eda_altium_edit import _canonical, _connectivity_evidence
        from .eda_altium_analysis import erc
        rows = adapter._rows(native)
        try:
            connection = _connectivity_evidence(rows)
            expected_connection = _connectivity_evidence(report["after"])
            electrical = {"connectivity": connection, "erc": erc(rows)}
            verified = (connection.get("complete") is True and electrical["erc"].get("status") == "passed"
                        and connection["pins"] == expected_connection["pins"]
                        and _canonical(rows) == _canonical(report["after"]))
        except ValueError as exc:
            verified, electrical = False, {"error": str(exc)}
        if not verified:
            record.update(status="verification_failed", operation_id=native.get("operation_id"), electrical_checks=electrical)
            _write(path, record)
            return {"status": "verification_failed", "commit_id": commit_id, "record_path": str(path), "electrical_checks": electrical}
        _check_hashes(project, sheet, record["after_hashes"])
        result = {"status": "succeeded", "commit_id": commit_id, "project_path": str(project),
                  "sheet_path": str(sheet), "before_hashes": record["before_hashes"], "after_hashes": record["after_hashes"],
                  "operation_id": native.get("operation_id"), "backup_record": str(path), "electrical_checks": electrical}
        record.update(status="succeeded", result=result)
        _write(path, record)
        return result


def restore(editor, commit_id, request_key):
    editor.adapter._require(edit=True)
    key = _key(request_key)
    _, path, _ = _read_record(editor, commit_id)
    with file_lock(path):
        _, path, record = _read_record(editor, commit_id)
        project, sheet = editor._project(record["project"])
        if str(sheet) != record["sheet"]:
            raise RuntimeError("Source document path changed")
        prior = record.get("restoration")
        if prior and prior["status"] == "not_applied":
            _check_hashes(project, sheet, record["after_hashes"])
            if prior["request_key_sha256"] == key:
                return {"status": "not_applied", "commit_id": commit_id}
            record.setdefault("restoration_history", []).append(prior)
            prior = None
        if prior and prior["request_key_sha256"] != key:
            raise ValueError("Restoration already has a different request_key")
        if prior and prior["status"] == "succeeded":
            _check_hashes(project, sheet, record["before_hashes"])
            return prior["result"]
        if record["status"] not in {"succeeded", "verification_failed", "unknown", "verifying", "replacing"}:
            raise ValueError("Commit is not eligible for restoration")
        snapshot = editor._snapshot(record["snapshot_id"])
        backup = editor.adapter.state_guard.resolve_existing(record["sheet_backup"])
        if _hash(backup) != record["before_hashes"]["sheet"]:
            raise RuntimeError("Backup hash mismatch")
        with file_lock(sheet):
            if prior and prior["status"] == "replacing" and _hashes(project, sheet) == record["after_hashes"]:
                record["restoration"]["status"] = "not_applied"
                _write(path, record)
                return {"status": "not_applied", "commit_id": commit_id}
            if not prior:
                blocked = _close_clean(editor, project, sheet, record["after_hashes"], commit_id + ":restore")
                if blocked:
                    return dict(blocked, commit_id=commit_id)
                temporary = _replace_copy(editor, backup, sheet, record["before_hashes"]["sheet"])
                record["restoration"] = {"request_key_sha256": key, "status": "replacing"}
                _write(path, record)
                _check_hashes(project, sheet, record["after_hashes"])
                os.replace(temporary, sheet)
            _check_hashes(project, sheet, record["before_hashes"])
            native = editor._inspect_rows(project, sheet, commit_id + ":restore:verify")
            if native.get("status") != "succeeded":
                record["restoration"].update(status="unknown", operation_id=native.get("operation_id"))
                _write(path, record)
                return {"status": "unknown", "commit_id": commit_id, "operation_id": native.get("operation_id")}
            from .eda_altium_edit import _canonical, _connectivity_evidence
            rows = editor.adapter._rows(native)
            try:
                expected_connection = _connectivity_evidence(snapshot["rows"])
                connection = _connectivity_evidence(rows)
                graph_matches = not expected_connection["complete"] or (connection["complete"] and connection["pins"] == expected_connection["pins"])
                verified = _canonical(rows) == _canonical(snapshot["rows"]) and graph_matches
            except ValueError:
                verified = False
            if not verified:
                record["restoration"]["status"] = "verification_failed"
                _write(path, record)
                return {"status": "verification_failed", "commit_id": commit_id}
            _check_hashes(project, sheet, record["before_hashes"])
            result = {"status": "succeeded", "commit_id": commit_id, "restored_hashes": record["before_hashes"],
                      "operation_id": native.get("operation_id"), "connectivity": connection,
                      "verification_scope": "Restored exact baseline bytes and physical objects; connectivity checked only when the baseline was compiled"}
            record["restoration"].update(status="succeeded", result=result)
            _write(path, record)
            return result
