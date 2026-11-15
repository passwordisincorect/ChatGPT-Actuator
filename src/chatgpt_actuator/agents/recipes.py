"""Explicit file contracts rendered without inference; execution remains a Mission."""
import csv
import io
import json
import time

from .intake import output_conditions
from .quality import contracts, evaluate
from ..missions.store import encode


def prepare(workspace, spec):
    if not isinstance(spec, dict):
        raise ValueError('Recipe must be an explicit object')
    kind, validators = spec.get('kind'), []
    if kind == 'files' and set(spec) == {'kind','outputs'}:
        outputs = spec['outputs']
        if not isinstance(outputs, list) or any(not isinstance(o,dict) or set(o) != {'path','text'} for o in outputs):
            raise ValueError('File recipe requires exact path/text outputs')
    elif kind == 'bom_csv' and set(spec) == {'kind','path','expected'}:
        validators = contracts(workspace, [spec])
        stream = io.StringIO(newline='')
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(['part','quantity','unit'])
        for row in spec['expected']:
            writer.writerow([row['part'],row['quantity'],row['unit']])
        outputs = [{'path':spec['path'],'text':stream.getvalue()}]
    elif kind == 'json_report' and set(spec) == {'kind','path','document','contract'}:
        if not isinstance(spec['contract'],dict) or set(spec['contract'])!={'fields','nonempty','expected'}:
            raise ValueError('JSON recipe contract requires only fields, nonempty and expected')
        validators = contracts(workspace, [{'kind':kind,'path':spec['path'],**spec['contract']}])
        text = json.dumps(spec['document'],ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n'
        if not evaluate(validators[0],text)['passed']:
            raise ValueError('Declared JSON document does not meet its contract')
        outputs = [{'path':spec['path'],'text':text}]
    else:
        raise ValueError('Supported recipes: files, bom_csv, json_report with explicit values')
    criteria = output_conditions(workspace, outputs)
    normalized = [{'path':c['arguments']['path'],'text':c['value']} for c in criteria]
    result = {'kind':kind,'outputs':normalized,'validators':validators}
    if len(encode(result).encode()) > 32768:
        raise ValueError('Recipe exceeds 32 KiB')
    return result, criteria


def current(doc):
    if doc.get('graph') and doc.get('graph_current'):
        return next(n.get('recipe') for n in doc['graph'] if n['id'] == doc['graph_current'])
    return doc.get('recipe')


async def decide(service, doc, principal):
    recipe = current(doc)
    failed = {e.get('arguments',{}).get('path') for e in doc.get('goal_evidence',[]) if not e.get('passed')}
    output = next((o for o in recipe['outputs'] if o['path'] in failed), None)
    if output is None:
        raise RuntimeError('Recipe cannot repair unmet requirements outside its exact outputs')
    read = {'tool':'filesystem_read_text','arguments':{'path':output['path']}}
    service._check_action(doc, read, principal)
    arguments = {'path':output['path'],'content':output['text'],'overwrite':False}
    required=recipe.get('required_sha256',{}).get(output['path'])
    try:
        value = await service.engine._call(read, principal, time.monotonic()+10)
        if not isinstance(value.get('sha256'),str) or value.get('truncated'):
            raise RuntimeError('Recipe requires a complete native SHA256 observation')
        if required is not None and value['sha256']!=required:
            raise RuntimeError('Restoration CAS baseline changed; preserve the newer user content')
        arguments.update(overwrite=True,expected_sha256=value['sha256'])
    except FileNotFoundError:
        if required is not None:
            raise RuntimeError('Restoration CAS baseline was deleted; preserve the user deletion')
    criterion = {'tool':'filesystem_read_text','arguments':{'path':output['path']},
                 'path':['content'],'op':'eq','value':output['text']}
    return {'kind':'step','reason':'Apply declared deterministic recipe','tool':'filesystem_write_text',
            'arguments_json':encode(arguments),'preconditions_json':'[]','postconditions_json':encode([criterion])}
