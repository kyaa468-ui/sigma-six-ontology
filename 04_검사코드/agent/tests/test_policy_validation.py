"""Actual local engine replay, immutable result gates, and missing-input checks."""
import copy
import json
import socket
import sys
from datetime import datetime
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import common as C
import sizing
import risk
import replay_context as R
import policy_validation as V
import validation_store as S

NOW = datetime.fromisoformat('2026-11-06T10:01:00+09:00')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def forbidden(*a, **k): raise AssertionError('External access is forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    for name, filename in (('WORK',''), ('BRIEFING','briefing.json'), ('JOURNAL','journal.jsonl'),
                           ('POSITIONS','positions.json'), ('ACCOUNT','account.json'), ('MARKETS','markets.json')):
        monkeypatch.setattr(C, name, str(tmp_path / filename))
    monkeypatch.setattr(C, 'now', lambda: NOW)
    monkeypatch.setenv('RUN_ID', 'test-run'); monkeypatch.setenv('RUN_MODE','decide')
    (tmp_path/'briefing.json').write_text(json.dumps({'candidates':[{'code':'000001','rsi14':30,'news':[]}]}))
    (tmp_path/'journal.jsonl').write_text('')
    (tmp_path/'positions.json').write_text('{}')
    p = json.loads((ROOT/'config/params.json').read_text())
    r = json.loads((ROOT/'config/risk_limits.json').read_text())
    return tmp_path,p,r


def definition():
    sources = R.source_bundle(); sha = R.digest(sources)
    body = {'definition_id':'test-definition','sources':sources,'sha256':sha,
            'source_identity':{'status':'verified_loaded_source'}}
    return {'definition_id':'test-definition','sha256':sha}, {'test-definition':{'status':'recorded','definition':body}}


def event(setup, raw=.7):
    _,p,r = setup
    account = {'cash':10_000_000, 'total':10_000_000, 'holdings':[]}
    detail = C.conf_boost_details('000001','매수',raw,p,regime='상승',policy_context={})
    audit = {}
    plan = sizing.plan_buy('000001',detail['adjusted'],20_000,account,p,r,regime='상승',audit=audit)
    ref,defs = definition()
    return {'event_id':'event-one','kind':'order_plan','definition':ref,'plan':plan,
            'inputs':{'sizing':audit,'confidence':detail},'opportunity_ids':['op-one']}, defs


def proposal(amount=-.1):
    return {'kind':'conditional_adjustment', 'condition':{'op':'lt','left':{'field':'features.gap'},'right':{'value':.03,'unit':'ratio'}},
            'effect':{'operation':'add_confidence','amount':amount},'active':False}


def test_actual_buy_plan_reproduces_then_delta_before_cap(setup):
    row,defs = event(setup, .7)
    result = R.replay_event(row,defs,proposal(-.1),{'features':{'gap':.02}})
    assert result['baseline_status']=='passed', result
    assert result['effect_status']=='complete' and result['changed']
    assert result['candidate']['confidence']['raw']==.7
    assert result['candidate']['confidence']['adjusted']==.65
    assert result['candidate']['plan']['qty'] < result['baseline']['plan']['qty']
    assert result['baseline']['read_sequence']
    assert not (setup[0]/'ontology'/'execution').exists()


def test_cap_is_reapplied_from_uncapped_value(setup):
    row,defs=event(setup,.99)
    result=R.replay_event(row,defs,proposal(-.02),{'features':{'gap':.02}})
    assert result['baseline_status']=='passed',result
    assert result['baseline']['confidence']['before_cap']==1.04
    assert result['candidate']['confidence']['before_cap']==1.02
    assert result['candidate']['confidence']['adjusted']==1
    assert result['candidate']['confidence']['raw']==.99


def test_unknown_condition_keeps_original_adjustment_visible(setup):
    row,defs=event(setup)
    result=R.replay_event(row,defs,proposal(),{})
    assert result['baseline_status']=='passed',result
    assert result['candidate']['condition_status']=='unknown'
    assert result['candidate']['plan']==result['baseline']['plan']


