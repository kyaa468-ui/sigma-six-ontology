"""Frozen semantics and disclosure accounting, using only local fixture records."""
import copy
from pathlib import Path

import pytest

from test_ontology import C, isolated
import case_review as CR
import case_store as S
import research_evidence as RE


def report(review_id='review-a', column='z_institution_ratio', coefficient=-.3):
    result = {'review_id':review_id,
              'comparison':{'summary':{'refutation_rate':.25,'complete_count':4,'refuted_count':1}},
              'statistics':{'models':[{'model_id':'model-a','columns':['intercept',column],
                            'coefficients':[.1,coefficient],'stratum':{'days':3},
                            'transform':{'features':['gap','slope','foreign_ratio','institution_ratio']}}]}}
    result['metric_catalog'] = CR._metric_catalog({k:v for k,v in result.items() if k != 'review_id'})
    dataset = {'review_id':review_id,'cases':[{'application_id':'p1','features':{'gap':.02},
                 'signed_flows':{'institution':-4_000_000_000,'foreign':None,
                                 'interpretation':'received_values_not_simultaneous'},
                 'stratum':{'days':3},'outcome_eligible':True,'label':1,'verdict':'refuted'}]}
    return result,dataset


def coefficient(catalog):
    return next(e for e in catalog if e.get('metric_kind')=='model_coefficient' and e['feature_name']!='intercept')


def test_compound_reference_disambiguates_same_path_and_variable():
    a,b = report(),report('review-b','z_gap',.3)
    first,second = RE.build_catalog('review-a',*a),RE.build_catalog('review-b',*b)
    x,y = coefficient(first),coefficient(second)
    assert x['metric_id'] == y['metric_id'] and x['reference'] != y['reference']
    claim = {'id':'f1','reference':x['reference'],'op':'direction','direction':'negative','feature_name':'z_institution_ratio'}
    assert RE.validate_claim(claim,first)['fact_validation']=='passed'
    with pytest.raises(ValueError,match='NOT_DELIVERED'): RE.validate_claim(claim,second)
    claim['reference']=y['reference']; claim['direction']='positive'
    with pytest.raises(ValueError,match='FEATURE_MISMATCH'): RE.validate_claim(claim,second)
    claim['feature_name']='z_gap'
    assert RE.validate_claim(claim,second)['fact_validation']=='passed'


def test_rate_denominator_units_and_unknown_zero():
    result,dataset=report(); catalog=RE.build_catalog('review-a',result,dataset)
    rate=next(x for x in catalog if x['metric_kind']=='refutation_rate')
    assert rate['denominator']['value']==4 and rate['unit']=='ratio'
    good={'id':'rate','reference':rate['reference'],'op':'value','value':25,'unit':'%'}
    assert RE.validate_claim(good,catalog)['fact_validation']=='passed'
    with pytest.raises(ValueError,match='FACT_MISMATCH'): RE.validate_claim({**good,'value':75},catalog)
    cash=next(x for x in catalog if x.get('feature_name')=='signed_flows.institution')
    fact={'id':'inst','reference':cash['reference'],'feature_name':cash['feature_name'], 'value':-40,'unit':'억 원'}
    assert RE.validate_claim(fact,catalog)['fact_validation']=='passed'
    with pytest.raises(ValueError,match='FACT_MISMATCH'): RE.validate_claim({**fact,'value':40},catalog)
    absent=next(x for x in catalog if x.get('feature_name')=='signed_flows.foreign')
    with pytest.raises(ValueError,match='UNKNOWN_AS_ZERO'):
        RE.validate_claim({'id':'zero','reference':absent['reference'],'feature_name':absent['feature_name'],'value':0},catalog)


def test_free_prose_unseen_page_and_wrong_frozen_values_rejected():
    result,dataset=report(); catalog=RE.build_catalog('review-a',result,dataset)
    case=next(x for x in catalog if x.get('application_id'))
    summary=[x for x in catalog if not x.get('application_id')]
    claim={'id':'f','reference':case['reference'],'value':case['value']}
    with pytest.raises(ValueError,match='NOT_DELIVERED'): RE.validate_claim(claim,summary)
    with pytest.raises(ValueError,match='SCHEMA_ERROR'): RE.validate_claim({**claim,'text':'기관 순매수'},catalog)
    result['metric_catalog'][0]['value']='changed'
    with pytest.raises(ValueError,match='METRIC_VALUE_MISMATCH'): RE.build_catalog('review-a',result,dataset)


def test_sign_never_uses_display_rounding():
    result,dataset=report(coefficient=-1e-9)
    source=coefficient(RE.build_catalog('review-a',result,dataset))
    assert source['display_value']==0
    claim={'id':'f','reference':source['reference'],'feature_name':source['feature_name'],'op':'direction','direction':'negative'}
    assert RE.validate_claim(claim,[source])['fact_validation']=='passed'
    with pytest.raises(ValueError,match='FACT_MISMATCH'): RE.validate_claim({**claim,'direction':'zero'},[source])


def test_discovery_reservation_idempotent_and_absence_never_means_unused():
    first=RE.reserve_usage('proposal-run',['p1'],{'result_hash':'hash'},'proposal_context')
    second=RE.reserve_usage('proposal-run',['p1'],{'result_hash':'hash'},'proposal_context')
    assert first==second
    assert len(S.records('usage_events')['records'])==1
    view=RE.usage_status(['p1','p2'])
    assert [x['status'] for x in view['items']]==['used','unknown']
    assert not any(x['independent_unused'] for x in view['items'])
    before={str(p):p.read_bytes() for p in S.root().rglob('*') if p.is_file()}
    RE.usage_status(['p1','p2'])
    assert before=={str(p):p.read_bytes() for p in S.root().rglob('*') if p.is_file()}


