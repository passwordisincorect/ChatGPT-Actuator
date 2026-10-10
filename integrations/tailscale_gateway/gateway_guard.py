"""Fail-closed perimeter for the PUBLIC MCP gateway.

Independent defense in depth: enforces read-only tool boundary before application
dispatch. Neither grants privileges nor exposes any backend directly.
Stores only short-lived in-memory counters; never logs tokens or payloads.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from typing import Callable

READONLY_TOOLS = frozenset({
    "routing_policy_info", "routing_decide", "system_info",
    "system_resource_snapshot", "powershell_info", "eda_info",
    "orch_info", "orch_project_list", "orch_task_list", "orch_task_get",
    "orch_chat_list", "orch_chat_status", "orch_chat_job_list",
    "orch_chat_job_status", "orch_task_review_status",
})
BODY_LIMITS = {
    "/mcp": 1024 * 1024,
    "/orch/mcp": 1024 * 1024,
    "/full/mcp": 1024 * 1024,
    "/oauth/register": 8192,
    "/oauth/token": 8192,
}
ORCH_READONLY_TOOLS = frozenset({
    "orch_info", "orch_project_list", "orch_task_list", "orch_task_get",
    "orch_chat_list", "orch_chat_status", "orch_chat_job_list",
    "orch_chat_job_status", "orch_task_review_status",
})
ORCH_MORE_READONLY_TOOLS = frozenset({
    "orch_project_get", "orch_chat_read",
})
ORCH_WRITE_TOOLS = frozenset({
    "orch_project_create", "orch_project_update", "orch_project_attach_task",
    "orch_task_create", "orch_task_plan", "orch_chat_create",
})
ORCH_EXECUTE_TOOLS = frozenset({
    "orch_task_auto_start", "orch_task_auto_advance", "orch_task_recover",
    "orch_task_cancel", "orch_task_collect", "orch_task_review_submit",
    "orch_task_finalize", "orch_chat_send", "orch_chat_submit",
    "orch_chat_cancel", "orch_chat_retry", "orch_chat_close",
})
ORCH_ALL_TOOLS = (ORCH_READONLY_TOOLS | ORCH_MORE_READONLY_TOOLS
                  | ORCH_WRITE_TOOLS | ORCH_EXECUTE_TOOLS)
assert len(ORCH_ALL_TOOLS) == 29
WINDOW_SECONDS = 60
MAX_TOTAL_BUCKETS = 4096


class RequestGuard:
    def __init__(self, app, clock: Callable[[], float] = time.monotonic):
        self.app = app
        self.clock = clock
        self.buckets: dict[str, deque[float]] = {}
        self.concurrency = asyncio.Semaphore(16)

    def check_bucket(self, key: str, limit: int) -> bool:
        now = self.clock()
        if len(self.buckets) >= MAX_TOTAL_BUCKETS and key not in self.buckets:
            self.buckets = {
                k: v for k, v in self.buckets.items()
                if v and v[-1] > now - WINDOW_SECONDS
            }
            if len(self.buckets) >= MAX_TOTAL_BUCKETS:
                return False
        bucket = self.buckets.setdefault(key, deque())
        cutoff = now - WINDOW_SECONDS
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        headers = {}
        for k, v in scope.get("headers", []):
            headers.setdefault(k.lower(), []).append(v)
        auth = headers.get(b"authorization", [])
        authenticated = len(auth) == 1 and auth[0].startswith(b"Bearer ")
        is_mcp = path in ("/mcp", "/orch/mcp", "/full/mcp")

        async def reject(status: int, reason: bytes = b"", retry: bool = False):
            extra = [(b"cache-control", b"no-store")]
            if retry:
                extra.append((b"retry-after", b"60"))
            await send({"type": "http.response.start", "status": status,
                        "headers": extra + [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": reason})

        # Reject ambiguous field lines before parsing or proxying a request.
        singleton = (b"authorization", b"mcp-protocol-version", b"mcp-method",
                     b"mcp-name", b"content-type", b"content-length", b"origin")
        if any(len(headers.get(key, ())) > 1 for key in singleton):
            return await reject(400, b"Duplicate security header")

        if len(scope.get("query_string", b"")) > 4096:
            return await reject(414, b"Request target too long")
        if len(scope.get("headers", [])) > 48:
            return await reject(431, b"Too many request headers")
        if is_mcp:
            if not self.check_bucket("mcp-all", 500):
                return await reject(429, b"Gateway request limit reached", True)
            if authenticated:
                # In-memory only. Hashing here avoids keeping plaintext credentials as dictionary keys.
                fingerprint = hashlib.sha256(auth[0]).hexdigest()
                if not self.check_bucket("mcp-token-" + fingerprint, 120):
                    return await reject(429, b"Per-session request limit reached", True)
            elif not self.check_bucket("mcp-guest", 75):
                return await reject(429, b"Anonymous request limit reached", True)
        elif path == "/oauth/register":
            if not self.check_bucket("dcr-global", 12):
                return await reject(429, b"Registration rate limit", True)
        elif path == "/oauth/token":
            if not self.check_bucket("token-global", 90):
                return await reject(429, b"Token request limit", True)
        elif path == "/oauth/authorize":
            if not self.check_bucket("authorize-global", 90):
                return await reject(429, b"Authorization request limit", True)

        if path in BODY_LIMITS and scope.get("method") == "POST":
            max_bytes = BODY_LIMITS[path]
            content_lengths = headers.get(b"content-length", [])
            if len(content_lengths) > 1:
                return await reject(400, b"Duplicate content length")
            if content_lengths:
                try:
                    claimed_size = int(content_lengths[0])
                except ValueError:
                    return await reject(400, b"Invalid content length")
                if claimed_size < 0 or claimed_size > max_bytes:
                    return await reject(413, b"Payload too large")
            # Pre-buffer with strict cap, including chunked/no-content-length requests.
            chunks = []
            size = 0
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                if event["type"] != "http.request":
                    continue
                data = event.get("body", b"")
                size += len(data)
                if size > max_bytes:
                    return await reject(413, b"Payload too large")
                chunks.append(data)
                if not event.get("more_body", False):
                    break
            body = b"".join(chunks)
            if is_mcp:
                try:
                    rpc = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    return await reject(400, b"Invalid JSON")
                # Fail closed on batch operations and unapproved tools even if
                # a future gateway patch erroneously expands its internal allowlist.
                if not isinstance(rpc, dict):
                    return await reject(403, b"Batch requests are not allowed")
                if rpc.get("method") == "tools/call":
                    params = rpc.get("params")
                    name = params.get("name") if isinstance(params, dict) else None
                    allowed = ORCH_ALL_TOOLS if path == "/orch/mcp" else READONLY_TOOLS
                    if path != "/full/mcp" and name not in allowed:
                        return await reject(403, b"Tool not permitted on public endpoint")
                    if path == "/orch/mcp" and name in ORCH_EXECUTE_TOOLS:
                        if not self.check_bucket("orch-execute-all", 24):
                            return await reject(429, b"Orchestrator execute rate limit", True)

            delivered = False

            async def replay_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}
            receive = replay_receive

        if not is_mcp:
            return await self.app(scope, receive, send)

        # Bounded concurrent backend dispatch.
        try:
            await asyncio.wait_for(self.concurrency.acquire(), timeout=0.5)
        except asyncio.TimeoutError:
            return await reject(429, b"Gateway busy", True)
        try:
            return await self.app(scope, receive, send)
        finally:
            self.concurrency.release()