@pytest.mark.parametrize('damage,reason',[
    ('runtime','runtime_context_missing'),('read','unused_recorded_reads'),
    ('presence','argument_presence_missing'),('source','original_engine_definition_hash_mismatch')])
def test_no_invented_historical_context(setup,damage,reason):
    row,defs=event(setup)
    if damage=='runtime':row['inputs']['sizing'].pop('runtime_context')
    if damage=='read':row['inputs']['sizing']['actual_reads'].append({'path':'extra.json','value':{}})
    if damage=='presence':row['inputs']['sizing']['runtime_context'].pop('explicit_inputs')
    if damage=='source':defs['test-definition']['definition']['sources']['common.py']+='\nraise RuntimeError("never execute")\n'
    result=R.replay_event(row,defs,proposal(),{'features':{'gap':.02}})
    assert result['baseline_status']=='incomplete'
    assert reason in result['reason']
    assert result['effect_status']=='not_run'


def test_original_result_mismatch_stops_effect(setup):
    row,defs=event(setup);row['plan']['qty']+=1
    result=R.replay_event(row,defs,proposal(),{'features':{'gap':.02}})
    assert result['baseline_status']=='failed' and result['effect_status']=='not_run'


def test_risk_replay_preserves_call_specific_reads_and_mode(setup):
    _,p,r=setup;account={'cash':10_000_000,'total':10_000_000,'holdings':[]}
    audit={};order={'code':'000001','side':'매수','qty':10,'confidence':.7}
    bad=risk.check(order,account,20_000,p,r,audit=audit)
    ref,defs=definition()
    row={'event_id':'risk-one','kind':'risk_evaluation','definition':ref,'inputs':audit,'violations':bad}
    result=R.replay_event(row,defs,proposal(-.3),{'features':{'gap':.02}})
    assert result['baseline_status']=='passed',result
    assert result['candidate']['violations']!=result['baseline']['violations']


def test_read_queue_never_falls_back_to_disk(setup):
    reads=R.FrozenReads([{'path':'/archive/a.json','value':{'x':1}}])
    assert reads.read('/virtual/a.json')=={'x':1}
    with pytest.raises(R.ReplayUnavailable,match='unrecorded_read'):reads.read(C.ACCOUNT)
    with pytest.raises(R.ReplayUnavailable,match='read_order_mismatch'):
        R.FrozenReads([{'path':'a.json','value':{}}]).read('b.json')


def install_dataset(monkeypatch,setup,missing=False):
    row,defs=event(setup)
    opportunities=[{'opportunity_id':'op-one','code':'000001','confidence_for':'매수',
        'origin_at':NOW.isoformat(),'run_attempt_id':'attempt-one','judgment_ids':['judgment-one'],
        'judgments':[{'judgment_id':'judgment-one'}],'condition_context':{'features':{'gap':.02}}}]
    if missing:opportunities.append({'opportunity_id':'op-missing','code':'000002','confidence_for':'매수','judgments':[]})
    bundle={'input_id':'frozen-input','complete':True,'recording_status':'recorded','opportunities':opportunities,
            'events':[row],'engine_definitions':defs,'gaps':[]}
    manifest={'validation_id':'test-validation','proposal_snapshot':proposal(), 'criteria_complete':False,
        'missing_criteria':['minimums.sessions'],'spec':{'mode':'retrospective','period':{'start':'2026-11-02T09:00:00+09:00','end':NOW.isoformat()}}}
    monkeypatch.setattr(V.Spec,'load_spec',lambda _:copy.deepcopy(manifest))
    monkeypatch.setattr(V.Dataset,'get_inputs',lambda _:copy.deepcopy(bundle))
    monkeypatch.setattr(V.Dataset,'ensure_inputs',lambda _:copy.deepcopy(bundle))
    return bundle


def test_replay_committed_idempotent_and_not_adoptable(setup,monkeypatch):
    install_dataset(monkeypatch,setup)
    a=V.run_validation('test-validation');b=V.run_validation('test-validation')
    assert a['baseline_status']=='passed',a
    assert a['result_id']==b['result_id'] and a['validation_result_hash']==b['validation_result_hash']
    assert a['adoption_eligibility']['status']=='ineligible'
    assert a['independent_validation']['status']=='unverified'
    assert V.get_validation_cases('test-validation')['total']==1
    assert len(S.records('results')['records'])==1


