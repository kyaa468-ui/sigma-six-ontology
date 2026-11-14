"""T01-T12/T14: real local MCP paths with fixed market/account inputs."""
import copy
import os
from pathlib import Path

import pytest

from test_ontology import isolated, C, M, P, clone
from test_judgment_integration import setup_run, submission
from test_execution_integration import hook_call
import ai_judgment as J
import semantic_layer as S

pytestmark = pytest.mark.semantic


def as_role(monkeypatch, role):
    monkeypatch.setenv('SION_SEMANTIC_ROLE', role)


def interpretation(monkeypatch):
    fake, manifest = setup_run(monkeypatch)
    as_role(monkeypatch, 'interpretation')
    text = M.get_briefing()
    import evidence_wire as W
    if W.enabled():
        import json
        from test_compact_integration import unpack
        payload = json.loads(text.split('\n',1)[1])
        e = next(r for r in unpack(payload) if r['field']=='inst_net' and r['subject']=='000001')
        source = {'wire_format':W.FORMAT,'bundle_refs':[payload['bundle_alias']],
                  'arguments':[{'id':'inst','kind':'fact_claim','stance':'oppose',
                                'evidence':e['ref'],'measurement_kind':e['kind'],
                                'assertion':{'op':'sign','direction':'sell'}}]}
    else:
        source = submission(text)
    source['arguments'].append({'id':'h1','kind':'hypothesis','text':'기관 매도가 이어져 매도 압력이 발생할 수 있다',
                                'claim_ids':['inst'],'stance':'oppose'})
    data = {**{k:source[k] for k in ('delivery_ids','wire_format','bundle_refs') if k in source}, 'arguments':source['arguments'],
            'paths':[{'id':'p1','claim_ids':['inst','h1'],'assumptions':['현재 매도가 이어짐'],
                      'grouping_reason':'동일한 매도 압력 경로','grouping_status':'resolved',
                      'direction':'oppose','horizon':'기존 일반 최대 5평일 내'}]}
    ctx = M.get_semantic_context('000001','매수')
    return fake, manifest, ctx, data


def evaluation(role, path='p1', agree='unknown'):
    return {'id':role+'-eval','path_id':path,'stance':'oppose','buy_interpretation':agree,
            'evidence_clarity':'전달된 기관 순매도 수치를 확인', 'causal_clarity':'지속 여부는 가정',
            'likelihood':'지속 가능성은 있으나 확정할 수 없음','loss_magnitude':'가격 영향 규모는 자료 부족',
            'reason':'매도 압력의 가능성을 검토','uncertainties':['지속성 미확인'],'refs':[path]}


def through_bear(monkeypatch):
    fake, manifest, ctx, data = interpretation(monkeypatch)
    assert M.submit_semantic_stage(ctx['input_id'],data)['status']=='accepted'
    for role in ('bull','bear'):
        as_role(monkeypatch,role)
        c=M.get_semantic_context('000001','매수')
        assert M.submit_semantic_stage(c['input_id'],{'evaluations':[evaluation(role)],
               'objections':[{'id':role+'-objection','text':'매도가 단기 일회성일 수 있다','refs':['h1']}]})['status']=='accepted'
    as_role(monkeypatch,'lead')
    ctx=M.get_semantic_context('000001','매수')
    final={'confidence_band_id':'clear_risk','confidence_raw':.7,'recommended_action':'매수',
           'decision':'기관 매도 경로를 뚜렷한 위험 하나로 정성 평가했다. 지속성과 손실 규모에는 불확실성이 있다.',
           'adopted_claim_ids':['inst','h1'], 'salient_risk_path_ids':['p1'],
           'dispositions':[{'evaluation_id':role+'-'+suffix,'state':'deferred' if suffix=='objection' else 'adopted',
                            'reason':'지속성은 불확실하나 기존 구간 판단에서 검토함'}
                           for role in ('bull','bear') for suffix in ('eval','objection')]}
    return fake, manifest, ctx, final


def completed(monkeypatch):
    fake,manifest,ctx,data=through_bear(monkeypatch)
    result=M.submit_semantic_stage(ctx['input_id'],data)
    assert result['status']=='accepted',result
    return fake,manifest,ctx,data,result['judgment_id']


