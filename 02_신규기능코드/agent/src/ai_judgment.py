"""Validate AI submissions against immutable, actually delivered evidence.

No market/model calls occur here. Verification applies to structured assertions,
never to the truth of quoted reporting or the AI's free-text explanation.
"""
from __future__ import annotations

import copy
import contextvars
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from urllib.parse import urlsplit

import common as C
import record_store
import storage_objects as objects

SCHEMA_VERSION = 'ai-judgment@v1'
_FROZEN = contextvars.ContextVar('frozen_judgment_validation', default=None)
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}\Z')
SCOPE_SUBJECTS = {'market', 'account', 'config', 'history'}
UNVERIFIED_KINDS = {'news', 'external_unverified'}
PREMISE_DEFINITION = {
    'definition_id': 'MA20_MAINTENANCE@v1',
    'start': 'next_trading_day_after_judgment',
    'duration_unit': 'trading_day',
    'price': 'completed_daily_close',
    'reference': 'same_trading_day_ma20',
    'above_refuted_when': 'close < ma20',
    'below_refuted_when': 'close > ma20',
    'equality': 'maintained',
    'scoring': 'not_implemented',
}
PREMISE_DEFINITION_V2 = {
    **PREMISE_DEFINITION,
    'definition_id': 'MA20_MAINTENANCE@v2',
    'scoring': 'MA20_MAINTENANCE_EVALUATOR@v2',
    'calendar': 'XKRX_versioned_sessions',
    'ma20': 'arithmetic_mean_of_20_exact_exchange_sessions_including_target',
    'comparison_precision': 'unrounded_decimal',
    'price_basis': {'equity': 'adjusted_close', 'etf': 'provider_published_close'},
    'source_policy': 'same_provider_and_price_basis_within_each_20_session_snapshot',
    'completion_policy': 'matching_full_20_close_window_on_later_trading_session',
    'provider_finality': 'separately_reported_not_implied_by_policy_completion',
    'missing_policy': 'unknown_no_fill_forward_no_horizon_extension',
}
PREMISE_DEFINITIONS = {x['definition_id']: x for x in (PREMISE_DEFINITION, PREMISE_DEFINITION_V2)}
UNIT = {
    'KRW': ('KRW', Decimal(1)), '원': ('KRW', Decimal(1)),
    '억 원': ('KRW', Decimal('100000000')), '억원': ('KRW', Decimal('100000000')),
    'ratio': ('ratio', Decimal(1)), '%': ('ratio', Decimal('.01')),
    'percent': ('ratio', Decimal('.01')), '퍼센트': ('ratio', Decimal('.01')),
    'share': ('share', Decimal(1)), '주': ('share', Decimal(1)),
    'point': ('point', Decimal(1)), '점': ('point', Decimal(1)),
    'day': ('day', Decimal(1)), 'count': ('count', Decimal(1)),
    'trading_day': ('trading_day', Decimal(1)), 'number': ('number', Decimal(1)),
    'weekday_count': ('weekday_count', Decimal(1)),
}


class InvalidSubmission(ValueError):
    def __init__(self, code, path, message):
        self.error = {'code': code, 'path': path, 'message': message}
        super().__init__(message)


def _fail(code, path, message):
    raise InvalidSubmission(code, path, message)


def _now():
    return C.now().isoformat(timespec='microseconds')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _digest(value):
    return objects.fingerprint(value)[0]


def _id(value, path):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value) or '..' in value:
        _fail('INVALID_ID', path, 'ASCII identifier required; path traversal is forbidden')
    return value


def _path(kind, identifier):
    _id(identifier, kind)
    return C.ontology_root() / kind / (identifier + '.json')


def _archive(kind, identifier, value):
    return record_store.publish(_path(kind, identifier), value, kind=kind)


def _load(kind, identifier):
    path = _path(kind, identifier)
    try:
        return record_store.load(path)
    except (OSError, ValueError):
        return None


def load_judgment(judgment_id):
    """Only committed judgments count as accepted; unfinished writes are invisible."""
    try:
        row = _load('judgments', judgment_id)
        if not row:
            return None
        event = _load('submissions', row['acceptance_event_id'])
        if not event or event.get('status') != 'accepted' or event.get('judgment_id') != judgment_id:
            return None
        return row
    except (InvalidSubmission, KeyError):
        return None


def _required_text(value, path):
    if not isinstance(value, str) or not value.strip():
        _fail('SCHEMA_ERROR', path, 'Nonempty text required')
    return value


def _number(value, path):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        _fail('SCHEMA_ERROR', path, 'Finite number required')
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise InvalidOperation
        return result
    except InvalidOperation:
        _fail('SCHEMA_ERROR', path, 'Finite number required')


def _numeric(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool)


def _converted(value, unit, path):
    if unit not in UNIT:
        _fail('UNSUPPORTED_UNIT', path, 'Unit is not registered for numeric normalization')
    dimension, factor = UNIT[unit]
    return _number(value, path) * factor, dimension


def _time_not_future(value, path):
    if value in (None, '', 'not_provided', '미제공'):
        return
    try:
        at = (_FROZEN.get() or {}).get('at') or C.now()
        # Dates identify an observation's trading day, not a claimed release time.
        if len(value) == 10:
            if datetime.fromisoformat(value).date() > at.date():
                _fail('FUTURE_EVIDENCE', path, 'Observation is dated after submission')
            return
        when = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if when.tzinfo is None:
            _fail('INVALID_TIME', path, 'Datetime must include its timezone')
        if when > at:
            _fail('FUTURE_EVIDENCE', path, 'Evidence did not exist at submission time')
    except InvalidSubmission:
        raise
    except (ValueError, TypeError, AttributeError):
        _fail('INVALID_TIME', path, 'Invalid evidence timestamp')