def test_unsubmitted_opportunity_remains_denominator(setup,monkeypatch):
    install_dataset(monkeypatch,setup,missing=True)
    result=V.run_validation('test-validation')
    assert result['coverage']['expected_opportunities']==2
    assert result['baseline_status']=='incomplete'
    assert result['cases'][1]['reasons']==['original_ai_judgment_missing','original_supported_call_missing']


def test_report_integrity_failure_is_not_empty_success(setup,monkeypatch):
    install_dataset(monkeypatch,setup)
    result=V.run_validation('test-validation')
    path=S.root()/'results'/f"{result['result_id']}.json"
    # Keep the old logical checksum while tampering with the hydrated envelope.
    # Compressed blob corruption is separately covered by test_snapshot_store.
    import snapshot_store
    payload=snapshot_store.load(path);payload['record']['baseline_status']='tampered'
    path.write_text(json.dumps(payload))
    assert V.get_policy_validation(result['result_id'])['status']=='recording_error'


def test_storage_error_does_not_touch_live_order_state(setup,monkeypatch):
    install_dataset(monkeypatch,setup)
    monkeypatch.setattr(S,'archive',lambda *a,**k:(_ for _ in ()).throw(OSError('disk')))
    result=V.run_validation('test-validation')
    assert result['recording_status']=='recording_error'
    assert not (setup[0]/'order_attempts.sqlite3').exists()


def test_numeric_parameter_is_global_even_outside_analysis_condition(setup):
    row,defs=event(setup,.7)
    change={**proposal(), 'target_snapshot':{'key':'decision.max_weight'},
            'effect':{'operation':'set_parameter','value':.08}}
    result=R.replay_event(row,defs,change,{'features':{'gap':.5}})
    assert result['baseline_status']=='passed',result
    assert result['candidate']['condition_status']=='global_parameter:not_matched'
    assert result['candidate']['plan']['qty']<result['baseline']['plan']['qty']


def selection_fixture(setup,kind='intraday',empty=False):
    import intraday as I
    _,p,r=setup;p=copy.deepcopy(p)
    p['screen']['intraday']['min_candidates']=2
    p['dip']['enabled']=False
    row={'code':'000010','name':'시험종목','price':20400,'close':20400,'ma5':20200,'ma20':20000,
         'ma20_slope5':.02,'vol20':.1,'trading_value_20d':100_000_000_000,'limit_up_recent':False,
         'ret5':.01,'ret20':.1,'foreign_qty':10000,'inst_qty':-1000,'foreign_net':1e9,'inst_net':-1e8,
         'value':1e10,'chg':.01,'value_rank':1}
    if empty:row['price']=row['close']=19000
    inputs=[copy.deepcopy(row)]
    trace=R.MemoryTrace()
    normal=I.screen(copy.deepcopy(inputs),p,r,.5,'상승',trace)
    prep=[{'code':'000020','name':'장전종목','score':.5,'_ontology':{'admission':{'route':'general'}}},
          {'code':'069500','name':'지수','score':0,'index':True,'_ontology':{'admission':{'route':'index'}}}]
    prior=[{'code':'000030','score':.3,'dip':True,'_ontology':{'admission':{'route':'dip'}}}]
    if normal:
        final=I.fill_up(copy.deepcopy(normal),copy.deepcopy(prep),set(),2,'replay-run')
    else:final=prior
    return {'run_id':'selection-test','kind':'intraday','created_at':NOW.isoformat(),'params':p,'risk':r,'regime':'상승',
        'runtime_context':{'at':NOW.isoformat(),'module_state':{'common._EXCH':None},'environment':{'RUN_MODE':'decide','RUN_ID':'screen'}},
        'engine_sources':R.source_bundle(('common.py','intraday.py')),'source_identity':{'status':'verified_loaded_source'},
        'observations':[{'engine_input':row}],
        'input_manifest':{'complete':True,'ordered_codes':['000010'],
            'screen_result':[{k:x.get(k) for k in ('code','score','high_vol')} for x in normal],
            'final_result':R.selection_projection(final)},
        'context':{'elapsed_fraction':.5,'pipeline_complete':True,'prep_candidates':prep,'prior_candidates':prior,'held':[]}}


