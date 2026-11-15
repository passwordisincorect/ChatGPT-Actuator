"""CAS cart ledger. Caller observations are evidence, never website exactly-once proof."""
import hashlib,json,secrets,time,re
from urllib.parse import urlsplit
from dataclasses import dataclass
from .dom_progress import key,public_url

@dataclass(frozen=True)
class ConsumedReservation:
    """Internal in-process marker; JSON broker arguments cannot forge this type."""
    identity:dict
    delta:int

def check_action(url,label,reservation):
    """Recognize known Shopee cart buttons, not general buttons or site text."""
    parsed=urlsplit(url)
    if parsed.hostname not in {'shopee.vn','www.shopee.vn','m.shopee.vn'} or not re.search(r'add\s+to\s+cart|thêm\s+vào\s+giỏ',label,re.I):return False
    from .automation_errors import CartCheckpointRequired
    product=re.search(r'(?:/product/|i\.)(\d+)[/.](\d+)',parsed.path)
    if not isinstance(reservation,ConsumedReservation) or not product or product.groups()!=(reservation.identity['shop_id'],reservation.identity['item_id']):raise CartCheckpointRequired('Observe and reserve this product before adding')
    return True

def whole(value,name):
    if type(value) is not int or not 0<=value<=100000:raise ValueError('Bounded nonnegative integer required: '+name)
    return value

def identity(value):
    if not isinstance(value,dict) or set(value)!={'account','shop_id','item_id','variant','pieces_per_pack'}:raise ValueError('Exact cart identity fields required')
    for field in ('account','shop_id','item_id','variant'):
        v=value[field]
        if not isinstance(v,str) or not v.strip() or len(v)>160:raise ValueError('Bounded confirmed cart identity required')
    if whole(value['pieces_per_pack'],'pieces_per_pack')<1:raise ValueError('Positive pieces_per_pack required')
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def require_zero_coverage(data):
    if data['quantity_before']!=0 or data['delta']<=0:return
    from .cart_coverage import coverage
    from .automation_errors import CartCoverageRequired
    supplied=data.get('coverage')
    try:valid=coverage(supplied) and supplied['matching_rows']==0
    except ValueError:valid=False
    if not valid:raise CartCoverageRequired('Zero-quantity additions require explicit consistent cart coverage; a missing loaded row is not global absence')

