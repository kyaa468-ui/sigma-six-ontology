"""Read-only, origin-based cases for research. No quotes, scoring or trading calls."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import ai_judgment as J
import common as C
import record_store
import storage_objects as objects
import evaluation_context as EC
import judgment_context as CX
import scoring_store as SS
import case_store as S

DATASET_VERSION = 'CASE_DATASET@v1'


def _time(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timezone required')
    return result


def _number(value):
    return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def _read_original(kind, identifier):
    path = J._path(kind, identifier)
    row = record_store.load(path)
    source_sha, _ = objects.file_fingerprint(path)
    if not isinstance(row, dict):
        raise ValueError('Source record must be an object')
    return row, {'kind': kind, 'id': identifier, 'sha256': source_sha}


def _unique_field(catalog, names):
    for name in names:
        found = [x for x in catalog if x.get('field') == name]
        if found:
            signatures = {S.digest({k: v for k, v in x.items() if k != 'evidence_id'}) for x in found}
            if len(signatures) != 1:
                return {'value': None, 'status': 'unknown', 'reason': 'conflicting_displayed_field'}
            return found[0]
    return {'value': None, 'status': 'unknown', 'reason': 'field_not_delivered'}


def _observed(entry):
    return _number(entry.get('value')) if entry.get('status') not in ('unknown', 'unavailable') else None


def _direction(value):
    return 'unknown' if value is None else 'buy' if value > 0 else 'sell' if value < 0 else 'zero'


def _adoption(judgment, application, observation_id=None):
    arguments = {a['id']: a for a in judgment.get('arguments', [])}
    roots = set(application.get('claim_ids', []))
    supporting = set()
    pending = [x for x in roots if arguments.get(x, {}).get('kind') == 'hypothesis'
               and arguments[x].get('adoption') == 'adopted']
    visited = set()
    while pending:
        identifier = pending.pop()
        if identifier in visited:
            continue
        visited.add(identifier)
        for ref in arguments.get(identifier, {}).get('claim_ids', []):
            supporting.add(ref)
            pending.append(ref)
    rows, above, foreign = [], False, False
    for identifier, arg in arguments.items():
        direct = identifier in roots and arg.get('adoption') == 'adopted'
        refs = arg.get('evidence_snapshots', [])
        rows.append({'claim_id': identifier, 'kind': arg.get('kind'), 'adoption': arg.get('adoption'),
                     'premise_linked': identifier in roots, 'directly_adopted_for_premise': direct,
                     'supports_adopted_hypothesis': identifier in supporting,
                     'references': copy.deepcopy(arg.get('references', []))})
        if (not direct or arg.get('kind') != 'fact_claim' or arg.get('fact_validation') != 'passed'
                or not observation_id or any(x.get('observation_id') != observation_id for x in refs)):
            continue
        assertion = arg.get('assertion') or {}
        for evidence in refs:
            field = evidence.get('field', '')
            value = evidence.get('value')
            if field in ('price_comparison.above', 'criterion.PRICE_ABOVE_MA20.briefing_reference') and value is True:
                above = True
            if (field in ('foreign', 'foreign_net') and _observed(evidence) is not None
                    and _observed(evidence) > 0 and assertion.get('op') in ('value', 'sign')):
                foreign = True
        if (assertion.get('op') == 'compare' and assertion.get('operator') == 'gt'
                and assertion.get('result') is True and len(refs) == 2
                and refs[0].get('field') == 'price' and refs[1].get('field') == 'ma20'):
            above = True
    return {'claims': rows, 'explicitly_uses_above_ma20_and_foreign_buy': above and foreign,
            'unmentioned_evidence': copy.deepcopy(judgment.get('unmentioned_evidence', []))}


def extract_case_context(judgment, application):
    """Origin features only; usable immediately after acceptance for shadow prediction."""
    if judgment.get('judgment_id') != application.get('judgment_id'):
        raise ValueError('Only the premise origin judgment can define its comparison features')
    reasons, receipts, sources = [], [], []
    origin_at = judgment['created_at']
    for did in judgment.get('delivery_ids', []):
        receipt, reference = _read_original('deliveries', did)
        if receipt.get('delivery_id') != did or _time(receipt['created_at']) > _time(origin_at):
            raise ValueError('Delivery identity or availability mismatch')
        if receipt.get('run_attempt_id') != judgment.get('run_attempt_id'):
            raise ValueError('Delivery belongs to another attempt')
        receipts.append(receipt); sources.append(reference)
    frozen = copy.deepcopy(judgment.get('comparison_snapshot'))
    if frozen is None:
        # Old records can use only their actually archived deliveries. The
        # choice is explicit provenance, never an invented historical snapshot.
        frozen = CX.comparison_snapshot(application['code'], receipts, judgment.get('comparison_context'))
        frozen['historical_binding'] = 'derived_from_original_delivery_only'
    elif frozen.get('status') == 'recorded':
        current = CX.comparison_snapshot(application['code'], receipts,
                  {k: frozen[k] for k in ('delivery_id', 'observation_id')})
        if current['catalog'] != frozen.get('catalog'):
            raise ValueError('Delivery changed after accepted comparison snapshot')
    if frozen.get('status') != 'recorded':
        reasons.append(frozen.get('reason', 'comparison_context_unknown'))
    catalog = frozen.get('catalog', [])
    fields = {
        'gap': _unique_field(catalog, ['price_comparison.gap', 'criterion.MA20_GAP.briefing_reference']),
        'slope': _unique_field(catalog, ['ma20_slope5']),
        'foreign': _unique_field(catalog, ['foreign', 'foreign_net']),
        'institution': _unique_field(catalog, ['institution', 'inst_net']),
        'denominator': _unique_field(catalog, ['trading_value_20d']),
        'price': _unique_field(catalog, ['price', 'close']),
        'route': _unique_field(catalog, ['admission.route']),
    }
    for field, entry in fields.items():
        if entry.get('available_at') and _time(entry['available_at']) > _time(origin_at):
            reasons.append('future_feature:' + field)
            entry['value'] = None
    gap, slope = _observed(fields['gap']), _observed(fields['slope'])
    f, i, denominator = (_observed(fields[k]) for k in ('foreign', 'institution', 'denominator'))
    if denominator is not None and denominator <= 0:
        denominator = None
    regime_snapshot = copy.deepcopy(judgment.get('decision_regime_snapshot') or
                                    {'status': 'unknown', 'reason': 'decision_regime_unrecorded'})
    regime = regime_snapshot.get('value') if regime_snapshot.get('status') == 'recorded' else None
    if regime_snapshot.get('status') == 'recorded' and any(
            not regime_snapshot.get(k) or _time(regime_snapshot[k]) > _time(origin_at)
            for k in ('captured_at', 'registered_at')):
        regime = None
        reasons.append('decision_regime_not_available_at_origin')
    if regime not in ('상승', '횡보', '하락'):
        regime = None
    features = {'gap': gap, 'slope': slope,
                'foreign_ratio': f / denominator if f is not None and denominator else None,
                'institution_ratio': i / denominator if i is not None and denominator else None,
                'regime': regime}
    reasons.extend('missing_feature:' + key for key, value in features.items() if value is None)
    period_f, period_i = fields['foreign'].get('period'), fields['institution'].get('period')
    observed_f, observed_i = fields['foreign'].get('observed_at'), fields['institution'].get('observed_at')
    method_f, method_i = fields['foreign'].get('kind'), fields['institution'].get('kind')
    flow_method = method_f if method_f == method_i else 'mixed_or_unknown'
    aligned = bool(period_f and period_f == period_i) if flow_method == 'daily_accumulated' else bool(observed_f and observed_f == observed_i)
    if flow_method == 'daily_accumulated':
        flow_period = {'foreign_sessions': len(period_f) if isinstance(period_f, list) else None,
                       'institution_sessions': len(period_i) if isinstance(period_i, list) else None}
    elif flow_method == 'intraday_estimate':
        flow_period = {'definition': 'provider_intraday_snapshot'}
    else:
        flow_period = None
        reasons.append('unknown_flow_method')
    if flow_method == 'daily_accumulated' and (not period_f or not period_i):
        reasons.append('unknown_flow_period')
    receipt = next((r for r in receipts if r['delivery_id'] == frozen.get('delivery_id')), {})
    b = receipt.get('briefing_snapshot') or {}
    row = next((x for key in ('candidates', 'holdings') for x in b.get(key, [])
                if x.get('code') == application['code'] and
                (((x.get('_ontology') or {}).get('observation') or {}).get('observation_id') == frozen.get('observation_id'))), {})
    ontology = row.get('_ontology') or {}
    observation = ontology.get('observation') or {}
    selection = {}
    if observation.get('run_id'):
        try:
            selection, source = _read_original('selections', observation['run_id'])
            if (_time(selection['created_at']) > _time(origin_at)
                    or (selection.get('definitions') or {}).get('hash') != ontology.get('definitions_hash')
                    or not any(x.get('observation_id') == observation.get('observation_id')
                               and x == observation for x in selection.get('observations', []))):
                raise ValueError('Selection source does not match delivered observation')
            sources.append(source)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            selection = {}
            reasons.append('selection_definition_unavailable:' + str(exc))
    else:
        reasons.append('selection_definition_unrecorded')
    instrument_kind = 'etf' if row.get('index') or row.get('inverse') else 'equity' if row else 'unknown'
    price_kind = fields['price'].get('kind')
    route = fields['route'].get('value')
    criterion_versions = sorted({x['criterion_version_id'] for x in catalog if x.get('criterion_version_id')})
    rule_versions = sorted({x['rule_version_id'] for x in observation.get('rules', []) if x.get('rule_version_id')})
    selection_regimes = {(x.get('regime'), x.get('regime_source')) for x in observation.get('rules', [])
                         if 'regime' in x}
    selection_regime = ({'value': next(iter(selection_regimes))[0], 'source': next(iter(selection_regimes))[1]}
                        if len(selection_regimes) == 1 else
                        {'value': None, 'source': None, 'reason': 'selection_regime_not_unique_or_unrecorded'})
    stratum = {'definition_id': application.get('definition_id'),
               'definition_hash': S.digest(application.get('definition_snapshot')),
               'days': application.get('days'), 'direction': application.get('direction'),
               'confidence_for': judgment.get('confidence_for'), 'instrument_kind': instrument_kind,
               'price_kind': price_kind, 'flow_method': flow_method, 'flow_period': flow_period,
               'flow_comparability': 'aligned' if aligned else 'received_values_only',
               'admission_route': route, 'criteria_versions': criterion_versions, 'rule_versions': rule_versions,
               'selection_definition_hash': ontology.get('definitions_hash')}
    if not price_kind or not route or instrument_kind == 'unknown' or not criterion_versions:
        reasons.append('selection_context_incomplete')
    provenance = {key: copy.deepcopy(value) for key, value in fields.items()}
    return {'application_id': application['application_id'], 'code': application['code'],
            'origin_judgment_id': judgment['judgment_id'], 'origin_at': origin_at,
            'judgment_date': application['judgment_date'], 'case_context_version': judgment.get('case_context_version'),
            'features': features, 'feature_eligible': not reasons, 'feature_reasons': sorted(set(reasons)),
            'stratum': stratum, 'stratum_id': S.digest(stratum),
            'comparison_context': frozen, 'decision_regime_snapshot': regime_snapshot,
            'selection_regime': selection_regime,
            'market_rule_hint': ((receipt.get('briefing_snapshot') or {}).get('market') or {}).get('rule_hint'),
            'briefing_regime': (receipt.get('context') or {}).get('regime'),
            'feature_provenance': provenance, 'source_manifest': sources,
            'adoption': _adoption(judgment, application, frozen.get('observation_id')),
            'application_snapshot': copy.deepcopy(application), 'judgment_snapshot': copy.deepcopy(judgment),
            'selection_observation_snapshot': copy.deepcopy(observation),
            'selection_definition_snapshot': copy.deepcopy(selection.get('definitions')),
            'selection_settings_snapshot': {key: copy.deepcopy(selection.get(key)) for key in ('params', 'risk')},
            'signed_flows': {'foreign': f, 'institution': i, 'foreign_direction': _direction(f),
                             'institution_direction': _direction(i), 'same_period_confirmed': aligned,
                             'relative_size': 'unknown' if f is None or i is None else
                                'institution_larger' if abs(i) > abs(f) else 'foreign_larger' if abs(i) < abs(f) else 'equal',
                             'interpretation': 'aligned_values' if aligned else 'received_values_not_simultaneous'},
            'comparison_id': fields['gap'].get('comparison_id'), 'recording_status': 'recorded'}


def _available(row):
    return max(_time(row['knowledge_as_of']), _time(row['created_at']))


def project_case(case, market_as_of, knowledge_as_of):
    """Pure as-of selection from a frozen history; no today's definitions or inputs."""
    result = copy.deepcopy(case)
    cutoff = _time(knowledge_as_of)
    market = date.fromisoformat(market_as_of).isoformat()
    result.update(market_as_of=market, knowledge_as_of=cutoff.isoformat(), evaluation=None,
                  evaluation_id=None, label=None, label_available_at=None, outcome_eligible=False,
                  model_eligible=False, eligible=False, verdict='unknown', period_status='unknown',
                  data_status='unknown', evaluation_status='unscored', window_snapshot={},
                  evaluation_reasons=[], recording_status=case.get('source_recording_status', 'recorded'))
    available, issues = [], []
    for row in case.get('evaluation_history', []):
        try:
            if row.get('application_id', row.get('premise_application_id')) != case['application_id']:
                raise ValueError('Evaluation application mismatch')
            if _available(row) <= cutoff and row['market_as_of'] <= market:
                available.append(row)
        except Exception as exc:
            issues.append({'evaluation_id': row.get('evaluation_id'), 'error': str(exc)})
    selected = None
    if available:
        # Choose the latest observed market date first; a later knowledge cutoff
        # for an older market date must not overwrite a newer observed period.
        latest_date = max(x['market_as_of'] for x in available)
        candidates = [x for x in available if x['market_as_of'] == latest_date]
        superseded = {identifier for x in candidates for identifier in x.get('supersedes', [])}
        candidates = [x for x in candidates if x['evaluation_id'] not in superseded]
        # A historical backfill is explicitly not a replacement for an already
        # newer revision. Preserve competing current branches as a conflict.
        ids = {x['evaluation_id'] for x in candidates}
        candidates = [x for x in candidates if not (set(x.get('newer_revision_ids', [])) & ids)]
        if len(candidates) == 1:
            selected = candidates[0]
        else:
            result.update(evaluation_status='conflict', evaluation_reasons=['conflicting_evaluation_revisions'],
                          conflicting_evaluation_ids=sorted(x['evaluation_id'] for x in candidates))
    if selected:
        result.update(evaluation=selected, evaluation_id=selected['evaluation_id'], evaluation_status='selected',
                      verdict=selected.get('verdict', 'unknown'), period_status=selected.get('period_status', 'unknown'),
                      data_status=selected.get('data_status', 'unknown'),
                      selected_market_as_of=selected['market_as_of'], stale_evaluation=selected['market_as_of'] < market,
                      window_snapshot=copy.deepcopy(selected.get('window_snapshot') or {}),
                      label_available_at=_available(selected).isoformat(),
                      evaluation_reasons=copy.deepcopy(selected.get('reasons', [])))
        if selected.get('recording_status') != 'recorded':
            result['recording_status'] = 'recording_error'
    for attempt in case.get('scoring_history', []):
        try:
            when = _time(attempt['available_at'])
            if when <= cutoff and attempt.get('market_as_of', '') <= market and (
                    selected is None or when >= _available(selected)
                    or (attempt.get('scoring_job_id') and attempt.get('scoring_job_id') == selected.get('scoring_job_id'))) and attempt.get('recording_status') != 'recorded':
                result['recording_status'] = 'recording_error'
                result['evaluation_reasons'].append(attempt.get('reason', 'newer_scoring_recording_error'))
        except (ValueError, KeyError, TypeError) as exc:
            issues.append({'scoring_job_id': attempt.get('scoring_job_id'), 'error': str(exc)})
    if issues:
        result['recording_status'] = 'recording_error'
        result['evaluation_reasons'].append('evaluation_source_error')
    result['projection_errors'] = issues
    result['outcome_eligible'] = (result['recording_status'] == 'recorded' and selected is not None
        and result['period_status'] == 'ended' and result['data_status'] == 'complete'
        and result['verdict'] in ('refuted', 'maintained'))
    if result['outcome_eligible']:
        result['label'] = int(result['verdict'] == 'refuted')
    result['model_eligible'] = result['outcome_eligible'] and bool(result.get('feature_eligible'))
    result['eligible'] = result['model_eligible']
    return result


