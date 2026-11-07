"""Evaluate immutable, instrument-scoped premises without trading side effects.

A counterexample and a completed horizon are different facts. Data availability,
logical verdict, elapsed period and persistence are deliberately separate.
"""
from __future__ import annotations

import copy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import ai_judgment as J
import common as C
import scoring_store as S

EVALUATOR_VERSION = 'MA20_MAINTENANCE_EVALUATOR@v2'


def _timestamp(value):
    when = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if when.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    return when


def _positive(value):
    if value is None or isinstance(value, bool):
        raise ValueError('Missing or invalid price')
    try:
        value = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid decimal price') from exc
    if not value.is_finite() or value <= 0:
        raise ValueError('Price must be finite and positive')
    return value


def definition_compatibility(application):
    definition = application.get('definition_snapshot')
    if not isinstance(definition, dict):
        return {'status': 'incomplete', 'reason': 'definition_incomplete'}
    identity = application.get('definition_id')
    if identity not in J.PREMISE_DEFINITIONS or definition.get('definition_id') != identity:
        return {'status': 'unsupported', 'reason': 'unsupported_definition'}
    # Missing historical choices cannot be filled from today's defaults. A v1
    # snapshot is compatible only if it actually stored the full same contract.
    required = {k: v for k, v in J.PREMISE_DEFINITION_V2.items() if k not in ('definition_id', 'scoring')}
    missing = [key for key in required if key not in definition]
    if missing:
        return {'status': 'incomplete', 'reason': 'definition_incomplete', 'missing_fields': missing}
    different = [key for key, value in required.items() if definition.get(key) != value]
    if different:
        return {'status': 'unsupported', 'reason': 'definition_conflict', 'conflicting_fields': different}
    return {'status': 'compatible', 'reason': None,
            'binding': 'explicit_v1_full_contract' if identity.endswith('@v1') else 'registered_v2'}


def _daily_comparison(trading_date, row, direction, cutoff, calendar_id=None):
    result = {'date': trading_date, 'status': 'unknown', 'reason': 'missing_daily_observation',
              'observation_ids': [], 'ma20_input_ids': []}
    if not isinstance(row, dict):
        return result
    result.update(source_snapshot=copy.deepcopy(row), provider_finality=row.get('provider_finality', 'not_provided'))
    for key in ('observation_ids', 'ma20_input_ids'):
        value = row.get(key, [])
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            result['reason'] = 'invalid_observation_references'
            return result
        result[key] = copy.deepcopy(value)
    if row.get('status') != 'policy_complete':
        result['reason'] = row.get('reason') or ('daily_provisional' if row.get('status') == 'provisional' else 'daily_unavailable')
        return result
    try:
        if row.get('date') != trading_date:
            raise ValueError('daily_date_mismatch')
        if _timestamp(row.get('available_at')) > cutoff:
            raise ValueError('future_availability')
        close, ma20 = _positive(row.get('close_exact', row.get('close'))), _positive(row.get('ma20_exact', row.get('ma20')))
        inputs = row.get('ma20_inputs')
        ids = row.get('ma20_input_ids')
        if (not isinstance(inputs, list) or len(inputs) != 20 or any(not isinstance(x, dict) for x in inputs)
                or not isinstance(ids, list) or len(set(ids)) != 20):
            raise ValueError('incomplete_ma20_inputs')
        dates = [x.get('date') for x in inputs]
        if any(not isinstance(x, str) for x in dates) or dates != sorted(set(dates)) or dates[-1] != trading_date:
            raise ValueError('invalid_ma20_dates')
        if any(date.fromisoformat(x) > date.fromisoformat(trading_date) for x in dates):
            raise ValueError('future_ma20_input')
        values = [_positive(x.get('close_exact', x.get('close', x.get('value')))) for x in inputs]
        if values[-1] != close or sum(values) / Decimal(20) != ma20:
            raise ValueError('ma20_calculation_mismatch')
        basis = row.get('price_basis')
        expected_basis = J.PREMISE_DEFINITION_V2['price_basis'].get(row.get('instrument_kind'))
        if basis != expected_basis or expected_basis is None or not row.get('source'):
            raise ValueError('unknown_price_basis_or_source')
        if any(x.get('price_basis', basis) != basis or x.get('source', row['source']) != row['source'] for x in inputs):
            raise ValueError('mixed_price_basis_or_source')
        if (row.get('completion_policy') != J.PREMISE_DEFINITION_V2['completion_policy']
                or row.get('calculation_version') != 'session_aligned_sma20_decimal@v1'
                or not row.get('source_version') or not row.get('calendar_id')):
            raise ValueError('daily_definition_or_provenance_missing')
        if calendar_id is not None and row['calendar_id'] != calendar_id:
            raise ValueError('calendar_reference_mismatch')
        if any(x.get('observation_id', identifier) != identifier for x, identifier in zip(inputs, ids)):
            raise ValueError('ma20_input_reference_mismatch')
        confirmation_ids = row.get('confirmation_receipt_ids', [])
        if (row.get('confirmation_status') != 'matched' or len(set(confirmation_ids)) < 2
                or _timestamp(row.get('confirmed_at')) > cutoff):
            raise ValueError('completion_confirmation_missing')
        refuted = close < ma20 if direction == 'above' else close > ma20
        result.update(status='refuted' if refuted else 'maintained', reason=None,
                      close=str(close), ma20=str(ma20), operator='<' if direction == 'above' else '>',
                      refuted=refuted, equality=close == ma20,
                      available_at=row['available_at'], confirmation='policy_complete')
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        result['reason'] = str(exc)
    return result


