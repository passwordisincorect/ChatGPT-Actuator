"""Chromium-computed accessibility semantics; never export AX value/properties."""
import asyncio
from collections import deque


async def capture_accessibility(service, page, limit, depth_limit, include_frames):
    session = await page.context.new_cdp_session(page)
    nodes, documents, reasons = [], [], set()
    scanned = text_chars = 0
    text_limit = min(service.config.max_text_chars, 32768)
    try:
        await session.send('Accessibility.enable')
        tree = (await session.send('Page.getFrameTree'))['frameTree']
        frames = deque([tree])
        ordinal = 0
        while frames and ordinal < 16 and scanned < limit:
            entry = frames.popleft()
            info = entry['frame']
            if include_frames: frames.extend(entry.get('childFrames', []))
            frame_index = ordinal
            ordinal += 1
            if not service._is_allowed_url(str(info.get('url', ''))):
                documents.append({'frame_index':frame_index,'state':'blocked_host'})
                continue
            try:
                root = (await session.send('Accessibility.getRootAXNode', {'frameId':info['id']}))['node']
            except Exception:
                documents.append({'frame_index':frame_index,'state':'unavailable'})
                reasons.add('frame_unavailable')
                continue
            documents.append({'frame_index':frame_index,'state':'read'})
            queue = deque([(root, 0, None)])
            seen = set()
            while queue and scanned < limit:
                node, depth, parent = queue.popleft()
                key = node['nodeId']
                if key in seen: continue
                seen.add(key); scanned += 1
                index = parent
                if not node.get('ignored',False):
                    name = str(node.get('name',{}).get('value',''))
                    clipped = name[:max(0,min(512,text_limit-text_chars))]
                    if len(clipped)<len(name): reasons.add('text_limit')
                    text_chars += len(clipped)
                    index = len(nodes)
                    nodes.append({'index':index,'parent_index':parent,'depth':depth,
                        'frame_index':frame_index,'role':str(node.get('role',{}).get('value','')),
                        'name':clipped})
                if not node.get('childIds'): continue
                if depth >= depth_limit:
                    reasons.add('depth_limit'); continue
                # Never cross into an embedded document through another frame's tree.
                if str(node.get('role',{}).get('value','')).casefold() in {'iframe','iframepresentational'}:
                    continue
                try:
                    children = (await session.send('Accessibility.getChildAXNodes',
                        {'id':key,'frameId':info['id']}))['nodes']
                except Exception:
                    reasons.add('provider_error'); continue
                remaining = limit-scanned-len(queue)
                if len(children)>remaining: reasons.add('scan_limit')
                for child in children[:max(0,remaining)]:
                    if child.get('frameId') and child['frameId'] != info['id']: continue
                    queue.append((child,depth+1,index))
            if queue: reasons.add('scan_limit')
        if frames:
            reasons.add('frame_limit' if ordinal>=16 else 'scan_limit')
        service._require_allowed_url(str(page.url or ''))
        current = deque([(await session.send('Page.getFrameTree'))['frameTree']])
        urls = {}
        while current:
            entry = current.popleft()
            urls[entry['frame']['id']] = entry['frame'].get('url','')
            current.extend(entry.get('childFrames',[]))
        initial = deque([tree]); initial_ordinal = 0
        while initial and initial_ordinal < 16:
            entry = initial.popleft()
            if include_frames: initial.extend(entry.get('childFrames',[]))
            if any(d['frame_index']==initial_ordinal and d['state']=='read' for d in documents):
                identifier = entry['frame']['id']
                if identifier not in urls: raise LookupError('Accessibility frame detached during capture')
                service._require_allowed_url(str(urls[identifier]))
            initial_ordinal += 1
        return {'backend':'chromium_accessibility','nodes':nodes,'returned':len(nodes),
            'scanned':scanned,'documents':documents,'values_included':False,
            'truncated':bool(reasons),'truncation_reasons':sorted(reasons),
            'limits':{'max_scanned':limit,'max_depth':depth_limit,'max_frames':16}}
    finally:
        try: await asyncio.wait_for(session.detach(), timeout=1)
        except Exception: pass


async def bounded_capture(service, page, limit, depth, include_frames):
    return await asyncio.wait_for(capture_accessibility(service,page,limit,depth,include_frames),
                                  timeout=min(service.config.max_timeout_ms/1000, 15))
