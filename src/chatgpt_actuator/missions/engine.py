"""One claimed step per call, bounded observations, no automatic mutation replay."""
import asyncio
import copy
from contextvars import ContextVar
import hashlib
import inspect
import json
import re
import secrets
import time

from .store import MissionStore, encode
from ..batch import _invoke
from ..batch_preflight import check
from ..tool_policy import authorize, scope_for

TERMINAL = {'COMPLETED', 'CANCELLED', 'FAILED'}
CALL_TIMEOUT_SECONDS = 30
# Owner approval is scoped to the immutable full plan digest. Never supplied by MCP.
SENSITIVE = frozenset('filesystem_delete filesystem_move process_stop powershell_run '
    'command_profile_run eda_kicad_export_gerbers dom_click uia_invoke uia_control_window'.split())
FORBIDDEN_PREFIXES = ('mission_', 'batch_', 'work_session_', 'task_attempt_', 'dom_operation_', 'dom_review_')
_pending = set()
_mission_call = ContextVar('mission_call', default=None)


class MissionEngine:
    def __init__(self, path, handlers, approvals=None):
        self.store = MissionStore(path)
        self.handlers = handlers
        self.approvals = approvals or (lambda: {})
        self.pending = {}

    def _action(self, action, principal, *, readonly=False):
        if not isinstance(action, dict) or set(action) != {'tool', 'arguments'}:
            raise ValueError('Action requires exactly tool and arguments')
        tool, args = action['tool'], action['arguments']
        if not isinstance(tool, str) or tool.startswith(FORBIDDEN_PREFIXES) or tool not in self.handlers:
            raise ValueError('Tool is not mission-enabled')
        if not isinstance(args, dict) or self._has_ref(args):
            raise ValueError('Use concrete current arguments; batch $ref is unsupported')
        authorize(tool, principal, args)
        if readonly and scope_for(tool) != 'mcp:read':
            raise PermissionError('Verification requires a read-only tool')
        check(0, tool, args, self.handlers[tool])

    @staticmethod
    def _has_ref(value):
        if isinstance(value, dict):
            return '$ref' in value or any(MissionEngine._has_ref(v) for v in value.values())
        return isinstance(value, list) and any(MissionEngine._has_ref(v) for v in value)

    def _conditions(self, conditions, principal, *, required=True):
        if not isinstance(conditions, list) or len(conditions) > 20 or (required and not conditions):
            raise ValueError('Provide 1..20 read-only evidence conditions')
        for condition in conditions:
            if not isinstance(condition, dict) or set(condition) != {'tool', 'arguments', 'path', 'op', 'value'}:
                raise ValueError('Condition requires tool, arguments, path, op and value')
            if condition['op'] not in {'eq', 'gte', 'contains'}:
                raise ValueError('Unsupported condition operator')
            if not isinstance(condition['path'], list) or not all(type(v) in (str, int) for v in condition['path']):
                raise ValueError('path must be a list of object keys or list indices')
            if len(condition['path']) > 16:
                raise ValueError('Evidence path too deep')
            self._action({k: condition[k] for k in ('tool', 'arguments')}, principal, readonly=True)

    def create(self, principal, goal, steps, criteria, request_key, max_seconds=1800, max_attempts=3):
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 4000:
            raise ValueError('Goal must be 1..4000 characters')
        if not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', request_key):
            raise ValueError('Provide a stable request_key')
        if type(max_seconds) is not int or not 1 <= max_seconds <= 3600:
            raise ValueError('max_seconds must be 1..3600')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError('max_attempts must be 1..3')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
            raise ValueError('Provide 1..100 ordered steps')
        plan = {'goal': goal, 'steps': steps, 'criteria': criteria,
                'max_seconds': max_seconds, 'max_attempts': max_attempts}
        if len(encode(plan).encode()) > 262144:
            raise ValueError('Mission plan too large')
        self._conditions(criteria, principal)
        for step in steps:
            if not isinstance(step, dict) or set(step) != {'action', 'preconditions', 'postconditions'}:
                raise ValueError('Step requires action, preconditions and postconditions')
            self._action(step['action'], principal)
            self._conditions(step['preconditions'], principal, required=False)
            self._conditions(step['postconditions'], principal)
        digest = hashlib.sha256(encode({'owner': principal.owner, 'plan': plan}).encode()).hexdigest()
        with self.store.db() as db:
            old = db.execute('SELECT id,digest FROM missions WHERE owner=? AND request_key=?',
                             (principal.owner, request_key)).fetchone()
            if old:
                if old[1] != digest:
                    raise ValueError('Idempotency key already used for another plan')
                return self.store.load(db, principal.owner, old[0])
            if db.execute("SELECT COUNT(*) FROM missions WHERE owner=? AND COALESCE(json_extract(document,'$.archived'),0)=0", (principal.owner,)).fetchone()[0] >= 1000:
                raise RuntimeError('Owner mission storage limit reached')
            if db.execute('SELECT COUNT(*) FROM missions').fetchone()[0] >= 50000:
                raise RuntimeError('Retained Mission idempotency key limit reached')
            doc = {'id': secrets.token_urlsafe(24), 'owner': principal.owner, 'digest': digest,
                   'goal': goal, 'criteria': copy.deepcopy(criteria), 'created': time.time(),
                   'state': 'PENDING', 'reason': None, 'max_seconds': max_seconds,
                   'max_attempts': max_attempts, 'elapsed_seconds': 0.0,
                   'steps': [{**copy.deepcopy(s), 'state': 'PENDING', 'attempts': 0} for s in steps]}
            db.execute('INSERT INTO missions VALUES(?,?,?,?,?,?)',
                       (doc['id'], principal.owner, request_key, digest, encode(doc), time.time()))
            self.store.save(db, doc, 'created')
            return doc

    def status(self, principal, mission_id):
        with self.store.db() as db:
            doc = self.store.load(db, principal.owner, mission_id)
            events = db.execute('SELECT seq,kind,created,detail FROM mission_events WHERE mission=? '
                                'ORDER BY seq DESC LIMIT 30', (mission_id,)).fetchall()
            attempts = db.execute('SELECT id,step,status,dispatched,created,finished,evidence '
                'FROM mission_attempts WHERE mission=? ORDER BY created DESC LIMIT 30', (mission_id,)).fetchall()
            return {**doc, 'events': [{'seq': r[0], 'kind': r[1], 'created': r[2],
                    'detail': json.loads(r[3])} for r in reversed(events)],
                    'attempt_history': [{'id': r[0], 'step': r[1], 'state': r[2],
                        'dispatched': bool(r[3]), 'created': r[4], 'finished': r[5],
                        'evidence': json.loads(r[6]) if r[6] else None} for r in reversed(attempts)],
                    'automatic_replay': False, 'independent_ai': False}

    def next(self, principal, mission_id):
        doc = self.status(principal, mission_id)
        index = next((i for i, s in enumerate(doc['steps']) if s['state'] != 'VERIFIED'), None)
        return {'mission_id': mission_id, 'state': doc['state'], 'reason': doc['reason'],
                'next_step': index if doc['state'] == 'PENDING' else None,
                'next_action': 'verify_goal' if index is None and doc['state'] == 'PENDING' else
                               'execute_step' if doc['state'] == 'PENDING' else 'inspect_status',
                'digest': doc['digest']}

    def _approved(self, doc, index):
        policy = self.approvals()
        return isinstance(policy, dict) and policy.get(doc['digest'], {}).get(str(index)) is True

    @staticmethod
    def _healthy(value):
        if not isinstance(value, dict):
            return True
        if value.get('ok') is False or value.get('timed_out') or value.get('cancelled') or value.get('error'):
            return False
        if value.get('exit_code', 0) not in (0, None):
            return False
        if value.get('status') in ('blocked', 'failed', 'pending', 'cancelling', 'approval_required'):
            return False
        verification = value.get('verification', {})
        return not isinstance(verification, dict) or verification.get('state') not in ('mismatch', 'unknown')

    async def _call(self, action, principal, deadline):
        self._action(action, principal)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Mission call deadline exceeded')
        # Synchronous calls run off the event loop. Late providers may still finish;
        # their mutation is UNKNOWN and is never replayed by this engine.
        task = asyncio.create_task(_invoke(self.handlers[action['tool']], action['arguments'], use_thread=True))
        mission = _mission_call.get()
        self.pending.setdefault(mission, set()).add(task)
        _pending.add(task)
        def done(t):
            _pending.discard(t)
            self.pending.get(mission, set()).discard(t)
            if not t.cancelled():
                t.exception()
            if mission is not None and not self.pending.get(mission):
                with self.store.db() as db:
                    db.execute("UPDATE mission_attempts SET status='FINISHED',finished=? "
                               "WHERE mission=? AND status='CLEANUP_PENDING'", (time.time(), mission))
        task.add_done_callback(done)
        completed, _ = await asyncio.wait({task}, timeout=remaining)
        if not completed:
            raise TimeoutError('Provider outcome unknown; inspect before continuing')
        value = await task
        if not self._healthy(value):
            raise RuntimeError('Provider reported a blocked, failed or uncertain outcome')
        return value

    async def _observe(self, conditions, principal, deadline, capture=None):
        evidence = []
        for condition in conditions:
            value = await self._call({k: condition[k] for k in ('tool', 'arguments')}, principal, deadline)
            if capture is not None:
                capture(condition, value)
            try:
                for key in condition['path']:
                    value = value[key]
                expected = condition['value']
                if condition['op'] == 'eq':
                    passed = type(value) is type(expected) and value == expected
                elif condition['op'] == 'gte':
                    passed = type(value) in (int, float) and type(expected) in (int, float) and value >= expected
                else:
                    passed = isinstance(value, (str, list, dict)) and expected in value
            except (KeyError, IndexError, TypeError):
                passed = False
            evidence.append({'tool': condition['tool'], 'path': condition['path'], 'passed': passed,
                             'observed_sha256': hashlib.sha256(encode(value).encode()).hexdigest(),
                             'observed_at': time.time()})
        return all(e['passed'] for e in evidence), evidence

    def control(self, principal, mission_id, action, bindings=None):
        if action not in {'pause', 'resume', 'cancel', 'rebind'}:
            raise ValueError('action must be pause, resume, cancel or rebind')
        if bindings is not None and action != 'rebind':
            raise ValueError('bindings are only valid for rebind')
        with self.store.db() as db:
            doc = self.store.load(db, principal.owner, mission_id)
            if doc['state'] == 'RUNNING':
                raise RuntimeError('An active attempt must settle before controlling this mission')
            if doc['state'] in TERMINAL:
                raise ValueError('Mission is terminal')
            if action == 'rebind':
                if not isinstance(bindings, dict) or not 1 <= len(bindings) <= 100 or any(
                    not isinstance(k, str) or not isinstance(v, str) or not 1 <= len(k) <= 4096
                    or not 1 <= len(v) <= 4096 for k, v in bindings.items()):
                    raise ValueError('Provide a bounded old-reference to new-reference mapping')
                used = set()
                reference_keys = {'tab_ref', 'element_ref', 'root_ref', 'target_ref', 'workspace_ref'}
                def walk(value):
                    if isinstance(value, list):
                        return [walk(v) for v in value]
                    if isinstance(value, dict):
                        out = {}
                        for k, v in value.items():
                            if k in reference_keys and isinstance(v, str) and v in bindings:
                                used.add(v)
                                out[k] = bindings[v]
                            else:
                                out[k] = walk(v)
                        return out
                    return value
                for step in doc['steps']:
                    if step['state'] != 'VERIFIED':
                        step['action']['arguments'] = walk(step['action']['arguments'])
                        for conditions in (step['preconditions'], step['postconditions']):
                            for item in conditions:
                                item['arguments'] = walk(item['arguments'])
                        self._action(step['action'], principal)
                        self._conditions(step['postconditions'], principal)
                        self._conditions(step['preconditions'], principal, required=False)
                for item in doc['criteria']:
                    item['arguments'] = walk(item['arguments'])
                self._conditions(doc['criteria'], principal)
                if used != set(bindings):
                    raise ValueError('Every binding must replace an existing pending/evidence reference')
                # Fresh digest revokes earlier sensitive-step approvals; unknown
                # delivery stays UNKNOWN, so rebind can never authorize replay.
                doc['digest'] = hashlib.sha256(encode({'owner': principal.owner, 'goal': doc['goal'],
                    'steps': doc['steps'], 'criteria': doc['criteria']}).encode()).hexdigest()
            elif action == 'resume':
                if any(s['state'] == 'UNKNOWN' for s in doc['steps']):
                    raise RuntimeError('Unknown delivery: verify the interrupted step; replay is forbidden')
                if doc['elapsed_seconds'] >= doc['max_seconds']:
                    raise RuntimeError('Mission time budget exhausted')
                doc['state'], doc['reason'] = 'PENDING', None
            else:
                doc['state'], doc['reason'] = ('PAUSED' if action == 'pause' else 'CANCELLED'), action
            self.store.save(db, doc, action)
            return doc

    async def execute(self, principal, mission_id, expected_digest, expected_step):
        began = time.monotonic()
        with self.store.db() as db:
            doc = self.store.load(db, principal.owner, mission_id)
            if doc['digest'] != expected_digest:
                raise ValueError('Mission digest mismatch')
            if type(expected_step) is not int or not 0 <= expected_step < len(doc['steps']):
                raise ValueError('Invalid expected step')
            if doc['state'] != 'PENDING':
                return {'mission_id': mission_id, 'state': doc['state'], 'reason': doc['reason'], 'dispatched': False}
            index = next((i for i, s in enumerate(doc['steps']) if s['state'] != 'VERIFIED'), None)
            if doc['steps'][expected_step]['state'] == 'VERIFIED':
                return {'mission_id': mission_id, 'state': doc['state'], 'step': expected_step,
                        'step_state': 'VERIFIED', 'dispatched': False, 'already_verified': True}
            if index != expected_step:
                raise ValueError('Expected step is not the next ordered step')
            if index is None:
                return {'mission_id': mission_id, 'state': 'PENDING', 'next_action': 'verify_goal', 'dispatched': False}
            step = doc['steps'][index]
            if step['attempts'] >= doc['max_attempts'] or doc['elapsed_seconds'] >= doc['max_seconds']:
                doc['state'], doc['reason'] = 'FAILED', 'budget_exhausted'
                self.store.save(db, doc, 'budget_exhausted')
                return doc
            if step['action']['tool'] in SENSITIVE and not self._approved(doc, index):
                doc['state'], doc['reason'] = 'BLOCKED', 'owner_approval_required'
                self.store.save(db, doc, 'approval_required', {'step': index, 'digest': doc['digest']})
                return {'mission_id': mission_id, 'state': 'BLOCKED', 'reason': doc['reason'], 'dispatched': False}
            step['attempts'] += 1
            attempt = self.store.claim(db, doc, index)
        deadline = began + min(CALL_TIMEOUT_SECONDS, doc['max_seconds'] - doc['elapsed_seconds'])
        token = _mission_call.set(mission_id)
        dispatched, evidence = False, {}
        try:
            self._action(step['action'], principal)
            passed, evidence['preconditions'] = await self._observe(step['preconditions'], principal, deadline)
            if not passed:
                raise ValueError('Preconditions not satisfied')
            with self.store.db() as db:
                db.execute('UPDATE mission_attempts SET dispatched=1 WHERE id=?', (attempt,))
            dispatched = True
            action_result = await self._call(step['action'], principal, deadline)
            try:
                serialized = encode(action_result)
                evidence['result'] = action_result if len(serialized.encode()) <= 12000 else {
                    'truncated': True, 'sha256': hashlib.sha256(serialized.encode()).hexdigest()}
            except (TypeError, ValueError):
                evidence['result'] = {'non_json_result': True}
            passed, evidence['postconditions'] = await self._observe(step['postconditions'], principal, deadline)
            if not passed:
                raise ValueError('Postconditions not satisfied')
            step['state'], doc['state'], doc['reason'] = 'VERIFIED', 'PENDING', None
        except BaseException as exc:
            from ..atomic_files import FileConflictError, FilePreconditionRequired
            if step['action']['tool'].startswith('filesystem_') and isinstance(exc, (FileConflictError, FilePreconditionRequired)):
                # These native CAS guards prove the target was not changed.
                # Generic errors/timeouts remain UNKNOWN and cannot be replayed.
                dispatched = False
                with self.store.db() as db:
                    db.execute('UPDATE mission_attempts SET dispatched=0 WHERE id=?', (attempt,))
            mutation = scope_for(step['action']['tool']) != 'mcp:read'
            step['state'] = 'UNKNOWN' if dispatched and mutation else 'PENDING'
            doc['state'], doc['reason'] = 'BLOCKED', type(exc).__name__
            evidence['error'] = {'type': type(exc).__name__, 'delivery': 'unknown' if dispatched else 'not_dispatched'}
            if not isinstance(exc, Exception):
                raise
        finally:
            _mission_call.reset(token)
            doc['elapsed_seconds'] += time.monotonic() - began
            with self.store.db() as db:
                self.store.finish(db, doc, attempt, evidence, bool(self.pending.get(mission_id)))
        return {**self.next(principal, mission_id), 'step': index, 'step_state': step['state'],
                'dispatched': dispatched, 'evidence': evidence}

    async def verify(self, principal, mission_id, step_index=None):
        began = time.monotonic()
        with self.store.db() as db:
            doc = self.store.load(db, principal.owner, mission_id)
            if self.pending.get(mission_id):
                raise RuntimeError('Provider cleanup is pending; verification cannot settle delivery yet')
            if doc['state'] in TERMINAL or doc['state'] in {'RUNNING', 'PAUSED'}:
                raise ValueError('Mission cannot be verified in this state')
            if doc['elapsed_seconds'] >= doc['max_seconds']:
                raise RuntimeError('Mission time budget exhausted')
            if step_index is None:
                if any(s['state'] != 'VERIFIED' for s in doc['steps']):
                    raise ValueError('All steps must be verified first')
                conditions = doc['criteria']
            else:
                if type(step_index) is not int or not 0 <= step_index < len(doc['steps']):
                    raise ValueError('Invalid step index')
                if doc['steps'][step_index]['state'] != 'UNKNOWN':
                    raise ValueError('Only uncertain steps need recovery verification')
                conditions = doc['steps'][step_index]['postconditions']
            attempt = self.store.claim(db, doc, None)
        token = _mission_call.set(mission_id)
        evidence = {}
        try:
            passed, evidence['observations'] = await self._observe(conditions, principal,
                began + min(CALL_TIMEOUT_SECONDS, doc['max_seconds'] - doc['elapsed_seconds']))
            if passed:
                if step_index is None:
                    doc['state'], doc['reason'] = 'COMPLETED', None
                else:
                    doc['steps'][step_index]['state'] = 'VERIFIED'
                    doc['state'], doc['reason'] = 'PENDING', None
            else:
                doc['state'], doc['reason'] = 'BLOCKED', 'verification_mismatch'
        except BaseException as exc:
            doc['state'], doc['reason'] = 'BLOCKED', type(exc).__name__
            evidence['error'] = {'type': type(exc).__name__}
            if not isinstance(exc, Exception):
                raise
        finally:
            _mission_call.reset(token)
            doc['elapsed_seconds'] += time.monotonic() - began
            with self.store.db() as db:
                self.store.finish(db, doc, attempt, evidence, bool(self.pending.get(mission_id)))
        return {'mission_id': mission_id, 'state': doc['state'], 'reason': doc['reason'],
                'evidence': evidence, 'automatic_replay': False}
