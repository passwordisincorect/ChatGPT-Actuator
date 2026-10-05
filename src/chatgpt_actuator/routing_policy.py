from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class RoutingPolicyError(RuntimeError):
    """Raised when the local routing policy cannot be loaded safely."""


class RoutingPolicyEngine:
    """Small, local, deterministic routing policy engine.

    The engine recommends an execution plane only. It never invokes Actuator,
    Orchestrator, workers, or any other connector by itself.
    """

    VALID_ORIGINS = {"main", "actuator", "orchestrator", "worker"}
    VALID_ROUTES = {"direct", "actuator", "orchestrator"}

    def __init__(self, policy_path: str | Path | None = None) -> None:
        workspace_root = Path(__file__).resolve().parents[3]
        configured = policy_path or os.environ.get("CHATGPT_ROUTING_POLICY")
        self.policy_path = (
            Path(configured).expanduser()
            if configured
            else workspace_root / "routing-policy.json"
        )

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RoutingPolicyError(
                f"Routing policy not found: {self.policy_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RoutingPolicyError(
                f"Routing policy is invalid JSON: {self.policy_path}: {exc}"
            ) from exc
        except OSError as exc:
            raise RoutingPolicyError(
                f"Unable to read routing policy: {self.policy_path}: {exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise RoutingPolicyError("Routing policy root must be a JSON object.")

        default_route = str(raw.get("default_route", "")).strip().lower()
        if default_route not in {"actuator", "orchestrator"}:
            raise RoutingPolicyError(
                "routing-policy.json default_route must be 'actuator' or 'orchestrator'."
            )

        loop_guard = raw.get("loop_guard")
        if not isinstance(loop_guard, dict):
            raise RoutingPolicyError(
                "routing-policy.json must contain a loop_guard object."
            )

        required_guard_keys = {
            "main_to_actuator",
            "main_to_orchestrator",
            "orchestrator_to_actuator",
            "actuator_to_orchestrator",
            "worker_to_orchestrator",
        }
        missing = sorted(required_guard_keys - set(loop_guard))
        if missing:
            raise RoutingPolicyError(
                "routing-policy.json loop_guard missing keys: " + ", ".join(missing)
            )

        return raw

    def info(self) -> dict[str, Any]:
        policy = self._load()
        stat = self.policy_path.stat()
        return {
            "loaded": True,
            "policy_path": str(self.policy_path),
            "version": str(policy.get("version", "unknown")),
            "scope": str(policy.get("scope", "unknown")),
            "default_route": str(policy["default_route"]).lower(),
            "loop_guard": dict(policy["loop_guard"]),
            "actuator": policy.get("actuator", {}),
            "orchestrator": policy.get("orchestrator", {}),
            "account_switch": policy.get("account_switch", {}),
            "modified_unix": stat.st_mtime,
        }

    @staticmethod
    def _guard_allowed(
        origin: str,
        target: str,
        loop_guard: dict[str, Any],
    ) -> tuple[bool, str | None]:
        if target == "direct":
            return True, None

        # Continuing inside Actuator is not a cross-service handoff.
        if origin == "actuator" and target == "actuator":
            return True, None

        # Re-entering Orchestrator from itself is always a recursion hazard.
        if origin == "orchestrator" and target == "orchestrator":
            return False, "orchestrator_to_orchestrator"

        key = f"{origin}_to_{target}"
        if key not in loop_guard:
            return False, key
        return bool(loop_guard[key]), key

    @staticmethod
    def _fallback_route(
        origin: str,
        loop_guard: dict[str, Any],
    ) -> str:
        if origin == "actuator":
            return "actuator"
        if origin == "orchestrator":
            if bool(loop_guard.get("orchestrator_to_actuator", False)):
                return "actuator"
            return "direct"
        if origin == "worker":
            return "direct"

        if bool(loop_guard.get("main_to_actuator", False)):
            return "actuator"
        return "direct"

    def decide(
        self,
        goal: str,
        *,
        origin: str = "main",
        requires_execution: bool = True,
        local_machine_action: bool = False,
        short_deterministic_workflow: bool = False,
        complex_multi_step: bool = False,
        independent_workers: bool = False,
        parallel_reasoning: bool = False,
        review_rework: bool = False,
        restart_recovery: bool = False,
    ) -> dict[str, Any]:
        origin = str(origin).strip().lower()
        if origin not in self.VALID_ORIGINS:
            raise ValueError(
                "origin must be one of: main, actuator, orchestrator, worker"
            )

        goal = str(goal).strip()
        if not goal:
            raise ValueError("goal must not be empty")

        policy = self._load()
        loop_guard = dict(policy["loop_guard"])

        complex_signals = {
            "complex_multi_step": bool(complex_multi_step),
            "independent_workers": bool(independent_workers),
            "parallel_reasoning": bool(parallel_reasoning),
            "review_rework": bool(review_rework),
            "restart_recovery": bool(restart_recovery),
        }
        local_signals = {
            "local_machine_action": bool(local_machine_action),
            "short_deterministic_workflow": bool(short_deterministic_workflow),
        }

        if not requires_execution:
            desired = "direct"
            reason = "No execution/delegation is required; MAIN can answer directly."
        elif any(complex_signals.values()):
            desired = "orchestrator"
            active = [name for name, enabled in complex_signals.items() if enabled]
            reason = (
                "Complex/delegated execution signals favor ChatGPT-Orchestrator: "
                + ", ".join(active)
                + "."
            )
        elif any(local_signals.values()):
            desired = "actuator"
            active = [name for name, enabled in local_signals.items() if enabled]
            reason = (
                "Direct local execution signals favor ChatGPT-Actuator: "
                + ", ".join(active)
                + "."
            )
        else:
            desired = str(policy["default_route"]).lower()
            reason = (
                "No explicit routing signal was supplied; using policy default_route="
                + desired
                + "."
            )

        allowed, guard_key = self._guard_allowed(origin, desired, loop_guard)
        recommended = desired
        fallback_applied = False

        if not allowed:
            recommended = self._fallback_route(origin, loop_guard)
            fallback_applied = True
            reason += (
                f" Loop guard blocked {origin}->{desired}; "
                f"safe fallback is {recommended}."
            )

        return {
            "goal": goal,
            "origin": origin,
            "requested_route": desired,
            "recommended_route": recommended,
            "requires_execution": bool(requires_execution),
            "signals": {
                **local_signals,
                **complex_signals,
            },
            "reason": reason,
            "guard": {
                "key": guard_key,
                "allowed": allowed,
                "blocked": not allowed,
                "fallback_applied": fallback_applied,
            },
            "policy": {
                "version": str(policy.get("version", "unknown")),
                "scope": str(policy.get("scope", "unknown")),
                "default_route": str(policy["default_route"]).lower(),
                "path": str(self.policy_path),
            },
        }
