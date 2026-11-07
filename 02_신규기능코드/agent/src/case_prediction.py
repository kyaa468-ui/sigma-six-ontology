"""Isolated premise forecasts: historical replay is never a live prediction.

Only a first acceptance callback can create a prospective forecast. The forecast
is not returned to trading tools. Models become usable only after an immutable
publication commit and are valid for the explicitly published following week.
"""
from __future__ import annotations

import copy
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import common as C
import case_statistics as ST
import case_store as S

PREDICTION_VERSION = 'PREMISE_PREDICTION@v1'
MAX_CALLBACK_DELAY_SECONDS = 60


def _summary(row):
    # The acceptance tool may report bookkeeping, but never a research probability.
    return {key: row.get(key) for key in ('application_id', 'prediction_id', 'status')}


def _choose_model(case, origin_at):
    cutoff = ST.instant(origin_at)
    publications = S.records('model_publications')
    if publications['errors']:
        raise ValueError('publication_history_corrupt')
    choices = []
    for publication in publications['records']:
        if publication.get('status') != 'published':
            continue
        if ST.instant(publication['available_at']) > cutoff:
            continue
        if not ST.instant(publication['valid_from']) <= cutoff < ST.instant(publication['valid_until']):
            continue
        commit = S.load('publication_commits', publication['review_id'])
        if not commit or publication.get('publication_id') not in commit.get('publication_ids', []):
            continue
        if ST.instant(commit['published_at']) > cutoff:
            continue
        model = S.load('models', publication['model_id'])
        if not model:
            raise ValueError('published_model_missing')
        if model.get('stratum_id') == ST.stratum_key(case):
            if ST.instant(model['training_cutoff']) >= cutoff:
                raise ValueError('model_training_not_before_origin')
            choices.append((publication, model))
    if not choices:
        return None, None
    newest = max(ST.instant(p['available_at']) for p, _ in choices)
    selected = [(p, m) for p, m in choices if ST.instant(p['available_at']) == newest]
    if len({m['model_id'] for _, m in selected}) != 1:
        raise ValueError('ambiguous_model_publication')
    return selected[0]


