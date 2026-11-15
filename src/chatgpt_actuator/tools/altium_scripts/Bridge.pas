var ActuatorOutput, ActuatorDoc, ActuatorSch, ActuatorWire, ActuatorPhysicalPins, ActuatorConnectedPins;
  ActuatorComponentCount, ActuatorPinCount, ActuatorLabelCount, ActuatorWireCount;
  ActuatorJunctionCount, ActuatorPowerCount, ActuatorNoERCCount, ActuatorErrorSettingCount;
  ActuatorErrorSettingFailed, ActuatorUnconnectedPinCount, ActuatorCompileSuccess;

function J(S : String) : String;
var I, C;
begin
  Result := '"';
  for I := 1 to Length(S) do
  begin
    C := Ord(S[I]);
    if C = 34 then Result := Result + '\"'
    else if C = 92 then Result := Result + '\\'
    else if (C < 32) or (C > 126) then Result := Result + '\u' + IntToHex(C, 4)
    else Result := Result + S[I];
  end;
  Result := Result + '"';
end;

procedure Emit(S : String);
begin
  ActuatorOutput.Add(S);
end;

function PinElectricalName(Pin) : String;
begin
  if Pin.Electrical = eElectricInput then Result := 'input'
  else if Pin.Electrical = eElectricIO then Result := 'io'
  else if Pin.Electrical = eElectricOutput then Result := 'output'
  else if Pin.Electrical = eElectricOpenCollector then Result := 'open_collector'
  else if Pin.Electrical = eElectricPassive then Result := 'passive'
  else if Pin.Electrical = eElectricHiZ then Result := 'high_impedance'
  else if Pin.Electrical = eElectricOpenEmitter then Result := 'open_emitter'
  else if Pin.Electrical = eElectricPower then Result := 'power'
  else Result := 'unknown:' + IntToStr(Ord(Pin.Electrical));
end;

function BoundsJSON(Obj) : String;
begin
  Result := '[' + IntToStr(Round(CoordToMils(Obj.BoundingRectangle.Left))) + ',' +
    IntToStr(Round(CoordToMils(Obj.BoundingRectangle.Bottom))) + ',' +
    IntToStr(Round(CoordToMils(Obj.BoundingRectangle.Right))) + ',' +
    IntToStr(Round(CoordToMils(Obj.BoundingRectangle.Top))) + ']';
end;

procedure DumpPins(Component, Ref);
var It, Pin;
begin
  It := Component.SchIterator_Create;
  try
    It.AddFilter_ObjectSet(MkSet(ePin));
    Pin := It.FirstSchObject;
    while Pin <> Nil do
    begin
      Emit('{"kind":"pin","symbol":' + J(Ref) +
        ',"number":' + J(Pin.Designator) + ',"name":' + J(Pin.Name) +
        ',"electrical_type":' + J(PinElectricalName(Pin)) +
        ',"part":' + IntToStr(Pin.OwnerPartId) +
        ',"x":' + IntToStr(Round(CoordToMils(Pin.Location.X))) +
        ',"y":' + IntToStr(Round(CoordToMils(Pin.Location.Y))) +
        ',"length":' + IntToStr(Round(CoordToMils(Pin.PinLength))) +
        ',"orientation":' + IntToStr(Pin.Orientation) + '}');
      Pin := It.NextSchObject;
    end;
  finally
    Component.SchIterator_Destroy(It);
  end;
end;

procedure InspectLibrary(LibraryPath : String);
var Doc, Lib, It, Comp;
begin
  Client.StartServer('SCH');
  Doc := Client.OpenDocument('SCHLIB', LibraryPath);
  if Doc = Nil then Raise('Cannot open schematic library');
  if Doc.GetModified <> 0 then Raise('Unsaved library edits must be saved before inspection');
  Lib := SchServer.GetSchDocumentByPath(LibraryPath);
  if Lib = Nil then Raise('Schematic library API unavailable');
  It := Lib.SchLibIterator_Create;
  try
    It.AddFilter_ObjectSet(MkSet(eSchComponent));
    Comp := It.FirstSchObject;
    while Comp <> Nil do
    begin
      Emit('{"kind":"symbol","name":' + J(Comp.LibReference) +
        ',"parts":' + IntToStr(Comp.PartCount) + '}');
      DumpPins(Comp, Comp.LibReference);
      Comp := It.NextSchObject;
    end;
  finally
    Lib.SchIterator_Destroy(It);
  end;
end;

procedure CreateSheet(SheetPath : String; Width, Height : Integer);
var Server;
begin
  if FileExists(SheetPath) then Raise('Destination already exists');
  Client.StartServer('SCH');
  Server := Client.ServerModuleByName['SCH'];
  ActuatorDoc := Server.CreateDocument('SCH', SheetPath);
  if ActuatorDoc = Nil then Raise('Cannot create SCH document');
  Client.ShowDocument(ActuatorDoc);
  ActuatorSch := SchServer.GetSchDocumentByPath(SheetPath);
  if ActuatorSch = Nil then Raise('SCH document API unavailable');
  ActuatorSch.UseCustomSheet := True;
  ActuatorSch.CustomX := MilsToCoord(Width);
  ActuatorSch.CustomY := MilsToCoord(Height);
  ActuatorSch.SnapGridSize := MilsToCoord(100);
  // The stock title block has a fixed physical size and can cover small
  // generated sheets. Use the clean custom sheet rather than overlap circuitry.
  ActuatorSch.TitleBlockOn := False;
end;

procedure PlaceComponent(LibraryPath, SymbolName, Ref, Value : String; X, Y, Rotation, Part : Integer);
var Comp;
begin
  Comp := SchServer.LoadComponentFromLibrary(SymbolName, LibraryPath);
  if Comp = Nil then Raise('Cannot load symbol: ' + SymbolName);
  Comp.CurrentPartID := Part;
  Comp.Orientation := Rotation div 90;
  Comp.MoveToXY(MilsToCoord(X), MilsToCoord(Y));
  Comp.Designator.Text := Ref;
  Comp.Comment.Text := Value;
  ActuatorSch.RegisterSchObjectInContainer(Comp);
  Emit('{"kind":"placed","reference":' + J(Ref) + ',"symbol":' + J(SymbolName) + '}');
  DumpPins(Comp, Ref);
end;

procedure OpenSheet(SheetPath : String);
begin
  ActuatorDoc := Client.OpenDocument('SCH', SheetPath);
  if ActuatorDoc = Nil then Raise('Cannot open schematic');
  if ActuatorDoc.GetModified <> 0 then Raise('Unsaved schematic edits');
  ActuatorSch := SchServer.GetSchDocumentByPath(SheetPath);
  if ActuatorSch = Nil then Raise('SCH API unavailable');
end;

procedure SaveSheet;
begin
  ActuatorSch.GraphicallyInvalidate;
  if not ActuatorDoc.DoFileSave('Advanced Schematic binary') then Raise('Cannot save SCH document');
  Emit('{"kind":"saved","path":' + J(ActuatorDoc.FileName) + '}');
end;

procedure WireLabel(X1, Y1, X2, Y2, Direction : Integer; NetName : String);
var Wire, LabelObj;
begin
  Wire := SchServer.SchObjectFactory(eWire, eCreate_Default);
  Wire.Location := Point(MilsToCoord(X1), MilsToCoord(Y1));
  Wire.InsertVertex := 1;
  Wire.SetState_Vertex(1, Point(MilsToCoord(X1), MilsToCoord(Y1)));
  Wire.InsertVertex := 2;
  Wire.SetState_Vertex(2, Point(MilsToCoord(X2), MilsToCoord(Y2)));
  ActuatorSch.RegisterSchObjectInContainer(Wire);
  LabelObj := SchServer.SchObjectFactory(eNetLabel, eCreate_Default);
  LabelObj.Location := Point(MilsToCoord(X2), MilsToCoord(Y2));
  LabelObj.Text := NetName;
  LabelObj.FontID := SchServer.FontManager.GetFontID(8, 0, False, False, False, False, 'Arial');
  LabelObj.Orientation := eRotate0;
  if (Direction = 1) or (Direction = 3) then LabelObj.Orientation := eRotate90;
  LabelObj.Justification := eJustify_BottomLeft;
  if (Direction = 2) or (Direction = 3) then LabelObj.Justification := eJustify_BottomRight;
  ActuatorSch.RegisterSchObjectInContainer(LabelObj);
