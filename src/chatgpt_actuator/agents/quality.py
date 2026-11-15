"""Frozen deterministic BOM/report contracts, evaluated through secured read tools."""
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import math
from pathlib import Path
import time

from ..missions.store import encode


def quantity(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise ValueError('Quantity must be a bounded decimal string')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError('Invalid decimal quantity') from None
    if not number.is_finite() or number <= 0 or number > 1000000000:
        raise ValueError('Quantity outside supported bounds')
    return number


def contracts(workspace, values):
    if not isinstance(values, list) or not 1 <= len(values) <= 10:
        raise ValueError('Provide 1..10 quality contracts')
    root = Path(workspace).resolve(strict=True)
    result = []
    for spec in values:
        if not isinstance(spec, dict) or spec.get('kind') not in {'bom_csv', 'json_report'}:
            raise ValueError('Quality contract must be bom_csv or json_report')
        if not isinstance(spec.get('path'), str) or not spec['path']:
            raise ValueError('Quality contract requires a path')
        path = (root / spec['path']).resolve()
        if root not in path.parents:
            raise PermissionError('Quality path escaped the workspace')
        if spec['kind'] == 'bom_csv':
            if set(spec) != {'kind', 'path', 'expected'} or not isinstance(spec['expected'], list) or not 1 <= len(spec['expected']) <= 1000:
                raise ValueError('BOM requires 1..1000 explicit expected rows')
            parts = set()
            for row in spec['expected']:
                if not isinstance(row, dict) or set(row) != {'part', 'quantity', 'unit'}:
                    raise ValueError('Expected BOM rows require part, quantity, unit')
                if any(not isinstance(row[k], str) or not row[k].strip() or row[k] != row[k].strip()
                       or len(row[k]) > 200 for k in ('part', 'unit')) or row['part'] in parts:
                    raise ValueError('Explicit unique part and unit required')
                quantity(row['quantity'])
                parts.add(row['part'])
        else:
            if set(spec) != {'kind', 'path', 'fields', 'nonempty', 'expected'}:
                raise ValueError('JSON report requires fields, nonempty and expected objects')
            fields = spec['fields']
            if not isinstance(fields, dict) or not 1 <= len(fields) <= 30 or any(
                    not isinstance(k, str) or not 1 <= len(k) <= 100 or v not in
                    {'string', 'number', 'array', 'object', 'boolean'} for k, v in fields.items()):
                raise ValueError('Provide bounded typed JSON fields')
            if not isinstance(spec['nonempty'], list) or any(k not in fields for k in spec['nonempty']):
                raise ValueError('nonempty must name declared fields')
            if not isinstance(spec['expected'], dict) or any(k not in fields for k in spec['expected']):
                raise ValueError('Expected values must name declared fields')
        result.append({**spec, 'path': str(path)})
    if len(encode(result).encode()) > 32768:
        raise ValueError('Quality contracts exceed 32 KiB')
    return result


def evaluate(spec, content):
    issues = []
    def issue(code, **detail):
        if len(issues) < 20:
            issues.append({'code': code, **detail})
    if not isinstance(content, str) or len(content.encode('utf-8')) > 262144:
        issue('content_size_exceeded')
    elif spec['kind'] == 'bom_csv':
        try:
            reader = csv.DictReader(io.StringIO(content), strict=True)
            names = reader.fieldnames or []
            if set(names) != {'part', 'quantity', 'unit'} or len(names) != 3:
                issue('bom_header_requires_part_quantity_unit')
            else:
                expected = {r['part']: r for r in spec['expected']}
                seen = set()
                for index, row in enumerate(reader):
                    if index >= 2000:
                        issue('bom_row_limit')
                        break
                    if None in row or any(v is None for v in row.values()):
                        issue('malformed_bom_row', row=index + 2)
                        continue
                    part = row['part'].strip()
                    if part in seen:
                        issue('duplicate_part', part=part[:200])
                    seen.add(part)
                    wanted = expected.get(part)
                    if wanted is None:
                        issue('unexpected_part', part=part[:200])
                        continue
                    try:
                        if quantity(row['quantity'].strip()) != quantity(wanted['quantity']):
                            issue('quantity_mismatch', part=part)
                    except ValueError:
                        issue('invalid_quantity', part=part)
                    if row['unit'].strip() != wanted['unit']:
                        issue('unit_mismatch', part=part)
                for part in sorted(set(expected) - seen)[:20]:
                    issue('missing_part', part=part)
        except csv.Error:
            issue('invalid_csv')
    else:
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('Duplicate JSON key')
                    result[key] = value
                return result
            def invalid_constant(value):
                raise ValueError('Nonfinite JSON constant')
            value = json.loads(content, object_pairs_hook=unique, parse_constant=invalid_constant)
            if not isinstance(value, dict):
                issue('report_requires_object')
            else:
                for key, kind in spec['fields'].items():
                    item = value.get(key)
                    valid = {'string': lambda: isinstance(item, str), 'number': lambda: type(item) in (int, float) and math.isfinite(item),
                             'array': lambda: isinstance(item, list), 'object': lambda: isinstance(item, dict),
                             'boolean': lambda: type(item) is bool}[kind]()
                    if key not in value or not valid:
                        issue('field_type_mismatch', field=key)
                    elif key in spec['nonempty'] and (item is None or (isinstance(item, str) and not item.strip()) or item == [] or item == {}):
                        issue('empty_required_field', field=key)
                for key, wanted in spec['expected'].items():
                    if key not in value or encode(value[key]) != encode(wanted):
                        issue('expected_value_mismatch', field=key)
        except (ValueError, TypeError, RecursionError, OverflowError):
            issue('invalid_json')
    return {'kind': spec['kind'], 'path': spec['path'], 'passed': not issues, 'issues': issues,
            'observed_sha256': hashlib.sha256(content.encode()).hexdigest() if isinstance(content, str) else None,
            'observed_at': time.time()}
