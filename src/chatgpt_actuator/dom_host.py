from __future__ import annotations

import asyncio
from concurrent.futures import Future
import inspect
from threading import Event, Thread


class DOMAsyncProxy:
    def __init__(self, host: "DOMExecutionHost") -> None:
        self._host = host

    def __getattr__(self, name: str):
        async def caller(*args, **kwargs):
            return await self._host.acall(name, *args, **kwargs)
        return caller


class DOMExecutionHost:
    """Own one asyncio loop for all BrowserDOMService operations."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._ready = Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self._thread = Thread(
            target=runner,
            name="ChatGPT-Actuator-DOM",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("DOM execution loop failed to start.")

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        self.start()
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("DOM execution loop is not running.")
        return loop

    async def _invoke(self, method: str, *args, **kwargs):
        service = self.runtime.get_service("dom")
        target = getattr(service, method)
        result = target(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def submit(self, method: str, *args, **kwargs) -> Future:
        return asyncio.run_coroutine_threadsafe(
            self._invoke(method, *args, **kwargs),
            self._require_loop(),
        )

    async def acall(self, method: str, *args, **kwargs):
        return await asyncio.wrap_future(self.submit(method, *args, **kwargs))

    def call(self, method: str, *args, timeout: float = 40.0, **kwargs):
        return self.submit(method, *args, **kwargs).result(timeout=timeout)

    def stop(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None:
            return
        try:
            self.call("disconnect", timeout=5.0)
        except Exception:
            pass
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)
        self._loop = None
        self._thread = None
        self._ready.clear()