def test_normal_actual_mcp_path_and_consumption(monkeypatch):
    fake,manifest,ctx,data,jid=completed(monkeypatch)
    planned=M.plan_order('000001','매수',.7,judgment_id=jid)
    assert planned['qty']>0,planned
    assert M.record_decision('000001','관망',.7,'bull','bear','검토',judgment_id=jid,confidence_for='매수')['ok']
    trace=M.get_semantic_trace('000001','매수')
    assert all(trace['stages'].values())
    assert J.load_judgment(jid)['semantic']['salient_risk_count']==1
    assert trace['stages']['lead']['input_hash']==S._read('inputs',ctx['input_id'])['payload_hash']
    assert list((C.ontology_root()/'semantic'/manifest['run_attempt_id']/'consumptions').glob('*.json'))
    assert not fake.orders


@pytest.mark.parametrize('change,code',[
    ('unit','UNIT_MISMATCH'),('period','SEMANTIC_MEASUREMENT_MISMATCH'),('target','SUBJECT_MISMATCH')])
def test_observation_semantics(monkeypatch,change,code):
    _,_,ctx,data=interpretation(monkeypatch)
    a=data['arguments'][0]
    if change=='unit': a['assertion']={'op':'value','value':-4,'unit':'share'}
    if change=='period': a['measurement_context']={'period':['2099-01-01']}
    if change=='target':
        # A foreign instrument is tested against the actual source subject.
        chain=S._read('chains',ctx['chain']['chain_id'])
        normalized=copy.deepcopy(a);normalized['proposer']='interpretation';normalized['adoption']='reference'
        with pytest.raises(J.InvalidSubmission) as exc:
            J._argument(normalized,'claim',{**J._context(),'code':'999999','confidence_for':'매수','delivery_ids':data['delivery_ids']})
        assert exc.value.error['code']==code
        return
    result=M.submit_semantic_stage(ctx['input_id'],data)
    assert result['status']=='rejected' and result['error_code']==code,result


@pytest.mark.compact_wire
@pytest.mark.parametrize('case', ['delivered', 'wrong_period', 'hidden_leaf'])
def test_compact_period_context_uses_delivered_leaf_paths(monkeypatch, case):
    _,_,ctx,data=interpretation(monkeypatch)
    data['arguments'][0]['measurement_context']={'period':['2026-11-04','2026-11-05']}
    if case=='wrong_period':
        data['arguments'][0]['measurement_context']['period'][0]='2026-11-03'
    if case=='hidden_leaf':
        import evidence_wire as W
        original=W.check_visible
        monkeypatch.setattr(W,'check_visible',lambda *a,**k: original(*a,**k)-{'period.1'})
    result=M.submit_semantic_stage(ctx['input_id'],data)
    if case=='delivered':assert result['status']=='accepted',result
    else:assert result['error_code']=='SEMANTIC_MEASUREMENT_MISMATCH',result


def test_predecessor_role_direction_and_attempt_boundaries(monkeypatch):
    _,_,ctx,data=interpretation(monkeypatch)
    as_role(monkeypatch,'lead')
    with pytest.raises(S.SemanticError,match='PREDECESSOR_MISSING'): S.context('000001','매수')
    assert S.submit(ctx['input_id'],data)['error_code']=='SEMANTIC_INPUT_SCOPE_MISMATCH'
    as_role(monkeypatch,'interpretation')
    with pytest.raises(S.SemanticError,match='TARGET_MISMATCH'): S.context('000001','매도')
    monkeypatch.setenv('RUN_ATTEMPT_ID','other-attempt')
    assert S.submit(ctx['input_id'],data)['error_code']=='SEMANTIC_INPUT_SCOPE_MISMATCH'


def test_missing_bear_and_direct_final_bypass(monkeypatch):
    _,_,ctx,data=interpretation(monkeypatch)
    assert S.submit(ctx['input_id'],data)['status']=='accepted'
    as_role(monkeypatch,'lead')
    with pytest.raises(S.SemanticError,match='PREDECESSOR_MISSING'): S.context('000001','매수')
    assert M.record_ai_judgments([{}])['error_code']=='USE_SEMANTIC_STAGE_SUBMISSION'
    assert 'SEMANTIC_ACCEPTED_JUDGMENT_REQUIRED' in M.plan_order('000001','매수',.7)['error']


