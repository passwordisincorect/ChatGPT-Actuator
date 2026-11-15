"""Translate explicit output contracts into frozen evidence and narrow delegation."""
from pathlib import Path


FILE_TOOLS = ['filesystem_list', 'filesystem_stat', 'filesystem_read_text',
              'filesystem_write_text', 'filesystem_create_directory']


def output_conditions(workspace, outputs):
    root = Path(workspace).resolve(strict=True)
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 20:
        raise ValueError('Provide 1..20 file outputs with path and text/contains')
    conditions, paths = [], set()
    for item in outputs:
        if not isinstance(item, dict) or set(item) not in ({'path', 'text'}, {'path', 'contains'}):
            raise ValueError('Each output requires only path and text or contains')
        if not isinstance(item['path'], str) or not item['path']:
            raise ValueError('Output path is required')
        target = (root / item['path']).resolve()
        if target == root or root not in target.parents:
            raise PermissionError('Output path must stay inside the workspace')
        if target in paths:
            raise ValueError('Duplicate output path')
        paths.add(target)
        field = 'text' if 'text' in item else 'contains'
        value = item[field]
        if not isinstance(value, str) or len(value.encode('utf-8')) > 16384 or (field == 'contains' and not value):
            raise ValueError('Expected output text must be bounded; contains cannot be empty')
        conditions.append({'tool': 'filesystem_read_text', 'arguments': {'path': str(target)},
                           'path': ['content'], 'op': 'eq' if field == 'text' else 'contains', 'value': value})
    return conditions


def prepare(workspace, outputs, stages=None):
    """No model guesses success criteria or permissions. The chat supplies user intent."""
    final = output_conditions(workspace, outputs)
    milestones = []
    if stages is not None:
        if not isinstance(stages, list) or not 1 <= len(stages) <= 12:
            raise ValueError('Provide 1..12 ordered stages')
        for stage in stages:
            if not isinstance(stage, dict) or set(stage) != {'title', 'outputs'}:
                raise ValueError('Each stage requires title and outputs')
            milestones.append({'title': stage['title'], 'criteria': output_conditions(workspace, stage['outputs'])})
    count = len(milestones)
    calls = min(32, max(6, count * 3))
    return {'criteria': final, 'allowed_tools': list(FILE_TOOLS), 'milestones': milestones or None,
            'max_calls': calls, 'max_cycles': min(128, max(16, count * 10)),
            'max_seconds': min(28800, max(1200, count * 1800)), 'token_budget': calls * 30000,
            'context': {'intake_profile': 'file_outputs_v1',
                        'instruction': 'Create the declared file outputs. Use current goal_evidence: a missing output can be created with overwrite=false. For an existing file, read its current SHA256 before a guarded overwrite. Stage outputs are durable requirements. Only project_memory is historical; task observations are executor evidence.'}}
