"""Capture inputs when received, prove monitoring slots, and preserve failures."""
import copy
from datetime import timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_ontology import C, M, B, ROOT, isolated
import execution_trace as E
import evaluation_context as EC
import portfolio_capture as PC
import monitor
import validation_dataset as VD


def records():
    errors=[]
    result=EC._records(Path(C.WORK)/'ontology/executions',C.now(),errors)
    assert errors==[]
    return result


def session(monkeypatch, *, omit=None, bad=None, close=True):
    clock={'at':C.now()}; start=clock['at']; end=start+timedelta(minutes=2)
    monkeypatch.setattr(C,'now',lambda:clock['at'])
    monkeypatch.setattr(monitor,'holdings',lambda:None if clock.get('bad') else [])
    monkeypatch.setattr(monitor,'STATE',str(Path(C.WORK)/'monitor_state.json'))
    monkeypatch.setattr(monitor,'time',SimpleNamespace(time=lambda:clock['at'].timestamp()))
    declared=PC.begin_session(start.isoformat(),end.isoformat(),codes=['000001'])
    assert declared['recording_status']=='recorded'
    for minute in range(3):
        clock['at']=start+timedelta(minutes=minute)
        clock['bad']=minute==bad
        if minute!=omit:monitor.check_once()
    if close:PC.finish_session(declared['session_id'])
    return PC.coverage(records(),start.isoformat(),end.isoformat(),E.account_scope(),['000001']),declared,start,end


def test_account_success_does_not_imply_complete_pagination():
    snap={'total':1000,'stock_eval':0,'holdings':[]}
    assert PC.account_metadata(snap,'fixture')['complete'] is False
    snap['capture_metadata']={'pagination_complete':True,'required_fields_present':True}
    assert PC.account_metadata(snap,'fixture')['complete'] is True
    snap['total']=float('nan')
    assert PC.account_metadata(snap,'fixture')['complete'] is False
    snap['total']=1000
    snap['holdings']=[{'code':'x','qty':1},{'code':'x','qty':1}]
    assert PC.account_metadata(snap,'fixture')['complete'] is False


def test_received_quote_is_not_provider_time_or_known_capacity():
    q=PC.record_quote('000001',20400,source='local_boundary',price_basis='unadjusted')
    assert q['capture_status']=='recorded' and q['provider_observed_at'] is None
    assert q['time_basis']=='received_at' and q['observed_at']==q['available_at']==C.now().isoformat()
    assert q['executable_qty'] is None
    bad=PC.record_quote('000001',20400,source='fixture',executable_qty=100)
    assert bad['status']=='invalid' and bad['executable_qty'] is None
    future=PC.record_quote('000001',20400,source='fixture',provider_observed_at=(C.now()+timedelta(seconds=1)).isoformat())
    assert future['status']=='invalid'


def test_new_capture_preserves_absence_and_does_not_repair_past_state():
    acc={'cash':1000,'total':1000,'holdings':[],'complete':False}
    state=PC.capture_state(acc)
    assert state['positions'] is None and state['journal'] is None
    C.write_json(C.POSITIONS,{})
    C.append_jsonl(C.JOURNAL,{'kind':'fixture'})
    stored=next(r for r in records() if r['event_id']==state['event_id'])
    assert stored['positions'] is None and stored['journal'] is None


def test_complete_capture_requires_actual_successful_check_in_every_slot(monkeypatch):
    proof,_,_,_=session(monkeypatch)
    assert proof['complete'] and len(proof['checks'])==3
    assert all(check['params']==C.params() for check in proof['checks'])
    # Empty holdings do not read a briefing in the legacy branch, but a changed
    # portfolio can hold shares. Keep that contemporaneous extra read separate.
    assert all('briefing.json' in check['snapshots'] for check in proof['checks'])
    assert all(check['counterfactual_reads'][0]['usage']=='counterfactual_monitor_branch' for check in proof['checks'])