def record_new_premises(judgment):
    """Fail-open bookkeeping, called after the acceptance commit, never on replay."""
    import case_dataset as D
    results = []
    for application in judgment.get('premise_applications', []):
        if application.get('judgment_id') != judgment.get('judgment_id'):
            continue
        identifier = application['application_id']
        prediction_id = 'prediction-' + S.digest([PREDICTION_VERSION, identifier])
        try:
            with S.locked(prediction_id):
                existing = S.load('predictions', prediction_id)
                if existing is not None:
                    results.append(_summary(existing)); continue
                target = S.load('prediction_targets', prediction_id)
                if target is not None:
                    # A crash after starting cannot be repaired using a newer
                    # model or a now-known outcome on a later callback.
                    results.append({'application_id': identifier, 'prediction_id': prediction_id,
                                    'status': 'unfinished'}); continue
                started = C.now().isoformat()
                target = {'prediction_id': prediction_id, 'application_id': identifier,
                          'origin_judgment_id': judgment['judgment_id'], 'code': application['code'],
                          'origin_at': judgment['created_at'], 'started_at': started,
                          'prediction_version': PREDICTION_VERSION, 'kind': 'prospective'}
                S.archive('prediction_targets', prediction_id, target)
                row = {**target, 'created_at': C.now().isoformat(), 'status': 'not_predicted'}
                elapsed = (ST.instant(row['created_at']) - ST.instant(row['origin_at'])).total_seconds()
                import validation_holdout as VH
                reserved = VH.is_reserved_judgment(judgment)
                if reserved:
                    row.update(status='validation_reserved', validation_ids=reserved, reason='reserved_before_research_feature_extraction')
                elif not 0 <= elapsed <= MAX_CALLBACK_DELAY_SECONDS:
                    row.update(status='late_callback_not_predicted', reason='first_acceptance_callback_window_expired')
                else:
                    case = D.extract_case_context(judgment, application)
                    row.update(features=copy.deepcopy(case.get('features')), stratum=copy.deepcopy(case.get('stratum')),
                               stratum_id=ST.stratum_key(case), feature_hash=ST.digest(case.get('features')),
                               comparison_context=copy.deepcopy(case.get('comparison_context')),
                               feature_reasons=copy.deepcopy(case.get('feature_reasons', [])))
                    try:
                        if case.get('feature_eligible') is not True:
                            raise ValueError('original_measurement_context_incomplete')
                        ST.numerical_features(case)
                    except (ValueError, TypeError) as exc:
                        row.update(status='features_unavailable', reason=str(exc))
                    else:
                        publication, model = _choose_model(case, row['origin_at'])
                        if model is None:
                            row.update(status='no_model', reason='no_committed_model_for_this_origin_and_stratum')
                        else:
                            probability, baseline = ST.predict(model, case)
                            row.update(status='predicted', model_id=model['model_id'],
                                       publication_id=publication['publication_id'], publication_snapshot=publication,
                                       training_cutoff=model['training_cutoff'], probability=probability,
                                       baseline_probability=baseline, prediction_definition=copy.deepcopy(ST.SPEC),
                                       parameter_count=len(model['columns']),
                                       feature_definition=case.get('case_context_version'))
                S.archive('predictions', prediction_id, row)
                results.append(_summary(row))
        except Exception as exc:
            S.record_error(None, 'prospective_prediction:' + identifier, exc)
            results.append({'application_id': identifier, 'prediction_id': prediction_id,
                            'status': 'recording_error', 'error': str(exc)})
    return {'items': results, 'recording_status': 'recording_error' if any(r['status'] == 'recording_error' for r in results) else 'recorded'}


def _saturdays(start, end):
    current = start + timedelta(days=(5-start.weekday()) % 7)
    while current <= end:
        yield datetime.combine(current, datetime.min.time(), tzinfo=C.KST).replace(hour=10)
        current += timedelta(days=7)