def record(store,owner,task,item,url,revision,payload):
    key(task);key(item);public_url(url)
    parsed=urlsplit(url)
    if parsed.query or parsed.fragment or parsed.path.startswith('/verify/'):raise ValueError('Use a clean product URL, never a verification/tracking URL')
    if type(revision) is not int or revision<0:raise ValueError('Expected revision required')
    if not isinstance(payload,dict):raise ValueError('Cart event required')
    event=payload.get('event')
    if event not in {'observe','reserve','reconcile','resolve'}:raise ValueError('Cart event must be observe, reserve, reconcile or resolve')
    fields={'observe':{'event','identity','quantity','target_quantity','complete','evidence_ref'},
            'reserve':{'event'},'reconcile':{'event','identity','quantity','complete','evidence_ref','attempt_id'},
            'resolve':{'event','identity','quantity','complete','evidence_ref','attempt_id','acknowledge_unknown'}}
    if set(payload)-{'prices','coverage'}!=fields[event] or (set(payload)&{'prices','coverage'} and event=='reserve'):raise ValueError('Exact event fields required')
    if 'coverage' in payload:
        from .cart_coverage import coverage
        if not coverage(payload['coverage']):raise ValueError('Cart coverage incomplete or count mismatch; loaded rows are not global absence')
    prices=payload.get('prices',{})
    allowed_prices={'product_unit_vnd','after_voucher_unit_vnd','cart_unit_vnd','cart_line_total_vnd'}
    if not isinstance(prices,dict) or set(prices)-allowed_prices:raise ValueError('Explicit separate VND price fields required')
    for value in prices.values():
        if value is not None and (type(value) is not int or not 0<=value<=10**12):raise ValueError('Price must be a nonnegative whole VND amount or null')
    if event!='reserve':
        ident=identity(payload['identity']);quantity=whole(payload['quantity'],'quantity')
        if 'coverage' in payload:
            matches=payload['coverage']['matching_rows']
            if matches>1 or (quantity==0 and matches!=0) or (quantity>0 and matches!=1):
                raise ValueError('Cart quantity and exact-identity row coverage disagree; inspect before reserving')
        if payload['complete'] is not True:raise ValueError('Complete matching cart evidence required; unseen rows are not zero')
        evidence=payload['evidence_ref']
        if not isinstance(evidence,str) or not re.fullmatch(r'[A-Za-z0-9_:.-]{1,160}',evidence):raise ValueError('Opaque bounded observation reference required')
        if event=='observe':target=whole(payload['target_quantity'],'target_quantity')
    now=time.time()
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT data FROM cart_checkpoints WHERE owner=? AND task=? AND item=?',(owner,task,item)).fetchone()
        previous=json.loads(row[0]) if row else None
        if (previous['revision'] if previous else 0)!=revision:raise ValueError('Cart checkpoint revision conflict; read status before another request')
        if event=='observe':
            if previous and previous['state'] in {'pending','unknown','needs_review'}:raise ValueError('Reconcile the pending/unknown attempt first; observe cannot erase it')
            if previous and previous['identity_hash']!=ident:raise ValueError('Cart identity changed; use a different item key')
            data={'identity':payload['identity'],'identity_hash':ident,'quantity_before':quantity,'target_quantity':target,
                  'delta':max(target-quantity,0),'state':'observed','observed_at':now,'evidence_ref':evidence,'url':url,'prices_before':prices,
                  'coverage':payload.get('coverage'),'coverage_source':'caller_supplied' if 'coverage' in payload else 'not_supplied'}
        else:
            if previous is None:raise ValueError('Observe the exact cart identity first')
            if previous['url']!=url:raise ValueError('Cart item URL changed')
            data=dict(previous)
            if event=='reserve':
                if data['state'] not in {'observed','sufficient'}:raise ValueError('Pending or unknown outcome; reconcile before reserving again')
                if now-data['observed_at']>60:raise ValueError('Cart observation expired; read cart again')
                require_zero_coverage(data)
                if data['delta']==0:data['state']='sufficient'
                else:
                    busy=db.execute("SELECT 1 FROM cart_checkpoints WHERE owner=? AND identity_hash=? AND state IN ('pending','unknown','needs_review') LIMIT 1",(owner,data['identity_hash'])).fetchone()
                    if busy:raise ValueError('This cart identity already has an unresolved attempt in another checkpoint')
                    data.update(state='pending',attempt_id=secrets.token_urlsafe(18),reserved_at=now)
            else:
                if data['state'] not in {'pending','unknown','needs_review'}:raise ValueError('No pending attempt to reconcile')
                if ident!=data['identity_hash'] or payload['attempt_id']!=data.get('attempt_id'):raise ValueError('Cart attempt/identity mismatch')
                if evidence in {data['evidence_ref'],data.get('after_evidence_ref')}:raise ValueError('Fresh after-action observation required')
                if event=='resolve' and payload['acknowledge_unknown'] is not True:raise ValueError('Explicit acknowledgement of inspected unknown outcome required')
                data.update(quantity_after=quantity,after_evidence_ref=evidence,prices_after=prices,
                    coverage_after=payload.get('coverage'),
                    state='resolved' if event=='resolve' else 'completed' if quantity==data['quantity_before']+data['delta'] else 'needs_review')
        data.update(revision=revision+1,updated_at=now,evidence_source='caller_reported_cart_observation',
                    automatic_replay=False,business_outcome_verified=False)
        if not previous and db.execute('SELECT COUNT(*) FROM cart_checkpoints WHERE owner=?',(owner,)).fetchone()[0]>=10000:raise ValueError('Cart checkpoint budget reached')
        db.execute('INSERT OR REPLACE INTO cart_checkpoints VALUES(?,?,?,?,?,?,?)',(owner,task,item,json.dumps(data,ensure_ascii=False),data['revision'],data['identity_hash'],data['state']))
    return {'item_key':item,**data,'dispatch_performed':False,'next_action':'inspect_cart' if data['state']=='needs_review' else 'no_automatic_action'}

def status(store,owner,task):
    key(task)
    with store.db() as db:
        rows=db.execute('SELECT item,data FROM cart_checkpoints WHERE owner=? AND task=? ORDER BY item LIMIT 1001',(owner,task)).fetchall()
    return {'items':[{'item_key':item,**json.loads(data)} for item,data in rows[:1000]],'truncated':len(rows)>1000,'automatic_replay':False,'dispatch_performed':False}

def claim(store,owner,payload):
    """Consume one reservation before a possible click; crash means inspect, never replay."""
    if not isinstance(payload,dict) or set(payload)!={'task_key','item_key','attempt_id','expected_revision'}:raise ValueError('Exact reserved cart checkpoint required')
    task=key(payload['task_key']);item=key(payload['item_key'])
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT data FROM cart_checkpoints WHERE owner=? AND task=? AND item=?',(owner,task,item)).fetchone()
        data=json.loads(row[0]) if row else None
        if not data or data['state']!='pending' or data.get('attempt_id')!=payload['attempt_id'] or type(payload['expected_revision']) is not int or data['revision']!=payload['expected_revision']:raise ValueError('Reservation missing, changed or already consumed; inspect cart before retry')
        if time.time()-data['reserved_at']>60:raise ValueError('Reservation expired; reconcile before retry')
        require_zero_coverage(data)
        data.update(state='unknown',revision=data['revision']+1,updated_at=time.time())
        db.execute('UPDATE cart_checkpoints SET data=?,revision=?,state=? WHERE owner=? AND task=? AND item=?',(json.dumps(data,ensure_ascii=False),data['revision'],data['state'],owner,task,item))
    return {'task_key':task,'item_key':item,**data,'business_outcome_verified':False,'automatic_replay':False}
