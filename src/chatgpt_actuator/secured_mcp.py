"""MCP registration and invocation use the same permission manifest."""
import inspect
import time
import secrets
from threading import RLock
from dataclasses import replace
from .work_sessions import WorkSessions
from .control_plane_guard import protect_control_plane
from functools import wraps
from mcp.server.mcpserver import MCPServer
from .tool_policy import authorize, current_principal, principal_context, scope_for, BACKGROUND_BLOCKED_TOOLS

class SecuredMCPServer(MCPServer):
    def __init__(self, *args, runtime, **kwargs):
        self.runtime = runtime
        self.sessions = None
        self._session_lock = RLock()
        super().__init__(*args, **kwargs)

    def run(self, *args, **kwargs):
        service = None
        factory = getattr(self, 'agent_background_factory', None)
        policy = self.runtime.raw_config()
        if factory and policy.get('agents', {}).get('enabled') is True and policy.get('missions', {}).get('enabled') is True:
            service = factory()
            service.start()
        try:
            return super().run(*args, **kwargs)
        finally:
            if service:
                service.stop()

    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)
        def decorator(fn):
            name = kwargs.get("name") or fn.__name__
            from .tool_contracts import DESCRIPTIONS
            if not fn.__doc__:fn.__doc__=DESCRIPTIONS.get(name, str(kwargs.get('title') or name)+'. Requires '+scope_for(name)+' and existing local policy.')
            scope_for(name)  # refuse unreviewed tools at registration
            if name in BACKGROUND_BLOCKED_TOOLS:
                raise PermissionError("BACKGROUND_ONLY: forbidden tool registration")
            signature = inspect.signature(fn)
            def enter(a, kw):
                principal = current_principal(self.runtime)
                try:
                    bound = signature.bind(*a, **kw)
                    authorize(name, principal, bound.arguments)
                    protect_control_plane(self.runtime, name, principal, bound.arguments)
                    sid = None
                    parameters = dict(bound.arguments)
                    operation = None
                    if hasattr(self.runtime, "config_path"):
                        with self._session_lock:
                            if self.sessions is None:
                                self.sessions = WorkSessions(self.runtime.config_path.parent / "work-sessions.sqlite3",
                                    self.runtime.raw_config().get("security",{}).get("resource_limits"))
                        sid, parameters = self.sessions.prepare(name, parameters, principal)
                        recovery={'dom_operation_status','dom_operation_cancel','dom_operation_recover','dom_review_status','dom_review_record','dom_browser_choices'}
                        recovery_batch=name in {'batch_execute','batch_readonly'} and parameters.get('actions') and all(action.get('tool') in recovery for action in parameters['actions'])
                        operation = None if recovery_batch else self.sessions.acquire_operation(sid, name)
                        principal = replace(principal, session=sid)
                    try:
                        job = self.sessions.job_start(sid, name) if sid else None
                    except BaseException:
                        if operation: self.sessions.release_operation(operation)
                        raise
                    return principal_context.set(principal), sid, parameters, job, operation
                except Exception as exc:
                    self.runtime.audit.record("tool_denied", tool=name, owner=principal.owner,
                                              session=principal.session, error_type=type(exc).__name__)
                    raise
            def finish(token, job, operation, status, began, request_id):
                principal = principal_context.get()
                try:
                    if job: self.sessions.job_finish(job, status)
                    if status=='completed' and hasattr(self.runtime,'config_path'):
                        from .evidence import Evidence,signed_gateway_request
                        from . import __version__
                        import os
                        import sqlite3
                        from .evidence import transport_category
                        dispatch_source=(('oauth_gateway' if principal.connection_kind=='oauth_gateway' else 'signed_gateway') if signed_gateway_request.get() else
                            'configured_stdio' if os.getenv('ACTUATOR_MCP_TRANSPORT')=='stdio' else 'local_or_unidentified')
                        try:Evidence(self.runtime.config_path.parent/'runtime-evidence.sqlite3').record_dispatch(
                            principal.owner,principal.session,__version__,name,dispatch_source)
                        except (OSError,sqlite3.Error):pass
                        category=transport_category(signed_gateway_request.get(),principal.owner,
                            os.getenv('ACTUATOR_MCP_TRANSPORT'),os.getenv('ACTUATOR_CONNECTION_KIND'),principal.connection_kind)
                        if category:
                            try:Evidence(self.runtime.config_path.parent/'runtime-evidence.sqlite3').record(category,__version__)
                            except (OSError,sqlite3.Error):pass
                    self.runtime.audit.record("tool_outcome", tool=name, owner=principal.owner,
                        session=principal.session, status=status, request_id=request_id,
                        duration_ms=round((time.monotonic()-began)*1000,3))
                finally:
                    if hasattr(self.runtime,'config_path'):
                        try:
                            from .automation_health import export
                            export(self.runtime)
                        except Exception:
                            pass  # Diagnostics must never bypass lease/context cleanup.
                    try:
                        if operation: self.sessions.release_operation(operation)
                    finally: principal_context.reset(token)
            if inspect.iscoroutinefunction(fn):
                @wraps(fn)
                async def checked(*a, **kw):
                    token, sid, parameters, job, operation = enter(a, kw)
                    began, request_id = time.monotonic(), secrets.token_urlsafe(12)
                    status = "failed"
                    try:
                        result = await fn(**parameters)
                        from .automation_contract import result as automation_result
                        result=automation_result(name,result)
                        output = self.sessions.output(name, result, sid) if sid else result
                        status = ("timed_out" if isinstance(result, dict) and result.get("timed_out")
                                  else "cancelled" if isinstance(result,dict) and result.get("cancelled")
                                  else "failed" if isinstance(result,dict) and result.get("exit_code",0) not in (0,None)
                                  else "completed")
                        return output
                    finally:
                        finish(token, job, operation, status, began, request_id)
            else:
                @wraps(fn)
                def checked(*a, **kw):
                    token, sid, parameters, job, operation = enter(a, kw)
                    began, request_id = time.monotonic(), secrets.token_urlsafe(12)
                    status = "failed"
                    try:
                        result = fn(**parameters)
                        from .automation_contract import result as automation_result
                        result=automation_result(name,result)
                        output = self.sessions.output(name, result, sid) if sid else result
                        status = ("timed_out" if isinstance(result, dict) and result.get("timed_out")
                                  else "cancelled" if isinstance(result,dict) and result.get("cancelled")
                                  else "failed" if isinstance(result,dict) and result.get("exit_code",0) not in (0,None)
                                  else "completed")
                        return output
                    finally:
                        finish(token, job, operation, status, began, request_id)
            def preflight(parameters):
                principal=current_principal(self.runtime)
                authorize(name,principal,parameters)
                protect_control_plane(self.runtime,name,principal,parameters)
                if self.sessions and principal.session!='default':
                    from .session_resources import validate_refs
                    validate_refs(self.sessions,principal.session,name,parameters)
                if name in {'dom_operation_status','dom_operation_cancel','dom_operation_recover','dom_review_status','dom_review_record','dom_browser_choices'}:return
                if name.startswith('dom_') and isinstance(parameters.get('url'),str):
                    self.runtime.get_service('dom')._require_allowed_url(parameters['url'])
                if name.startswith('dom_'):
                    service=self.runtime.get_service('dom')
                    flag={'dom_open_tab':'allow_navigation','dom_navigate':'allow_navigation','dom_close_tab':'allow_navigation','dom_click':'allow_click','dom_set_value':'allow_set_value','dom_select':'allow_select','dom_toggle':'allow_toggle','dom_scroll':'allow_scroll','dom_scroll_into_view':'allow_scroll'}.get(name)
                    if flag:service._require_mutation(flag,'Batch DOM policy')
                    else:service._require_read()
            checked._actuator_preflight=preflight
            register(checked)
            return checked
        return decorator

    async def list_tools(self):
        principal = current_principal(self.runtime)
        tools = await super().list_tools()
        allowed = []
        for tool in tools:
            try:
                authorize(tool.name, principal)
            except PermissionError:
                continue
            allowed.append(tool)
        return allowed

    def streamable_http_app(self, **kwargs):
        from .gateway_identity import GatewayIdentityMiddleware
        app = super().streamable_http_app(**kwargs)
        app.add_middleware(GatewayIdentityMiddleware, runtime=self.runtime)
        return app

    async def call_tool(self, name, arguments, context=None):
        principal = current_principal(self.runtime)
        if context is not None:
            params = getattr(context, "_input_params", None)
            meta = getattr(params, "meta", None)
            if meta is not None:
                data = meta.model_dump() if hasattr(meta, "model_dump") else meta
                if isinstance(data, dict) and "work_session" in data:
                    session = data["work_session"]
                    if not isinstance(session, str) or len(session)>128:
                        raise ValueError("Invalid work_session")
                    principal = replace(principal, session=session)
        try:
            authorize(name,principal,arguments)
        except Exception as exc:
            self.runtime.audit.record("tool_denied",tool=name,owner=principal.owner,
                session=principal.session,error_type=type(exc).__name__)
            from .execution_errors import diagnostic
            from mcp.types import CallToolResult,TextContent
            import json
            detail=diagnostic(exc,name)
            return CallToolResult(isError=True,content=[TextContent(type='text',text=json.dumps({'error':detail}))],structuredContent={'error':detail},_meta={'actuator/contract':{'schema_version':1,'tool':name,'outcome':'error','provider_outcome_verified':False}})
        token = principal_context.set(principal)
        try:
            try:
                result = await super().call_tool(name, arguments, context)
                data=getattr(result,'structured_content',None)
                if data is None:
                    import json
                    content=getattr(result,'content',[])
                    if len(content)==1 and getattr(content[0],'type',None)=='text' and len(content[0].text)<=1048576:
                        try:data=json.loads(content[0].text)
                        except (ValueError,TypeError):pass
                if isinstance(data,dict):
                    body=data.get('result') if set(data)=={'result'} else data
                    if isinstance(body,dict):
                        verification=body.get('verification')
                        mismatch=isinstance(verification,dict) and verification.get('state')=='mismatch'
                        code=('DEADLINE_EXCEEDED' if body.get('timed_out') else
                              'CANCELLED' if body.get('cancelled') else
                              'PROCESS_EXIT_FAILURE' if body.get('exit_code',0) not in (0,None) else
                              'VERIFICATION_MISMATCH' if mismatch else
                              'PROVIDER_BLOCKED' if body.get('status')=='blocked' else
                              'OPERATION_FAILED' if body.get('ok') is False else
                              'VALIDATION_FAILED' if body.get('ready_to_freeze') is False else None)
                        if code:
                            from mcp.types import TextContent
                            import json
                            detail={'schema_version':1,'code':code,'tool':name,'delivery_state':'unknown',
                                    'retry_safe':False,'next_action':'Inspect the returned outcome before another action',
                                    'message':code.replace('_',' ').capitalize()}
                            if isinstance(body.get('preflight'),dict) and body['preflight'].get('state')=='rejected':
                                detail={**body['preflight']['error'],'tool':name,'delivery_state':'not_dispatched','retry_safe':False}
                            result.is_error=True
                            result.structured_content={**data,'error':detail}
                            result.content=[TextContent(type='text',text=json.dumps(result.structured_content,ensure_ascii=False))]
                from .tool_contracts import attach
                return attach(result,data,name)
            except Exception as exc:
                from .execution_errors import diagnostic
                from mcp.types import CallToolResult,TextContent
                import json
                detail=diagnostic(exc,name)
                return CallToolResult(isError=True,content=[TextContent(type='text',text=json.dumps({'error':detail}))],structuredContent={'error':detail},_meta={'actuator/contract':{'schema_version':1,'tool':name,'outcome':'error','provider_outcome_verified':False}})
        finally:
            principal_context.reset(token)