def test_missing_counterargument_handling_and_explicit_unreviewed(monkeypatch):
    _,_,ctx,data=through_bear(monkeypatch)
    bad=copy.deepcopy(data);bad['dispositions'].pop()
    assert S.submit(ctx['input_id'],bad)['error_code']=='SEMANTIC_COUNTERARGUMENT_DISPOSITION_MISSING'
    data['dispositions'][-1]['state']='unreviewed'
    result=S.submit(ctx['input_id'],data)
    assert result['status']=='accepted',result
    assert J.load_judgment(result['judgment_id'])['semantic']['unreviewed_ids']==['bear-objection']


def test_input_tampering_and_accepted_retry(monkeypatch):
    _,_,ctx,data=interpretation(monkeypatch)
    result=S.submit(ctx['input_id'],data)
    assert result['status']=='accepted'
    assert S.submit(ctx['input_id'],data)['idempotent']
    bad=copy.deepcopy(data);bad['paths'][0]['horizon']='changed'
    assert S.submit(ctx['input_id'],bad)['error_code']=='SEMANTIC_ACCEPTED_STAGE_IMMUTABLE'


def test_joint_interpretation_is_not_same_citation(monkeypatch):
    _,_,ctx,data=through_bear(monkeypatch)
    data['confidence_raw']=.81;data['confidence_band_id']='strong'
    assert S.submit(ctx['input_id'],data)['error_code']=='SEMANTIC_JOINT_INTERPRETATION_REQUIRED'


def test_explicit_duplicate_paths_rejected_semantic_equivalence_not_claimed(monkeypatch):
    _,_,ctx,data=interpretation(monkeypatch)
    duplicate=copy.deepcopy(data['paths'][0]);duplicate['id']='p2';data['paths'].append(duplicate)
    assert S.submit(ctx['input_id'],data)['error_code']=='SEMANTIC_EXPLICIT_DUPLICATE_PATH'


@pytest.mark.parametrize('grouping_status, expected_count', [('resolved', 2), ('uncertain', None)])
def test_shared_cause_branches_keep_distinct_risks_and_unknown_grouping(monkeypatch, grouping_status, expected_count):
    _,_,ctx,data=interpretation(monkeypatch)
    data['arguments'].append({'id':'h2','kind':'hypothesis','text':'기관 매도를 추종하는 투자자의 추가 매도 가능성',
                              'claim_ids':['inst'],'stance':'oppose'})
    data['paths'].append({'id':'p2','claim_ids':['inst','h2'],'assumptions':['추종 매도 발생'],
                          'grouping_reason':'같은 관측이지만 추가 투자자의 추종 매도라는 별도 경로',
                          'grouping_status':grouping_status,'direction':'oppose','horizon':'기존 스윙 기간'})
    assert S.submit(ctx['input_id'],data)['status']=='accepted'
    for role in ('bull','bear'):
        as_role(monkeypatch,role);ctx=S.context('000001','매수')
        values=[]
        for path in ('p1','p2'):
            value=evaluation(role,path);value['id']=role+'-'+path;values.append(value)
        assert S.submit(ctx['input_id'],{'evaluations':values,'objections':[]})['status']=='accepted'
    as_role(monkeypatch,'lead');ctx=S.context('000001','매수')
    final={'confidence_band_id':'balanced','confidence_raw':.5,'recommended_action':'관망',
           'decision':'별도 경로를 구분하지만 통계적 독립이나 묶음의 정답은 보증하지 않는다.',
           'adopted_claim_ids':['inst','h1','h2'],'salient_risk_path_ids':['p1','p2'],
           'dispositions':[{'evaluation_id':r+'-'+p,'state':'adopted','reason':'정성 평가로 검토함'}
                           for r in ('bull','bear') for p in ('p1','p2')]}
    result=S.submit(ctx['input_id'],final)
    assert result['status']=='accepted',result
    assert J.load_judgment(result['judgment_id'])['semantic']['salient_risk_count'] is expected_count


