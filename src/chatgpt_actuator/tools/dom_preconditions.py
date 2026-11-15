"""Validate target conditions before one mutation, without exporting values."""
import asyncio
class DOMPreconditionError(ValueError):pass

def validate(condition):
    if condition is None:return
    if not isinstance(condition,dict) or set(condition)-{'visible','enabled','text_equals','value_equals'}:
        raise ValueError('Unsupported DOM precondition')
    for key,value in condition.items():
        if key in {'visible','enabled'} and type(value) is not bool:raise ValueError('Boolean precondition required')
        if key in {'text_equals','value_equals'} and (not isinstance(value,str) or len(value)>2000):raise ValueError('Text precondition requires <=2000 characters')

async def check(element,condition):
    validate(condition)
    if condition is None:return
    for key,value in condition.items():
        observed=(await element.is_visible() if key=='visible' else await element.is_enabled() if key=='enabled' else
                  await asyncio.wait_for(element.inner_text(),timeout=1) if key=='text_equals' else await element.input_value(timeout=1000))
        if observed!=value:raise DOMPreconditionError('DOM target changed before dispatch')
