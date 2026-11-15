import ast
from pathlib import Path
import unittest

from chatgpt_actuator.tool_policy import required_scopes
from chatgpt_actuator.batch import PARALLEL_READ_ONLY_TOOLS


ROOT = Path(__file__).resolve().parents[1]


class AltiumSch3RegistrationTests(unittest.TestCase):
    def test_tool_wrappers_are_registered(self):
        module = ast.parse((ROOT / "src/chatgpt_actuator/server.py").read_text(encoding="utf-8"))
        functions = {
            node.name: node for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        expected = {
            "eda_altium_sch_check_pin_contracts",
            "eda_altium_sch_diff_connectivity",
            "eda_altium_sch_lint",
            "eda_altium_sch_erc",
            "eda_altium_sch_edit_commit",
            "eda_altium_sch_edit_restore",
        }
        self.assertTrue(expected <= functions.keys())
        for name in expected:
            self.assertTrue(any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
                and decorator.func.attr == "tool"
                for decorator in functions[name].decorator_list
            ), name)

    def test_new_tools_have_least_privilege_scopes(self):
        self.assertEqual(required_scopes("eda_altium_sch_check_pin_contracts"), ("mcp:read",))
        self.assertEqual(required_scopes("eda_altium_sch_diff_connectivity"), ("mcp:read",))
        self.assertEqual(required_scopes("eda_altium_sch_lint"), ("mcp:read",))
        self.assertEqual(required_scopes("eda_altium_sch_erc"), ("mcp:read", "mcp:execute"))
        full_edit = ("mcp:read", "mcp:write", "mcp:execute")
        self.assertEqual(required_scopes("eda_altium_sch_edit_commit"), full_edit)
        self.assertEqual(required_scopes("eda_altium_sch_edit_restore"), full_edit)

    def test_pure_snapshot_checks_are_parallel_read_only(self):
        self.assertTrue({
            "eda_altium_sch_check_pin_contracts",
            "eda_altium_sch_diff_connectivity",
            "eda_altium_sch_lint",
        } <= PARALLEL_READ_ONLY_TOOLS)

    def test_gateway_policy_matches_backend_policy(self):
        backend = ROOT / "src/chatgpt_actuator/tool_policy.py"
        gateway = ROOT / "integrations/tailscale_gateway/actuator_tool_policy.py"
        self.assertEqual(gateway.read_bytes(), backend.read_bytes())


if __name__ == "__main__":
    unittest.main()
