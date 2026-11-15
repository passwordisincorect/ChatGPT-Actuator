import unittest
from types import SimpleNamespace

from chatgpt_actuator.automation_errors import PolicyDenied
from chatgpt_actuator.tool_catalog import catalog
from chatgpt_actuator.tool_policy import Principal, authorize, required_scopes, scope_for


class _Server:
    def __init__(self, names):
        self._tool_manager = SimpleNamespace(list_tools=lambda: [
            SimpleNamespace(name=name, parameters={}, description="") for name in names
        ])


class AltiumScopeTests(unittest.TestCase):
    def test_scope_requirements_keep_primary_scope_and_support_namespace(self):
        self.assertEqual(scope_for("eda_altium_sch_plan_create"), "mcp:write")
        self.assertEqual(scope_for("actuator__eda_altium_sch_plan_create"), "mcp:write")
        self.assertEqual(required_scopes("actuator__eda_altium_sch_plan_create"),
                         ("mcp:read", "mcp:write", "mcp:execute"))
        self.assertEqual(required_scopes("actuator__eda_altium_sch_validate"),
                         ("mcp:read", "mcp:execute"))

    def test_write_only_and_execute_only_cannot_plan(self):
        name = "eda_altium_sch_plan_create"
        for granted in ({"mcp:write"}, {"mcp:execute"}):
            with self.subTest(granted=granted), self.assertRaises(PolicyDenied) as denied:
                authorize(name, Principal(scopes=frozenset(granted)))
            self.assertEqual(denied.exception.reason, "missing_scope")
            self.assertEqual(denied.exception.required_scope, "mcp:read")

    def test_all_required_scopes_authorize_altium_operations(self):
        all_scopes = frozenset({"mcp:read", "mcp:write", "mcp:execute"})
        for name in (
            "eda_altium_library_inspect", "eda_altium_sch_plan_create",
            "eda_altium_sch_validate", "eda_altium_sch_materialize",
            "eda_altium_sch_verify", "eda_altium_operation_status",
        ):
            with self.subTest(name=name):
                authorize(name, Principal(scopes=all_scopes))

    def test_catalog_reports_supplemental_missing_scope_and_requirements(self):
        name = "eda_altium_sch_materialize"
        principal = Principal(scopes=frozenset({"mcp:execute", "mcp:write"}))
        result = catalog(SimpleNamespace(raw_config=lambda: {}), _Server([name]), principal)
        entry = result["tools"][0]
        self.assertEqual(entry["state"], "missing_scope")
        self.assertEqual(entry["required_scope"], "mcp:execute")
        self.assertEqual(entry["required_scopes"], ["mcp:read", "mcp:write", "mcp:execute"])
        self.assertEqual(result["authorized_count"], 0)

    def test_catalog_authorizes_with_all_scopes_and_keeps_non_altium_contract(self):
        names = ["eda_altium_sch_validate", "filesystem_read_text"]
        principal = Principal(scopes=frozenset({"mcp:read", "mcp:execute"}))
        result = catalog(SimpleNamespace(raw_config=lambda: {}), _Server(names), principal)
        entries = {entry["name"]: entry for entry in result["tools"]}
        self.assertEqual(result["authorized_count"], 2)
        self.assertEqual(entries["eda_altium_sch_validate"]["required_scopes"],
                         ["mcp:read", "mcp:execute"])
        self.assertEqual(entries["filesystem_read_text"]["required_scopes"], ["mcp:read"])
        self.assertEqual(entries["filesystem_read_text"]["required_scope"], "mcp:read")


if __name__ == "__main__":
    unittest.main()