def _receipt(delivery_id, path, context):
    _id(delivery_id, path)
    frozen = _FROZEN.get()
    cache=context.setdefault('_receipt_cache',{})
    if delivery_id not in cache:
        cache[delivery_id] = frozen['receipts'].get(delivery_id) if frozen is not None else _load('deliveries', delivery_id)
    receipt=cache[delivery_id]
    if not receipt:
        _fail('UNKNOWN_DELIVERY', path, 'No persisted delivery receipt')
    if receipt.get('run_attempt_id') != context['run_attempt_id']:
        _fail('ATTEMPT_MISMATCH', path, 'Delivery belongs to another run attempt')
    if receipt.get('ai_run_id', receipt.get('run_id')) != context['run_id']:
        _fail('RUN_MISMATCH', path, 'Delivery belongs to another AI run')
    if receipt.get('delivery_id', delivery_id) != delivery_id:
        _fail('REFERENCE_MISMATCH', path, 'Receipt ID mismatch')
    _time_not_future(receipt.get('created_at'), path + '.created_at')
    return receipt


def _evidence(ref, path, context):
    if not isinstance(ref, dict):
        _fail('SCHEMA_ERROR', path, 'Evidence reference requires delivery_id and evidence_id')
    delivery = ref.get('delivery_id')
    if delivery not in context['delivery_ids']:
        _fail('DELIVERY_NOT_DECLARED', path + '.delivery_id', 'Reference must be in delivery_ids')
    receipt = _receipt(delivery, path + '.delivery_id', context)
    eid = _id(ref.get('evidence_id'), path + '.evidence_id')
    catalogs=context.setdefault('_catalog_indices',{})
    if delivery not in catalogs:
        index={}
        for entry in receipt.get('evidence_catalog',[]):
            index.setdefault(entry.get('evidence_id'),[]).append(entry)
        catalogs[delivery]=index
    matches = catalogs[delivery].get(eid,[])
    if len(matches) != 1:
        _fail('NOT_DELIVERED', path + '.evidence_id', 'Only a unique explicitly displayed catalog item can be cited')
    evidence = matches[0]
    if evidence.get('subject') not in {context['code']} | SCOPE_SUBJECTS:
        _fail('SUBJECT_MISMATCH', path, 'Evidence belongs to another instrument')
    if evidence.get('kind') == 'adopted_lesson':
        try:
            import lesson_registry
            frozen = evidence.get('lesson_context') or {}
            lesson_registry.validate_frozen_lesson(frozen, context['code'], receipt['created_at'], context.get('confidence_for'))
            if evidence.get('value') != frozen['text'] or evidence.get('subject') != frozen['code']:
                raise ValueError('lesson_catalog_mismatch')
            for key in ('lesson_version_id', 'proposal_id', 'adoption_id', 'validation_id', 'confidence_for', 'source_case_ids'):
                if evidence.get(key) != frozen.get(key):
                    raise ValueError('lesson_catalog_metadata_mismatch')
        except (ValueError, KeyError, TypeError) as exc:
            _fail('LESSON_APPLICABILITY_MISMATCH', path, str(exc))
    for field in ('observed_at', 'available_at'):
        _time_not_future(evidence.get(field), path + '.' + field)
        if field in ref and ref[field] != evidence.get(field):
            _fail('TIME_MISMATCH', path + '.' + field, 'Claimed evidence time differs from receipt')
    for field in ('observation_id', 'subject', 'field'):
        if field in ref and ref[field] != evidence.get(field):
            _fail('REFERENCE_MISMATCH', path + '.' + field, 'Claimed reference metadata differs from receipt')
    return copy.deepcopy(evidence)


def _value_matches(assertion, evidence, path):
    expected = evidence.get('value')
    value = assertion.get('value')
    if 'value' not in assertion or 'unit' not in assertion:
        _fail('STRUCTURED_ASSERTION_REQUIRED', path, 'Value assertions require value and unit')
    if _numeric(expected):
        actual, dimension = _converted(value, assertion['unit'], path + '.unit')
        wanted, wanted_dimension = _converted(expected, evidence.get('unit'), path + '.unit')
        if dimension != wanted_dimension:
            _fail('UNIT_MISMATCH', path + '.unit', 'Incompatible units')
        precision = evidence.get('precision')
        if isinstance(precision, int) and not isinstance(precision, bool) and 0 <= precision <= 12:
            # Precision is in the catalog's own unit; sign remains strictly exact elsewhere.
            factor = UNIT[evidence['unit']][1]
            quantum = Decimal(1).scaleb(-precision)
            equal = (actual / factor).quantize(quantum, rounding=ROUND_HALF_EVEN) == (wanted / factor).quantize(quantum, rounding=ROUND_HALF_EVEN)
        else:
            equal = actual == wanted
        if not equal:
            _fail('FACT_MISMATCH', path + '.value', 'Value differs from delivered evidence after unit normalization')
    elif assertion.get('unit') != evidence.get('unit') or type(value) is not type(expected) or value != expected:
        _fail('FACT_MISMATCH', path + '.value', 'Value/type/unit differs from delivered evidence')