def _reference_returns(contexts, execution, judgment_ids, cutoff, market_as_of):
    verified = {o['order_attempt_id'] for o in execution.get('orders', [])
                if o.get('financial_attribution') and o.get('link_status') == 'linked'}
    output = {}
    for snapshot in sorted(contexts, key=lambda x: x.get('captured_at', '')):
        if (_time(snapshot['captured_at']) > cutoff or snapshot.get('market_as_of', '') > market_as_of
                or snapshot.get('recording_status') != 'recorded'):
            continue
        original = (snapshot.get('inputs') or {}).get('journal', [])
        for audit in (snapshot.get('audit') or {}).get('evaluations', []):
            index = audit.get('journal_source_index')
            if not isinstance(index, int) or not 0 <= index < len(original):
                continue
            row = original[index]
            if row.get('judgment_id') not in judgment_ids or row.get('order_attempt_id') not in verified:
                continue
            value = _number(audit.get('return_unrounded'))
            if value is not None:
                output[row['order_attempt_id']] = {'order_attempt_id': row['order_attempt_id'],
                    'judgment_id': row['judgment_id'], 'reference_return': value,
                    'kind': 'legacy_action_price_return', 'net_pnl_verified': False,
                    'snapshot_role': snapshot.get('snapshot_role'),
                    'source_sha256': S.digest(snapshot),
                    'scoring_job_id': snapshot.get('scoring_job_id'), 'journal_source_index': index,
                    'captured_at': snapshot['captured_at'], 'market_as_of': snapshot['market_as_of'],
                    'audit_snapshot': copy.deepcopy(audit), 'journal_snapshot': copy.deepcopy(row)}
    return list(output.values())


