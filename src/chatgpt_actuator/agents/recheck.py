"""Read-only rechecks can unblock verified goals, never replay mutations."""
import time
from dataclasses import replace

from . import clocks
from ..tool_policy import principal_context


async def inspect(service, owner, job_id, resume_if_verified=True):
    if not service.enabled():
        raise PermissionError('Agent policy is disabled')
    with service.queue.store.db() as db:
        doc = service.queue.get(db, owner, job_id)
    if doc['state'] != 'BLOCKED' or doc.get('archived'):
        raise ValueError('Only blocked owned jobs can be rechecked')
    if clocks.remaining(doc) <= 0:
        return {'state': 'BLOCKED', 'ready': False, 'reason': 'execution_budget_exhausted', 'resumed': False}
    session = service.sessions.open(owner)['session_id']
    principal = replace(service._principal(doc), session=session)
    token = principal_context.set(principal)
    evidence, issues = [], []
    try:
        from .recovery import resolve_conditions
        conditions = [c for p in doc.get('milestones', []) for c in p['criteria']] + doc['criteria']
        conditions += [c for n in doc.get('graph',[]) if doc.get('graph_states',{}).get(n['id'],{}).get('state')=='DONE' for c in n['criteria']]
        deadline = time.monotonic() + min(30, clocks.remaining(doc))
        for condition in resolve_conditions(doc, conditions):
            service._check_action(doc, {k: condition[k] for k in ('tool', 'arguments')}, principal)
            try:
                _, observed = await service.engine._observe([condition], principal, deadline)
                evidence += observed
            except Exception as exc:
                evidence.append({'passed': False, 'tool': condition['tool'], 'error_type': type(exc).__name__})
        quality_doc = {k: doc[k] for k in ('workspace', 'allowed_tools', 'allowed_hosts')}
        quality_doc['validators'] = doc.get('validators', [])
        quality_doc['_deadline'] = deadline
        quality = await service._quality(quality_doc, principal)
        ready = bool(evidence) and all(e['passed'] for e in evidence) and quality
        for node in doc.get('graph',[]):
            chosen=doc.get('graph_states',{}).get(node['id'],{}).get('predicate')
            if chosen is not None and node['when']:
                try:
                    passed, observed = await service.engine._observe(node['when'],principal,deadline)
                    if passed!=chosen:
                        ready=False;issues.append('frozen_branch_changed')
                except Exception:
                    ready=False;issues.append('branch_evidence_unavailable')
        if not ready:
            issues.append('frozen_requirements_not_met')
        mission = service.engine.status(principal, doc['mission_id']) if doc.get('mission_id') else None
        if mission and (mission.get('reason') == 'owner_approval_required' or mission['state'] in {'RUNNING', 'PAUSED', 'CANCELLED', 'FAILED'}):
            ready = False
            issues.append('mission_requires_owner_inspection')
        if ready and mission and any(s['state'] == 'UNKNOWN' for s in mission['steps']):
            # Verify only. The engine never resubmits an UNKNOWN action here.
            if resume_if_verified:
                result = await service.engine.verify(principal, mission['id'], 0)
                ready = result['state'] == 'PENDING'
            else:
                ready = False
            if not ready:
                issues.append('unknown_delivery_not_reconciled')
        if any(service.engine.pending.values()):
            ready = False
            issues.append('provider_cleanup_pending')
        cleared_proposal = False
        if ready and resume_if_verified and mission and mission['state'] != 'COMPLETED':
            if all(s['state'] == 'PENDING' for s in mission['steps']):
                service.engine.control(principal, mission['id'], 'cancel')
                cleared_proposal = True
            elif all(s['state'] == 'VERIFIED' for s in mission['steps']):
                result = await service.engine.verify(principal, mission['id'])
                ready = result['state'] == 'COMPLETED'
        resumed = False
        with service.queue.store.db() as db:
            current = service.queue.get(db, owner, job_id)
            if current['updated'] != doc['updated'] or current['state'] != 'BLOCKED':
                raise ValueError('Task changed while rechecking; inspect fresh state')
            if ready and resume_if_verified:
                if cleared_proposal:
                    # Settled, never-dispatched proposal is unnecessary: cancel it
                    # before allowing the normal goal verifier to finish the job.
                    current['mission_id'], current['decision'] = None, None
                current['state'], current['reason'] = 'QUEUED', 'requirements_rechecked'
                current['requested_action'] = None
                current['recheck_count'] = current.get('recheck_count', 0) + 1
                service.queue.save(db, current, 'read_only_recheck_resumed', preserve_control=False)
                resumed = True
        return {'state': 'QUEUED' if resumed else 'BLOCKED', 'ready': ready, 'resumed': resumed,
                'issues': issues, 'goal_evidence': evidence, 'quality_evidence': quality_doc.get('quality_evidence', []),
                'model_calls': 0, 'mutation_replays': 0}
    finally:
        principal_context.reset(token)
        service.sessions.close(owner, session)