def _fact(arg, path, context):
    allowed_fields = {'id', 'kind', 'proposer', 'stance', 'adoption', 'evidence', 'measurement_kind', 'assertion', 'field'}
    if set(arg) - allowed_fields:
        _fail('STRUCTURED_ASSERTION_REQUIRED', path, 'Free prose cannot be marked as a verified fact; use hypothesis')
    evidence = _evidence(arg.get('evidence'), path + '.evidence', context)
    if arg.get('measurement_kind') != evidence.get('kind'):
        _fail('MEASUREMENT_KIND_MISMATCH', path + '.measurement_kind', 'Measurement meaning differs from delivery')
    source_assertion = evidence.get('kind') in ('history', 'ai_classification', 'adopted_lesson')
    if evidence.get('kind') in UNVERIFIED_KINDS or evidence.get('status') == 'reported_unverified' and not source_assertion:
        _fail('UNVERIFIED_SOURCE', path, 'Use source_quote for reported or AI-submitted external material')
    if 'field' in arg and arg['field'] != evidence.get('field'):
        _fail('MEASUREMENT_KIND_MISMATCH', path + '.field', 'Cannot rename a positive-only ratio or arithmetic sum')
    assertion = arg.get('assertion')
    if not isinstance(assertion, dict):
        _fail('STRUCTURED_ASSERTION_REQUIRED', path + '.assertion', 'Structured assertion required')
    op = assertion.get('op')
    allowed = {'value': {'op', 'value', 'unit'}, 'sign': {'op', 'direction'},
               'compare': {'op', 'operator', 'right', 'result'}, 'unknown': {'op'}}
    if op not in allowed or set(assertion) - allowed.get(op, set()):
        _fail('UNSUPPORTED_ASSERTION', path + '.assertion', 'Only registered structured assertions are supported')
    if source_assertion and op not in ('value', 'unknown'):
        _fail('UNVERIFIED_SOURCE', path, 'Only the submitted source label/text can be verified')
    unknown = evidence.get('status') == 'unknown' or evidence.get('value') is None
    if unknown and op != 'unknown':
        zero = assertion.get('direction') == 'zero' or assertion.get('value') == 0
        _fail('UNKNOWN_AS_ZERO' if zero else 'UNKNOWN_EVIDENCE', path, 'Missing measurement cannot assert an observed value or direction')
    label = f"{evidence['subject']} {evidence['field']}"
    references = [copy.deepcopy(arg['evidence'])]
    if op == 'unknown':
        if not unknown:
            _fail('FACT_MISMATCH', path, 'Delivered evidence is observed, not missing')
        summary = f'{label}: 미확인'
    elif op == 'value':
        _value_matches(assertion, evidence, path + '.assertion')
        summary = f"{label}: {evidence['value']} {evidence['unit']}"
    elif op == 'sign':
        quantity_flow = (evidence.get('kind') == 'intraday_current' and evidence.get('unit') == 'share'
                         and evidence.get('field') in ('foreign_qty', 'inst_qty'))
        if not quantity_flow and (evidence.get('unit') not in ('KRW', 'share') or evidence.get('kind') not in ('intraday_estimate', 'daily_accumulated')):
            _fail('DIRECTION_NOT_APPLICABLE', path, 'Buy/sell direction requires an actor net flow measurement')
        if evidence.get('field') not in ('foreign', 'institution', 'foreign_net', 'inst_net', 'foreign_qty', 'inst_qty'):
            _fail('DIRECTION_NOT_APPLICABLE', path, 'A ratio or aggregate cannot establish each actor\'s trading direction')
        amount = _number(evidence['value'], path)
        direction = 'buy' if amount > 0 else 'sell' if amount < 0 else 'zero'
        if assertion.get('direction') != direction:
            _fail('FACT_MISMATCH', path + '.assertion.direction', 'Direction contradicts the unrounded signed measurement')
        summary = f"{label}: {dict(buy='순매수', sell='순매도', zero='실제 0')[direction]} ({evidence['value']} {evidence['unit']})"
    else:
        right = _evidence(assertion.get('right'), path + '.assertion.right', context)
        if right.get('status') == 'unknown' or right.get('value') is None:
            _fail('UNKNOWN_EVIDENCE', path, 'Comparison operand is missing')
        if right.get('kind') in UNVERIFIED_KINDS or right.get('status') == 'reported_unverified':
            _fail('UNVERIFIED_SOURCE', path, 'Cannot compare against unverified reporting')
        if not evidence.get('observation_id') or evidence.get('observation_id') != right.get('observation_id') or evidence.get('subject') != right.get('subject'):
            _fail('OBSERVATION_MISMATCH', path, 'Both operands must belong to the same subject and observation')
        left_value, left_unit = _converted(evidence['value'], evidence['unit'], path)
        right_value, right_unit = _converted(right['value'], right['unit'], path)
        if left_unit != right_unit:
            _fail('UNIT_MISMATCH', path, 'Comparison units are incompatible')
        operator = assertion.get('operator')
        results = {'gt': left_value > right_value, 'gte': left_value >= right_value,
                   'lt': left_value < right_value, 'lte': left_value <= right_value, 'eq': left_value == right_value}
        if operator not in results or not isinstance(assertion.get('result'), bool):
            _fail('STRUCTURED_ASSERTION_REQUIRED', path, 'Comparison requires operator and boolean result')
        if assertion['result'] != results[operator]:
            _fail('FACT_MISMATCH', path, 'Comparison result contradicts delivered values')
        symbol = {'gt': '>', 'gte': '≥', 'lt': '<', 'lte': '≤', 'eq': '='}[operator]
        summary = f"{label} {evidence['value']} {symbol} {right['field']} {right['value']}: {results[operator]}"
        references.append(copy.deepcopy(assertion['right']))
    if source_assertion:
        summary = '출처에 기록된 주장·분류(시장 사실 미검증): ' + summary
    if evidence.get('temporal_scope') == 'received_values_only':
        summary += '; 수신값 산술합/관계, 동시점 합계 아님: ' + str(evidence.get('reason') or '공급자 기준 시각 일치 미확인')
    if evidence.get('meaning') == 'positive_parts_only':
        summary += '; 양수 부분만 합산한 후보용 비율(두 주체 동반 순매수를 뜻하지 않음)'
    return {**copy.deepcopy(arg), 'server_summary': summary,
            'reference_validation': 'passed', 'fact_validation': 'passed',
            'evidence_snapshots': [evidence] + ([right] if op == 'compare' else []),
            'verified_scope': 'source_assertion_only' if source_assertion else 'structured_assertion_only',
            'source_truth': 'unverified' if source_assertion else 'measurement_only', 'references': references}


