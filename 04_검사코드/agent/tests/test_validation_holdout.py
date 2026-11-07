"""Reservation blocks discovery before features and requires positive enrollment."""
from datetime import timedelta
import pytest
from test_ontology import C, isolated
from test_validation_data import proposal, spec
import validation_spec as V
import validation_store as S
import validation_holdout as H
import judgment_coverage as JC
import case_prediction as CP
import case_dataset as D
import ai_judgment as J


def reserved(monkeypatch):
    pid=proposal(monkeypatch); now=C.now(); trial=spec('prospective')
    trial['period']={'start':(now+timedelta(days=1)).isoformat(),'end':(now+timedelta(days=3)).isoformat()}
    row=V.register_validation(pid,'future',trial)
    assert row['status']=='registered',row
    return row,now


def test_reservation_precedes_outcome_and_missing_logs_are_not_independent(monkeypatch):
    row,now=reserved(monkeypatch)
    assert H.status(row['validation_id'])['independent'] is False
    origin=now+timedelta(days=1,hours=1);monkeypatch.setattr(C,'now',lambda:origin)
    b={'candidates':[{'code':'000001'}]};a={'holdings':[]}
    attempt=JC.start_attempt('r','decide',b,a)
    result=H.enroll_attempt(attempt,b,a)
    assert result['recording_status']=='recorded',result
    assert result['enrollments'][0]['status']=='enrolled_before_outcome'
    assert H.status(row['validation_id'])['independent'] is True
    excluded=H.discovery_exclusions([{'application_id':'later','code':'000001','created_at':origin.isoformat()}])
    assert excluded[0]['application_id']=='later'
    assert H.filter_cases([{'application_id':'later','code':'000001','origin_at':origin.isoformat(),'label':1}])[0]==[]
    assert H.enroll_attempt(attempt,b,a)==result


def test_late_enrollment_does_not_acquire_independence(monkeypatch):
    row,now=reserved(monkeypatch);origin=now+timedelta(days=1)
    monkeypatch.setattr(C,'now',lambda:origin)
    attempt=JC.start_attempt('r','decide',{'candidates':[{'code':'000001'}]},{'holdings':[]})
    monkeypatch.setattr(C,'now',lambda:origin+timedelta(minutes=2))
    assert H.enroll_attempt(attempt)['enrollments'][0]['status']=='late_enrollment'
    assert not H.status(row['validation_id'])['independent']


def test_build_cases_filters_before_context_and_outcome_projection(monkeypatch):
    row,now=reserved(monkeypatch);origin=now+timedelta(days=1)
    monkeypatch.setattr(C,'now',lambda:origin+timedelta(hours=1))
    app={'application_id':'held','code':'000001','created_at':origin.isoformat(),'judgment_date':origin.date().isoformat(),'judgment_id':'j'}
    monkeypatch.setattr(J,'committed_premises',lambda *a:{'applications':[{'application':app,'judgment_ids':['j']}],
        'unavailable_applications':[],'errors':[],'target_set_incomplete':False})
    monkeypatch.setattr(D,'extract_case_context',lambda *a:(_ for _ in ()).throw(AssertionError('reserved features exposed')))
    result=D.build_cases(origin.date().isoformat(),C.now().isoformat())
    assert result['cases']==[] and result['withheld_count']==1 and result['expected_count']==0
    assert result['errors']==[]


def test_prediction_does_not_extract_reserved_features(monkeypatch):
    row,now=reserved(monkeypatch);origin=now+timedelta(days=1)
    monkeypatch.setattr(C,'now',lambda:origin)
    monkeypatch.setattr(D,'extract_case_context',lambda *a:(_ for _ in ()).throw(AssertionError('reserved prediction features exposed')))
    judgment={'judgment_id':'j','code':'000001','confidence_for':'매수','created_at':origin.isoformat(),
              'premise_applications':[{'judgment_id':'j','application_id':'held','code':'000001'}]}
    result=CP.record_new_premises(judgment)
    assert result['items'][0]['status']=='validation_reserved'


def test_release_requires_committed_final_result_and_does_not_mutate_reservation(monkeypatch):
    row,now=reserved(monkeypatch);vid=row['validation_id']
    with pytest.raises(ValueError):H.release(vid,'r','release')
    result={'validation_id':vid,'result_id':'r','status':'insufficient_data'}
    S.archive('results','r',result)
    with pytest.raises(ValueError):H.release(vid,'r','release')
    S.archive('result_commits','r',{'result_hash':S.digest(result)})
    before=S.load('holdout_reservations',vid)
    released=H.release(vid,'r','release')
    assert released['independent_for_new_trials'] is False
    assert H.release(vid,'r','release')==released
    assert S.load('holdout_reservations',vid)==before and H.reservations()==[]


def test_corrupt_reservation_fails_closed_for_discovery(monkeypatch):
    row,now=reserved(monkeypatch)
    S._path('holdout_reservations',row['validation_id']).write_text('{')
    with pytest.raises(ValueError,match='HOLDOUT_HISTORY_UNREADABLE'):H.filter_cases([])


def test_known_discovery_exposure_disqualifies_positive_enrollment(monkeypatch):
    import case_store as CS
    import research_evidence as RE
    import json
    from pathlib import Path
    row,now=reserved(monkeypatch);origin=now+timedelta(days=1)
    monkeypatch.setattr(C,'now',lambda:origin)
    attempt=JC.start_attempt('r','decide',{'candidates':[{'code':'000001'}]},{'holdings':[]})
    H.enroll_attempt(attempt)
    path=Path(C.WORK)/'ontology/premises/a.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'application_id':'a','code':'000001','created_at':origin.isoformat()}))
    with pytest.raises(ValueError,match='RESERVED_HOLDOUT_DISCOVERY_FORBIDDEN'):
        RE.reserve_usage('owner',['a'])
    # A preexisting/bypassed exposure archive remains visible and disqualifies;
    # it cannot be erased by the absence of a normal usage event.
    CS.archive('datasets','exposed',{'review_id':'exposed','cases':[{'application_id':'a'}]})
    proof=H.status(row['validation_id'])
    assert proof['independent'] is False and proof['exposed_application_ids']==['a']