@__import__("evaluation_context").scoped
def build_cases(market_as_of, knowledge_as_of):
    cutoff = _time(knowledge_as_of)
    if cutoff > C.now() or date.fromisoformat(market_as_of) > cutoff.astimezone(C.KST).date():
        raise ValueError('Future comparison cutoff')
    committed = J.committed_premises(knowledge_as_of, market_as_of)
    import validation_holdout as VH
    withheld = VH.discovery_exclusions([*committed['applications'], *committed.get('unavailable_applications', [])])
    blocked = {x['application_id'] for x in withheld}
    errors, sources = list(committed['errors']), []
    reads = {kind: SS.records(kind) for kind in ('evaluations', 'jobs', 'job_items', 'job_results', 'legacy_contexts')}
    for kind, read in reads.items():
        errors.extend(read['errors'])
    # Freeze only source rows available by this report's cutoffs. The histories
    # themselves are shown to review AI and must not carry later revisions.
    evaluations, definitions = [], {}
    for row in reads['evaluations']['records']:
        if row.get('application_id', row.get('premise_application_id')) in blocked:
            continue
        try:
            if _available(row) <= cutoff and row['market_as_of'] <= market_as_of:
                evaluations.append(row)
                sources.append({'kind': 'scoring/evaluations', 'id': row['evaluation_id'], 'sha256': S.digest(row)})
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({'kind': 'evaluation_source', 'evaluation_id': row.get('evaluation_id'), 'error': str(exc)})
    for identifier in sorted({x.get('evaluator_definition_id') for x in evaluations} - {None}):
        try:
            definition = SS.load('definitions', identifier)
            if not definition or definition.get('definition_id') != identifier:
                raise ValueError('Original evaluator definition unavailable')
            definitions[identifier] = definition
            sources.append({'kind': 'scoring/definitions', 'id': identifier, 'sha256': S.digest(definition)})
        except (OSError, ValueError, TypeError) as exc:
            errors.append({'kind': 'evaluation_definition_source', 'definition_id': identifier, 'error': str(exc)})
    results = {r['scoring_job_id']: r for r in reads['job_results']['records']}
    items = {(x['scoring_job_id'], x['application_id']): x for x in reads['job_items']['records']}
    histories = defaultdict(list)
    expected = {x['application_id']: x for x in committed.get('unavailable_applications', []) if x['application_id'] not in blocked}
    for job in reads['jobs']['records']:
        try:
            if _time(job['started_at']) > cutoff or job['market_as_of'] > market_as_of:
                continue
            sources.append({'kind': 'scoring/jobs', 'id': job['scoring_job_id'], 'sha256': S.digest(job)})
            terminal = results.get(job['scoring_job_id'])
            finished = terminal and _time(terminal['finished_at']) <= cutoff
            if finished:
                sources.append({'kind': 'scoring/job_results', 'id': job['scoring_job_id'], 'sha256': S.digest(terminal)})
            for target in job.get('expected_targets', []):
                if target['application_id'] in blocked:
                    continue
                expected[target['application_id']] = target
                item = items.get((job['scoring_job_id'], target['application_id']))
                if finished and item:
                    sources.append({'kind': 'scoring/job_items', 'id': target['application_id'], 'sha256': S.digest(item)})
                histories[target['application_id']].append({
                    'scoring_job_id': job['scoring_job_id'], 'market_as_of': job['market_as_of'],
                    'available_at': terminal['finished_at'] if finished else job['started_at'],
                    'recording_status': item.get('recording_status', 'recording_error') if finished and item else 'recording_error',
                    'reason': 'unfinished_scoring_job' if not finished else 'scoring_item_missing' if not item else 'scoring_recording_error'})
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({'kind': 'scoring_job', 'error': str(exc)})
    cases = []
    found = set()
    for entry in committed['applications']:
        app = entry['application']
        if app['application_id'] in blocked:
            continue
        found.add(app['application_id'])
        try:
            origin, source = _read_original('judgments', app['judgment_id']); sources.append(source)
            _, source = _read_original('premises', app['application_id']); sources.append(source)
            _, source = _read_original('submissions', origin['acceptance_event_id']); sources.append(source)
            context = extract_case_context(origin, app)
            sources.extend(context['source_manifest'])
            execution = EC.execution_context(entry['judgment_ids'], knowledge_as_of)
            if execution.get('recording_status') != 'recorded':
                errors.extend(execution.get('errors', []))
            context.update(judgment_ids=entry['judgment_ids'], execution_context=execution,
                reference_returns=_reference_returns(reads['legacy_contexts']['records'], execution, set(entry['judgment_ids']), cutoff, market_as_of),
                source_recording_status='recording_error' if reads['evaluations']['errors'] else 'recorded',
                evaluation_definition_ids=sorted({x.get('evaluator_definition_id') for x in evaluations
                    if x.get('application_id', x.get('premise_application_id')) == app['application_id']} - {None}),
                evaluation_history=[copy.deepcopy(x) for x in evaluations
                                    if x.get('application_id', x.get('premise_application_id')) == app['application_id']],
                scoring_history=histories[app['application_id']])
            cases.append(project_case(context, market_as_of, knowledge_as_of))
        except Exception as exc:
            errors.append({'application_id': app['application_id'], 'error': str(exc)})
            cases.append({'application_id': app['application_id'], 'code': app['code'],
                'origin_judgment_id': app['judgment_id'], 'judgment_date': app['judgment_date'],
                'origin_at': app.get('created_at'), 'application_snapshot': app,
                'features': {}, 'stratum': {}, 'stratum_id': None, 'feature_eligible': False,
                'feature_reasons': ['source_recording_error'], 'source_recording_status': 'recording_error',
                'recording_status': 'recording_error', 'outcome_eligible': False, 'model_eligible': False,
                'eligible': False, 'label': None, 'label_available_at': None, 'verdict': 'unknown',
                'evaluation_status': 'source_error', 'evaluation_history': [], 'scoring_history': histories[app['application_id']]})
    for identifier, target in expected.items():
        if identifier in found:
            continue
        errors.append({'application_id': identifier, 'reason': 'previously_committed_target_unavailable'})
        cases.append({'application_id': identifier, 'code': target.get('code'), 'judgment_ids': target.get('judgment_ids', []),
                      'judgment_date': target.get('judgment_date'), 'origin_at': target.get('origin_at'),
                      'origin_judgment_id': target.get('origin_judgment_id'),
                      'features': {}, 'stratum': {}, 'stratum_id': None, 'feature_eligible': False,
                      'feature_reasons': ['committed_source_unavailable'], 'recording_status': 'recording_error',
                      'source_recording_status': 'recording_error', 'outcome_eligible': False,
                      'model_eligible': False, 'eligible': False, 'label': None, 'label_available_at': None,
                      'verdict': 'unknown', 'evaluation_status': 'source_error', 'evaluation_history': [],
                      'scoring_history': histories[identifier]})
    return {'dataset_version': DATASET_VERSION, 'cases': sorted(cases, key=lambda x: x['application_id']),
            'expected_count': len(cases), 'target_set_incomplete': bool(errors) or committed['target_set_incomplete'],
            'errors': errors, 'source_manifest': sources, 'source_definitions': definitions,
            'withheld_for_validation': withheld, 'withheld_count': len(withheld),
            'market_as_of': market_as_of, 'knowledge_as_of': knowledge_as_of}


