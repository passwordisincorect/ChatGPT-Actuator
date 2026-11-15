"""AD26 schematic adapter: fixed DelphiScript operations, revisioned IR and read-back.

No caller-supplied script is executed. Coordinates at the API boundary are mils.
Altium itself remains the authority for symbol geometry and compiled connectivity.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from threading import RLock
import time
import uuid

from ..security import PathGuard
from ..security.path_canonical import canonical_resolved_path, resolve_canonical
from ..atomic_files import file_lock
from ..tool_policy import principal_context


_LOCK = RLock()
_SCHEMA = "chatgpt-actuator/altium-schematic-ir/v1"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_REF = re.compile(r"[A-Za-z][A-Za-z0-9_]*[0-9][A-Za-z0-9_]*\Z")
def _runtime_fingerprint():
    folder = Path(__file__).parent
    names = ("eda_altium.py", "eda_altium_edit.py", "eda_altium_analysis.py", "eda_altium_configuration.py", "eda_altium_changes.py", "eda_altium_commit.py", "altium_scripts/Bridge.pas")
    return hashlib.sha256(b"".join((folder / name).read_bytes() for name in names)).hexdigest()


_LOADED_RUNTIME_FINGERPRINT = _runtime_fingerprint()


def _json(data):
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _pas(value):
    # Emit ASCII DelphiScript expressions, including Unicode and control characters.
    chunks, text = [], ""
    for char in str(value):
        if 32 <= ord(char) < 127:
            text += char.replace("'", "''")
        else:
            if text:
                chunks.append("'" + text + "'")
                text = ""
            for offset in range(0, len(char.encode("utf-16-le")), 2):
                unit = char.encode("utf-16-le")[offset:offset + 2]
                chunks.append("#" + str(int.from_bytes(unit, "little")))
    if text or not chunks:
        chunks.append("'" + text + "'")
    return "".join(chunks)


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(_json(data), encoding="utf-8")
    temporary.replace(path)


class _PrivateStateGuard:
    """Confine adapter-owned state without applying caller workspace narrowing."""

    def __init__(self, root):
        self.root = resolve_canonical(root, strict=False)
        self.roots = (self.root,)

    def _resolve(self, raw_path, *, strict):
        resolved = resolve_canonical(raw_path, strict=strict)
        if resolved != self.root and self.root not in resolved.parents:
            raise PermissionError("Altium adapter state path escaped its private root")
        return resolved

    def resolve_target(self, raw_path):
        return self._resolve(raw_path, strict=False)

    def resolve_existing(self, raw_path):
        return self._resolve(raw_path, strict=True)


def _path_overlaps(left, right):
    left, right = canonical_resolved_path(left), canonical_resolved_path(right)
    return left == right or left in right.parents or right in left.parents


def _default_state_root(audit):
    audit_path = resolve_canonical(audit.path, strict=False)
    identity = hashlib.sha256(os.path.normcase(str(audit_path)).encode("utf-8")).hexdigest()[:24]
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if os.name == "nt" and local_app_data else Path(tempfile.gettempdir())
    return resolve_canonical(base / "ChatGPT-Actuator" / "altium" / identity, strict=False)


def compare_nets(plan, rows):
    """Compare exact pin partitions and names, including unexpected connected pins."""
    expected = {n["name"].casefold(): {(p["reference"].casefold(), str(p["pin"])) for p in n["pins"]} for n in plan["nets"]}
    actual = {}
    for row in rows:
        if row.get("kind") == "net":
            actual.setdefault(row["name"].casefold(), set())
        elif row.get("kind") == "net_pin":
            actual.setdefault(row["net"].casefold(), set()).add((row["reference"].casefold(), str(row["pin"])))
    errors = []
    for name, pins in expected.items():
        found = actual.get(name, set())
        if found != pins:
            errors.append({"net": name, "missing_pins": sorted(pins - found), "unexpected_pins": sorted(found - pins)})
    for name, pins in actual.items():
        if name not in expected and pins:
            errors.append({"net": name, "unexpected_net": True, "pins": sorted(pins)})
    compiled = any(r.get("kind") == "compiled" for r in rows)
    if not compiled:
        errors.append({"error": "Compiled Altium connectivity evidence is missing"})
    return {"ok": compiled and not errors, "expected_nets": len(expected), "actual_nets": len(actual), "errors": errors}


class AltiumAdapter:
    def __init__(self, config, audit, guard, discover, *, state_dir=None, excluded_roots=()):
        self.config, self.audit, self.guard, self.discover = config, audit, guard, discover
        self.library_guard = PathGuard((*guard.roots, *config.altium_library_roots))
        self.state_root = resolve_canonical(state_dir, strict=False) if state_dir is not None else _default_state_root(audit)
        self.state_guard = _PrivateStateGuard(self.state_root)
        self.excluded_roots = tuple(resolve_canonical(root, strict=False) for root in (
            *excluded_roots, *config.allowed_project_roots, *config.altium_library_roots))
        self._state_ready = False
        self._state_lock = RLock()

    def _ensure_private_state(self):
        with self._state_lock:
            if self._state_ready:
                return self.state_root
            state_root = resolve_canonical(self.state_root, strict=False)
            if any(_path_overlaps(state_root, root) for root in self.excluded_roots):
                raise PermissionError("Altium private state path overlaps a configured user-accessible root")
            state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            resolved = resolve_canonical(state_root, strict=True)
            if any(_path_overlaps(resolved, root) for root in self.excluded_roots):
                raise PermissionError("Altium private state path overlaps a configured user-accessible root")
            if os.name != "nt":
                resolved.chmod(0o700)
            self.state_root = resolved
            self.state_guard = _PrivateStateGuard(resolved)
            self._state_ready = True
            return resolved

    def _require(self, edit=False):
        if not self.config.enabled or not self.config.allow_read:
            raise PermissionError("EDA read permission is required")
        if edit and not self.config.allow_edit:
            raise PermissionError("EDA edit permission is required")
        principal = principal_context.get()
        if principal and ("mcp:read" not in principal.scopes or (edit and "mcp:write" not in principal.scopes)):
            raise PermissionError("Missing EDA read/write scope")
        self._ensure_private_state()

    @staticmethod
    def _owner():
        principal = principal_context.get()
        return principal.owner if principal else "local"

    def _op_path(self, operation_id):
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Invalid operation_id")
        return self.state_guard.resolve_target(self.state_root / "operations" / operation_id)

    def _resolve_cached_source(self, raw_path):
        for guard in (self.state_guard, self.library_guard):
            try:
                return guard.resolve_existing(raw_path)
            except (OSError, PermissionError):
                continue
        raise PermissionError("Cached Altium library source is outside authorized roots")

    def _record(self, operation_id):
        path = self.state_guard.resolve_existing(self._op_path(operation_id) / "operation.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["owner"] != self._owner():
            raise PermissionError("Operation belongs to another owner")
        return path, data

    def _read_response(self, folder, operation_id):
        path = self.state_guard.resolve_target(folder / "response.jsonl")
        if not path.exists():
            return None
        if path.stat().st_size > self.config.max_output_bytes:
            raise ValueError("Altium response exceeds configured maximum size")
        raw = path.read_bytes()
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        rows = [json.loads(line) for line in raw.decode(encoding).splitlines() if line.strip()]
        if not rows or rows[0] != {"operation_id": operation_id}:
            raise ValueError("Stale or mismatched Altium response")
        if not isinstance(rows[-1], dict) or rows[-1].get("status") not in {"ok", "error"}:
            raise ValueError("Incomplete Altium response")
        if not all(isinstance(row, dict) for row in rows):
            # The matching terminal envelope proves the script finished. Retain
            # the untouched raw file and reject its schema without replaying it.
            return [rows[0], {"status":"error", "message":"Invalid Altium response row type", "response_sha256":_hash(path)}]
        return rows

    def _reconcile(self, path, data):
        if data["status"] in {"running", "unknown"}:
            try:
                rows = self._read_response(path.parent, data["operation_id"])
            except (ValueError, UnicodeError) as exc:
                data.update(status="unknown", error=str(exc))
                with file_lock(path):
                    _write(path, data)
                return data
            if rows:
                data.update(status="succeeded" if rows[-1]["status"] == "ok" else "failed", rows=rows, finished_at=time.time())
                with file_lock(path):
                    _write(path, data)
        return data

    def operation_status(self, operation_id):
        self._require()
        with _LOCK:
            path, data = self._record(operation_id)
            # Coordinate status reconciliation with writers in other Actuator
            # processes, which may still be recording launcher/delivery state.
            with file_lock(path):
                data = json.loads(path.read_text(encoding="utf-8"))
            result = self._reconcile(path, data)
            if result['status'] == 'publishing' and Path(result['publication']['destination']).exists():
                result = self._recover_publication(path, result, allow_publish=False)
            if result["status"] == "verification_pending":
                child_path, child = self._record(result["verification_operation_id"])
                result = dict(result, verification_status=self._reconcile(child_path, child)["status"], publication_pending=True)
            if result["status"] == "succeeded" and result["kind"] == "materialize" and "result" not in result:
                result = dict(result, publication_pending=True, next_action="Call materialize with the original request_key to validate and publish without replay")
            return result

    def capabilities(self):
        fingerprint = self._adapter_fingerprint()
        if fingerprint is None:
            return {"adapter_installed": True, "script_dispatch_verified": False,
                    "connectivity_verified": False, "evidence": {}, "adapter_sha256": "",
                    "library_roots": [str(p) for p in self.library_guard.roots], "units": "mil",
                    "state_available": False, "state_error": self._adapter_fingerprint_error()}
        evidence, state_error = {}, None
        try:
            self._ensure_private_state()
            proof = self.state_guard.resolve_target(self.state_root / "capabilities.json")
            with file_lock(proof):
                if not proof.is_file():
                    raise ValueError("capabilities evidence file is missing")
                evidence = json.loads(proof.read_text(encoding="utf-8"))
            if not isinstance(evidence, dict):
                raise ValueError("capabilities evidence must be a JSON object")
            valid, diagnostic = self._validate_capability_evidence(evidence, fingerprint)
            if not valid:
                state_error = diagnostic
        except (OSError, PermissionError, ValueError, TypeError, json.JSONDecodeError) as exc:
            evidence = {}
            state_error = str(exc)
        fingerprint = fingerprint or ""
        current = state_error is None
        return {"adapter_installed": True, "script_dispatch_verified": current and bool(evidence.get("script_dispatch_verified")),
                "connectivity_verified": current and bool(evidence.get("connectivity_verified")), "evidence": evidence, "adapter_sha256": fingerprint,
                "library_roots": [str(p) for p in self.library_guard.roots], "units": "mil",
                "state_available": state_error is None, "state_error": state_error}

    @staticmethod
    def _adapter_fingerprint():
        try:
            disk_fingerprint = _runtime_fingerprint()
        except OSError:
            return None
        return disk_fingerprint if disk_fingerprint == _LOADED_RUNTIME_FINGERPRINT else None

    @staticmethod
    def _adapter_fingerprint_error():
        try:
            disk_fingerprint = _runtime_fingerprint()
        except OSError as exc:
            return "Unable to read installed Altium adapter files for fingerprinting: " + str(exc)
        if disk_fingerprint != _LOADED_RUNTIME_FINGERPRINT:
            return "Installed Altium adapter files changed after this runtime loaded; restart the process before asserting readiness"
        return None

    @staticmethod
    def _valid_proof(evidence, prefix, provenance_values):
        operation_id = evidence.get(prefix + "operation_id")
        verified_at = evidence.get(prefix + "checked_at")
        provenance = evidence.get(prefix + "provenance")
        return (isinstance(operation_id, str) and re.fullmatch(r"[0-9a-f]{32}", operation_id) is not None
                and isinstance(verified_at, (int, float)) and not isinstance(verified_at, bool)
                and math.isfinite(verified_at) and verified_at > 0
                and isinstance(provenance, str) and bool(provenance)
                and provenance in provenance_values)

    @classmethod
    def _validate_capability_evidence(cls, evidence, fingerprint):
        if not isinstance(evidence, dict):
            return False, "capabilities evidence must be a JSON object"
        if not evidence:
            return False, "capabilities evidence is empty or incomplete"
        if evidence.get("schema") != "chatgpt-actuator/altium-capabilities/v1":
            return False, "capabilities evidence has an invalid or missing schema"
        if not isinstance(evidence.get("adapter_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", evidence["adapter_sha256"]):
            return False, "capabilities evidence has an invalid or missing adapter fingerprint"
        if fingerprint is None:
            return False, cls._adapter_fingerprint_error() or "Unable to verify loaded Altium adapter identity"
        if evidence["adapter_sha256"] != fingerprint:
            return False, "capabilities evidence fingerprint is stale for the installed Altium adapter"
        if type(evidence.get("script_dispatch_verified")) is not bool or type(evidence.get("connectivity_verified")) is not bool:
            return False, "capabilities evidence proof flags are missing or have invalid types"
        operation_id = evidence.get("operation_id")
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            return False, "capabilities evidence has an invalid or missing operation ID"
        if not isinstance(evidence.get("provenance"), str) or not evidence["provenance"]:
            return False, "capabilities evidence has invalid or missing provenance"
        for field in ("checked_at", "updated_at"):
            value = evidence.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                return False, f"capabilities evidence has an invalid or missing {field} timestamp"
        dispatch_ok = cls._valid_proof(evidence, "script_dispatch_", {"successful_script_dispatch"})
        connectivity_ok = cls._valid_proof(evidence, "connectivity_", {
            "saved_native_connectivity_comparison", "materialize_publication", "verified_edit_publication"})
        if evidence["script_dispatch_verified"] and not dispatch_ok:
            return False, "script dispatch readiness lacks valid operation, timestamp, or provenance evidence"
        if evidence["connectivity_verified"] and not connectivity_ok:
            return False, "connectivity readiness lacks valid native comparison operation, timestamp, or provenance evidence"
        return True, None

    def _update_capability_evidence(self, *, operation_id, provenance, script_dispatch=False, connectivity=False,
                                    checked_at=None):
        """Atomically add only newly proven flags; old flags survive only for this adapter."""
        fingerprint = self._adapter_fingerprint()
        if not fingerprint:
            raise OSError(self._adapter_fingerprint_error() or "Unable to fingerprint the installed Altium adapter")
        path = self.state_guard.resolve_target(self.state_root / "capabilities.json")
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Capability proof requires a valid operation ID")
        with file_lock(path):
            try:
                prior = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                valid, _ = self._validate_capability_evidence(prior, fingerprint)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                prior, valid = {}, False
            now = time.time()
            proof_time = now if checked_at is None else checked_at
            if (isinstance(proof_time, bool) or not isinstance(proof_time, (int, float))
                    or not math.isfinite(proof_time) or proof_time <= 0):
                raise ValueError("Capability proof requires a finite positive verification timestamp")
            dispatch_proof = ({"operation_id": prior["script_dispatch_operation_id"],
                               "checked_at": prior["script_dispatch_checked_at"],
                               "provenance": prior["script_dispatch_provenance"]}
                              if valid and prior["script_dispatch_verified"] else None)
            connectivity_proof = ({"operation_id": prior["connectivity_operation_id"],
                                   "checked_at": prior["connectivity_checked_at"],
                                   "provenance": prior["connectivity_provenance"]}
                                  if valid and prior["connectivity_verified"] else None)
            if script_dispatch:
                dispatch_proof = {"operation_id": operation_id,
                                  "checked_at": (dispatch_proof["checked_at"] if dispatch_proof
                                                 and dispatch_proof["operation_id"] == operation_id
                                                 and dispatch_proof["provenance"] == "successful_script_dispatch"
                                                 else proof_time),
                                  "provenance": "successful_script_dispatch"}
            if connectivity:
                connectivity_proof = {"operation_id": operation_id,
                                      "checked_at": (connectivity_proof["checked_at"] if connectivity_proof
                                                     and connectivity_proof["operation_id"] == operation_id
                                                     and connectivity_proof["provenance"] == provenance
                                                     else proof_time),
                                      "provenance": provenance}
            evidence = {
                "schema": "chatgpt-actuator/altium-capabilities/v1",
                "adapter_sha256": fingerprint,
                "script_dispatch_verified": dispatch_proof is not None,
                "connectivity_verified": connectivity_proof is not None,
                "operation_id": operation_id,
                "provenance": provenance if connectivity else "successful_script_dispatch",
                "checked_at": now,
                "updated_at": now,
            }
            for prefix, proof in (("script_dispatch_", dispatch_proof), ("connectivity_", connectivity_proof)):
                evidence[prefix + "operation_id"] = proof["operation_id"] if proof else None
                evidence[prefix + "checked_at"] = proof["checked_at"] if proof else None
                evidence[prefix + "provenance"] = proof["provenance"] if proof else None
            valid, diagnostic = self._validate_capability_evidence(evidence, fingerprint)
            if not valid:
                raise ValueError("Refusing invalid Altium capability evidence: " + diagnostic)
            _write(path, evidence)
        return evidence

    def _run(self, command, *, kind, request_key=None, payload=None):
        self._require()
        principal = principal_context.get()
        if principal and "mcp:execute" not in principal.scopes:
            raise PermissionError("Altium dispatch requires mcp:execute")
        executable = self.discover()
        if executable is None:
            raise FileNotFoundError("Altium Designer executable is unavailable")
        if request_key is not None and (not request_key.strip() or len(request_key) > 200):
            raise ValueError("request_key must be 1..200 characters")
        payload_digest = hashlib.sha256(_json(payload or {}).encode()).hexdigest()
        with _LOCK:
            self.state_guard.resolve_target(self.state_root).mkdir(parents=True, exist_ok=True)
            # A named OS mutex serializes callers across Actuator processes.
            import win32event
            import win32api
            mutex = win32event.CreateMutex(None, False, r"Local\ChatGPTActuatorAltiumAdapter")
            acquired = win32event.WaitForSingleObject(mutex, 0) in (0, 0x80)
            if not acquired:
                win32api.CloseHandle(mutex)
                raise RuntimeError("Altium adapter is busy")
            try:
                request_path = None
                if request_key:
                    key = hashlib.sha256((self._owner() + "\0" + kind + "\0" + request_key).encode()).hexdigest()
                    request_path = self.state_guard.resolve_target(self.state_root / "requests" / (key + ".json"))
                    if request_path.exists():
                        prior = json.loads(request_path.read_text(encoding="utf-8"))
                        if prior["digest"] != payload_digest:
                            raise ValueError("request_key was already used with different inputs")
                        return self.operation_status(prior["operation_id"])
                cleanup_previous_operation_id = None
                dispatch_command = command
                active = self.state_guard.resolve_target(self.state_root / "active.json")
                if active.exists():
                    previous = json.loads(active.read_text(encoding="utf-8"))
                    previous_folder = self._op_path(previous["operation_id"])
                    previous_path = self.state_guard.resolve_existing(previous_folder / "operation.json")
                    previous_data = self._reconcile(previous_path, json.loads(previous_path.read_text(encoding="utf-8")))
                    if previous_data["status"] in {"running", "unknown"}:
                        raise RuntimeError("Previous Altium delivery is unresolved; inspect operation " + previous["operation_id"])
                    if previous_data.get("owner") == self._owner() and previous_data.get("status") in {"succeeded", "failed"}:
                        # Close only the exact script project generated for the
                        # previous terminal operation. Keep its files and
                        # response as durable evidence.
                        previous_folder = self.state_guard.resolve_existing(previous_folder)
                        old_project = self.state_guard.resolve_existing(previous_folder / "Bridge.PrjScr")
                        old_script = self.state_guard.resolve_existing(previous_folder / "Bridge.pas")
                        if (old_project.parent != previous_folder or old_script.parent != previous_folder
                                or old_project.name != "Bridge.PrjScr" or old_script.name != "Bridge.pas"):
                            raise PermissionError("Previous Altium script project is outside its owned operation folder")
                        dispatch_command = (
                            "try\n"
                            f"  CloseOwnedProject({_pas(old_project)}, {_pas(old_script)});\n"
                            "except\n"
                            f"  Emit('{{\"kind\":\"script_cleanup_warning\",\"path\":' + J({_pas(old_script)}) + ',\"message\":' + J(ExceptionMessage) + '}}');\n"
                            "end;\n" + command
                        )
                        cleanup_previous_operation_id = previous_data["operation_id"]
                operation_id = uuid.uuid4().hex
                folder = self._op_path(operation_id)
                # AD's command parser does not reliably support spaces in -R paths.
                if any(c.isspace() for c in str(folder)) or any(c in str(folder) for c in '|()"'):
                    raise ValueError("Altium exchange root must have a path without whitespace or |()\"")
                folder.mkdir(parents=True)
                folder = self.state_guard.resolve_existing(folder)
                template = Path(__file__).with_name("altium_scripts") / "Bridge.pas"
                source = template.read_text(encoding="utf-8")
                tokens = {"COMMAND": command, "OPERATION_ID": _pas(operation_id), "RESPONSE_TMP": _pas(folder / "response.tmp"), "RESPONSE": _pas(folder / "response.jsonl")}
                # Single substitution prevents caller data from expanding template tokens.
                tokens["COMMAND"] = dispatch_command
                source = re.sub(r"@@(COMMAND|OPERATION_ID|RESPONSE_TMP|RESPONSE)@@", lambda match: tokens[match[1]], source)
                self.state_guard.resolve_target(folder / "Bridge.pas").write_text(source, encoding="utf-8-sig")
                project = self.state_guard.resolve_target(folder / "Bridge.PrjScr")
                project.write_text("[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Bridge.pas\n", encoding="utf-8")
                record = {"operation_id": operation_id, "owner": self._owner(), "kind": kind, "status": "running", "created_at": time.time(), "adapter_sha256": self._adapter_fingerprint(), "payload": payload or {}, "digest": payload_digest}
                if cleanup_previous_operation_id is not None:
                    record["cleanup_previous_operation_id"] = cleanup_previous_operation_id
                path = self.state_guard.resolve_target(folder / "operation.json")
                with file_lock(path):
                    _write(path, record)
                if request_path:
                    _write(request_path, {"operation_id": operation_id, "digest": payload_digest})
                _write(active, {"operation_id": operation_id})
                arg = f"-RScriptingSystem:RunScript(ProjectName={project}|ProcName=Bridge.pas>ActuatorRun)"
                try:
                    process = subprocess.Popen([str(executable), arg], shell=False)
                except OSError as exc:
                    record.update(status="failed", error=str(exc))
                    with file_lock(path):
                        _write(path, record)
                    raise
                record["launcher_pid"] = process.pid
                with file_lock(path):
                    _write(path, record)
                self.audit.record("altium_dispatch", operation_id=operation_id, kind=kind, owner=self._owner())
                deadline = time.monotonic() + min(self.config.default_timeout_seconds, self.config.max_timeout_seconds)
                while time.monotonic() < deadline:
                    result = self._reconcile(path, record)
                    if result["status"] in {"succeeded", "failed"}:
                        if (result["status"] == "succeeded"
                                and result.get("adapter_sha256") == self._adapter_fingerprint()):
                            try:
                                self._update_capability_evidence(operation_id=operation_id, provenance=kind,
                                                                 script_dispatch=True,
                                                                 checked_at=result.get("finished_at"))
                            except OSError as exc:
                                result["diagnostic_warning"] = "Operation succeeded; capability evidence write failed: " + str(exc)
                        return result
                    time.sleep(0.2)
                record.update(status="unknown", timed_out=True, message="Delivery unconfirmed; poll operation_status. Do not replay.")
                with file_lock(path):
                    _write(path, record)
                return record
            finally:
                win32event.ReleaseMutex(mutex)
                win32api.CloseHandle(mutex)

    @staticmethod
    def _rows(result):
        if result["status"] != "succeeded":
            return None
        return result["rows"]

    def library_inspect(self, library_path, *, cached_only=False):
        self._require()
        library = self.library_guard.resolve_existing(library_path)
        if not library.is_file() or library.suffix.casefold() not in {".schlib", ".intlib"}:
            raise ValueError("Expected a .SchLib or .IntLib file")
        fingerprint = _hash(library)
        cache = self.state_guard.resolve_target(self.state_root / "libraries" / (fingerprint + ".json"))
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
            # Cached paths are only reused for the same authorized source location.
            if data["path"] == str(library) and data.get('catalog_version') == 1 and all(
                _hash(self._resolve_cached_source(p)) == digest for p, digest in data.get('source_hashes', {}).items()
            ) and data.get('source_hashes'):
                return data
        if cached_only:
            raise ValueError("Inspect the library with eda_altium_library_inspect before previewing component additions")
        sources = [library]
        if library.suffix.casefold() == ".intlib":
            extracted = self.state_guard.resolve_target(self.state_root / "libraries" / fingerprint)
            extracted.mkdir(parents=True, exist_ok=True)
            operation = self._run(f"IntegratedLibraryManager.ExtractSourcesToPath({_pas(library)}, {_pas(extracted)});", kind="extract_library")
            if not self._rows(operation):
                return operation
            sources = [self.state_guard.resolve_existing(p) for p in extracted.rglob("*") if p.suffix.casefold() == ".schlib"]
            if not sources:
                raise ValueError("Integrated library contains no extracted schematic sources")
        symbols = []
        for source in sources:
            operation = self._run(f"InspectLibrary({_pas(source)});", kind="inspect_library")
            rows = self._rows(operation)
            if rows is None:
                return operation
            for row in rows:
                if row.get("kind") == "symbol":
                    pins = [r for r in rows if r.get("kind") == "pin" and r["symbol"] == row["name"]]
                    symbols.append({"name": row["name"], "parts": row["parts"], "source_path": str(source), "pins": pins})
        if _hash(library) != fingerprint:
            raise RuntimeError("Library changed during inspection")
        data = {"path": str(library), "sha256": fingerprint, "symbols": symbols, 'catalog_version':1,
                'source_hashes':{str(p):_hash(p) for p in sources}}
        _write(cache, data)
        self.audit.record("altium_library_inspect", path=str(library), symbols=len(symbols))
        return data

    def _load(self, ir_path):
        path = self.guard.resolve_existing(ir_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != _SCHEMA:
            raise ValueError("Unsupported Altium schematic IR schema")
        return path, data

    def plan_create(self, project_dir, name, components, nets, title="", expected_revision=None):
        self._require(edit=True)
        project = self.guard.resolve_existing(project_dir)
        if (not project.is_dir() or not _NAME.fullmatch(name) or name.endswith('.')
                or re.fullmatch(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", name.split('.')[0], re.I)):
            raise ValueError("Existing project directory and safe design name are required")
        target = self.guard.resolve_target(project / ".actuator" / "altium" / (name + ".sch-ir.json"))
        with _LOCK, file_lock(target):
            revision = 1
            if target.exists():
                prior = json.loads(target.read_text(encoding="utf-8"))
                if expected_revision != prior["revision"]:
                    raise ValueError("Existing plan requires matching expected_revision")
                revision = prior["revision"] + 1
            elif expected_revision is not None:
                raise ValueError("Plan does not exist")
            plan = {"schema": _SCHEMA, "name": name, "title": str(title), "revision": revision,
                    "project_dir": str(project), "components": deepcopy(components), "nets": deepcopy(nets), "units": "mil", "stub_length": 200}
            validation = self._validate(plan, inspect=True)
            if not validation["ok"]:
                return {"status": "invalid", "validation": validation}
            plan["components"] = validation["components"]
            plan["library_hashes"] = validation["library_hashes"]
            plan["nets"] = validation["nets"]
            if target.exists():
                history = self.guard.resolve_target(target.parent / ".history" / f"{name}.rev{revision - 1:06d}.json")
                _write(history, prior)
            _write(target, plan)
            self.audit.record("altium_plan_create", path=str(target), revision=revision)
            return {"status": "ready", "ir_path": str(target), "revision": revision, "validation": validation}

    def _validate(self, plan, inspect=True):
        errors, normalized, catalogs, libraries = [], [], {}, {}
        components, nets = plan.get("components"), plan.get("nets")
        if not isinstance(components, list) or not components or not isinstance(nets, list) or not nets:
            return {"ok": False, "errors": ["components and nets must be nonempty arrays"]}
        if len(components) > 250 or len(nets) > 2000:
            return {"ok": False, "errors": ["Design exceeds v1 limits (250 placed parts, 2000 nets)"]}
        seen, refs = set(), {}
        for index, original in enumerate(components):
            try:
                component = deepcopy(original)
                ref, symbol = str(component["reference"]), str(component["symbol"])
                if not _REF.fullmatch(ref):
                    raise ValueError("Invalid reference: " + ref)
                part = component.get("part", 1)
                if type(part) is not int or part < 1:
                    raise ValueError("part must be a positive integer")
                key = (ref.casefold(), part)
                if key in seen:
                    raise ValueError("Duplicate reference/part: " + ref)
                seen.add(key)
                source = self.library_guard.resolve_existing(component["library_path"])
                if source.suffix.casefold() not in {".schlib", ".intlib"}:
                    raise ValueError("Invalid symbol library extension")
                if str(source) not in catalogs:
                    catalogs[str(source)] = self.library_inspect(str(source)) if inspect else {"symbols": []}
                catalog = catalogs[str(source)]
                if "symbols" not in catalog:
                    raise RuntimeError("Library inspection incomplete: " + str(catalog.get("operation_id")))
                matches = [s for s in catalog["symbols"] if s["name"].casefold() == symbol.casefold()]
                if len(matches) != 1:
                    raise ValueError("Missing or ambiguous symbol: " + symbol)
                definition = matches[0]
                if part > definition["parts"]:
                    raise ValueError("Symbol part does not exist")
                previous = refs.get(ref.casefold())
                if previous and (previous["library_path"], previous["symbol"], previous["value"]) != (str(source), definition["name"], str(component.get("value", ""))):
                    raise ValueError("Parts sharing a reference must use the same symbol/library/value")
                rotation = component.get("rotation", 0)
                if isinstance(rotation, bool) or rotation not in (0, 90, 180, 270):
                    raise ValueError("rotation must be 0, 90, 180 or 270")
                x, y = component.get("x"), component.get("y")
                if (x is None) != (y is None):
                    raise ValueError("Supply both x and y, or neither")
                auto_position = x is None
                if x is None:
                    x, y = 0, 0
                if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0 or v > 30000 for v in (x, y)):
                    raise ValueError("Coordinates must be finite mil values in 0..30000")
                component.update(reference=ref, library_path=str(source), symbol=definition["name"], part=part,
                                 value=str(component.get("value", "")), x=round(x), y=round(y), rotation=rotation,
                                 source_path=definition["source_path"], pins=[p for p in definition["pins"] if p["part"] in (0, part)])
                component["_auto_position"] = auto_position
                refs[ref.casefold()] = component
                normalized.append(component)
                libraries[str(source)] = catalog["sha256"]
            except (KeyError, TypeError, ValueError, OSError) as exc:
                errors.append(f"component {index}: {exc}")
        # Place automatic components using actual rotated pin extents and room
        # for the longest label, including space before left/down-facing pins.
        extents = []
        for c in normalized:
            origin = dict(c, x=0, y=0)
            points = [self._pin_position(origin, p) for p in c["pins"]]
            extents.append((min((p[0] for p in points), default=0), min((p[1] for p in points), default=0),
                            max((p[0] for p in points), default=0), max((p[1] for p in points), default=0)))
        margin = max(700, max((len(n.get("name", "")) * 60 + 250 for n in nets if isinstance(n, dict) and isinstance(n.get("name"), str)), default=0))
        cell_x = max((b[2] - b[0] for b in extents), default=0) + margin * 2
        cell_y = max((b[3] - b[1] for b in extents), default=0) + margin * 2
        columns = min(4, len(normalized)) or 1
        for index, (c, bounds) in enumerate(zip(normalized, extents)):
            if c.pop("_auto_position"):
                c["x"] = int(math.ceil((margin + (index % columns) * cell_x - bounds[0]) / 100) * 100)
                c["y"] = int(math.ceil((margin + (index // columns) * cell_y - bounds[1]) / 100) * 100)
                if max(c["x"], c["y"]) > 30000:
                    errors.append("Automatic layout exceeds 30000 mil; supply compact explicit positions")
        pin_map = {}
        for comp in normalized:
            for pin in comp["pins"]:
                key = (comp["reference"].casefold(), pin["number"])
                pin_map.setdefault(key, []).append(comp)
        net_names, assigned, normalized_nets = set(), set(), []
        for index, net in enumerate(nets):
            try:
                name = net["name"]
                if not isinstance(name, str) or not name or name != name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
                    raise ValueError("Net name must be 1..100 printable characters without surrounding whitespace")
                if name.casefold() in net_names:
                    raise ValueError("Duplicate/conflicting net name: " + name)
                net_names.add(name.casefold())
                if not isinstance(net["pins"], list) or not net["pins"]:
                    raise ValueError("Net requires at least one pin")
                for endpoint in net["pins"]:
                    key = (str(endpoint["reference"]).casefold(), str(endpoint["pin"]))
                    if key not in pin_map:
                        raise ValueError("Unknown or unplaced pin: " + str(endpoint))
                    if key in assigned:
                        raise ValueError("Pin appears more than once or belongs to two nets: " + str(endpoint))
                    assigned.add(key)
                normalized_nets.append({"name": name, "pins": [{"reference": str(p["reference"]), "pin": str(p["pin"])} for p in net["pins"]]})
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"net {index}: {exc}")
        return {"ok": not errors, "errors": errors, "components": normalized, "library_hashes": libraries, "nets": normalized_nets}

    def validate(self, ir_path):
        self._require()
        _, plan = self._load(ir_path)
        return self._validate(plan)

    @staticmethod
    def _pin_position(component, pin):
        x, y = pin["x"], pin["y"]
        rotation = component["rotation"]
        if rotation == 90:
            x, y = -y, x
        elif rotation == 180:
            x, y = -x, -y
        elif rotation == 270:
            x, y = y, -x
        direction = (pin["orientation"] + rotation // 90) % 4
        dx, dy = ((1, 0), (0, 1), (-1, 0), (0, -1))[direction]
        # AD26's Location is the pin body end. Its compiled electrical endpoint
        # is Location + PinLength along Orientation, verified in live fixtures.
        return component["x"] + x + dx * pin["length"], component["y"] + y + dy * pin["length"], direction

    @staticmethod
    def _overlaps(a, b):
        return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]

    def _routes(self, plan):
        components = plan["components"]
        bodies = []
        for c in components:
            positions = []
            for p in c["pins"]:
                px, py, direction = self._pin_position(c, p)
                dx, dy = ((1, 0), (0, 1), (-1, 0), (0, -1))[direction]
                positions.append((px - dx * p["length"], py - dy * p["length"]))
            if positions:
                bodies.append((min(p[0] for p in positions) - 35, min(p[1] for p in positions) - 35,
                               max(p[0] for p in positions) + 35, max(p[1] for p in positions) + 35))
        for index, body in enumerate(bodies):
            if any(self._overlaps(body, old) for old in bodies[:index]):
                raise ValueError("Component bodies overlap; adjust placement")
        labels, wires, routes = [], [], []
        # Deterministic order makes the layout stable across requests.
        for net in sorted(plan["nets"], key=lambda n: n["name"].casefold()):
            for endpoint in net["pins"]:
                for c in components:
                    if c["reference"].casefold() != endpoint["reference"].casefold():
                        continue
                    for pin in c["pins"]:
                        if pin["number"] != str(endpoint["pin"]):
                            continue
                        x, y, direction = self._pin_position(c, pin)
                        dx, dy = ((1, 0), (0, 1), (-1, 0), (0, -1))[direction]
                        extent = max(100, len(net["name"]) * 55)
                        for length in range(200, 3100, 100):
                            ex, ey = x + dx * length, y + dy * length
                            if direction == 0:
                                box = (ex, ey, ex + extent, ey + 85)
                            elif direction == 2:
                                box = (ex - extent, ey, ex, ey + 85)
                            elif direction == 1:
                                box = (ex - 85, ey, ex, ey + extent)
                            else:
                                box = (ex - 85, ey - extent, ex, ey)
                            wire = (min(x, ex) - 1, min(y, ey) - 1, max(x, ex) + 1, max(y, ey) + 1)
                            if min(box) < 0:
                                continue
                            if any(self._overlaps(box, old) for old in bodies + labels):
                                continue
                            if any(self._overlaps(wire, old) for old in bodies):
                                continue
                            if any(other != net["name"].casefold() and self._overlaps(wire, old) for old, other in wires):
                                continue
                            if any(self._overlaps(box, old) for old, _ in wires) or any(self._overlaps(wire, old) for old in labels):
                                continue
                            labels.append(box)
                            wires.append((wire, net["name"].casefold()))
                            routes.append({"reference": c["reference"], "part": c["part"], "pin": pin["number"],
                                           "x1": x, "y1": y, "x2": ex, "y2": ey, "direction": direction, "net": net["name"]})
                            break
                        else:
                            raise ValueError("Cannot place a clear net label for " + c["reference"] + "." + pin["number"] + "; move the component")
        return routes

    def _request_prior(self, kind, request_key, payload):
        key = hashlib.sha256((self._owner() + "\0" + kind + "\0" + request_key).encode()).hexdigest()
        path = self.state_guard.resolve_target(self.state_root / "requests" / (key + ".json"))
        if path.is_file():
            prior = json.loads(path.read_text(encoding="utf-8"))
            if prior["digest"] != hashlib.sha256(_json(payload).encode()).hexdigest():
                raise ValueError("request_key was already used with different inputs")
            return self.operation_status(prior["operation_id"])
        return None

    def materialize(self, ir_path, request_key, output_dir=None):
        self._require(edit=True)
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
            raise ValueError("request_key must be 1..200 characters")
        path, plan = self._load(ir_path)
        ir_hash = _hash(path)
        project_dir = self.guard.resolve_existing(plan["project_dir"])
        destination = self.guard.resolve_target(output_dir or project_dir / plan["name"])
        payload = {"ir_path": str(path), "ir_sha256": ir_hash, "destination": str(destination)}
        prior = self._request_prior("materialize", request_key, payload)
        if prior:
            if prior["status"] in {"succeeded", "verification_pending", "publishing"} and "result" not in prior:
                return self._finish_materialize(self._op_path(prior["operation_id"]) / "operation.json", prior)
            return prior
        if destination.exists():
            raise FileExistsError("Output directory already exists: " + str(destination))
        if destination.drive.casefold() != project_dir.drive.casefold():
            raise ValueError("Staging and destination must be on the same volume")
        validation = self._validate(plan)
        if not validation["ok"]:
            return {"status": "invalid", "validation": validation}
        if validation["library_hashes"] != plan["library_hashes"]:
            raise ValueError("Library changed; revise and validate the plan first")
        # Geometry always comes from the current authorized library catalog.
        plan["components"] = validation["components"]
        routes = self._routes(plan)
        stage_id = hashlib.sha256((self._owner() + "\0" + request_key + "\0" + str(destination)).encode()).hexdigest()[:32]
        stage = self.guard.resolve_target(project_dir / ".actuator" / "altium" / "staging" / stage_id)
        # A crash before dispatch may leave only deterministic preparation files.
        # Reuse that preparation only if the exact intent matches and no native
        # schematic (which could have delivered effects) has ever appeared.
        stage.mkdir(parents=True, exist_ok=True)
        stage = self.guard.resolve_existing(stage)
        intent = self.guard.resolve_target(stage / 'intent.json')
        if intent.exists():
            if json.loads(intent.read_text(encoding='utf-8')) != payload or list(stage.glob('*.SchDoc')):
                raise RuntimeError('Staged delivery cannot be safely reused; inspect the original operation')
        elif any(stage.iterdir()):
            raise RuntimeError('Unrecognized existing staging directory')
        else:
            _write(intent, payload)
        name = plan["name"]
        project = self.guard.resolve_target(stage / (name + ".PrjPcb"))
        sheet = self.guard.resolve_target(stage / (name + ".SchDoc"))
        project.write_text("[Design]\nVersion=1.0\nHierarchyMode=1\nNetlistSinglePinNets=1\nNameNetsHierarchically=0\n[Document1]\nDocumentPath=" + sheet.name + "\nAnnotationEnabled=1\n", encoding="utf-8")
        _write(self.guard.resolve_target(stage / "plan.json"), plan)
        _write(self.guard.resolve_target(stage / "layout.json"), routes)
        width = math.ceil(max([c["x"] + 1500 for c in plan["components"]] + [max(r["x1"], r["x2"]) + len(r["net"]) * 60 + 500 for r in routes]) / 100) * 100
        height = math.ceil(max([c["y"] + 1500 for c in plan["components"]] + [max(r["y1"], r["y2"]) + len(r["net"]) * 60 + 500 for r in routes]) / 100) * 100
        lines = [f"CreateSheet({_pas(sheet)}, {width}, {height});"]
        for comp in plan["components"]:
            lines.append("PlaceComponent(" + ", ".join([_pas(comp["source_path"]), _pas(comp["symbol"]), _pas(comp["reference"]), _pas(comp["value"]), str(comp["x"]), str(comp["y"]), str(comp["rotation"]), str(comp["part"])]) + ");")
        for route in routes:
            lines.append("ConnectPin(" + ", ".join([_pas(route["reference"]), str(route["part"]), _pas(route["pin"]), *[str(route[k]) for k in ("x1", "y1", "x2", "y2", "direction")], _pas(route["net"])]) + ");")
        # Allow AD's event loop to finish loading a new project before compiling it
        # in a separate script dispatch. Compiling immediately can report zero nets.
        lines.extend(["SaveSheet;", f"PrepareProject({_pas(project)});"])
        result = self._run("\n".join(lines), kind="materialize", request_key=request_key, payload=payload)
        if result["status"] == "succeeded":
            return self._finish_materialize(self._op_path(result["operation_id"]) / "operation.json", result)
        return result

    def _stage(self, data):
        payload = data["payload"]
        path, plan = self._load(payload["ir_path"])
        if _hash(path) != payload["ir_sha256"]:
            raise ValueError("Plan changed while Altium was executing")
        # Recover staging from the saved script's exact sheet path via operation evidence.
        saved = [r for r in data.get("rows", []) if r.get("kind") == "saved"]
        if len(saved) != 1:
            raise ValueError("Expected one saved schematic in Altium response")
        sheet = self.guard.resolve_existing(saved[0]["path"])
        if sheet.name != plan["name"] + ".SchDoc" or sheet.parent.parent.name != "staging":
            raise ValueError("Unexpected staged schematic location")
        return path, plan, sheet.parent

    @staticmethod
    def _comparison(plan, rows, expected_sheet_path=None):
        comparison = compare_nets(plan, rows)
        expected = sorted((c["reference"].casefold(), c["symbol"].casefold(), c.get("part", 1), c.get("value", "")) for c in plan["components"])
        observed = sorted((r["reference"].casefold(), r["symbol"].casefold(), r["part"], r["value"]) for r in rows if r.get("kind") == "observed_component")
        if expected != observed:
            comparison["ok"] = False
            comparison["errors"].append({"error": "Saved component inventory differs", "expected": expected, "actual": observed})
        if expected_sheet_path is not None:
            projects = [r for r in rows if r.get('kind') == 'project']
            documents = [r for r in rows if r.get('kind') == 'document']
            expected_path = str(Path(expected_sheet_path).resolve()).casefold()
            if (len(projects) != 1 or projects[0].get('documents') != 1 or len(documents) != 1
                    or str(Path(documents[0].get('path', '')).resolve()).casefold() != expected_path):
                comparison['ok'] = False
                comparison['errors'].append({'error':'Expected exactly one compiled schematic document at the requested path',
                                             'expected_sheet_path':str(expected_sheet_path), 'projects':projects, 'documents':documents})
            sheets = [r for r in rows if r.get('kind') == 'observed_sheet']
            labels = [r for r in rows if r.get('kind') == 'observed_label']
            objects = [r for r in rows if r.get('kind') in {'observed_component','observed_label'}]
            layout_errors = []
            if len(sheets) != 1:
                layout_errors.append('Saved sheet dimensions are missing')
            else:
                width, height = sheets[0]['width'], sheets[0]['height']
                for obj in objects:
                    box = obj.get('bounds', [])
                    if len(box) != 4 or box[0] < 0 or box[1] < 0 or box[2] > width or box[3] > height:
                        layout_errors.append({'error':'Object lies outside the saved sheet', 'object':obj})
                components = [r for r in objects if r['kind'] == 'observed_component']
                for index, component in enumerate(components):
                    box = component.get('bounds', [])
                    if len(box) == 4 and any(len(other.get('bounds', [])) == 4 and AltiumAdapter._overlaps(box, other['bounds']) for other in components[:index]):
                        layout_errors.append({'error':'Saved component rectangles overlap', 'component':component})
                for index, label in enumerate(labels):
                    box = label.get('bounds', [])
                    if len(box) != 4:
                        continue
                    obstacles = labels[:index] + [r for r in objects if r['kind'] == 'observed_component']
                    if any(len(other.get('bounds', [])) == 4 and AltiumAdapter._overlaps(box, other['bounds']) for other in obstacles):
                        layout_errors.append({'error':'Saved net label overlaps another label or component', 'label':label})
            comparison['layout'] = {'ok':not layout_errors, 'errors':layout_errors, 'source':'Altium saved-object bounding rectangles'}
            if layout_errors:
                comparison['ok'] = False
                comparison['errors'].append({'error':'Saved schematic layout verification failed', 'details':layout_errors})
        return comparison

    def _finish_materialize(self, path, data):
        self._require(edit=True)
        if "result" in data:
            return data
        if data.get('publication'):
            return self._recover_publication(path, data, allow_publish=True)
        try:
            ir_path, plan, stage = self._stage(data)
            destination = self.guard.resolve_target(data["payload"]["destination"])
            sheet = stage / (plan["name"] + ".SchDoc")
            project = stage / (plan["name"] + ".PrjPcb")
            project = self.guard.resolve_existing(project)
            sheet = self.guard.resolve_existing(sheet)
            normalization = self._run(f"NormalizeOwnedSheet({_pas(project)}, {_pas(sheet)});", kind="materialize_normalize",
                                      request_key=data["operation_id"] + ":normalize",
                                      payload={"parent": data["operation_id"], "project": str(project), "sheet": str(sheet)})
            data["normalization_operation_id"] = normalization["operation_id"]
            if normalization["status"] in {"running", "unknown"}:
                data.update(status="verification_pending", verification_operation_id=normalization["operation_id"], publication_pending=True)
                _write(path, data)
                return data
            if normalization["status"] != "succeeded":
                data.update(status="failed", error="Owned staged schematic normalization failed", normalization=normalization)
                _write(path, data)
                return data
            baseline = {"project": _hash(project), "schematic": _hash(sheet)}
            verification = self._run(f"VerifyProject({_pas(project)}, {_pas(sheet)});\nCloseOwnedProject({_pas(project)}, {_pas(sheet)});", kind="materialize_verify",
                                     request_key=data["operation_id"], payload={"parent": data["operation_id"], "baseline": baseline})
            data["verification_operation_id"] = verification["operation_id"]
            if verification["status"] in {"running", "unknown"}:
                data.update(status="verification_pending", publication_pending=True)
                _write(path, data)
                return data
            if verification["status"] != "succeeded":
                data.update(status="failed", error="Altium verification failed", verification=verification)
                _write(path, data)
                return data
            if baseline != {"project": _hash(project), "schematic": _hash(sheet)}:
                raise ValueError("Staged files changed during verification")
            comparison = self._comparison(plan, verification["rows"], sheet)
            data["verification_rows"] = verification["rows"]
            data["status"] = "succeeded"
            report = {"operation_id": data["operation_id"], "ir_sha256": data["payload"]["ir_sha256"],
                      "schematic_sha256": baseline["schematic"], "project_sha256": baseline["project"],
                      "comparison": comparison, "altium_evidence": verification["rows"],
                      "verification_operation_id": verification.get("operation_id"),
                      "verification_adapter_sha256": verification.get("adapter_sha256"),
                      "verification_finished_at": verification.get("finished_at"),
                      "verified_at": time.time()}
            _write(self.guard.resolve_target(stage / "verification.json"), report)
            if not comparison["ok"]:
                data.update(status="failed", result={"comparison": comparison, "report_path": str(stage / "verification.json")})
            else:
                if destination.exists():
                    raise FileExistsError("Destination appeared before publication")
                if baseline != {"project": _hash(project), "schematic": _hash(sheet)}:
                    raise ValueError("Staged files changed before publication")
                data.update(status='publishing', publication={'stage':str(stage), 'destination':str(destination),
                    'baseline':baseline, 'project_name':project.name, 'schematic_name':sheet.name,
                    'report_sha256':_hash(stage/'verification.json')})
                _write(path, data)  # Durable intent before the atomic directory move.
                return self._recover_publication(path, data, allow_publish=True)
        except (ValueError, OSError) as exc:
            data.update(status="failed", error=str(exc))
        _write(path, data)
        return data

    def _recover_publication(self, path, data, *, allow_publish):
        """Reconcile either side of rename without dispatching Altium again."""
        if data.get("status") not in {"publishing", "succeeded"}:
            return data
        publication = data['publication']
        try:
            destination = self.guard.resolve_target(publication['destination'])
            stage = self.guard.resolve_target(publication['stage'])
            source = destination if destination.exists() else stage
            if source == stage and not allow_publish:
                return dict(data, publication_pending=True)
            if source == stage:
                self._require(edit=True)
            project_name, sheet_name = publication['project_name'], publication['schematic_name']
            if any(Path(name).name != name for name in (project_name, sheet_name)):
                raise ValueError('Invalid publication artifact names')
            source_guard = self.guard
            report_path = source_guard.resolve_existing(source/'verification.json')
            if _hash(report_path) != publication['report_sha256']:
                raise ValueError('Publication verification report changed')
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if (report['operation_id'] != data['operation_id'] or report['ir_sha256'] != data['payload']['ir_sha256']
                    or report.get('comparison', {}).get('ok') is not True):
                raise ValueError('Publication receipt does not match the verified operation')
            def hashes(folder):
                folder_guard = self.guard
                return {'project':_hash(folder_guard.resolve_existing(folder/project_name)),
                        'schematic':_hash(folder_guard.resolve_existing(folder/sheet_name))}
            if hashes(source) != publication['baseline']:
                raise ValueError('Publication artifacts changed after verification')
            if source == stage:
                destination.parent.mkdir(parents=True, exist_ok=True)
                stage.rename(destination)  # Refuses a concurrent destination on Windows.
            if hashes(destination) != publication['baseline']:
                raise ValueError('Published artifacts changed; verification is no longer current')
            data.update(status='succeeded', published_at=time.time(), result={
                'project_path':str(destination/project_name), 'schematic_path':str(destination/sheet_name),
                'report_path':str(destination/'verification.json'), 'comparison':report['comparison']})
            data.pop('publication_pending', None)
        except (OSError, ValueError, KeyError) as exc:
            data.update(status='failed', error=str(exc))
            _write(path, data)
            return data
        try:
            _write(path, data)
        except OSError as exc:
            # The earlier durable publishing intent still permits reconciliation.
            # Never turn a proven publication into an invented delivery failure.
            return dict(data, status='publishing', receipt_error=str(exc), publication_pending=True)
        report_fingerprint = report.get("verification_adapter_sha256")
        verification_operation_id = report.get("verification_operation_id")
        verification_finished_at = report.get("verification_finished_at")
        fingerprint = self._adapter_fingerprint()
        eligible = (fingerprint is not None and report_fingerprint == fingerprint
                    and isinstance(verification_operation_id, str)
                    and re.fullmatch(r"[0-9a-f]{32}", verification_operation_id) is not None
                    and isinstance(verification_finished_at, (int, float))
                    and not isinstance(verification_finished_at, bool)
                    and math.isfinite(verification_finished_at) and verification_finished_at > 0)
        if eligible:
            try:
                self._update_capability_evidence(operation_id=verification_operation_id,
                                                 provenance="materialize_publication",
                                                 script_dispatch=True, connectivity=True,
                                                 checked_at=verification_finished_at)
            except OSError as exc:
                data['diagnostic_warning'] = 'Publication succeeded; ancillary evidence/audit write failed: ' + str(exc)
                try:
                    _write(path, data)
                except OSError:
                    pass
        try:
            self.audit.record('altium_schematic_published', operation_id=data['operation_id'], path=str(destination))
        except OSError as exc:
            data['diagnostic_warning'] = 'Publication succeeded; ancillary evidence/audit write failed: ' + str(exc)
            try:
                _write(path, data)
            except OSError:
                pass
        return data

    def verify(self, ir_path, project_path):
        self._require()
        _, plan = self._load(ir_path)
        project = self.guard.resolve_existing(project_path)
        if project.suffix.casefold() != ".prjpcb":
            raise ValueError("Expected a .PrjPcb file")
        sheet = self.guard.resolve_existing(project.with_suffix(".SchDoc"))
        baseline = {"project": _hash(project), "schematic": _hash(sheet)}
        prepared = self._run(f"PrepareProject({_pas(project)});", kind="prepare_verify")
        if prepared["status"] != "succeeded":
            return prepared
        result = self._run(f"VerifyProject({_pas(project)}, {_pas(sheet)});", kind="verify",
                           payload={"ir_path": str(ir_path), "project_path": str(project), "baseline": baseline})
        rows = self._rows(result)
        if rows is not None:
            if baseline != {"project": _hash(project), "schematic": _hash(sheet)}:
                raise ValueError("Files changed during verification")
            result["comparison"] = self._comparison(plan, rows, sheet)
            finished_at = result.get("finished_at")
            operation_id = result.get("operation_id")
            eligible = (result.get("adapter_sha256") == self._adapter_fingerprint()
                        and self._adapter_fingerprint() is not None
                        and isinstance(operation_id, str) and re.fullmatch(r"[0-9a-f]{32}", operation_id) is not None
                        and isinstance(finished_at, (int, float)) and not isinstance(finished_at, bool)
                        and math.isfinite(finished_at) and finished_at > 0)
            if result["comparison"].get("ok") is True and eligible:
                try:
                    self._update_capability_evidence(operation_id=operation_id,
                                                     provenance="saved_native_connectivity_comparison",
                                                     script_dispatch=True, connectivity=True,
                                                     checked_at=finished_at)
                except OSError as exc:
                    result["diagnostic_warning"] = "Verification succeeded; capability evidence write failed: " + str(exc)
        return result