def _argument(arg, path, context):
    if not isinstance(arg, dict):
        _fail('SCHEMA_ERROR', path, 'Argument must be an object')
    _id(arg.get('id'), path + '.id')
    _required_text(arg.get('proposer'), path + '.proposer')
    for key, choices in [('stance', {'support', 'oppose', 'neutral'}), ('adoption', {'adopted', 'rejected', 'reference'})]:
        if arg.get(key) not in choices:
            _fail('SCHEMA_ERROR', path + '.' + key, 'Invalid argument role')
    kind = arg.get('kind')
    if kind == 'fact_claim':
        try:
            return _fact(arg, path, context)
        except InvalidSubmission as exc:
            if exc.error['code'] in {'FACT_MISMATCH', 'UNKNOWN_AS_ZERO', 'UNKNOWN_EVIDENCE',
                                     'MEASUREMENT_KIND_MISMATCH', 'DIRECTION_NOT_APPLICABLE',
                                     'UNIT_MISMATCH', 'UNSUPPORTED_UNIT', 'UNVERIFIED_SOURCE'}:
                exc.error.update(reference_validation='passed', fact_validation='failed')
            raise
    if kind == 'hypothesis':
        _required_text(arg.get('text'), path + '.text')
        refs = arg.get('claim_ids', [])
        if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
            _fail('SCHEMA_ERROR', path + '.claim_ids', 'claim_ids must be a list of argument IDs')
        return {**copy.deepcopy(arg), 'hypothesis_status': 'submitted_unverified',
                'fact_validation': 'not_applicable'}
    if kind == 'source_quote':
        evidence = _evidence(arg.get('evidence'), path + '.evidence', context)
        quote = _required_text(arg.get('quote'), path + '.quote')
        snippets = [evidence.get(key) for key in ('quote_text', 'text', 'title', 'snippet')]
        if evidence.get('unit') == 'text':
            snippets.append(evidence.get('value'))
        if not any(isinstance(x, str) and quote in x for x in snippets):
            _fail('QUOTE_MISMATCH', path + '.quote', 'Quote is absent from the delivered text (metadata is not source text)')
        return {**copy.deepcopy(arg), 'reference_validation': 'passed',
                'quote_validation': 'matched_stored_text', 'source_truth': 'unverified',
                'evidence_snapshots': [evidence], 'fact_validation': 'not_applicable'}
    _fail('SCHEMA_ERROR', path + '.kind', 'Unknown argument kind')


def _premises(items, context, claim_ids, jid):
    if not isinstance(items, list):
        _fail('SCHEMA_ERROR', 'premises', 'premises must be an array')
    applications, proposals = [], []
    for i, item in enumerate(items):
        path = f'premises[{i}]'
        if not isinstance(item, dict):
            _fail('SCHEMA_ERROR', path, 'Premise must be an object')
        if 'application_id' in item:
            if set(item) != {'application_id'}:
                _fail('IMMUTABLE_PREMISE', path, 'Reuse references cannot alter the existing condition or horizon')
            existing = _load('premises', _id(item['application_id'], path + '.application_id'))
            origin = load_judgment(existing.get('judgment_id', '')) if existing else None
            if (not existing or existing.get('code') != context['code'] or not origin
                    or not any(_json(p) == _json(existing) for p in origin.get('premise_applications', []))):
                _fail('UNKNOWN_PREMISE', path, 'No committed premise application for this instrument')
            applications.append(existing)
            continue
        refs = item.get('claim_ids', [])
        if not isinstance(refs, list) or any(x not in claim_ids for x in refs):
            _fail('UNKNOWN_CLAIM', path + '.claim_ids', 'Premise references unknown arguments')
        if item.get('definition_id') in PREMISE_DEFINITIONS:
            days = item.get('days')
            if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
                _fail('EXPLICIT_HORIZON_REQUIRED', path + '.days', 'A positive N trading days must be submitted explicitly')
            if item.get('direction') not in ('above', 'below'):
                _fail('SCHEMA_ERROR', path + '.direction', 'above or below required')
            allowed = {'definition_id', 'days', 'direction', 'claim_ids'}
            if set(item) - allowed:
                _fail('UNREGISTERED_PREMISE_CONDITION', path, 'Use PremiseProposal for conditions outside the registered definition')
            applications.append({**copy.deepcopy(item), 'application_id': f'premise-{uuid.uuid4().hex}',
                'code': context['code'], 'judgment_id': jid, 'judgment_date': C.today().isoformat(),
                'created_at': _now(), 'definition_snapshot': copy.deepcopy(PREMISE_DEFINITIONS[item['definition_id']]),
                'start_trading_day': None, 'end_trading_day': None,
                'calendar_status': 'resolve_from_trading_calendar_in_step4', 'status': 'pending_evaluation'})
        else:
            _required_text(item.get('text'), path + '.text')
            proposals.append({'proposal_id': f'proposal-{uuid.uuid4().hex}', 'code': context['code'],
                              'judgment_id': jid, 'created_at': _now(), 'proposal': copy.deepcopy(item),
                              'status': 'submitted_unverified'})
    return applications, proposals


def _context():
    return {'run_attempt_id': os.environ.get('RUN_ATTEMPT_ID', ''), 'run_id': os.environ.get('RUN_ID', '')}


def _audit_payload(value):
    # Invalid JSON numerics are preserved explicitly, never emitted as market facts.
    if isinstance(value, float) and not math.isfinite(value):
        return {'invalid_number': repr(value)}
    if isinstance(value, dict):
        return {str(k): _audit_payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_payload(v) for v in value]
    return value if value is None or isinstance(value, (str, int, float, bool)) else {'invalid_type': type(value).__name__, 'representation': repr(value)}


def _event(item, status, errors=None, judgment_id=None, event_id=None):
    event_id = event_id or f'submission-{uuid.uuid4().hex}'
    event = {**_context(), 'event_id': event_id, 'created_at': _now(), 'status': status,
             'code': item.get('code') if isinstance(item, dict) else None,
             'confidence_for': item.get('confidence_for') if isinstance(item, dict) else None,
             'request_key': item.get('request_key') if isinstance(item, dict) else None,
             'judgment_id': judgment_id, 'errors': errors or [],
             'submitted_payload': _audit_payload(item), 'request_hash': _digest(_audit_payload(item))}
    _archive('submissions', event_id, event)
    return event


def _recording_error(item, exc):
    error = {'code': 'RECORDING_ERROR', 'path': '$', 'message': f'{type(exc).__name__}: {exc}'}
    try:
        _event(item, 'recording_error', [error])
    except Exception:
        pass
    try:
        import judgment_coverage as coverage
        reporter = getattr(coverage, 'recording_error', None)
        if reporter:
            reporter('record_ai_judgments', exc)
        else:
            coverage._error(_context()['run_attempt_id'], 'record_ai_judgments', exc)
    except Exception:
        pass
    print(f'ai_judgment recording_error: {error["message"]}', file=sys.stderr)
    return {'status': 'recording_error', 'errors': [error]}


