"""Check a pinned node's conditions in the same JS task as semantic dispatch."""
SCRIPT="""(el,condition) => {
  const modal=el.ownerDocument.querySelector('dialog:modal');let inside=!modal;
  let enabled=!el.matches(':disabled') && !el.disabled;
  for(let n=el;n;n=n.assignedSlot||n.parentElement||n.getRootNode().host||null){
    if(n.hasAttribute('inert'))return {dispatched:false};
    if(n.getAttribute('aria-disabled')==='true')enabled=false;
    if(n===modal)inside=true;
  }
  if(!inside || !enabled || !el.isConnected || typeof el.click!=='function')return {dispatched:false};
  const rect=el.getBoundingClientRect(),style=getComputedStyle(el);
  const visible=rect.width>0 && rect.height>0 && style.visibility!=='hidden' && style.visibility!=='collapse';
  for(const [key,expected] of Object.entries(condition||{})){
    const actual=key==='enabled'?enabled:key==='visible'?visible:key==='text_equals'?el.innerText:key==='value_equals'?el.value:undefined;
    if(actual!==expected)return {dispatched:false};
  }
  el.click();return {dispatched:true};
}"""

async def dispatch(node,condition):
    from .dom_preconditions import DOMPreconditionError
    result=await node.evaluate(SCRIPT,condition)
    if not result.get('dispatched'):raise DOMPreconditionError('DOM condition changed at dispatch')
