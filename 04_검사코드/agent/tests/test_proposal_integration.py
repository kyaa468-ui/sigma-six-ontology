"""Real step 1–6 tool boundaries; only temporary records and fake processes."""
import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_ontology import C, M, ROOT, isolated
from test_close_evaluation import calendar, bytes_under
from test_case_review import completed_case, runner, protect_trading
from test_judgment_integration import setup_run, submission, load_module
from test_execution_integration import hook_call, planned
import case_review as CR
import learning_proposals as L
import proposal_store as S
import execution_trace as E
import runtime_inputs as RI
import judgment_coverage as JC


def ready(monkeypatch, calendar):
    fake, attempt, app = completed_case(monkeypatch, calendar)
    review=CR.build_review()
    run=L.build_context(review['review_id'])
    assert run['context_status']=='ready',run
    rid=run['proposal_run_id']
    monkeypatch.setenv('RUN_MODE','proposal_review'); monkeypatch.setenv('PROPOSAL_RUN_ID',rid)
    context=M.get_proposal_context(rid); page=M.get_proposal_cases(rid)
    assert context['recording_status']=='recorded' and page['recording_status']=='recorded'
    return fake,attempt,app,review,rid,context,page


def retain(context,page):
    metric=next(x for x in context['catalog'] if x['value'] is not None)
    return {'item_id':'keep','kind':'retain_current','title':'자료 부족으로 현행 유지',
            'delivery_ids':[context['delivery_id'],page['delivery_id']],
            'fact_claims':[{'id':'f1','reference':metric['reference'],'op':'value','value':metric['value'],'unit':metric['unit']}],
            'stratum_ids':[page['cases'][0]['stratum_id']],
            'condition':{'op':'eq','left':{'field':'stratum.instrument_kind'},'right':{'value':'equity','unit':'text'}},
            'reason':'한 사례만으로 새 운영 규칙을 채택할 수 없음','reason_kind':'insufficient_evidence',
            'validation_kind':'none','support_case_ids':[],'counter_case_ids':[]}


def test_actual_six_stage_tools_preserve_trading_and_export(monkeypatch,calendar):
    fake,attempt,app,review,rid,ctx,page=ready(monkeypatch,calendar)
    protected=protect_trading()
    before=JC.get_coverage(attempt['run_attempt_id'])
    result=M.record_learning_proposals(rid,'keep-request',[retain(ctx,page)])
    assert result['items'][0]['status']=='accepted',result
    queried=M.get_learning_proposals(rid)
    assert queried['submission_status']=='accepted'
    proposal=queried['proposals'][0]
    bundle=L.export_validation_bundle(proposal['proposal_id'])
    assert bundle['proposal']['active'] is False and bundle['stage7_completed'] is False
    assert bundle['cases'][0]['application_id']==app['application_id']
    assert bundle['cases'][0]['origin_judgment_id']==app['judgment_id']
    assert bundle['cases'][0]['evaluation_id']
    assert len(fake.orders)==1 and JC.get_coverage(attempt['run_attempt_id'])==before
    # The protected snapshot includes proposal records; exclude their own writes.
    assert all(p.read_bytes()==data for p,data in protected.items())
    monkeypatch.setenv('RUN_MODE','decide')
    briefing=M.get_briefing()
    assert '자료 부족으로 현행 유지' not in briefing
    assert not Path(M.tuning.LESSONS).exists()


@pytest.mark.parametrize('mode',['decide','view','exit','tune','review','case_review',''])
@pytest.mark.parametrize('name,args',[
    ('get_proposal_context',()),('get_proposal_cases',()),
    ('record_learning_proposals',('request',[])),('get_learning_proposals',())])
def test_new_tools_are_not_a_trading_or_tuning_read_path(monkeypatch,mode,name,args):
    monkeypatch.setenv('RUN_MODE',mode);monkeypatch.setenv('PROPOSAL_RUN_ID','p1')
    before=bytes_under(Path(C.WORK))
    result=getattr(M,name)('p1',*args)
    assert not result.get('ok',False) and result['error_code'] in ('PROPOSAL_MODE_REQUIRED','CASE_REVIEW_TOOL_FORBIDDEN')
    assert bytes_under(Path(C.WORK))==before


def test_proposal_mode_denies_all_old_tools_before_side_effects(monkeypatch):
    monkeypatch.setenv('RUN_MODE','proposal_review');monkeypatch.setenv('PROPOSAL_RUN_ID','p1')
    calls=[lambda:M.get_briefing(),lambda:M.get_account(),lambda:M.set_regime('상승','test'),
           lambda:M.get_tuning_context(),lambda:M.apply_tuning([], 'test', ['must not appear']),
           lambda:M.submit_review(1,'test','must not enter next tune','test'),
           lambda:M.plan_order('000001','매수',.7),
           lambda:M.place_order('000001','매수',1,.7,'','',''),
           lambda:M.get_case_review('review-other')]
    before=bytes_under(Path(C.WORK))
    for call in calls:
        assert call()['error_code']=='PROPOSAL_TOOL_FORBIDDEN'
    assert bytes_under(Path(C.WORK))==before