end;

procedure InspectSheet(SheetPath : String);
var Sheet, It, Obj;
begin
  Sheet := SchServer.GetSchDocumentByPath(SheetPath);
  if Sheet = Nil then Raise('SCH API unavailable for inspection');
  Emit('{"kind":"observed_sheet","width":' + IntToStr(Round(CoordToMils(Sheet.CustomX))) +
    ',"height":' + IntToStr(Round(CoordToMils(Sheet.CustomY))) + '}');
  It := Sheet.SchIterator_Create;
  try
    It.AddFilter_ObjectSet(MkSet(eSchComponent, eWire, eNetLabel));
    It.SetState_IterationDepth(eIterateFirstLevel);
    Obj := It.FirstSchObject;
    while Obj <> Nil do
    begin
      if Obj.ObjectId = eSchComponent then
        Emit('{"kind":"observed_component","reference":' + J(Obj.Designator.Text) +
          ',"symbol":' + J(Obj.LibReference) + ',"part":' + IntToStr(Obj.CurrentPartID) + ',"value":' + J(Obj.Comment.Text) + ',"bounds":' + BoundsJSON(Obj) + '}');
      if Obj.ObjectId = eNetLabel then
        Emit('{"kind":"observed_label","name":' + J(Obj.Text) +
          ',"x":' + IntToStr(Round(CoordToMils(Obj.Location.X))) + ',"y":' + IntToStr(Round(CoordToMils(Obj.Location.Y))) + ',"bounds":' + BoundsJSON(Obj) + '}');
      Obj := It.NextSchObject;
    end;
  finally
    Sheet.SchIterator_Destroy(It);
  end;
end;

function ErrorSeverityName(Level) : String;
begin
  if Level = eErrorLevelNoReport then Result := 'no_report'
  else if Level = eErrorLevelWarning then Result := 'warning'
  else if Level = eErrorLevelError then Result := 'error'
  else if Level = eErrorLevelFatal then Result := 'fatal'
  else Result := 'unknown:' + IntToStr(Ord(Level));
end;

procedure ConnectPin(Ref : String; Part : Integer; PinNumber : String;
  X1, Y1, X2, Y2, Direction : Integer; NetName : String);
var It, Pins, Comp, Pin, Found, PX, PY, DX, DY;
begin
  Found := False;
  It := ActuatorSch.SchIterator_Create;
  try
    It.AddFilter_ObjectSet(MkSet(eSchComponent));
    It.SetState_IterationDepth(eIterateFirstLevel);
    Comp := It.FirstSchObject;
    while Comp <> Nil do
    begin
      if (Comp.Designator.Text = Ref) and (Comp.CurrentPartID = Part) then
      begin
        Pins := Comp.SchIterator_Create;
        try
          Pins.AddFilter_ObjectSet(MkSet(ePin));
          Pin := Pins.FirstSchObject;
          while Pin <> Nil do
          begin
            if (Pin.Designator = PinNumber) and ((Pin.OwnerPartId = Part) or (Pin.OwnerPartId = 0)) then
            begin
              DX := 0; DY := 0;
              if Pin.Orientation = 0 then DX := 1;
              if Pin.Orientation = 1 then DY := 1;
              if Pin.Orientation = 2 then DX := -1;
              if Pin.Orientation = 3 then DY := -1;
              PX := CoordToMils(Pin.Location.X) + DX * CoordToMils(Pin.PinLength);
              PY := CoordToMils(Pin.Location.Y) + DY * CoordToMils(Pin.PinLength);
              if (Abs(PX - X1) > 1) or (Abs(PY - Y1) > 1) then Raise('Pin geometry differs from validated library');
              Found := True;
            end;
            Pin := Pins.NextSchObject;
          end;
        finally
          Comp.SchIterator_Destroy(Pins);
        end;
      end;
      Comp := It.NextSchObject;
    end;
  finally
    ActuatorSch.SchIterator_Destroy(It);
  end;
  if not Found then Raise('Cannot locate pin ' + Ref + '.' + PinNumber);
  WireLabel(X1, Y1, X2, Y2, Direction, NetName);
end;

function IsDerivedVirtualBOM(Doc, ProjectPath) : Boolean;
begin
  Result := (UpperCase(Doc.DM_DocumentKind) = 'VIRTUALBOM') and
    (UpperCase(ExtractFileName(Doc.DM_FullPath)) = 'ACTIVEBOM.VIRTUALBOM') and
    (UpperCase(ExtractFilePath(Doc.DM_FullPath)) = UpperCase(ExtractFilePath(ProjectPath))) and
    (not FileExists(Doc.DM_FullPath));
end;

function BoolJSON(Value : Boolean) : String;
begin
  if Value <> 0 then Result := 'true' else Result := 'false';
end;

procedure EmitErrorSetting(ProjectType, Description : String; Kind, Group, Level : Integer);
begin
  Emit('{"kind":"erc_error_setting","code":' + J(ProjectType) +
    ',"project_type":' + J(ProjectType) + ',"project_level":' + IntToStr(Level) +
    ',"description":' + J(Description) + ',"kind_id":' + IntToStr(Kind) +
    ',"group_id":' + IntToStr(Group) + ',"severity":' + J(ErrorSeverityName(Level)) +
    ',"mapping_verified":false}');
  Inc(ActuatorErrorSettingCount);
end;

procedure CaptureErrorSettings(Project, ProjectPath);
var Lines, Seen, I, K, EqPos, N, Digit, Digits, Level, Line, Key, Value, InSection, Valid;
begin
  ActuatorErrorSettingCount := 0;
  ActuatorErrorSettingFailed := False;
  Lines := TStringList.Create;
  Seen := TStringList.Create;
  try
    try
      Lines.LoadFromFile(ProjectPath);
      InSection := False;
      for I := 0 to Lines.Count - 1 do
      begin
        Line := Trim(Lines.Strings[I]);
        if (Line = '') or (Line[1] = ';') or (Line[1] = '#') then Continue;
        if (Line[1] = '[') and (Line[Length(Line)] = ']') then
        begin
          InSection := UpperCase(Trim(Copy(Line, 2, Length(Line) - 2))) = 'ELECTRICAL RULES CHECK';
          Continue;
        end;
        if not InSection then Continue;
        EqPos := Pos('=', Line);
        if EqPos = 0 then Continue;
        Key := Trim(Copy(Line, 1, EqPos - 1));
        Value := Trim(Copy(Line, EqPos + 1, Length(Line) - EqPos));
        if UpperCase(Copy(Key, 1, 4)) <> 'TYPE' then Continue;
        N := 0; Digits := 0; Valid := True;
        for K := 5 to Length(Key) do
        begin
          Digit := Ord(Key[K]) - Ord('0');
          if (Digit < 0) or (Digit > 9) then Valid := False
          else
          begin
            Digits := Digits + 1;
            if N <= 2048 then N := N * 10 + Digit;
          end;
        end;
        if (not Valid) or (Digits = 0) or (N < 1) or (N > 2048) or
           (N > Ord(eError_DifferentialPairUnconnectedNet)) or
           (IntToStr(N) <> Copy(Key, 5, Length(Key) - 4)) then
        begin
          ActuatorErrorSettingFailed := True;
          Continue;
        end;
        Key := 'Type' + IntToStr(N);
        if Seen.IndexOf(Key) >= 0 then
        begin
          ActuatorErrorSettingFailed := True;
          Continue;
        end;
        Seen.Add(Key);
        Level := -1;
        if (Value = '0') or (Value = '1') or (Value = '2') or (Value = '3') then Level := StrToInt(Value);
        if Level < 0 then
        begin
          ActuatorErrorSettingFailed := True;
          Continue;
        end;
        try
          // AD26 compatibility assumption: saved TypeN maps to TErrorKind ordinal N.
          // Require the later native adversarial-settings acceptance test before claiming this mapping is verified.
          EmitErrorSetting(Key, GetWorkspace.DM_ViolationTypeDescription(N), N,
            Ord(GetWorkspace.DM_ViolationTypeGroup(N)), Ord(Project.DM_ErrorLevels(N)));
          if Ord(Project.DM_ErrorLevels(N)) <> Level then ActuatorErrorSettingFailed := True;
        except
          ActuatorErrorSettingFailed := True;
        end;
      end;
      if Seen.Count = 0 then ActuatorErrorSettingFailed := True;
    except
      ActuatorErrorSettingFailed := True;
    end;
  finally
    Seen.Free;
    Lines.Free;
  end;