def evaluate_premise(application, window, daily, market_as_of, knowledge_as_of):
    """Pure evaluation. `daily` maps exact session dates to validated daily rows."""
    cutoff = _timestamp(knowledge_as_of)
    market_date = date.fromisoformat(str(market_as_of))
    market_as_of = market_date.isoformat()
    source = copy.deepcopy(application)
    result = {'schema_version': 'premise-evaluation@v1', 'evaluator_version': EVALUATOR_VERSION,
              'application_id': application.get('application_id'),
              'premise_application_id': application.get('application_id'), 'code': application.get('code'),
              'origin_judgment_id': application.get('judgment_id'), 'market_as_of': market_date.isoformat(),
              'knowledge_as_of': cutoff.isoformat(), 'application_snapshot': source,
              'definition_snapshot': copy.deepcopy(application.get('definition_snapshot')),
              'window_snapshot': copy.deepcopy(window), 'window_id': window.get('window_id'),
              'verdict': 'unknown', 'period_status': 'calendar_unknown', 'data_status': 'unknown',
              'reasons': [], 'comparisons': [], 'first_known_refutation': None,
              'first_refutation_exact': False, 'logical_decision_complete': False,
              'recording_status': 'not_recorded'}
    compatible = definition_compatibility(application)
    result['definition_compatibility'] = compatible
    if compatible['status'] != 'compatible':
        result['reasons'].append(compatible['reason'])
        return result
    try:
        sessions = window.get('sessions', [])
        days = application.get('days')
        if window.get('status') != 'resolved' or not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise ValueError('calendar_unknown')
        if not isinstance(sessions, list) or len(sessions) != days or sessions != sorted(set(sessions)):
            raise ValueError('invalid_session_window')
        for day in sessions:
            date.fromisoformat(day)
        if sessions[0] <= application['judgment_date']:
            raise ValueError('window_not_after_original_judgment')
        if application.get('direction') not in ('above', 'below'):
            raise ValueError('invalid_direction')
        if market_date > cutoff.astimezone(C.KST).date():
            raise ValueError('future_market_date')
        if application.get('created_at') and _timestamp(application['created_at']) > cutoff:
            raise ValueError('future_application')
    except (ValueError, TypeError, KeyError) as exc:
        result['reasons'].append(str(exc))
        return result
    period = 'not_started' if market_as_of < sessions[0] else 'ended' if market_as_of >= sessions[-1] else 'in_progress'
    if period == 'ended' and 'session_closes' in window:
        try:
            end = _timestamp(window['session_closes'][sessions[-1]])
            if end.astimezone(C.KST).date().isoformat() != sessions[-1]:
                raise ValueError('Session close date differs from final session')
            result['period_end_at'] = end.isoformat()
            if cutoff < end:
                period = 'in_progress'
        except (ValueError, TypeError, KeyError):
            period = 'calendar_unknown'
            result['reasons'].append('final_session_close_unknown')
    result['period_status'] = period
    due = [d for d in sessions if d <= market_as_of]
    for d in sessions:
        if d > market_as_of:
            result['comparisons'].append({'date': d, 'status': 'not_due', 'reason': 'future_session'})
        else:
            result['comparisons'].append(_daily_comparison(d, daily.get(d), application['direction'], cutoff, window.get('calendar_id')))
    known = [x for x in result['comparisons'] if x['status'] in ('maintained', 'refuted')]
    unknown = [x for x in result['comparisons'] if x['status'] == 'unknown']
    refutations = [x for x in known if x['status'] == 'refuted']
    result['data_status'] = 'complete' if len(known) == len(sessions) else 'complete_to_date' if len(known) == len(due) else 'incomplete'
    result['expected_sessions'] = len(sessions)
    result['due_sessions'] = len(due)
    result['verified_sessions'] = len(known)
    result['missing_sessions'] = [x['date'] for x in unknown]
    result['observation_ids'] = sorted({i for x in result['comparisons'] for i in x.get('observation_ids', [])})
    if refutations:
        first = refutations[0]
        result['first_known_refutation'] = {k: copy.deepcopy(first[k]) for k in ('date', 'close', 'ma20', 'operator', 'available_at', 'observation_ids', 'ma20_input_ids')}
        result['first_refutation_exact'] = all(x['status'] == 'maintained' for x in result['comparisons'] if x['date'] < first['date'])
        result['verdict'], result['logical_decision_complete'] = 'refuted', True
        if not result['first_refutation_exact']:
            result['reasons'].append('earlier_sessions_unconfirmed')
    elif period == 'ended' and len(known) == len(sessions):
        result['verdict'], result['logical_decision_complete'] = 'maintained', True
    elif period in ('not_started', 'in_progress'):
        result['verdict'] = 'pending'
        result['reasons'].append('period_not_ended')
    else:
        result['reasons'].append('required_observations_unconfirmed')
    if unknown:
        result['reasons'].extend(sorted({x['reason'] for x in unknown if x.get('reason')}))
    return result


