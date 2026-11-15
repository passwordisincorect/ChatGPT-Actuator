"""Opt-in recovery of an exact delegated browser target; no navigation or replay."""
import copy
import time
from urllib.parse import urlsplit

REFERENCE_KEYS = {'tab_ref', 'element_ref'}


def replace_arguments(value, bindings):
    if isinstance(value, list):
        return [replace_arguments(v, bindings) for v in value]
    if isinstance(value, dict):
        return {k: bindings.get(v, v) if k in REFERENCE_KEYS and isinstance(v, str)
                else replace_arguments(v, bindings) for k, v in value.items()}
    return value


def resolve_conditions(doc, conditions):
    # Evidence values and selectors are immutable. Only concrete argument refs move.
    return [{**c, 'arguments': replace_arguments(c['arguments'], doc.get('reference_bindings', {}))}
            for c in conditions]


def validate_recovery(value, task, principal, engine):
    from ..dom_attachment import endpoint
    from ..dom_progress import public_url
    fields = {'cdp_url', 'tab_id', 'expected_url', 'tab_ref', 'elements'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Recovery requires cdp_url, tab_id, expected_url, tab_ref and elements')
    endpoint(value['cdp_url'])
    public_url(value['expected_url'])
    for field, limit in (('tab_id', 128), ('tab_ref', 4096)):
        if not isinstance(value[field], str) or not 1 <= len(value[field]) <= limit:
            raise ValueError('Concrete browser target/reference required')
    host = urlsplit(value['expected_url']).hostname
    if not any(host == h or host.endswith('.' + h) for h in task['allowed_hosts']):
        raise PermissionError('Recovery target must belong to the delegated hosts')
    required = {'dom_browser_recover', 'dom_list_tabs'}
    if not isinstance(value['elements'], list) or len(value['elements']) > 8:
        raise ValueError('At most 8 explicit element recovery locators')
    refs = {value['tab_ref']}
    for element in value['elements']:
        if not isinstance(element, dict) or set(element) != {'element_ref', 'selector', 'expected_text'}:
            raise ValueError('Recovery elements require element_ref, selector and expected_text')
        for key, limit in (('element_ref', 4096), ('selector', 512), ('expected_text', 1000)):
            if not isinstance(element[key], str) or not 1 <= len(element[key]) <= limit:
                raise ValueError('Bounded explicit recovery locator required')
        if element['element_ref'] in refs:
            raise ValueError('Duplicate recovery reference')
        refs.add(element['element_ref'])
        required.update({'dom_find', 'dom_read_text'})
    if not required <= set(task['allowed_tools']):
        raise PermissionError('Recovery tools must be explicitly delegated')
    for tool in required:
        from ..tool_policy import authorize
        authorize(tool, principal)
    return copy.deepcopy(value)


async def recover(service, doc, principal):
    spec = doc['browser_recovery']
    if doc.get('recovery_session') == principal.session:
        return True
    if doc.get('recovery_count', 0) >= 3:
        raise RuntimeError('Browser recovery budget exhausted')

    async def call(tool, arguments):
        action = {'tool': tool, 'arguments': arguments}
        service._check_action(doc, action, principal)
        return await service.engine._call(action, principal, time.monotonic() + 10)

    inventory = await call('dom_list_tabs', {})
    tabs = inventory.get('tabs', [])
    matches = [t for t in tabs if t.get('url') == spec['expected_url'] and
               t.get('binding', {}).get('target_id') == spec['tab_id']]
    if len(matches) != 1:
        if tabs:
            raise RuntimeError('Delegated browser target changed; recovery cannot switch tabs')
        after = [{'tool': 'dom_list_tabs', 'arguments': {}, 'path': ['tabs', 0, 'url'],
                  'op': 'eq', 'value': spec['expected_url']},
                 {'tool': 'dom_list_tabs', 'arguments': {}, 'path': ['tabs', 0, 'binding', 'target_id'],
                  'op': 'eq', 'value': spec['tab_id']}]
        action = {'tool': 'dom_browser_recover', 'arguments': {k: spec[k]
                  for k in ('cdp_url', 'tab_id', 'expected_url')}}
        service._check_action(doc, action, principal)
        if not service._sync(doc):
            return False
        # The recovery itself uses durable dispatch and evidence, just like other
        # mutations. An interrupted attachment is reconciled before any new one.
        mission = service.engine.create(principal, 'Recover exact delegated browser target',
            [{'action': action, 'preconditions': [], 'postconditions': after}], after,
            'recovery-' + doc['id'] + '-' + str(doc.get('recovery_count', 0)) + '-' + principal.session)
        if mission['state'] == 'BLOCKED' and mission['steps'][0]['state'] == 'UNKNOWN':
            await service.engine.verify(principal, mission['id'], 0)
        elif mission['state'] == 'PENDING' and mission['steps'][0]['state'] != 'VERIFIED':
            await service.engine.execute(principal, mission['id'], mission['digest'], 0)
        mission = service.engine.status(principal, mission['id'])
        if mission['steps'][0]['state'] == 'VERIFIED' and mission['state'] == 'PENDING':
            mission = await service.engine.verify(principal, mission['id'])
        if mission['state'] != 'COMPLETED':
            raise RuntimeError('Browser recovery delivery needs inspection')
        inventory = await call('dom_list_tabs', {})
        matches = [t for t in inventory.get('tabs', []) if t.get('url') == spec['expected_url'] and
                   t.get('binding', {}).get('target_id') == spec['tab_id']]
        if len(matches) != 1:
            raise RuntimeError('Exact target unavailable after recovery')
    fresh = matches[0]['tab_ref']
    bindings = {spec['tab_ref']: fresh}
    for element in spec['elements']:
        if not service._sync(doc):
            return False
        found = await call('dom_find', {'tab_ref': fresh, 'selector': element['selector'], 'max_items': 2})
        if found.get('count') != 1 or len(found.get('matches', [])) != 1:
            raise RuntimeError('Recovery selector is missing or ambiguous')
        ref = found['matches'][0].get('element_ref')
        if not isinstance(ref, str) or not ref:
            raise RuntimeError('Recovery element is unavailable')
        text = await call('dom_read_text', {'element_ref': ref, 'max_chars': 1001})
        if text.get('text') != element['expected_text'] or text.get('truncated'):
            raise RuntimeError('Recovery element identity text changed')
        bindings[element['element_ref']] = ref
    previous = doc.get('reference_bindings', {})
    if not service._sync(doc):
        return False
    if doc.get('mission_id'):
        mission = service.engine.status(principal, doc['mission_id'])
        if mission['state'] not in {'COMPLETED', 'CANCELLED', 'FAILED'}:
            effective = {previous.get(old, old): new for old, new in bindings.items()
                         if previous.get(old, old) != new}
            # Pass only keys actually present in pending action/evidence arguments.
            arguments = [c['arguments'] for c in mission['criteria']]
            for step in mission['steps']:
                if step['state'] != 'VERIFIED':
                    arguments.append(step['action']['arguments'])
                    arguments.extend(c['arguments'] for c in step['preconditions'] + step['postconditions'])
            def referenced(value, ref):
                if isinstance(value, list):
                    return any(referenced(v, ref) for v in value)
                if isinstance(value, dict):
                    return any((k in REFERENCE_KEYS and v == ref) or referenced(v, ref) for k, v in value.items())
                return False
            effective = {old: new for old, new in effective.items() if referenced(arguments, old)}
            if effective:
                service.engine.control(principal, mission['id'], 'rebind', effective)
    doc['reference_bindings'] = bindings
    # Replan only when no durable action is pending. UNKNOWN actions stay UNKNOWN.
    if not doc.get('mission_id'):
        doc['decision'] = None
    doc['recovery_session'] = principal.session
    doc['recovery_count'] = doc.get('recovery_count', 0) + 1
    service._remember(doc, 'browser_recovery', {'tab_ref': fresh,
        'reference_bindings': bindings, 'actions_replayed': 0, 'navigation_performed': False})
    service._save(doc, 'browser_recovered')
    return True