def _validate(item, content_hash, jid):
    if not isinstance(item, dict):
        _fail('SCHEMA_ERROR', '$', 'Judgment must be an object')
    context = {**_context(), 'code': _id(item.get('code'), 'code')}
    if not context['run_id'] or not context['run_attempt_id']:
        _fail('MISSING_RUN_CONTEXT', '$', 'RUN_ID and RUN_ATTEMPT_ID are required')
    _required_text(item.get('request_key'), 'request_key')
    if item.get('confidence_for') not in ('매수', '매도'):
        _fail('SCHEMA_ERROR', 'confidence_for', 'Choose 매수 or 매도 explicitly')
    context['confidence_for'] = item['confidence_for']
    if item.get('recommended_action') not in ('매수', '매도', '보유', '관망'):
        _fail('SCHEMA_ERROR', 'recommended_action', 'Unknown action')
    if item['recommended_action'] in ('매수', '매도') and item['recommended_action'] != item['confidence_for']:
        _fail('DIRECTION_MISMATCH', 'confidence_for', 'Action and confidence direction disagree')
    score = _number(item.get('confidence_raw'), 'confidence_raw')
    if not 0 <= score <= 1:
        _fail('SCHEMA_ERROR', 'confidence_raw', 'Confidence must be in [0, 1]')
    _required_text(item.get('decision'), 'decision')
    deliveries = item.get('delivery_ids')
    if not isinstance(deliveries, list) or not deliveries or any(not isinstance(x, str) for x in deliveries):
        _fail('SCHEMA_ERROR', 'delivery_ids', 'At least one delivery ID required')
    context['delivery_ids'] = deliveries
    for i, delivery in enumerate(deliveries):
        _receipt(delivery, f'delivery_ids[{i}]', context)
    arguments = item.get('arguments')
    if not isinstance(arguments, list) or not arguments:
        _fail('SCHEMA_ERROR', 'arguments', 'At least one structured argument required')
    validated = [_argument(arg, f'arguments[{i}]', context) for i, arg in enumerate(arguments)]
    ids = [arg['id'] for arg in validated]
    if len(ids) != len(set(ids)):
        _fail('DUPLICATE_CLAIM_ID', 'arguments', 'Argument IDs must be unique within a judgment')
    for i, arg in enumerate(validated):
        if arg['kind'] == 'hypothesis' and any(ref not in ids or ref == arg['id'] for ref in arg.get('claim_ids', [])):
            _fail('UNKNOWN_CLAIM', f'arguments[{i}].claim_ids', 'Hypothesis must reference other submitted arguments')
    if item.get('supersedes_judgment_id'):
        previous = load_judgment(_id(item['supersedes_judgment_id'], 'supersedes_judgment_id'))
        if not previous or previous['code'] != item['code'] or previous['confidence_for'] != item['confidence_for']:
            _fail('INVALID_PREVIOUS_JUDGMENT', 'supersedes_judgment_id', 'Previous judgment must exist for the same target and score direction')
    applications, proposals = _premises(item.get('premises', []), context, set(ids), jid)
    if hasattr(C, 'conf_boost_details'):
        confidence = C.conf_boost_details(item['code'], item['confidence_for'], float(score))
    else:
        adjusted, note = C.conf_boost(item['code'], item['confidence_for'], float(score))
        confidence = {'raw': float(score), 'result': adjusted, 'note': note, 'details_status': 'legacy_function'}
    mentioned = {(r['delivery_id'], r['evidence_id']) for arg in validated for r in (
        arg.get('references', []) or ([arg['evidence']] if arg.get('evidence') else []))}
    unmentioned = objects.unmentioned_evidence(
        [(did,_receipt(did,'delivery_ids',context).get('evidence_catalog',[])) for did in deliveries],mentioned)
    position_contexts = list(dict.fromkeys(e.get('position_episode_id') or
                         (e.get('value') if e.get('field') == 'position_episode_id' else None)
                         for did in deliveries for e in _receipt(did, 'delivery_ids', context).get('evidence_catalog', [])
                         if e.get('subject') == item['code']))
    position_contexts = [x for x in position_contexts if x]
    import judgment_context as comparison
    try:
        comparison_context = comparison.comparison_snapshot(item['code'],
            [_receipt(did, 'delivery_ids', context) for did in deliveries], item.get('comparison_context'))
    except ValueError as exc:
        _fail('INVALID_COMPARISON_CONTEXT', 'comparison_context', str(exc))
    decision_regime_snapshot = comparison.snapshot_regime()
    return {**copy.deepcopy(item), **_context(), 'schema_version': SCHEMA_VERSION, 'judgment_id': jid,
            'position_episode_ids': position_contexts,
            'created_at': _now(), 'request_hash': content_hash, 'status': 'accepted',
            'confidence_raw': float(score), 'arguments': validated, 'unmentioned_evidence': unmentioned,
            'premise_applications': applications, 'premise_proposals': proposals,
            'confidence_at_submission': {'usage': 'reference_only', 'details': confidence},
            'comparison_snapshot': comparison_context,
            'case_context_version': 'CASE_CONTEXT@v1',
            'decision_regime_snapshot': decision_regime_snapshot,
            'decision_validation': 'submitted_unverified'}


def _reserve_request(item, content_hash):
    """A rejected key is immutable too; correction requires a new request key."""
    if not isinstance(item, dict) or not isinstance(item.get('request_key'), str) or not item['request_key'].strip():
        return
    key = 'request-' + _digest([_context()['run_attempt_id'], item['request_key']])[:40]
    entry = {**_context(), 'request_key': item['request_key'], 'request_hash': content_hash, 'created_at': _now()}
    try:
        _archive('judgment_requests', key, entry)
    except FileExistsError:
        existing = _load('judgment_requests', key)
        if not existing or existing.get('request_hash') != content_hash:
            _fail('IDEMPOTENCY_CONFLICT', 'request_key', 'Changed submission requires a new request_key, including rejected submissions')


