"""One explicit variant action followed by observed selection/price/availability."""
import asyncio
import time
from .tools.dom_verification import DOMValidationError


async def choose(dom, tab, element, method, value, selected_selector, expected_text, price_selector=None, availability_selector=None):
    if method not in ('select', 'click', 'toggle'):
        raise DOMValidationError('Use select, click or toggle')
    if method == 'select' and (not isinstance(value, str) or not value):
        raise DOMValidationError('Select requires a nonempty option value')
    if method != 'select' and value is not None:
        raise DOMValidationError('Only select accepts an option value')
    if not isinstance(expected_text, str) or not expected_text.strip() or len(expected_text) > 500:
        raise DOMValidationError('Expected selected text required, up to 500 characters')
    selectors = {'selected': selected_selector, 'price': price_selector, 'availability': availability_selector}
    for name, selector in selectors.items():
        if name == 'selected' or selector is not None:
            if not isinstance(selector, str) or not selector.strip() or len(selector) > 1000:
                raise DOMValidationError('Bounded nonempty observation selector required')
    async with asyncio.timeout(25):
        tabs = await dom.list_tabs()
        if not any(t['tab_ref'] == tab for t in tabs['tabs']):
            raise LookupError('Owned tab unavailable')
        detail = await dom.details(element)
        if detail.get('tab_ref') != tab:
            raise DOMValidationError('Variant control must belong to the selected tab')
        async def read():
            output = {}
            for name, selector in selectors.items():
                if selector is None: continue
                found = await dom.find(tab, selector=selector, max_items=2)
                if len(found['matches']) > 1:
                    raise ValueError('Observation selector is ambiguous')
                output[name] = (await dom.read_text(found['matches'][0]['element_ref'], max_chars=1200)).get('text', '') if found['matches'] else None
            return output
        # Validate all selectors and ambiguity before dispatching any mutation.
        try:before = await read()
        except (ValueError,TypeError):raise DOMValidationError('Invalid or ambiguous observation selector') from None
        except Exception as exc:
            if type(exc).__name__=='Error' and ('selector' in str(exc).casefold() or 'unexpected token' in str(exc).casefold()):raise DOMValidationError('Invalid observation selector') from None
            raise
        if method == 'select': action = await dom.select(element, value=value)
        elif method == 'toggle': action = await dom.toggle(element, checked=True)
        else: action = await dom.click(element)
        after = None; stable = None; began = time.monotonic(); samples = 0; verified = False
        try:
            while time.monotonic() - began < 3:
                current = await read(); samples += 1
                if current == after and (current.get('selected') or '').strip() == expected_text.strip():
                    stable = stable or time.monotonic()
                    if time.monotonic() - stable >= .3:
                        verified = True; after = current; break
                else: stable = None
                after = current
                await asyncio.sleep(.1)
            observation_error = None
        except Exception as exc:
            observation_error = type(exc).__name__
        return {'action': action, 'before': before, 'after': after,
                'verification': {'state': 'verified' if verified else 'unconfirmed', 'samples': samples, 'observation_error': observation_error, 'business_outcome_verified': False},
                'availability_is_observed_text': True, 'retry_safe': False, 'automatic_replay': False}