end;

procedure ReadWorkspace;
var I, DocIndex, Project, Flat, Doc, Focused, ProjectCount, DocumentCount, Modified;
  ProjectPath, DocumentPath, ActivePath;
begin
  ProjectCount := GetWorkspace.DM_ProjectCount;
  DocumentCount := 0;
  for I := 0 to ProjectCount - 1 do
  begin
    Project := GetWorkspace.DM_Projects(I);
    ProjectPath := Project.DM_ProjectFullPath;
    Doc := Client.GetDocumentByPath(ProjectPath);
    if Doc = Nil then Modified := -1 else Modified := Doc.GetModified;
    if Modified < 0 then
      Emit('{"kind":"workspace_project","path":' + J(ProjectPath) + ',"modified":null}')
    else
      Emit('{"kind":"workspace_project","path":' + J(ProjectPath) + ',"modified":' + BoolJSON(Modified <> 0) + '}');
    for DocIndex := 0 to Project.DM_LogicalDocumentCount - 1 do
    begin
      Flat := Project.DM_LogicalDocuments(DocIndex);
      DocumentPath := Flat.DM_FullPath;
      Doc := Client.GetDocumentByPath(DocumentPath);
      if Doc = Nil then Modified := -1 else Modified := Doc.GetModified;
      if Modified < 0 then
        Emit('{"kind":"workspace_document","project_path":' + J(ProjectPath) + ',"path":' + J(DocumentPath) + ',"open":false,"modified":null}')
      else
        Emit('{"kind":"workspace_document","project_path":' + J(ProjectPath) + ',"path":' + J(DocumentPath) + ',"open":true,"modified":' + BoolJSON(Modified <> 0) + '}');
      DocumentCount := DocumentCount + 1;
    end;
  end;
  ActivePath := '';
  try
    Focused := GetWorkspace.DM_FocusedDocument;
    if Focused <> Nil then ActivePath := Focused.DM_FullPath;
  except
    ActivePath := '';
  end;
  if ActivePath = '' then Emit('{"kind":"workspace_active_document","available":false}')
  else Emit('{"kind":"workspace_active_document","available":true,"path":' + J(ActivePath) + '}');
  Emit('{"kind":"workspace_summary","projects":' + IntToStr(ProjectCount) +
    ',"documents":' + IntToStr(DocumentCount) + '}');
end;

procedure CollectPhysicalPins(SheetPath : String);
var Sheet, It, Obj, PinIt, Pin, Identity;
begin
  Sheet := SchServer.GetSchDocumentByPath(SheetPath);
  if Sheet = Nil then Raise('SCH API unavailable for physical pin inventory');
  ActuatorPhysicalPins.Clear;
  It := Sheet.SchIterator_Create;
  try
    It.SetState_IterationDepth(eIterateFirstLevel);
    It.AddFilter_ObjectSet(MkSet(eSchComponent));
    Obj := It.FirstSchObject;
    while Obj <> Nil do
    begin
      PinIt := Obj.SchIterator_Create;
      try
        PinIt.AddFilter_ObjectSet(MkSet(ePin));
        Pin := PinIt.FirstSchObject;
        while Pin <> Nil do
        begin
          if (Pin.OwnerPartId = 0) or (Pin.OwnerPartId = Obj.CurrentPartID) then
          begin
            Identity := UpperCase(Obj.Designator.Text) + '|' + Pin.Designator;
            if ActuatorPhysicalPins.IndexOf(Identity) < 0 then ActuatorPhysicalPins.Add(Identity);
          end;
          Pin := PinIt.NextSchObject;
        end;
      finally
        Obj.SchIterator_Destroy(PinIt);
      end;
      Obj := It.NextSchObject;
    end;
  finally
    Sheet.SchIterator_Destroy(It);
  end;
end;

function FindOpenProject(ProjectPath : String) : IProject;
var I, Candidate, Matches;
begin
  Result := Nil;
  Matches := 0;
  for I := 0 to GetWorkspace.DM_ProjectCount - 1 do
  begin
    Candidate := GetWorkspace.DM_Projects(I);
    if UpperCase(Candidate.DM_ProjectFullPath) = UpperCase(ProjectPath) then
    begin
      Result := Candidate;
      Inc(Matches);
    end;
  end;
  if Matches > 1 then Raise('Project path matches multiple open projects');
end;

function OpenTargetProject(ProjectPath : String) : IProject;
var Candidate;
begin
  Result := FindOpenProject(ProjectPath);
  if Result <> Nil then Exit;
  Candidate := GetWorkspace.DM_OpenProject(ProjectPath, True);
  if Candidate <> Nil then
  begin
    if UpperCase(Candidate.DM_ProjectFullPath) <> UpperCase(ProjectPath) then
      Raise('Opened project path does not match the requested project');
    Result := Candidate;
    Exit;
  end;
  Result := FindOpenProject(ProjectPath);
  if Result = Nil then Raise('Cannot open project');
  if UpperCase(Result.DM_ProjectFullPath) <> UpperCase(ProjectPath) then
    Raise('Resolved project path does not match the requested project');
end;

procedure EmitProjectERC(Project);
var I, Violation, ViolationCount;
begin
  ViolationCount := 0;
  for I := 0 to Project.DM_ViolationCount - 1 do
  begin
    Violation := Project.DM_Violations(I);
    if Violation = Nil then Continue;
    Inc(ViolationCount);
    Emit('{"kind":"erc_violation","message":' + J(Violation.DM_ShortDescriptorString) +
      ',"detail":' + J(Violation.DM_DetailString) +
      ',"severity":' + J(ErrorSeverityName(Violation.DM_ErrorLevel)) +
      ',"source":' + J(Violation.DM_OwnerDocumentName) +
      ',"suppressed":' + BoolJSON(Violation.DM_IsSuppressed) + '}');
  end;
  Emit('{"kind":"erc_summary","complete":true,"compile_success":' + BoolJSON(ActuatorCompileSuccess) +
    ',"connectivity_complete":' + BoolJSON(ActuatorCompileSuccess) +
    ',"violation_count":' + IntToStr(ViolationCount) +
    ',"report_suppressed":' + BoolJSON(Project.DM_ReportSuppressedErrorsInMessages) +
    ',"error_reporting_complete":' + BoolJSON((ActuatorErrorSettingCount > 0) and (not ActuatorErrorSettingFailed)) +
    ',"error_reporting_setting_count":' + IntToStr(ActuatorErrorSettingCount) +
    ',"connection_matrix_complete":false,"configuration_complete":false,"configuration_note":"Saved TypeN to native error-kind ordinal mapping is an AD26 compatibility assumption pending adversarial native validation; connection-matrix settings remain externally verified."}');
end;

procedure EmitIncompleteProjectERC;
begin
  ActuatorErrorSettingCount := 0;
  ActuatorErrorSettingFailed := True;
  Emit('{"kind":"erc_summary","complete":false,"compile_success":false,"connectivity_complete":false,' +
    '"violations_available":false,"violation_count":null,"report_suppressed":null,' +
    '"error_reporting_complete":false,"error_reporting_setting_count":0,' +
    '"connection_matrix_complete":false,"configuration_complete":false,' +
    '"configuration_note":"Project compilation failed; validation and fresh violation enumeration were skipped."}');
end;

procedure VerifyProjectCore(ProjectPath, SheetPath : String; Strict : Boolean);
var Project, ProjectDoc, Flat, Net, Pin, I, K, Doc, Documents;
  PinKey, NetName, AssignedNet, Identity, UnconnectedCount;