def test_new_path_cannot_reuse_prior_evaluation_id(monkeypatch):
    _,_,ctx,data=interpretation(monkeypatch)
    assert S.submit(ctx['input_id'],data)['status']=='accepted'
    as_role(monkeypatch,'bull');ctx=S.context('000001','매수')
    assert S.submit(ctx['input_id'],{'evaluations':[evaluation('bull')],'objections':[]})['status']=='accepted'
    as_role(monkeypatch,'bear');ctx=S.context('000001','매수')
    path=copy.deepcopy(data['paths'][0]);path.update(id='bull-eval',claim_ids=['h1'])
    result=S.submit(ctx['input_id'],{'paths':[path],'evaluations':[],'objections':[]})
    assert result['error_code']=='SEMANTIC_DUPLICATE_ID'


def test_uncertain_risk_is_not_exact_count(monkeypatch):
    _,_,ctx,data=through_bear(monkeypatch)
    data['salient_risk_path_ids']=[]
    assert S.submit(ctx['input_id'],data)['error_code']=='SEMANTIC_CLEAR_RISK_COUNT_REQUIRED'


def test_current_setting_change_invalidates_without_rewriting(monkeypatch):
    _,_,ctx,data,jid=completed(monkeypatch)
    before=copy.deepcopy(J.load_judgment(jid))
    p=clone(P);p['decision']['buy_min_confidence']+=.01
    monkeypatch.setattr(C,'params',lambda:p)
    with pytest.raises(S.SemanticError,match='CONTEXT_CHANGED'): S.consume('000001','매수',.7,jid)
    assert J.load_judgment(jid)==before


def test_correction_invalidates_relations_and_preserves_history(monkeypatch):
    _,_,ctx,data,jid=completed(monkeypatch)
    before=copy.deepcopy(J.load_judgment(jid))
    invalid=S.invalidate('h1','가정 철회: 동일 원문이나 적용 대상 연결이 잘못됨')
    assert invalid['affected'] and not invalid['external_effects_cancelled']
    with pytest.raises(S.SemanticError,match='REASSESSMENT_REQUIRED'): S.consume('000001','매수',.7,jid)
    assert J.load_judgment(jid)==before


def test_same_full_inputs_preserve_existing_plan_calculation(monkeypatch):
    _,_,_,_,jid=completed(monkeypatch)
    strict=M.plan_order('000001','매수',.7,judgment_id=jid)
    monkeypatch.setenv('SION_SEMANTIC_MODE','legacy')
    legacy=M.plan_order('000001','매수',.7,judgment_id=jid)
    for field in ('qty','confidence_raw','boost','weight','limited_by','today_change'):
        assert strict.get(field)==legacy.get(field),(field,strict,legacy)


def test_wrong_number_and_nonlead_orders_are_blocked(monkeypatch):
    _,_,_,_,jid=completed(monkeypatch)
    assert 'SEMANTIC_CONFIDENCE_MISMATCH' in M.plan_order('000001','매수',.71,judgment_id=jid)['error']
    as_role(monkeypatch,'bear')
    assert M.plan_order('000001','매수',.7,judgment_id=jid)['error_code']=='SEMANTIC_ROLE_TOOL_FORBIDDEN'


def test_protective_exit_and_research_boundaries(monkeypatch):
    interpretation(monkeypatch)
    as_role(monkeypatch,'lead')
    with pytest.raises(S.SemanticError,match='PROTECTIVE_TRIGGER_REQUIRED'): S.consume('000001','매도',1,None,rule_exit=True)
    monkeypatch.setenv('RUN_MODE','exit')
    assert S.consume('000001','매도',1,None,rule_exit=True)==1
    monkeypatch.setenv('RUN_MODE','decide')
    with pytest.raises(S.SemanticError):S.consume('000001','매수',1,None,rule_exit=True)
    monkeypatch.setenv('RUN_MODE','case_review')
    assert M.get_semantic_context()['error_code']=='SEMANTIC_TRADING_MODE_REQUIRED'


def test_hook_requires_accepted_semantic_result(monkeypatch,capsys):
    interpretation(monkeypatch);as_role(monkeypatch,'lead')
    result=hook_call(monkeypatch,capsys,{'code':'000001','side':'매수','qty':1,'confidence':.7,
                                      'bull':'x','bear':'x','decision':'x'})
    assert result['permissionDecision']=='deny' and 'SEMANTIC' in result['permissionDecisionReason']


