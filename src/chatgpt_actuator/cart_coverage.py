"""Observe loaded cart coverage; never equate missing visible rows with global absence."""
import re

FIELDS={'row_selector','footer_selector','more_selector','loading_selector','match_selector'}

def validate(value):
    if not isinstance(value,dict) or set(value)!=FIELDS:
        raise ValueError('Provide exact cart coverage selectors: '+', '.join(sorted(FIELDS)))
    if any(not isinstance(v,str) or not v.strip() or len(v)>1000 for v in value.values()):
        raise ValueError('Bounded nonempty cart selectors required')
    return value

def coverage(value):
    fields={'loaded_rows','declared_rows','matching_rows','has_more','loading'}
    if not isinstance(value,dict) or set(value)!=fields:raise ValueError('Exact cart coverage counters required')
    for name in ('loaded_rows','declared_rows','matching_rows'):
        if type(value[name]) is not int or not 0<=value[name]<=10000:raise ValueError('Bounded cart row count required')
    if any(type(value[name]) is not bool for name in ('has_more','loading')):raise ValueError('Boolean cart coverage flags required')
    if value['matching_rows']>value['loaded_rows']:raise ValueError('Matching count exceeds loaded rows')
    return value['loaded_rows']==value['declared_rows'] and not value['has_more'] and not value['loading']

async def inspect(page, selectors):
    selectors=validate(selectors)
    rows=page.locator(selectors['row_selector'])
    count=await rows.count()
    footer=page.locator(selectors['footer_selector'])
    footer_count=await footer.count()
    text=await footer.inner_text(timeout=1000) if footer_count==1 else ''
    declared=re.search(r'(?:Select All|Chọn tất cả)\s*\((\d{1,5})\)',text,re.I)
    async def visible(selector):
        loc=page.locator(selector);n=await loc.count()
        return True if n>50 else any([await loc.nth(i).is_visible() for i in range(n)])
    matches=await rows.filter(has=page.locator(selectors['match_selector'])).count()
    values={'loaded_rows':count,'declared_rows':int(declared.group(1)) if declared else None,
            'matching_rows':matches,'has_more':await visible(selectors['more_selector']),
            'loading':await visible(selectors['loading_selector'])}
    consistent=coverage(values) if declared and count<=10000 and matches<=count else False
    return {**values,'state':'consistent_visible_list' if consistent else 'incomplete_or_ambiguous',
            'scope':'caller_selected_loaded_list','account_cart_complete_verified':False,
            'missing_match_means':'absent_in_loaded_rows_only','selectors_source':'caller_supplied',
            'business_outcome_verified':False,'next_action':'Reconcile identity, count units and complete account scope before reserving an add'}