begin
  ProjectDoc := Client.GetDocumentByPath(ProjectPath);
  if ProjectDoc = Nil then Raise('Project editor document is unavailable');
  if UpperCase(ProjectDoc.FileName) <> UpperCase(ProjectPath) then Raise('Project editor path does not match the requested project');
  if ProjectDoc.GetModified <> 0 then Raise('Unsaved project options');
  Doc := Client.GetDocumentByPath(SheetPath);
  if Doc <> Nil then
  begin
    if Doc.GetModified <> 0 then Raise('Unsaved schematic edits');
    Client.CloseDocument(Doc);
  end;
  Doc := Client.OpenDocument('SCH', SheetPath);
  if Doc = Nil then Raise('Cannot reopen schematic');
  CollectPhysicalPins(SheetPath);
  ActuatorConnectedPins.Clear;
  ActuatorUnconnectedPinCount := 0;
  InspectSheet(SheetPath);
  Project := OpenTargetProject(ProjectPath);
  ActuatorCompileSuccess := Project.DM_Compile <> 0;
  if ActuatorCompileSuccess then ActuatorCompileSuccess := Project.DM_Compile <> 0;
  if ActuatorCompileSuccess and (Project.DM_ProjectVariantCount > 0) then Raise('Project variants are unsupported');
  Emit('{"kind":"compile_summary","success":' + BoolJSON(ActuatorCompileSuccess) + ',"complete":true}');
  Documents := 0;
  for I := 0 to Project.DM_LogicalDocumentCount - 1 do
    if not IsDerivedVirtualBOM(Project.DM_LogicalDocuments(I), ProjectPath) then Documents := Documents + 1;
  Emit('{"kind":"project","documents":' + IntToStr(Documents) + ',"logical_documents":' + IntToStr(Project.DM_LogicalDocumentCount) + ',"path":' + J(Project.DM_ProjectFullPath) + '}');
  for I := 0 to Project.DM_LogicalDocumentCount - 1 do
  begin
    Flat := Project.DM_LogicalDocuments(I);
    if IsDerivedVirtualBOM(Flat, ProjectPath) then
      Emit('{"kind":"derived_document","path":' + J(Flat.DM_FullPath) + ',"document_kind":' + J(Flat.DM_DocumentKind) + '}')
    else if not ActuatorCompileSuccess then
      Emit('{"kind":"document","path":' + J(Flat.DM_FullPath) + ',"document_kind":' + J(Flat.DM_DocumentKind) + '}')
    else
      Emit('{"kind":"document","path":' + J(Flat.DM_FullPath) + ',"components":' + IntToStr(Flat.DM_ComponentCount) + ',"nets":' + IntToStr(Flat.DM_NetCount) + '}');
  end;
  if not ActuatorCompileSuccess then
  begin
    EmitIncompleteProjectERC;
    if Strict then Raise('Project compile failed');
    Exit;
  end;
  Project.DM_Validate;
  CaptureErrorSettings(Project, ProjectPath);
  Flat := Project.DM_DocumentFlattened;
  if Flat = Nil then Raise('Compiled nets unavailable');
  Emit('{"kind":"compiled","nets":' + IntToStr(Flat.DM_NetCount) + '}');
  for I := 0 to Flat.DM_NetCount - 1 do
  begin
    Net := Flat.DM_Nets(I);
    Emit('{"kind":"net","name":' + J(Net.DM_CalculatedNetName) + '}');
    for K := 0 to Net.DM_PinCount - 1 do
    begin
      Pin := Net.DM_Pins(K);
      PinKey := UpperCase(Pin.DM_LogicalPartDesignator) + '|' + Pin.DM_PinNumber + '|' + Net.DM_CalculatedNetName;
      Identity := UpperCase(Pin.DM_LogicalPartDesignator) + '|' + Pin.DM_PinNumber + '|';
      if ActuatorPhysicalPins.IndexOf(Copy(Identity, 1, Length(Identity) - 1)) < 0 then
        Raise('Compiled pin is missing from the native schematic pin inventory');
      AssignedNet := '';
      for Documents := 0 to ActuatorConnectedPins.Count - 1 do
        if Copy(ActuatorConnectedPins[Documents], 1, Length(Identity)) = Identity then
          AssignedNet := Copy(ActuatorConnectedPins[Documents], Length(Identity) + 1, Length(ActuatorConnectedPins[Documents]));
      if (AssignedNet <> '') and (AssignedNet <> Net.DM_CalculatedNetName) then
        Raise('Conflicting compiled nets for pin ' + Pin.DM_LogicalPartDesignator + '.' + Pin.DM_PinNumber);
      if ActuatorConnectedPins.IndexOf(PinKey) < 0 then ActuatorConnectedPins.Add(PinKey);
      Emit('{"kind":"net_pin","net":' + J(Net.DM_CalculatedNetName) +
        ',"reference":' + J(Pin.DM_LogicalPartDesignator) +
        ',"pin":' + J(Pin.DM_PinNumber) + '}');
    end;
  end;
  UnconnectedCount := 0;
  for I := 0 to ActuatorPhysicalPins.Count - 1 do
  begin
    Identity := ActuatorPhysicalPins[I] + '|';
    AssignedNet := '';
    for K := 0 to ActuatorConnectedPins.Count - 1 do
      if Copy(ActuatorConnectedPins[K], 1, Length(Identity)) = Identity then
        AssignedNet := Copy(ActuatorConnectedPins[K], Length(Identity) + 1, Length(ActuatorConnectedPins[K]));
    if AssignedNet = '' then
    begin
      Inc(UnconnectedCount);
      K := Pos('|', Identity);
      Emit('{"kind":"unconnected_pin","reference":' + J(Copy(Identity, 1, K - 1)) +
        ',"pin":' + J(Copy(Identity, K + 1, Length(Identity) - K - 1)) + '}');
    end;
  end;
  ActuatorUnconnectedPinCount := UnconnectedCount;
  ProjectDoc := Client.GetDocumentByPath(ProjectPath);
  if ProjectDoc = Nil then Raise('Project editor document disappeared during verification');
  if UpperCase(ProjectDoc.FileName) <> UpperCase(ProjectPath) then Raise('Project editor path changed during verification');
  if ProjectDoc.GetModified <> 0 then Raise('Project options changed during verification');
  EmitProjectERC(Project);
end;

procedure VerifyProject(ProjectPath, SheetPath : String);
begin
  VerifyProjectCore(ProjectPath, SheetPath, True);
end;

procedure PrepareProject(ProjectPath : String);
var Project;
begin
  Project := OpenTargetProject(ProjectPath);
end;

procedure NormalizeOwnedSheet(ProjectPath, SheetPath : String);
var Project, Doc, ProjectDoc, I, Matches, Documents, Flat, WasModified, ProjectWasModified;
begin
  // Persist first-read updates only on owned staging, then verify saved bytes again.
  VerifyProject(ProjectPath, SheetPath);
  Project := OpenTargetProject(ProjectPath);
  Doc := Client.GetDocumentByPath(SheetPath);
  if Doc = Nil then Doc := Client.OpenDocument('SCH', SheetPath);
  if Doc = Nil then Raise('Owned staged schematic is unavailable');
  if UpperCase(Doc.FileName) <> UpperCase(SheetPath) then Raise('Owned staged schematic path mismatch');
  Matches := 0;
  Documents := 0;
  for I := 0 to Project.DM_LogicalDocumentCount - 1 do
  begin
    Flat := Project.DM_LogicalDocuments(I);
    if not IsDerivedVirtualBOM(Flat, ProjectPath) then Inc(Documents);
    if UpperCase(Flat.DM_FullPath) = UpperCase(SheetPath) then Inc(Matches);
  end;
  if (Matches <> 1) or (Documents <> 1) then Raise('Normalization requires the exact single owned schematic');
  Doc := Client.GetDocumentByPath(SheetPath);
  if Doc = Nil then Raise('Owned staged schematic disappeared');
  WasModified := Doc.GetModified <> 0;
  if not Doc.DoFileSave('Advanced Schematic binary') then Raise('Cannot save normalized owned schematic');
  if Doc.GetModified <> 0 then Raise('Owned schematic remains modified after normalization');
  ProjectDoc := Client.GetDocumentByPath(ProjectPath);
  if ProjectDoc = Nil then Raise('Owned staged project document is unavailable');
  if UpperCase(ProjectDoc.FileName) <> UpperCase(ProjectPath) then Raise('Owned staged project path mismatch');
  ProjectWasModified := ProjectDoc.GetModified <> 0;
  if not ProjectDoc.DoFileSave('') then Raise('Cannot save normalized owned project');
  if ProjectDoc.GetModified <> 0 then Raise('Owned project remains modified after normalization');
  Emit('{"kind":"normalized_owned_sheet","path":' + J(SheetPath) + ',"was_modified":' + BoolJSON(WasModified) + '}');
  Emit('{"kind":"normalized_owned_project","path":' + J(ProjectPath) + ',"was_modified":' + BoolJSON(ProjectWasModified) + '}');
