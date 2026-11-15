"""Explicit local-administrator controls; never expose signing material to HTML."""
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

MESSAGES = {
    'DEFAULT_SESSION_REQUIRED': 'Chọn phiên mặc định mà kết nối MCP đang dùng. Nếu chưa có, gọi một tool đọc trong kết nối Actuator rồi làm mới.',
    'BROWSER_TARGET_CHANGED': 'Tab đã đóng hoặc đổi URL. Đọc lại danh sách trước khi kết nối; chưa có thao tác tự chạy lại.',
    'BROWSER_BUSY': 'Phiên còn lệnh hoặc tab đang dùng. Kiểm tra và giải phóng tài nguyên trước khi đổi trình duyệt.',
    'SESSION_UNAVAILABLE': 'Phiên không còn hoạt động. Chọn lại phiên sau khi làm mới.',
    'CHECKPOINT_CONFLICT': 'Checkpoint đã thay đổi. Đọc lại tiến độ trước khi xác nhận.',
    'INSPECTION_REQUIRED': 'Cần kiểm tra kết quả trước và xác nhận đọc lại. Checkpoint thao tác sửa không được đặt lại ở đây.',
    'RESOURCE_LIMIT': 'Đã chạm giới hạn tài nguyên. Kiểm tra lệnh đang chạy và giải phóng tài nguyên của phiên.',
    'ACCESS_DENIED': 'Backend từ chối quyền hoặc phiên. Kiểm tra quyền và chọn lại phiên.',
    'INVALID_REQUEST': 'Tham số chưa hợp lệ. Kiểm tra URL, selector và cấu hình tác vụ.',
    'REFERENCE_UNAVAILABLE': 'Tham chiếu đã cũ. Đọc lại giao diện và lấy tham chiếu mới.',
    'DEADLINE_EXCEEDED': 'Đã hết thời gian chờ. Kiểm tra trạng thái trước khi gửi yêu cầu khác.',
    'CANCELLED': 'Lệnh đã bị hủy. Kiểm tra kết quả trước khi thao tác tiếp.',
    'NODE_BUSY': 'Actuator đang có tác vụ sử dụng tài nguyên. Chờ tác vụ hoàn tất rồi kiểm tra lại.',
    'BACKEND_UNAVAILABLE': 'Chưa kết nối được backend. Kiểm tra trạng thái kết nối và lệnh đã gửi.',
    'BACKEND_FAILURE': 'Backend chưa trả được kết quả hợp lệ. Kiểm tra trạng thái và nhật ký.',
}

class DashboardError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code,str) and code in MESSAGES else 'BACKEND_FAILURE'
        super().__init__(MESSAGES[self.code])
    def response(self):
        return {'error': str(self), 'code': self.code, 'retry_safe': False}

def decode_control_result(decoded):
    if not isinstance(decoded,dict):raise DashboardError('BACKEND_FAILURE')
    result=decoded.get('result',{})
    if not isinstance(result,dict):raise DashboardError('BACKEND_FAILURE')
    data=result.get('structuredContent')
    if not data:
        try:data=json.loads(result.get('content',[{}])[0].get('text','{}'))
        except (ValueError,IndexError,TypeError,AttributeError):data={}
    if decoded.get('error') or result.get('isError'):
        error=data.get('error',{}) if isinstance(data,dict) else {}
        if not isinstance(error,dict):error={}
        diagnostic=error.get('diagnostic',{})
        raise DashboardError(error.get('code') or (diagnostic.get('code') if isinstance(diagnostic,dict) else None))
    if not isinstance(data,dict) or not data:raise DashboardError('BACKEND_FAILURE')
    return data.get('result',data) if set(data)=={'result'} else data