def record_ai_judgments(items):
    """Accept/reject independently. Rejections never alter legacy trading decisions."""
    if not isinstance(items, list):
        items = [items]
    results = []
    for item in items:
        try:
            if isinstance(item, dict):
                item = copy.deepcopy(item)
                aliases = {'buy': '매수', 'sell': '매도', 'hold': '보유', 'watch': '관망'}
                for field in ('confidence_for', 'recommended_action'):
                    if isinstance(item.get(field), str):
                        item[field] = aliases.get(item[field], item[field])
            content_hash = _digest(item)
            key = item.get('request_key') if isinstance(item, dict) else None
            jid = 'judgment-' + _digest([_context()['run_attempt_id'], key])[:40]
            duplicate = next((event for p in (C.ontology_root() / 'submissions').glob('*.json')
                              if (event := _load('submissions', p.stem))
                              and event.get('status') == 'rejected'
                              and event.get('run_attempt_id') == _context()['run_attempt_id']
                              and event.get('request_key') == key
                              and event.get('request_hash') == content_hash), None)
            if duplicate:
                results.append({'status': 'rejected', 'errors': duplicate['errors'],
                                'submission_id': duplicate['event_id'], 'idempotent': True})
                continue
            old = load_judgment(jid)
            if old:
                if old['request_hash'] != content_hash:
                    _fail('IDEMPOTENCY_CONFLICT', 'request_key', 'Changed submission requires a new request_key')
                results.append({'status': 'accepted', 'judgment_id': jid, 'idempotent': True,
                                'premise_application_ids': [p['application_id'] for p in old['premise_applications']]})
                continue
            _reserve_request(item, content_hash)
            row = _validate(item, content_hash, jid)
            event_id = f'submission-{uuid.uuid4().hex}'
            row['acceptance_event_id'] = event_id
            for premise in row['premise_applications']:
                if premise['judgment_id'] == jid:
                    _archive('premises', premise['application_id'], premise)
            for proposal in row['premise_proposals']:
                _archive('premise_proposals', proposal['proposal_id'], proposal)
            try:
                _archive('judgments', jid, row)
            except FileExistsError:
                # Concurrent identical retries converge on the persisted acceptance.
                old = load_judgment(jid)
                if old and old['request_hash'] == content_hash:
                    results.append({'status': 'accepted', 'judgment_id': jid, 'idempotent': True})
                    continue
                raise
            try:
                _event(item, 'accepted', judgment_id=jid, event_id=event_id)
            except Exception:
                # Not committed: no caller has received acceptance; allow a later retry.
                _path('judgments', jid).unlink(missing_ok=True)
                raise
            results.append({'status': 'accepted', 'judgment_id': jid, 'idempotent': False,
                            'premise_application_ids': [p['application_id'] for p in row['premise_applications']],
                            'premise_proposal_ids': [p['proposal_id'] for p in row['premise_proposals']],
                            'fact_summaries': [a['server_summary'] for a in row['arguments'] if 'server_summary' in a]})
            # Research predictions are a separate side record. They are never
            # returned to the trading AI and cannot undo the acceptance commit.
            try:
                import case_prediction
                case_prediction.record_new_premises(row)
            except Exception as exc:
                try:
                    import case_store
                    case_store.record_error(None, 'new_premise_prediction', exc)
                except Exception:
                    print(f'case prediction recording_error: {type(exc).__name__}: {exc}', file=sys.stderr)
        except InvalidSubmission as exc:
            try:
                event = _event(item, 'rejected', [exc.error])
                results.append({'status': 'rejected', 'errors': [exc.error], 'submission_id': event['event_id']})
            except Exception as storage_exc:
                results.append(_recording_error(item, storage_exc))
        except (TypeError, ValueError) as exc:
            try:
                error = {'code': 'SCHEMA_ERROR', 'path': '$', 'message': str(exc)}
                _event(item, 'rejected', [error])
                results.append({'status': 'rejected', 'errors': [error]})
            except Exception as storage_exc:
                results.append(_recording_error(item, storage_exc))
        except Exception as exc:
            results.append(_recording_error(item, exc))
    return results


def iter_ai_judgments(run_attempt_id=None, code=None, delivery_id=None):
    for row in record_store.iter_records(C.ontology_root()/'judgments',
            filters={'run_attempt_id':run_attempt_id,'code':code}):
        committed=load_judgment(row['judgment_id'])
        if committed and (delivery_id is None or delivery_id in committed.get('delivery_ids',[])):
            yield committed


def get_ai_judgments(run_attempt_id=None, code=None, delivery_id=None):
    return list(iter_ai_judgments(run_attempt_id,code,delivery_id))


def read_ai_judgments(knowledge_as_of=None):
    """Operator read: preserve accepted-but-unavailable and orphan records.

    The AI tool's historical list contract is unchanged. This richer reader does
    not silently turn a deleted accepted judgment into 'no judgment submitted'.
    """
    cutoff = datetime.fromisoformat((knowledge_as_of or _now()).replace('Z', '+00:00'))
    if cutoff.tzinfo is None:
        raise ValueError('knowledge_as_of requires timezone')
    from judgment_coverage import _records
    submissions, errors = _records('submissions')
    rows, read_errors = _records('judgments')
    errors += read_errors
    def visible(row):
        try:
            at = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
            if at.tzinfo is None:
                raise ValueError('Source timestamp requires timezone')
            return at <= cutoff
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({'judgment_id': row.get('judgment_id'), 'error': str(exc)})
            return False
    submissions = [s for s in submissions if visible(s)]
    values = {r['judgment_id']: r for r in rows if visible(r) and r.get('judgment_id')}
    commits = {s['event_id']: s for s in submissions if s.get('status') == 'accepted'}
    accepted, unavailable = [], []
    for identifier in sorted(set(values) | {s.get('judgment_id') for s in commits.values()} - {None}):
        row = values.get(identifier)
        commit = commits.get((row or {}).get('acceptance_event_id'))
        if row and commit and commit.get('judgment_id') == identifier:
            accepted.append(copy.deepcopy(row))
        else:
            origins = [s for s in commits.values() if s.get('judgment_id') == identifier]
            source = row or (origins[0] if origins else {})
            unavailable.append({'judgment_id': identifier, 'run_attempt_id': source.get('run_attempt_id'),
                'code': source.get('code'), 'confidence_for': source.get('confidence_for'),
                'created_at': source.get('created_at'), 'status': 'uncommitted' if row else 'missing_or_corrupt',
                'reason': 'acceptance_commit_unavailable' if row else 'accepted_judgment_unavailable'})
    return {'judgments': accepted, 'unavailable': unavailable, 'errors': errors,
            'recording_status': 'recording_error' if errors or unavailable else 'recorded'}


