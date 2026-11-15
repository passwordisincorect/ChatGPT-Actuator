"""Electrical, authorization and uncertain-delivery contracts for the AD26 adapter."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chatgpt_actuator.audit import AuditLogger
from chatgpt_actuator.config import EDAConfig
from chatgpt_actuator.security import PathGuard
from chatgpt_actuator.errors import AccessDeniedError
from chatgpt_actuator.tool_policy import Principal, principal_context, authorize
from chatgpt_actuator.tools.eda_altium import AltiumAdapter, _hash, _pas, _write, compare_nets


class AltiumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='actuator-altium-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_temp = tempfile.TemporaryDirectory(prefix='actuator-altium-private-state-')
        self.addCleanup(self.state_temp.cleanup)
        config = EDAConfig(True, True, True, False, (str(self.root),), '', '', .02, 1, 200000)
        self.api = AltiumAdapter(config, AuditLogger(False, str(self.root/'audit.log')), PathGuard((self.root,)),
                                 lambda: Path('X2.EXE'), state_dir=self.state_temp.name,
                                 excluded_roots=(self.root,))
        self.library = self.root/'parts.SchLib'
        self.library.write_bytes(b'fixture')
        self.pins = [dict(number='1', name='A', part=1, x=0, y=0, length=100, orientation=2),
                     dict(number='2', name='B', part=1, x=300, y=0, length=100, orientation=0)]
        self.catalog = {'path':str(self.library),'sha256':_hash(self.library),'symbols':[
            {'name':'R','parts':2,'source_path':str(self.library),'pins':self.pins +
             [dict(number='3', name='C', part=2, x=0, y=0, length=100, orientation=2)]}]}
        self.components = [dict(reference='R1',symbol='R',value='1k',library_path=str(self.library),x=1500,y=1500)]
        self.nets = [dict(name='VCC',pins=[dict(reference='R1',pin='1')]), dict(name='GND',pins=[dict(reference='R1',pin='2')])]
        mock = patch.object(self.api,'library_inspect',return_value=self.catalog)
        self.inspect = mock.start();self.addCleanup(mock.stop)

    def create(self, components=None, nets=None, **kw):
        return self.api.plan_create(str(self.root),'test', self.components if components is None else components,
                                    self.nets if nets is None else nets, **kw)

    def plan(self):
        result = self.create()
        self.assertEqual(result['status'],'ready')
        return result['ir_path'], json.loads(Path(result['ir_path']).read_text())

    def test_create_revision_requires_cas_and_retains_history(self):
        result = self.create()
        with self.assertRaises(ValueError): self.create()
        changed = self.create(expected_revision=1)
        self.assertEqual(changed['revision'],2)
        self.assertEqual(len(list((Path(result['ir_path']).parent/'.history').glob('*.json'))),1)

    def test_duplicate_reference_and_unknown_pin_are_invalid(self):
        self.assertFalse(self.create(components=self.components*2)['validation']['ok'])
        nets=deepcopy(self.nets);nets[0]['pins'][0]['pin']='404'
        self.assertFalse(self.create(nets=nets)['validation']['ok'])

    def test_pin_cannot_be_in_two_nets_or_repeated(self):
        nets=deepcopy(self.nets);nets[1]['pins'].append(nets[0]['pins'][0])
        self.assertFalse(self.create(nets=nets)['validation']['ok'])
        nets=deepcopy(self.nets);nets[0]['pins']*=2
        self.assertFalse(self.create(nets=nets)['validation']['ok'])

    def test_names_are_unique_case_insensitively(self):
        nets=deepcopy(self.nets);nets[1]['name']='vcc'
        self.assertFalse(self.create(nets=nets)['validation']['ok'])

    def test_missing_symbol_and_part_are_invalid(self):
        for change in ({'symbol':'missing'},{'part':3}):
            with self.subTest(change=change):
                components=deepcopy(self.components);components[0].update(change)
                self.assertFalse(self.create(components=components)['validation']['ok'])

    def test_multipart_reference_resolves_physical_pins(self):
        components=deepcopy(self.components);components.append(dict(components[0],part=2,x=3500))
        nets=deepcopy(self.nets);nets.append(dict(name='OTHER',pins=[dict(reference='R1',pin='3')]))
        result=self.create(components=components,nets=nets)
        self.assertTrue(result['validation']['ok'])
        components[1]['value']='different'
        self.assertFalse(self.create(components=components,nets=nets,expected_revision=1)['validation']['ok'])

    def test_invalid_coordinates_and_rotation(self):
        for change in ({'x':float('nan')},{'rotation':45},{'part':True},{'x':None},{'y':-1}):
            with self.subTest(change=change):
                components=deepcopy(self.components);components[0].update(change)
                self.assertFalse(self.create(components=components)['validation']['ok'])

    def test_numeric_pin_inputs_are_canonicalized(self):
        nets=deepcopy(self.nets);nets[0]['pins'][0]['pin']=1
        path,_=self.plan()
        self.create(nets=nets,expected_revision=1)
        self.assertEqual(json.loads(Path(path).read_text())['nets'][0]['pins'][0]['pin'],'1')

    def test_outside_library_and_project_paths_are_denied(self):
        outside=Path(self.temp.name).parent/'outside.SchLib'
        outside.write_bytes(b'outside')
        self.addCleanup(outside.unlink)
        components=deepcopy(self.components);components[0]['library_path']=str(outside)
        with self.assertRaises(AccessDeniedError): self.create(components=components)
        with self.assertRaises(AccessDeniedError): self.api.plan_create(str(self.root.parent),'test',self.components,self.nets)

    def test_reserved_windows_names_are_rejected(self):
        with self.assertRaises(ValueError): self.api.plan_create(str(self.root),'CON',self.components,self.nets)

    def test_edit_and_execute_scopes_are_independently_enforced(self):
        token=principal_context.set(Principal(scopes=frozenset({'mcp:read'})))
        try:
            with self.assertRaises(PermissionError):self.create()
            with self.assertRaises(PermissionError):self.api._run('Emit(1);',kind='probe')
        finally: principal_context.reset(token)

    def test_scope_manifest_classifies_all_six_tools(self):
        p=Principal()
        for name in ('library_inspect','sch_plan_create','sch_validate','sch_materialize','sch_verify','operation_status'):
            authorize('eda_altium_'+name,p)

    def test_real_hot_endpoint_rotations(self):
        c=dict(self.components[0],rotation=0)
        expected=[(1400,1500,2),(1500,1400,3),(1600,1500,0),(1500,1600,1)]
        for angle,point in zip((0,90,180,270),expected):
            c['rotation']=angle
            self.assertEqual(self.api._pin_position(c,self.pins[0]),point)

    def test_routes_start_at_hot_end_and_have_200mil_stubs(self):
        _,plan=self.plan();routes=self.api._routes(plan)
        for route in routes:
            self.assertEqual(abs(route['x1']-route['x2'])+abs(route['y1']-route['y2']),200)
        self.assertEqual({r['net'] for r in routes},{'VCC','GND'})

    def test_overlapping_component_bodies_are_rejected(self):
        components=deepcopy(self.components);components.append(dict(components[0],reference='R2'))
        result=self.create(components=components)
        plan=json.loads(Path(result['ir_path']).read_text())
        with self.assertRaises(ValueError):self.api._routes(plan)

    def test_long_adjacent_labels_fit_without_shared_boxes(self):
        _,plan=self.plan();plan['nets'][0]['name']='POWER_SUPPLY_VERY_LONG_NAME'
        with self.assertRaises(ValueError):self.api._routes(plan)
        plan['components'][0]['x']=4000
        self.assertEqual(len(self.api._routes(plan)),2)

    def test_missing_pin_short_and_extra_net_are_detected(self):
        _,plan=self.plan()
        rows=[{'kind':'compiled'}, {'kind':'net_pin','net':'VCC','reference':'R1','pin':'1'},
              {'kind':'net_pin','net':'GND','reference':'R1','pin':'2'}]
        self.assertTrue(compare_nets(plan,rows)['ok'])
        self.assertFalse(compare_nets(plan,rows[:-1])['ok'])
        short=deepcopy(rows);short[-1]['net']='VCC'
        self.assertFalse(compare_nets(plan,short)['ok'])
        self.assertFalse(compare_nets(plan,rows+[dict(kind='net_pin',net='OTHER',reference='R2',pin='1')])['ok'])
        self.assertFalse(compare_nets(plan,rows[1:])['ok'])

    def test_saved_inventory_must_match_in_addition_to_nets(self):
        _,plan=self.plan();rows=[dict(kind='compiled')]+[dict(kind='net_pin',net=n['name'],**p) for n in plan['nets'] for p in n['pins']]
        self.assertFalse(self.api._comparison(plan,rows)['ok'])
        rows.append(dict(kind='observed_component',reference='R1',symbol='R',part=1,value='1k'))
        self.assertTrue(self.api._comparison(plan,rows)['ok'])

    def test_string_data_cannot_inject_pascal_or_template_tokens(self):
        self.assertEqual(_pas("x'); RunProcess('evil');"),"'x''); RunProcess(''evil'');'")
        self.assertIn('#10',_pas('a\nb'))
        self.assertIn('#937',_pas('Ω'))

    def dispatch(self, body='Emit(1);', **kw):
        # r2 uses its Windows mutex and operation journal, not PCBA's native broker.
        return self.api._run(body,kind='probe',**kw)

    def responder(self,args,**kw):
        project=args[1].split('ProjectName=',1)[1].split('|',1)[0]
        folder=Path(project).parent
        (folder/'response.jsonl').write_text(json.dumps({'operation_id':folder.name})+'\n'+json.dumps({'status':'ok'})+'\n',encoding='utf-8')
        return SimpleNamespace(pid=999)

    def test_successful_response_and_retry_do_not_relaunch(self):
        with patch('subprocess.Popen',side_effect=self.responder) as launch:
            first=self.dispatch(request_key='same',payload={'x':1})
            second=self.dispatch(request_key='same',payload={'x':1})
            self.assertEqual(first['operation_id'],second['operation_id'])
            self.assertEqual(launch.call_count,1)
            with self.assertRaises(ValueError):self.dispatch(request_key='same',payload={'x':2})

    def test_timeout_remains_unknown_and_blocks_replay(self):
        with patch('subprocess.Popen',return_value=SimpleNamespace(pid=999)) as launch:
            result=self.dispatch(request_key='same')
            self.assertEqual(result['status'],'unknown')
            self.assertEqual(self.dispatch(request_key='same')['operation_id'],result['operation_id'])
            with self.assertRaises(RuntimeError):self.dispatch(request_key='different')
            self.assertEqual(launch.call_count,1)

    def test_prior_terminal_script_cleanup_is_best_effort_before_requested_command(self):
        previous_id = 'a' * 32
        previous_folder = self.api._op_path(previous_id)
        previous_folder.mkdir(parents=True)
        (previous_folder/'Bridge.PrjScr').write_text('project')
        (previous_folder/'Bridge.pas').write_text('script')
        _write(previous_folder/'operation.json', dict(operation_id=previous_id, owner='local', kind='probe', status='succeeded'))
        _write(self.api.state_root/'active.json', dict(operation_id=previous_id))
        with patch('subprocess.Popen', side_effect=self.responder):
            result = self.dispatch('Emit("requested command");')
        generated = (self.api._op_path(result['operation_id'])/'Bridge.pas').read_text(encoding='utf-8-sig')
        cleanup_start = generated.index('try\n  CloseOwnedProject(')
        cleanup_end = generated.index('end;\nEmit("requested command");', cleanup_start)
        self.assertLess(cleanup_start, cleanup_end)
        cleanup = generated[cleanup_start:cleanup_end]
        self.assertIn('except', cleanup)
        self.assertIn('"script_cleanup_warning"', cleanup)
        self.assertIn('J(ExceptionMessage)', cleanup)
        self.assertEqual(generated.count('Emit("requested command");'), 1)
        self.assertEqual(result['cleanup_previous_operation_id'], previous_id)

    def test_late_response_is_reconciled_without_replay(self):
        with patch('subprocess.Popen',return_value=SimpleNamespace(pid=999)):
            result=self.dispatch(request_key='late')
        folder=self.api._op_path(result['operation_id'])
        (folder/'response.jsonl').write_text(json.dumps({'operation_id':folder.name})+'\n'+json.dumps({'status':'ok'})+'\n')
        self.assertEqual(self.api.operation_status(result['operation_id'])['status'],'succeeded')

    def test_partial_response_without_terminal_marker_stays_unknown(self):
        with patch('subprocess.Popen', return_value=SimpleNamespace(pid=999)):
            result = self.dispatch(request_key='partial-without-marker')
        folder = self.api._op_path(result['operation_id'])
        (folder/'response.jsonl').write_text(json.dumps({'operation_id':folder.name})+'\n')
        self.assertEqual(self.api.operation_status(result['operation_id'])['status'], 'unknown')

    def test_stale_response_cannot_certify_success(self):
        def stale(args,**kw):
            proc=self.responder(args,**kw)
            folder=Path(args[1].split('ProjectName=',1)[1].split('|',1)[0]).parent
            (folder/'response.jsonl').write_text('{"operation_id":"old"}\n{"status":"ok"}\n')
            return proc
        with patch('subprocess.Popen',side_effect=stale):
            self.assertEqual(self.dispatch()['status'],'unknown')

    def test_missing_altium_is_reported_before_dispatch(self):
        self.api.discover=lambda:None
        with self.assertRaises(FileNotFoundError):self.dispatch()

    def test_vendor_error_is_terminal_and_retained(self):
        def error(args,**kw):
            proc=self.responder(args,**kw)
            folder=Path(args[1].split('ProjectName=',1)[1].split('|',1)[0]).parent
            (folder/'response.jsonl').write_text(json.dumps({'operation_id':folder.name})+'\n{"status":"error","message":"modal"}\n')
            return proc
        with patch('subprocess.Popen',side_effect=error):self.assertEqual(self.dispatch()['status'],'failed')

    def test_foreign_owner_cannot_read_operations(self):
        with patch('subprocess.Popen',side_effect=self.responder):result=self.dispatch()
        token=principal_context.set(Principal(owner='other'))
        try:
            with self.assertRaises(PermissionError):self.api.operation_status(result['operation_id'])
        finally:principal_context.reset(token)

    def test_readonly_status_never_publishes(self):
        op='a'*32;folder=self.api._op_path(op)
        _write(folder/'operation.json',dict(operation_id=op,owner='local',kind='materialize',status='succeeded',rows=[]))
        with patch.object(self.api,'_finish_materialize') as finish:
            self.assertTrue(self.api.operation_status(op)['publication_pending'])
            finish.assert_not_called()

    def test_external_library_change_requires_a_new_plan(self):
        path,_=self.plan();self.catalog['sha256']='changed'
        with self.assertRaises(ValueError):self.api.materialize(path,'one')

    def test_existing_destination_is_never_overwritten(self):
        path,_=self.plan();destination=self.root/'test';destination.mkdir();(destination/'user.txt').write_text('keep')
        with self.assertRaises(FileExistsError):self.api.materialize(path,'one')
        self.assertEqual((destination/'user.txt').read_text(),'keep')

    def test_materialize_normalizes_owned_stage_before_readonly_verification_baseline(self):
        ir_path, plan = self.plan()
        stage = self.root / ".actuator" / "altium" / "staging" / "normalization-fixture"
        stage.mkdir(parents=True)
        project = stage / (plan["name"] + ".PrjPcb")
        sheet = stage / (plan["name"] + ".SchDoc")
        project.write_bytes(b"owned project")
        sheet.write_bytes(b"initial schematic")
        data = {"operation_id": "a" * 32, "payload": {"ir_path": str(ir_path), "ir_sha256": _hash(ir_path),
                "destination": str(self.root / "published")}}
        operation_path = stage / "operation.json"
        calls = []
        state = {"normalize_pending": True}

        def run(command, *, kind, request_key=None, payload=None):
            calls.append((kind, command, request_key, payload))
            if kind == "materialize_normalize":
                sheet.write_bytes(b"normalized schematic")
                project.write_bytes(b"native serialized project metadata")
                status = "unknown" if state["normalize_pending"] else "succeeded"
                state["normalize_pending"] = False
                return {"status": status, "operation_id": "b" * 32}
            if kind == "materialize_verify":
                return {"status": "unknown", "operation_id": "c" * 32}
            raise AssertionError((kind, command))

        with patch.object(self.api, "_stage", return_value=(Path(ir_path), plan, stage)), \
             patch.object(self.api, "_run", side_effect=run):
            first = self.api._finish_materialize(operation_path, data)
            self.assertEqual(first["status"], "verification_pending")
            self.assertFalse(any(row[0] == "materialize_verify" for row in calls))
            first_key = next(row[2] for row in calls if row[0] == "materialize_normalize")
            self.assertEqual(first_key, "a" * 32 + ":normalize")
            normalize_command = next(row[1] for row in calls if row[0] == "materialize_normalize")
            self.assertIn(str(project), normalize_command)
            self.assertIn(str(sheet), normalize_command)
            normalized_hash = _hash(sheet)
            normalized_project_hash = _hash(project)

            second = self.api._finish_materialize(operation_path, data)
            self.assertEqual(second["status"], "verification_pending")
            verify = next(row for row in calls if row[0] == "materialize_verify")
            self.assertEqual(verify[3]["baseline"]["schematic"], normalized_hash)
            self.assertEqual(verify[3]["baseline"]["project"], normalized_project_hash)
            self.assertEqual(verify[2], "a" * 32)
            self.assertEqual(len([row for row in calls if row[0] == "materialize_normalize"]), 2)
            self.assertFalse(Path(data["payload"]["destination"]).exists())


if __name__=='__main__':unittest.main()
