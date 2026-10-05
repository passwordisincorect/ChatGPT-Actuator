from __future__ import annotations

import unittest

from chatgpt_actuator import server


class RoutingMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_tools_advertise_direct_local_role(self):
        tools = await server.mcp.list_tools()
        by_name = {tool.name: tool for tool in tools}

        readonly = by_name["batch_readonly"].description or ""
        execute = by_name["batch_execute"].description or ""

        self.assertIn("direct local", readonly.lower())
        self.assertIn("ChatGPT-Orchestrator", readonly)
        self.assertIn("direct local", execute.lower())
        self.assertIn("ChatGPT-Orchestrator", execute)

    async def test_routing_tools_are_exposed(self):
        tools = await server.mcp.list_tools()
        by_name = {tool.name: tool for tool in tools}

        self.assertIn("routing_policy_info", by_name)
        self.assertIn("routing_decide", by_name)

        description = by_name["routing_decide"].description or ""
        self.assertIn("ambiguous", description.lower())
        self.assertIn("ChatGPT-Actuator", description)
        self.assertIn("ChatGPT-Orchestrator", description)

        schema = by_name["routing_decide"].input_schema
        properties = schema.get("properties", {})
        for name in (
            "goal",
            "origin",
            "requires_execution",
            "local_machine_action",
            "short_deterministic_workflow",
            "complex_multi_step",
            "independent_workers",
            "parallel_reasoning",
            "review_rework",
            "restart_recovery",
        ):
            self.assertIn(name, properties)


if __name__ == "__main__":
    unittest.main()
