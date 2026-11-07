"""Synthetic first-acceptance forecasts and chronological replay, without services."""
import copy
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import common as C
import case_dataset as D
import case_prediction as P
import case_statistics as ST
import case_store as S
from test_case_statistics import synthetic, NOW

ORIGIN = datetime.fromisoformat('2026-11-09T10:00:00+09:00')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'WORK', str(tmp_path))
    monkeypatch.setattr(C, 'now', lambda: ORIGIN)
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('Network forbidden'))


@pytest.fixture(scope='module')
def fitted_model():
    cases, calendar = synthetic()
    return ST.fit_groups(cases, calendar, NOW.isoformat(), uncertainty=False)['models'][0]


def fresh_case():
    case = copy.deepcopy(synthetic(1, 1)[0][0])
    case.update(application_id='fresh-app', origin_judgment_id='fresh-judgment',
                origin_at=ORIGIN.isoformat(), judgment_date=ORIGIN.date().isoformat(),
                feature_eligible=True, outcome_eligible=False, model_eligible=False, label=None,
                comparison_context={'delivery_id':'delivery-1','observation_id':'observation-1'})
    return case


def judgment(case):
    return {'judgment_id': case['origin_judgment_id'], 'created_at': case['origin_at'],
            'premise_applications': [{'application_id':case['application_id'], 'code':case['code'],
                                     'judgment_id':case['origin_judgment_id']}]}


def publish(model, *, commit=True, publication_at=None, commit_at=None):
    publication = {'publication_id':'publication-1','review_id':'review-1','model_id':model['model_id'],
                   'available_at':publication_at or NOW.isoformat(), 'valid_from':'2026-11-09T00:00:00+09:00',
                   'valid_until':'2026-11-16T00:00:00+09:00','status':'published'}
    S.archive('models', model['model_id'], model)
    S.archive('model_publications','publication-1', publication)
    if commit:
        S.archive('publication_commits','review-1',{'review_id':'review-1','publication_ids':['publication-1'],
                                                'published_at':commit_at or NOW.isoformat()})
    return publication


def mock_context(monkeypatch, case):
    monkeypatch.setattr(D, 'extract_case_context', lambda *_: copy.deepcopy(case))


def forecasts_for(cases, *, kind='prospective'):
    return [{'kind':kind,'prediction_id':'forecast-'+c['application_id'],'application_id':c['application_id'],
             'origin_at':c['origin_at'],'code':c['code'],'created_at':c['origin_at'],
             'stratum_id':ST.stratum_key(c),'stratum':copy.deepcopy(c['stratum']),
             'feature_hash':ST.digest(c['features']), 'training_cutoff':'2025-12-31T10:00:00+09:00',
             'status':'predicted','probability':.15 if c['label']==0 else .85,
             'baseline_probability':.5,'parameter_count':8} for c in cases]


def test_first_acceptance_predicts_once_and_does_not_return_probability(monkeypatch, fitted_model):
    case = fresh_case(); mock_context(monkeypatch, case); publish(fitted_model)
    row = judgment(case)
    result = P.record_new_premises(row)
    assert result['items'][0]['status'] == 'predicted'
    assert 'probability' not in str(result)
    saved = S.records('predictions')['records'][0]
    assert saved['probability'] == pytest.approx(ST.predict(fitted_model,case)[0])
    assert saved['publication_id']=='publication-1' and saved['parameter_count']==8
    before = S.digest(saved)
    monkeypatch.setattr(C, 'now', lambda: ORIGIN+timedelta(days=10))
    assert P.record_new_premises(row)==result
    assert len(S.records('predictions')['records'])==1
    assert S.digest(S.records('predictions')['records'][0])==before
    reuse = copy.deepcopy(row); reuse['judgment_id']='later-judgment'
    assert P.record_new_premises(reuse)['items']==[]