def retrospective(cases, calendar, knowledge_as_of, deadline=None):
    """Frozen-source expanding-window replay. Historic model existence is not claimed."""
    import case_dataset as D
    import validation_holdout as VH
    cases, withheld = VH.filter_cases(cases)
    cutoff = ST.instant(knowledge_as_of)
    if not cases:
        return {'kind': 'retrospective', 'status': 'completed', 'folds': [], 'predictions': [],
                'performance': evaluate_predictions([], [], calendar, cutoff.isoformat(), kind='retrospective'), 'errors': []}
    first = min(ST.instant(c['origin_at']).date() for c in cases)
    predictions, folds, errors = [], [], []
    try:
        # Include the first cohort's preceding Saturday, even when that fold
        # has no training cases. Otherwise its missing model would vanish from
        # the retrospective denominator.
        for training_cutoff in _saturdays(first - timedelta(days=7), cutoff.date()):
            ST.check_deadline(deadline)
            if training_cutoff >= cutoff:
                continue
            test_start = (training_cutoff + timedelta(days=2)).replace(hour=0)
            test_end = test_start + timedelta(days=7)
            targets = [c for c in cases if test_start <= ST.instant(c['origin_at']) < min(test_end, cutoff)]
            if not targets:
                continue
            training, purged = [], []
            market_as_of = (training_cutoff.date() - timedelta(days=1)).isoformat()
            for case in cases:
                if ST.instant(case['origin_at']) >= training_cutoff:
                    continue
                projected = D.project_case(case, market_as_of, training_cutoff.isoformat())
                sessions = (projected.get('window_snapshot') or {}).get('sessions', [])
                if not sessions or sessions[-1] >= test_start.date().isoformat():
                    purged.append({'application_id': case['application_id'], 'reason': 'overlapping_or_unresolved_label_window'})
                    continue
                training.append(projected)
            fits = ST.fit_groups(training, calendar, training_cutoff.isoformat(), deadline, uncertainty=False)
            if fits['status'] == 'timeout':
                raise TimeoutError('retrospective_model_deadline')
            errors.extend({'training_cutoff': training_cutoff.isoformat(), **error}
                          for error in fits.get('errors', []))
            if fits.get('status') == 'incomplete':
                errors.append({'training_cutoff': training_cutoff.isoformat(),
                               'reason': 'retrospective_training_source_incomplete'})
            models = {m['stratum_id']: m for m in fits['models']}
            fold = {'training_cutoff': training_cutoff.isoformat(), 'test_start': test_start.isoformat(),
                    'test_end': test_end.isoformat(), 'target_ids': sorted(c['application_id'] for c in targets),
                    'training_ids': sorted(c['application_id'] for c in training), 'purged': purged,
                    'model_statuses': fits['groups'], 'models': fits['models'],
                    'errors': fits.get('errors', []), 'kind': 'retrospective',
                    'computed_at': C.now().isoformat(), 'historical_model_existence': 'not_claimed'}
            fold['fold_id'] = 'replay-fold-' + S.digest(fold)
            folds.append(fold)
            for case in sorted(targets, key=lambda c: (c['origin_at'], c['application_id'])):
                row = {'kind': 'retrospective', 'application_id': case['application_id'], 'code': case['code'],
                       'origin_at': case['origin_at'], 'created_at': C.now().isoformat(), 'fold_id': fold['fold_id'],
                       'stratum': copy.deepcopy(case['stratum']), 'stratum_id': ST.stratum_key(case),
                       'features': copy.deepcopy(case['features']), 'feature_hash': ST.digest(case['features']),
                       'training_cutoff': training_cutoff.isoformat(), 'status': 'no_model'}
                model = models.get(row['stratum_id'])
                if model is not None and fits.get('status') == 'completed':
                    try:
                        if case.get('feature_eligible') is not True:
                            raise ValueError('original_measurement_context_incomplete')
                        probability, baseline = ST.predict(model, case)
                        row.update(status='predicted', probability=probability, baseline_probability=baseline,
                                   model_id=model['model_id'], parameter_count=len(model['columns']),
                                   prediction_definition=copy.deepcopy(ST.SPEC))
                    except (ValueError, TypeError, KeyError) as exc:
                        row.update(status='features_unavailable', reason=str(exc))
                row['prediction_id'] = 'replay-prediction-' + S.digest(row)
                predictions.append(row)
        performance = evaluate_predictions(cases, predictions, calendar, cutoff.isoformat(),
                                           kind='retrospective', deadline=deadline)
        errors.extend(performance.get('errors', []))
        return {'kind': 'retrospective', 'status': 'incomplete' if errors or performance.get('status') == 'incomplete' else 'completed',
                'folds': folds, 'predictions': predictions,
                'performance': performance, 'errors': errors,
                'historical_model_existence': 'not_claimed', 'computed_at': C.now().isoformat()}
    except TimeoutError as exc:
        return {'kind': 'retrospective', 'status': 'timeout', 'folds': folds, 'predictions': predictions,
                'performance': None, 'errors': [{'reason': str(exc)}]}


