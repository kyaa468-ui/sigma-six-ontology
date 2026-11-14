"""Frozen experiments exercise the real fact checker; no provider/network calls."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import json
from pathlib import Path
import pytest
from test_ontology import C, M, isolated, clone
from test_judgment_integration import setup_run, submission
import prompt_validation as P
import validation_store as S


def frozen(monkeypatch):
    _, manifest=setup_run(monkeypatch)
    text=M.get_briefing(); item=submission(text)
    receipt=read_stored(Path(C.WORK)/'ontology/deliveries'/f"{item['delivery_ids'][0]}.json")
    request={'target':{'code':'000001','confidence_for':'매수','run_attempt_id':manifest['run_attempt_id'],'run_id':receipt['ai_run_id']},
             'as_of':C.now().isoformat(),'receipts':[receipt]}
    return request,item


def test_frozen_fact_checker_blocks_unseen_wrong_sign_and_live_lookup(monkeypatch):
    request,item=frozen(monkeypatch)
    tool=P.FrozenTools(request)
    assert tool.call('record_ai_judgments',{'items':[item]})[0]['status']=='rejected'
    tool.call('get_briefing')
    bad=clone(item);bad['arguments'][0]['assertion']['direction']='buy';bad['request_key']='wrong-sign'
    assert tool.call('record_ai_judgments',{'items':[bad]})[0]['errors'][0]['code']=='FACT_MISMATCH'
    item['request_key']='corrected'
    assert tool.call('record_ai_judgments',{'items':[item]})[0]['status']=='accepted'
    assert tool.call('place_order',{})['error_code']=='FROZEN_TOOL_FORBIDDEN'
    assert tool.call('get_account')['status']=='unavailable'
    assert tool.call('get_briefing')['status']=='unavailable'


def setup_pair(monkeypatch):
    request,item=frozen(monkeypatch)
    import validation_spec as V
    import validation_dataset as D
    spec={'spec':{'comparison_kind':'prompt_pair','prompt':{'model':'fake-model','budget_usd':1},'repetitions':2},
          'proposal_snapshot':{'effect':{'operation':'prompt_guidance','text':'기관 매도도 확인'},'condition':{'value':True,'unit':'bool'}}}
    inputs={'opportunities':[{**request['target'],'at':request['as_of']}], 'deliveries':request['receipts'],
            'runtime_inputs':{request['target']['run_attempt_id']:{'status':'recorded','snapshot':{'cmd_prompt':'고정 원래 프롬프트','sources':{}}}}}
    monkeypatch.setattr(V,'load_spec',lambda _:spec)
    monkeypatch.setattr(D,'get_inputs',lambda _:inputs)
    monkeypatch.setattr(D,'ensure_inputs',lambda _:inputs)
    return item,inputs,spec


def test_paired_run_keeps_all_repetitions_and_arm_failures(monkeypatch):
    item,_,_=setup_pair(monkeypatch)
    observed=[]
    def fake(req,tools):
        observed.append(req)
        tools.call('get_briefing')
        if req['arm']=='candidate' and req['repetition']==1: raise TimeoutError('local fake')
        tools.call('record_ai_judgments',{'items':[item]})
        return {'returncode':0,'output':'fixed local fake'}
    result=P.run_prompt_validation('v1',model='fake-model',repetitions=2,budget_usd=1,provider=fake)
    assert result['status']=='incomplete'
    assert [x['status'] for x in result['pairs']]==['paired','incomplete']
    assert len(result['trial_ids'])==4 and result['market_sample_count']==1
    assert sum(x['budget_usd'] for x in observed)==1
    assert 'EXPERIMENTAL GUIDANCE' not in observed[0]['prompt']
    assert 'EXPERIMENTAL GUIDANCE' in observed[1]['prompt']
    assert result['adoption_eligibility'] is False
    assert len(S.records('prompt_trials')['records'])==4


def test_actual_ai_requires_separate_explicit_flag_and_fixed_settings(monkeypatch):
    setup_pair(monkeypatch)
    with pytest.raises(ValueError,match='EXPLICIT_WITH_AI'):
        P.run_prompt_validation('v1',model='fake-model',repetitions=2,budget_usd=1)
    with pytest.raises(ValueError,match='DIFFER_FROM_REGISTRATION'):
        P.run_prompt_validation('v1',model='other',repetitions=2,budget_usd=1,provider=lambda *a:None)
    assert not S.records('prompt_experiments')['records']


def test_legacy_prompt_comparison_does_not_claim_staged_execution(monkeypatch):
    _,inputs,_=setup_pair(monkeypatch)
    for row in inputs['runtime_inputs'].values():
        row['snapshot']['options']={'semantic_stages':['interpretation','bull','bear','lead']}
    def forbidden(*args):
        raise AssertionError('A single-call replay cannot represent the staged pipeline')
    result=P.run_prompt_validation('v1',model='fake-model',repetitions=2,budget_usd=1,provider=forbidden)
    assert not result['trial_ids']
    assert any(g['reason']=='staged_semantic_replay_not_supported_by_legacy_prompt_comparison' for g in result['gaps'])


def test_unknown_condition_does_not_invent_prompt_effect(monkeypatch):
    item,_,spec=setup_pair(monkeypatch)
    spec['proposal_snapshot']['condition']={'op':'gt','left':{'field':'features.gap'},'right':{'value':0,'unit':'ratio'}}
    seen=[]
    def fake(req,tools):
        seen.append(req);tools.call('get_briefing');tools.call('record_ai_judgments',{'items':[item]});return {}
    result=P.run_prompt_validation('v1',model='fake-model',repetitions=2,budget_usd=1,provider=fake)
    assert all(x['condition_result'] is None and not x['new_guidance_applied'] for x in seen)
    assert result['status']=='complete'


def test_missing_original_cutoff_or_prompt_is_gap_not_current_fallback(monkeypatch):
    _,inputs,_=setup_pair(monkeypatch)
    inputs['opportunities'][0].pop('at')
    def forbidden(*args):raise AssertionError('must not call')
    result=P.run_prompt_validation('v1',model='fake-model',repetitions=2,budget_usd=1,provider=forbidden)
    assert result['status']=='incomplete' and result['gaps'][0]['reason']=='historical_decision_cutoff_unavailable'


def test_post_judgment_adoption_condition_forbidden_before_prompt(monkeypatch):
    _,_,spec=setup_pair(monkeypatch)
    spec['proposal_snapshot']['condition']={'field':'adoption.explicitly_uses_above_ma20_and_foreign_buy'}
    with pytest.raises(ValueError,match='FIELD_UNAVAILABLE_AT_STAGE'):
        P.run_prompt_validation('v1',model='fake-model',repetitions=2,budget_usd=1,provider=lambda *a:None)


def test_cli_provider_uses_external_directory_and_only_frozen_tools(monkeypatch):
    request,item=frozen(monkeypatch)
    request.update(trial_id='trial-isolated-cli',model='fake-model',budget_usd=.25,prompt='fixed')
    observed={}
    class Process:
        returncode=0
        def communicate(self,timeout):return ('{"type":"result","result":"fake"}','')
    def popen(cmd,**kwargs):
        observed.update(cmd=cmd,**kwargs)
        folder=Path(kwargs['cwd'])
        assert not folder.is_relative_to(Path(C.AGENT))
        assert kwargs['env']['RUN_MODE']=='prompt_validation'
        config=read_stored(Path(cmd[cmd.index('--mcp-config')+1]))
        assert list(config['mcpServers'])==['frozen']
        assert config['mcpServers']['frozen']['args'][0].endswith('validation_mcp.py')
        settings=read_stored(Path(cmd[cmd.index('--settings')+1]))
        assert settings['autoMemoryEnabled'] is False and 'Read' in settings['permissions']['deny']
        assert cmd[cmd.index('--setting-sources')+1]==''
        return Process()
    monkeypatch.setattr(P.subprocess,'Popen',popen)
    result=P._cli_provider(request,P.FrozenTools(request))
    assert result['returncode']==0
    assert not Path(observed['cwd']).exists()


def test_repeat_run_cannot_silently_spend_registered_budget_again(monkeypatch):
    item,_,_=setup_pair(monkeypatch)
    def fake(req,tools):
        tools.call('get_briefing');tools.call('record_ai_judgments',{'items':[item]});return {}
    P.run_prompt_validation('v1',model='fake-model',repetitions=2,budget_usd=1,provider=fake)
    with pytest.raises(ValueError,match='ALREADY_STARTED'):
        P.run_prompt_validation('v1',model='fake-model',repetitions=2,budget_usd=1,provider=fake)


def test_real_provider_orchestrates_frozen_bull_bear_and_total_with_shared_budget(monkeypatch):
    request,item=frozen(monkeypatch)
    request.update(trial_id='trial-multi',model='fixed-model',budget_usd=4,prompt='total',agents={'bull':'original bull','bear':'original bear'})
    seen=[]
    def stage(req,tools):
        seen.append(req)
        if req['agent_role']=='coordinator':
            tools.call('get_briefing');tools.call('record_ai_judgments',{'items':[item]})
        else:
            assert tools.call('record_ai_judgments',{'items':[item]})['error_code']=='FROZEN_TOOL_FORBIDDEN'
        return {'returncode':0,'provider_result_status':'parsed','stdout':req['agent_role'],'provider_result':{'result':req['agent_role']}}
    monkeypatch.setattr(P,'_cli_stage',stage)
    tools=P.FrozenTools(request);result=P._cli_provider(request,tools)
    assert [x['agent_role'] for x in seen]==['bull','bear','coordinator']
    assert [x['budget_usd'] for x in seen]==[1,1,2]
    assert all(x['model']=='fixed-model' for x in seen)
    assert 'original bull' in seen[0]['prompt'] and 'bull' in seen[1]['prompt']
    assert len(result['subagent_outputs'])==3 and len(tools.submissions)==1


def test_model_output_parse_failure_retains_original_stdout(monkeypatch):
    request,_=frozen(monkeypatch)
    request.update(trial_id='trial-bad-json',model='fixed-model',budget_usd=1,prompt='fixed')
    class Process:
        returncode=0
        def communicate(self,timeout):return ('{bad model output','stderr retained')
    monkeypatch.setattr(P.subprocess,'Popen',lambda *a,**k:Process())
    result=P._cli_stage(request,P.FrozenTools(request))
    assert result['provider_result_status']=='invalid_output'
    assert result['stdout']=='{bad model output' and result['stderr']=='stderr retained'


def test_replacing_prior_adopted_guidance_gets_new_receipt_preserves_facts(monkeypatch):
    import ontology as O
    import lesson_registry as L
    request,_=frozen(monkeypatch)
    receipt=request['receipts'][0]
    original_catalog=O.render_catalog(receipt['evidence_catalog'])
    original_id=receipt['delivery_id']
    lesson={'code':'000001','text':'old guidance'}
    monkeypatch.setattr(L,'render_for_briefing',lambda _:['','old adopted guidance block'])
    receipt['context']['adopted_lessons']=[lesson]
    receipt['evidence_catalog'].append({'evidence_id':original_id+':lesson','subject':'000001','kind':'adopted_lesson','value':'old guidance'})
    receipt['text']=receipt['text'].replace(original_catalog,'\nold adopted guidance block'+O.render_catalog(receipt['evidence_catalog']))
    before=clone(receipt)
    transformed=P.experimental_receipts([receipt],'candidate')[0]
    assert receipt==before and transformed['original_delivery_id']==original_id
    assert transformed['delivery_id']!=original_id
    assert 'old adopted guidance block' not in transformed['text']
    assert not any(e.get('kind')=='adopted_lesson' for e in transformed['evidence_catalog'])
    assert [e.get('value') for e in transformed['evidence_catalog']]==[e.get('value') for e in before['evidence_catalog'][:-1]]
    assert P.experimental_receipts([receipt],'baseline')==[receipt]
