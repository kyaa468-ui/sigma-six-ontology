"""Prespecified, observational premise models. No broker or trading policy calls.

All transforms are learned on the supplied training cases. The uncertainty
procedure resamples whole consecutive trading-date blocks, not independent rows.
Models are JSON coefficients and definitions; no executable model pickle is used.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import warnings
from collections import Counter, defaultdict
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import common as C

SPEC = {
    'version': 'PREMISE_LOGIT@v1',
    'features': ['gap', 'slope', 'foreign_ratio', 'institution_ratio'],
    'interaction': 'standardized_gap * standardized_slope',
    'regime_order': ['상승', '횡보', '하락'],
    'label': {'refuted': 1, 'maintained': 0},
    'min_cases': 200, 'min_each_label': 30, 'cases_per_parameter_each_label': 10,
    'min_instruments': 10, 'min_date_blocks': 10, 'block_length': 'max(5, 2*N)',
    'bootstrap_repetitions': 1000, 'bootstrap_seed': 20261107,
    'bootstrap_min_success_fraction': .95, 'interval': [.025, .975],
    'max_condition_number': 1e8, 'separation_tolerance': 1e-8,
    'fit_maxiter': 100, 'fit_tolerance': 1e-9,
    'libraries': {'statsmodels': '0.15.0', 'scipy': '1.18.1'},
    'interpretation': 'exploratory_conditional_association_not_causal_or_trading_gain',
    'population': 'observed_instruments_not_new_instrument_population',
}


def digest(value):
    import storage_objects
    return storage_objects.fingerprint(value)[0]


def instant(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone_required')
    return stamp.astimezone(C.KST)


def check_deadline(deadline):
    if deadline is not None and ((isinstance(deadline, datetime) and C.now() >= deadline)
                                 or (not isinstance(deadline, datetime) and time.monotonic() >= deadline)):
        raise TimeoutError('case_statistics_deadline')


def stratum_key(case):
    return digest(case.get('stratum') or {})


def numerical_features(case):
    values = case.get('features') or {}
    numbers = []
    for name in SPEC['features']:
        value = values.get(name)
        if isinstance(value, bool) or value is None:
            raise ValueError('missing_feature:' + name)
        value = float(value)
        if not math.isfinite(value):
            raise ValueError('nonfinite_feature:' + name)
        numbers.append(value)
    if values.get('regime') not in SPEC['regime_order']:
        raise ValueError('unknown_regime')
    return numbers, values['regime']


def eligible_cases(cases, knowledge_as_of):
    """Reject future labels and duplicate/conflicting applications before fitting."""
    cutoff = instant(knowledge_as_of)
    by, errors, exclusions = {}, [], []
    for case in cases:
        identifier = case.get('application_id')
        if not identifier:
            errors.append({'reason': 'missing_application_id'})
            continue
        if identifier in by:
            if digest(by[identifier]) != digest(case):
                errors.append({'application_id': identifier, 'reason': 'conflicting_duplicate_application'})
            continue
        by[identifier] = case
    rejected = {e.get('application_id') for e in errors}
    accepted = []
    for identifier, case in sorted(by.items()):
        try:
            if identifier in rejected:
                raise ValueError('conflicting_duplicate_application')
            if case.get('model_eligible') is not True or case.get('outcome_eligible') is not True:
                raise ValueError('case_not_model_eligible')
            if isinstance(case.get('label'), bool) or case.get('label') not in (0, 1):
                raise ValueError('label_unavailable')
            if instant(case['label_available_at']) > cutoff:
                raise ValueError('future_label')
            if instant(case['origin_at']) > cutoff:
                raise ValueError('future_origin')
            if case.get('evaluation_record_available_at') and instant(case['evaluation_record_available_at']) > cutoff:
                raise ValueError('future_evaluation_record')
            numerical_features(case)
            fields=('application_id','code','judgment_date','origin_at','features','stratum','label','label_available_at',
                    'evaluation_id','evaluation_record_available_at','window_snapshot','model_eligible','outcome_eligible')
            accepted.append({k:copy.deepcopy(case[k]) for k in fields if k in case})
        except (ValueError, TypeError, KeyError) as exc:
            exclusions.append({'application_id': identifier, 'reason': str(exc)})
    return accepted, exclusions, errors


def calendar_axis(cases, calendar, knowledge_as_of=None):
    if calendar.get('status') != 'complete':
        raise ValueError('calendar_unavailable')
    if knowledge_as_of and calendar.get('available_at') and instant(calendar['available_at']) > instant(knowledge_as_of):
        raise ValueError('calendar_not_available_at_cutoff')
    dates = [r['date'] for r in calendar.get('sessions', [])]
    if dates != sorted(set(dates)):
        raise ValueError('calendar_dates_not_unique_ordered')
    used = {c['judgment_date'] for c in cases}
    if not used or not used <= set(dates):
        raise ValueError('origin_dates_outside_calendar')
    return [d for d in dates if min(used) <= d <= max(used)]


def _design(cases, transform=None):
    import numpy as np
    raw, regime = zip(*(numerical_features(c) for c in cases))
    raw = np.asarray(raw, dtype=float)
    if transform is None:
        mean, scale = raw.mean(axis=0), raw.std(axis=0, ddof=0)
        if np.any(scale <= 0) or not np.all(np.isfinite(scale)):
            raise ValueError('feature_variation_insufficient')
        categories = [r for r in SPEC['regime_order'] if r in regime]
        transform = {'mean': mean.tolist(), 'scale': scale.tolist(), 'features': list(SPEC['features']),
                     'regimes': categories, 'regime_reference': categories[0],
                     'interaction': SPEC['interaction']}
    else:
        if transform.get('features') != SPEC['features'] or any(r not in transform['regimes'] for r in regime):
            raise ValueError('feature_schema_or_regime_outside_training_support')
        mean, scale = np.asarray(transform['mean']), np.asarray(transform['scale'])
        if mean.shape != (4,) or scale.shape != (4,) or np.any(scale <= 0):
            raise ValueError('invalid_frozen_transform')
    standardized = (raw - mean) / scale
    matrix = [np.ones(len(cases)), *standardized.T, standardized[:, 0] * standardized[:, 1]]
    columns = ['intercept', *['z_' + name for name in SPEC['features']], 'z_gap:z_slope']
    for category in transform['regimes'][1:]:
        matrix.append(np.asarray([int(r == category) for r in regime]))
        columns.append('regime:' + category)
    matrix = np.column_stack(matrix)
    if not np.all(np.isfinite(matrix)):
        raise ValueError('nonfinite_design')
    return matrix, transform, columns


def _fit_design(matrix, labels, deadline=None):
    """Check identification and complete/quasi separation independently of fit flags."""
    import numpy as np
    from scipy.optimize import linprog
    import statsmodels.api as sm
    check_deadline(deadline)
    if len(set(map(int, labels))) != 2:
        raise ValueError('single_label')
    if np.linalg.matrix_rank(matrix) < matrix.shape[1]:
        raise ValueError('rank_deficient')
    condition = float(np.linalg.cond(matrix))
    if not math.isfinite(condition) or condition > SPEC['max_condition_number']:
        raise ValueError('ill_conditioned')
    signed = matrix * (2 * np.asarray(labels) - 1)[:, None]
    # beta=u-v, ||beta||1<=1. A positive nonnegative-margin direction is
    # complete or quasi separation; the zero vector alone is not separation.
    objective = np.r_[-signed.sum(axis=0), signed.sum(axis=0)]
    constraints = np.vstack((np.column_stack((-signed, signed)), np.ones((1, matrix.shape[1] * 2))))
    rhs = np.r_[np.zeros(len(matrix)), 1.0]
    lp = linprog(objective, A_ub=constraints, b_ub=rhs, bounds=(0, None), method='highs',
                 options={'time_limit': 5, 'primal_feasibility_tolerance': 1e-9, 'dual_feasibility_tolerance': 1e-9})
    if not lp.success:
        raise ValueError('separation_check_unresolved')
    if -float(lp.fun) > SPEC['separation_tolerance']:
        raise ValueError('complete_or_quasi_separation')
    check_deadline(deadline)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter('always')
        fitted = sm.Logit(labels, matrix, check_rank=True).fit(method='newton',
            maxiter=SPEC['fit_maxiter'], tol=SPEC['fit_tolerance'], disp=False)
    if any('separation' in str(w.message).lower() for w in seen):
        raise ValueError('separation_warning')
    if not fitted.mle_retvals.get('converged'):
        raise ValueError('not_converged')
    coefficients = np.asarray(fitted.params)
    if not np.all(np.isfinite(coefficients)):
        raise ValueError('nonfinite_coefficients')
    information = -np.asarray(fitted.model.hessian(coefficients))
    if np.linalg.matrix_rank(information) < matrix.shape[1] or np.linalg.cond(information) > SPEC['max_condition_number']:
        raise ValueError('unidentified_information_matrix')
    return coefficients, {'iterations': int(fitted.mle_retvals.get('iterations', 0)),
                          'condition_number': condition, 'log_likelihood': float(fitted.llf)}


def sample_guard(cases, parameter_count, days):
    counts = Counter(c['label'] for c in cases)
    block = max(5, 2 * int(days))
    minimum_label = max(SPEC['min_each_label'], SPEC['cases_per_parameter_each_label'] * parameter_count)
    actual = {'cases': len(cases), 'labels': {str(k): counts[k] for k in (0, 1)},
              'instruments': len({c['code'] for c in cases}), 'judgment_dates': len({c['judgment_date'] for c in cases}),
              'parameters': parameter_count, 'block_length': block}
    reasons = []
    if actual['cases'] < SPEC['min_cases']: reasons.append('insufficient_cases')
    if any(counts[k] < minimum_label for k in (0, 1)): reasons.append('insufficient_each_label')
    if actual['instruments'] < SPEC['min_instruments']: reasons.append('insufficient_instruments')
    if actual['judgment_dates'] < SPEC['min_date_blocks'] * block: reasons.append('insufficient_judgment_dates')
    return {'eligible': not reasons, 'reasons': reasons, 'actual': actual,
            'required': {'cases': SPEC['min_cases'], 'each_label': minimum_label,
                         'instruments': SPEC['min_instruments'], 'judgment_dates': SPEC['min_date_blocks'] * block},
            'meaning': 'operational_start_guard_not_a_statistical_guarantee'}


def block_indices(cases, axis, length, rng):
    """Moving date blocks. Every case on a drawn date stays together."""
    import numpy as np
    if length <= 0 or len(axis) < length:
        raise ValueError('insufficient_block_axis')
    by = defaultdict(list)
    for index, case in enumerate(cases): by[case['judgment_date']].append(index)
    drawn = []
    while len(drawn) < len(axis):
        start = int(rng.integers(0, len(axis) - length + 1))
        drawn.extend(axis[start:start + length])
    dates = drawn[:len(axis)]
    return np.asarray([i for d in dates for i in by[d]], dtype=int), dates


def bootstrap_intervals(cases, matrix, labels, axis, length, *, repetitions=None, deadline=None):
    import numpy as np
    repetitions = SPEC['bootstrap_repetitions'] if repetitions is None else repetitions
    rng = np.random.default_rng(SPEC['bootstrap_seed'])
    estimates, failures = [], Counter()
    for _ in range(repetitions):
        check_deadline(deadline)
        indices, _dates = block_indices(cases, axis, length, rng)
        try:
            beta, _ = _fit_design(matrix[indices], labels[indices], deadline)
            estimates.append(beta)
        except (ValueError, ArithmeticError, np.linalg.LinAlgError) as exc:
            failures[str(exc)] += 1
    success = len(estimates) / repetitions if repetitions else 0
    available = success >= SPEC['bootstrap_min_success_fraction']
    return {'status': 'available' if available else 'bootstrap_unstable', 'method': 'moving_trading_date_blocks',
            'block_length': length, 'repetitions': repetitions, 'successful': len(estimates),
            'success_fraction': success, 'failures': dict(failures), 'seed': SPEC['bootstrap_seed'],
            'coefficient_intervals': np.quantile(estimates, SPEC['interval'], axis=0).T.tolist() if available else None,
            'transform_policy': 'original_training_standardization_held_fixed',
            'interpretation': 'exploratory_95_percent_pointwise_intervals_conditional_on_observed_instruments'}


def fit_groups(cases, calendar, knowledge_as_of, deadline=None, *, uncertainty=True):
    import numpy as np
    cutoff = instant(knowledge_as_of).isoformat()
    selected, excluded, errors = eligible_cases(cases, cutoff)
    groups = defaultdict(list)
    all_strata = {stratum_key(c): copy.deepcopy(c.get('stratum') or {}) for c in cases}
    for case in selected: groups[stratum_key(case)].append(case)
    output, statuses = [], []
    dependencies = {name: version(name) for name in ('statsmodels', 'scipy', 'numpy', 'pandas')}
    definition = {'spec': copy.deepcopy(SPEC), 'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'dependencies': dependencies}
    for key, stratum in sorted(all_strata.items()):
        rows = sorted(groups[key], key=lambda c: (c['judgment_date'], c['application_id']))
        status = {'stratum_id': key, 'stratum': stratum, 'status': 'insufficient_data',
                  'case_ids': [c['application_id'] for c in rows],
                  'excluded': [e for e in excluded if any(c.get('application_id') == e['application_id'] and stratum_key(c) == key for c in cases)]}
        try:
            check_deadline(deadline)
            if any(dependencies[name] != expected for name, expected in SPEC['libraries'].items()):
                raise ValueError('dependency_version_mismatch')
            days = stratum.get('days')
            if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
                raise ValueError('unknown_horizon')
            regimes = [r for r in SPEC['regime_order'] if any(c['features']['regime'] == r for c in rows)]
            guard = sample_guard(rows, 6 + max(0, len(regimes) - 1), days)
            status['sample_guard'] = guard
            if not guard['eligible']:
                status['reasons'] = guard['reasons']; statuses.append(status); continue
            axis = calendar_axis(rows, calendar, cutoff)
            matrix, transform, columns = _design(rows)
            labels = np.asarray([c['label'] for c in rows], dtype=int)
            beta, fit = _fit_design(matrix, labels, deadline)
            length = max(5, 2 * days)
            intervals = {'primary': {'status': 'not_requested'}, 'sensitivity_2L': {'status': 'not_requested'}}
            if uncertainty:
                intervals = {'primary': bootstrap_intervals(rows, matrix, labels, axis, length, deadline=deadline),
                             'sensitivity_2L': bootstrap_intervals(rows, matrix, labels, axis, 2 * length, deadline=deadline)}
            artifact = {'model_version': SPEC['version'], 'status': 'ready', 'stratum_id': key, 'stratum': stratum,
                        'definition': definition, 'training_cutoff': cutoff, 'computed_at': C.now().isoformat(),
                        'training_case_ids': status['case_ids'], 'training_inputs': [{
                            'application_id': c['application_id'], 'code': c['code'], 'judgment_date': c['judgment_date'],
                            'origin_at': c['origin_at'], 'features': c['features'], 'label': c['label'],
                            'label_available_at': c['label_available_at'], 'evaluation_id': c.get('evaluation_id'),
                            'window_snapshot': c.get('window_snapshot')} for c in rows],
                        'sample_guard': guard, 'transform': transform, 'columns': columns, 'coefficients': beta.tolist(),
                        'baseline_probability': float(labels.mean()), 'fit': fit, 'uncertainty': intervals,
                        'calendar_id': calendar.get('calendar_id'), 'calendar_hash': calendar.get('hash'),
                        'calendar_axis': axis, 'publication_status': 'not_published',
                        'interpretation': SPEC['interpretation']}
            artifact['training_input_hash'] = digest(artifact['training_inputs'])
            artifact['model_id'] = 'case-model-' + digest(artifact)
            output.append(artifact)
            status.update(status='ready', model_id=artifact['model_id'], uncertainty_status=intervals['primary']['status'])
        except TimeoutError as exc:
            status.update(status='timeout', reasons=[str(exc)]); statuses.append(status)
            return {'models': [], 'groups': statuses, 'errors': errors, 'excluded': excluded,
                    'recording_status': 'not_recorded', 'status': 'timeout', 'definition': definition}
        except (ValueError, TypeError, KeyError, ArithmeticError, np.linalg.LinAlgError) as exc:
            status.update(status='unavailable', reasons=[str(exc)])
        statuses.append(status)
    return {'models': output, 'groups': statuses, 'errors': errors, 'excluded': excluded,
            'recording_status': 'not_recorded', 'status': 'incomplete' if errors else 'completed', 'definition': definition}


def predict(model, case):
    """Evaluate frozen JSON coefficients only. No fit, provider or outcome reads."""
    import numpy as np
    from scipy.special import expit
    if model.get('status') != 'ready' or model.get('model_version') != SPEC['version']:
        raise ValueError('model_not_ready_or_unsupported')
    if stratum_key(case) != model.get('stratum_id'):
        raise ValueError('stratum_mismatch')
    matrix, _transform, columns = _design([case], model['transform'])
    beta = np.asarray(model['coefficients'], dtype=float)
    if columns != model['columns'] or beta.shape != (matrix.shape[1],) or not np.all(np.isfinite(beta)):
        raise ValueError('invalid_model_coefficients')
    probability = float(expit(matrix @ beta)[0])
    baseline = float(model['baseline_probability'])
    if not 0 <= baseline <= 1 or not math.isfinite(probability):
        raise ValueError('invalid_probability')
    return probability, baseline


def prediction_metrics(labels, probabilities):
    """Exact paired metrics; AUC uses pair rankings, including half credit for ties."""
    import numpy as np
    from scipy.stats import rankdata
    raw_y, p = np.asarray(labels), np.asarray(probabilities, dtype=float)
    if not len(raw_y) or len(raw_y) != len(p) or not np.isin(raw_y, [0, 1]).all() or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError('invalid_prediction_arrays')
    y = raw_y.astype(int)
    clipped = np.clip(p, 1e-12, 1 - 1e-12)
    n1, n0 = int(y.sum()), int(len(y) - y.sum())
    auc = float((rankdata(p)[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)) if n1 and n0 else None
    calibration = []
    bins = np.minimum((p * 10).astype(int), 9)
    for index in range(10):
        use = bins == index
        calibration.append({'bin': index, 'lower': index / 10, 'upper': (index + 1) / 10,
                            'upper_inclusive': index == 9, 'count': int(use.sum()),
                            'mean_probability': float(p[use].mean()) if use.any() else None,
                            'observed_refutation_rate': float(y[use].mean()) if use.any() else None})
    return {'brier': float(np.mean((p-y)**2)),
            'log_loss': float(np.mean(-(y*np.log(clipped)+(1-y)*np.log(1-clipped)))),
            'roc_auc': auc, 'auc_status': 'available' if auc is not None else 'single_label',
            'calibration': calibration, 'count': len(y), 'log_clip_epsilon': 1e-12}


def loss_difference_intervals(cases, labels, model_probabilities, baseline_probabilities,
                              axis, length, *, repetitions=None, deadline=None):
    import numpy as np
    repetitions = SPEC['bootstrap_repetitions'] if repetitions is None else repetitions
    y, model, base = map(np.asarray, (labels, model_probabilities, baseline_probabilities))
    rng = np.random.default_rng(SPEC['bootstrap_seed'])
    differences = []
    for _ in range(repetitions):
        check_deadline(deadline)
        indices, _ = block_indices(cases, axis, length, rng)
        if not len(indices): continue
        m = prediction_metrics(y[indices], model[indices]); b = prediction_metrics(y[indices], base[indices])
        differences.append([m['brier']-b['brier'], m['log_loss']-b['log_loss']])
    success = len(differences) / repetitions if repetitions else 0
    okay = success >= SPEC['bootstrap_min_success_fraction']
    values = np.quantile(differences, SPEC['interval'], axis=0).T.tolist() if okay else None
    return {'status': 'available' if okay else 'bootstrap_unstable', 'block_length': length,
            'method': 'paired_moving_trading_date_blocks', 'repetitions': repetitions,
            'seed': SPEC['bootstrap_seed'], 'success_fraction': success,
            'brier_difference_interval': values[0] if values else None,
            'log_loss_difference_interval': values[1] if values else None,
            'meaning': 'model_minus_baseline; negative_is_lower_loss; not_trading_gain'}
