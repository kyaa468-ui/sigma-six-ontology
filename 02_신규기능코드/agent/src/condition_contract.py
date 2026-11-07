"""One typed, three-valued condition dialect for research and runtime policy.

No expression is executable code. All branches are checked before evaluation;
unknown inputs and invalid arithmetic remain unknown rather than becoming zero.
"""
from __future__ import annotations

import math

VERSION = 'POLICY_CONDITION@v1'
FIELDS = {
    'features.gap':'ratio', 'features.slope':'ratio', 'features.foreign_ratio':'ratio',
    'features.institution_ratio':'ratio', 'features.regime':'text',
    'signed_flows.foreign':'KRW', 'signed_flows.institution':'KRW',
    'signed_flows.foreign_direction':'text', 'signed_flows.institution_direction':'text',
    'signed_flows.same_period_confirmed':'bool', 'stratum.price_kind':'text',
    'stratum.flow_method':'text', 'stratum.flow_comparability':'text',
    'stratum.admission_route':'text', 'stratum.instrument_kind':'text',
    'stratum.confidence_for':'text', 'adoption.explicitly_uses_above_ma20_and_foreign_buy':'bool',
}
NUMERIC = {'ratio','KRW','confidence','trading_days','weekday_count','score_points','number'}
PRE_JUDGMENT = {'briefing','candidate','candidate_screen','ai_interpretation','before_judgment'}


def validate_condition(expr, fields=None, stage=None, depth=0):
    fields = FIELDS if fields is None else fields
    if depth > 16 or not isinstance(expr, dict):
        raise ValueError('INVALID_CONDITION')
    if set(expr) == {'field'}:
        field = expr['field']
        if not isinstance(field,str) or field not in fields:
            raise ValueError('FORBIDDEN_CONDITION_FIELD')
        if stage in PRE_JUDGMENT and field.startswith('adoption.'):
            raise ValueError('FIELD_UNAVAILABLE_AT_STAGE')
        return fields[field]
    if set(expr) == {'value','unit'}:
        value, unit = expr['value'], expr['unit']
        if unit == 'bool' and type(value) is bool or unit == 'text' and isinstance(value,str):
            return unit
        if unit not in NUMERIC or type(value) not in (int,float) or not math.isfinite(value):
            raise ValueError('CONDITION_UNIT_MISMATCH')
        return unit
    if set(expr) != {'op','left','right'}:
        raise ValueError('INVALID_CONDITION')
    left = validate_condition(expr['left'],fields,stage,depth+1)
    right = validate_condition(expr['right'],fields,stage,depth+1)
    op = expr['op']
    if op in ('and','or'):
        if left != 'bool' or right != 'bool': raise ValueError('CONDITION_TYPE_MISMATCH')
        return 'bool'
    if op in ('gt','gte','lt','lte','eq','ne'):
        if left != right or op not in ('eq','ne') and left not in NUMERIC:
            raise ValueError('CONDITION_UNIT_MISMATCH')
        return 'bool'
    if op in ('add','sub') and left == right and left in NUMERIC: return left
    if op == 'mul' and left in NUMERIC and right in ('number','ratio'): return left
    if op == 'div' and left in NUMERIC and right in NUMERIC:
        if left == right: return 'ratio'
        if right in ('number','ratio'): return left
    raise ValueError('UNSUPPORTED_CONDITION_OPERATOR')


def _field(case, name):
    if name in case: return case[name]
    value = case
    for part in name.split('.'):
        if not isinstance(value,dict): return None
        value = value.get(part)
    return value


def _value(expr, case, fields):
    if 'field' in expr:
        result, unit = _field(case,expr['field']), fields[expr['field']]
        valid = (type(result) is bool if unit == 'bool' else isinstance(result,str) if unit == 'text'
                 else type(result) in (int,float) and math.isfinite(result))
        return result if valid else None
    if 'value' in expr: return expr['value']
    a, b = _value(expr['left'],case,fields), _value(expr['right'],case,fields)
    op = expr['op']
    if op == 'and': return False if a is False or b is False else None if a is None or b is None else True
    if op == 'or': return True if a is True or b is True else None if a is None or b is None else False
    if a is None or b is None: return None
    if op == 'div' and b == 0: return None
    try:
        result = {'gt':lambda:a>b,'gte':lambda:a>=b,'lt':lambda:a<b,'lte':lambda:a<=b,
                  'eq':lambda:a==b,'ne':lambda:a!=b,'add':lambda:a+b,'sub':lambda:a-b,
                  'mul':lambda:a*b,'div':lambda:a/b}[op]()
        return None if isinstance(result,float) and not math.isfinite(result) else result
    except (ArithmeticError,TypeError): return None


def evaluate_condition(expr, case, fields=None, stage=None):
    fields = FIELDS if fields is None else fields
    validate_condition(expr,fields,stage)
    return _value(expr,case,fields)


validate = validate_condition
evaluate = evaluate_condition
condition_value = evaluate_condition
