import asyncio
class PopupTracker:
    def __init__(self,context):
        self.context=context;self.pages=[];self.event=asyncio.Event();context.on('page',self.arrived)
    def arrived(self,page):self.pages.append(page);self.event.set()
    async def wait(self,timeout_ms):
        if timeout_ms and not self.pages:
            try:await asyncio.wait_for(self.event.wait(),timeout=timeout_ms/1000)
            except asyncio.TimeoutError:pass
    def close(self):self.context.remove_listener('page',self.arrived)
