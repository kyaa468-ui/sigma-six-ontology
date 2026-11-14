"""Offline durable dispatch fault tests, including independent processes."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import json
import multiprocessing
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import common as C
import execution_trace as E

NOW = datetime.fromisoformat('2026-11-06T10:01:00+09:00')
PAYLOAD = {'code':'000001','side':'매수','qty':100,'confidence':.7,'rule_exit':False,'judgment_id':None,'plan_id':None,'rule_trigger_id':None}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network forbidden in execution tests')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(C,'WORK',str(tmp_path))
    monkeypatch.setattr(C,'now',lambda:NOW)
    monkeypatch.setenv('RUN_ID','offline-run')
    monkeypatch.setenv('RUN_ATTEMPT_ID','attempt-local')
    monkeypatch.setenv('MODE','mock')
    monkeypatch.delenv('ONTOLOGY_ACCOUNT_SCOPE',raising=False)
    monkeypatch.delenv('ACCOUNT_NAME',raising=False)
    return tmp_path


def reserve():
    scope=E.account_scope()
    row=E.reserve_attempt(scope)
    assert row['status']=='reserved'
    return scope,row['order_attempt_id']


def sender_log(path, crash=False):
    fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    try:
        os.write(fd,b'POST\n');os.fsync(fd)
    finally:
        os.close(fd)
    if crash: os._exit(0)
    return {'ok':True,'order_no':'0012345','qty':100}


def worker(work, scope, aid, log, crash=False, claim_only=False):
    C.WORK=work
    if claim_only:
        E._inspect_or_claim(scope,aid,PAYLOAD,claim=True)
        os._exit(0)
    E.dispatch_once(scope,aid,PAYLOAD,lambda:sender_log(log,crash))


def process(work,scope,aid,log,**kwargs):
    ctx=multiprocessing.get_context('spawn')
    child=ctx.Process(target=worker,args=(str(work),scope,aid,str(log)),kwargs=kwargs)
    child.start()
    return child


def join(child):
    child.join(10)
    if child.is_alive():
        child.terminate();child.join();pytest.fail('Local dispatch process hung')
    assert child.exitcode==0


def test_scope_matches_hook_and_broker_without_exposing_account(monkeypatch):
    monkeypatch.setenv('ACCOUNT_NAME','private team account')
    a=E.account_scope()
    assert a==E.account_scope(SimpleNamespace(name='live',base='https://mockapi.example'))
    assert 'private' not in a
    monkeypatch.setenv('ACCOUNT_NAME','different account')
    assert a!=E.account_scope()


def test_success_and_reprocessing_send_once(isolated):
    scope,aid=reserve();calls=[]
    send=lambda:calls.append(True) or {'ok':True,'order_no':'00123','qty':100,'price':20400}
    first=E.dispatch_once(scope,aid,PAYLOAD,send)
    second=E.dispatch_once(scope,aid,PAYLOAD,send)
    assert len(calls)==1 and first['ok'] and second['ok'] and second['replayed']
    assert first['submission_status']=='accepted'
    assert E.probe_attempt(scope,aid,PAYLOAD)['status']=='complete'
    assert E.reserve_attempt(scope,attempt_id=aid)['status']=='complete'


def test_response_loss_never_reopens_claim(isolated):
    scope,aid=reserve();calls=[]
    def send():
        calls.append(True)
        raise TimeoutError('broker accepted then response lost')
    first=E.dispatch_once(scope,aid,PAYLOAD,send)
    assert first['submission_status']=='submission_unknown'
    assert first['execution_quality']=='unknown'
    for _ in range(3):
        assert E.dispatch_once(scope,aid,PAYLOAD,send)['submission_status']=='submission_unknown'
    assert len(calls)==1


@pytest.mark.parametrize('which',['audit','result'])
def test_after_acceptance_storage_failure_does_not_resend(isolated,monkeypatch,which):
    scope,aid=reserve();calls=[]
    def fail(*a,**k): raise OSError('disk fault')
    if which=='audit': monkeypatch.setattr(E,'_archive',fail)
    else: monkeypatch.setattr(E,'_store_result',fail)
    send=lambda:calls.append(True) or {'ok':True,'order_no':'0000444','qty':100}
    first=E.dispatch_once(scope,aid,PAYLOAD,send)
    assert first['recording_status']=='recording_error'
    again=E.dispatch_once(scope,aid,PAYLOAD,send)
    assert len(calls)==1
    if which=='result': assert again['submission_status']=='submission_unknown'


def test_pre_dispatch_guard_failure_sends_nothing(isolated,monkeypatch):
    scope,aid=reserve();calls=[]
    monkeypatch.setattr(E,'_connect',lambda *a,**k:(_ for _ in ()).throw(OSError('ledger unreadable')))
    result=E.dispatch_once(scope,aid,PAYLOAD,lambda:calls.append(True))
    assert result['error_code']=='DISPATCH_GUARD_ERROR' and result['sent_by_this_call'] is False
    assert calls==[]


def test_commit_failure_sends_nothing(isolated,monkeypatch):
    scope,aid=reserve();calls=[]
    original=E._connect
    class BrokenCommit:
        def __init__(self,db):self.db=db
        def __getattr__(self,key):return getattr(self.db,key)
        def commit(self):raise OSError('commit fsync failed')
    monkeypatch.setattr(E,'_connect',lambda *a,**k:BrokenCommit(original(*a,**k)))
    result=E.dispatch_once(scope,aid,PAYLOAD,lambda:calls.append(True))
    assert result['error_code']=='DISPATCH_GUARD_ERROR' and calls==[]


def test_payload_conflict_never_sends_changed_order(isolated):
    scope,aid=reserve();calls=[]
    assert E.probe_attempt(scope,aid,PAYLOAD)['status']=='reserved'
    conflict=E.dispatch_once(scope,aid,{**PAYLOAD,'qty':101},lambda:calls.append(True))
    assert conflict['error_code']=='ORDER_ATTEMPT_CONFLICT' and calls==[]
    assert E.dispatch_once(scope,aid,PAYLOAD,lambda:{'ok':True})['ok']
    assert E.dispatch_once(scope,aid,{**PAYLOAD,'side':'매도'},lambda:calls.append(True))['error_code']=='ORDER_ATTEMPT_CONFLICT'
    assert calls==[]


def test_unknown_id_cannot_create_ledger_or_send(isolated):
    calls=[]
    result=E.dispatch_once(E.account_scope(),'order-unknown',PAYLOAD,lambda:calls.append(True))
    assert result['status']=='error' and calls==[] and not E._db_path().exists()
    scope,aid=reserve()
    assert E.dispatch_once(scope,'order-unknown',PAYLOAD,lambda:calls.append(True))['error_code']=='UNKNOWN_ORDER_ATTEMPT'
    assert E.dispatch_once(scope,None,PAYLOAD,lambda:calls.append(True))['error_code']=='ORDER_ATTEMPT_REQUIRED'


def test_concurrent_processes_transmit_once(isolated):
    scope,aid=reserve();log=isolated/'posts.txt'
    children=[process(isolated,scope,aid,log) for _ in range(3)]
    for child in children:join(child)
    assert log.read_text().splitlines()==['POST']
    assert E.dispatch_once(scope,aid,PAYLOAD,lambda:sender_log(log))['replayed']
    assert log.read_text().splitlines()==['POST']


def test_process_death_after_broker_acceptance_never_resends(isolated):
    scope,aid=reserve();log=isolated/'posts.txt'
    join(process(isolated,scope,aid,log,crash=True))
    result=E.dispatch_once(scope,aid,PAYLOAD,lambda:sender_log(log))
    assert result['submission_status']=='submission_unknown'
    assert result['execution_quality']=='unknown'
    assert log.read_text().splitlines()==['POST']


def test_process_death_before_post_preserves_zero_send_unknown(isolated):
    scope,aid=reserve();log=isolated/'posts.txt'
    join(process(isolated,scope,aid,log,claim_only=True))
    result=E.dispatch_once(scope,aid,PAYLOAD,lambda:sender_log(log))
    assert result['submission_status']=='submission_unknown' and not log.exists()


def test_plan_archive_and_definition_are_immutable(isolated):
    import position_episodes as PE
    episode=PE.observe({'holdings':[{'code':'000001','qty':10}]},source={'kind':'offline_account'})['000001']
    out=E.save_plan({'code':'000001','side':'매수','qty':100},{'price':20400,'ma20':20000,'position_episode_id':episode})
    assert out['recording_status']=='recorded'
    loaded=E.load_plan(out['plan_id'])
    assert loaded['inputs']['price']==20400
    assert loaded['position_episode_id']==episode
    trace=E.get_execution_trace(plan_id=out['plan_id'])
    assert trace['recording_status']=='recorded' and len(trace['plans'])==1
    assert trace['definitions'][0]['sources']['risk.py']
    assert E.get_execution_trace(position_episode_id=episode)['plans'][0]['plan_id']==out['plan_id']
    assert trace['position_episodes'][0]['stored_state']['origin']=='origin_unrecorded'


def test_trace_links_by_ids_not_instrument_time(isolated):
    first=E.save_plan({'code':'000001','qty':100},{})
    second=E.save_plan({'code':'000001','qty':100},{})
    E.record('risk',plan_id=first['plan_id'],order_attempt_id=first['order_attempt_id'],outcome='blocked')
    trace=E.get_execution_trace(plan_id=first['plan_id'])
    assert len(trace['plans'])==1
    assert all(e.get('plan_id')!=second['plan_id'] for e in trace['events'])


def test_unrelated_corrupt_record_does_not_poison_current_trace(isolated):
    first=E.save_plan({'code':'000001','qty':100},{})
    path=isolated/'ontology/executions/attempt-other/order_plan/broken.json'
    path.parent.mkdir(parents=True);path.write_text('{')
    trace=E.get_execution_trace(plan_id=first['plan_id'])
    assert trace['recording_status']=='recorded' and trace['unrelated_store_errors']


def test_safe_ids_and_json_nonfinite_state(isolated):
    assert E.reserve_attempt('../escape')['error_code']=='INVALID_ID'
    assert E.get_execution_trace(order_attempt_id='../escape')['error_code']=='INVALID_ID'
    event=E.record('size',limits={'unbounded':float('inf')})
    assert event['recording_status']=='recorded'
    path=next((isolated/'ontology/executions/attempt-local/size').glob('*.json'))
    assert read_stored(path)['limits']['unbounded']['status']=='non_finite'


def test_missing_previously_initialized_ledger_is_never_recreated(isolated):
    scope,aid=reserve();calls=[]
    E.dispatch_once(scope,aid,PAYLOAD,lambda:{'ok':True})
    E._db_path().unlink()
    assert E.reserve_attempt(scope,attempt_id=aid)['error_code']=='DISPATCH_GUARD_ERROR'
    assert not E._db_path().exists()
    assert E.dispatch_once(scope,aid,PAYLOAD,lambda:calls.append(True))['submission_status']=='submission_unknown'
    assert calls==[]
    with pytest.raises(OSError): E.pending_orders()
    assert E.get_execution_trace(order_attempt_id=aid)['recording_status']=='recording_error'


def test_unknown_trace_is_partial_not_success(isolated):
    report=E.get_execution_trace(order_attempt_id='order-missing')
    assert report['recording_status']=='partial' and report['missing_links'][0]['id']=='order-missing'


@pytest.mark.parametrize('submitted',[None,'plan-other'])
def test_issued_plan_never_fills_missing_or_mismatched_submission(isolated,submitted):
    plan=E.save_plan({'code':'000001','qty':100},{})
    scope=E.account_scope();calls=[]
    payload={**PAYLOAD,'plan_id':submitted}
    result=E.dispatch_once(scope,plan['order_attempt_id'],payload,lambda:calls.append(True) or {'ok':True})
    assert result['ok'] and calls==[True]
    row=E.pending_orders()[0]
    assert row['issued_plan_id']==plan['plan_id'] and row['plan_id']==submitted
    assert row['plan_link']['status']==('missing' if submitted is None else 'mismatch')
    trace=E.get_execution_trace(order_attempt_id=plan['order_attempt_id'])
    assert trace['recording_status']=='partial'
    assert any(x['kind']=='submitted_plan_link' for x in trace['missing_links'])
    dispatch_events=[x for x in trace['events'] if x['kind'].startswith('dispatch_')]
    assert all(x['plan_id']==submitted for x in dispatch_events)
    assert E.reserve_attempt(scope,attempt_id=plan['order_attempt_id'])['error_code']=='ORDER_ATTEMPT_CONFLICT'


def test_invalid_auxiliary_ids_are_evidence_errors_not_new_trading_vetoes(isolated):
    scope=E.account_scope();calls=[]
    attempt=E.reserve_attempt(scope,plan_id='잘못된 ID')
    assert attempt['status']=='reserved'
    payload={**PAYLOAD,'plan_id':'잘못된 ID','judgment_id':'../invalid judgment','rule_trigger_id':'wrong reference'}
    assert E.probe_attempt(scope,attempt['order_attempt_id'],payload)['status']=='reserved'
    sender=lambda:calls.append(True) or {'ok':True}
    assert E.dispatch_once(scope,attempt['order_attempt_id'],payload,sender)['ok']
    assert E.dispatch_once(scope,attempt['order_attempt_id'],payload,sender)['replayed']
    assert calls==[True]
    trace=E.get_execution_trace(order_attempt_id=attempt['order_attempt_id'])
    assert trace['recording_status']=='partial'
    assert trace['dispatch'][0]['reference_validation']['judgment_id']=='invalid'
    assert trace['dispatch'][0]['plan_link']['status']=='invalid'


@pytest.mark.parametrize('qty',[0,-1])
def test_nonpositive_quantity_can_be_probed_but_never_dispatched(isolated,qty):
    scope,aid=reserve();calls=[];payload={**PAYLOAD,'qty':qty}
    assert E.probe_attempt(scope,aid,payload)['status']=='reserved'
    assert E.dispatch_once(scope,aid,payload,lambda:calls.append(True))['error_code']=='INVALID_ORDER_PAYLOAD'
    assert calls==[]


def test_definition_write_failure_is_not_recorded_plan_success(isolated,monkeypatch):
    monkeypatch.setattr(E,'_definition_snapshot',lambda:(_ for _ in ()).throw(OSError('archive unavailable')))
    result=E.save_plan({'code':'000001','qty':100},{})
    assert result['recording_status']=='recording_error'
    assert E.get_execution_trace(plan_id=result['plan_id'])['recording_status']=='recording_error'


def test_legacy_intent_fingerprint_uses_canonical_numeric_values():
    assert E.intent_fingerprint({**PAYLOAD,'qty':100,'confidence':1})==E.intent_fingerprint({**PAYLOAD,'qty':100.0,'confidence':1.0})
    assert E.intent_fingerprint(PAYLOAD)!=E.intent_fingerprint({**PAYLOAD,'qty':101})


def test_trace_expands_explicit_trigger_event_id(isolated):
    trigger=E.record('rule_trigger',code='000001',monitor_key='000001@2026-11-01/90/110',
                     comparisons=[{'left':89,'operator':'<=','right':90}],matched_rule='STOP_LOSS@v1')
    unrelated=E.record('rule_trigger',code='000001',monitor_key='000001@2026-11-01/90/110')
    out=E.save_plan({'code':'000001','side':'매도','qty':100},{},rule_trigger_id=trigger['event_id'])
    trace=E.get_execution_trace(order_attempt_id=out['order_attempt_id'])
    assert trace['recording_status']=='recorded'
    assert [row['event_id'] for row in trace['rule_triggers']]==[trigger['event_id']]
    assert unrelated['event_id'] not in [row['event_id'] for row in trace['events']]
    assert E.get_execution_trace(rule_trigger_id=trigger['event_id'])['plans'][0]['plan_id']==out['plan_id']


def test_trace_loads_delivery_selection_and_retained_candidate_origin(isolated,monkeypatch):
    import ai_judgment as J
    monkeypatch.setattr(J,'load_judgment',lambda jid:{'judgment_id':jid,'delivery_ids':['delivery-local']})
    ontology=isolated/'ontology'
    C.write_json(str(ontology/'deliveries/delivery-local.json'),
                 {'delivery_id':'delivery-local','selection_run_id':'selection-latest','briefing_snapshot':{'candidates':[]}})
    C.write_json(str(ontology/'selections/selection-latest.json'),
                 {'run_id':'selection-latest','candidates':[{'code':'000001','evidence':
                  {'observation':{'run_id':'selection-original'},'admission':{'run_id':'selection-original'}}}]})
    C.write_json(str(ontology/'selections/selection-original.json'),{'run_id':'selection-original','candidates':[]})
    out=E.save_plan({'code':'000001','qty':100},{},judgment_id='judgment-local')
    trace=E.get_execution_trace(order_attempt_id=out['order_attempt_id'])
    assert trace['recording_status']=='recorded'
    assert {row['run_id'] for row in trace['selections']}=={'selection-latest','selection-original'}
    assert trace['deliveries'][0]['delivery_id']=='delivery-local'