@pytest.mark.parametrize('kwargs',[
    {'commit':False},
    {'publication_at':'2026-11-09T10:01:00+09:00'},
    {'commit_at':'2026-11-09T10:01:00+09:00'},
])
def test_uncommitted_or_future_publications_are_never_used(monkeypatch,fitted_model,kwargs):
    case=fresh_case(); mock_context(monkeypatch,case); publish(fitted_model,**kwargs)
    result=P.record_new_premises(judgment(case))
    assert result['items'][0]['status']=='no_model'
    assert 'probability' not in S.records('predictions')['records'][0]


def test_published_model_only_applies_to_the_explicit_next_week(fitted_model):
    publish(fitted_model); case=fresh_case()
    assert P._choose_model(case,'2026-11-08T23:59:59+09:00')==(None,None)
    assert P._choose_model(case,'2026-11-09T00:00:00+09:00')[1]['model_id']==fitted_model['model_id']
    assert P._choose_model(case,'2026-11-16T00:00:00+09:00')==(None,None)


@pytest.mark.parametrize('alteration', ['missing_number','ambiguous_context','stratum_not_trained'])
def test_missing_or_incomparable_context_is_not_imputed(monkeypatch,fitted_model,alteration):
    case=fresh_case(); publish(fitted_model)
    if alteration=='missing_number': case['features']['institution_ratio']=None
    if alteration=='ambiguous_context': case['feature_eligible']=False
    if alteration=='stratum_not_trained': case['stratum']['measurement']='other-measurement'
    mock_context(monkeypatch,case)
    result=P.record_new_premises(judgment(case))
    assert result['items'][0]['status']==('no_model' if alteration=='stratum_not_trained' else 'features_unavailable')
    assert 'probability' not in S.records('predictions')['records'][0]


def test_delayed_callback_is_not_a_backfill(monkeypatch,fitted_model):
    case=fresh_case(); mock_context(monkeypatch,case); publish(fitted_model)
    monkeypatch.setattr(C,'now',lambda:ORIGIN+timedelta(minutes=2))
    result=P.record_new_premises(judgment(case))
    assert result['items'][0]['status']=='late_callback_not_predicted'
    assert 'probability' not in S.records('predictions')['records'][0]


def test_prediction_save_failure_preserves_target_and_cannot_retry_after_outcome(monkeypatch,fitted_model):
    case=fresh_case(); mock_context(monkeypatch,case); publish(fitted_model)
    original=S.archive
    def fail(kind,*args):
        if kind=='predictions': raise OSError('synthetic disk full')
        return original(kind,*args)
    monkeypatch.setattr(S,'archive',fail)
    result=P.record_new_premises(judgment(case))
    assert result['recording_status']=='recording_error'
    assert len(S.records('prediction_targets')['records'])==1
    monkeypatch.setattr(S,'archive',original)
    monkeypatch.setattr(C,'now',lambda:ORIGIN+timedelta(days=3))
    assert P.record_new_premises(judgment(case))['items'][0]['status']=='unfinished'
    assert S.records('predictions')['records']==[]
    perf=P.prospective_performance([case],{},C.now().isoformat())
    assert perf['counts']=={'recording_error':1} and perf['coverage_rate']==0


def test_started_but_unfinished_prediction_is_not_silently_zero_targets(monkeypatch):
    case=fresh_case(); row=judgment(case)
    prediction_id='prediction-'+S.digest([P.PREDICTION_VERSION,case['application_id']])
    S.archive('prediction_targets',prediction_id,{'prediction_id':prediction_id,'application_id':case['application_id'],
                                               'started_at':ORIGIN.isoformat()})
    assert P.record_new_premises(row)['items'][0]['status']=='unfinished'
    perf=P.prospective_performance([case],{},ORIGIN.isoformat())
    assert perf['expected_count']==1 and perf['counts']=={'unfinished':1}


def test_missing_forecasts_legacy_cases_and_outcomes_stay_distinct():
    cases,calendar=synthetic(2,3)
    legacy=copy.deepcopy(cases[-1]); legacy.pop('case_context_version')
    pending=copy.deepcopy(cases[0]); pending['outcome_eligible']=False
    predictions=forecasts_for([pending,cases[1]])
    predictions[1]['status']='no_model'; predictions[1].pop('probability')
    result=P.evaluate_predictions([pending,cases[1],cases[2],legacy],predictions,calendar,NOW.isoformat())
    assert result['expected_count']==3
    assert result['counts']=={'outcome_unavailable':1,'no_model':1,'not_recorded':1}
    assert result['historical_not_expected']==[legacy['application_id']]
    assert result['coverage_rate']==pytest.approx(1/3)
    assert result['groups']==[]


