"""Weekly research snapshots, separate from trading, tuning and AI coverage.

Only this explicit job builds research. Read tools return immutable snapshots;
AI statements cannot edit measurements, labels, models or original premises.
"""
from __future__ import annotations

import copy
import math
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

import common as C
import case_store as S

SPEC_VERSION = 'CASE_REVIEW@v1'
SINGLE_WORKER_SECONDS = 1500


def instant(value=None):
    value = C.now() if value is None else value
    result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    return result.astimezone(C.KST)


def weekly_cutoff(now=None):
    now = instant(now)
    saturday = (now - timedelta(days=(now.weekday() - 5) % 7)).replace(hour=10, minute=0, second=0, microsecond=0)
    return saturday - timedelta(days=7) if saturday > now else saturday


def _definition():
    names = ('case_review.py', 'case_dataset.py', 'case_statistics.py', 'case_prediction.py',
             'case_store.py', 'judgment_context.py', 'research_evidence.py')
    sources = {name: Path(__file__).with_name(name).read_text(encoding='utf-8') for name in names}
    packages = {}
    for name in ('statsmodels', 'scipy', 'numpy', 'pandas'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = 'not_installed'
    body = {'spec_version': SPEC_VERSION, 'sources': sources, 'dependencies': packages}
    body['definition_id'] = 'case-definition-' + S.digest(body)
    S.archive('definitions', body['definition_id'], body)
    return body


def _calendar(cases, cutoff, market):
    import market_calendar as MC
    dates = [c['judgment_date'] for c in cases if isinstance(c.get('judgment_date'), str)
             and len(c['judgment_date']) == 10]
    first = min(dates, default=market)
    return MC.find_calendar_snapshot(first, market, cutoff, allow_partial=True)


def _metric_catalog(value, path='', output=None):
    """Only computed summary/model/performance fields become verifiable claims."""
    output = [] if output is None else output
    if isinstance(value, dict):
        for key in sorted(value):
            _metric_catalog(value[key], path + '/' + str(key), output)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _metric_catalog(item, path + '/' + str(index), output)
    elif value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Nonfinite computed metric')
        unit = 'bool' if isinstance(value, bool) else 'number' if isinstance(value, (int, float)) else 'text'
        leaf = path.rsplit('/', 1)[-1]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and (
                'rate' in leaf or 'probability' in leaf or leaf in ('roc_auc', 'auc', 'brier', 'log_loss')):
            unit = 'ratio'
        output.append({'metric_id': 'metric-' + S.digest(path)[:24], 'path': path,
                       'value': value, 'display_value': round(value, 6) if isinstance(value, float) else value,
                       'unit': unit, 'precision': 6 if isinstance(value, float) else None,
                       'status': 'unknown' if value is None else 'computed'})
    return output


def _summary_metrics(value):
    """Keep frozen full inputs in artifacts, without multiplying AI fact rows."""
    if isinstance(value, dict):
        return {k: _summary_metrics(v) for k, v in value.items() if k not in
                ('training_inputs', 'training_case_ids', 'calendar_axis', 'definition', 'predictions')}
    if isinstance(value, list):
        return [_summary_metrics(x) for x in value]
    return value


def _publish_models(review_id, models, cutoff):
    # Publications are usable only after the final commit exists. A crash while
    # writing the individual rows cannot publish half a batch.
    monday = (cutoff + timedelta(days=(7 - cutoff.weekday()) % 7 or 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    available = instant().isoformat()
    ids = []
    for model in models:
        if model.get('status') not in ('ready', 'fitted', 'available', 'ok'):
            continue
        mid = model['model_id']
        pid = 'publication-' + S.digest([review_id, mid])
        body = {'publication_id': pid, 'review_id': review_id, 'model_id': mid, 'available_at': available,
                'valid_from': monday.isoformat(), 'valid_until': (monday + timedelta(days=7)).isoformat(), 'status': 'published'}
        S.archive('model_publications', pid, body)
        ids.append(pid)
    S.archive('publication_commits', review_id, {'review_id': review_id, 'publication_ids': ids,
                                               'published_at': instant().isoformat()})
    return ids


def _changes(cases, cutoff, previous):
    """Compare immutable case snapshots, including outcome corrections."""
    if previous is None:
        return {'previous_review_id': None, 'new': [c['application_id'] for c in cases], 'changed': [], 'unchanged': []}
    old = S.load('datasets', previous['review_id'])
    by = {c['application_id']: c for c in old['cases']} if old else {}
    groups = {'new': [], 'changed': [], 'unchanged': []}
    def signature(case):
        # Report time itself is not a changed observation. Relevant original
        # inputs, selected evaluation and recording failures remain compared.
        keys = ('application_id', 'origin_judgment_id', 'features', 'stratum', 'adoption',
                'decision_regime_snapshot', 'comparison_context', 'evaluation_id',
                'verdict', 'period_status', 'data_status', 'recording_status',
                'outcome_eligible', 'model_eligible', 'feature_reasons', 'evaluation_reasons',
                'conflicting_evaluation_ids', 'judgment_ids', 'reference_returns')
        return S.digest({k: case.get(k) for k in keys})
    for case in cases:
        key = case['application_id']
        category = 'new' if key not in by else 'unchanged' if signature(case) == signature(by[key]) else 'changed'
        groups[category].append(key)
    return {'previous_review_id': previous['review_id'], **groups}


def build_review(market_as_of=None, knowledge_as_of=None, *, revision=None, timeout_seconds=SINGLE_WORKER_SECONDS):
    import case_dataset as D
    import case_statistics as ST
    import case_prediction as P
    import research_evidence as RE

    cutoff = instant(knowledge_as_of) if knowledge_as_of else weekly_cutoff()
    if cutoff > instant():
        raise ValueError('Future knowledge cutoff is forbidden')
    market = date.fromisoformat(market_as_of) if market_as_of else (cutoff - timedelta(days=(cutoff.weekday() - 4) % 7 or 7)).date()
    if market > cutoff.date():
        raise ValueError('Market cutoff cannot exceed knowledge cutoff')
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError('Positive calculation timeout required')
    if revision is not None and (not isinstance(revision, str) or not revision.strip() or len(revision) > 100):
        raise ValueError('revision must be a nonempty key up to 100 characters')
    rid = 'case-review-' + S.digest([market.isoformat(), cutoff.isoformat(), revision])[:40]
    try:
        if S.load('jobs', rid):
            return get_case_review(rid)
        with S.locked('weekly-build', timeout=0):
            if S.load('jobs', rid):
                return get_case_review(rid)
            previous_results = S.records('results')
            previous = max((r for r in previous_results['records']
                            if r.get('analysis_status') == 'complete' and instant(r['knowledge_as_of']) <= cutoff),
                           key=lambda r: (r['knowledge_as_of'], r['finished_at']), default=None)
            manifest = {'review_id': rid, 'spec_version': SPEC_VERSION,
                        'market_as_of': market.isoformat(), 'knowledge_as_of': cutoff.isoformat(),
                        'revision': revision, 'supersedes': previous['review_id'] if revision and previous else None,
                        'started_at': instant().isoformat(),
                        'timeout_seconds': timeout_seconds, 'run_status': 'building'}
            S.archive('jobs', rid, manifest)
            started = time.monotonic()
            deadline = started + timeout_seconds
            definition = _definition()
            selected = D.build_cases(manifest['market_as_of'], manifest['knowledge_as_of'])
            cases = selected['cases']
            # Reserve discovery use before publishing or analysing the dataset.
            # An interrupted build cannot later masquerade as unused holdout data.
            RE.reserve_usage(rid, [c['application_id'] for c in cases],
                             source_refs={'selection_hash': S.digest(selected)},
                             purpose='step5_discovery_and_prediction_evaluation')
            calendar = _calendar(cases, manifest['knowledge_as_of'], manifest['market_as_of'])
            dataset = {**manifest, **selected, 'calendar': calendar, 'definition_id': definition['definition_id']}
            dataset['input_hash'] = S.digest(selected)
            S.archive('datasets', rid, dataset)
            comparison = D.compare_cases(cases, calendar=calendar)
            errors = copy.deepcopy(selected.get('errors', [])) + previous_results['errors']
            incomplete = bool(selected.get('target_set_incomplete')) or bool(errors)
            comparison['target_set_incomplete'] = incomplete
            comparison['rate_scope'] = 'observed_recorded_targets_only' if incomplete else 'discovery_eligible_application_population' if selected.get('withheld_count') else 'accepted_application_population'
            comparison['withheld_count'] = selected.get('withheld_count', 0)
            if incomplete:
                statistics = {'models': [], 'groups': [], 'status': 'source_incomplete', 'errors': []}
                retrospective = {'predictions': [], 'folds': [], 'status': 'source_incomplete'}
                prospective = {'status': 'source_incomplete'}
            else:
                statistics = ST.fit_groups(cases, calendar, manifest['knowledge_as_of'], deadline=deadline)
                retrospective = P.retrospective(cases, calendar, manifest['knowledge_as_of'], deadline=deadline)
                try:
                    prospective = P.prospective_performance(cases, calendar, manifest['knowledge_as_of'], deadline=deadline)
                except TimeoutError as exc:
                    prospective = {'status': 'timeout', 'errors': [{'error': str(exc)}], 'groups': []}
                errors.extend(statistics.get('errors', []))
                errors.extend(retrospective.get('errors', []))
                errors.extend(prospective.get('errors', []))
                for name, value in (('statistics', statistics), ('retrospective', retrospective), ('prospective', prospective)):
                    if value.get('status') in ('timeout', 'incomplete'):
                        errors.append({'operation': name, 'status': value['status']})
            if time.monotonic() > deadline:
                errors.append({'operation': 'build_review', 'status': 'timeout', 'error': 'Calculation deadline exceeded'})
            models = statistics.get('models', [])
            for model in models:
                S.archive('models', model['model_id'], model)
            result = {**manifest, 'definition_id': definition['definition_id'], 'input_hash': dataset['input_hash'],
                      'finished_at': instant().isoformat(), 'expected_count': len(cases),
                      'case_ids': [c['application_id'] for c in cases], 'comparison': comparison,
                      'withheld_for_validation':selected.get('withheld_for_validation',[]),
                      'withheld_count':selected.get('withheld_count',0),
                      'weekly_changes': _changes(cases, cutoff, previous),
                      'calendar_readiness': {k: calendar.get(k) for k in
                          ('calendar_id', 'status', 'reason', 'coverage_start', 'coverage_end',
                           'requested_coverage_start', 'requested_coverage_end', 'partial_requested_coverage',
                           'component_calendar_ids', 'limitations')},
                      'statistics': statistics, 'retrospective': retrospective, 'prospective': prospective,
                      'target_set_incomplete': incomplete, 'errors': errors,
                      'analysis_status': 'partial' if errors or incomplete else 'complete',
                      'recording_status': 'recorded', 'usage_scope': 'research_only_not_unused_validation_for_step7'}
            # Do not duplicate thousands of training rows as text evidence. Full
            # artifacts remain frozen; the AI receives the computed summaries.
            visible_statistics = _summary_metrics(statistics)
            result['metric_catalog'] = _metric_catalog({'comparison': comparison, 'statistics': visible_statistics,
                                                       'prospective': prospective})
            # Retrospective prediction rows need not be printed as evidence: the
            # summary and metrics suffice; case pages carry original references.
            result['metric_catalog'] += _metric_catalog({'retrospective': _summary_metrics(retrospective)})
            S.archive('results', rid, result)
            S.archive('research_usage', rid, {'review_id': rid, 'used_at': instant().isoformat(),
                       'application_ids': result['case_ids'], 'source_manifest': selected.get('source_manifest', {}),
                       'knowledge_as_of': manifest['knowledge_as_of'], 'purpose': 'step5_discovery_and_prediction_evaluation'})
            RE.record_usage(rid, result['case_ids'], 'step5_discovery_and_prediction_evaluation',
                            source_refs={'result_hash': S.digest(result), 'input_hash': dataset['input_hash']})
            if result['analysis_status'] == 'complete':
                _publish_models(rid, models, cutoff)
            return get_case_review(rid)
    except S.StoreBusy:
        # Another weekly worker is healthy. Contention must never poison its
        # immutable result with a recording_error or publish a competing model.
        current = get_case_review(rid)
        return {**current, 'run_status': 'busy' if current.get('run_status') == 'not_found' else current['run_status'],
                'worker_status': 'busy'}
    except Exception as exc:
        error = S.record_error(rid, 'build_review', exc)
        try:
            S.archive('job_failures', 'failure-' + uuid.uuid4().hex,
                      {'review_id': rid, 'created_at': instant().isoformat(),
                       'status': 'timeout' if isinstance(exc, TimeoutError) else 'recording_error', 'error': error})
        except Exception:
            pass
        return {**get_case_review(rid), 'review_id': rid, 'run_status': 'partial',
                'recording_status': 'recording_error', 'errors': [error]}


def get_case_review(review_id):
    """Read-only projection; never completes, collects or recomputes a job."""
    try:
        job = S.load('jobs', review_id)
        if job is None:
            return {'review_id': review_id, 'run_status': 'not_found', 'recording_status': 'unknown'}
        result = S.load('results', review_id)
        submissions = S.records('review_submissions')
        attempts = S.records('ai_attempts')
        terminals = S.records('ai_results')
        failures = S.records('job_failures')
        stored_errors = S.errors(review_id)
        errors = submissions['errors'] + attempts['errors'] + terminals['errors'] + failures['errors'] + stored_errors['errors'] + stored_errors['records']
        own = [x for x in submissions['records'] if x.get('review_id') == review_id]
        ai_starts = [x for x in attempts['records'] if x.get('review_id') == review_id]
        ai_ends = [x for x in terminals['records'] if x.get('review_id') == review_id]
        failures = [x for x in failures['records'] if x.get('review_id') == review_id]
        result = copy.deepcopy(result or {**job, 'analysis_status': 'unfinished'})
        # A result file is written before research usage and model publication.
        # The final (possibly empty) publication commit closes that transaction.
        # A killed worker in between must not look complete or launch review AI.
        awaiting_commit = (job.get('spec_version') == SPEC_VERSION and result.get('analysis_status') == 'complete'
                           and S.load('publication_commits', review_id) is None)
        if awaiting_commit:
            result.update(stored_analysis_status='complete', analysis_status='unfinished', recording_status='incomplete')
        accepted = [x for x in own if x.get('status') == 'accepted']
        status = ('not_applicable' if result.get('expected_count') == 0 and not result.get('target_set_incomplete') else
                  'accepted' if accepted else 'rejected' if own else 'not_submitted')
        latest_end = max(ai_ends, key=lambda x: x.get('created_at', ''), default={})
        execution = latest_end.get('status', 'unfinished' if ai_starts else 'not_started')
        result.update(ai_status=status, ai_submissions=own, ai_attempts=ai_starts, ai_results=ai_ends,
                      ai_execution_status=execution,
                      failures=failures, recording_status='recording_error' if errors or failures else result.get('recording_status', 'incomplete'))
        if not S.load('results', review_id) or awaiting_commit:
            result['run_status'] = 'unfinished'
        elif errors or failures or result['analysis_status'] != 'complete':
            result['run_status'] = 'partial'
        elif execution in ('error', 'timeout', 'unfinished'):
            result['run_status'] = 'partial'
        elif status in ('accepted', 'not_applicable'):
            result['run_status'] = 'complete'
        elif execution == 'not_requested':
            result['run_status'] = 'analysis_only'
        else:
            result['run_status'] = 'awaiting_ai'
        result['errors'] = result.get('errors', []) + errors
        return result
    except Exception as exc:
        return {'review_id': review_id, 'run_status': 'recording_error', 'recording_status': 'recording_error',
                'errors': [{'error': str(exc)}]}


def get_review_cases(review_id, offset=0, limit=50):
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be a nonnegative integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('limit must be 1..100')
    try:
        dataset = S.load('datasets', review_id)
        if dataset is None:
            return {'review_id': review_id, 'cases': [], 'status': 'not_found'}
        return {'review_id': review_id, 'offset': offset, 'total': len(dataset['cases']),
                'cases': dataset['cases'][offset:offset+limit], 'target_set_incomplete': dataset.get('target_set_incomplete', False),
                'recording_status': 'recorded'}
    except Exception as exc:
        return {'review_id': review_id, 'cases': [], 'recording_status': 'recording_error', 'errors': [str(exc)]}


def _validate_finding(finding, metrics):
    if not isinstance(finding, dict) or set(finding) - {'id', 'metric_id', 'op', 'value', 'unit', 'direction', 'right_metric_id', 'operator', 'result'}:
        raise ValueError('SCHEMA_ERROR: facts require structured fields, not free text')
    if not isinstance(finding.get('id'), str) or not finding['id']:
        raise ValueError('SCHEMA_ERROR: finding id required')
    source = metrics.get(finding.get('metric_id'))
    if source is None:
        raise ValueError('UNKNOWN_REFERENCE')
    value, op = source['value'], finding.get('op', 'value')
    if op == 'value':
        claimed = finding.get('value')
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(claimed, bool) or claimed is None:
                raise ValueError('FACT_MISMATCH')
            actual, claim = Decimal(str(value)), Decimal(str(claimed))
            if not claim.is_finite():
                raise ValueError('FACT_MISMATCH')
            unit = finding.get('unit', source['unit'])
            if unit in ('%', 'percent') and source['unit'] == 'ratio':
                claim /= 100
            elif unit != source['unit']:
                raise ValueError('UNIT_MISMATCH')
            precision = source.get('precision')
            step = Decimal(1).scaleb(-precision) if precision is not None else Decimal(1)
            mismatch = claim.quantize(step) != actual.quantize(step) if precision is not None else claim != actual
            if mismatch:
                raise ValueError('FACT_MISMATCH')
        elif type(claimed) is not type(value) or claimed != value:
            raise ValueError('UNKNOWN_AS_ZERO' if value is None and claimed == 0 else 'FACT_MISMATCH')
    elif op == 'direction':
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError('FACT_MISMATCH')
        direction = 'positive' if value > 0 else 'negative' if value < 0 else 'zero'
        if finding.get('direction') != direction:
            raise ValueError('FACT_MISMATCH')
    elif op == 'compare':
        right = metrics.get(finding.get('right_metric_id'))
        if right is None or right['unit'] != source['unit'] or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (value, right['value'])):
            raise ValueError('COMPARISON_MISMATCH')
        a, b = Decimal(str(value)), Decimal(str(right['value']))
        ops = {'gt': a > b, 'gte': a >= b, 'lt': a < b, 'lte': a <= b, 'eq': a == b}
        if finding.get('operator') not in ops or not isinstance(finding.get('result'), bool) or finding['result'] != ops[finding['operator']]:
            raise ValueError('FACT_MISMATCH')
    else:
        raise ValueError('SCHEMA_ERROR: unsupported fact operation')
    return {**copy.deepcopy(finding), 'reference_validation': 'passed', 'fact_validation': 'passed',
            'metric_snapshot': source,
            'server_summary': f"{source['path']}: {source['display_value']} ({source['unit']}); 검증된 계산값, 인과 판단 아님"}


def _rejected_attempt(review_id, request_key, findings, hypotheses, errors):
    """Even schema rejection is an observed submission, not an omission."""
    try:
        raw = {'request_key': request_key, 'findings': findings, 'hypotheses': hypotheses}
        try:
            S.canonical(raw)
        except (ValueError, TypeError):
            raw = {'unserializable_submission_repr': repr(raw)}
        attempt_id = 'review-rejection-' + uuid.uuid4().hex
        if not S.load('results', review_id):
            return {'status': 'rejected', 'errors': errors}
        record = {'review_id': review_id, 'submission_id': attempt_id, 'created_at': instant().isoformat(),
                  'status': 'rejected', 'errors': errors, 'raw_submission': raw, 'findings': [], 'hypotheses': []}
        S.archive('review_submissions', attempt_id, record)
        return record
    except Exception as exc:
        return {'status': 'recording_error', 'errors': [S.record_error(review_id, 'rejected_submission', exc)]}


def record_case_review(review_id, request_key, findings, hypotheses):
    """Item-level validation, immutable request replay, unverified hypotheses."""
    if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
        return _rejected_attempt(review_id, request_key, findings, hypotheses, [{'path': 'request_key', 'code': 'SCHEMA_ERROR'}])
    if not isinstance(findings, list) or not isinstance(hypotheses, list):
        return _rejected_attempt(review_id, request_key, findings, hypotheses, [{'path': 'submission', 'code': 'SCHEMA_ERROR'}])
    try:
        payload_hash = S.digest({'findings': findings, 'hypotheses': hypotheses})
    except (TypeError, ValueError):
        return _rejected_attempt(review_id, request_key, findings, hypotheses, [{'path': 'submission', 'code': 'SCHEMA_ERROR'}])
    key = 'review-request-' + S.digest([review_id, request_key])
    try:
        with S.locked(key):
            old = S.load('review_submissions', key)
            if old:
                return ({**old, 'replayed': True} if old['request_hash'] == payload_hash else
                        {'status': 'rejected', 'errors': [{'code': 'IDEMPOTENCY_CONFLICT'}]})
            result = S.load('results', review_id)
            if result is None:
                return {'status': 'rejected', 'errors': [{'code': 'UNKNOWN_REVIEW'}]}
            metrics = {x['metric_id']: x for x in result['metric_catalog']}
            good, proposed, errors, seen = [], [], [], set()
            for index, finding in enumerate(findings):
                try:
                    item = _validate_finding(finding, metrics)
                    if item['id'] in seen:
                        raise ValueError('DUPLICATE_ID')
                    seen.add(item['id']); good.append(item)
                except (ValueError, TypeError, InvalidOperation) as exc:
                    errors.append({'path': f'findings[{index}]', 'code': str(exc).split(':')[0]})
            valid_cases = set(result['case_ids'])
            hypothesis_ids = set()
            for index, hypothesis in enumerate(hypotheses):
                try:
                    if not isinstance(hypothesis, dict) or set(hypothesis) - {'id','text','finding_ids','case_ids'}:
                        raise ValueError('SCHEMA_ERROR')
                    if not isinstance(hypothesis.get('text'), str) or not hypothesis['text'].strip():
                        raise ValueError('SCHEMA_ERROR')
                    if not isinstance(hypothesis.get('id'), str) or not hypothesis['id'] or hypothesis['id'] in hypothesis_ids:
                        raise ValueError('SCHEMA_ERROR')
                    refs, case_refs = hypothesis.get('finding_ids', []), hypothesis.get('case_ids', [])
                    if (not isinstance(refs, list) or not isinstance(case_refs, list) or not (refs or case_refs)
                            or any(x not in seen for x in refs) or any(x not in valid_cases for x in case_refs)):
                        raise ValueError('UNKNOWN_REFERENCE')
                    hypothesis_ids.add(hypothesis['id'])
                    proposed.append({**copy.deepcopy(hypothesis), 'hypothesis_status': 'submitted_unverified',
                                     'role': 'retrospective_explanation_not_original_premise'})
                except (ValueError, TypeError) as exc:
                    errors.append({'path': f'hypotheses[{index}]', 'code': str(exc)})
            if not good:
                errors.append({'path': 'findings', 'code': 'NO_VERIFIED_FINDING'})
            record = {'review_id': review_id, 'request_key': request_key, 'request_hash': payload_hash,
                      'submission_id': key, 'created_at': instant().isoformat(),
                      'status': 'accepted' if not errors else 'rejected', 'findings': good,
                      'hypotheses': proposed, 'errors': errors,
                      'item_results': [{'id': x['id'], 'status': 'accepted'} for x in good] + errors,
                      'raw_submission': {'findings': findings, 'hypotheses': hypotheses}}
            S.archive('review_submissions', key, record)
            return record
    except Exception as exc:
        error = S.record_error(review_id, 'record_case_review', exc)
        return {'status': 'recording_error', 'errors': [error]}
