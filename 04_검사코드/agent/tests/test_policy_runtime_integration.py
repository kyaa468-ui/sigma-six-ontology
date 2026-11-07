"""Step 1–7 production entry points under local broker/temp state only."""
import json
from pathlib import Path
import pytest
from test_ontology import C, M, B, isolated, clone
from test_execution_integration import planned, hook_call
import execution_trace as E
import policy_registry as PR


@pytest.fixture(autouse=True)
def isolated_policy_authority(monkeypatch,tmp_path,isolated):
    import tuning
    path=str(tmp_path/'policy-params.json')
    monkeypatch.setattr(tuning,'PARAMS_PATH',path)
    C.write_json(path,C.params())


def test_plan_pins_revision_and_new_policy_blocks_before_broker(monkeypatch,capsys):
    fake,_,plan,args=planned(monkeypatch)
    saved=E.load_plan(plan['plan_id'])
    assert saved['inputs']['policy']['revision']==PR.snapshot()['revision']
    p=C.params();p['decision']['buy_min_confidence']+=.01
    monkeypatch.setattr(C,'params',lambda:clone(p))
    C.write_json(M.tuning.PARAMS_PATH,p)
    denied=hook_call(monkeypatch,capsys,args)
    assert denied['permissionDecision']=='deny'
    out=M.place_order(**args)
    assert out['ok'] is False and out['error']=='STALE_POLICY_PLAN'
    assert fake.orders==[]


def test_transmitted_retry_is_once_despite_policy_revision_change(monkeypatch):
    fake,_,_,args=planned(monkeypatch)
    first=M.place_order(**args)
    assert first['ok'] is True and len(fake.orders)==1
    p=C.params();p['decision']['buy_min_confidence']+=.01
    monkeypatch.setattr(C,'params',lambda:clone(p))
    C.write_json(M.tuning.PARAMS_PATH,p)
    second=M.place_order(**args)
    assert second.get('replayed') and len(fake.orders)==1


@pytest.mark.parametrize('mode',['validation_review','prompt_validation'])
def test_validation_modes_block_before_guard_reservation(monkeypatch,capsys,mode):
    fake,_,_,args=planned(monkeypatch)
    before=sorted(str(p) for p in Path(C.WORK).rglob('*'))
    monkeypatch.setenv('RUN_MODE',mode)
    assert M.place_order(**args)['error_code']=='VALIDATION_TOOL_FORBIDDEN'
    assert hook_call(monkeypatch,capsys,args)['permissionDecision']=='deny'
    assert before==sorted(str(p) for p in Path(C.WORK).rglob('*'))
    assert not fake.orders


def test_actual_tool_io_saved_and_failure_does_not_repeat_tool(monkeypatch):
    fake,manifest,_,_=planned(monkeypatch)
    import runtime_inputs as RI
    records=Path(C.WORK)/'ontology/runtime_inputs'/manifest['run_attempt_id']/'tools'
    rows=[json.loads(p.read_text())['record'] for p in records.glob('*.json')]
    assert {'get_briefing','record_ai_judgments','plan_order'}<={r['tool'] for r in rows}
    monkeypatch.setattr(RI,'record_tool_call',lambda *a,**k:(_ for _ in ()).throw(OSError('local injected save failure')))
    result=M.record_decision('000001','관망',.7,'bull','bear','local')
    assert result=={'ok':True}


def test_validation_cli_defaults_to_local_replay_and_no_ai(monkeypatch,capsys):
    from test_judgment_integration import load_module
    from test_ontology import ROOT
    import policy_validation as V
    import prompt_validation as P
    runner=load_module('step7_runner',ROOT/'run.py')
    seen=[]
    monkeypatch.setattr(V,'run_validation',lambda identifier,mode='replay':seen.append((identifier,mode)) or {'status':'insufficient_data'})
    monkeypatch.setattr(P,'run_prompt_validation',lambda *a,**k:(_ for _ in ()).throw(AssertionError('AI must require explicit flag')))
    monkeypatch.setattr(runner.sys,'argv',['run.py','validate-policy','--validation-id','validation-local'])
    runner.main()
    assert seen==[('validation-local','replay')]
    assert json.loads(capsys.readouterr().out)['status']=='insufficient_data'
    monkeypatch.setattr(runner.sys,'argv',['run.py','validate-policy','--validation-id','validation-local','--with-ai','--no-ai'])
    with pytest.raises(SystemExit):runner.main()
    assert len(seen)==1