def test_wrong_proposal_scope_has_no_storage_or_broker_call(monkeypatch):
    monkeypatch.setenv('RUN_MODE','proposal_review');monkeypatch.setenv('PROPOSAL_RUN_ID','p1')
    before=bytes_under(Path(C.WORK))
    assert M.get_proposal_context('p2')['error_code']=='PROPOSAL_SCOPE_MISMATCH'
    assert bytes_under(Path(C.WORK))==before


def test_proposal_hook_denies_before_order_reservation(monkeypatch,capsys):
    monkeypatch.setenv('RUN_MODE','proposal_review')
    before=bytes_under(Path(C.WORK))
    result=hook_call(monkeypatch,capsys,{'code':'000001','side':'매수','qty':1,'confidence':.7})
    assert result['permissionDecision']=='deny'
    assert bytes_under(Path(C.WORK))==before


@pytest.mark.parametrize('terminal',['normal','error','timeout'])
def test_proposal_runner_records_omission_and_failure(monkeypatch,calendar,terminal):
    *_,rid,ctx,page=ready(monkeypatch,calendar)
    run=runner(); captured={}
    class Fake:
        pid=999999
        returncode=1 if terminal=='error' else 0
        def __init__(self,cmd,**kw):captured.update(cmd=cmd,**kw)
        def communicate(self,timeout):
            if terminal=='timeout' and timeout>30:raise subprocess.TimeoutExpired('fake',timeout)
            return '{}',''
    monkeypatch.setattr(run.subprocess,'Popen',Fake)
    monkeypatch.setattr(run.os,'killpg',lambda *a:None)
    result=run.proposal_ai(rid)
    assert result['submission_status']=='not_submitted'
    assert result['ai_execution_status']==terminal
    cmd=captured['cmd'];assert cmd[cmd.index('--tools')+1]==''
    assert cmd[cmd.index('--allowedTools')+1:cmd.index('--disallowedTools')]==run.PROPOSAL_TOOLS
    assert captured['env']['RUN_MODE']=='proposal_review'
    before=bytes_under(Path(C.WORK))
    run.proposal_ai(rid)
    assert bytes_under(Path(C.WORK))==before


def test_runner_dry_no_ai_and_missing_source_do_not_invoke_model(monkeypatch,calendar):
    fake,attempt,app=completed_case(monkeypatch,calendar)
    review=CR.build_review();run=runner()
    monkeypatch.setattr(run,'proposal_ai',lambda *_:pytest.fail('AI not requested'))
    before=bytes_under(Path(C.WORK));run.run_proposals(review['review_id'],dry=True)
    assert bytes_under(Path(C.WORK))==before
    result=run.run_proposals(review['review_id'],no_ai=True)
    assert result['run_status']=='analysis_only' and result['submission_status']=='not_submitted'
    before=bytes_under(Path(C.WORK))
    run.run_proposals(review['review_id'],no_ai=True)
    assert bytes_under(Path(C.WORK))==before
    assert run.run_proposals('missing-review')['run_status']=='source_not_ready'


def test_proposal_ai_lock_contention_is_not_a_failed_job(monkeypatch,calendar):
    *_,rid,ctx,page=ready(monkeypatch,calendar)
    run=runner()
    with S.locked('ai-'+rid):
        with ThreadPoolExecutor(1) as pool:
            result=pool.submit(run.proposal_ai,rid).result(timeout=5)
    assert result['worker_status']=='busy'
    assert not S.records('errors')['records']


def test_prompt_inputs_and_outputs_survive_same_minute_reruns(monkeypatch):
    a=RI.capture_attempt('attempt-one','decide','first',['get_briefing'])
    b=RI.capture_attempt('attempt-two','decide','second',['get_briefing'])
    assert a['recording_status']==b['recording_status']=='recorded'
    assert RI.load_attempt('attempt-one')['cmd_prompt']=='first'
    assert RI.load_attempt('attempt-two')['cmd_prompt']=='second'
    assert 'CLAUDE.md' in RI.load_attempt('attempt-one')['sources']
    assert RI.load_attempt('attempt-one')['tool_contracts']
    assert RI.capture_attempt('attempt-one','decide','changed',[])['recording_status']=='recording_error'
    assert RI.record_result('attempt-one','normal',{'text':'one'})['recording_status']=='recorded'
    assert RI.record_result('attempt-two','normal',{'text':'two'})['recording_status']=='recorded'
    assert len(list((Path(C.WORK)/'ontology/runtime_inputs').glob('*.result.json')))==2