@pytest.mark.compact_wire
def test_compact_actual_stage_chain_and_accepted_order(monkeypatch,capsys):
    fake,_,_,_,jid=completed(monkeypatch)
    planned=M.plan_order('000001','매수',.7,judgment_id=jid)
    args={'code':'000001','side':'매수','qty':1,'confidence':.7,'bull':'b','bear':'s','decision':'test',
          'judgment_id':jid,'plan_id':planned['plan_id'],'order_attempt_id':planned['order_attempt_id']}
    hook=hook_call(monkeypatch,capsys,args)
    assert hook['permissionDecision']=='allow',hook
    result=M.place_order(**hook['updatedInput'])
    assert result['ok'],result
    assert len(fake.orders)==1
    repeated=M.place_order(**hook['updatedInput'])
    assert repeated['replayed'] and len(fake.orders)==1


def test_runner_requires_all_role_results_and_bounds_budget(monkeypatch):
    from test_judgment_integration import load_module, ROOT
    import semantic_runner as SR
    setup_run(monkeypatch)
    seen=[]
    def invoke(kind,prompt,semantic_state,semantic_role):
        seen.append((semantic_role,os.environ['RUN_ATTEMPT_ID']))
        semantic_state['cost']+=.1
        return True
    # Merely returning success without submitting a role cannot advance.
    assert not SR.run('view','test',invoke)
    assert [r for r,_ in seen]==['interpretation']
    assert 'Agent' not in SR.tools_for('bull','decide')
    assert 'mcp__kiwoom__place_order' not in SR.tools_for('lead','view')


def test_staged_cli_arguments_and_invocation_capture(monkeypatch):
    import json,time
    from test_judgment_integration import load_module, ROOT
    _,manifest=setup_run(monkeypatch)
    runner=load_module('semantic_test_runner',ROOT/'run.py')
    as_role(monkeypatch,'bull')
    state={'attempt':manifest,'budget':5,'cost':1,'deadline':time.monotonic()+60,'calls':[]}
    seen=[]
    class Process:
        returncode=0
        def __init__(self,cmd,**kwargs):
            seen.append((cmd,kwargs['env']))
        def communicate(self,timeout):
            assert timeout<=60
            return json.dumps({'result':'fixture','usage':{},'total_cost_usd':.2}),''
    monkeypatch.setattr(runner.subprocess,'Popen',Process)
    assert runner.claude('view','fixture',semantic_state=state,semantic_role='bull')
    assert state['cost']==1.2
    cmd,env=seen[0]
    assert cmd[cmd.index('--max-budget-usd')+1]=='4'
    assert env['RUN_ATTEMPT_ID']==manifest['run_attempt_id']
    assert env['RUN_ID']==manifest['run_id']
    assert env['SION_SEMANTIC_ROLE']=='bull'
    import runtime_inputs
    saved=runtime_inputs.load_attempt(manifest['run_attempt_id']+'-bull')
    assert saved['options']['semantic_role']=='bull'
    assert 'config/ontology/semantic_definitions.json' in saved['sources']


@pytest.mark.parametrize('failed_capture', ['input', 'result'])
def test_staged_cli_stops_on_missing_invocation_record(monkeypatch, failed_capture):
    import json, time
    from test_judgment_integration import load_module, ROOT
    _, manifest = setup_run(monkeypatch)
    runner = load_module('semantic_recording_failure_runner', ROOT/'run.py')
    as_role(monkeypatch, 'bull')
    state = {'attempt':manifest,'budget':5,'cost':0,'deadline':time.monotonic()+60,'calls':[]}
    called = []
    class Process:
        returncode = 0
        def __init__(self, *args, **kwargs): called.append(True)
        def communicate(self, timeout):
            return json.dumps({'result':'fixture','usage':{},'total_cost_usd':.2}), ''
    monkeypatch.setattr(runner.subprocess, 'Popen', Process)
    method = 'capture_attempt' if failed_capture == 'input' else 'record_result'
    monkeypatch.setattr(runner.runtime_inputs, method, lambda *a, **k: {'recording_status':'recording_error'})
    assert not runner.claude('view','fixture',semantic_state=state,semantic_role='bull')
    assert len(called) == (0 if failed_capture == 'input' else 1)
    assert not state['calls'][-1]['ok']


