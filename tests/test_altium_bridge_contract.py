import re
import unittest
from pathlib import Path


BRIDGE = (Path(__file__).parents[1] / "src/chatgpt_actuator/tools/altium_scripts/Bridge.pas").read_text(encoding="utf-8")


class AltiumBridgeContractTests(unittest.TestCase):
    def test_native_snapshot_proves_pin_coverage_and_records_erc(self):
        for token in (
            '"kind":"edit_pin"', '"kind":"net_pin"', '"kind":"unconnected_pin"',
            '"kind":"capture_summary"', '"complete":true',
            '"kind":"erc_violation"', 'DM_Validate', 'DM_Violations',
            'DM_ErrorLevels', 'DM_ViolationTypeDescription',
            'DM_ReportSuppressedErrorsInMessages', '"configuration_complete":false',
            '"project_type":', '"project_level":', 'ActuatorErrorSettingFailed',
            'CaptureErrorSettings(Project, ProjectPath)',
            'TStringList.Create', 'ELECTRICAL RULES CHECK', 'N > 2048',
            'Project.DM_ErrorLevels(N)',
            'procedure ReadWorkspace', 'DM_LogicalDocumentCount',
            'DM_FocusedDocument', 'workspace_summary',
            'Unsaved schematic edits',
        ):
            self.assertIn(token, BRIDGE)
        self.assertIn("ActuatorPhysicalPins.IndexOf(Identity) < 0", BRIDGE)
        self.assertIn("Conflicting compiled nets for pin", BRIDGE)
        self.assertIn('AD26 compatibility assumption', BRIDGE)
        self.assertNotIn('ActuatorErrorSettingCount = 75', BRIDGE)
        self.assertIn('"error_reporting_setting_count":', BRIDGE)

    def test_every_edit_dispatch_has_a_native_procedure_and_clean_sheet_guard(self):
        procedures = set(re.findall(r"(?im)^procedure\s+(\w+)\s*(?:\(|;)", BRIDGE))
        expected = {
            "DeleteObject", "MoveComponent", "SetComponentParameter", "AddComponent",
            "BeginWire", "WireVertex", "EndWire", "AddLabel", "AddJunction",
            "AddPower", "AddNoERC", "CloseCleanSheet",
        }
        self.assertTrue(expected <= procedures, expected - procedures)
        self.assertIn("UID must match exactly one supported schematic object", BRIDGE)
        self.assertIn("Doc.GetModified <> 0", BRIDGE)
        self.assertIn("SCHM_PrimitiveRegistration", BRIDGE)
        self.assertIn("SCHM_BeginModify", BRIDGE)
        self.assertIn("SCHM_EndModify", BRIDGE)
        delete_body = BRIDGE.split("procedure DeleteObject(", 1)[1].split("\nend;", 1)[0]
        self.assertIn("UnRegisterSchObjectFromContainer(Target)", delete_body)
        self.assertIn("RemoveSchObject(Target)", delete_body)

    def test_component_parameter_can_only_create_when_expected_value_is_empty(self):
        body = BRIDGE.split("procedure SetComponentParameter(", 1)[1].split("\nend;", 1)[0]
        self.assertIn("CreateParameter := ParamMatches = 0", body)
        self.assertIn("CreateParameter and (ExpectedValue <> '')", body)
        self.assertIn("Target.AddSchObject(Selected)", body)
        self.assertIn("SCHM_PrimitiveRegistration, Selected.I_ObjectAddress", body)
        self.assertIn("Component parameter name is invalid or reserved", body)
        self.assertNotIn("Target.Comment", body)

    def test_dynamic_erc_capture_is_bounded_and_workspace_inventory_is_read_only(self):
        capture = BRIDGE.split("procedure CaptureErrorSettings(", 1)[1].split("\nprocedure ReadWorkspace", 1)[0]
        for token in (
            "ELECTRICAL RULES CHECK", "if not InSection then Continue",
            "N > 2048", "Seen.IndexOf(Key) >= 0", "Level < 0",
            "N > Ord(eError_DifferentialPairUnconnectedNet)",
            "Project.DM_ErrorLevels(N)", "ActuatorErrorSettingFailed := True",
        ):
            self.assertIn(token, capture)
        self.assertNotIn("DM_Compile", BRIDGE.split("procedure ReadWorkspace", 1)[1].split("procedure VerifyProject", 1)[0])
        self.assertIn('"documents":\' + IntToStr(DocumentCount)', BRIDGE)
        self.assertLess(BRIDGE.index("function BoolJSON("), BRIDGE.index("procedure ReadWorkspace;"))

    def test_compiled_pin_join_inventory_precedes_compile_and_snapshot_emits_once(self):
        verify = BRIDGE.split("procedure VerifyProjectCore(", 1)[1].split("\nprocedure VerifyProject", 1)[0]
        inspect = BRIDGE.split("procedure InspectExisting(", 1)[1].split("\nprocedure SetComponentValue", 1)[0]
        self.assertLess(verify.index("CollectPhysicalPins(SheetPath)"), verify.index("Project.DM_Compile"))
        self.assertIn("ActuatorPhysicalPins.Add(Identity)", BRIDGE.split("procedure CollectPhysicalPins(", 1)[1].split("\nfunction FindOpenProject", 1)[0])
        self.assertNotIn("ActuatorPhysicalPins.Add(Name)", BRIDGE.split("procedure InspectEditableSheet(", 1)[1].split("\nprocedure InspectExisting", 1)[0])
        self.assertLess(inspect.index("VerifyProjectCore(ProjectPath, SheetPath, False)"), inspect.index("InspectEditableSheet(SheetPath)"))
        self.assertEqual(BRIDGE.count('"kind":"capture_summary"'), 1)

    def test_compile_failure_emits_diagnostics_but_never_authoritative_connectivity(self):
        core = BRIDGE.split("procedure VerifyProjectCore(", 1)[1].split("\nprocedure VerifyProject(", 1)[0]
        strict = BRIDGE.split("procedure VerifyProject(", 1)[1].split("\nprocedure PrepareProject", 1)[0]
        self.assertIn("VerifyProjectCore(ProjectPath, SheetPath, True)", strict)
        self.assertIn('"kind":"compile_summary"', core)
        self.assertIn("ActuatorCompileSuccess := Project.DM_Compile <> 0", core)
        self.assertIn("if ActuatorCompileSuccess then ActuatorCompileSuccess := Project.DM_Compile <> 0", core)
        self.assertNotIn("ActuatorCompileSuccess := Project.DM_Compile;", core)
        self.assertIn('"compile_success":', BRIDGE)
        failure = core.index("if not ActuatorCompileSuccess then")
        self.assertLess(failure, core.index("Flat := Project.DM_DocumentFlattened"))
        failed_branch = core[failure:core.index("    Exit;", failure) + len("    Exit;")]
        self.assertIn("EmitIncompleteProjectERC", failed_branch)
        self.assertNotIn("DM_Validate", failed_branch)
        self.assertNotIn("CaptureErrorSettings", failed_branch)
        self.assertLess(failed_branch.index("EmitIncompleteProjectERC"), failed_branch.index("if Strict then Raise('Project compile failed')"))
        self.assertNotIn('"kind":"compiled"', failed_branch)
        self.assertNotIn('"kind":"net_pin"', failed_branch)
        self.assertNotIn('"kind":"unconnected_pin"', failed_branch)
        self.assertIn('"connectivity_complete":', BRIDGE)
        incomplete_erc = BRIDGE.split("procedure EmitIncompleteProjectERC;", 1)[1].split("\nprocedure VerifyProjectCore", 1)[0]
        self.assertIn('"complete":false', incomplete_erc)
        self.assertIn('"violations_available":false', incomplete_erc)
        self.assertIn('"violation_count":null', incomplete_erc)
        self.assertNotIn("DM_Violations", incomplete_erc)
        self.assertGreater(core.index("Project.DM_Validate"), failure)
        self.assertIn('"component_uid":', BRIDGE)

    def test_loaded_project_resolution_is_shared_and_cleanup_reuses_enumerated_handle(self):
        resolver = BRIDGE.split("function FindOpenProject(", 1)[1].split("\nprocedure VerifyProject", 1)[0]
        opening = BRIDGE.split("function OpenTargetProject(", 1)[1].split("\nprocedure VerifyProject", 1)[0]
        verify = BRIDGE.split("procedure VerifyProjectCore(", 1)[1].split("\nprocedure VerifyProject(", 1)[0]
        prepare = BRIDGE.split("procedure PrepareProject(", 1)[1].split("\nprocedure CloseOwnedProject", 1)[0]
        cleanup = BRIDGE.split("procedure CloseOwnedProject(", 1)[1].split("\nprocedure PrepareExisting", 1)[0]
        self.assertIn("GetWorkspace.DM_Projects(I)", resolver)
        self.assertIn("Matches > 1", resolver)
        self.assertLess(opening.index("FindOpenProject(ProjectPath)"), opening.index("DM_OpenProject(ProjectPath, True)"))
        self.assertIn("FindOpenProject(ProjectPath)", opening[opening.index("DM_OpenProject(ProjectPath, True)"):])
        self.assertIn("OpenTargetProject(ProjectPath)", verify)
        self.assertIn("OpenTargetProject(ProjectPath)", prepare)
        self.assertIn("Project := FindOpenProject(ProjectPath)", cleanup)
        self.assertNotIn("DM_OpenProject", cleanup)
        self.assertIn("ProjectDoc := Client.GetDocumentByPath(ProjectPath)", cleanup)
        self.assertIn("Client.CloseDocument(ProjectDoc)", cleanup)
        self.assertIn("Flat.DM_FullPath) = UpperCase(DocumentPath)", cleanup)
        self.assertIn("MemberCount <> 1", cleanup)
        self.assertIn("Client.GetDocumentByPath(Flat.DM_FullPath)", cleanup)
        self.assertIn("Owned project has an unsaved member document", cleanup)
        self.assertNotIn("WorkspaceManager:CloseObject", cleanup)
        self.assertIn("Owned logical document did not close", cleanup)
        self.assertLess(cleanup.index("ProjectDoc.GetModified <> 0"), cleanup.index("Client.CloseDocument(ProjectDoc)"))
        self.assertLess(cleanup.index("MemberDoc.GetModified <> 0"), cleanup.index("Client.CloseDocument(ProjectDoc)"))

    def test_owned_sheet_normalization_verifies_and_guards_before_persisting(self):
        body = BRIDGE.split("procedure NormalizeOwnedSheet(", 1)[1].split("\nprocedure CloseOwnedProject", 1)[0]
        self.assertIn("VerifyProject(ProjectPath, SheetPath);", body)
        self.assertIn("Project := OpenTargetProject(ProjectPath)", body)
        self.assertIn("Client.GetDocumentByPath(SheetPath)", body)
        self.assertIn("UpperCase(Doc.FileName) <> UpperCase(SheetPath)", body)
        self.assertIn("UpperCase(Flat.DM_FullPath) = UpperCase(SheetPath)", body)
        self.assertIn("(Matches <> 1) or (Documents <> 1)", body)
        self.assertIn("Doc.DoFileSave('Advanced Schematic binary')", body)
        self.assertIn("WasModified := Doc.GetModified <> 0", body)
        self.assertIn("if Doc.GetModified <> 0 then Raise('Owned schematic remains modified after normalization')", body)
        self.assertIn("ProjectDoc := Client.GetDocumentByPath(ProjectPath)", body)
        self.assertIn("UpperCase(ProjectDoc.FileName) <> UpperCase(ProjectPath)", body)
        self.assertIn("ProjectDoc.DoFileSave('')", body)
        self.assertIn("ProjectWasModified := ProjectDoc.GetModified <> 0", body)
        self.assertIn("if ProjectDoc.GetModified <> 0 then Raise('Owned project remains modified after normalization')", body)
        self.assertIn('"kind":"normalized_owned_project"', body)
        self.assertLess(body.index("VerifyProject(ProjectPath, SheetPath);"), body.index("Doc.DoFileSave"))
        self.assertLess(body.index("(Matches <> 1) or (Documents <> 1)"), body.index("Doc.DoFileSave"))
        self.assertLess(body.index("UpperCase(Doc.FileName) <> UpperCase(SheetPath)"), body.index("Doc.DoFileSave"))
        self.assertLess(body.index("(Matches <> 1) or (Documents <> 1)"), body.index("ProjectDoc.DoFileSave('')"))
        self.assertLess(body.index("UpperCase(ProjectDoc.FileName) <> UpperCase(ProjectPath)"), body.index("ProjectDoc.DoFileSave('')"))
        self.assertLess(body.index("ProjectDoc.DoFileSave('')"), body.index('"kind":"normalized_owned_sheet"'))
        self.assertLess(body.index('"kind":"normalized_owned_sheet"'), body.index('"kind":"normalized_owned_project"'))
        self.assertNotIn("CloseOwnedProject", body)

    def test_wire_lifecycle_starts_empty_and_appends_real_vertices(self):
        run = BRIDGE.split("procedure ActuatorRun;", 1)[1]
        self.assertLess(run.index("ActuatorWire := Nil;"), run.index("@@COMMAND@@"))
        begin = BRIDGE.split("procedure BeginWire(", 1)[1].split("\nprocedure WireVertex", 1)[0]
        vertex = BRIDGE.split("procedure WireVertex(", 1)[1].split("\nprocedure EndWire", 1)[0]
        end = BRIDGE.split("procedure EndWire;", 1)[1].split("\nprocedure AddLabel", 1)[0]
        self.assertIn("if ActuatorWire <> Nil then Raise('A wire is already being created')", begin)
        self.assertNotIn("InsertVertex", begin)
        self.assertIn("Index := ActuatorWire.GetState_VerticesCount + 1", vertex)
        self.assertIn("ActuatorWire.InsertVertex := Index", vertex)
        self.assertIn("ActuatorWire.SetState_Vertex(Index, Point", vertex)
        self.assertNotIn("SetState_Vertex(ActuatorWire.InsertVertex", vertex)
        self.assertIn("if ActuatorWire = Nil then Raise('BeginWire must run before WireVertex')", vertex)
        self.assertIn("if ActuatorWire.GetState_VerticesCount < 2 then Raise('Wire needs at least two vertices')", end)
        self.assertLess(end.index("RegisterEditObject(ActuatorWire)"), end.index("ActuatorWire := Nil"))

    def test_project_document_is_clean_at_verification_start_and_erc_receipt(self):
        core = BRIDGE.split("procedure VerifyProjectCore(", 1)[1].split("\nprocedure VerifyProject(", 1)[0]
        initial = core.index("ProjectDoc := Client.GetDocumentByPath(ProjectPath)")
        compile_call = core.index("Project.DM_Compile")
        initial_check = core.index("ProjectDoc.GetModified <> 0", initial)
        final = core.index("ProjectDoc := Client.GetDocumentByPath(ProjectPath)", initial + 1)
        receipt = core.index("EmitProjectERC(Project)")
        self.assertLess(initial, initial_check)
        self.assertLess(initial_check, compile_call)
        self.assertLess(core.index("ProjectDoc.FileName", initial), compile_call)
        self.assertLess(final, receipt)
        self.assertLess(core.index("ProjectDoc.FileName", final), receipt)
        self.assertLess(core.index("ProjectDoc.GetModified <> 0", final), receipt)
        self.assertIn("ProjectDoc = Nil", core[initial:compile_call])
        self.assertIn("ProjectDoc = Nil", core[final:receipt])

    def test_component_move_preserves_comment_rigid_transform(self):
        move = BRIDGE.split("procedure MoveComponent(", 1)[1].split("\nprocedure SetComponentParameter", 1)[0]
        self.assertIn("DX := OldCommentX - Target.Location.X", move)
        self.assertIn("DY := OldCommentY - Target.Location.Y", move)
        self.assertIn("OldCommentAutoposition := Target.Comment.Autoposition", move)
        self.assertIn("Delta := (Rotation div 90 - Target.Orientation + 4) mod 4", move)
        self.assertIn("if Delta = 1 then begin NewDX := -DY; NewDY := DX; end", move)
        self.assertIn("else if Delta = 2 then begin NewDX := -DX; NewDY := -DY; end", move)
        self.assertIn("else if Delta = 3 then begin NewDX := DY; NewDY := -DX; end", move)
        self.assertLess(move.index("Target.MoveToXY"), move.index("Target.Comment.Location := Point"))
        self.assertIn("Target.Comment.Orientation := (OldCommentRotation + Delta) mod 4", move)
        self.assertLess(move.index("Target.Comment.Autoposition := False"), move.index("Target.MoveToXY"))
        self.assertIn("Target.Comment.Autoposition := OldCommentAutoposition", move)
        self.assertLess(move.index("Target.Comment.Autoposition := False"), move.index("finally\n      Target.Comment.Autoposition := OldCommentAutoposition"))

    def test_existing_project_is_reloaded_with_exact_configuration_proof(self):
        prepare = BRIDGE.split("procedure PrepareExisting(", 1)[1].split("\nfunction ComponentParametersJSON", 1)[0]
        cleanup = BRIDGE.split("procedure CloseOwnedProject(", 1)[1].split("\nprocedure PrepareExisting", 1)[0]
        close = prepare.index("CloseOwnedProject(ProjectPath, SheetPath)")
        reopen = prepare.index("PrepareProject(ProjectPath)")
        reload_row = prepare.index('"kind":"project_configuration_reload"')
        self.assertLess(prepare.index("ProjectDoc.GetModified <> 0"), close)
        self.assertLess(prepare.index("Doc.GetModified <> 0"), close)
        self.assertLess(prepare.index("Project.DM_ProjectVariantCount > 0"), close)
        self.assertLess(prepare.index("(Matches <> 1) or (Documents <> 1)"), close)
        self.assertIn("Owned project has an unsaved member document", cleanup)
        self.assertIn("MemberCount <> 1", cleanup)
        self.assertLess(close, prepare.index("FindOpenProject(ProjectPath) <> Nil", close))
        self.assertLess(prepare.index("FindOpenProject(ProjectPath) <> Nil", close), reopen)
        self.assertLess(reopen, reload_row)
        self.assertIn("PreviouslyOpen := Project <> Nil", prepare)
        self.assertIn("ProjectDoc.FileName", prepare)
        self.assertIn("ProjectDoc.GetModified <> 0", prepare)
        self.assertIn(',"complete":true,"previously_open":', prepare)
        self.assertIn("BoolJSON(PreviouslyOpen)", prepare)

    def test_successful_compile_rejects_project_variants_and_mapping_is_unverified(self):
        core = BRIDGE.split("procedure VerifyProjectCore(", 1)[1].split("\nprocedure VerifyProject(", 1)[0]
        compile_success = core.index("if ActuatorCompileSuccess then ActuatorCompileSuccess := Project.DM_Compile <> 0")
        variant_guard = core.index("if ActuatorCompileSuccess and (Project.DM_ProjectVariantCount > 0)")
        self.assertGreater(variant_guard, compile_success)
        setting = BRIDGE.split("procedure EmitErrorSetting(", 1)[1].split("\nprocedure CaptureErrorSettings", 1)[0]
        self.assertIn('"mapping_verified":false', setting)


if __name__ == "__main__":
    unittest.main()