def test_direct_order_and_hook_definitions_are_bound_without_changing_order(monkeypatch,capsys):
    fake,manifest,plan,args=planned(monkeypatch)
    jid=args['judgment_id']
    hook_call(monkeypatch,capsys,args)
    result=M.place_order(**args)
    assert result['ok']
    events=E.get_execution_trace(judgment_id=jid)['events']
    risk=[e for e in events if e['kind']=='risk_evaluation']
    assert {e['actor'] for e in risk}=={'hook','server'}
    assert all(e['definition']['definition_id'] and e['definition']['recording_status']=='recorded' for e in risk)


@pytest.mark.parametrize('no_ai,ai_status,run_status,expected',[
    (False,'accepted','complete',1),(True,'accepted','complete',0),
    (False,'not_submitted','awaiting_ai',0),(False,'rejected','awaiting_ai',0),
    (False,'accepted','partial',0)])
def test_fifth_step_chains_only_completed_review(monkeypatch,no_ai,ai_status,run_status,expected):
    run=runner(); calls=[]
    status={'review_id':'review-1','analysis_status':'complete','expected_count':1,
            'ai_status':ai_status,'run_status':run_status,'recording_status':'recorded'}
    monkeypatch.setattr(CR,'build_review',lambda *a,**k:dict(status))
    monkeypatch.setattr(CR,'get_case_review',lambda *a:dict(status))
    monkeypatch.setattr(run,'case_review_ai',lambda *a:dict(status))
    monkeypatch.setattr(run,'run_proposals',lambda rid:calls.append(rid) or {'run_status':'complete'})
    out=run.run_case_review(no_ai=no_ai)
    assert len(calls)==expected and out['run_status']==run_status


def test_sixth_step_failure_does_not_poison_fifth_step(monkeypatch):
    run=runner()
    status={'review_id':'review-1','analysis_status':'complete','expected_count':1,
            'ai_status':'accepted','run_status':'complete','recording_status':'recorded'}
    monkeypatch.setattr(CR,'build_review',lambda *a,**k:dict(status))
    monkeypatch.setattr(run,'case_review_ai',lambda *a:dict(status))
    def fail(*a): raise OSError('local proposal disk failure')
    monkeypatch.setattr(run,'run_proposals',fail)
    assert run.run_case_review()['run_status']=='complete'
    import case_store
    assert not case_store.records('errors')['records']


def test_new_definition_record_failure_keeps_original_execution(monkeypatch):
    fake,manifest,plan,args=planned(monkeypatch)
    before=JC.get_coverage(manifest['run_attempt_id'])
    def unavailable(): raise OSError('local metadata archive unavailable')
    monkeypatch.setattr(E,'_definition_snapshot',unavailable)
    result=M.place_order(**args)
    assert result['ok'] and len(fake.orders)==1
    assert JC.get_coverage(manifest['run_attempt_id'])['counts']==before['counts']
    records=E.get_execution_trace(judgment_id=args['judgment_id'])['events']
    events=[e for e in records if e['kind']=='risk_evaluation' and e['actor']=='server']
    assert events[0]['definition']['recording_status']=='recording_error'


def test_runtime_input_failure_does_not_change_coverage(monkeypatch):
    fake,attempt=setup_run(monkeypatch)
    before=JC.get_coverage(attempt['run_attempt_id'])
    monkeypatch.setattr(RI.os,'link',lambda *a:(_ for _ in ()).throw(OSError('disk full')))
    assert RI.capture_attempt('snapshot-error','decide','prompt',[])['recording_status']=='recording_error'
    assert JC.get_coverage(attempt['run_attempt_id'])==before


def test_direct_research_ai_helpers_do_not_start_on_missing_or_corrupt_history(monkeypatch,calendar):
    *_,review,rid,ctx,page=ready(monkeypatch,calendar)
    run=runner()
    monkeypatch.setattr(run.subprocess,'Popen',lambda *a,**k:pytest.fail('must not relaunch unknown history'))
    assert run.proposal_ai('missing-proposal')['run_status']=='not_found'
    assert run.case_review_ai('missing-review')['run_status']=='not_found'
    path=S.root()/'ai_attempts'/'broken.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{broken')
    assert run.proposal_ai(rid)['recording_status']=='recording_error'
    import case_store
    path=case_store.root()/'ai_attempts'/'broken.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{broken')
    assert run.case_review_ai(review['review_id'])['recording_status']=='recording_error'