def test_positive_interpretation_requires_two_explicit_assessments(monkeypatch):
    _,_,ctx,data=interpretation(monkeypatch)
    data['paths'][0]['direction']='support'
    data['arguments'][1]['text']='기관 매도가 일회성이라면 수급이 회복될 수 있다'
    assert S.submit(ctx['input_id'],data)['status']=='accepted'
    for role in ('bull','bear'):
        as_role(monkeypatch,role);ctx=S.context('000001','매수')
        value=evaluation(role,agree='agree');value['stance']='support'
        assert S.submit(ctx['input_id'],{'evaluations':[value],'objections':[]})['status']=='accepted'
    as_role(monkeypatch,'lead');ctx=S.context('000001','매수')
    final={'confidence_band_id':'strong','confidence_raw':.81,'recommended_action':'매수',
           'decision':'조건부 매수 해석에 양측 동의. 강도 평가는 AI 판단으로 남는다.',
           'adopted_claim_ids':['h1'],'salient_risk_path_ids':[],
           'dispositions':[{'evaluation_id':r+'-eval','state':'adopted','reason':'방향 해석 동의'} for r in ('bull','bear')]}
    result=S.submit(ctx['input_id'],final)
    assert result['status']=='accepted',result
    assert J.load_judgment(result['judgment_id'])['semantic']['joint_buy_interpretation_paths']==['p1']


def test_cyclic_interpretation_dependencies_rejected(monkeypatch):
    _,_,ctx,data=interpretation(monkeypatch)
    data['arguments'][1]['claim_ids']=['h2']
    data['arguments'].append({'id':'h2','kind':'hypothesis','text':'circular','claim_ids':['h1'],'stance':'oppose'})
    assert S.submit(ctx['input_id'],data)['error_code']=='SEMANTIC_CYCLIC_CLAIM_DEPENDENCY'


def test_historical_lookup_is_read_only_and_projection_uses_same_chain(monkeypatch):
    _,manifest,ctx,data,jid=completed(monkeypatch)
    before={str(p):p.stat().st_mtime_ns for p in C.ontology_root().rglob('*.json')}
    historical=S.stored_trace(jid)
    assert historical['stages']['lead']['judgment_id']==jid
    assert {str(p):p.stat().st_mtime_ns for p in C.ontology_root().rglob('*.json')}==before
    import trace_query as Q
    projection=Q.build_projection()
    candidates=[t for t in projection['traces'].values() if t.get('target',{}).get('run_attempt_id')==manifest['run_attempt_id']]
    assert candidates and candidates[0]['semantic_execution']['stages']['lead']['judgment_id']==jid


def test_delivery_payload_integrity_prevents_silently_dropped_bear(monkeypatch):
    _,_,ctx,data=through_bear(monkeypatch)
    original=S._read
    def altered(kind,identifier,attempt=None):
        value=original(kind,identifier,attempt)
        if kind=='inputs' and identifier==ctx['input_id']:
            value=copy.deepcopy(value)
            value['payload']['predecessors']=[p for p in value['payload']['predecessors'] if p['role']!='bear']
        return value
    monkeypatch.setattr(S,'_read',altered)
    assert S.submit(ctx['input_id'],data)['error_code']=='SEMANTIC_INPUT_INTEGRITY'


def test_no_free_order_plan_and_rule_exit_cannot_bypass_judgment(monkeypatch):
    fake,_,_,_,jid=completed(monkeypatch)
    import execution_trace as E
    token=E.reserve_attempt(E.account_scope())['order_attempt_id']
    result=M.place_order('000001','매수',1,.7,'b','s','d',judgment_id=jid,order_attempt_id=token)
    assert result['error_code']=='SEMANTIC_PLAN_LINK_REQUIRED',result
    as_role(monkeypatch,'lead')
    result=M.plan_order('000001','매도',1,rule_exit=True)
    assert 'SEMANTIC_PROTECTIVE_TRIGGER_REQUIRED' in result['error']
    assert not fake.orders
