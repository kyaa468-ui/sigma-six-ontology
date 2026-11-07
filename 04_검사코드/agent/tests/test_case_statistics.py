"""Local synthetic association/prediction mathematics; market access is forbidden."""
import copy
import socket
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import common as C
import case_statistics as ST

NOW = datetime.fromisoformat('2026-11-07T10:00:00+09:00')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'WORK', str(tmp_path))
    monkeypatch.setattr(C, 'now', lambda: NOW)
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('Network forbidden'))


def synthetic(n_dates=80, per_day=6, seed=52):
    rng = np.random.default_rng(seed)
    current = date(2026, 1, 5)
    axis = []
    while len(axis) < n_dates + 30:
        if current.weekday() < 5: axis.append(current.isoformat())
        current += timedelta(days=1)
    calendar = {'calendar_id': 'synthetic-calendar', 'hash': 'fixture', 'status': 'complete',
                'available_at': '2026-01-01T00:00:00+09:00',
                'sessions': [{'date': d, 'open_at': d+'T09:00:00+09:00', 'close_at': d+'T15:30:00+09:00'} for d in axis]}
    raw = rng.normal(size=(n_dates*per_day, 4))
    logits = .7*raw[:, 0] - .65*raw[:, 3] + .2*raw[:, 1] + .15*raw[:, 0]*raw[:, 1]
    probability = 1/(1+np.exp(-logits))
    labels = (rng.random(len(raw)) < probability).astype(int)
    cases = []
    for i, (x, label) in enumerate(zip(raw, labels)):
        d = axis[i//per_day]
        cases.append({'application_id': f'premise-{i:05}', 'code': f'{i % 12:06}', 'judgment_date': d,
                      'origin_at': d+'T10:00:00+09:00', 'origin_judgment_id': f'judgment-{i}',
                      'case_context_version': 'CASE_CONTEXT@v1',
                      'stratum': {'definition_id': 'MA20_MAINTENANCE@v2', 'days': 1, 'direction': 'above',
                                  'measurement': 'synthetic', 'instrument_kind': 'equity'},
                      'features': {'gap': float(x[0]/100), 'slope': float(x[1]/100),
                                   'foreign_ratio': float(x[2]), 'institution_ratio': float(x[3]),
                                   'regime': ['상승','횡보','하락'][i % 3]},
                      'outcome_eligible': True, 'model_eligible': True, 'label': int(label),
                      'label_available_at': axis[i//per_day+2]+'T08:30:00+09:00',
                      'evaluation_record_available_at': axis[i//per_day+2]+'T08:31:00+09:00',
                      'evaluation_id': f'evaluation-{i}',
                      'window_snapshot': {'sessions': [axis[i//per_day+1]], 'status': 'resolved'}})
    return cases, calendar


def test_model_policy_is_fixed_and_sample_guards_do_not_impute():
    cases, calendar = synthetic(10, 6)
    result = ST.fit_groups(cases, calendar, NOW.isoformat(), uncertainty=False)
    assert not result['models'] and result['groups'][0]['status'] == 'insufficient_data'
    assert {'insufficient_cases','insufficient_each_label','insufficient_judgment_dates'} <= set(result['groups'][0]['reasons'])
    assert ST.SPEC['bootstrap_repetitions'] == 1000
    case = copy.deepcopy(cases[0]); case['features']['institution_ratio'] = None
    selected, exclusions, _ = ST.eligible_cases([case], NOW.isoformat())
    assert not selected and exclusions[0]['reason'] == 'missing_feature:institution_ratio'


def test_future_labels_future_evaluations_and_duplicate_cases_are_excluded():
    cases, _ = synthetic()
    bad = copy.deepcopy(cases[0]); bad['label_available_at'] = '2027-01-01T00:00:00+09:00'
    later = copy.deepcopy(cases[1]); later['evaluation_record_available_at'] = '2027-01-01T00:00:00+09:00'
    conflicting = copy.deepcopy(cases[2]); conflicting['label'] = 1-conflicting['label']
    rows, excluded, errors = ST.eligible_cases([bad, later, cases[2], conflicting, cases[3], cases[3]], NOW.isoformat())
    assert [r['application_id'] for r in rows] == [cases[3]['application_id']]
    assert {'future_label','future_evaluation_record','conflicting_duplicate_application'} <= {e['reason'] for e in excluded}
    assert errors


def test_fit_predict_uses_only_frozen_training_transform():
    cases, calendar = synthetic()
    result = ST.fit_groups(cases, calendar, NOW.isoformat(), uncertainty=False)
    assert result['status'] == 'completed' and len(result['models']) == 1, result['groups']
    model = result['models'][0]
    assert model['coefficients'][1] > 0 and model['coefficients'][4] < 0
    before = copy.deepcopy(model['transform'])
    future = copy.deepcopy(cases[0]); future['features']['gap'] = 10
    p, baseline = ST.predict(model, future)
    assert 0 <= p <= 1 and baseline == np.mean([c['label'] for c in cases])
    assert model['transform'] == before
    future['features']['regime'] = '미확인'
    with pytest.raises(ValueError, match='unknown_regime'): ST.predict(model, future)


@pytest.mark.parametrize('matrix,labels,reason', [
    (np.array([[1,-2],[1,-1],[1,1],[1,2.]]), np.array([0,0,1,1]), 'complete_or_quasi_separation'),
    (np.array([[1,-1],[1,0],[1,0],[1,1.]]), np.array([0,0,1,1]), 'complete_or_quasi_separation'),
    (np.array([[1,1],[1,1],[1,1],[1,1.]]), np.array([0,1,0,1]), 'rank_deficient'),
    (np.array([[1,-2],[1,-1],[1,1],[1,2.]]), np.array([1,1,1,1]), 'single_label'),
])
def test_identification_failures_are_explicit(matrix, labels, reason):
    with pytest.raises(ValueError, match=reason): ST._fit_design(matrix, labels)


def test_nonconvergence_is_not_treated_as_fit_success(monkeypatch):
    import statsmodels.api as sm
    cases, _ = synthetic()
    matrix, _, _ = ST._design(cases)
    monkeypatch.setattr(sm, 'Logit', lambda *a, **k: SimpleNamespace(
        fit=lambda **kw: SimpleNamespace(mle_retvals={'converged':False})))
    with pytest.raises(ValueError, match='not_converged'):
        ST._fit_design(matrix, np.array([c['label'] for c in cases]))


def test_date_blocks_keep_all_instruments_and_repeated_cases_together():
    cases, calendar = synthetic(20, 6)
    axis = ST.calendar_axis(cases, calendar, NOW.isoformat())
    idx, dates = ST.block_indices(cases, axis, 5, np.random.default_rng(7))
    assert len(idx) == len(dates)*6
    for start in range(0, len(dates), 5):
        positions = [axis.index(d) for d in dates[start:start+5]]
        assert positions == list(range(positions[0], positions[0]+len(positions)))
    for d in set(dates):
        assert sum(cases[i]['judgment_date'] == d for i in idx) == dates.count(d)*6


def test_metrics_calibration_and_paired_loss_have_known_values():
    values = ST.prediction_metrics([0,1], [.25,.75])
    assert values['brier'] == .0625 and values['roc_auc'] == 1
    assert values['log_loss'] == pytest.approx(-np.log(.75))
    assert sum(b['count'] for b in values['calibration']) == 2
    assert ST.prediction_metrics([0,1], [.5,.5])['roc_auc'] == .5
    assert ST.prediction_metrics([1,1], [1,.5])['roc_auc'] is None
    with pytest.raises(ValueError): ST.prediction_metrics([1], [float('nan')])
    with pytest.raises(ValueError): ST.prediction_metrics([.5], [.5])


def test_bootstrap_success_guard_suppresses_unstable_intervals(monkeypatch):
    cases, calendar = synthetic(10, 6)
    matrix, _, _ = ST._design(cases)
    labels = np.asarray([c['label'] for c in cases])
    def failure(*args, **kwargs):
        raise ValueError('synthetic_separation')
    monkeypatch.setattr(ST, '_fit_design', failure)
    result = ST.bootstrap_intervals(cases, matrix, labels, ST.calendar_axis(cases, calendar), 5, repetitions=20)
    assert result['status']=='bootstrap_unstable'
    assert result['coefficient_intervals'] is None and result['success_fraction']==0


def test_deadline_never_returns_a_partial_model_for_publication():
    cases, calendar = synthetic()
    result = ST.fit_groups(cases, calendar, NOW.isoformat(), deadline=0)
    assert result['status'] == 'timeout' and result['models'] == []


def test_production_1000_draw_policy_on_synthetic_known_association():
    cases, calendar = synthetic()
    result = ST.fit_groups(cases, calendar, NOW.isoformat())
    assert len(result['models']) == 1, result['groups']
    model = result['models'][0]
    for key in ('primary','sensitivity_2L'):
        interval = model['uncertainty'][key]
        assert interval['repetitions'] == 1000
        assert interval['status'] == 'available' and interval['success_fraction'] >= .95
        assert len(interval['coefficient_intervals']) == len(model['coefficients'])
    assert model['uncertainty']['primary']['coefficient_intervals'][1][0] > 0
    assert model['uncertainty']['primary']['coefficient_intervals'][4][1] < 0
