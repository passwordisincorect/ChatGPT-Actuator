"""Evidence-derived metrics and bounded improvement suggestions; no self-modification."""
import collections
import json
import math
import statistics


def metrics(store, owner):
    with store.db() as db:
        docs = [json.loads(r[0]) for r in db.execute(
            'SELECT document FROM agent_jobs WHERE owner=? ORDER BY rowid DESC LIMIT 100', (owner,))]
    counts = dict(collections.Counter(d['state'] for d in docs))
    terminal = sum(counts.get(s, 0) for s in ('COMPLETED', 'FAILED', 'CANCELLED'))
    completed = counts.get('COMPLETED', 0)
    attempted = sum(bool(d.get('first_started_at') or d.get('started_at') or
                         d['state'] in {'COMPLETED', 'FAILED', 'BLOCKED'}) for d in docs)
    durations = sorted(max(0, d.get('elapsed_seconds') if d.get('elapsed_seconds') is not None else
                           d['updated'] - (d.get('first_started_at') or d.get('started_at')))
                       for d in docs if d['state'] == 'COMPLETED' and (d.get('first_started_at') or d.get('started_at')))
    reasons = collections.Counter((d.get('reason') or '').split(':', 1)[0][:100]
                                  for d in docs if d['state'] in {'BLOCKED', 'FAILED'})
    recommendations = []
    if any('budget' in k.casefold() for k in reasons):
        recommendations.append({'kind': 'reduce_planning_calls',
            'recommendation': 'Inspect repeated observations and split durable milestones before increasing budgets.'})
    if counts.get('BLOCKED'):
        recommendations.append({'kind': 'inspect_blocked_evidence',
            'recommendation': 'Inspect blocked Mission evidence; reconcile uncertain delivery without mutation replay.'})
    return {'sample_jobs': len(docs), 'counts': counts,
            'completion_fraction_of_terminal': completed / terminal if terminal else None,
            'terminal_jobs': terminal, 'blocked_jobs': counts.get('BLOCKED', 0),
            'attempted_jobs': attempted, 'completion_fraction_of_attempted': completed / attempted if attempted else None,
            'archived_jobs_in_sample': sum(bool(d.get('archived')) for d in docs),
            'provider_retries': sum(d.get('retry_count', 0) for d in docs),
            'planner_failures': sum(d.get('planner_failures', 0) for d in docs),
            'planner_seconds': sum(d.get('planner_seconds', 0) for d in docs),
            'fair_yields': sum(d.get('yield_count', 0) for d in docs),
            'worker_recoveries': sum(d.get('worker_recoveries', 0) for d in docs),
            'quality_failures': sum(d.get('quality_failures', 0) for d in docs),
            'completed_duration_p50_seconds': durations[(len(durations) - 1) // 2] if durations else None,
            'completed_duration_p95_seconds': durations[math.ceil(len(durations) * .95) - 1] if durations else None,
            'planner_calls': sum(d['planner_calls'] for d in docs),
            'prompt_bytes': sum(d.get('prompt_bytes', 0) for d in docs),
            'verified_rechecks': sum(d.get('recheck_count', 0) for d in docs),
            'recipe_proposals':sum(d.get('recipe_steps',0) for d in docs),
            'input_verifications':sum(d.get('input_checks',0) for d in docs),
            'input_signals':sum(d.get('input_signals',0) for d in docs),
            'workflow_nodes_done':sum(sum(n['state']=='DONE' for n in d.get('graph_nodes',[])) if d.get('archived') else
                sum(s.get('state')=='DONE' for s in d.get('graph_states',{}).values()) for d in docs),
            'reported_tokens': sum(d['reported_tokens'] for d in docs),
            'reserved_tokens': sum(d['reserved_tokens'] for d in docs),
            'browser_recoveries': sum(d.get('recovery_count', 0) for d in docs),
            'milestones_verified': sum(d.get('milestone_index', 0) for d in docs),
            'stop_reasons': dict(reasons), 'recommendations': recommendations,
            'source': 'persisted_execution_records', 'automatic_policy_changes': False}


def compare(baseline, candidate):
    """Compare paired, explicitly supplied acceptance cases; never change policy."""
    if not baseline or len(baseline) != len(candidate):
        raise ValueError('Comparable paired case lists required')
    for old, new in zip(baseline, candidate):
        if old['case'] != new['case']:
            raise ValueError('Case identities differ')
        for row in (old, new):
            for key in ('reported_tokens', 'planner_calls', 'seconds', 'mutation_replays'):
                if type(row[key]) not in (int, float) or not math.isfinite(row[key]) or row[key] < 0:
                    raise ValueError('Invalid measured benchmark value')
    def summarize(rows):
        return {'cases': len(rows), 'passed': sum(r['state'] == 'PASS' for r in rows),
                'reported_tokens': sum(r['reported_tokens'] for r in rows),
                'planner_calls': sum(r['planner_calls'] for r in rows),
                'median_seconds': statistics.median(r['seconds'] for r in rows),
                'mutation_replays': sum(r['mutation_replays'] for r in rows)}
    old, new = summarize(baseline), summarize(candidate)
    reduction = 1 - new['reported_tokens'] / old['reported_tokens'] if old['reported_tokens'] else None
    accepted = (new['passed'] == old['passed'] == len(candidate) and new['mutation_replays'] == 0 and
                new['planner_calls'] <= old['planner_calls'] and reduction is not None and reduction >= .25)
    return {'state': 'PASS' if accepted else 'REJECT', 'baseline': old, 'candidate': new,
            'token_reduction_fraction': reduction, 'cases': [r['case'] for r in candidate],
            'automatic_policy_changes': False, 'scope': 'Paired synthetic BOM repair samples; not a production-wide guarantee'}