def test_legacy_dataset_without_usage_is_still_not_fresh_holdout():
    S.archive('datasets','old-review',{'review_id':'old-review','cases':[{'application_id':'p1'}]})
    result=RE.usage_status(['p1'])
    assert result['items'][0]['status']=='used'
    assert not result['items'][0]['independent_unused']
    folder=S.root()/'usage_events';folder.mkdir(exist_ok=True)
    (folder/'broken.json').write_text('{bad')
    result=RE.usage_status(['p2'])
    assert result['recording_status']=='recording_error'
    assert result['items'][0]['status']=='unknown'


def test_reservation_failure_prevents_dataset_exposure(monkeypatch):
    from test_close_evaluation import at
    at(monkeypatch,'2026-11-14T10:00:00+09:00')
    def fail(*a,**k): raise OSError('usage storage unavailable')
    monkeypatch.setattr(RE,'reserve_usage',fail)
    result=CR.build_review()
    assert result['run_status']=='partial'
    assert not S.records('datasets')['records']
    assert not S.records('publication_commits')['records']


def test_coefficient_interval_feature_response_and_units_are_not_interchangeable():
    result,dataset=report()
    result['statistics']['models'][0]['uncertainty']={'primary':{'coefficient_intervals':[[-.2,.2],[-.6,-.1]]}}
    result['metric_catalog']=CR._metric_catalog({k:v for k,v in result.items() if k not in ('review_id','metric_catalog')})
    catalog=RE.build_catalog('review-a',result,dataset)
    source=next(x for x in catalog if x['metric_kind']=='coefficient_interval_bound' and x['feature_name']=='z_institution_ratio')
    assert source['interval_bound']=='lower' and source['outcome']=='premise_refuted'
    claim={'id':'ci','reference':source['reference'],'feature_name':source['feature_name'],
           'op':'value','value':source['value'],'unit':source['unit'],'outcome':'premise_refuted'}
    assert RE.validate_claim(claim,catalog)['fact_validation']=='passed'
    with pytest.raises(ValueError,match='MEANING_MISMATCH'): RE.validate_claim({**claim,'outcome':'trading_profit'},catalog)
    with pytest.raises(ValueError,match='MEANING_MISMATCH'): RE.validate_claim({**claim,'interval_bound':'upper'},catalog)
    with pytest.raises(ValueError,match='UNIT_MISMATCH'): RE.validate_claim({**claim,'unit':'ratio'},catalog)


def test_unknown_schema_and_nonfinite_claims_cannot_be_verified():
    result,dataset=report();catalog=RE.build_catalog('review-a',result,dataset)
    source=coefficient(catalog)
    claim={'id':'coefficient','reference':source['reference'],'feature_name':source['feature_name'],'value':float('nan')}
    with pytest.raises(ValueError,match='NONFINITE_NUMBER'): RE.validate_claim(claim,catalog)
    with pytest.raises(ValueError,match='COMPOUND_REFERENCE_REQUIRED'):
        RE.validate_claim({**claim,'reference':{'metric_id':source['metric_id']}},catalog)


@pytest.mark.parametrize('extra',[{'value':40},{'unit':'억원'},{'operator':'gt','result':True}])
def test_direction_cannot_hide_other_claims(extra):
    result,dataset=report();source=coefficient(RE.build_catalog('review-a',result,dataset))
    claim={'id':'coefficient','reference':source['reference'],'feature_name':source['feature_name'],
           'op':'direction','direction':'negative',**extra}
    with pytest.raises(ValueError,match='SCHEMA_ERROR'): RE.validate_claim(claim,[source])


def test_value_cannot_hide_wrong_direction_or_unit():
    result,dataset=report();catalog=RE.build_catalog('review-a',result,dataset)
    source=coefficient(catalog)
    claim={'id':'coefficient','reference':source['reference'],'feature_name':source['feature_name'],'value':-.3}
    with pytest.raises(ValueError,match='SCHEMA_ERROR'):
        RE.validate_claim({**claim,'direction':'positive'},catalog)
    for source in [x for x in catalog if x['value'] is None or isinstance(x['value'],(str,bool))]:
        claim={'id':'f','reference':source['reference'],'value':source['value'],'unit':'nonsense'}
        if source['feature_name'] is not None: claim['feature_name']=source['feature_name']
        with pytest.raises(ValueError,match='UNIT_MISMATCH'): RE.validate_claim(claim,catalog)
    with pytest.raises(ValueError,match='SCHEMA_ERROR'):
        RE.validate_claim({'id':'f','reference':catalog[0]['reference']},catalog)


def test_valid_envelope_with_unknown_usage_schema_is_not_unused():
    S.archive('usage_events','unknown',{'application_ids':['p1'],'kind':'fresh','version':'unknown'})
    S.archive('datasets','bad-shape',{'review_id':'bad-shape','cases':[None]})
    result=RE.usage_status(['p1'])
    assert result['recording_status']=='recording_error'
    assert result['items'][0]['status']=='unknown' and not result['items'][0]['independent_unused']
    assert all(e['reason']=='usage_history_schema_unknown' for e in result['errors'])
