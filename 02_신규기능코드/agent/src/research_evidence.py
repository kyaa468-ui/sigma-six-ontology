"""Typed frozen research references and conservative discovery-use accounting.

A stored comparison is not a delivered comparison. Callers must persist their
actual delivery and pass only that displayed catalog to validate_claim(). No
read here fetches market data or repairs source history.
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation

import common as C
import case_store as S

VERSION = 'RESEARCH_EVIDENCE@v1'


def _lookup(value, path):
    for part in path.strip('/').split('/'):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def _descriptor(result, metric):
    path, value = metric['path'], metric['value']
    leaf = path.rsplit('/', 1)[-1]
    meta = {'metric_kind': 'stored_text' if isinstance(value, str) else 'boolean' if isinstance(value, bool) else 'computed_value',
            'feature_name': None, 'model_id': None, 'stratum': None, 'denominator': None,
            'unit': 'text' if value is None or isinstance(value, str) else 'bool' if isinstance(value, bool) else 'number'}
    model_scope = re.match(r'(/(?:statistics/models/\d+|retrospective/folds/\d+/models/\d+))(?:/|$)', path)
    if model_scope:
        model = _lookup(result,model_scope[1])
        meta.update(model_id=model['model_id'],stratum=copy.deepcopy(model.get('stratum')),
                    outcome='premise_refuted',model_version=model.get('model_version'))
        if leaf == 'status':
            meta['metric_kind'] = 'model_status'
    model_match = re.search(r'(/(?:statistics/models/\d+|retrospective/folds/\d+/models/\d+))/(?:coefficients/(\d+)|uncertainty/(primary|sensitivity_2L)/coefficient_intervals/(\d+)/(0|1))$', path)
    if model_match:
        model = _lookup(result, model_match[1])
        column = model['columns'][int(model_match[2] or model_match[4])]
        meta.update(metric_kind='model_coefficient' if model_match[2] else 'coefficient_interval_bound', feature_name=column,
                    model_id=model['model_id'], stratum=copy.deepcopy(model.get('stratum')),
                    unit='log_odds' if column == 'intercept' or column.startswith('regime:') else 'log_odds_per_standardized_feature',
                    outcome='premise_refuted', transform=copy.deepcopy(model.get('transform')))
        if model_match[3]:
            meta.update(uncertainty_variant=model_match[3],interval_bound='lower' if model_match[5]=='0' else 'upper')
    elif leaf == 'refutation_rate':
        group = _lookup(result, path.rsplit('/', 1)[0])
        meta.update(metric_kind='refutation_rate', unit='ratio',
                    denominator={'value': group.get('complete_count'), 'kind': 'ended_and_complete_cases'},
                    numerator={'value': group.get('refuted_count'), 'kind': 'refuted_cases'},
                    stratum=copy.deepcopy(group.get('conditions')))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if '/sample_guard/' in path and leaf in ('instruments','judgment_dates','parameters','block_length'):
            meta.update(metric_kind='count', unit={'instruments':'instruments','judgment_dates':'trading_days',
                         'parameters':'parameters','block_length':'trading_days'}[leaf])
        elif (leaf.endswith('_count') or leaf in ('count','cases','each_label')
              or '/states/' in path or '/counts/' in path or '/sample_guard/actual/labels/' in path):
            meta.update(metric_kind='count', unit='cases')
        elif leaf in ('roc_auc', 'auc', 'brier', 'log_loss'):
            meta.update(metric_kind='prediction_metric', unit='ratio' if leaf in ('roc_auc', 'auc', 'brier') else 'nat', feature_name=leaf)
        elif ('probability' in leaf or leaf.endswith('_rate') or leaf.endswith('_fraction')):
            meta.update(metric_kind='ratio', unit='ratio')
    return meta


def build_catalog(review_id, result, dataset):
    """Qualify every legacy path-ID with its immutable result and meaning."""
    if result.get('review_id') != review_id or dataset.get('review_id', review_id) != review_id:
        raise ValueError('REVIEW_MISMATCH')
    result_hash = S.digest(result)
    output, seen = [], set()
    for metric in result.get('metric_catalog', []):
        mid = metric['metric_id']
        if mid in seen:
            raise ValueError('DUPLICATE_METRIC_ID')
        seen.add(mid)
        value = metric['value']
        try:
            actual = _lookup(result, metric['path'])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError('METRIC_PATH_UNAVAILABLE') from exc
        if type(actual) is not type(value) or actual != value:
            raise ValueError('METRIC_VALUE_MISMATCH')
        descriptor = _descriptor(result, metric)
        output.append({**copy.deepcopy(metric), **descriptor,
                       'reference': {'review_id': review_id, 'result_hash': result_hash, 'metric_id': mid},
                       'evidence_version': VERSION, 'server_label': metric['path'],
                       'status': 'unknown' if value is None else 'computed'})
    # Cases are not silently put into a summary delivery. The proposal page
    # provider decides which of these are actually returned to the AI.
    for case in dataset.get('cases', []):
        identifier = case['application_id']
        values = [('label', case.get('label'), 'category', 'premise_label'),
                  ('verdict', case.get('verdict'), 'text', 'premise_verdict'),
                  ('outcome_eligible', case.get('outcome_eligible'), 'bool', 'outcome_eligibility')]
        for field, value in (case.get('features') or {}).items():
            values.append(('features.'+field, value, 'text' if field == 'regime' else 'ratio', 'origin_feature'))
        for field in ('foreign', 'institution'):
            values.append(('signed_flows.'+field, (case.get('signed_flows') or {}).get(field), 'KRW', 'signed_flow'))
        for field, value, unit, kind in values:
            mid = 'case-metric-' + S.digest([identifier, field])[:32]
            output.append({'metric_id': mid, 'path': '/cases/'+identifier+'/'+field,
                           'reference': {'review_id': review_id, 'result_hash': result_hash, 'metric_id': mid},
                           'value': value, 'display_value': round(value, 6) if isinstance(value, float) else value,
                           'unit': unit, 'precision': 6 if isinstance(value, float) else None,
                           'status': 'unknown' if value is None else 'computed', 'metric_kind': kind,
                           'feature_name': field, 'model_id': None, 'application_id': identifier,
                           'stratum': copy.deepcopy(case.get('stratum')), 'denominator': None,
                           'temporal_scope': (case.get('signed_flows') or {}).get('interpretation'),
                           'evidence_version': VERSION, 'server_label': identifier+':'+field})
    return output


def _referenced(reference, catalog):
    if not isinstance(reference, dict) or set(reference) != {'review_id','result_hash','metric_id'}:
        raise ValueError('COMPOUND_REFERENCE_REQUIRED')
    rows = [item for item in catalog if item.get('reference') == reference]
    if len(rows) != 1:
        raise ValueError('NOT_DELIVERED')
    return rows[0]


def _numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('NUMBER_REQUIRED')
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('NONFINITE_NUMBER')
    return number


def _normalized(value, supplied, expected):
    number = _numeric(value)
    units = {'KRW': ('money', Decimal(1)), '원': ('money',Decimal(1)),
             '억 원': ('money',Decimal(100000000)), '억원': ('money',Decimal(100000000)),
             'ratio': ('ratio',Decimal(1)), '%': ('ratio',Decimal('.01')), 'percent': ('ratio',Decimal('.01'))}
    if supplied == expected:
        return number
    if supplied not in units or expected not in units or units[supplied][0] != units[expected][0]:
        raise ValueError('UNIT_MISMATCH')
    return number * units[supplied][1] / units[expected][1]


def validate_claim(claim, catalog):
    """Machine-check only structured assertions; no free sentence is certified."""
    allowed = {'id','reference','op','value','unit','direction','feature_name','metric_kind',
               'model_id','outcome','interval_bound','uncertainty_variant','right_reference','operator','result'}
    if not isinstance(claim, dict) or set(claim) - allowed or not isinstance(claim.get('id'),str) or not claim['id']:
        raise ValueError('SCHEMA_ERROR')
    op = claim.get('op', 'value')
    fields = {'value': {'value','unit'}, 'direction': {'direction'},
              'compare': {'right_reference','operator','result'}}
    if op not in fields:
        raise ValueError('UNSUPPORTED_ASSERTION')
    if (set(claim) & {'value','unit','direction','right_reference','operator','result'}) - fields[op]:
        raise ValueError('SCHEMA_ERROR')
    required = fields[op] - ({'unit'} if op == 'value' else set())
    if not required <= set(claim):
        raise ValueError('SCHEMA_ERROR')
    source = _referenced(claim.get('reference'), catalog)
    if source.get('metric_kind') in ('model_coefficient','coefficient_interval_bound','origin_feature','signed_flow') and claim.get('feature_name') != source.get('feature_name'):
        raise ValueError('FEATURE_MISMATCH')
    if 'feature_name' in claim and claim['feature_name'] != source.get('feature_name'):
        raise ValueError('FEATURE_MISMATCH')
    for key in ('metric_kind','model_id','outcome','interval_bound','uncertainty_variant'):
        if key in claim and claim[key] != source.get(key):
            raise ValueError('MEANING_MISMATCH')
    value = source['value']
    if op == 'value':
        actual = claim.get('value')
        if 'unit' in claim and (value is None or isinstance(value,(str,bool))) and claim['unit'] != source['unit']:
            raise ValueError('UNIT_MISMATCH')
        if value is None:
            if actual is not None:
                raise ValueError('UNKNOWN_AS_ZERO' if actual == 0 else 'FACT_MISMATCH')
        elif isinstance(value, (int,float)) and not isinstance(value,bool):
            normalized = _normalized(actual, claim.get('unit', source['unit']), source['unit'])
            expected = _numeric(value)
            precision = source.get('precision')
            if precision is not None:
                scale = Decimal(1).scaleb(-int(precision))
                try:
                    normalized, expected = normalized.quantize(scale), expected.quantize(scale)
                except InvalidOperation as exc:
                    raise ValueError('FACT_MISMATCH') from exc
            if normalized != expected:
                raise ValueError('FACT_MISMATCH')
        elif type(actual) is not type(value) or actual != value:
            raise ValueError('FACT_MISMATCH')
    elif op == 'direction':
        if value is None:
            raise ValueError('UNKNOWN_AS_ZERO')
        number = _numeric(value)
        direction = 'positive' if number > 0 else 'negative' if number < 0 else 'zero'
        if claim.get('direction') != direction:
            raise ValueError('FACT_MISMATCH')
    elif op == 'compare':
        other = _referenced(claim.get('right_reference'), catalog)
        if (source['unit'] != other['unit'] or source.get('metric_kind') != other.get('metric_kind')
                or source.get('feature_name') != other.get('feature_name')
                or source.get('stratum') != other.get('stratum')):
            raise ValueError('COMPARISON_MEANING_MISMATCH')
        a, b = _numeric(value), _numeric(other['value'])
        operations = {'gt':a>b,'gte':a>=b,'lt':a<b,'lte':a<=b,'eq':a==b}
        if claim.get('operator') not in operations or type(claim.get('result')) is not bool or claim['result'] != operations[claim['operator']]:
            raise ValueError('FACT_MISMATCH')
    else:
        raise ValueError('UNSUPPORTED_ASSERTION')
    return {**copy.deepcopy(claim), 'reference_validation':'passed', 'fact_validation':'passed',
            'evidence_snapshot': copy.deepcopy(source),
            'server_summary': f"{source['server_label']}: {source.get('display_value',value)} {source['unit']} (보관된 계산값; 인과·매매 효과 미검증)"}


def _use(owner_id, application_ids, source_refs, purpose, kind):
    if not isinstance(owner_id,str) or not owner_id or not isinstance(purpose,str) or not purpose:
        raise ValueError('USAGE_ID_AND_PURPOSE_REQUIRED')
    if not isinstance(application_ids,(list,tuple)) or any(not isinstance(x,str) or not x for x in application_ids):
        raise ValueError('INVALID_APPLICATION_IDS')
    import validation_holdout as VH
    VH.assert_discovery_allowed(application_ids)
    ids = sorted(set(application_ids))
    base = {'owner_id':owner_id, 'application_ids':ids, 'source_refs':copy.deepcopy(source_refs or []),
            'purpose':purpose, 'kind':kind, 'version':'RESEARCH_USAGE@v1'}
    uid = 'usage-'+S.digest(base)
    old = S.load('usage_events',uid)
    if old:
        return old
    body = {**base,'usage_id':uid,'created_at':C.now().isoformat()}
    # Serializing the same id is short; a same-content retry never rewrites time.
    with S.locked(uid):
        old = S.load('usage_events',uid)
        return old if old else S.archive('usage_events',uid,body)


def reserve_usage(owner_id, application_ids, source_refs=None, purpose='discovery'):
    """Must be durable before analysis/presentation; failure prevents exposure."""
    return _use(owner_id,application_ids,source_refs,purpose,'reserved')


def record_usage(owner_id, application_ids, purpose, source_refs=None):
    return _use(owner_id,application_ids,source_refs,purpose,'used')


def usage_status(application_ids):
    """Absence is unknown, never evidence of an independent unseen holdout."""
    if not isinstance(application_ids,(list,tuple)) or any(not isinstance(x,str) or not x for x in application_ids):
        raise ValueError('INVALID_APPLICATION_IDS')
    wanted = sorted(set(application_ids))
    reads = {kind:S.records(kind) for kind in ('usage_events','research_usage','datasets')}
    errors = [e for value in reads.values() for e in value['errors']]
    references = {key:[] for key in wanted}
    for kind,value in reads.items():
        for row in value['records']:
            try:
                if kind == 'datasets':
                    cases = row['cases']
                    if not isinstance(cases,list) or any(not isinstance(c,dict) for c in cases):
                        raise ValueError('invalid archived cases')
                    ids = [c['application_id'] for c in cases]
                else:
                    ids = row['application_ids']
                if not isinstance(ids,list) or any(not isinstance(x,str) or not x for x in ids):
                    raise ValueError('invalid archived application IDs')
                if kind == 'usage_events' and (row.get('version') != 'RESEARCH_USAGE@v1'
                                               or row.get('kind') not in ('reserved','used')):
                    raise ValueError('unknown research usage schema')
            except (KeyError,TypeError,ValueError) as exc:
                errors.append({'kind':kind,'reason':'usage_history_schema_unknown','error':str(exc)})
                continue
            for identifier in set(ids) & set(wanted):
                references[identifier].append({'kind':kind,'owner_id':row.get('owner_id',row.get('review_id')),
                    'usage_id':row.get('usage_id'),'purpose':row.get('purpose'), 'sha256':S.digest(row)})
    return {'items':[{'application_id':key, 'status':'used' if references[key] else 'unknown',
                     'independent_unused':False, 'references':references[key],
                     'reason':'discovery_reservation_or_archived_dataset' if references[key] else
                              'usage_history_corrupt' if errors else 'no_proof_of_nonuse'} for key in wanted],
            'recording_status':'recording_error' if errors else 'recorded','errors':errors}