@pytest.mark.parametrize('corruption', ['future_training','future_evaluation','changed_feature','late_prediction'])
def test_future_information_and_revised_original_features_are_rejected(corruption):
    cases,calendar=synthetic(1,1); case=cases[0]; prediction=forecasts_for(cases)[0]
    if corruption=='future_training': prediction['training_cutoff']=case['origin_at']
    if corruption=='future_evaluation': case['evaluation_record_available_at']='2027-01-01T00:00:00+09:00'
    if corruption=='changed_feature': case['features']['gap']+=.01
    if corruption=='late_prediction': prediction['created_at']=NOW.isoformat()
    result=P.evaluate_predictions(cases,[prediction],calendar,NOW.isoformat())
    assert result['counts']=={'invalid_prediction_or_outcome':1} and not result['groups']


def test_metrics_are_hidden_until_sufficient_test_cases_and_keep_kinds_separate():
    cases,calendar=synthetic(10,6)
    forecasts=forecasts_for(cases)
    forecasts+=forecasts_for(cases,kind='retrospective')
    result=P.evaluate_predictions(cases,forecasts,calendar,NOW.isoformat())
    assert result['expected_count']==60 and result['counts']=={'scorable':60}
    assert result['groups'][0]['status']=='insufficient_data'
    assert result['groups'][0]['model_metrics'] is None
    assert result['groups'][0]['sample_guard']['actual']['parameters']==8


def test_paired_performance_and_block_intervals_are_repeatable_and_read_only():
    cases,calendar=synthetic()
    forecasts=forecasts_for(cases)
    for row in forecasts: S.archive('predictions',row['prediction_id'],row)
    before={str(p):p.read_bytes() for p in S.root().rglob('*.json')}
    a=P.prospective_performance(cases,calendar,NOW.isoformat())
    b=P.prospective_performance(cases,calendar,NOW.isoformat())
    assert a==b and a['expected_count']==480
    group=a['groups'][0]
    assert group['status']=='available'
    assert group['model_metrics']['brier']==pytest.approx(.15**2)
    assert group['baseline_metrics']['brier']==pytest.approx(.25)
    assert group['model_metrics']['roc_auc']==1
    for interval in group['loss_difference_intervals'].values():
        assert interval['repetitions']==1000 and interval['brier_difference_interval'][1]<0
    assert before=={str(p):p.read_bytes() for p in S.root().rglob('*.json')}
    corrected=copy.deepcopy(cases); corrected[0]['label']=1-corrected[0]['label']
    changed=P.prospective_performance(corrected,calendar,NOW.isoformat())
    assert changed['groups'][0]['model_metrics']['brier']>group['model_metrics']['brier']
    assert before=={str(p):p.read_bytes() for p in S.root().rglob('*.json')}


def test_conflicting_duplicates_never_select_a_favorable_outcome():
    cases,calendar=synthetic(1,2); forecasts=forecasts_for(cases)
    conflict=copy.deepcopy(cases[0]); conflict['label']=1-conflict['label']
    result=P.evaluate_predictions(cases+[conflict],forecasts,calendar,NOW.isoformat())
    assert result['counts']['conflicting_case_versions']==1 and result['coverage_rate'] is None
    bad=copy.deepcopy(forecasts[0]); bad['probability']=1-bad['probability']
    result=P.evaluate_predictions(cases,forecasts+[bad],calendar,NOW.isoformat())
    assert result['counts']['conflicting_predictions']==1