end;

procedure CloseOwnedProject(ProjectPath, DocumentPath : String);
var I, MemberCount, MemberDoc, ProjectDoc, Project, Flat, ExpectedDoc;
begin
  // Called only for a prior terminal adapter script or an owned staged copy.
  // Close the exact project editor document; AD26 CloseObject focus dispatch
  // did not unload owned projects in the native acceptance probe.
  Project := FindOpenProject(ProjectPath);
  if Project = Nil then Exit;
  ProjectDoc := Client.GetDocumentByPath(ProjectPath);
  if ProjectDoc = Nil then Raise('Owned project editor document is unavailable');
  if UpperCase(ProjectDoc.FileName) <> UpperCase(ProjectPath) then Raise('Project editor path does not match the owned project');
  if ProjectDoc.GetModified <> 0 then Raise('Owned project has unsaved edits; refusing cleanup');
  MemberCount := 0;
  for I := 0 to Project.DM_LogicalDocumentCount - 1 do
  begin
    Flat := Project.DM_LogicalDocuments(I);
    if UpperCase(Flat.DM_FullPath) = UpperCase(DocumentPath) then Inc(MemberCount);
    MemberDoc := Client.GetDocumentByPath(Flat.DM_FullPath);
    if MemberDoc <> Nil then
      if MemberDoc.GetModified <> 0 then Raise('Owned project has an unsaved member document; refusing cleanup: ' + Flat.DM_FullPath);
  end;
  if MemberCount <> 1 then Raise('Expected owned document is not a unique member of the target project');
  Client.CloseDocument(ProjectDoc);
  for I := 0 to GetWorkspace.DM_ProjectCount - 1 do
    if UpperCase(GetWorkspace.DM_Projects(I).DM_ProjectFullPath) = UpperCase(ProjectPath) then Raise('Owned project cleanup did not complete');
  ExpectedDoc := Client.GetDocumentByPath(DocumentPath);
  if ExpectedDoc <> Nil then
  begin
    if UpperCase(ExpectedDoc.FileName) <> UpperCase(DocumentPath) then Raise('Remaining document path does not match the owned document');
    if ExpectedDoc.GetModified <> 0 then Raise('Owned document has unsaved edits; refusing cleanup');
    Client.CloseDocument(ExpectedDoc);
  end;
  if Client.GetDocumentByPath(DocumentPath) <> Nil then Raise('Owned logical document did not close');
end;

procedure PrepareExisting(ProjectPath, SheetPath : String);
var Doc, ProjectDoc, Project, Flat, I, Matches, Documents, PreviouslyOpen;
begin
  Project := FindOpenProject(ProjectPath);
  PreviouslyOpen := Project <> Nil;
  ProjectDoc := Client.GetDocumentByPath(ProjectPath);
  if ProjectDoc <> Nil then
  begin
    if UpperCase(ProjectDoc.FileName) <> UpperCase(ProjectPath) then Raise('Project editor path does not match the requested project');
    if ProjectDoc.GetModified <> 0 then Raise('Unsaved project options');
  end;
  Doc := Client.GetDocumentByPath(SheetPath);
  if Doc <> Nil then
  begin
    if UpperCase(Doc.FileName) <> UpperCase(SheetPath) then Raise('Schematic path does not match the requested sheet');
    if Doc.GetModified <> 0 then Raise('Unsaved schematic edits');
  end;
  if Project <> Nil then
  begin
    if Project.DM_ProjectVariantCount > 0 then Raise('Project variants are unsupported');
    Matches := 0;
    Documents := 0;
    for I := 0 to Project.DM_LogicalDocumentCount - 1 do
    begin
      Flat := Project.DM_LogicalDocuments(I);
      if not IsDerivedVirtualBOM(Flat, ProjectPath) then Inc(Documents);
      if UpperCase(Flat.DM_FullPath) = UpperCase(SheetPath) then Inc(Matches);
    end;
    if (Matches <> 1) or (Documents <> 1) then Raise('Preparation requires the exact single target schematic');
    Project := Nil;
    Flat := Nil;
    ProjectDoc := Nil;
    Doc := Nil;
    CloseOwnedProject(ProjectPath, SheetPath);
    if FindOpenProject(ProjectPath) <> Nil then Raise('Target project remains open before reload');
  end;
  PrepareProject(ProjectPath);
  Project := FindOpenProject(ProjectPath);
  if Project = Nil then Raise('Reloaded project is unavailable');
  ProjectDoc := Client.GetDocumentByPath(ProjectPath);
  if ProjectDoc = Nil then Raise('Reloaded project editor document is unavailable');
  if UpperCase(ProjectDoc.FileName) <> UpperCase(ProjectPath) then Raise('Reloaded project editor path mismatch');
  if ProjectDoc.GetModified <> 0 then Raise('Reloaded project has unsaved edits');
  if Project.DM_ProjectVariantCount > 0 then Raise('Project variants are unsupported');
  Matches := 0;
  Documents := 0;
  for I := 0 to Project.DM_LogicalDocumentCount - 1 do
  begin
    Flat := Project.DM_LogicalDocuments(I);
    if not IsDerivedVirtualBOM(Flat, ProjectPath) then Inc(Documents);
    if UpperCase(Flat.DM_FullPath) = UpperCase(SheetPath) then Inc(Matches);
  end;
  if (Matches <> 1) or (Documents <> 1) then Raise('Reload requires the exact single target schematic');
  Emit('{"kind":"project_configuration_reload","project_path":' + J(ProjectPath) +
    ',"sheet_path":' + J(SheetPath) + ',"complete":true,"previously_open":' + BoolJSON(PreviouslyOpen) + '}');
end;

function ComponentParametersJSON(Component) : String;
var It, Param, Items;
begin
  Items := '';
  It := Component.SchIterator_Create;
  try
    It.AddFilter_ObjectSet(MkSet(eParameter));
    Param := It.FirstSchObject;
    while Param <> Nil do
    begin
      // Comment.Text is the sole writable component field in this release.
      if Param.I_ObjectAddress <> Component.Comment.I_ObjectAddress then
      begin
        if Items <> '' then Items := Items + ',';
        Items := Items + '{"name":' + J(Param.Name) + ',"text":' + J(Param.Text) +
          ',"x":' + IntToStr(Param.Location.X) + ',"y":' + IntToStr(Param.Location.Y) +
          ',"rotation":' + IntToStr(Param.Orientation) + ',"hidden":' + BoolJSON(Param.IsHidden) + '}';
      end;
      Param := It.NextSchObject;
    end;
  finally
    Component.SchIterator_Destroy(It);
  end;
  Result := '[' + Items + ']';
end;

