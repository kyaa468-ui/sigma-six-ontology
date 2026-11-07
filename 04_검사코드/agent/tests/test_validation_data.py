"""Local original-call denominator and immutable registration regressions."""
import copy
from datetime import timedelta
from pathlib import Path
import pytest
from test_ontology import C, M, isolated
from test_learning_proposals import source, item
from test_judgment_integration import setup_run, submission, watch
import learning_proposals as P
import validation_spec as V
import validation_store as S
import validation_dataset as D
import judgment_coverage as JC


def proposal(monkeypatch):
    run,context,page=source(monkeypatch)
    result=P.record_learning_proposals(run['proposal_run_id'],'validation-proposal',[item(context,page)])
    assert result['status']=='accepted',result
    return result['items'][0]['proposal_id']


def spec(mode='retrospective'):
    now=C.now()
    return {'mode':mode,'family_id':'family-one',
        'period':{'start':(now-timedelta(days=1)).isoformat(),'end':now.isoformat(),'knowledge_as_of':now.isoformat()},
        'scope':{'codes':['*'],'confidence_for':['매수','매도']}}


def register(monkeypatch):
    pid=proposal(monkeypatch)
    result=V.register_validation(pid,'trial-one',spec())
    assert result['status']=='registered',result
    return result


def test_spec_retries_are_immutable_and_missing_criteria_are_development_only(monkeypatch):
    row=register(monkeypatch); before=S.load('specs',row['validation_id'])
    again=V.register_validation(row['proposal_id'],'trial-one',spec())
    assert again['replayed'] and S.load('specs',row['validation_id'])==before
    assert row['criteria_complete'] is False and row['adoption_readiness']=='development_only'
    changed=spec();changed['minimums']={'sessions':1}
    assert V.register_validation(row['proposal_id'],'trial-one',changed)['errors'][0]['code']=='IDEMPOTENCY_CONFLICT'
    assert before['source_bundle']['all_cases'] and len(before['source_bundle']['all_cases'])==3


def test_historical_export_survives_broken_live_registry(monkeypatch):
    pid=proposal(monkeypatch)
    before=P.export_validation_bundle(pid)
    monkeypatch.setattr(P,'target_snapshot',lambda:(_ for _ in ()).throw(ValueError('broken current source')))
    after=P.export_validation_bundle(pid)
    assert after['proposal']==before['proposal'] and after['base_snapshot']==before['base_snapshot']
    assert after['base_status']=='unknown' and after['deployability']['errors']
    assert after['historical_readiness']==before['historical_readiness']
    assert V.register_validation(pid,'historic',spec())['status']=='registered'


def test_dataset_keeps_unsubmitted_rejected_and_watch_targets_without_fabricated_score(monkeypatch):
    _,attempt=setup_run(monkeypatch)
    b={'candidates':[{'code':'000001'},{'code':'000002'},{'code':'000003'}]}
    JC.observe_targets(briefing=b,account={'holdings':[]})
    good=submission(M.get_briefing()); result=M.record_ai_judgments([good])[0]
    assert result['status']=='accepted'; watch(result['judgment_id'])
    bad=copy.deepcopy(good);bad.update(request_key='bad-other',code='000002')
    assert M.record_ai_judgments([bad])[0]['status']=='rejected'
    after=C.now()+timedelta(seconds=1)
    monkeypatch.setattr(C,'now',lambda:after)
    row=register(monkeypatch)
    data=D.build_inputs(row['validation_id'])
    targets={x['code']:x for x in data['opportunities']}
    assert targets['000001']['coverage_status']=='linked'
    assert targets['000002']['coverage_status']=='rejected'
    assert targets['000003']['coverage_status']=='not_submitted'
    assert targets['000003']['raw_confidence'] is None and not targets['000003']['judgments']
    assert data['events'] and data['engine_definitions']
    assert D.build_inputs(row['validation_id'])==data
    # Later discovery neither fills missing judgment nor mutates frozen calls.
    JC.observe_targets(briefing={'candidates':[{'code':'000004'}]},account={'holdings':[]})
    assert D.get_inputs(row['validation_id'])==data


def test_time_cutoff_excludes_future_attempt_and_preserves_corruption(monkeypatch):
    row=register(monkeypatch)
    future=C.now()+timedelta(seconds=1)
    monkeypatch.setattr(C,'now',lambda:future)
    JC.start_attempt('future','decide',{'candidates':[{'code':'f'}]},{'holdings':[]})
    data=D.build_inputs(row['validation_id'])
    assert not data['opportunities'] and any(g['reason']=='no_original_attempt_manifests' for g in data['gaps'])
    broken=Path(C.WORK)/'ontology/attempts/broken.json';broken.write_text('{')
    data=D.build_inputs(row['validation_id'],refresh_key='audit-broken')
    assert not data['complete'] and any(g.get('record')=='broken.json' for g in data['gaps'])


def test_max_hold_days_documents_actual_weekday_unit(monkeypatch):
    snapshot=P.target_snapshot()
    target=next(x for x in snapshot['targets'] if x['target_id']=='parameter.decision.max_hold_days')
    assert target['unit']=='weekday_count'
    assert 'without exchange-holiday exclusion' in target['meaning']


