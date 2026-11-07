"""Real tool/Hook paths with temporary policy authority and a local broker."""
import json

import pytest

from test_ontology import C, M, P, clone, isolated
from test_policy_adoption import authority, activate
from test_execution_integration import planned, hook_call
from test_judgment_integration import setup_run
import execution_trace as E
import judgment_coverage as JC
import policy_registry as PR
import tuning as T

REAL_PARAMS = C.params


@pytest.fixture
def runtime_authority(monkeypatch,authority):
    # Unlike most older tests, use the real pinned params reader here. Both its
    # config path and the tuning/activation authority point into the temp tree.
    monkeypatch.setattr(C,'params',REAL_PARAMS)
    return authority


def activated(monkeypatch):
    return activate(monkeypatch,effect={'operation':'add_confidence','amount':.05,'unit':'confidence',
                                       'placement':'after_legacy_boost_before_cap'})


@pytest.mark.parametrize('active',[False,True])
def test_params_change_after_risk_before_dispatch_cannot_send(monkeypatch,runtime_authority,active):
    if active: activated(monkeypatch)
    fake,_,plan,args=planned(monkeypatch)
    original=M.risk.check; checks=[]
    def check_then_tune(*a,**kw):
        errors=original(*a,**kw)
        assert not errors
        checks.append(clone(kw['audit']))
        T.write_params([{'key':'decision.buy_min_confidence','to':P['decision']['buy_min_confidence']+.01}])
        return errors
    monkeypatch.setattr(M.risk,'check',check_then_tune)
    result=M.place_order(**args)
    assert result['error']=='STALE_POLICY_PLAN' and not result['ok']
    assert checks and fake.orders==[]
    payload=M.execution_payload(args['code'],args['side'],args['qty'],args['confidence'],
                                judgment_id=args['judgment_id'],plan_id=args['plan_id'])
    probe=E.probe_attempt(E.account_scope(),args['order_attempt_id'],payload)
    assert probe.get('status') not in ('claimed','complete')
    if active:
        assert checks[0]['confidence']['adjustments'][-1]['applied'] is True
        assert PR.snapshot()['status']=='suspended'


def test_active_policy_briefing_judgment_plan_hook_order_watch_share_context(monkeypatch,capsys,runtime_authority):
    prepared,activation=activated(monkeypatch)
    fake,manifest,plan,args=planned(monkeypatch)
    saved=E.load_plan(plan['plan_id'])
    ctx=saved['inputs']['policy_context']
    assert ctx['status']=='recorded' and ctx['observation_id']
    assert ctx['policy']['status']=='active'
    assert ctx['policy']['revision']==activation['revision']
    assert ctx['inputs']['features']['gap']>0
    expected=saved['inputs']['confidence']
    assert expected['raw']==.7 and expected['adjustments'][-1]['amount']==.05
    assert expected['adjustments'][-1]['applied']
    output=hook_call(monkeypatch,capsys,args)
    assert output['permissionDecision']=='allow'
    result=M.place_order(**output['updatedInput'])
    assert result['ok'] and len(fake.orders)==1
    assert M.record_decision('000001','관망',.7,'bull','bear','local',
                            judgment_id=args['judgment_id'],confidence_for='매수',plan_id=plan['plan_id'])=={'ok':True}
    coverage=JC.get_coverage(manifest['run_attempt_id'])
    assert coverage['counts']['linked']==1
    calls={call['stage']:call['results'][0]['boost'] for call in coverage['targets'][0]['calls']}
    details=[calls['plan_order']['plan'],calls['hook']['confidence'],
             calls['place_order']['risk']['confidence'],calls['place_order']['order_logging'],
             calls['record_decision']['decision']]
    for detail in details:
        assert detail['raw']==.7 and detail['adjusted']==expected['adjusted']
        assert detail['before_cap']==expected['before_cap']
        assert detail['policy']['revision']==activation['revision']
        assert detail['adjustments'][-1]['amount']==.05 and detail['adjustments'][-1]['applied']
    for stage in ('plan_order','place_order','record_decision'):
        assert calls[stage]['policy_context']['observation_id']==ctx['observation_id']
    assert calls['hook']['policy_context']['observation_id']==ctx['observation_id']
    assert C.read_jsonl(C.JOURNAL)[-1]['confidence']==.7


@pytest.mark.parametrize('mode,rule_exit',[('decide',True),('exit',False)])
def test_active_policy_does_not_require_plan_for_existing_rule_exit(monkeypatch,capsys,runtime_authority,mode,rule_exit):
    activated(monkeypatch)
    fake,manifest=setup_run(monkeypatch,held=True)
    monkeypatch.setenv('RUN_MODE',mode)
    token=E.reserve_attempt(E.account_scope())['order_attempt_id']
    args={'code':'000001','side':'매도','qty':30,'confidence':.7,'bull':'local','bear':'local','decision':'rule',
          'rule_exit':rule_exit,'order_attempt_id':token}
    output=hook_call(monkeypatch,capsys,args)
    assert output['permissionDecision']=='allow',output
    result=M.place_order(**output['updatedInput'])
    assert result['ok'] and fake.orders==[('000001','매도',30)]
    retried=M.place_order(**output['updatedInput'])
    assert retried['replayed'] and len(fake.orders)==1


def test_source_proof_change_during_risk_also_requires_new_plan(monkeypatch,runtime_authority):
    activated(monkeypatch)
    fake,_,_,args=planned(monkeypatch)
    original=M.risk.check; old_base=PR._current_base
    def check_then_change_source(*a,**kw):
        errors=original(*a,**kw)
        monkeypatch.setattr(PR,'_current_base',lambda:{**old_base(),'definition_hash':'changed-source'})
        return errors
    monkeypatch.setattr(M.risk,'check',check_then_change_source)
    result=M.place_order(**args)
    assert result.get('error')=='STALE_POLICY_PLAN' and fake.orders==[]


def test_hook_revision_change_between_preflight_and_context_is_not_allowed(monkeypatch,capsys,runtime_authority):
    activated(monkeypatch)
    fake,_,_,args=planned(monkeypatch)
    original=PR.build_context
    def change_before_context(*a,**kw):
        T.write_params([{'key':'decision.buy_min_confidence','to':P['decision']['buy_min_confidence']+.01}])
        return original(*a,**kw)
    monkeypatch.setattr(PR,'build_context',change_before_context)
    output=hook_call(monkeypatch,capsys,args)
    assert output['permissionDecision']=='deny'
    assert fake.orders==[]
