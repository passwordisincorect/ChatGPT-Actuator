"""Bind native AD26 ERC evidence to the saved project's actual ERC settings."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


_RULE = re.compile(r"Type([1-9][0-9]*)\Z", re.IGNORECASE)
_MATRIX_ROW = re.compile(r"L([1-9][0-9]*)\Z", re.IGNORECASE)
_MATRIX_LEVELS = frozenset("NWEF")
# Qualified AD26.10.1 saves serialize Type1..Type165 except Type120/Type121.
_SAVED_RULES = frozenset(range(1, 166)) - {120, 121}


def _decode(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def _sections(text):
    result, section = {}, None
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().casefold()
            if not section or section in result:
                raise ValueError(f"duplicate or empty project section at line {line_number}")
            result[section] = {}
            continue
        if section is None or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        folded = key.casefold()
        if folded in result[section]:
            raise ValueError(f"duplicate project setting at line {line_number}")
        result[section][folded] = value
    return result


def _configured(project_path):
    path = Path(project_path).resolve(strict=True)
    if not path.is_file() or path.suffix.casefold() != ".prjpcb":
        raise ValueError("project_path must identify an existing .PrjPcb file")
    data = path.read_bytes()
    sections = _sections(_decode(data))
    errors = {}
    for key, value in sections.get("electrical rules check", {}).items():
        match = _RULE.fullmatch(key)
        if not match:
            if key.startswith("type"):
                raise ValueError(f"malformed Error Reporting rule key {key}")
            continue
        if value not in {"0", "1", "2", "3"}:
            raise ValueError(f"invalid electrical rule level for {key}")
        errors[f"Type{int(match.group(1))}"] = int(value)
    if set(errors) != {f"Type{i}" for i in _SAVED_RULES}:
        raise ValueError("AD26.10.1 Error Reporting TypeN coverage is missing or unexpected")
    matrix = {}
    for key, value in sections.get("erc connection matrix", {}).items():
        match = _MATRIX_ROW.fullmatch(key)
        if not match:
            if key.startswith("l"):
                raise ValueError(f"malformed ERC connection matrix row key {key}")
            continue
        row_number = int(match.group(1))
        if row_number > 17 or len(value) != 17 or any(level not in _MATRIX_LEVELS for level in value):
            raise ValueError(f"invalid ERC connection matrix row {key}")
        matrix[f"L{row_number}"] = value
    if set(matrix) != {f"L{i}" for i in range(1, 18)}:
        raise ValueError("complete 17-by-17 ERC connection matrix is missing")
    suppressed = sections.get("design", {}).get("reportsuppressederrorsinmessages")
    if suppressed not in {"0", "1"}:
        raise ValueError("ReportSuppressedErrorsInMessages is missing or invalid")
    return {"project_sha256": hashlib.sha256(data).hexdigest(), "project_path": str(path),
            "report_suppressed_errors": suppressed == "1", "error_reporting": errors,
            "connection_matrix": matrix}


def _native_error_settings(summary, rows):
    settings = [r for r in rows if r.get("kind") == "erc_error_setting"]
    if summary.get("error_reporting_complete") is not True:
        return None, "native error-reporting capture is incomplete"
    if type(summary.get("error_reporting_setting_count")) is not int or summary["error_reporting_setting_count"] != len(settings):
        return None, "native error-reporting setting count is inconsistent"
    effective = summary.get("effective_error_reporting")
    if isinstance(effective, dict):
        normalized = {}
        for key, value in effective.items():
            match = _RULE.fullmatch(key) if isinstance(key, str) else None
            if not match or type(value) is not int or value not in {0, 1, 2, 3}:
                return None, "native effective error-reporting settings are malformed"
            canonical = f"Type{int(match.group(1))}"
            if canonical in normalized:
                return None, "duplicate native mapping for saved error-reporting rule"
            normalized[canonical] = value
        return normalized, None
    return None, "native effective error-reporting settings are unavailable"


def _reload_config(rows, config, project_path, evidence):
    """Validate native reopen/compile evidence before trusting saved settings as active."""
    if not isinstance(evidence, dict) or set(evidence) != {
            "kind", "complete", "previously_open", "project_path", "sheet_path", "project_sha256",
            "sheet_sha256", "prepare_operation_id", "inspection_operation_id", "adapter_sha256"}:
        return False
    if (evidence.get("kind") != "project_configuration_reload" or evidence.get("complete") is not True
            or type(evidence.get("previously_open")) is not bool
            or not isinstance(evidence.get("project_path"), str)
            or not isinstance(evidence.get("sheet_path"), str)):
        return False
    hashes = ("project_sha256", "sheet_sha256", "adapter_sha256")
    ids = ("prepare_operation_id", "inspection_operation_id")
    if any(not isinstance(evidence.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", evidence[key]) is None for key in hashes):
        return False
    if any(not isinstance(evidence.get(key), str) or re.fullmatch(r"[0-9a-f]{32}", evidence[key]) is None for key in ids):
        return False
    if evidence[ids[0]] == evidence[ids[1]] or evidence["project_sha256"] != config["project_sha256"]:
        return False
    project = Path(project_path).resolve(strict=True)
    if Path(evidence["project_path"]).resolve(strict=True) != project:
        return False
    text = _decode(project.read_bytes())
    sections = _sections(text)
    if sum(bool(re.fullmatch(r"configuration[0-9]+", name)) for name in sections) > 1 or any(
            re.fullmatch(r"variant[0-9]+", name) for name in sections):
        return False
    docs = re.findall(r"(?im)^\s*DocumentPath\s*=\s*(.*?)\s*$", text)
    if len(docs) != 1 or not docs[0]:
        return False
    document_path = docs[0].strip('"')
    if Path(document_path).is_absolute():
        return False
    base = project.parent.resolve()
    sheet = (base / document_path).resolve(strict=True)
    if not sheet.is_file() or not sheet.is_relative_to(base) or sheet.suffix.casefold() != ".schdoc":
        return False
    if Path(evidence["sheet_path"]).resolve(strict=True) != sheet:
        return False
    if hashlib.sha256(sheet.read_bytes()).hexdigest() != evidence["sheet_sha256"]:
        return False
    compile_summaries = [r for r in rows if r.get("kind") == "compile_summary"]
    if len(compile_summaries) != 1 or compile_summaries[0].get("success") is not True or compile_summaries[0].get("complete") is not True:
        return False
    docs_rows = [r for r in rows if r.get("kind") == "project"]
    if len(docs_rows) != 1 or docs_rows[0].get("documents") != 1 or Path(docs_rows[0].get("path", "")).resolve() != project:
        return False
    if docs_rows[0].get("variants", 0) != 0:
        return False
    native_docs = [r for r in rows if r.get("kind") == "document"]
    if len(native_docs) != 1 or Path(native_docs[0].get("path", "")).resolve(strict=True) != sheet:
        return False
    operation_rows = [r for r in rows if "operation_id" in r]
    return (len(operation_rows) == 1
            and operation_rows[0].get("operation_id") == evidence["inspection_operation_id"])


def annotate(rows, project_path, *, reload_evidence=None):
    """Return copied native rows, adding hash-bound config and conservative ERC completeness."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("native rows must be a list of objects")
    result = [dict(row) for row in rows]
    summaries = [row for row in result if row.get("kind") == "erc_summary"]
    if len(summaries) != 1:
        raise ValueError("exactly one native ERC summary is required")
    summary = summaries[0]
    config, error = None, None
    try:
        config = _configured(project_path)
        native, error = _native_error_settings(summary, result)
        reloaded = _reload_config(result, config, project_path, reload_evidence)
        if not reloaded:
            error = "native project configuration reload evidence is missing or invalid"
        if native is not None:
            if native != config["error_reporting"]:
                error = "native error-reporting levels do not match the saved project"
        else:
            claimed = "effective_error_reporting" in summary
            if claimed:
                error = error or ("native effective error-reporting settings are malformed" if claimed else "native effective error-reporting settings are unavailable and reload evidence is missing or invalid")
            else:
                if reloaded:
                    error = None
                if reloaded:
                    config["method"] = "saved_project_natively_reloaded"
                    config["native_error_reporting"] = None
                    config["connection_matrix_verified_from_saved_project"] = True
        if error is None and summary.get("report_suppressed") is not config["report_suppressed_errors"]:
            error = "native suppressed-error setting does not match the saved project"
        if summary.get("complete") is not True:
            error = "native ERC violation capture is incomplete"
        if type(summary.get("violation_count")) is not int or summary["violation_count"] != sum(
                row.get("kind") == "erc_violation" for row in result):
            error = "native ERC violation count is inconsistent"
        if native is not None:
            config["native_error_reporting"] = native
            config["connection_matrix_verified_from_saved_project"] = True
    except (OSError, UnicodeError, ValueError) as exc:
        error = str(exc)
    if config is not None:
        config.setdefault("native_error_reporting", None)
    summary["config"] = config or {}
    summary["configuration_complete"] = error is None
    if error:
        summary["configuration_note"] = error
    else:
        summary.pop("configuration_note", None)
    return result