// Exact native coordinates avoid rounding away changes to existing wiring.
procedure InspectEditableSheet(SheetPath : String);
var Sheet, It, Obj, V, Index, Vertices, Name, PinIt, Pin, DX, DY, PinX, PinY;
begin
  Sheet := SchServer.GetSchDocumentByPath(SheetPath);
  if Sheet = Nil then Raise('SCH API unavailable for edit inspection');
  Emit('{"kind":"edit_sheet","width":' + IntToStr(Sheet.CustomX) +
    ',"height":' + IntToStr(Sheet.CustomY) + ',"grid":' + IntToStr(Sheet.SnapGridSize) + '}');
  ActuatorComponentCount := 0; ActuatorPinCount := 0; ActuatorLabelCount := 0; ActuatorWireCount := 0;
  ActuatorJunctionCount := 0; ActuatorPowerCount := 0; ActuatorNoERCCount := 0;
  It := Sheet.SchIterator_Create;
  try
    It.SetState_IterationDepth(eIterateFirstLevel);
    Obj := It.FirstSchObject;
    while Obj <> Nil do
    begin
      if Obj.ObjectId = eSchComponent then
      begin
        Inc(ActuatorComponentCount);
        Emit('{"kind":"edit_component","reference":' + J(Obj.Designator.Text) +
          ',"symbol":' + J(Obj.LibReference) + ',"part":' + IntToStr(Obj.CurrentPartID) +
          ',"part_count":' + IntToStr(Obj.PartCount) + ',"value":' + J(Obj.Comment.Text) +
          ',"uid":' + J(Obj.UniqueId) + ',"x":' + IntToStr(Obj.Location.X) +
          ',"y":' + IntToStr(Obj.Location.Y) + ',"rotation":' + IntToStr(Obj.Orientation) +
          ',"mirrored":' + BoolJSON(Obj.IsMirrored) +
          ',"parameters":' + ComponentParametersJSON(Obj) +
          ',"comment_x":' + IntToStr(Obj.Comment.Location.X) + ',"comment_y":' + IntToStr(Obj.Comment.Location.Y) +
          ',"comment_rotation":' + IntToStr(Obj.Comment.Orientation) +
          ',"comment_hidden":' + BoolJSON(Obj.Comment.IsHidden) + '}');
        PinIt := Obj.SchIterator_Create;
        try
          PinIt.AddFilter_ObjectSet(MkSet(ePin));
          Pin := PinIt.FirstSchObject;
          while Pin <> Nil do
          begin
            DX := 0; DY := 0;
            if Pin.Orientation = 0 then DX := 1;
            if Pin.Orientation = 1 then DY := 1;
            if Pin.Orientation = 2 then DX := -1;
            if Pin.Orientation = 3 then DY := -1;
            PinX := Pin.Location.X + DX * Pin.PinLength;
            PinY := Pin.Location.Y + DY * Pin.PinLength;
          if (Pin.OwnerPartId = 0) or (Pin.OwnerPartId = Obj.CurrentPartID) then
          begin
          Emit('{"kind":"edit_pin","reference":' + J(Obj.Designator.Text) +
              ',"pin":' + J(Pin.Designator) + ',"name":' + J(Pin.Name) +
              ',"electrical_type":' + J(PinElectricalName(Pin)) +
              ',"x":' + IntToStr(PinX) + ',"y":' + IntToStr(PinY) +
              ',"uid":' + J(Pin.UniqueId) + ',"component_uid":' + J(Obj.UniqueId) +
              ',"part":' + IntToStr(Pin.OwnerPartId) +
              ',"orientation":' + IntToStr(Pin.Orientation) + '}');
            Inc(ActuatorPinCount);
          end;
            Pin := PinIt.NextSchObject;
          end;
        finally
          Obj.SchIterator_Destroy(PinIt);
        end;
      end;
      if Obj.ObjectId = eNetLabel then
      begin
        Inc(ActuatorLabelCount);
        Emit('{"kind":"edit_label","name":' + J(Obj.Text) +
          ',"x":' + IntToStr(Obj.Location.X) + ',"y":' + IntToStr(Obj.Location.Y) +
          ',"rotation":' + IntToStr(Obj.Orientation) +
          ',"justification":' + IntToStr(Obj.Justification) + ',"font_id":' + IntToStr(Obj.FontID) +
          ',"uid":' + J(Obj.UniqueId) + '}');
      end;
      if Obj.ObjectId = eWire then
      begin
        Inc(ActuatorWireCount);
        Vertices := '';
        for Index := 1 to Obj.GetState_VerticesCount do
        begin
          V := Obj.GetState_Vertex(Index);
          if Index > 1 then Vertices := Vertices + ',';
          Vertices := Vertices + '[' + IntToStr(V.X) + ',' + IntToStr(V.Y) + ']';
        end;
        Emit('{"kind":"edit_wire","vertices":[' + Vertices +
          '],"line_width":' + IntToStr(Obj.LineWidth) + ',"color":' + IntToStr(Obj.Color) +
          ',"uid":' + J(Obj.UniqueId) + '}');
      end;
      if Obj.ObjectId = eJunction then
      begin
        Inc(ActuatorJunctionCount);
        Emit('{"kind":"edit_junction","x":' + IntToStr(Obj.Location.X) +
          ',"y":' + IntToStr(Obj.Location.Y) + ',"uid":' + J(Obj.UniqueId) + '}');
      end;
      if Obj.ObjectId = ePowerObject then
      begin
        Inc(ActuatorPowerCount);
        Emit('{"kind":"edit_power","name":' + J(Obj.Text) +
          ',"x":' + IntToStr(Obj.Location.X) + ',"y":' + IntToStr(Obj.Location.Y) +
          ',"rotation":' + IntToStr(Obj.Orientation) + ',"style":' + IntToStr(Obj.Style) +
          ',"uid":' + J(Obj.UniqueId) + '}');
      end;
      if Obj.ObjectId = eNoERC then
      begin
        Inc(ActuatorNoERCCount);
        Emit('{"kind":"edit_noerc","x":' + IntToStr(Obj.Location.X) +
          ',"y":' + IntToStr(Obj.Location.Y) + ',"uid":' + J(Obj.UniqueId) + '}');
      end;
      if Obj.ObjectId = eParameter then
        Emit('{"kind":"edit_sheet_parameter","name":' + J(Obj.Name) + ',"text":' + J(Obj.Text) +
          ',"x":' + IntToStr(Obj.Location.X) + ',"y":' + IntToStr(Obj.Location.Y) +
          ',"rotation":' + IntToStr(Obj.Orientation) + ',"hidden":' + BoolJSON(Obj.IsHidden) + '}');
      // Any naming/hierarchy object blocks v1 rename rather than silently
      // reinterpreting bus, power, hierarchical or harness naming rules.
      if (Obj.ObjectId = ePort) or
         (Obj.ObjectId = eBus) or (Obj.ObjectId = eBusEntry) or
         (Obj.ObjectId = eSheetSymbol) or
         ((Obj.ObjectId <> eSchComponent) and (Obj.ObjectId <> eNetLabel) and
           (Obj.ObjectId <> eWire) and (Obj.ObjectId <> eJunction) and
           (Obj.ObjectId <> ePowerObject) and (Obj.ObjectId <> eNoERC) and
           (Obj.ObjectId <> eParameter)) then
      begin
        Name := '';
        if Obj.ObjectId = ePort then Name := Obj.Name;
        Emit('{"kind":"edit_blocker","object_type":' + IntToStr(Obj.ObjectId) +
          ',"name":' + J(Name) + ',"x":' + IntToStr(Obj.Location.X) +
          ',"y":' + IntToStr(Obj.Location.Y) + '}');
      end;
      Obj := It.NextSchObject;
    end;
  finally
    Sheet.SchIterator_Destroy(It);
  end;
end;

procedure InspectExisting(ProjectPath, SheetPath : String);
begin
  ActuatorPhysicalPins.Clear;
  ActuatorConnectedPins.Clear;
  VerifyProjectCore(ProjectPath, SheetPath, False);
  InspectEditableSheet(SheetPath);
  Emit('{"kind":"capture_summary","components":' + IntToStr(ActuatorComponentCount) +
    ',"pins":' + IntToStr(ActuatorPinCount) + ',"unconnected_pins":' + IntToStr(ActuatorUnconnectedPinCount) +
    ',"labels":' + IntToStr(ActuatorLabelCount) + ',"wires":' + IntToStr(ActuatorWireCount) +
    ',"junctions":' + IntToStr(ActuatorJunctionCount) + ',"powers":' + IntToStr(ActuatorPowerCount) +
    ',"noerc":' + IntToStr(ActuatorNoERCCount) + ',"complete":true,"connectivity_complete":' + BoolJSON(ActuatorCompileSuccess) + '}');
end;

procedure SetComponentValue(Ref, ExpectedValue, NewValue : String);
var It, Obj, Target, Matches;
begin
  Target := Nil;
  Matches := 0;
  It := ActuatorSch.SchIterator_Create;
  try
    It.AddFilter_ObjectSet(MkSet(eSchComponent));
    It.SetState_IterationDepth(eIterateFirstLevel);
    Obj := It.FirstSchObject;
    while Obj <> Nil do
    begin
      if UpperCase(Obj.Designator.Text) = UpperCase(Ref) then
      begin
        Target := Obj;
        Matches := Matches + 1;
      end;
      Obj := It.NextSchObject;
    end;
  finally
    ActuatorSch.SchIterator_Destroy(It);
  end;
  if Matches <> 1 then Raise('Component reference is missing or ambiguous');
  if Target.PartCount <> 1 then Raise('Multipart component edits are unsupported');
  if Target.Comment.Text <> ExpectedValue then Raise('Component value changed since preview');
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    SchServer.RobotManager.SendMessage(Target.Comment.I_ObjectAddress, c_BroadCast, SCHM_BeginModify, c_NoEventData);
    Target.Comment.Text := NewValue;
    SchServer.RobotManager.SendMessage(Target.Comment.I_ObjectAddress, c_BroadCast, SCHM_EndModify, c_NoEventData);
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, '');
  end;
