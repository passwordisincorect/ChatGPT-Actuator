"""Bounded read-only DOM inspection; original implementation, no upstream code copied."""

DOM_SNAPSHOT_SCRIPT = r"""({limit, maxDepth, textLimit, includeHidden, includeShadow, rootSelector}) => {
  const nodes = [], reasons = new Set();
  const symbol = Symbol.for('actuator.semantic.snapshot.v2');
  const registry = globalThis[symbol] || (globalThis[symbol] = {nodes:new WeakMap(),serial:0,epoch:globalThis.crypto?.randomUUID?.() || String(Date.now())+'-'+Math.random()});
  const keyFor = el => {if(!registry.nodes.has(el)) registry.nodes.set(el,registry.epoch+':'+(++registry.serial));return registry.nodes.get(el);};
  let scanned = 0, textUsed = 0, omittedFrames = 0, omittedShadowRoots = 0;
  const clip = value => {
    const s = String(value || '');
    const remaining = Math.max(0, textLimit - textUsed);
    const clipped = s.slice(0, Math.min(512, remaining));
    textUsed += clipped.length;
    if (clipped.length < s.length) reasons.add('text_limit');
    return clipped;
  };
  const ownText = el => {
    let text = '', inspected = 0;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_ALL);
    for (let n = walker.nextNode(); n && inspected++ < 32; n = walker.nextNode()) {
      if (n.nodeType === Node.ELEMENT_NODE && ['SCRIPT','STYLE','TEMPLATE'].includes(n.tagName)) break;
      if (n.nodeType === Node.TEXT_NODE) text += n.data.slice(0, 512 - text.length);
      if (text.length >= 512) break;
    }
    return text.trim();
  };
  // Pointer traversal bounds working memory even for a huge flat DOM.
  const stack = [];
  const root = rootSelector ? document.querySelector(rootSelector) : document.body;
  if (!root) throw new Error('Snapshot root unavailable');
  stack.push({el: root, depth: 0, parent: null, scope:'light'});
  while (stack.length) {
    if (scanned >= limit) { reasons.add('scan_limit'); break; }
    const {el, depth, parent, scope} = stack.pop();
    scanned++;
    const tag = el.tagName.toLowerCase();
    if (el.nextElementSibling && depth > 0)
      stack.push({el: el.nextElementSibling, depth, parent, scope});
    if (['script', 'style', 'template', 'noscript'].includes(tag)) continue;
    const style = getComputedStyle(el);
    const hidden = el.hidden || el.getAttribute('aria-hidden') === 'true' ||
      style.display === 'none' || style.visibility === 'hidden';
    if (hidden && !includeHidden) continue;
    const index = nodes.length;
    const type = (el.getAttribute('type') || '').toLowerCase();
    const roles = {button:'button',a:el.hasAttribute('href')?'link':null,
      select:'combobox',textarea:'textbox',h1:'heading',h2:'heading',h3:'heading',
      main:'main',nav:'navigation',ul:'list',ol:'list',li:'listitem',img:'img'};
    let role = el.getAttribute('role') || roles[tag] || null;
    if (tag === 'input') role = ({checkbox:'checkbox',radio:'radio',range:'slider',
      button:'button',submit:'button'})[type] || 'textbox';
    // Only labels/text: never serialize input.value, attributes, URLs or HTML.
    let name = el.getAttribute('aria-label') || '';
    if (!name && el.labels) name = Array.from(el.labels).slice(0, 4).map(ownText).join(' ');
    if (!name && tag === 'img') name = el.getAttribute('alt') || '';
    if (!name && ['button','a','label','option','summary','h1','h2','h3','h4','h5','h6','p','span'].includes(tag)) name = ownText(el);
    if (el.hasAttribute('aria-labelledby')) {
      const labels = el.getAttribute('aria-labelledby').split(/\s+/).slice(0, 8)
        .map(id => document.getElementById(id)).filter(Boolean).map(ownText).join(' ');
      if (labels) name = labels;
    }
    nodes.push({index, node_key:keyFor(el), parent_index:parent, depth, tree_scope:scope, tag, role, name:clip(name),
      hidden, disabled:el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      is_password:tag === 'input' && type === 'password'});
    if (tag === 'iframe' || tag === 'frame') omittedFrames++;
    if (el.shadowRoot && !includeShadow) omittedShadowRoots++;
    if (el.firstElementChild) {
      if (depth >= maxDepth) reasons.add('depth_limit');
      else stack.push({el:el.firstElementChild, depth:depth+1, parent:index, scope});
    }
    if (includeShadow && el.shadowRoot && el.shadowRoot.firstElementChild) {
      if (depth >= maxDepth) reasons.add('depth_limit');
      else stack.push({el:el.shadowRoot.firstElementChild,depth:depth+1,parent:index,scope:'open_shadow'});
    }
  }
  return {nodes, scanned, returned:nodes.length, truncated:reasons.size > 0,
    truncation_reasons:Array.from(reasons), omitted_frames:omittedFrames,
    omitted_open_shadow_roots:omittedShadowRoots, text_chars:textUsed};
}"""