def evaluate_predictions(cases, predictions, calendar, knowledge_as_of, *, kind='prospective', deadline=None):
    """Publish performance only after the same sample/period guards are met.

The denominator includes missing forecasts and unavailable labels. A later
outcome revision changes this evaluation, never the original probabilities.
"""
    import validation_holdout as VH
    cases, withheld = VH.filter_cases(cases)
    blocked_ids = {x['application_id'] for x in withheld}
    predictions = [p for p in predictions if p.get('application_id') not in blocked_ids]
    cutoff = ST.instant(knowledge_as_of)
    if kind not in ('prospective', 'retrospective'):
        raise ValueError('unknown_prediction_kind')
    available, errors = [], []
    for row in predictions:
        if row.get('kind') != kind:
            continue
        try:
            # Retrospective predictions are explicitly computed now, but the
            # simulated folds remain bounded by their own training cutoffs.
            if kind == 'prospective' and ST.instant(row['created_at']) > cutoff:
                continue
            available.append(row)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append({'prediction_id': row.get('prediction_id'), 'error': str(exc)})
    by = defaultdict(list)
    for row in available: by[row['application_id']].append(row)
    unique, conflicts = {}, set()
    for case in cases:
        identifier = case['application_id']
        if identifier in unique and S.digest(unique[identifier]) != S.digest(case):
            errors.append({'application_id': identifier, 'reason': 'conflicting_case_versions'})
            conflicts.add(identifier)
        else:
            unique[identifier] = case
    if kind == 'prospective':
        expected = [c for c in unique.values() if c.get('case_context_version') or c['application_id'] in by]
        historical = [c['application_id'] for c in unique.values() if c not in expected]
    else:
        expected = [c for c in unique.values() if c['application_id'] in by]
        historical = []
    pairs, states = defaultdict(list), []
    for case in sorted(expected, key=lambda c: c['application_id']):
        identifier = case['application_id']
        state = {'application_id': identifier, 'status': 'not_recorded'}
        if identifier in conflicts:
            state.update(status='conflicting_case_versions'); states.append(state); continue
        own = by.get(identifier, [])
        if len(own) > 1:
            # Repeated identical archives do not add observations; divergent
            # probabilities are a conflict, never selected by performance.
            hashes = {S.digest({k:v for k,v in p.items() if k != 'prediction_id'}) for p in own}
            if len(hashes) != 1:
                errors.append({'application_id': identifier, 'reason': 'conflicting_predictions'})
                state.update(status='conflicting_predictions'); states.append(state); continue
            own = own[:1]
        if not own:
            states.append(state); continue
        row = own[0]
        state.update(status=row['status'], prediction_id=row['prediction_id'])
        if row['status'] != 'predicted':
            states.append(state); continue
        try:
            if row.get('code') != case.get('code') or row.get('origin_at') != case.get('origin_at'):
                raise ValueError('prediction_origin_mismatch')
            if ST.instant(row['training_cutoff']) >= ST.instant(case['origin_at']):
                raise ValueError('training_not_before_test_origin')
            if kind == 'prospective':
                elapsed = (ST.instant(row['created_at']) - ST.instant(case['origin_at'])).total_seconds()
                if not 0 <= elapsed <= MAX_CALLBACK_DELAY_SECONDS:
                    raise ValueError('not_a_first_acceptance_prediction')
            if row.get('stratum_id') != ST.stratum_key(case) or row.get('feature_hash') != ST.digest(case.get('features')):
                raise ValueError('prediction_features_or_stratum_changed')
            if not case.get('outcome_eligible') or isinstance(case.get('label'), bool) or case.get('label') not in (0, 1):
                state.update(status='outcome_unavailable'); states.append(state); continue
            if ST.instant(case['label_available_at']) > cutoff:
                raise ValueError('outcome_not_yet_available')
            if case.get('evaluation_record_available_at') and ST.instant(case['evaluation_record_available_at']) > cutoff:
                raise ValueError('evaluation_not_yet_recorded')
            probability, baseline = float(row['probability']), float(row['baseline_probability'])
            # Validation also covers NaN and values outside [0,1].
            ST.prediction_metrics([case['label']], [probability])
            ST.prediction_metrics([case['label']], [baseline])
            state.update(status='scorable', label=case['label'], evaluation_id=case.get('evaluation_id'))
            pairs[ST.stratum_key(case)].append((case, row))
        except (ValueError, KeyError, TypeError) as exc:
            state.update(status='invalid_prediction_or_outcome', reason=str(exc))
        states.append(state)
    groups = []
    for key, pair in sorted(pairs.items()):
        ST.check_deadline(deadline)
        rows = [c for c, _ in pair]
        strata = rows[0]['stratum']
        # The estimated model's parameter count cannot shrink merely because a
        # test period happened to contain fewer regime categories.
        parameters = max(p.get('parameter_count', 6 + len(ST.SPEC['regime_order'])-1) for _, p in pair)
        guard = ST.sample_guard(rows, parameters, strata['days'])
        group = {'stratum_id': key, 'stratum': copy.deepcopy(strata), 'sample_guard': guard,
                 'application_ids': [c['application_id'] for c in rows],
                 'prediction_ids': [p['prediction_id'] for _, p in pair],
                 'status': 'insufficient_data', 'model_metrics': None, 'baseline_metrics': None}
        if guard['eligible']:
            try:
                axis = ST.calendar_axis(rows, calendar, cutoff.isoformat())
                y = [c['label'] for c in rows]
                model = [p['probability'] for _, p in pair]
                baseline = [p['baseline_probability'] for _, p in pair]
                length = max(5, 2*strata['days'])
                group.update(status='available', model_metrics=ST.prediction_metrics(y, model),
                             baseline_metrics=ST.prediction_metrics(y, baseline),
                             loss_difference_intervals={
                                 'primary': ST.loss_difference_intervals(rows, y, model, baseline, axis, length, deadline=deadline),
                                 'sensitivity_2L': ST.loss_difference_intervals(rows, y, model, baseline, axis, 2*length, deadline=deadline)})
            except (ValueError, TypeError, KeyError) as exc:
                group.update(status='unavailable', reason=str(exc), model_metrics=None, baseline_metrics=None)
        groups.append(group)
    counts = dict(Counter(s['status'] for s in states))
    return {'kind': kind, 'knowledge_as_of': cutoff.isoformat(), 'expected_count': len(expected),
            'withheld_count':len(withheld),
            'historical_not_expected': historical, 'counts': counts, 'targets': states,
            'groups': groups, 'errors': errors, 'coverage_rate': None if not expected or errors else
                sum(s['status'] in ('scorable', 'outcome_unavailable') for s in states)/len(expected),
            'status': 'incomplete' if errors else 'completed',
            'interpretation': 'premise_refutation_prediction_only_not_trading_performance'}