end;

procedure RenameNetLabels(OldName, NewName : String);
var It, Obj, Matches;
begin
  Matches := 0;
  It := ActuatorSch.SchIterator_Create;
  try
    It.AddFilter_ObjectSet(MkSet(eNetLabel));
    It.SetState_IterationDepth(eIterateFirstLevel);
    Obj := It.FirstSchObject;
    while Obj <> Nil do
    begin
      if UpperCase(Obj.Text) = UpperCase(NewName) then Raise('Target net label already exists');
      if UpperCase(Obj.Text) = UpperCase(OldName) then Matches := Matches + 1;
      Obj := It.NextSchObject;
    end;
    if Matches = 0 then Raise('No matching net labels');
    SchServer.ProcessControl.PreProcess(ActuatorSch, '');
    try
      Obj := It.FirstSchObject;
      while Obj <> Nil do
      begin
        if UpperCase(Obj.Text) = UpperCase(OldName) then
        begin
          SchServer.RobotManager.SendMessage(Obj.I_ObjectAddress, c_BroadCast, SCHM_BeginModify, c_NoEventData);
          Obj.Text := NewName;
          SchServer.RobotManager.SendMessage(Obj.I_ObjectAddress, c_BroadCast, SCHM_EndModify, c_NoEventData);
        end;
        Obj := It.NextSchObject;
      end;
    finally
      SchServer.ProcessControl.PostProcess(ActuatorSch, '');
    end;
  finally
    ActuatorSch.SchIterator_Destroy(It);
  end;
end;

function EditObjectType(Obj, ExpectedType : String) : Boolean;
begin
  Result := False;
  if (ExpectedType = 'component') and (Obj.ObjectId = eSchComponent) then Result := True
  else if (ExpectedType = 'wire') and (Obj.ObjectId = eWire) then Result := True
  else if (ExpectedType = 'label') and (Obj.ObjectId = eNetLabel) then Result := True
  else if (ExpectedType = 'junction') and (Obj.ObjectId = eJunction) then Result := True
  else if (ExpectedType = 'power') and (Obj.ObjectId = ePowerObject) then Result := True
  else if (ExpectedType = 'noerc') and (Obj.ObjectId = eNoERC) then Result := True;
end;

procedure FindEditObject(Uid, ExpectedType : String; var Target, Matches);
var It, Obj;
begin
  Target := Nil; Matches := 0;
  It := ActuatorSch.SchIterator_Create;
  try
    It.SetState_IterationDepth(eIterateFirstLevel);
    Obj := It.FirstSchObject;
    while Obj <> Nil do
    begin
      if EditObjectType(Obj, ExpectedType) and (Obj.UniqueId = Uid) then
      begin
        Target := Obj;
        Inc(Matches);
      end;
      Obj := It.NextSchObject;
    end;
  finally
    ActuatorSch.SchIterator_Destroy(It);
  end;
  if Matches <> 1 then Raise('UID must match exactly one supported schematic object');
end;

procedure EnsureEditUidUnused(Uid : String);
var It, Obj;
begin
  if Uid = '' then Raise('A non-empty unique id is required');
  It := ActuatorSch.SchIterator_Create;
  try
    It.SetState_IterationDepth(eIterateFirstLevel);
    Obj := It.FirstSchObject;
    while Obj <> Nil do
    begin
      if Obj.UniqueId = Uid then Raise('The requested unique id is already in use');
      Obj := It.NextSchObject;
    end;
  finally
    ActuatorSch.SchIterator_Destroy(It);
  end;
end;

procedure RegisterEditObject(Obj);
begin
  ActuatorSch.RegisterSchObjectInContainer(Obj);
  SchServer.RobotManager.SendMessage(ActuatorSch.I_ObjectAddress, c_BroadCast,
    SCHM_PrimitiveRegistration, Obj.I_ObjectAddress);
end;

procedure DeleteObject(Uid, ExpectedType : String);
var Target, Matches;
begin
  FindEditObject(Uid, ExpectedType, Target, Matches);
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    if ExpectedType = 'component' then ActuatorSch.UnRegisterSchObjectFromContainer(Target)
    else ActuatorSch.RemoveSchObject(Target);
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, 'Edit');
    ActuatorSch.GraphicallyInvalidate;
  end;
end;

procedure MoveComponent(Uid : String; X, Y, Rotation : Integer);
var Target, Matches, OldCommentX, OldCommentY, OldCommentRotation, OldCommentAutoposition, DX, DY, NewDX, NewDY, Delta;
begin
  if (Rotation < 0) or (Rotation > 270) or ((Rotation mod 90) <> 0) then Raise('Rotation must be 0, 90, 180 or 270 degrees');
  FindEditObject(Uid, 'component', Target, Matches);
  OldCommentX := Target.Comment.Location.X;
  OldCommentY := Target.Comment.Location.Y;
  OldCommentRotation := Target.Comment.Orientation;
  OldCommentAutoposition := Target.Comment.Autoposition;
  DX := OldCommentX - Target.Location.X;
  DY := OldCommentY - Target.Location.Y;
  Delta := (Rotation div 90 - Target.Orientation + 4) mod 4;
  NewDX := DX; NewDY := DY;
  if Delta = 1 then begin NewDX := -DY; NewDY := DX; end
  else if Delta = 2 then begin NewDX := -DX; NewDY := -DY; end
  else if Delta = 3 then begin NewDX := DY; NewDY := -DX; end;
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    SchServer.RobotManager.SendMessage(Target.I_ObjectAddress, c_BroadCast, SCHM_BeginModify, c_NoEventData);
    Target.Comment.Autoposition := False;
    try
      Target.Orientation := Rotation div 90;
      Target.MoveToXY(MilsToCoord(X), MilsToCoord(Y));
      Target.Comment.Location := Point(MilsToCoord(X) + NewDX, MilsToCoord(Y) + NewDY);
      Target.Comment.Orientation := (OldCommentRotation + Delta) mod 4;
    finally
      Target.Comment.Autoposition := OldCommentAutoposition;
    end;
    SchServer.RobotManager.SendMessage(Target.I_ObjectAddress, c_BroadCast, SCHM_EndModify, c_NoEventData);
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, 'Edit');
    ActuatorSch.GraphicallyInvalidate;
  end;
end;

procedure SetComponentParameter(Uid, Name, ExpectedValue, NewValue : String);
var Target, Matches, It, Param, Selected, ParamMatches, CreateParameter;
begin
  FindEditObject(Uid, 'component', Target, Matches);
  if (Name = '') or (UpperCase(Name) = 'COMMENT') or (UpperCase(Name) = 'VALUE') then Raise('Component parameter name is invalid or reserved');
  It := Target.SchIterator_Create;
  Selected := Nil; ParamMatches := 0;
  try
    It.AddFilter_ObjectSet(MkSet(eParameter));
    Param := It.FirstSchObject;
    while Param <> Nil do
    begin
      if Param.Name = Name then
      begin
        Selected := Param;
        Inc(ParamMatches);
      end;
      Param := It.NextSchObject;
    end;
  finally
    Target.SchIterator_Destroy(It);
  end;
  if ParamMatches > 1 then Raise('Component parameter name is ambiguous');
  CreateParameter := ParamMatches = 0;
  if CreateParameter and (ExpectedValue <> '') then Raise('Component parameter is missing since preview');
  if (not CreateParameter) and (Selected.Text <> ExpectedValue) then Raise('Component parameter changed since preview');
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    if CreateParameter then
    begin
      Selected := SchServer.SchObjectFactory(eParameter, eCreate_Default);
      if Selected = Nil then Raise('Cannot create component parameter');
      Selected.Name := Name;
      Selected.Text := NewValue;
      Selected.ParamType := eParameterType_String;
      Selected.ReadOnlyState := eReadOnly_None;
      Selected.IsHidden := True;
      Target.AddSchObject(Selected);
      SchServer.RobotManager.SendMessage(Target.I_ObjectAddress, c_BroadCast,
        SCHM_PrimitiveRegistration, Selected.I_ObjectAddress);
    end
    else
    begin
      SchServer.RobotManager.SendMessage(Selected.I_ObjectAddress, c_BroadCast, SCHM_BeginModify, c_NoEventData);
      Selected.Text := NewValue;
      SchServer.RobotManager.SendMessage(Selected.I_ObjectAddress, c_BroadCast, SCHM_EndModify, c_NoEventData);
    end;
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, 'Edit');
    ActuatorSch.GraphicallyInvalidate;
  end;
