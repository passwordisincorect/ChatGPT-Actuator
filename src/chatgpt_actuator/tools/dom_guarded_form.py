"""Bounded condition checking and form mutation in one JS task; no retarget/focus."""
import asyncio,time
SCRIPT="""(el,args) => {
  const eligible=()=>{
  const modal=el.ownerDocument.querySelector('dialog:modal');let inside=!modal;
  let enabled=!el.matches(':disabled') && !el.disabled;
  for(let n=el;n;n=n.assignedSlot||n.parentElement||n.getRootNode().host||null){
    if(n.hasAttribute('inert'))return false;
    if(n.getAttribute('aria-disabled')==='true')enabled=false;
    if(n===modal)inside=true;
  }
  if(!inside || !enabled || !el.isConnected)return false;
  const rect=el.getBoundingClientRect(),style=getComputedStyle(el);
  const visible=rect.width>0 && rect.height>0 && style.visibility!=='hidden' && style.visibility!=='collapse';
  for(const [key,expected] of Object.entries(args.condition||{})){
    const actual=key==='enabled'?enabled:key==='visible'?visible:key==='text_equals'?el.innerText:key==='value_equals'?el.value:undefined;
    if(actual!==expected)return false;
  }
  return true;};
  if(!eligible())return {refused:true};
  const fire=(name)=>{
    if(name==='input'){
      try{return el.dispatchEvent(new InputEvent('input',{bubbles:true,composed:true,inputType:args.value?'insertText':'deleteContentBackward',data:args.value||null}));}catch(_){}
    }
    return el.dispatchEvent(new Event(name,{bubbles:true,composed:true}));
  };
  if(args.method==='set_value'){
    if(el.readOnly)return {refused:true};
    if(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement){
      if(el instanceof HTMLInputElement && el.type==='file')return {invalid:true};
      const proto=el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
      const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;
      if(!setter)return {invalid:true};setter.call(el,args.value);fire('input');fire('change');
    }else if(el.isContentEditable){
      const expected=String(args.value).split(String.fromCharCode(13)).join('');
      const permitted=el.dispatchEvent(new InputEvent('beforeinput',{bubbles:true,composed:true,cancelable:true,inputType:expected?'insertText':'deleteContentBackward',data:expected||null}));
      if(!permitted || !eligible())return {outcome_unknown:true};
      el.replaceChildren();const p=document.createElement('p');if(expected)p.textContent=expected;else p.appendChild(document.createElement('br'));el.appendChild(p);fire('input');
    }else return {invalid:true};
    return {dispatched:true};
  }
  if(args.method==='select'){
    if(!(el instanceof HTMLSelectElement))return {invalid:true};
    const option=Array.from(el.options).find(o=>args.value!==null?o.value===args.value:o.label===args.label);
    if(!option)return {pending:true};
    if(option.disabled || option.parentElement?.disabled)return {refused:true};
    for(const o of el.options)o.selected=o===option;fire('input');fire('change');
    return {dispatched:true,selected:Array.from(el.selectedOptions).map(o=>o.value)};
  }
  if(args.method==='toggle'){
    if(!(el instanceof HTMLInputElement) || !['checkbox','radio'].includes(el.type))return {invalid:true};
    const before=el.checked,target=args.checked===null?!before:args.checked;
    if(el.type==='radio' && !target)return {invalid:true};
    if(before!==target)el.click();
    return {dispatched:true,before,after:el.checked,requested:target};
  }
  return {invalid:true};
}"""

async def dispatch(node,method,condition,timeout_ms,**arguments):
    from .dom_preconditions import DOMPreconditionError
    from .dom_verification import DOMValidationError
    deadline=time.monotonic()+timeout_ms/1000
    while True:
        result=await node.evaluate(SCRIPT,{'method':method,'condition':condition,**arguments})
        if result.get('outcome_unknown'):
            from ..automation_errors import MutationOutcomeUnknown
            raise MutationOutcomeUnknown('beforeinput callback outcome must be inspected')
        if result.get('refused'):raise DOMPreconditionError('DOM condition/control changed at dispatch')
        if result.get('invalid'):raise DOMValidationError('Unsupported guarded form operation')
        if result.get('dispatched'):return result
        if not result.get('pending') or time.monotonic()>=deadline:raise DOMPreconditionError('Option unavailable before dispatch')
        await asyncio.sleep(.05)