def prospective_performance(cases, calendar, knowledge_as_of, deadline=None):
    read = S.records('predictions')
    targets = S.records('prediction_targets')
    failed = S.errors()
    cutoff = ST.instant(knowledge_as_of)
    forecasts = list(read['records'])
    existing = {p.get('application_id') for p in forecasts}
    failures = {}
    for error in failed['records']:
        operation = error.get('operation', '')
        if operation.startswith('prospective_prediction:') and ST.instant(error['recorded_at']) <= cutoff:
            failures[operation.split(':', 1)[1]] = error
    pending = {r['application_id']: r for r in targets['records'] if ST.instant(r['started_at']) <= cutoff}
    for case in cases:
        identifier = case['application_id']
        if identifier in existing or (identifier not in pending and identifier not in failures):
            continue
        target, error = pending.get(identifier), failures.get(identifier)
        forecasts.append({'kind': 'prospective', 'application_id': identifier,
                          'prediction_id': (target or {}).get('prediction_id', 'prediction-' + S.digest([PREDICTION_VERSION, identifier])),
                          'created_at': error['recorded_at'] if error else target['started_at'],
                          'status': 'recording_error' if error else 'unfinished',
                          'reason': error.get('error') if error else 'acceptance_callback_started_without_durable_result'})
    result = evaluate_predictions(cases, forecasts, calendar, knowledge_as_of, deadline=deadline)
    result['errors'].extend(read['errors'] + targets['errors'] + failed['errors'])
    if result['errors']:
        result.update(status='incomplete', coverage_rate=None)
    return result