def _state(case):
    if case.get('recording_status') != 'recorded': return 'recording_error'
    if case.get('evaluation_status') == 'conflict': return 'conflict'
    if case.get('evaluation_status') == 'unscored': return 'unscored'
    if case.get('outcome_eligible'): return 'complete_' + case['verdict']
    if case.get('verdict') == 'refuted' and case.get('period_status') != 'ended': return 'early_refuted'
    if case.get('period_status') in ('not_started', 'in_progress'): return 'in_progress'
    if case.get('period_status') == 'ended' and case.get('data_status') != 'complete': return 'data_incomplete'
    return 'unknown'


def _summary(cases):
    states = Counter(_state(c) for c in cases)
    complete = [c for c in cases if c.get('outcome_eligible')]
    refuted = sum(c.get('label') == 1 for c in complete)
    outcome_exclusions, model_exclusions = Counter(), Counter()
    for c in cases:
        if not c.get('outcome_eligible'):
            outcome_exclusions.update(set(c.get('evaluation_reasons') or [_state(c)]))
        if not c.get('model_eligible'):
            model_exclusions.update(set(c.get('feature_reasons', [])) |
                                    (set(c.get('evaluation_reasons') or [_state(c)]) if not c.get('outcome_eligible') else set()))
    return {'case_count': len(cases), 'states': dict(states), 'complete_count': len(complete),
            'refuted_count': refuted, 'maintained_count': len(complete) - refuted,
            'refutation_rate': refuted / len(complete) if complete else None,
            'unknown_or_unfinished_count': len(cases) - len(complete),
            'model_eligible_count': sum(bool(c.get('model_eligible')) for c in cases),
            'outcome_exclusion_reasons': dict(outcome_exclusions), 'model_exclusion_reasons': dict(model_exclusions),
            'instrument_count': len({c.get('code') for c in cases if c.get('code')}),
            'judgment_date_count': len({c.get('judgment_date') for c in cases if c.get('judgment_date')}),
            'case_ids': [c['application_id'] for c in cases]}


