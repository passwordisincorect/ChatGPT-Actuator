"""Bounded observations; a completed click alone never proves a business outcome."""
import asyncio
import time

class DOMValidationError(ValueError):
    """Invalid action condition detected before dispatch."""

async def validate_click(page, expected_selector, verification):
    config = options(verification)
    selector = config.get('selector') or expected_selector
    if selector:
        try:
            # Parse through Playwright; a postcondition need not exist yet.
            await page.locator(selector).count()
        except Exception:
            raise DOMValidationError('Invalid or unavailable verification selector') from None

def options(value):
    if value is None:return {'stable_ms':300,'timeout_ms':1000,'wait_for_match':False}
    if not isinstance(value,dict) or set(value)-{'stable_ms','timeout_ms','wait_for_match','selector','state','text_contains','observe_ms','url_equals','url_contains'}:
        raise ValueError('Invalid verification options')
    result={'stable_ms':300,'timeout_ms':1000,'wait_for_match':False,**value}
    if type(result['stable_ms']) is not int or not 50<=result['stable_ms']<=3000:raise ValueError('stable_ms requires 50..3000')
    if type(result['timeout_ms']) is not int or not result['stable_ms']<result['timeout_ms']<=10000:raise ValueError('timeout_ms must exceed stable_ms and be <=10000')
    if type(result['wait_for_match']) is not bool:raise ValueError('wait_for_match must be boolean')
    if type(result.get('observe_ms',0)) is not int or not 0<=result.get('observe_ms',0)<=8000 or result.get('observe_ms',0)>=result['timeout_ms']:raise ValueError('observe_ms must be nonnegative and below timeout_ms')
    if 'selector' in result and (not isinstance(result['selector'],str) or not 1<=len(result['selector'])<=512):raise ValueError('Invalid verification selector')
    if result.get('state','visible') not in {'visible','hidden','attached','detached'}:raise ValueError('Invalid selector state')
    if 'state' in result and 'selector' not in result:raise DOMValidationError('state requires selector')
    if 'text_contains' in result and (not isinstance(result['text_contains'],str) or not 1<=len(result['text_contains'])<=512 or 'selector' not in result):raise ValueError('text_contains requires selector and 1..512 characters')
    for key in ('url_equals','url_contains'):
        if key in result and (not isinstance(result[key],str) or not 1<=len(result[key])<=4096):raise DOMValidationError('Bounded URL condition required')
    return result

async def stable(check, condition, verification=None):
    """Observe once dispatched; never retry the mutation. No observed values returned."""
    config=options(verification)
    async def observe():
        began=time.monotonic(); samples=0; matched_since=None
        while True:
            matches=await check(); samples+=1
            now=time.monotonic();elapsed=now-began
            matched_since=(now if matched_since is None else matched_since) if matches else None
            verified=matches and (now-matched_since)*1000>=config['stable_ms'] and elapsed*1000>=config.get('observe_ms',0)
            if verified or (not matches and not config['wait_for_match']):
                return {'state':'verified' if verified else 'mismatch','condition':condition,
                        'stable_window_ms':config['stable_ms'],'observed_ms':round(elapsed*1000),
                        'samples':samples,'stable':bool(verified),
                        'values_included':False,'retry_safe':False,'business_outcome_verified':False}
            await asyncio.sleep(.05)
    try:return await asyncio.wait_for(observe(),timeout=config['timeout_ms']/1000)
    except Exception:return {'state':'unconfirmed','condition':condition,'stable':False,
                            'values_included':False,'retry_safe':False,'business_outcome_verified':False}
async def state(element):
    try:
        return await element.evaluate("el => ({connected:el.isConnected,checked:typeof el.checked==='boolean'?el.checked:null,expanded:el.getAttribute('aria-expanded'),selected:typeof el.selectedIndex==='number'?el.selectedIndex:null})")
    except Exception:return None

async def click_result(page,element,before,before_url,expected_selector=None,allowed=None,verification=None,condition_scope=None):
    try:
        scope=condition_scope or page
        if allowed is not None and not allowed(page.url):
            return {'state':'unconfirmed','retry_safe':False,'business_outcome_verified':False}
        config=options(verification)
        if 'url_equals' in config or 'url_contains' in config:
            async def check_url():
                if allowed is not None and not allowed(page.url):return False
                if 'url_equals' in config and page.url!=config['url_equals']:return False
                if 'url_contains' in config and config['url_contains'] not in page.url:return False
                if 'selector' in config:
                    if allowed is not None and not allowed(scope.url):return False
                    node=scope.locator(config['selector']).first;count=await node.count();mode=config.get('state','visible')
                    matches=count>0 if mode=='attached' else count==0 if mode=='detached' else await node.is_visible() if mode=='visible' else not await node.is_visible()
                    if not matches:return False
                    if 'text_contains' in config and config['text_contains'] not in await node.inner_text(timeout=500):return False
                return True
            return await stable(check_url,'URL_and_optional_selector',config)
        if config.get('selector'):
            async def check_condition():
                if allowed is not None and (not allowed(page.url) or not allowed(scope.url)):return False
                target=scope.locator(config['selector']).first
                mode=config.get('state','visible');count=await target.count()
                observed=(count>0 if mode=='attached' else count==0 if mode=='detached' else
                          await target.is_visible() if mode=='visible' else not await target.is_visible())
                if observed and 'text_contains' in config:observed=config['text_contains'] in (await target.inner_text(timeout=500))
                return observed
            return await stable(check_condition,'selector_condition',config)
        if expected_selector:
            if allowed is not None and not allowed(scope.url):return {'state':'unconfirmed','retry_safe':False,'business_outcome_verified':False}
            await scope.locator(expected_selector).first.wait_for(state='visible',timeout=2000)
            async def check():
                return (allowed is None or (allowed(page.url) and allowed(scope.url))) and await scope.locator(expected_selector).first.is_visible()
            return await stable(check,'expected_selector_visible',config)
        deadline=time.monotonic()+config['timeout_ms']/1000
        while True:
            after=await state(element);navigation=page.url!=before_url
            if navigation or before is not None and after is not None and before!=after or time.monotonic()>=deadline:break
            await asyncio.sleep(.05)
        if allowed is not None and not allowed(page.url):return {'state':'unconfirmed','retry_safe':False,'business_outcome_verified':False}
        changed=navigation or (before is not None and after is not None and before!=after)
        return {'state':'observed_change' if changed else 'unconfirmed','navigation_observed':navigation,
                'target_state_changed':before is not None and after is not None and before!=after,
                'retry_safe':False,'business_outcome_verified':False}
    except Exception:
        return {'state':'unconfirmed','retry_safe':False,'business_outcome_verified':False}

async def value_result(element,expected,verification=None):
    async def check():
        return await element.evaluate("(el,value)=>el.isConnected && String(el.isContentEditable?el.innerText:el.value)===value",expected)
    return await stable(check,'value_equals_requested',verification)

async def selection_result(element,selected,verification=None):
    async def check():
        return await element.evaluate("(el,values)=>el.isConnected && JSON.stringify(Array.from(el.selectedOptions||[],o=>o.value))===JSON.stringify(values)",selected)
    return await stable(check,'selected_values_equal_requested',verification)

async def checked_result(element, expected,verification=None):
    async def check():return await element.is_checked() == expected
    return await stable(check,'checked_equals_requested',verification)