def test_local_market_import_preserves_actual_import_time_and_unverified_provenance(monkeypatch):
    row=register(monkeypatch);now=C.now(); observed=now-timedelta(minutes=1)
    tape=[{'observation_id':'quote1','code':'000001','observed_at':observed.isoformat(),
           'available_at':observed.isoformat(),'price':20400,'price_basis':'unadjusted',
           'source':'local_test_fixture','executable_qty':12}]
    result=D.import_validation_market(row['validation_id'],'market1',tape)
    assert result['imported_at']==now.isoformat()
    assert result['historical_availability_verified'] is False
    assert D.import_validation_market(row['validation_id'],'market1',tape)==result
    changed=copy.deepcopy(tape);changed[0]['price']=20500
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        D.import_validation_market(row['validation_id'],'market1',changed)
    inputs=D.build_inputs(row['validation_id'])
    assert inputs['simulation_inputs']['market_observations']==tape
    assert inputs['simulation_inputs']['historical_market_availability_verified'] is False
    assert not inputs['simulation_inputs']['complete']
    assert any(g['reason']=='original_initial_account_unavailable' for g in inputs['simulation_inputs']['gaps'])


@pytest.mark.parametrize('field,value', [('price',float('nan')),('price_basis','adjusted'),('executable_qty',True)])
def test_market_import_does_not_silently_repair_invalid_values(monkeypatch,field,value):
    row=register(monkeypatch);at=C.now().isoformat()
    tape=[{'observation_id':'q','code':'000001','observed_at':at,'available_at':at,'price':20400,
           'price_basis':'unadjusted','source':'local_fixture','executable_qty':10}]
    tape[0][field]=value
    with pytest.raises(ValueError):D.import_validation_market(row['validation_id'],'bad',tape)
    assert not S.records('market_imports')['records']


def test_actual_order_plan_dataset_preserves_unverified_loaded_engine(monkeypatch):
    from test_execution_integration import planned
    import replay_context as R
    _,attempt,_,args=planned(monkeypatch)
    after=C.now()+timedelta(seconds=1);monkeypatch.setattr(C,'now',lambda:after)
    row=register(monkeypatch);data=D.build_inputs(row['validation_id'])
    original=next(e for e in data['events'] if e['kind']=='order_plan')
    assert original['opportunity_ids']
    target=next(o for o in data['opportunities'] if o['opportunity_id'] in original['opportunity_ids'])
    assert target['raw_confidence'] is not None
    result=R.replay_event(original,data['engine_definitions'],row['proposal_snapshot'],target['condition_context'])
    # The shared fixture monkeypatches common.params/clock. A disk source body
    # must not claim those modified loaded functions were the original engine.
    assert result['baseline_status']=='incomplete',result
    assert result['reason']=='original_loaded_definition_unverified'
    assert data['simulation_inputs']['complete'] is False
    assert any(g['reason']=='original_final_decision_stream_missing' for g in data['simulation_inputs']['gaps'])


def test_runtime_captured_after_original_decision_is_not_a_historical_prompt(monkeypatch):
    import runtime_inputs as RI
    _,attempt=setup_run(monkeypatch)
    accepted=M.record_ai_judgments([submission(M.get_briefing())])[0]
    watch(accepted['judgment_id'])
    later=C.now()+timedelta(seconds=1);monkeypatch.setattr(C,'now',lambda:later)
    RI.capture_attempt(attempt['run_attempt_id'],'decide','late current prompt',[])
    after=later+timedelta(seconds=1);monkeypatch.setattr(C,'now',lambda:after)
    row=register(monkeypatch);data=D.build_inputs(row['validation_id'])
    assert data['runtime_inputs'][attempt['run_attempt_id']]['snapshot'] is None
    assert any(g['reason']=='runtime_input_not_available_at_original_decision' for g in data['gaps'])


def test_market_refresh_creates_new_input_without_changing_old_snapshot(monkeypatch):
    row=register(monkeypatch);first=D.ensure_inputs(row['validation_id'])
    at=C.now().isoformat()
    D.import_validation_market(row['validation_id'],'new-tape',[{'observation_id':'new1','code':'000001',
        'observed_at':at,'available_at':at,'price':20400,'price_basis':'unadjusted','source':'local_test','executable_qty':0}])
    second=D.ensure_inputs(row['validation_id'])
    assert first['input_id']!=second['input_id']
    assert D.get_inputs(row['validation_id'],first['input_id'])==first
    assert second['market_imports'] and D.ensure_inputs(row['validation_id'])==second
    assert D.get_inputs(row['validation_id'])==second and second['input_revision']==first['input_revision']+1


def test_policy_pointer_is_separate_base_provenance_not_numeric_settings(monkeypatch):
    pid=proposal(monkeypatch);before=P.target_snapshot();params=C.params()
    params['_ontology_policy']={'revision':'new-active-revision','package_id':'p1'}
    monkeypatch.setattr(C,'params',lambda:copy.deepcopy(params))
    after=P.target_snapshot()
    assert after['settings_hash']==before['settings_hash']
    assert after['policy_base_ref']==params['_ontology_policy']
    assert '_ontology_policy' not in after['params']
    exported=P.export_validation_bundle(pid)
    assert exported['base_status']=='stale_base'
    assert exported['base_snapshot'].get('policy_base_ref') is None


@pytest.mark.parametrize('extra,reason',[
    ({'stop_rules':{'max_loss':.1}},'UNSUPPORTED_STOP_RULES'),
    ({'prompt':{'temperature':0}},'UNSUPPORTED_PROMPT_SETTING'),
    ({'comparison_kind':'prompt_pair','prompt':{'model':'fixed','budget_usd':1}},'EXPLICIT_REPETITIONS_REQUIRED'),
])
def test_unsupported_or_incomplete_execution_settings_are_not_silently_ignored(extra,reason):
    with pytest.raises(ValueError,match=reason):V.validate_spec({**spec(),**extra})
