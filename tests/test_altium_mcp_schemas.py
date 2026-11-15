"""Exercise actual MCP schema generation without constructing the live runtime."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations


class AltiumMCPSchemaTests(unittest.TestCase):
    def test_public_schemas_register_and_forward_optional_contracts(self):
        source = Path(__file__).resolve().parents[1] / "src/chatgpt_actuator/server.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {"eda_altium_sch_edit_preview", "eda_altium_sch_edit_apply", "eda_altium_sch_check_pin_contracts",
                 "eda_altium_sch_diff_connectivity", "eda_altium_sch_lint", "eda_altium_sch_erc",
                 "eda_altium_sch_edit_commit", "eda_altium_sch_edit_restore"}
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        calls = []
        class EDA:
            def __getattr__(self, name):
                def invoke(*args, **kwargs):
                    calls.append((name, args, kwargs))
                    return {"status": "test_forwarded"}
                return invoke
        server = MCPServer("SCH3 schema self-check")
        namespace = {"mcp": server, "eda": EDA(), "READ_ONLY": ToolAnnotations(readOnlyHint=True),
                     "WRITE_ADDITIVE": ToolAnnotations(), "WRITE_DESTRUCTIVE": ToolAnnotations(), "WRITE_STATEFUL": ToolAnnotations()}
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, str(source), "exec"), namespace)
        async def check():
            tools = {tool.name: tool for tool in await server.list_tools()}
            self.assertEqual(set(tools), names)
            preview = tools["eda_altium_sch_edit_preview"].input_schema
            self.assertIn("expected_connectivity", preview["properties"])
            self.assertIn("intent", preview["properties"])
            self.assertEqual(tools["eda_altium_sch_check_pin_contracts"].input_schema["properties"]["require_all_pins"]["default"], True)
            await server.call_tool("eda_altium_sch_edit_preview", {"snapshot_id": "a" * 32, "changes": [{"op": "add_wire", "vertices": [[1, 2], [3, 2]]}],
                                   "output_dir": "D:/fixture/copy", "expected_connectivity": [{"component": "R1", "pin": "1", "nc": True, "reason": "unused"}],
                                   "intent": {"decoupling": [], "pullups": []}})
        asyncio.run(check())
        self.assertEqual(calls[-1][0], "altium_sch_edit_preview")
        self.assertEqual(calls[-1][1][3][0]["reason"], "unused")


if __name__ == "__main__":
    unittest.main()