end;

procedure AddComponent(LibraryPath, SymbolName, Ref, Value, Uid : String; X, Y, Rotation, Part : Integer);
var Comp, It, Obj;
begin
  if (Rotation < 0) or (Rotation > 270) or ((Rotation mod 90) <> 0) then Raise('Rotation must be 0, 90, 180 or 270 degrees');
  EnsureEditUidUnused(Uid);
  It := ActuatorSch.SchIterator_Create;
  try
    It.AddFilter_ObjectSet(MkSet(eSchComponent));
    Obj := It.FirstSchObject;
    while Obj <> Nil do
    begin
      if UpperCase(Obj.Designator.Text) = UpperCase(Ref) then Raise('Component designator already exists');
      Obj := It.NextSchObject;
    end;
  finally
    ActuatorSch.SchIterator_Destroy(It);
  end;
  Comp := SchServer.LoadComponentFromLibrary(SymbolName, LibraryPath);
  if Comp = Nil then Raise('Cannot load symbol: ' + SymbolName);
  Comp.CurrentPartID := Part;
  Comp.Orientation := Rotation div 90;
  Comp.MoveToXY(MilsToCoord(X), MilsToCoord(Y));
  Comp.Designator.Text := Ref;
  Comp.Comment.Text := Value;
  Comp.UniqueId := Uid;
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    RegisterEditObject(Comp);
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, 'Edit');
  end;
end;

procedure BeginWire(Uid : String);
begin
  if ActuatorWire <> Nil then Raise('A wire is already being created');
  EnsureEditUidUnused(Uid);
  ActuatorWire := SchServer.SchObjectFactory(eWire, eCreate_Default);
  if ActuatorWire = Nil then Raise('Cannot create wire');
  ActuatorWire.UniqueId := Uid;
end;

procedure WireVertex(X, Y : Integer);
var Index;
begin
  if ActuatorWire = Nil then Raise('BeginWire must run before WireVertex');
  Index := ActuatorWire.GetState_VerticesCount + 1;
  ActuatorWire.InsertVertex := Index;
  ActuatorWire.SetState_Vertex(Index, Point(MilsToCoord(X), MilsToCoord(Y)));
end;

procedure EndWire;
begin
  if ActuatorWire = Nil then Raise('BeginWire must run before EndWire');
  if ActuatorWire.GetState_VerticesCount < 2 then Raise('Wire needs at least two vertices');
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    RegisterEditObject(ActuatorWire);
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, 'Edit');
    ActuatorSch.GraphicallyInvalidate;
  end;
  ActuatorWire := Nil;
end;

procedure AddLabel(Uid, Text : String; X, Y, Rotation : Integer);
var Obj;
begin
  if (Rotation < 0) or (Rotation > 270) or ((Rotation mod 90) <> 0) then Raise('Rotation must be 0, 90, 180 or 270 degrees');
  EnsureEditUidUnused(Uid);
  Obj := SchServer.SchObjectFactory(eNetLabel, eCreate_Default);
  if Obj = Nil then Raise('Cannot create net label');
  Obj.UniqueId := Uid;
  Obj.Text := Text;
  Obj.Location := Point(MilsToCoord(X), MilsToCoord(Y));
  Obj.Orientation := Rotation div 90;
  Obj.FontID := SchServer.FontManager.GetFontID(8, 0, False, False, False, False, 'Arial');
  Obj.Justification := eJustify_BottomLeft;
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    RegisterEditObject(Obj);
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, 'Edit');
    ActuatorSch.GraphicallyInvalidate;
  end;
end;

procedure AddJunction(Uid : String; X, Y : Integer);
var Obj;
begin
  EnsureEditUidUnused(Uid);
  Obj := SchServer.SchObjectFactory(eJunction, eCreate_Default);
  if Obj = Nil then Raise('Cannot create junction');
  Obj.UniqueId := Uid;
  Obj.Location := Point(MilsToCoord(X), MilsToCoord(Y));
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    RegisterEditObject(Obj);
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, 'Edit');
    ActuatorSch.GraphicallyInvalidate;
  end;
end;

procedure AddPower(Uid, Text : String; X, Y, Rotation, Style : Integer);
var Obj;
begin
  if (Rotation < 0) or (Rotation > 270) or ((Rotation mod 90) <> 0) then Raise('Rotation must be 0, 90, 180 or 270 degrees');
  EnsureEditUidUnused(Uid);
  Obj := SchServer.SchObjectFactory(ePowerObject, eCreate_Default);
  if Obj = Nil then Raise('Cannot create power port');
  Obj.UniqueId := Uid;
  Obj.Text := Text;
  Obj.Location := Point(MilsToCoord(X), MilsToCoord(Y));
  Obj.Orientation := Rotation div 90;
  if Style = 0 then Obj.Style := ePowerCircle
  else if Style = 1 then Obj.Style := ePowerArrow
  else if Style = 2 then Obj.Style := ePowerBar
  else if Style = 3 then Obj.Style := ePowerWave
  else if Style = 4 then Obj.Style := ePowerGndPower
  else if Style = 5 then Obj.Style := ePowerGndSignal
  else if Style = 6 then Obj.Style := ePowerGndEarth
  else Raise('Unsupported power-port style');
  Obj.ShowNetName := True;
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    RegisterEditObject(Obj);
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, 'Edit');
    ActuatorSch.GraphicallyInvalidate;
  end;
end;

procedure AddNoERC(Uid : String; X, Y : Integer);
var Obj;
begin
  EnsureEditUidUnused(Uid);
  Obj := SchServer.SchObjectFactory(eNoERC, eCreate_Default);
  if Obj = Nil then Raise('Cannot create No-ERC marker');
  Obj.UniqueId := Uid;
  Obj.Location := Point(MilsToCoord(X), MilsToCoord(Y));
  SchServer.ProcessControl.PreProcess(ActuatorSch, '');
  try
    RegisterEditObject(Obj);
  finally
    SchServer.ProcessControl.PostProcess(ActuatorSch, 'Edit');
    ActuatorSch.GraphicallyInvalidate;
  end;
end;

procedure CloseCleanSheet(SheetPath : String);
var Doc;
begin
  Doc := Client.GetDocumentByPath(SheetPath);
  if Doc = Nil then Exit;
  if UpperCase(Doc.FileName) <> UpperCase(SheetPath) then Raise('Open document path does not match the requested sheet');
  if Doc.GetModified <> 0 then Raise('Unsaved schematic edits');
  Client.CloseDocument(Doc);
  if Client.GetDocumentByPath(SheetPath) <> Nil then Raise('Clean sheet did not close');
end;

procedure ActuatorRun;
begin
  ActuatorOutput := TStringList.Create;
  ActuatorPhysicalPins := TStringList.Create;
  ActuatorConnectedPins := TStringList.Create;
  ActuatorWire := Nil;
  try
    Emit('{"operation_id":' + J(@@OPERATION_ID@@) + '}');
    try
      @@COMMAND@@
      Emit('{"status":"ok"}');
    except
      Emit('{"status":"error","message":' + J(ExceptionMessage) + '}');
    end;
    ActuatorOutput.SaveToFile(@@RESPONSE_TMP@@);
    RenameFile(@@RESPONSE_TMP@@, @@RESPONSE@@);
  finally
    ActuatorConnectedPins.Free;
    ActuatorPhysicalPins.Free;
    ActuatorOutput.Free;
  end;
end;
