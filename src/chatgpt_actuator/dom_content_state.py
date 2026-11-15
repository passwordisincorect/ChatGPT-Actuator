"""Conservative observed text indicators. No login, challenge bypass or retry."""
import re
from urllib.parse import urlsplit


def classify(title, text, url=None):
    content = (str(title or '') + '\n' + str(text or '')).casefold()
    try:path=urlsplit(url or '').path.casefold().rstrip('/')
    except ValueError:path=''
    if path in {'/verify/captcha','/verify/traffic','/verify/ivs'}:
        return {'state':'captcha','confidence':'verification_url','human_action_required':True,
                'next_step':'Wait or complete verification directly in the selected browser, then request a fresh observation',
                'automatic_retry':False}
    rules = (
        ('session_expired', r'session (?:has )?expired|phiên (?:đã )?hết hạn', 'Sign in directly in the selected browser, then request a fresh observation'),
        ('captcha', r'verify (?:that )?you are human|complete the captcha|solve the captcha to verify|xác minh bạn là con người|security verification required|verification (?:can.t|cannot) be completed', 'Wait or complete verification directly in the browser, then request a fresh observation'),
        ('login_required', r'login required|sign in to continue|log in to continue|đăng nhập để tiếp tục', 'Sign in directly in the selected browser, then request a fresh observation'),
    )
    for state, pattern, step in rules:
        if re.search(pattern, content):
            return {'state': state, 'confidence': 'text_indicator', 'human_action_required': True, 'next_step': step, 'automatic_retry': False}
    return {'state': 'no_known_blocker_observed', 'confidence': 'limited_text_observation', 'human_action_required': False, 'automatic_retry': False}


async def observe(dom, tab, title, url=None):
    initial=classify(title,'',url)
    if initial['human_action_required']:return initial
    matches = await dom.find(tab, selector='xpath=/html/body', max_items=1)
    text = ''
    if matches['matches']:
        text = (await dom.read_text(matches['matches'][0]['element_ref'], max_chars=2000)).get('text', '')
    return classify(title, text, url)