@pytest.mark.parametrize('empty',[False,True])
def test_intraday_pipeline_reproduces_fill_etf_and_zero_pass_retention(setup,empty):
    selection=selection_fixture(setup,empty=empty)
    result=R.replay_selection(selection,proposal())
    assert result['baseline_status']=='passed',result
    assert result['pipeline_status']=='passed',result
    assert result['pipeline_baseline']==selection['input_manifest']['final_result']
    if empty:assert result['pipeline_baseline'][0]['code']=='000030'
    else:assert [r['admission_route'] for r in result['pipeline_baseline']]==['general','prep_fill','index']


def test_selection_missing_or_different_order_never_claims_complete(setup):
    selection=selection_fixture(setup)
    selection['input_manifest']['ordered_codes']=['not-recorded']
    result=R.replay_selection(selection)
    assert result['baseline_status']=='incomplete' and result['reason']=='screen_input_pool_incomplete'


def test_missing_pipeline_is_distinct_from_screen_success(setup):
    selection=selection_fixture(setup);selection['context']['pipeline_complete']=False
    result=R.replay_selection(selection)
    assert result['baseline_status']=='passed' and result['pipeline_status']=='incomplete'


def test_active_baseline_policy_reproduces_and_new_package_replaces(setup):
    import policy_registry as PR
    row,defs=event(setup,.7);_,p,r=setup
    old={**proposal(.1),'scope':{'codes':['*'],'confidence_for':['매수','매도']}}
    old['effect'].update(unit='confidence',placement='after_legacy_boost_before_cap')
    context={'code':'000001','side':'매수','confidence_raw':.7,'status':'recorded','inputs':{'features':{'gap':.02}},
        'policy':{'status':'active','policy_revision':'revision-old','params':p,'package':old}}
    with PR.use_context(context):
        detail=C.conf_boost_details('000001','매수',.7,p,regime='상승')
        audit={};plan=sizing.plan_buy('000001',detail['adjusted'],20_000,row['inputs']['sizing']['inputs']['account'],p,r,regime='상승',audit=audit)
    row.update(plan=plan,inputs={'sizing':audit,'confidence':detail,'policy_context':context})
    body=defs['test-definition']['definition'];body['sources'].update(R.source_bundle(('policy_registry.py','condition_contract.py')))
    body['sha256']=R.digest(body['sources']);row['definition']['sha256']=body['sha256']
    body['policy_source_identity']={'status':'verified_loaded_source'}
    body['source_identity']['policy_interpreter']=body['policy_source_identity']
    result=R.replay_event(row,defs,proposal(-.1),{'features':{'gap':.02}})
    assert result['baseline_status']=='passed',result
    assert result['baseline']['confidence']['adjusted']==.85
    assert result['candidate']['confidence']['adjusted']==.65  # old +.10 is replaced, not stacked
    row['inputs'].pop('policy_context')
    assert R.replay_event(row,defs)['reason']=='active_policy_context_missing'


def test_operator_market_import_never_proves_adoption(setup,monkeypatch):
    bundle=install_dataset(monkeypatch,setup)
    bundle['simulation_inputs']={'historical_market_availability_verified':False}
    manifest=V.Spec.load_spec('test-validation')
    monkeypatch.setattr(V,'_independence',lambda *a:{'status':'verified'})
    result=V._report('test-validation',manifest,bundle,'replay','r')
    assert not result['adoption_eligibility']['gates']['historical_market_availability_verified']
    assert 'historical_market_availability_verified' in result['adoption_eligibility']['reasons']