def committed_premises(knowledge_as_of=None, market_as_of=None):
    """Read accepted applications once, without treating orphan files as targets.

    Unlike the display-only judgment reader, this reports malformed source
    records: the scorer must not advertise an empty, complete target set after
    losing a committed judgment or application. No source record is repaired.
    """
    cutoff = datetime.fromisoformat((knowledge_as_of or _now()).replace('Z', '+00:00'))
    if cutoff.tzinfo is None:
        raise ValueError('knowledge_as_of requires timezone')
    market_date = datetime.fromisoformat(market_as_of or cutoff.date().isoformat()).date()
    errors, applications, cache, declared_targets = [], {}, __import__('collections').OrderedDict(), {}

    def read(kind, identifier):
        path = _path(kind, identifier)
        row = record_store.load(path)
        if not isinstance(row, dict):
            raise ValueError('Expected source record object')
        return row

    def committed(identifier):
        if identifier in cache:
            return cache[identifier]
        row = read('judgments', identifier)
        event = read('submissions', row['acceptance_event_id'])
        if row.get('judgment_id') != identifier or event.get('status') != 'accepted' or event.get('judgment_id') != identifier:
            raise ValueError('Judgment has no matching acceptance commit')
        for when in (row.get('created_at'), event.get('created_at')):
            stamp = datetime.fromisoformat(when.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('Source timestamp requires timezone')
            if stamp > cutoff:
                cache[identifier] = None
                return None
        cache[identifier] = row
        if len(cache) > 128: cache.popitem(last=False)
        return row

    def source_files(kind):
        try:
            return sorted(p for p in (C.ontology_root() / kind).iterdir() if p.suffix == '.json')
        except FileNotFoundError:
            return []
        except OSError as exc:
            errors.append({'kind': kind, 'error': str(exc)})
            return []

    identifiers = {p.stem for p in source_files('judgments')}
    # Acceptance commits also reveal deleted judgment files. Scanning only the
    # remaining judgments could otherwise turn a lost target into a clean zero.
    for path in source_files('submissions'):
        try:
            event = read('submissions', path.stem)
            if event.get('status') == 'accepted':
                stamp = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('Source timestamp requires timezone')
                if stamp <= cutoff:
                    identifiers.add(_id(event.get('judgment_id'), 'judgment_id'))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append({'kind': 'submission_source', 'submission_id': path.stem, 'error': str(exc)})
    for judgment_id in sorted(identifiers):
        try:
            judgment = committed(judgment_id)
            if judgment is None:
                continue
            values = judgment.get('premise_applications', [])
            if not isinstance(values, list):
                raise ValueError('Malformed premise application list')
            # Accepted embedded targets remain visible when their separate
            # source record is lost. They are not accepted as usable data and
            # orphan premise files never enter this list.
            for embedded in values:
                if not isinstance(embedded, dict):
                    continue
                try:
                    identifier = _id(embedded.get('application_id'), 'application_id')
                    stamp = datetime.fromisoformat(embedded['created_at'].replace('Z', '+00:00'))
                    if (stamp.tzinfo is not None and stamp <= cutoff
                            and datetime.fromisoformat(embedded['judgment_date']).date() <= market_date):
                        declared_targets.setdefault(identifier, {
                            'application_id': identifier, 'code': embedded.get('code'),
                            'judgment_ids': [judgment_id], 'judgment_date': embedded['judgment_date'],
                            'origin_at': embedded['created_at'], 'origin_judgment_id': embedded.get('judgment_id'),
                            'reason': 'accepted_target_source_unavailable'})
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
            for embedded in values:
                if not isinstance(embedded, dict):
                    raise ValueError('Malformed premise application')
                identifier = _id(embedded.get('application_id'), 'application_id')
                application = read('premises', identifier)
                if _json(application) != _json(embedded):
                    raise ValueError(f'{identifier}: stored application differs from accepted judgment')
                origin = committed(application['judgment_id'])
                if origin is None:
                    continue
                if application.get('code') != judgment.get('code') or application.get('code') != origin.get('code'):
                    raise ValueError(f'{identifier}: instrument mismatch')
                if not any(p.get('application_id') == identifier and _json(p) == _json(application)
                           for p in origin.get('premise_applications', [])):
                    raise ValueError(f'{identifier}: origin judgment did not accept this premise')
                if datetime.fromisoformat(application['judgment_date']).date() > market_date:
                    continue
                created = datetime.fromisoformat(application['created_at'].replace('Z', '+00:00'))
                if created.tzinfo is None:
                    raise ValueError('Application timestamp requires timezone')
                if created > cutoff:
                    continue
                target = applications.setdefault(identifier, {'application': copy.deepcopy(application), 'judgment_ids': []})
                if judgment['judgment_id'] not in target['judgment_ids']:
                    target['judgment_ids'].append(judgment['judgment_id'])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append({'kind': 'judgment_source', 'judgment_id': judgment_id, 'error': str(exc)})
    return {'applications': list(applications.values()),
            'unavailable_applications': [v for k, v in declared_targets.items() if k not in applications],
            'errors': errors, 'target_set_incomplete': bool(errors)}


def record_external_evidence(items):
    """Store AI-provided text, explicitly without fetching or authenticating its URL."""
    if not isinstance(items, list):
        items = [items]
    results = []
    for item in items:
        try:
            if not isinstance(item, dict):
                _fail('SCHEMA_ERROR', '$', 'External material must be an object')
            context = _context()
            if not all(context.values()):
                _fail('MISSING_RUN_CONTEXT', '$', 'RUN_ID and RUN_ATTEMPT_ID required')
            _required_text(item.get('request_key'), 'request_key')
            subject = _id(item.get('subject'), 'subject')
            content = _required_text(item.get('text'), 'text')
            url = _required_text(item.get('url'), 'url')
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                _fail('SCHEMA_ERROR', 'url', 'HTTP(S) source URL required; it will not be fetched')
            did = 'external-' + _digest([context['run_attempt_id'], item['request_key']])[:40]
            content_hash = _digest(item)
            existing = _load('deliveries', did)
            if existing:
                if existing.get('request_hash') != content_hash:
                    _fail('IDEMPOTENCY_CONFLICT', 'request_key', 'Changed external material requires a new key')
                results.append({'status': 'recorded_unverified', 'delivery_id': did, 'evidence_id': did + ':text', 'idempotent': True})
                continue
            eid = did + ':text'
            catalog = [{'evidence_id': eid, 'subject': subject, 'field': 'external_text',
                        'value': content, 'unit': 'text', 'kind': 'external_unverified',
                        'status': 'reported_unverified', 'observed_at': None, 'available_at': _now(),
                        'observation_id': did, 'source_url': url, 'quote_text': content,
                        'source_verification': 'ai_submitted_unverified', 'title': item.get('title')}]
            row = {**context, 'ai_run_id': context['run_id'], 'delivery_id': did, 'created_at': _now(),
                   'request_hash': content_hash, 'source_verification': 'ai_submitted_unverified',
                   'original_submission': copy.deepcopy(item), 'evidence_catalog': catalog}
            _archive('deliveries', did, row)
            results.append({'status': 'recorded_unverified', 'delivery_id': did, 'evidence_id': eid, 'idempotent': False})
        except InvalidSubmission as exc:
            results.append({'status': 'rejected', 'errors': [exc.error]})
        except Exception as exc:
            results.append(_recording_error(item, exc))
    return results


def validate_references(refs, code=None):
    """Validate optional regime references without treating interpretation as fact."""
    try:
        if not isinstance(refs, list):
            _fail('SCHEMA_ERROR', 'references', 'Expected an array of evidence references')
        context = {**_context(), 'code': code, 'delivery_ids': [r.get('delivery_id') for r in refs if isinstance(r, dict)]}
        if not context['run_attempt_id'] or not context['run_id']:
            _fail('MISSING_RUN_CONTEXT', '$', 'RUN_ID and RUN_ATTEMPT_ID required')
        return {'status': 'valid', 'evidence': [_evidence(ref, f'references[{i}]', context) for i, ref in enumerate(refs)], 'errors': []}
    except InvalidSubmission as exc:
        return {'status': 'rejected', 'evidence': [], 'errors': [exc.error]}


def validate_frozen_submission(item, receipts, context, at):
    """Step-7 pure checker: same fact/reference contract, no live reads/writes.

    Only receipts actually returned by the frozen tool server are in `receipts`.
    It does not accept a production judgment or alter coverage/premise storage.
    """
    token = _FROZEN.set({'receipts': receipts, 'at': datetime.fromisoformat(at)})
    try:
        if not isinstance(item, dict): _fail('SCHEMA_ERROR', '$', 'Object required')
        _required_text(item.get('request_key'), 'request_key')
        code = _id(item.get('code'), 'code')
        side = item.get('confidence_for')
        if code != context['code'] or side != context['confidence_for']:
            _fail('TARGET_MISMATCH', 'code', 'Frozen experiment target differs')
        if side not in ('매수', '매도'): _fail('SCHEMA_ERROR', 'confidence_for', 'Explicit direction required')
        action = item.get('recommended_action')
        if action not in ('매수', '매도', '보유', '관망') or action in ('매수', '매도') and action != side:
            _fail('DIRECTION_MISMATCH', 'recommended_action', 'Action and direction differ')
        score = _number(item.get('confidence_raw'), 'confidence_raw')
        if not 0 <= score <= 1: _fail('SCHEMA_ERROR', 'confidence_raw', 'Score outside [0,1]')
        _required_text(item.get('decision'), 'decision')
        ids = item.get('delivery_ids')
        if not isinstance(ids, list) or not ids: _fail('SCHEMA_ERROR', 'delivery_ids', 'Delivered references required')
        ctx = {**context, 'delivery_ids': ids}
        for i, did in enumerate(ids): _receipt(did, f'delivery_ids[{i}]', ctx)
        args = item.get('arguments')
        if not isinstance(args, list) or not args: _fail('SCHEMA_ERROR', 'arguments', 'Structured arguments required')
        validated = [_argument(arg, f'arguments[{i}]', ctx) for i, arg in enumerate(args)]
        claims = [arg['id'] for arg in validated]
        if len(set(claims)) != len(claims): _fail('DUPLICATE_CLAIM_ID', 'arguments', 'Duplicate claim')
        for arg in validated:
            if arg['kind'] == 'hypothesis' and any(x not in claims or x == arg['id'] for x in arg.get('claim_ids', [])):
                _fail('UNKNOWN_CLAIM', 'arguments', 'Unknown hypothesis support')
        # Premises stay experiment proposals; no application IDs are fabricated.
        premises = item.get('premises', [])
        if not isinstance(premises,list): _fail('SCHEMA_ERROR', 'premises', 'Array required')
        for premise in premises:
            if not isinstance(premise, dict): _fail('SCHEMA_ERROR', 'premises', 'Object required')
            if premise.get('application_id') or premise.get('reuse_application_id'):
                _fail('FROZEN_PREMISE_UNAVAILABLE', 'premises', 'Existing application must be supplied separately; no current lookup')
            refs=premise.get('claim_ids',[])
            if not isinstance(refs,list) or any(x not in claims for x in refs):
                _fail('UNKNOWN_CLAIM','premises.claim_ids','Unknown premise support')
            if premise.get('definition_id') in PREMISE_DEFINITIONS:
                if type(premise.get('days')) is not int or premise['days']<=0:
                    _fail('EXPLICIT_HORIZON_REQUIRED','premises.days','Positive trading-day horizon required')
                if premise.get('direction') not in ('above','below'):
                    _fail('SCHEMA_ERROR','premises.direction','above or below required')
                if set(premise)-{'definition_id','days','direction','claim_ids'}:
                    _fail('UNREGISTERED_PREMISE_CONDITION','premises','Use a free premise proposal')
            else: _required_text(premise.get('text'),'premises.text')
        return {**copy.deepcopy(item), 'arguments': validated, 'status': 'accepted',
                'scope': 'isolated_experiment', 'decision_validation': 'submitted_unverified',
                'premise_status': 'experiment_proposals_not_registered', 'confidence_raw': float(score)}
    except InvalidSubmission as exc:
        return {'status': 'rejected', 'errors': [exc.error], 'scope': 'isolated_experiment'}
    finally:
        _FROZEN.reset(token)