def test_sleep_after_check_cadence_accepts_execution_drift_and_detects_a_missed_check(monkeypatch):
    clock={'at':C.now()};start=clock['at'];end=start+timedelta(seconds=372)
    monkeypatch.setattr(C,'now',lambda:clock['at'])
    monkeypatch.setattr(monitor,'holdings',lambda:[])
    monkeypatch.setattr(monitor,'STATE',str(Path(C.WORK)/'monitor_state.json'))
    monkeypatch.setattr(monitor,'time',SimpleNamespace(time=lambda:clock['at'].timestamp()))
    declared=PC.begin_session(None,end.isoformat(),cadence='sleep_after_check',
        valuation_policy='registered_window_boundaries_and_observed_checks',codes=['000001'])
    for seconds in range(0,373,62):
        clock['at']=start+timedelta(seconds=seconds)
        monitor.check_once()
    PC.finish_session(declared['session_id'])
    rows=records()
    proof=PC.coverage(rows,start.isoformat(),end.isoformat(),E.account_scope(),['000001'])
    assert proof['complete'] and len(proof['checks'])==7
    assert proof['valuation_times'][2]==(start+timedelta(seconds=124)).isoformat()
    removed=[r for r in rows if not (r.get('kind')=='portfolio_monitor' and r['check']['at']==proof['checks'][2]['at'])]
    failed=PC.coverage(removed,start.isoformat(),end.isoformat(),E.account_scope(),['000001'])
    assert not failed['complete'] and any(g['reason']=='monitor_gap_exceeds_declared_interval' for g in failed['gaps'])


def test_actual_monitor_loop_records_late_start_without_backfilling(monkeypatch):
    clock={'at':C.now().replace(hour=10,minute=30,second=30)};start=clock['at']
    params=C.params();params['schedule'].update(monitor_start='08:30',monitor_end='10:32',monitor_interval_sec=60)
    monkeypatch.setattr(C,'now',lambda:clock['at'])
    monkeypatch.setattr(C,'params',lambda:copy.deepcopy(params))
    monkeypatch.setattr(monitor,'holdings',lambda:[])
    monkeypatch.setattr(monitor,'STATE',str(Path(C.WORK)/'monitor_state.json'))
    def sleep(seconds):clock['at']+=timedelta(seconds=seconds)
    monkeypatch.setattr(monitor,'time',SimpleNamespace(time=lambda:clock['at'].timestamp(),sleep=sleep))
    monitor.loop()
    rows=records();declared=next(r for r in rows if r.get('kind')=='portfolio_session')
    assert declared['start']==start.isoformat() and declared['cadence']=='sleep_after_check'
    end=start.replace(minute=32,second=0)
    assert PC.coverage(rows,start.isoformat(),end.isoformat(),E.account_scope(),[])['complete']
    assert not PC.coverage(rows,start.replace(hour=8,minute=30,second=0).isoformat(),end.isoformat(),E.account_scope(),[])['complete']


@pytest.mark.parametrize('options,reason',[
    ({'omit':1},'scheduled_monitor_check_missing'),
    ({'bad':1},'scheduled_monitor_check_incomplete'),
    ({'close':False},'capture_session_unfinished')])
def test_missing_failed_and_unfinished_capture_are_not_complete(monkeypatch,options,reason):
    proof,_,_,_=session(monkeypatch,**options)
    assert not proof['complete'] and reason in [g['reason'] for g in proof['gaps']]


def test_coverage_rejects_deleted_original_and_changed_derived_check(monkeypatch):
    proof,_,start,end=session(monkeypatch)
    assert proof['complete']
    rows=records(); derived=next(r for r in rows if r.get('kind')=='portfolio_monitor')
    absent=[r for r in rows if r['event_id']!=derived['source_event_id']]
    assert any(g['reason']=='monitor_source_missing_or_mismatched' for g in PC.coverage(
        absent,start.isoformat(),end.isoformat(),E.account_scope(),['000001'])['gaps'])
    altered=copy.deepcopy(rows)
    next(r for r in altered if r['event_id']==derived['event_id'])['check']['params']={}
    assert not PC.coverage(altered,start.isoformat(),end.isoformat(),E.account_scope(),['000001'])['complete']
    assert not PC.coverage(rows,start.isoformat(),end.isoformat(),'different-account',['000001'])['complete']


def test_predeclared_schedule_cannot_be_created_after_the_interval():
    with pytest.raises(ValueError,match='BEFORE_START'):
        PC.begin_session((C.now()-timedelta(minutes=1)).isoformat(),(C.now()+timedelta(minutes=1)).isoformat())
    record=PC.begin_session(None,(C.now()+timedelta(minutes=1)).isoformat())
    assert record['start']==C.now().isoformat()


def test_capture_failure_does_not_block_an_existing_plan(monkeypatch):
    from test_execution_integration import planned
    monkeypatch.setattr(PC,'capture_state',lambda *a,**k:(_ for _ in ()).throw(OSError('fixture telemetry failure')))
    _,_,plan,_=planned(monkeypatch)
    assert plan['qty']>0
    saved=E.load_plan(plan['plan_id'])
    assert saved['inputs']['portfolio_state']['recording_status']=='recording_error'


