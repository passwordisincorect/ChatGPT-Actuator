"""DOM references pin node identity; no selector fallback after detach/navigation."""
import asyncio
import time


class PinnedNode:
    def __init__(self, handle):
        self.handle = handle

    def __getattr__(self, name):
        return getattr(self.handle, name)

    async def wait_for(self, state='visible', timeout=5000):
        deadline = time.monotonic() + timeout / 1000
        while True:
            try:
                attached = await self.handle.evaluate('(el) => el.isConnected')
                visible = attached and await self.handle.is_visible()
            except Exception:
                attached = visible = False
            if {'attached':attached, 'detached':not attached,
                'visible':visible, 'hidden':not visible}[state]:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('Pinned DOM node did not reach requested state')
            await asyncio.sleep(.05)