class AutomationDashboard:
    def __init__(self, home, rpc=None):
        self.home = Path(home)
        self.rpc = rpc or self._rpc

    def sessions(self):
        path = self.home / 'config/work-sessions.sqlite3'
        with closing(sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True, timeout=2)) as db:
            rows = db.execute("SELECT id,owner FROM sessions WHERE status='active' ORDER BY created DESC LIMIT 32").fetchall()
        return [{'session_id': sid, 'owner': owner} for sid, owner in rows]

    def session(self, sid):
        return next((s for s in self.sessions() if s['session_id'] == sid), None)

    def _rpc(self, session, tool, args):
        if tool not in {'dom_operation_status', 'dom_operation_cancel', 'dom_operation_recover', 'dom_review_status', 'dom_review_run', 'dom_review_record', 'work_session_status', 'dom_browser_sessions', 'dom_browser_choices', 'dom_browser_attach', 'dom_browser_resume', 'dom_browser_remember', 'dom_browser_detach'}:
            raise ValueError('Local control tool is not allowed')
        import httpx
        from .update_health import decode_rpc
        gateway = self.home.parent / 'MCP-Funnel-Gateway'
        spec = importlib.util.spec_from_file_location('_actuator_local_identity', gateway / 'gateway_identity.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        context = module.identity_context.set({'owner': session['owner'], 'session': session['session_id'], 'scopes': ['mcp:read', 'mcp:write']})
        try:
            body, headers = module.sign_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': tool, 'arguments': args}}, gateway)
            approval_attach=tool=='dom_browser_attach' and isinstance(args.get('cdp_url'),str) and args['cdp_url'].startswith('ws://')
            with httpx.Client(timeout=185 if approval_attach else 35 if tool in {'dom_review_run','dom_browser_attach','dom_browser_resume','dom_browser_detach'} else 5, trust_env=False) as client:
                response = client.post('http://127.0.0.1:8090/mcp', content=body, headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', **headers})
                response.raise_for_status()
                decoded = decode_rpc(response)
            return decode_control_result(decoded)
        except httpx.TimeoutException:
            raise DashboardError('DEADLINE_EXCEEDED') from None
        except httpx.HTTPError:
            raise DashboardError('BACKEND_UNAVAILABLE') from None
        finally:
            module.identity_context.reset(context)

    def inspect(self, sid):
        session = self.session(sid)
        if session is None:
            raise DashboardError('SESSION_UNAVAILABLE')
        return self.rpc(session, 'dom_operation_status', {'include_finished': True})

    def control(self, sid, action, operation_id, acknowledge_close=False):
        if action not in ('cancel', 'recover') or not isinstance(operation_id, str) or not operation_id:
            raise ValueError('Choose an operation and cancel/recover')
        session = self.session(sid)
        if session is None:
            raise DashboardError('SESSION_UNAVAILABLE')
        operations = self.rpc(session, 'dom_operation_status', {})['operations']
        operation = next((o for o in operations if o['operation_id'] == operation_id), None)
        if operation is None:
            raise ValueError('Operation finished or belongs to another session; refresh')
        if action == 'recover' and (operation['state'] != 'cancelling' or acknowledge_close is not True):
            raise ValueError('Cancel first, then acknowledge that recovery may close managed tabs')
        return self.rpc(session, 'dom_operation_' + action, {'operation_id': operation_id})

    def reviews(self, sid, task=None):
        session = self.session(sid)
        if session is None:
            raise DashboardError('SESSION_UNAVAILABLE')
        if task is not None:
            return self.rpc(session, 'dom_review_status', {'task_key': task})
        path = self.home / 'config/dom-progress.sqlite3'
        if not path.is_file():
            return {'tasks': []}
        with closing(sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True, timeout=2)) as db:
            rows = db.execute('SELECT task,status,COUNT(*) FROM dom_progress WHERE owner=? GROUP BY task,status ORDER BY task LIMIT 1001', (session['owner'],)).fetchall()
        truncated=len(rows)>1000
        rows=rows[:1000] if not truncated else [row for row in rows[:1000] if row[0]!=rows[-1][0]]
        tasks = {}
        for name, status, count in rows:
            tasks.setdefault(name, {})[status] = count
        return {'tasks': [{'task_key': name, 'counts': counts} for name, counts in tasks.items()], 'truncated':truncated}

    def resume(self, sid, payload):
        session = self.session(sid)
        if session is None:
            raise DashboardError('SESSION_UNAVAILABLE')
        if set(payload) - {'task_key', 'urls', 'detail_selector', 'variant_selector'}:
            raise ValueError('Unexpected review parameters')
        # Explicit bounded navigation/read only. No checkpoint reset or action replay.
        return self.rpc(session, 'dom_review_run', {**payload, 'max_items': 3})

    def reset_checkpoint(self,sid,task,item,revision,acknowledge):
        session=self.session(sid)
        if session is None:raise DashboardError('SESSION_UNAVAILABLE')
        if acknowledge is not True or type(revision) is not int or revision<1:raise DashboardError('INSPECTION_REQUIRED')
        status=self.rpc(session,'dom_review_status',{'task_key':task})
        if not status.get('plan') or status.get('truncated'):raise DashboardError('INSPECTION_REQUIRED')
        checkpoint=next((r for r in status['items'] if r['item_key']==item),None)
        if checkpoint is None or checkpoint['revision']!=revision:raise DashboardError('CHECKPOINT_CONFLICT')
        if checkpoint['stage'] not in ('title','detail','variants') or checkpoint['status'] not in ('blocked','unknown'):raise DashboardError('INSPECTION_REQUIRED')
        return self.rpc(session,'dom_review_record',{'task_key':task,'item_key':item,'url':checkpoint['url'],'stage':checkpoint['stage'],'status':'pending','summary':'Explicitly inspected and marked for read-only review; no action replay','expected_revision':revision})


    def browser_default(self,sid):
        session=self.session(sid)
        if session is None:raise DashboardError('SESSION_UNAVAILABLE')
        status=self.rpc(session,'work_session_status',{})
        default=status.get('default_session_id')
        return {'default_session_id':default,'selected_is_default':default==sid,'owner':session['owner']}

    def browser_session(self,sid):
        session=self.session(sid)
        if session is None:raise DashboardError('SESSION_UNAVAILABLE')
        if not self.browser_default(sid)['selected_is_default']:raise DashboardError('DEFAULT_SESSION_REQUIRED')
        return session

    def browsers(self,sid,endpoints=None):
        session=self.session(sid)
        if session is None:raise DashboardError('SESSION_UNAVAILABLE')
        if endpoints is not None:
            from .dom_attachment import endpoint
            if not isinstance(endpoints,list) or not 1<=len(endpoints)<=8:raise ValueError('Provide 1..8 endpoints')
            endpoints=[endpoint(value) for value in endpoints]
        return self.rpc(session,'dom_browser_sessions',{'cdp_urls':endpoints})

    def browser_choices(self,sid):
        session=self.session(sid)
        if session is None:raise DashboardError('SESSION_UNAVAILABLE')
        return self.rpc(session,'dom_browser_choices',{})

    def browser_change(self,sid,action,payload):
        session=self.browser_session(sid)
        if action not in {'attach','resume','remember','detach'} or not isinstance(payload,dict):raise ValueError('Explicit browser action required')
        if action=='detach':
            if payload!={'acknowledge_close':True}:raise DashboardError('INSPECTION_REQUIRED')
            if self.rpc(session,'dom_operation_status',{})['operations']:raise DashboardError('BROWSER_BUSY')
            return self.rpc(session,'dom_browser_detach',{})
        if action=='resume':
            if set(payload)!={'name'}:raise ValueError('Choose a bookmark')
            return self.rpc(session,'dom_browser_resume',{'name':payload['name'],'require_default_session':True})
        required={'cdp_url','tab_id','expected_url'}|({'name','expected_revision'} if action=='remember' else set())
        if set(payload)!=required:raise ValueError('Exact browser selection required')
        from .dom_attachment import endpoint
        from .dom_progress import public_url
        url=endpoint(payload['cdp_url']);public_url(payload['expected_url'])
        inventory=self.rpc(session,'dom_browser_sessions',{'cdp_urls':[url]})
        matches=[t for item in inventory['sessions'] if item['cdp_url']==url for t in item.get('tabs',[]) if t['tab_id']==payload['tab_id'] and t['url']==payload['expected_url']]
        if len(matches)!=1:raise DashboardError('BROWSER_TARGET_CHANGED')
        if action=='remember':return self.rpc(session,'dom_browser_remember',payload)
        return self.rpc(session,'dom_browser_attach',{'cdp_url':url,'tab_id':payload['tab_id'],'tab_url':payload['expected_url'],'require_default_session':True})

from .automation_dashboard_ui import HTML