def test_live_balance_metadata_does_not_change_values_or_api_count(monkeypatch):
    spec=importlib.util.spec_from_file_location('capture_live_snapshot',ROOT.parent/'live/snapshot.py')
    live=importlib.util.module_from_spec(spec);spec.loader.exec_module(live)
    calls=[]
    body={'return_code':0,'prsm_dpst_aset_amt':'10000000','tot_evlt_amt':'0','tot_pur_amt':'0','acnt_evlt_remn_indv_tot':[]}
    monkeypatch.setattr(live,'post_json',lambda *args:(calls.append(args) or copy.deepcopy(body),{'cont-yn':'N'}))
    result=live.get_balance({'KIWOOM_BASE':'https://invalid.local','ACCOUNT_NAME':'fixture'},'local-token')
    assert len(calls)==1 and result['total']==10000000 and result['stock_eval']==0 and result['holdings']==[]
    assert result['capture_metadata']['pagination_complete'] is True
    assert result['capture_metadata']['required_fields_present'] is True
    monkeypatch.setattr(live,'post_json',lambda *args:(copy.deepcopy(body),{}))
    assert live.get_balance({'KIWOOM_BASE':'https://invalid.local','ACCOUNT_NAME':'fixture'},'local-token')['capture_metadata']['pagination_complete'] is False


@pytest.mark.parametrize('response,expected,status',[
    ({'ord_alow_amt':'900'},900,'observed'),
    ({},0,'unknown'),
    ({'ord_alow_amt':'invalid'},1000,'estimate'),
    (RuntimeError('local fixture failure'),1000,'estimate')])
def test_cash_source_is_recorded_without_changing_legacy_result(response,expected,status):
    obj=object.__new__(B.KiwoomBroker);obj.base='https://fixture.invalid'
    obj.S=SimpleNamespace(num=lambda value:int(float(value or 0)))
    snap={'total':1000,'stock_eval':0,'holdings':[],
          'capture_metadata':{'pagination_complete':True,'required_fields_present':True}}
    obj._balance=lambda:copy.deepcopy(snap)
    calls=[]
    def call(*args):
        calls.append(args)
        if isinstance(response,Exception):raise response
        return copy.deepcopy(response)
    obj._call=call
    acc=obj.account()
    assert acc['cash']==expected and len(calls)==1
    assert acc['capture_metadata']['cash']['status']==status
    assert acc['complete'] is (status=='observed')


def test_mockapi_cash_estimate_and_cached_balance_price_keep_their_source_time():
    obj=object.__new__(B.KiwoomBroker);obj.base='https://mockapi.fixture.invalid'
    received=(C.now()-timedelta(seconds=10)).isoformat()
    snap={'total':10000,'stock_eval':2000,'holdings':[
        {'code':'000001','name':'fixture','qty':1,'avg_price':2000,'price':2000,'value':2000,'pnl_pct':0}],
        'capture_metadata':{'pagination_complete':True,'required_fields_present':True,
            'received_at':received,'holding_prices':[{'code':'000001','received_at':received}]}}
    obj._balance=lambda:copy.deepcopy(snap)
    obj._call=lambda *args:pytest.fail('No additional broker calls permitted')
    acc=obj.account()
    assert acc['cash']==8000 and not acc['complete']
    assert acc['capture_metadata']['cash']['status']=='estimate'
    cached=B.quote_metadata('000001')
    assert cached['available_at']==received and cached['provider_observed_at'] is None
    market=next(r for r in records() if r.get('kind')=='portfolio_market')['observation']
    assert market['received_at']==received and market['time_basis']=='received_at'
    assert market['executable_qty'] is None


def test_market_rows_keep_received_time_and_capacity_provenance():
    q=PC.record_quote('000001',20400,source='synthetic:fixture',price_basis='unadjusted',
                      executable_qty=100,capacity_source='synthetic',provenance='synthetic')
    original=next(r for r in records() if r['event_id']==q['observation_id'])
    # A plan with no committed order remains incomplete, but its quote meaning
    # must survive the frozen dataset unchanged.
    at=C.now().isoformat()
    event={'kind':'order_plan','event_id':'plan-event','created_at':at,'account_scope':E.account_scope(),
           'inputs':{'account':{'complete':True,'cash':1000,'total':1000,'holdings':[]},'quote':q,
                     'sizing':{'inputs':{'code':'000001'},'runtime_context':{'at':at},'actual_reads':[]}}}
    out=VD._simulation_inputs([event,original],[],{'spec':{'period':{'start':at,'end':(C.now()+timedelta(minutes=1)).isoformat()}}},[])
    row=out['market_observations'][0]
    assert row['time_basis']=='received_at' and row['provider_observed_at'] is None
    assert row['capacity_source']=='synthetic' and row['provenance']=='synthetic'
