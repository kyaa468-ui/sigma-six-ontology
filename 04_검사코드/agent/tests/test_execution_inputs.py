"""Execution input capture and legacy monitor timing, entirely local."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import copy
import json
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import common as C
import execution_trace as E
import monitor as M
import position_episodes as PE
import risk
import sizing

FIX = ROOT / 'tests/fixtures/ontology'
P = read_stored(FIX / 'params.json')
R = read_stored(FIX / 'risk_limits.json')
NOW = datetime.fromisoformat('2026-11-06T10:00:00+09:00')


def clone(x): return copy.deepcopy(x)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    def forbidden(*a, **k): raise AssertionError('External calls forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    for key, file in [('WORK',''),('BRIEFING','briefing.json'),('ACCOUNT','account.json'),('JOURNAL','journal.jsonl'),
                      ('POSITIONS','positions.json'),('MARKETS','markets.json')]:
        monkeypatch.setattr(C,key,str(tmp_path/file))
    monkeypatch.setattr(M,'STATE',str(tmp_path/'monitor_state.json'))
    monkeypatch.setattr(C,'now',lambda:NOW)
    monkeypatch.setattr(C,'params',lambda:clone(P))
    monkeypatch.setattr(C,'risk_limits',lambda:clone(R))
    monkeypatch.setenv('MODE','mock')
    monkeypatch.setenv('RUN_ID','test-inputs')
    monkeypatch.setenv('RUN_ATTEMPT_ID','attempt-inputs')
    monkeypatch.setenv('RUN_MODE','decide')
    monkeypatch.delenv('ONTOLOGY_ACCOUNT_SCOPE',raising=False)
    C.write_json(C.BRIEFING,{'candidates':[],'holdings':[]})
    C.write_json(C.POSITIONS,{})
    yield tmp_path


def holding(qty=20, avg=20000, price=20400):
    return {'code':'000001','name':'fixture','qty':qty,'avg_price':avg,'price':price,'value':qty*price}


def account(hs=()):
    hs=clone(list(hs)); value=sum(h['value'] for h in hs)
    return {'total':50_000_000,'cash':50_000_000-value,'stock_value':value,'holdings':hs}


def test_sizing_audit_is_same_single_result():
    audit={}; acc=account()
    expected=sizing.plan_buy('000001',.7,20400,acc)
    actual=sizing.plan_buy('000001',.7,20400,acc,audit=audit)
    assert actual==expected
    assert audit['qty']==actual['qty']
    assert audit['unit_with_fee']==20400*(1+P['costs']['fee_rate'])
    assert audit['limits'][audit['binding']]['value']==audit['budget']
    assert audit['limits']['인버스 먼저 전량 매도']=={'value':None,'unbounded':True}
    assert audit['actual_reads']
    acc['cash']=0
    assert audit['inputs']['account']['cash']==50_000_000
    json.dumps(audit,allow_nan=False)


def test_risk_records_pass_fail_inapplicable_and_unexecuted():
    audit={}; order={'code':'000001','side':'매수','qty':0,'confidence':.7,'today_change':None}
    bad=risk.check(order,account(),20400,audit=audit)
    assert bad==risk.check(order,account(),20400)
    checks={x['rule_id']:x for x in audit['checks']}
    assert checks['RISK_01']['status']=='failed'
    assert checks['RISK_02']['status']=='passed'
    assert checks['RISK_07']['status']=='not_applicable'
    assert checks['RISK_26']['status']=='not_evaluated' # sell branch never evaluated
    assert audit['sizing']['qty']>=0 and audit['actual_reads']
    assert audit['inputs']['order']==order


def test_sell_audit_preserves_actual_restrictions():
    audit={}; acc=account([holding()])
    out=sizing.plan_sell('000001',.6,20400,acc,audit=audit)
    assert out==sizing.plan_sell('000001',.6,20400,acc)
    assert audit['qty']==out['qty'] and audit['held_qty']==20
    assert audit['inputs']['rule_exit'] is False
    assert 'sold_today_partial' in audit['restrictions']


def test_position_interval_add_on_partial_close_and_reentry():
    first=PE.observe(account([holding()]))['000001']
    assert PE.get(first)['origin']=='origin_unrecorded'
    assert PE.observe(account([holding(40,20100)]))['000001']==first
    assert PE.observe(account([holding(10)]))['000001']==first
    assert PE.observe(None)=={}
    assert PE.observe({'holdings':[],'complete':False})=={}
    assert PE.current('000001')==first
    PE.observe(account())
    assert PE.current('000001') is None and PE.get(first)['closed']
    second=PE.observe(account([holding()]))['000001']
    assert second!=first and PE.get(second)['origin']=='observed_transition'


@pytest.mark.parametrize('snapshot',[{}, {'error':'query failed','holdings':[]}, {'holdings':[{'code':'000001'}]}, {'holdings':[{'code':'000001','qty':float('nan')}]}, {'holdings':[{'code':'000001','qty':-1}]}])
def test_invalid_snapshot_never_closes_episode(snapshot):
    first=PE.observe(account([holding()]))['000001']
    PE.observe(snapshot)
    assert PE.current('000001')==first


def test_position_scope_and_recording_failure(monkeypatch):
    first=PE.observe(account([holding()]))['000001']
    monkeypatch.setenv('ONTOLOGY_ACCOUNT_SCOPE','different-account')
    assert PE.current('000001') is None
    second=PE.observe(account([holding()]))['000001']
    assert second!=first
    monkeypatch.setattr(PE,'_db',lambda:(_ for _ in ()).throw(OSError('episode store unavailable')))
    assert PE.observe(account([holding()]))=={}


def test_monitor_records_actual_rule_not_unexecuted_rules(monkeypatch):
    hs=[holding(price=18900)]
    C.write_json(C.POSITIONS,{'000001':{'name':'fixture','entry_date':'2026-11-05'}})
    p=clone(P);p['decision']['stop_loss_pct']=-.05
    monkeypatch.setattr(C,'params',lambda:clone(p))
    result=M.evaluate_triggers(hs)
    ev=result['evaluations'][0]
    assert ev['position_meta']['stop']==19000
    assert ev['matched_rule']=='STOP_LOSS@v1'
    assert len(ev['comparisons'])==1
    assert ev['comparisons'][0]['left']==18900
    assert ev['monitor_key']=='000001@2026-11-05/19000/'+str(ev['position_meta']['take'])
    assert ev['position_episode_id']
    assert result['rule_trigger_ids']['000001']


def test_monitor_does_not_link_an_unsaved_trigger(monkeypatch):
    hs=[holding(price=18000)]
    C.write_json(C.POSITIONS,{'000001':{'name':'fixture','entry_date':'2026-11-05'}})
    monkeypatch.setattr(E,'record',lambda *a,**k:{'event_id':'unsaved','recording_status':'recording_error'})
    result=M.evaluate_triggers(hs)
    assert result['hits'] and not result['rule_trigger_ids']


def legacy_namespace(module):
    fixture=read_stored(FIX/'execution_baseline_source.json')
    assert fixture['baseline_commit']=='25f7da455f025720564ccd56ced36df4ea3ece1b'
    namespace=dict(vars(module))
    exec(compile(fixture['functions'][module.__name__], '<frozen-before-step3>', 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('side,conf,qty,price,mode,rule_exit,held',[
    ('매수',.5,10,20400,'decide',False,False),
    ('매수',.7,100,20400,'decide',False,False),
    ('매수',.9,0,20400,'view',False,False),
    ('매수',.9,100000,20400,'exit',False,False),
    ('매수',.9,10,19000,'decide',False,True),
    ('매도',.6,7,20400,'decide',False,True),
    ('매도',.9,20,20400,'decide',False,True),
    ('매도',.1,20,18900,'exit',True,True),
    ('매도',.9,21,20400,'decide',False,True),
    ('매도',.9,1,20400,'decide',False,False),
])
def test_sizing_and_risk_match_frozen_policy(side,conf,qty,price,mode,rule_exit,held,monkeypatch):
    monkeypatch.setenv('RUN_MODE',mode)
    acc=account([holding()] if held else [])
    old_sizing=legacy_namespace(sizing)
    if side=='매수':
        expected=old_sizing['plan_buy']('000001',conf,price,clone(acc),today_change=-.05)
        actual=sizing.plan_buy('000001',conf,price,clone(acc),today_change=-.05,audit={})
    else:
        expected=old_sizing['plan_sell']('000001',conf,price,clone(acc),rule_exit=rule_exit)
        actual=sizing.plan_sell('000001',conf,price,clone(acc),rule_exit=rule_exit,audit={})
    assert actual==expected
    order={'code':'000001','side':side,'qty':qty,'confidence':conf,'today_change':-.05,'rule_exit':rule_exit}
    expected=legacy_namespace(risk)['check'](clone(order),clone(acc),price)
    actual=risk.check(clone(order),clone(acc),price,audit={})
    assert actual==expected  # Same messages in the same policy evaluation order.


def monitor_scenario(tmp_path,monkeypatch,legacy=False,recording_failure=False):
    namespace=legacy_namespace(M) if legacy else vars(M)
    state=tmp_path/('legacy_state.json' if legacy else 'new_state.json')
    if legacy: namespace['STATE']=str(state)
    else: monkeypatch.setattr(M,'STATE',str(state))
    clock={'at':NOW,'holdings':[holding(price=18900)],'returncode':0}
    params=clone(P); params['decision']['stop_loss_pct']=-.05
    C.write_json(C.POSITIONS,{'000001':{'name':'fixture','entry_date':'2026-11-05'}})
    monkeypatch.setattr(C,'now',lambda:clock['at'])
    monkeypatch.setattr(C,'params',lambda:clone(params))
    monkeypatch.setattr(M.time,'time',lambda:clock['at'].timestamp())
    if legacy: namespace['holdings']=lambda:clone(clock['holdings'])
    else: monkeypatch.setattr(M,'holdings',lambda:clone(clock['holdings']))
    if recording_failure:
        def broken(*args,**kwargs): raise OSError('forced telemetry outage')
        monkeypatch.setattr(E,'record',broken)
    calls=[]
    def fake_run(cmd,**kwargs):
        calls.append({'at':clock['at'].isoformat(),'codes':cmd[cmd.index('--codes')+1],
                      'why':cmd[cmd.index('--why')+1],
                      'has_trigger_ids':'--rule-trigger-ids' in cmd})
        return SimpleNamespace(returncode=clock['returncode'])
    monkeypatch.setattr(M.subprocess,'run',fake_run)
    # The add-on average and stop setting alter the old monitor key immediately.
    # Exit 3 is not marked fired, so the very next tick must retry.
    steps=[('2026-11-06T09:29:00+09:00',0,None,None),
           ('2026-11-06T09:30:00+09:00',3,None,None),
           ('2026-11-06T09:31:00+09:00',0,None,None),
           ('2026-11-06T09:32:00+09:00',0,None,None),
           ('2026-11-06T09:33:00+09:00',0,21000,None),
           ('2026-11-06T09:34:00+09:00',0,None,-.06),
           ('2026-11-06T09:43:59+09:00',0,None,None),
           ('2026-11-06T09:44:00+09:00',3,None,None),
           ('2026-11-06T09:44:01+09:00',0,None,None),
           ('2026-11-06T09:45:00+09:00',0,None,None),
           ('2026-11-09T09:30:00+09:00',0,None,None)]
    episodes=[]
    for at,rc,avg,stop in steps:
        clock.update(at=datetime.fromisoformat(at),returncode=rc)
        if avg: clock['holdings']=[holding(qty=40,avg=avg,price=18900)]
        if stop: params['decision']['stop_loss_pct']=stop
        namespace['check_once']()
        if not legacy: episodes.append(PE.current('000001'))
    return calls,C.read_json(str(state)),episodes


@pytest.mark.parametrize('recording_failure',[False,True])
def test_monitor_retry_timing_is_frozen_policy_even_after_add_on_and_stop_change(tmp_path,monkeypatch,recording_failure):
    expected,oldstate,_=monitor_scenario(tmp_path,monkeypatch,legacy=True)
    actual,newstate,episodes=monitor_scenario(tmp_path,monkeypatch,recording_failure=recording_failure)
    strip_ids=lambda rows:[{k:v for k,v in row.items() if k!='has_trigger_ids'} for row in rows]
    assert strip_ids(actual)==strip_ids(expected)
    assert oldstate==newstate
    assert [x['at'][11:19] for x in actual]==['09:30:00','09:31:00','09:33:00','09:34:00','09:44:00','09:44:01','09:30:00']
    assert len(set(episodes))==1 and episodes[0]  # Audit interval does not reset on add-on or setting change.
    assert all(x['has_trigger_ids'] for x in actual)==(not recording_failure)
    assert list(newstate['fired'])==['000001@2026-11-05/19740/23100']


@pytest.mark.parametrize('price,entry_date,stop,take,max_days',[
    (18000,'2026-11-05',19000,22000,5),
    (23000,'2026-11-05',19000,22000,5),
    (20400,'2026-10-20',None,22000,5),
    (20400,None,None,None,5),
])
def test_monitor_trigger_precedence_matches_legacy(price,entry_date,stop,take,max_days,monkeypatch):
    hs=[holding(price=price)]
    meta={'000001':{'entry_date':entry_date,'stop':stop,'take':take,'max_days':max_days}}
    monkeypatch.setattr(C,'position_meta',lambda hs,p=None:clone(meta))
    old=legacy_namespace(M)['triggers'](clone(hs))
    new=M.evaluate_triggers(clone(hs))
    assert new['hits']==old
    ev=new['evaluations'][0]
    if price==18000: assert len(ev['comparisons'])==1
    if price==23000: assert len(ev['comparisons'])==2
    if entry_date is None: assert all(x['status']=='not_applicable' for x in ev['comparisons'])
