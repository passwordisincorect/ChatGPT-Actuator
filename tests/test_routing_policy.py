from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from chatgpt_actuator.routing_policy import RoutingPolicyEngine


class RoutingPolicyEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy_path = Path(self.tmp.name) / "routing-policy.json"
        self.policy_path.write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "scope": "test",
                    "default_route": "actuator",
                    "actuator": {"use_for": ["local"]},
                    "orchestrator": {"use_for": ["complex"]},
                    "loop_guard": {
                        "main_to_actuator": True,
                        "main_to_orchestrator": True,
                        "orchestrator_to_actuator": True,
                        "actuator_to_orchestrator": False,
                        "worker_to_orchestrator": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.engine = RoutingPolicyEngine(self.policy_path)

    def test_policy_info_loads_file(self):
        info = self.engine.info()
        self.assertTrue(info["loaded"])
        self.assertEqual(info["version"], "1.0")
        self.assertEqual(info["default_route"], "actuator")

    def test_local_action_routes_to_actuator(self):
        result = self.engine.decide(
            "Read a local file",
            local_machine_action=True,
            short_deterministic_workflow=True,
        )
        self.assertEqual(result["recommended_route"], "actuator")
        self.assertFalse(result["guard"]["blocked"])

    def test_complex_signal_has_priority_over_local_signal(self):
        result = self.engine.decide(
            "Inspect and independently review a complex local project",
            local_machine_action=True,
            complex_multi_step=True,
            review_rework=True,
        )
        self.assertEqual(result["requested_route"], "orchestrator")
        self.assertEqual(result["recommended_route"], "orchestrator")
        self.assertFalse(result["guard"]["blocked"])

    def test_actuator_cannot_route_up_to_orchestrator(self):
        result = self.engine.decide(
            "Delegate this to independent workers",
            origin="actuator",
            independent_workers=True,
        )
        self.assertEqual(result["requested_route"], "orchestrator")
        self.assertEqual(result["recommended_route"], "actuator")
        self.assertTrue(result["guard"]["blocked"])
        self.assertTrue(result["guard"]["fallback_applied"])

    def test_orchestrator_can_route_local_action_down_to_actuator(self):
        result = self.engine.decide(
            "Perform a local deterministic action",
            origin="orchestrator",
            local_machine_action=True,
        )
        self.assertEqual(result["recommended_route"], "actuator")
        self.assertFalse(result["guard"]["blocked"])

    def test_worker_cannot_reenter_orchestrator(self):
        result = self.engine.decide(
            "Ask orchestrator to review again",
            origin="worker",
            review_rework=True,
        )
        self.assertEqual(result["requested_route"], "orchestrator")
        self.assertEqual(result["recommended_route"], "direct")
        self.assertTrue(result["guard"]["blocked"])

    def test_no_execution_routes_direct(self):
        result = self.engine.decide(
            "Explain what a router is",
            requires_execution=False,
        )
        self.assertEqual(result["recommended_route"], "direct")

    def test_empty_goal_is_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.decide("   ")


if __name__ == "__main__":
    unittest.main()