def test_replay_uses_fold_availability_and_purges_windows_without_new_training_scales(monkeypatch):
    cases,calendar=synthetic(20,2)
    for c in cases: c['feature_eligible']=True
    observed=[]
    def project(case,market,knowledge):
        projected=copy.deepcopy(case)
        cutoff=ST.instant(knowledge)
        # Mimics the actual dataset selector: the correction was first recorded
        # on Jan 24; before then the initial label must remain in force.
        if case['application_id']=='premise-00000':
            projected['label']=int(cutoff>=datetime.fromisoformat('2026-01-24T10:00:00+09:00'))
        if ST.instant(projected['label_available_at'])>cutoff:
            projected['model_eligible']=False; projected['outcome_eligible']=False
        observed.append((case['application_id'],market,knowledge,projected['label']))
        return projected
    captures=[]
    def fit(rows,cal,knowledge,deadline,**kw):
        eligible,exclusions,errors=ST.eligible_cases(rows,knowledge)
        captures.append((knowledge,copy.deepcopy(eligible)))
        assert kw=={'uncertainty':False}
        return {'status':'completed','models':[],'groups':[],'errors':errors,'excluded':exclusions}
    monkeypatch.setattr(D,'project_case',project)
    monkeypatch.setattr(ST,'fit_groups',fit)
    result=P.retrospective(cases,calendar,'2026-02-07T10:00:00+09:00')
    assert result['status']=='completed' and result['folds']
    assert all(r['kind']=='retrospective' for r in result['predictions'])
    assert result['performance']['counts']=={'no_model':len(result['predictions'])}
    for cutoff,training in captures:
        for case in training:
            assert ST.instant(case['label_available_at'])<=ST.instant(cutoff)
            assert case['window_snapshot']['sessions'][-1]<(ST.instant(cutoff)+timedelta(days=2)).date().isoformat()
    first=[x for x in observed if x[0]=='premise-00000']
    assert first[0][-1]==0 and first[-1][-1]==1
    assert all(x[1]==(ST.instant(x[2]).date()-timedelta(days=1)).isoformat() for x in observed)
    assert all(f['historical_model_existence']=='not_claimed' for f in result['folds'])
    assert set(r['application_id'] for r in result['predictions'])==set(c['application_id'] for c in cases)


def test_actual_replay_fit_stores_training_only_means_and_available_labels(monkeypatch):
    import numpy as np
    cases,calendar=synthetic()
    for case in cases: case['feature_eligible']=True
    def asof(case,market,knowledge):
        row=copy.deepcopy(case)
        if ST.instant(row['evaluation_record_available_at'])>ST.instant(knowledge):
            row.update(model_eligible=False,outcome_eligible=False,label=None)
        return row
    monkeypatch.setattr(D,'project_case',asof)
    result=P.retrospective(cases,calendar,'2026-05-02T10:00:00+09:00')
    assert result['status']=='completed'
    models=[m for fold in result['folds'] for m in fold['models']]
    assert models and any(p['status']=='predicted' for p in result['predictions'])
    all_mean=np.mean([[c['features'][n] for n in ST.SPEC['features']] for c in cases],axis=0)
    for model in models:
        train=model['training_inputs']
        assert all(ST.instant(c['label_available_at'])<=ST.instant(model['training_cutoff']) for c in train)
        actual=np.mean([[c['features'][n] for n in ST.SPEC['features']] for c in train],axis=0)
        assert model['transform']['mean']==pytest.approx(actual)
        assert not np.allclose(actual,all_mean)


def test_replay_corrupt_fold_is_incomplete_not_publishable(monkeypatch):
    cases,calendar=synthetic(10,3)
    monkeypatch.setattr(D,'project_case',lambda c,*args:copy.deepcopy(c))
    monkeypatch.setattr(ST,'fit_groups',lambda *args,**kwargs:{
        'models':[],'groups':[],'status':'incomplete','errors':[{'reason':'synthetic_conflict'}]})
    result=P.retrospective(cases,calendar,'2026-02-07T10:00:00+09:00')
    assert result['status']=='incomplete'
    assert any(e.get('reason')=='synthetic_conflict' for e in result['errors'])


def test_replay_deadline_preserves_partial_state_and_never_claims_completed():
    cases,calendar=synthetic(20,2)
    result=P.retrospective(cases,calendar,NOW.isoformat(),deadline=0)
    assert result['status']=='timeout' and result['performance'] is None