def compare_cases(cases, calendar=None):
    groups, pairs, pairable = defaultdict(list), [], []
    sessions = [x['date'] for x in (calendar or {}).get('sessions', [])] if (calendar or {}).get('status') == 'complete' else []
    positions = {day: index for index, day in enumerate(sessions)}
    for c in cases:
        features, flows = c.get('features') or {}, c.get('signed_flows') or {}
        gap, slope = features.get('gap'), features.get('slope')
        gap_band = 'unknown' if gap is None else '<=0' if gap <= 0 else '(0,0.005]' if gap <= .005 else '(0.005,0.02]' if gap <= .02 else '>0.02'
        slope_sign = 'unknown' if slope is None else 'falling' if slope < 0 else 'rising' if slope > 0 else 'equal'
        key = {'stratum_id': c.get('stratum_id'), 'regime': features.get('regime'),
               'institution_direction': flows.get('institution_direction', 'unknown'),
               'gap_band': gap_band, 'slope_sign': slope_sign, 'relative_flow_size': flows.get('relative_size', 'unknown')}
        groups[S.canonical(key)].append(c)
        if (c.get('feature_eligible') and features.get('regime') and c.get('judgment_date') in positions
                and gap is not None and slope is not None
                and flows.get('institution_direction') in ('buy', 'sell')):
            pairable.append(c)
    used = set()
    for anchor in sorted((c for c in pairable if c['signed_flows']['institution_direction'] == 'sell'), key=lambda c: (c['judgment_date'], c['application_id'])):
        options = []
        for candidate in pairable:
            if candidate['application_id'] in used or candidate['signed_flows']['institution_direction'] != 'buy': continue
            if candidate['stratum'] != anchor['stratum'] or candidate['features']['regime'] != anchor['features']['regime']: continue
            days = abs(positions[anchor['judgment_date']] - positions[candidate['judgment_date']])
            gap = abs(anchor['features']['gap'] - candidate['features']['gap'])
            slope = abs(anchor['features']['slope'] - candidate['features']['slope'])
            if days <= 5 and gap <= .005 and slope <= .01:
                options.append((days / 5 + gap / .005 + slope / .01, candidate['application_id'], candidate, days, gap, slope))
        if options:
            distance, _, candidate, days, gap, slope = min(options, key=lambda x: (x[0], x[1]))
            used.update((anchor['application_id'], candidate['application_id']))
            pairs.append({'sell_case_id': anchor['application_id'], 'buy_case_id': candidate['application_id'],
                          'distance': distance, 'trading_days_apart': days, 'gap_difference': gap,
                          'slope_difference': slope, 'selection_policy': 'outcome_blind_one_to_one'})
    overlaps = []
    for index, first in enumerate(cases):
        a = set((first.get('window_snapshot') or {}).get('sessions', []))
        for second in cases[index+1:]:
            b = set((second.get('window_snapshot') or {}).get('sessions', []))
            if a & b and first.get('code') == second.get('code'):
                overlaps.append({'case_ids': [first['application_id'], second['application_id']], 'shared_sessions': sorted(a & b)})
    return {'definition': {'version': 'CASE_COMPARISON@v1', 'gap_bounds': [0, .005, .02],
                          'pair_max_trading_days': 5, 'pair_max_gap': .005, 'pair_max_slope': .01},
            'summary': _summary(cases),
            'groups': [{'group_id': 'group-' + S.digest(json.loads(key))[:24], 'conditions': json.loads(key), **_summary(value)}
                       for key, value in sorted(groups.items())],
            'pairs': pairs, 'unpaired_case_ids': [c['application_id'] for c in cases if c['application_id'] not in used],
            'pairing_status': 'available' if positions else 'calendar_unavailable',
            'same_instrument_overlaps': overlaps,
            'same_date_counts': dict(Counter(c['judgment_date'] for c in cases if c.get('judgment_date'))),
            'interpretation': 'observed_counts_and_associations_not_causal_effects'}
