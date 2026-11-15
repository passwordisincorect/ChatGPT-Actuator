"""Non-destructive Main/Worker account verification summary.

This is a static preflight check, NOT proof that a ChatGPT task will succeed.
No session contents or secrets are included in the returned report.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


_CONNECTED_STATES = {"signed_in", "signed_out", "session_unavailable", "no_chatgpt_tab"}
_AUTH_BLOCKED = {"authorization_required", "cdp_unavailable", "cdp_error",
                 "cdp_timeout", "playwright_unavailable", "no_browser_context"}


def _item(item_id: str, state: str, reason_code: str) -> dict[str, str]:
    return {"id": item_id, "state": state, "reason_code": reason_code}


def _connection(identity: dict[str, Any], side: str) -> dict[str, str]:
    state = str(identity.get("state") or "not_checked")
    if state in _CONNECTED_STATES:
        return _item(side + "_cdp", "ok", "CDP_CONNECTED")
    if state == "authorization_required":
        return _item(side + "_cdp", "warning", "CDP_AUTH_REQUIRED")
    if state == "cdp_timeout":
        return _item(side + "_cdp", "warning", "CDP_TIMEOUT")
    if state in _AUTH_BLOCKED:
        return _item(side + "_cdp", "warning", "CDP_UNAVAILABLE")
    return _item(side + "_cdp", "warning", "CDP_UNKNOWN")


def _identity(identity: dict[str, Any], side: str) -> dict[str, str]:
    if identity.get("state") == "signed_in" and identity.get("authenticated"):
        return _item(side + "_identity", "ok", "SIGNED_IN")
    if identity.get("state") == "signed_out":
        return _item(side + "_identity", "blocked", "SIGNED_OUT")
    if identity.get("state") == "no_chatgpt_tab":
        return _item(side + "_identity", "warning", "NO_CHATGPT_TAB")
    return _item(side + "_identity", "warning", "IDENTITY_UNKNOWN")


def assess_account_preflight(
    browser_accounts: dict[str, Any],
    orchestrator: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    """Produce an explicit assessment from *fresh* detection results.

    Callers must invoke identity detection before constructing this report.
    """
    if mode not in {"same", "separate"}:
        raise ValueError("mode must be same or separate")

    detection = browser_accounts.get("identity_detection") or {}
    main = detection.get("main") or {}
    worker = detection.get("worker") or {}
    configured = bool(browser_accounts.get("config_matches"))
    worker_running = bool(browser_accounts.get("worker_edge_running"))
    orch_running = bool(orchestrator.get("running"))
    orch_healthy = bool(orchestrator.get("health_ok"))
    jobs = int(orchestrator.get("active_job_count") or 0)
    tasks = int(orchestrator.get("active_task_count") or 0)
    busy = jobs > 0 or tasks > 0

    checks = [
        _item("config", "ok" if configured else "blocked",
              "CONFIG_OK" if configured else "CONFIG_MISMATCH"),
        _connection(main, "main"),
        _identity(main, "main"),
        _connection(worker, "worker"),
        _identity(worker, "worker"),
    ]

    if mode == "separate":
        checks.append(_item("alignment", "skipped", "SEPARATE_MODE"))
    else:
        same = detection.get("same_login_detected")
        checks.append(_item(
            "alignment", "ok" if same is True else ("blocked" if same is False else "warning"),
            "ACCOUNTS_MATCH" if same is True else (
                "ACCOUNTS_DIFFER" if same is False else "ALIGNMENT_UNKNOWN"),
        ))

    if not configured:
        preflight = _item("worker_preflight", "blocked", "CONFIG_MISMATCH")
    elif not worker_running:
        preflight = _item("worker_preflight", "blocked", "WORKER_NOT_RUNNING")
    elif not (worker.get("state") == "signed_in" and worker.get("authenticated")):
        preflight = _item("worker_preflight", "warning", "WORKER_NOT_VERIFIED")
    elif not orch_running or not orch_healthy:
        preflight = _item("worker_preflight", "warning", "ORCH_UNAVAILABLE")
    elif busy:
        preflight = _item("worker_preflight", "warning", "WORKER_BUSY")
    else:
        preflight = _item("worker_preflight", "ok", "STATIC_PREFLIGHT_OK")
    checks.append(preflight)

    if any(item["state"] == "blocked" for item in checks):
        overall = "blocked"
    elif any(item["state"] == "warning" for item in checks):
        overall = "attention"
    else:
        overall = "ready"

    return {
        "mode": mode,
        "profile_id": browser_accounts.get("active_account_id"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "verification_scope": "static_preflight_only",
        "worker_static_ready": preflight["state"] == "ok",
        "orchestrator_busy": busy,
        "checks": checks,
        "identities": {
            "main": {k: main.get(k) for k in ("state", "authenticated", "name", "email", "detected_at")},
            "worker": {k: worker.get(k) for k in ("state", "authenticated", "name", "email", "detected_at")},
        },
    }
