"""Verify gateway identity against an owner-installed public key."""
import base64
import hashlib
import json
from pathlib import Path
import time
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from .tool_policy import Principal, principal_context, background_powershell_approved

def verify_identity(headers,body,public_path):
    raw_header=headers.get("x-actuator-identity")
    signature=headers.get("x-actuator-signature")
    if raw_header is None and signature is None: return None
    try:
        if not raw_header or not signature or len(raw_header)>4096: raise ValueError()
        decode=lambda s:base64.urlsafe_b64decode(s+"="*(-len(s)%4))
        raw=decode(raw_header)
        public=load_pem_public_key(Path(public_path).read_bytes())
        public.verify(decode(signature),raw)
        claims=json.loads(raw)
        if claims["body_sha256"]!=hashlib.sha256(body).hexdigest(): raise ValueError()
        if not time.time()-1 <= claims["expires"] <= time.time()+60: raise ValueError()
        if not isinstance(claims["owner"],str) or not isinstance(claims["session"],str): raise ValueError()
        if not isinstance(claims["scopes"],list): raise ValueError()
        return Principal(owner=claims["owner"],scopes=frozenset(claims["scopes"]),
                         full_access=False,session=claims["session"],
                         connection_kind='oauth_gateway' if claims.get('connection_kind')=='oauth_gateway' else 'signed_gateway')
    except Exception as exc:
        raise PermissionError("Invalid gateway identity") from exc

class GatewayIdentityMiddleware:
    def __init__(self,app,runtime):
        self.app=app
        self.runtime=runtime
    async def __call__(self,scope,receive,send):
        if scope["type"]!="http": return await self.app(scope,receive,send)
        headers={k.decode().lower():v.decode() for k,v in scope.get("headers",[])}
        if "x-actuator-identity" not in headers and "x-actuator-signature" not in headers:
            return await self.app(scope,receive,send)
        body=b""
        while True:
            message=await receive()
            body+=message.get("body",b"")
            if len(body)>1048576:
                await send({"type":"http.response.start","status":413,"headers":[]})
                return await send({"type":"http.response.body","body":b""})
            if not message.get("more_body"): break
        from dataclasses import replace
        try:
            public=self.runtime.config_path.parent/"gateway-signing-public.pem"
            principal=verify_identity(headers,body,public)
            policy=self.runtime.raw_config().get("security",{})
            principal=replace(principal,
                scopes=principal.scopes & frozenset(policy.get("scopes",["mcp:read","mcp:write","mcp:execute"])),
                full_access=policy.get("mode","project")=="full_access",
                background_powershell=background_powershell_approved(self.runtime))
        except PermissionError:
            await send({"type":"http.response.start","status":403,"headers":[]})
            return await send({"type":"http.response.body","body":b""})
        delivered=False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {"type":"http.request","body":body,"more_body":False}
            return await receive()
        from .evidence import signed_gateway_request
        channel_token=signed_gateway_request.set(True)
        token=principal_context.set(principal)
        try: return await self.app(scope,replay,send)
        finally:
            principal_context.reset(token)
            signed_gateway_request.reset(channel_token)