def test_prompt_finalizer_rechecks_pairs_and_keeps_fake_run_ineligible(setup,monkeypatch):
    import prompt_validation as Prompt
    bundle=install_dataset(monkeypatch,setup)
    manifest=V.Spec.load_spec('test-validation')
    manifest['proposal_snapshot']={**proposal(),'effect':{'operation':'prompt_guidance','text':'시험 설명'}}
    manifest['spec'].update(comparison_kind='prompt_pair',repetitions=1,prompt={'model':'fake','budget_usd':1})
    monkeypatch.setattr(V.Spec,'load_spec',lambda _:copy.deepcopy(manifest))
    monkeypatch.setattr(V.Dataset,'get_inputs',lambda *a:copy.deepcopy(bundle))
    target=bundle['opportunities'][0];trials=[]
    for index,arm in enumerate(('baseline','candidate')):
        tid='prompt-'+arm
        request={'experiment_id':'experiment-one','model':'fake','arm':arm,'repetition':0,'target':target}
        saved={'trial_id':tid,'status':'accepted','arm':arm,'repetition':0,'target':target,
               'judgments':[{'confidence_raw':.7-index*.1,'recommended_action':'매수'}]}
        S.archive('prompt_requests',tid,request);S.archive('prompt_trials',tid,saved);trials.append(saved)
    experiment={'experiment_id':'experiment-one','validation_id':'test-validation','input_id':'frozen-input',
        'input_hash':S.digest(bundle),'spec_hash':S.digest(manifest),'proposal_hash':S.digest(manifest['proposal_snapshot']),
        'model':'fake','mode':'local_fake','repetitions':1,'budget_usd':1,'status':'complete',
        'pairs':[{'trial_ids':[t['trial_id'] for t in trials],'target':target,'repetition':0,'status':'paired'}],
        'trial_ids':[t['trial_id'] for t in trials], 'simulations':Prompt.simulate_trials(bundle,manifest['spec'],trials,1)}
    S.archive('prompt_results','experiment-one',experiment)
    S.archive('prompt_result_commits','experiment-one',{'result_hash':S.digest(experiment)})
    result=V.finalize_prompt_validation('test-validation','experiment-one')
    assert result['baseline_status']=='passed',result
    assert result['effect_status']=='complete' and result['cases'][0]['changed']
    assert result['performance']['status']=='incomplete'
    assert result['adoption_eligibility']['status']=='ineligible'
    assert 'explicit_real_ai' in result['adoption_eligibility']['reasons']
    assert V.finalize_prompt_validation('test-validation','experiment-one')['result_id']==result['result_id']


def test_real_unpatched_candidate_source_and_main_alias_are_verified(tmp_path):
    import os
    import subprocess
    script="""
import socket,sys
def forbidden(*a,**k): raise AssertionError('network forbidden')
socket.socket.connect=forbidden
sys.path.insert(0,sys.argv[1])
from pathlib import Path
import common,execution_trace,intraday
path=Path(intraday.__file__)
assert execution_trace._runtime_source('intraday',path)[1]['status']=='verified_loaded_source'
assert execution_trace._runtime_source('common',Path(common.__file__))[1]['status']=='verified_loaded_source'
common._EXCH={'000001':'KOSPI'}
assert execution_trace._runtime_source('common',Path(common.__file__))[1]['status']=='verified_loaded_source'
from audit_inputs import runtime_context
from replay_context import Engine
runtime=runtime_context();runtime['environment'].update(RUN_ID='warm-cache',RUN_MODE='decide')
common._EXCH={'000001':'KOSDAQ'}
engine=Engine(runtime,[],snapshots={'markets.json':{}},strict=False)
assert engine.common.market_of('000001',p={'inverse':{},'index':{}})=='KOSPI'
assert runtime['module_state']['common._EXCH']=={'000001':'KOSPI'}
sys.modules['__main__']=intraday
del sys.modules['intraday']
assert execution_trace._runtime_source('intraday',path)[1]['status']=='verified_loaded_source'
intraday.screen=lambda *a: []
assert execution_trace._runtime_source('intraday',path)[1]['status']=='unverified_loaded_source'
"""
    result=subprocess.run([sys.executable,'-c',script,str(ROOT/'src')],cwd=tmp_path,
                          env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
    assert result.returncode==0,result.stderr