def committed_premises(knowledge_as_of=None, market_as_of=None):
    return J.committed_premises(knowledge_as_of, market_as_of)


def _definition_archive():
    sources = {name: Path(__file__).with_name(name).read_text(encoding='utf-8')
               for name in ('premise_evaluation.py', 'ai_judgment.py')}
    body = {'definition_id': 'evaluation-definition-' + S.digest(sources),
            'evaluator_version': EVALUATOR_VERSION, 'sources': sources,
            'premise_definition': copy.deepcopy(J.PREMISE_DEFINITION_V2)}
    return S.archive('definitions', body['definition_id'], body)


def record_evaluation(result, scoring_job_id=None):
    """Immutable semantic revisions; rerunning an unchanged calculation reuses it."""
    try:
        definition = _definition_archive()
        semantic = {key: copy.deepcopy(value) for key, value in result.items()
                    if key not in ('knowledge_as_of', 'recording_status', 'evaluation_id', 'supersedes', 'scoring_job_id', 'created_at',
                                   'semantic_hash', 'newer_revision_ids', 'replayed')}
        semantic['evaluator_definition_id'] = definition['definition_id']
        semantic_hash = S.digest(semantic)
        series = [result.get('premise_application_id'), result.get('market_as_of')]
        cutoff = _timestamp(result['knowledge_as_of'])
        with S.locked('evaluations-' + S.digest(series)):
            history = S.records('evaluations')
            if history['errors']:
                raise S.StoreError('Cannot resolve evaluation revision history: ' + S.canonical(history['errors']))
            siblings = [x for x in history['records'] if x.get('premise_application_id') == series[0]
                        and x.get('market_as_of') == series[1]]
            eligible = [x for x in siblings if _timestamp(x['knowledge_as_of']) <= cutoff]
            superseded = {eid for x in eligible for eid in x.get('supersedes', [])}
            frontier = [x for x in eligible if x['evaluation_id'] not in superseded]
            if len(frontier) == 1 and frontier[0].get('semantic_hash') == semantic_hash:
                return {**frontier[0], 'recording_status': 'recorded', 'replayed': True}
            predecessors = sorted(x['evaluation_id'] for x in frontier)
            # A -> B -> A is a new revision, not a replay of the first A.
            # Older-cutoff backfills cannot supersede a newer known revision.
            newer = sorted(x['evaluation_id'] for x in siblings if _timestamp(x['knowledge_as_of']) > cutoff)
            identifier = 'evaluation-' + S.digest([semantic_hash, predecessors, result['knowledge_as_of']])
            body = {**copy.deepcopy(result), 'evaluation_id': identifier,
                    'evaluator_definition_id': definition['definition_id'], 'semantic_hash': semantic_hash,
                    'created_at': C.now().isoformat(), 'scoring_job_id': scoring_job_id,
                    'supersedes': predecessors, 'newer_revision_ids': newer, 'recording_status': 'recorded'}
            return {**S.archive('evaluations', identifier, body), 'recording_status': 'recorded'}
    except Exception as exc:
        error = S.record_error(scoring_job_id, 'record_evaluation', exc)
        return {**copy.deepcopy(result), 'recording_status': 'recording_error', 'error': error}
