"""Actual local research delivery -> structured proposal -> stage-7 bundle."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from test_ontology import C, ROOT, isolated
from test_case_review import completed_case
from test_close_evaluation import calendar
import case_store as CS
import case_review as CR
import proposal_store as S
import learning_proposals as P
import research_evidence as E


def source(monkeypatch, review_id='research-one'):
    result={'review_id':review_id,'market_as_of':'2026-11-05','knowledge_as_of':C.now().isoformat(),
            'analysis_status':'complete','recording_status':'recorded','expected_count':3,'case_ids':['a','b','c'],
            'comparison':{'summary':{'refutation_rate':.5,'complete_count':2,'refuted_count':1}},
            'statistics':{'models':[]}, 'metric_catalog':[
               {'metric_id':'rate','path':'/comparison/summary/refutation_rate','value':.5,
                'display_value':.5,'precision':6,'unit':'ratio','status':'computed'}]}
    cases=[]
    for identifier,flow,label in [('a',-4e9,1),('b',-3e9,0),('c',None,None)]:
        cases.append({'application_id':identifier,'stratum_id':'layer1','stratum':{'days':3},
                      'features':{'gap':.002,'slope':-.01,'foreign_ratio':.3,'institution_ratio':flow/1e10 if flow else None,'regime':'하락'},
                      'signed_flows':{'foreign':3e9,'institution':flow,'interpretation':'received_values_not_simultaneous'},
                      'outcome_eligible':label is not None,'label':label,'verdict':'refuted' if label else 'maintained' if label==0 else 'unknown',
                      'judgment_snapshot':{'confidence_raw':.7},'execution_context':{},'adoption':{}})
    CS.archive('jobs',review_id,{'review_id':review_id,'spec_version':CR.SPEC_VERSION})
    CS.archive('results',review_id,result)
    CS.archive('datasets',review_id,{'review_id':review_id,'cases':cases,'source_definitions':{}})
    CS.archive('publication_commits',review_id,{'review_id':review_id,'publication_ids':[],'published_at':C.now().isoformat()})
    run=P.build_context(review_id)
    assert run['context_status']=='ready',run
    context=P.get_proposal_context(run['proposal_run_id'])
    page=P.get_proposal_cases(run['proposal_run_id'])
    assert context['recording_status']=='recorded',context
    return run,context,page


def item(context,page,identifier='flow',kind='conditional_rule_change'):
    target=next(t for t in context['targets'] if t['target_id']=='confidence.conditional')
    metric=next(m for m in context['catalog'] if m['metric_id']=='rate')
    return {'item_id':identifier,'kind':kind,'title':'연구용 수급 조정',
            'delivery_ids':[context['delivery_id'],page['delivery_id']],
            'fact_claims':[{'id':'fact1','reference':metric['reference'],'op':'value','value':50,'unit':'%'}],
            'stratum_ids':['layer1'],
            'condition':{'op':'lt','left':{'field':'signed_flows.institution'},'right':{'value':0,'unit':'KRW'}},
            'target_ref':target['target_ref'],
            'effect':{'operation':'add_confidence','amount':-.04,'unit':'confidence','placement':'after_legacy_boost_before_cap'},
            'validation_kind':'deterministic_rule','validation_question':'행동과 비용 후 결과가 어떻게 변하는가?',
            'expected_change':'조건 안에서 확신도를 낮출 수 있다는 미검증 예상','reason':'모의 비교를 바탕으로 별도 시험',
            'support_case_ids':['a'],'counter_case_ids':['b']}


def test_two_proposals_share_sources_but_keep_distinct_conditions_and_questions(monkeypatch):
    run,context,page=source(monkeypatch);rid=run['proposal_run_id']
    first=item(context,page)
    second=item(context,page,'trend')
    second['condition']={'op':'and','left':{'op':'lte','left':{'field':'features.gap'},'right':{'value':.005,'unit':'ratio'}},
                         'right':{'op':'lt','left':{'field':'features.slope'},'right':{'value':0,'unit':'ratio'}}}
    second['validation_question']='수급과 별도로 추세 조건의 효과가 있는가?'
    result=P.record_learning_proposals(rid,'two',[first,second])
    assert result['status']=='accepted',result
    saved=P.get_learning_proposals(rid)
    assert saved['submission_status']=='accepted' and len(saved['proposals'])==2
    a,b=saved['proposals']
    assert a['server_cohort']['matched_case_ids']==['a','b']
    assert a['server_cohort']['unknown_condition_case_ids']==['c']
    assert a['server_cohort']['maintained_case_ids']==['b']
    assert b['server_cohort']['matched_case_ids']==['a','b','c']
    assert a['validation_question']!=b['validation_question']
    assert a['active'] is False and a['effect_validation']=='submitted_unverified'
    bundle=P.export_validation_bundle(a['proposal_id'])
    assert bundle['baseline_ai_score_policy']=='freeze_original' and not bundle['stage7_completed']
    assert bundle['readiness']['status']=='replay_inputs_incomplete'
    assert all(not x['independent_unused'] for x in bundle['usage_status']['items'])


@pytest.mark.parametrize('change,code',[
 ('wrong_rate','FACT_MISMATCH'),('wrong_review','NOT_DELIVERED'),('wrong_hash','NOT_DELIVERED'),
 ('future_condition','FORBIDDEN_CONDITION_FIELD'),('unit','CONDITION_UNIT_MISMATCH'),
 ('target','TARGET_REFERENCE_MISMATCH'),('wrong_validation','VALIDATION_KIND_MISMATCH'),
 ('nan','SCHEMA_ERROR'),('outside_case','CASE_NOT_DELIVERED')])
def test_wrong_facts_conditions_or_targets_rejected_independently(monkeypatch,change,code):
    run,context,page=source(monkeypatch);bad=item(context,page)
    if change=='wrong_rate':bad['fact_claims'][0]['value']=75
    elif change=='wrong_review':bad['fact_claims'][0]['reference']['review_id']='another'
    elif change=='wrong_hash':bad['fact_claims'][0]['reference']['result_hash']='another'
    elif change=='future_condition':bad['condition']['left']['field']='label'
    elif change=='unit':bad['condition']['right']['unit']='ratio'
    elif change=='target':bad['target_ref']['settings_hash']='old'
    elif change=='wrong_validation':bad['validation_kind']='prompt_lesson'
    elif change=='nan':bad['effect']['amount']=float('nan')
    elif change=='outside_case':bad['support_case_ids']=['missing']
    result=P.record_learning_proposals(run['proposal_run_id'],'bad',[bad])
    assert result['status']=='rejected',result
    errors=result.get('errors') or [x['error'] for x in result['items']]
    assert errors[0]['code']==code
    assert P.get_learning_proposals(run['proposal_run_id'])['submission_status']=='rejected'


def test_context_does_not_deliver_unseen_case_page(monkeypatch):
    run,context,page=source(monkeypatch)
    assert not any(x.get('application_id') for x in context['catalog'])
    bad=item(context,page);bad['delivery_ids']=[context['delivery_id']]
    result=P.record_learning_proposals(run['proposal_run_id'],'unseen',[bad])
    assert result['items'][0]['error']['code']=='CASE_NOT_DELIVERED'
    first_page=P.get_proposal_cases(run['proposal_run_id'],0,1)
    bad['delivery_ids']=[context['delivery_id'],first_page['delivery_id']]
    result=P.record_learning_proposals(run['proposal_run_id'],'unseen-counter',[bad])
    assert result['items'][0]['error']['code']=='CASE_NOT_DELIVERED'


def test_retained_and_insufficient_are_valid_and_cannot_activate(monkeypatch):
    run,context,page=source(monkeypatch)
    value=item(context,page,kind='retain_current')
    value.pop('target_ref');value.pop('effect');value['validation_kind']='none';value['reason_kind']='insufficient_evidence'
    result=P.record_learning_proposals(run['proposal_run_id'],'retain',[value])
    assert result['status']=='accepted'
    row=P.get_learning_proposals(run['proposal_run_id'])['proposals'][0]
    assert row['status']=='retained' and row['active'] is False
    bad={**value,'approved':True}
    assert P.record_learning_proposals(run['proposal_run_id'],'forged',[bad])['status']=='rejected'


def test_prompt_lesson_requires_model_reinvocation(monkeypatch):
    run,context,page=source(monkeypatch)
    value=item(context,page,kind='lesson_candidate')
    value['target_ref']=next(t for t in context['targets'] if t['target_id']=='lesson.guidance')['target_ref']
    value['effect']={'operation':'prompt_guidance','text':'수급 방향이 엇갈린 경우 기간과 상대 규모를 함께 확인'}
    value['validation_kind']='prompt_lesson'
    result=P.record_learning_proposals(run['proposal_run_id'],'lesson',[value])
    assert result['status']=='accepted'
    bundle=P.export_validation_bundle(result['items'][0]['proposal_id'])
    assert bundle['baseline_ai_score_policy']=='requires_model_reinvocation'
    assert bundle['proposal']['active'] is False


def test_parameter_proposal_checks_frozen_allowlist_without_writing(monkeypatch):
    run,context,page=source(monkeypatch)
    value=item(context,page,kind='parameter_change')
    target=next(t for t in context['targets'] if t['target_id']=='parameter.decision.buy_min_confidence')
    value['target_ref']=target['target_ref'];value['effect']={'operation':'set_parameter','value':target['value']+.01,'unit':target['unit']}
    original=(ROOT/'config/params.json').read_bytes()
    result=P.record_learning_proposals(run['proposal_run_id'],'parameter',[value])
    assert result['status']=='accepted',result
    assert (ROOT/'config/params.json').read_bytes()==original
    value['effect']['value']=.99
    assert P.record_learning_proposals(run['proposal_run_id'],'invalid-parameter',[value])['items'][0]['error']['code']=='PARAMETER_RANGE_OR_STEP'


def test_version_retry_conflict_and_current_base_drift(monkeypatch):
    run,context,page=source(monkeypatch);value=item(context,page);rid=run['proposal_run_id']
    first=P.record_learning_proposals(rid,'one',[value]);pid=first['items'][0]['proposal_id']
    assert P.record_learning_proposals(rid,'one',[value])['idempotent']
    changed=copy.deepcopy(value);changed['effect']['amount']=-.02
    assert P.record_learning_proposals(rid,'one',[changed])['errors'][0]['code']=='IDEMPOTENCY_CONFLICT'
    changed['supersedes_proposal_id']=pid
    second=P.record_learning_proposals(rid,'two',[changed])
    assert second['items'][0]['version']==2
    before=S.load('proposals',pid)
    baseline=C.params()
    monkeypatch.setattr(C,'params',lambda:{**baseline,'extra_changed_setting':True})
    projection=P.get_learning_proposals(rid)
    assert all(p['base_status']=='stale_base' for p in projection['proposals'])
    assert S.load('proposals',pid)==before


def test_actual_step1_to_step6_and_no_operational_side_effect(monkeypatch,calendar):
    fake,manifest,app=completed_case(monkeypatch,calendar)
    report=CR.build_review()
    protected={p:p.read_bytes() for p in Path(C.WORK).rglob('*') if p.is_file() and '/case_review/' not in str(p)}
    run=P.build_context(report['review_id'])
    assert run['context_status']=='ready',run
    assert run['submission_status']=='not_submitted'
    delivered=P.get_proposal_context(run['proposal_run_id'])
    assert delivered['catalog'] and len(fake.orders)==1
    assert all(p.read_bytes()==b for p,b in protected.items())
    assert not Path(C.WORK,'lessons.json').exists()


def test_concurrent_submission_commits_once_and_partial_store_is_not_acceptance(monkeypatch):
    run,context,page=source(monkeypatch);rid=run['proposal_run_id'];value=item(context,page)
    with ThreadPoolExecutor(2) as pool:
        rows=list(pool.map(lambda _:P.record_learning_proposals(rid,'same',[copy.deepcopy(value)]),range(2)))
    assert len({r['items'][0]['proposal_id'] for r in rows})==1
    assert len(S.records('proposals')['records'])==1
    archive=S.archive
    def fail(kind,*a):
        if kind=='submissions':raise OSError('commit failed')
        return archive(kind,*a)
    monkeypatch.setattr(S,'archive',fail)
    failed=P.record_learning_proposals(rid,'uncommitted',[value])
    assert failed['status']=='recording_error'
    assert len(P.get_learning_proposals(rid)['proposals'])==1


def test_failed_delivery_not_a_verified_reference(monkeypatch):
    run,context,page=source(monkeypatch);rid=run['proposal_run_id']
    archive=S.archive
    def fail(kind,*a):
        if kind=='deliveries':raise OSError('disk')
        return archive(kind,*a)
    monkeypatch.setattr(S,'archive',fail)
    response=P.get_proposal_context(rid)
    assert response['recording_status']=='recording_error' and 'delivery_id' not in response
    assert P.get_learning_proposals(rid)['run_status']=='recording_error'


def test_source_incomplete_and_partial_context_cannot_become_empty_success(monkeypatch):
    result=P.build_context('not-real')
    assert result['run_status']=='source_not_ready'
    run,context,page=source(monkeypatch)
    (S.root()/'context_commits'/f"{run['proposal_run_id']}.json").unlink()
    assert P.get_learning_proposals(run['proposal_run_id'])['run_status']=='unfinished'


def test_schema_rejection_and_ai_termination_states(monkeypatch):
    run,context,page=source(monkeypatch);rid=run['proposal_run_id']
    P.record_learning_proposals(rid,'',{})
    assert P.get_learning_proposals(rid)['submission_status']=='rejected'
    S.archive('ai_attempts','ai1',{'proposal_run_id':rid,'attempt_id':'ai1','created_at':C.now().isoformat(),'status':'started'})
    assert P.get_learning_proposals(rid)['ai_execution_status']=='unfinished'
    S.archive('ai_results','ai1',{'proposal_run_id':rid,'attempt_id':'ai1','created_at':C.now().isoformat(),'status':'timeout'})
    assert P.get_learning_proposals(rid)['run_status']=='partial'


def test_unknown_condition_not_zero_and_both_branches_are_validated(monkeypatch):
    run,context,page=source(monkeypatch);value=item(context,page)
    value['condition']={'op':'eq','left':{'field':'signed_flows.institution'},'right':{'value':0,'unit':'KRW'}}
    value['support_case_ids']=[];value['counter_case_ids']=[]
    result=P.record_learning_proposals(run['proposal_run_id'],'zero',[value])
    cohort=P.get_learning_proposals(run['proposal_run_id'])['proposals'][0]['server_cohort']
    assert cohort['matched_case_ids']==[] and cohort['unknown_condition_case_ids']==['c']
    value['condition']={'op':'or','left':{'value':True,'unit':'bool'},
                        'right':{'op':'gt','left':{'field':'reference_returns'},'right':{'value':0,'unit':'ratio'}}}
    assert P.record_learning_proposals(run['proposal_run_id'],'bypass',[value])['items'][0]['error']['code']=='FORBIDDEN_CONDITION_FIELD'


def test_no_targets_and_repeated_context_do_not_manufacture_maintain_result(monkeypatch):
    run,context,page=source(monkeypatch)
    before={p:p.read_bytes() for p in S.root().rglob('*') if p.is_file()}
    again=P.build_context('research-one')
    assert again['proposal_run_id']==run['proposal_run_id'] and again['submission_status']=='not_submitted'
    assert all(p.read_bytes()==b for p,b in before.items())
    rid='empty'
    CS.archive('jobs',rid,{'review_id':rid,'spec_version':CR.SPEC_VERSION})
    CS.archive('results',rid,{'review_id':rid,'market_as_of':'2026-11-05','knowledge_as_of':C.now().isoformat(),
                'analysis_status':'complete','recording_status':'recorded','expected_count':0,'case_ids':[],'metric_catalog':[]})
    CS.archive('datasets',rid,{'review_id':rid,'cases':[]})
    CS.archive('publication_commits',rid,{'review_id':rid,'publication_ids':[]})
    empty=P.build_context(rid)
    assert empty['run_status']=='no_targets' and empty['proposals']==[]


def test_uncommitted_context_never_exposes_delivery(monkeypatch):
    run,context,page=source(monkeypatch)
    rid=run['proposal_run_id'];(S.root()/'context_commits'/f'{rid}.json').unlink()
    count=len(S.records('deliveries')['records'])
    output=P.get_proposal_context(rid)
    assert output['recording_status']=='recording_error'
    assert len(S.records('deliveries')['records'])==count


def test_failed_definition_reference_does_not_make_replay_ready(monkeypatch):
    run,context,page=source(monkeypatch)
    stored=P._load_context(run['proposal_run_id'])
    for case in stored['cases']:
        case['execution_context']={'orders':[{'events':[{'kind':'order_plan','inputs':{'account':{'cash':1},'quote':{'price':1},'confidence':{'raw':.7}},
                              'definition':{'recording_status':'recording_error','definition_id':'missing'}}]}]}
    value=item(context,page)
    validated=P.validate_proposal(value,stored)
    assert validated['readiness']['status']=='replay_inputs_incomplete'


def test_stale_base_export_cannot_be_presented_as_ready(monkeypatch):
    run,context,page=source(monkeypatch)
    result=P.record_learning_proposals(run['proposal_run_id'],'base',[item(context,page)])
    pid=result['items'][0]['proposal_id']
    baseline=C.params();monkeypatch.setattr(C,'params',lambda:{**baseline,'changed':True})
    output=P.export_validation_bundle(pid)
    assert output['status']=='stale_base' and output['readiness']['status']=='stale_base'
    assert output['base_snapshot']['params']==baseline


def test_different_read_contexts_cannot_mix_compound_same_id(monkeypatch):
    run,context,page=source(monkeypatch,'first')
    _,other,_=source(monkeypatch,'second')
    value=item(context,page)
    value['fact_claims'][0]['reference']=other['catalog'][0]['reference']
    result=P.record_learning_proposals(run['proposal_run_id'],'mix',[value])
    assert result['items'][0]['error']['code']=='NOT_DELIVERED'


def test_failed_delivery_usage_cannot_leave_an_accepted_reference(monkeypatch):
    run,context,page=source(monkeypatch);rid=run['proposal_run_id']
    original=E.record_usage
    def fail(*a,**k):
        if len(a)>2 and a[2]=='step6_delivered': raise OSError('usage commit failed')
        return original(*a,**k)
    before={x['delivery_id'] for x in S.records('deliveries')['records']}
    monkeypatch.setattr(E,'record_usage',fail)
    output=P.get_proposal_context(rid)
    assert output['recording_status']=='recording_error' and 'delivery_id' not in output
    orphan=next(x for x in S.records('deliveries')['records'] if x['delivery_id'] not in before)
    monkeypatch.setattr(E,'record_usage',original)
    bad=item(context,page);bad['delivery_ids']=[orphan['delivery_id'],page['delivery_id']]
    result=P.record_learning_proposals(rid,'uncommitted-delivery',[bad])
    assert result['items'][0]['error']['code']=='DELIVERY_MISMATCH'


def test_submission_retry_after_commit_failure_reuses_exact_saved_version(monkeypatch):
    run,context,page=source(monkeypatch);rid=run['proposal_run_id'];value=item(context,page)
    original=S.archive
    def fail(kind,*a):
        if kind=='submissions': raise OSError('final commit unavailable')
        return original(kind,*a)
    monkeypatch.setattr(S,'archive',fail)
    assert P.record_learning_proposals(rid,'recoverable',[value])['status']=='recording_error'
    orphan=S.records('proposals')['records'][0]
    assert P._load_proposal(orphan['proposal_id']) is None
    monkeypatch.setattr(S,'archive',original)
    fixed=C.now()+timedelta(seconds=5);monkeypatch.setattr(C,'now',lambda:fixed)
    altered=copy.deepcopy(value);altered['effect']['amount']=-.02
    conflict=P.record_learning_proposals(rid,'recoverable',[altered])
    assert conflict['errors'][0]['code']=='IDEMPOTENCY_CONFLICT'
    recovered=P.record_learning_proposals(rid,'recoverable',[value])
    assert recovered['status']=='accepted' and recovered['items'][0]['proposal_id']==orphan['proposal_id']
    assert P._load_proposal(orphan['proposal_id'])==orphan
    assert len(S.records('proposals')['records'])==1
    assert P.get_learning_proposals(rid)['submission_status']=='accepted'


@pytest.mark.parametrize('identity,expected',[(None,'replay_inputs_incomplete'),
                        ('unverified_loaded_source','replay_inputs_incomplete'),
                        ('verified_loaded_source','ready_for_stage7_design')])
def test_deterministic_replay_requires_verified_original_loaded_sources(monkeypatch,identity,expected):
    run,context,page=source(monkeypatch)
    stored=P._load_context(run['proposal_run_id'])
    for case in stored['cases']:
        body={'sha256':'original-hash','sources':{'common.py':'original source'}}
        if identity:body['source_identity']={'status':identity}
        case['execution_context']={'non_order_paths':[{'events':[
            {'kind':'order_plan','inputs':{'account':{'cash':1},'quote':{'price':1},'confidence':{'raw':.7}},
             'definition':{'definition_id':'original-definition','sha256':'original-hash'}}]}],
            'engine_definitions':{'original-definition':{'status':'recorded','definition':body}}}
    value=item(context,page)
    assert P.validate_proposal(value,stored)['readiness']['status']==expected


@pytest.mark.parametrize('operation',['value_with_direction','direction_with_value','compare_with_value'])
def test_claim_operations_cannot_smuggle_a_contradictory_payload(monkeypatch,operation):
    run,context,page=source(monkeypatch);value=item(context,page)
    if operation=='value_with_direction':
        value['fact_claims'][0]['direction']='negative'
    elif operation=='direction_with_value':
        metric=next(x for x in page['catalog'] if x.get('application_id')=='a' and x.get('feature_name')=='signed_flows.institution')
        value['fact_claims']=[{'id':'f1','reference':metric['reference'],'feature_name':metric['feature_name'],
                              'op':'direction','direction':'negative','value':40,'unit':'억원'}]
    else:
        ref=value['fact_claims'][0]['reference']
        value['fact_claims']=[{'id':'f1','reference':ref,'op':'compare','right_reference':ref,
                              'operator':'eq','result':True,'value':999,'unit':'KRW'}]
    result=P.record_learning_proposals(run['proposal_run_id'],operation,[value])
    assert result['status']=='rejected' and result['items'][0]['error']['code']=='SCHEMA_ERROR'


@pytest.mark.parametrize('flag,value',[('active',True),('approved',True),('stage7_completed',True),('status','approved')])
def test_ai_cannot_submit_activation_or_approval_flags(monkeypatch,flag,value):
    run,context,page=source(monkeypatch);proposal=item(context,page);proposal[flag]=value
    result=P.record_learning_proposals(run['proposal_run_id'],flag,[proposal])
    assert result['items'][0]['status']=='rejected' and result['items'][0]['error']['code']=='SCHEMA_ERROR'


def test_all_tuning_targets_have_explicit_units_stages_and_captured_consumers():
    base=P.target_snapshot()
    targets={t['key']:t for t in base['targets'] if t.get('key')}
    assert set(targets)==set(base['tuning']['keys'])==set(base['registry']['parameter_bindings'])
    assert len(targets)==14
    assert targets['screen.spike_penalty']['unit']=='score_points'
    assert targets['screen.spike_ret5']['unit']=='ratio'
    assert targets['decision.max_hold_days']['unit']=='weekday_count'
    assert targets['decision.sell_min_confidence']['call_stage']=='sell_risk_check'
    assert targets['decision.stop_loss_pct']['call_stage']=='position_initialization_and_monitor'
    assert {'data_prep.py','intraday.py','broker.py','monitor.py','mcp_server.py','tuning.py'}<=set(base['sources'])
    for target in targets.values():
        assert target['meaning'] and target['consumers']
        assert all(c['file'] in base['sources'] and c['function'] and c['stage'] for c in target['consumers'])


def test_new_tuning_key_cannot_receive_an_inferred_default_target(monkeypatch):
    import tuning
    old=tuning.rules()
    monkeypatch.setattr(tuning,'rules',lambda:{**old,'keys':{**old['keys'],'decision.new_future_key':{'min':0,'max':1,'step':.1}}})
    with pytest.raises(ValueError,match='UNREGISTERED_PARAMETER_TARGET:decision.new_future_key'):
        P.target_snapshot()


def test_registered_consumer_must_exist_in_the_frozen_source(monkeypatch,tmp_path):
    registry=json.loads(P.TARGET_PATH.read_text())
    registry['parameter_bindings']['decision.stop_loss_pct']['consumers'][0]['function']='deleted_exit_function'
    path=tmp_path/'registry.json';path.write_text(json.dumps(registry))
    monkeypatch.setattr(P,'TARGET_PATH',path)
    with pytest.raises(ValueError,match='PARAMETER_CONSUMER_SOURCE_MISSING:decision.stop_loss_pct'):
        P.target_snapshot()


def test_score_penalty_cannot_be_submitted_as_a_return_ratio(monkeypatch):
    run,context,page=source(monkeypatch)
    value=item(context,page,kind='parameter_change')
    target=next(t for t in context['targets'] if t['target_id']=='parameter.screen.spike_penalty')
    value['target_ref']=target['target_ref']
    value['effect']={'operation':'set_parameter','value':target['value'],'unit':'ratio'}
    rejected=P.record_learning_proposals(run['proposal_run_id'],'wrong-penalty-unit',[value])
    assert rejected['items'][0]['error']['code']=='EFFECT_SCHEMA_ERROR'
    value['effect']['unit']='score_points'
    accepted=P.record_learning_proposals(run['proposal_run_id'],'correct-penalty-unit',[value])
    assert accepted['status']=='accepted'
    assert any(x['reason']=='original_complete_screening_pool_required' for x in accepted['items'][0]['readiness']['missing_inputs'])


@pytest.mark.parametrize('key',['decision.stop_loss_pct','decision.take_profit_pct','decision.max_hold_days'])
def test_exit_target_cannot_claim_ready_from_plan_without_position_and_monitor_sequence(monkeypatch,key):
    run,context,page=source(monkeypatch)
    stored=P._load_context(run['proposal_run_id'])
    for case in stored['cases']:
        case['execution_context']={'orders':[{'events':[{'kind':'order_plan',
             'inputs':{'account':{'cash':1},'quote':{'price':1},'confidence':{'raw':.7}},
             'definition':{'definition_id':'original','sha256':'original-hash'}}]}],
             'engine_definitions':{'original':{'status':'recorded','definition':{'sha256':'original-hash',
                        'sources':{'common.py':'original source'},'source_identity':{'status':'verified_loaded_source'}}}}}
    value=item(context,page,kind='parameter_change')
    target=next(t for t in context['targets'] if t.get('key')==key)
    value['target_ref']=target['target_ref'];value['effect']={'operation':'set_parameter','value':target['value'],'unit':target['unit']}
    ready=P.validate_proposal(value,stored)['readiness']
    reasons={x['reason'] for x in ready['missing_inputs']}
    assert ready['status']=='replay_inputs_incomplete'
    assert reasons=={'original_position_initialization_and_updates_required','original_monitor_trigger_and_price_sequence_required'}


def test_monitor_source_changes_invalidate_the_proposal_base(monkeypatch):
    run,context,page=source(monkeypatch)
    result=P.record_learning_proposals(run['proposal_run_id'],'monitor-base',[item(context,page)])
    original=Path.read_text
    def changed(path,*a,**kw):
        content=original(path,*a,**kw)
        return content+'\n# changed monitor definition\n' if path==ROOT/'src/monitor.py' else content
    monkeypatch.setattr(Path,'read_text',changed)
    bundle=P.export_validation_bundle(result['items'][0]['proposal_id'])
    assert bundle['status']=='stale_base'
    assert not bundle['base_snapshot']['sources']['monitor.py'].endswith('# changed monitor definition\n')
