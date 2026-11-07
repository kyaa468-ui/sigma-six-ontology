"""Step 1 -> 2 -> 3 through actual tools, entirely local and temporary."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_ontology import C, D, M, O, B, ROOT, clone, isolated
from test_judgment_integration import setup_run, submission, load_module
import execution_trace as E
import judgment_coverage as JC
import position_episodes as PE


def planned(monkeypatch, *, held=False):
    fake, manifest = setup_run(monkeypatch, held=held)
    briefing = C.read_json(C.BRIEFING)
    selection = O.SelectionTrace('prep', C.params(), '상승', 'local_fixture')
    candidates = D.screen(clone(briefing['candidates']), C.params(), True, '상승', trace=selection)
    selection.attach(candidates)
    briefing['candidates'] = candidates
    selection.finish(briefing)
    assert not selection.errors and candidates
    C.write_json(C.BRIEFING, briefing)
    if held:
        M.get_account()
    jid = M.record_ai_judgments([submission(M.get_briefing())])[0]['judgment_id']
    plan = M.plan_order('000001', '매수', .7, judgment_id=jid)
    args = dict(code='000001', side='매수', qty=1, confidence=.7, bull='local', bear='local', decision='local',
                judgment_id=jid, plan_id=plan['plan_id'], order_attempt_id=plan['order_attempt_id'])
    return fake, manifest, plan, args


def hook_call(monkeypatch, capsys, args, invoke='tool-1'):
    hook = load_module('execution_hook', ROOT/'.claude/hooks/risk_check.py')
    event = {'tool_name':'mcp__kiwoom__place_order','session_id':'local-session', 'tool_use_id':invoke, 'tool_input':args}
    monkeypatch.setattr(hook.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(event).encode())))
    monkeypatch.setattr(B,'cached_change',lambda _: .02)
    monkeypatch.setattr(B,'cached_quote',lambda _: 20400)
    try:
        hook.main()
    except SystemExit:
        pass
    return json.loads(capsys.readouterr().out)['hookSpecificOutput']


def test_evidence_judgment_plan_hook_order_confirmation_graph(monkeypatch, capsys):
    fake, manifest, plan, args = planned(monkeypatch)
    args['qty'] = 100
    sends = []
    def accepted(code, name, side, qty):
        sends.append(qty)
        C.write_json(C.ACCOUNT, {**fake.account_value, 'holdings':[
            {'code':code,'name':name,'qty':qty,'price':20400,'avg_price':20400,'value':qty*20400}]})
        return {'ok':True,'qty':qty,'price':20400,'order_no':'0000123','broker_acknowledgment':'accepted',
                'filled':True, 'execution_quality':'balance_delta_inference'}
    monkeypatch.setattr(fake,'order',accepted)
    output = hook_call(monkeypatch,capsys,args)
    assert output['permissionDecision']=='allow'
    result = M.place_order(**output['updatedInput'])
    assert result['ok'] and sends==[100]
    fake.query_order_executions = lambda *a,**k: {'complete':True,'pages':[], 'rows':[
        {'ord_no':'0000123','cntr_no':'000001','stk_cd':'A000001','dmst_stex_tp':'KRX',
         'io_tp_nm':'매수','cntr_qty':'40','cnfm_qty':'100','cntr_uv':'20400','cntr_tm':'10:31:01'}]}
    status = M.refresh_execution_status(args['order_attempt_id'])
    assert (status['confirmed_qty'],status['unconfirmed_qty'],status['confirmation_status'])==(40,60,'confirmed_partial')
    trace = M.get_execution_trace(order_attempt_id=args['order_attempt_id'])
    assert trace['judgments'][0]['judgment_id']==args['judgment_id']
    assert trace['deliveries'] and trace['definitions'] and trace['selections']
    assert trace['plans'][0]['inputs']['quote']['price']==20400
    assert {'hook_entry','risk_evaluation','dispatch_result','execution_observation'} <= {e['kind'] for e in trace['events']}
    context = next(e for e in trace['events'] if e['kind']=='order_position_context')
    assert context['before_episode_id'] is None and context['after_episode_id']==PE.current('000001')
    assert JC.get_coverage(manifest['run_attempt_id'])['counts']['linked']==1
    again = hook_call(monkeypatch,capsys,args,invoke='different-tool-id')
    assert again['updatedInput']['order_attempt_id']==args['order_attempt_id']
    assert M.place_order(**again['updatedInput'])['replayed']
    assert sends==[100]


@pytest.mark.parametrize('failure',['response_lost','guard_result','legacy_journal'])
def test_order_response_or_record_loss_one_send_via_tool(monkeypatch, failure):
    fake, _, plan, args = planned(monkeypatch)
    original = fake.order
    if failure=='response_lost':
        def order(*a,**k):
            original(*a,**k)
            raise TimeoutError('accepted by fake broker, response lost')
        monkeypatch.setattr(fake,'order',order)
    elif failure=='guard_result':
        monkeypatch.setattr(E,'_store_result',lambda *a,**k: (_ for _ in ()).throw(OSError('disk failure after POST')))
    else:
        journal = C.journal
        def journal_failure(kind, **kwargs):
            if kind=='fill': raise OSError('legacy fill record failed')
            return journal(kind,**kwargs)
        monkeypatch.setattr(C,'journal',journal_failure)
    first = M.place_order(**args)
    second = M.place_order(**args)
    assert len(fake.orders)==1 and second['replayed']
    if failure == 'response_lost':
        assert first['submission_status']==second['submission_status']=='submission_unknown'
    elif failure == 'guard_result':
        assert first['recording_status']=='recording_error'
        assert second['submission_status']=='submission_unknown'
    else:
        assert first['recording_status']=='recording_error'
        assert JC.get_coverage()['linked_rate'] is None


def test_missing_and_conflicting_attempt_never_send(monkeypatch):
    fake, _, _, args = planned(monkeypatch)
    assert M.place_order(**{**args,'order_attempt_id':None})['error_code']=='ORDER_ATTEMPT_REQUIRED'
    assert not fake.orders
    assert M.place_order(**args)['ok']
    assert M.place_order(**{**args,'qty':2})['error_code']=='ORDER_ATTEMPT_CONFLICT'
    assert len(fake.orders)==1


def test_guard_storage_failure_is_narrow_fail_closed(monkeypatch):
    fake, _, _, args = planned(monkeypatch)
    monkeypatch.setattr(E,'_connect',lambda *a,**k: (_ for _ in ()).throw(OSError('guard unavailable')))
    result = M.place_order(**args)
    assert result['error_code']=='DISPATCH_GUARD_ERROR' and not fake.orders


def test_audit_storage_failure_does_not_change_order_policy(monkeypatch):
    fake, _, _, args = planned(monkeypatch)
    monkeypatch.setattr(E,'_archive',lambda *a,**k: (_ for _ in ()).throw(OSError('trace unavailable')))
    assert M.place_order(**args)['ok'] and len(fake.orders)==1
    assert JC.get_coverage()['recording_status']=='recording_error'


def test_hook_supplies_compatible_stable_id_and_block_does_not_consume(monkeypatch,capsys):
    fake,_ = setup_run(monkeypatch)
    args=dict(code='000001',side='매수',qty=0,confidence=.7,bull='local',bear='local',decision='local')
    denied = hook_call(monkeypatch,capsys,args)
    assert denied['permissionDecision']=='deny' and not E.pending_orders()
    args['qty']=1
    allowed = hook_call(monkeypatch,capsys,args,invoke='new-intent')
    assert allowed['permissionDecision']=='allow'
    updated=allowed['updatedInput']
    assert M.place_order(**updated)['ok']
    replay=hook_call(monkeypatch,capsys,updated,invoke='recovery-call')
    assert M.place_order(**replay['updatedInput'])['replayed'] and len(fake.orders)==1
    # Even an old caller omitting the new ID cannot mint another intent merely
    # by using a new transport call ID within the same run/session.
    legacy_replay=hook_call(monkeypatch,capsys,args,invoke='another-transport-id')
    assert legacy_replay['updatedInput']['order_attempt_id']==updated['order_attempt_id']
    assert M.place_order(**legacy_replay['updatedInput'])['replayed'] and len(fake.orders)==1


def test_hook_recovers_issued_plan_token_when_caller_omits_attempt(monkeypatch,capsys):
    _,_,plan,args=planned(monkeypatch)
    args.pop('order_attempt_id')
    output=hook_call(monkeypatch,capsys,args)
    assert output['updatedInput']['order_attempt_id']==plan['order_attempt_id']


def test_server_risk_failure_does_not_consume_dispatch(monkeypatch):
    fake,_,_,args=planned(monkeypatch)
    result=M.place_order(**{**args,'qty':0})
    assert result['blocked'] and not fake.orders and not E.pending_orders()


def test_actual_stage_inputs_are_frozen_when_quote_and_rsi_change(monkeypatch):
    fake,_,plan,args=planned(monkeypatch)
    b=C.read_json(C.BRIEFING)
    b['candidates'][0]['rsi14_live']=20
    C.write_json(C.BRIEFING,b)
    monkeypatch.setattr(fake,'quote_info',lambda _: {'price':21000,'change':.03,'source':'second-fixture'})
    assert M.place_order(**args)['ok']
    trace=M.get_execution_trace(order_attempt_id=args['order_attempt_id'])
    saved=trace['plans'][0]
    risk=next(e for e in trace['events'] if e['kind']=='risk_evaluation' and e['actor']=='server')
    assert saved['inputs']['quote']['price']==20400 and risk['quote']['price']==21000
    assert risk['inputs']['actual_reads']
    event=next(e for e in trace['events'] if e['kind']=='tool_result' and e['stage']=='place_order')
    assert 'order_logging' in event['confidence_audit'] and 'fill' not in event['confidence_audit']
    assert 'common.py' in trace['definitions'][0]['sources']


def test_delivered_episode_context_persists_without_historical_backfill(monkeypatch):
    fake,_,plan,args=planned(monkeypatch,held=True)
    old_id=PE.current('000001')
    judgment=M.get_ai_judgments(code='000001')['judgments'][0]
    assert judgment['position_episode_ids']==[old_id]
    PE.observe({'holdings':[]})
    fake.account_value['holdings'][0]['qty']+=10
    new_id=PE.observe(fake.account_value)['000001']
    assert new_id!=old_id
    assert M.get_ai_judgments(code='000001')['judgments'][0]['position_episode_ids']==[old_id]


def test_delivery_failure_marks_only_own_coverage(monkeypatch):
    _,first=setup_run(monkeypatch)
    second=JC.start_attempt('other-run','decide',C.read_json(C.BRIEFING),C.read_json(C.ACCOUNT))
    # Keep the caller's environment pointing at the first run.
    monkeypatch.setenv('RUN_ATTEMPT_ID',first['run_attempt_id'])
    monkeypatch.setattr(O,'archive',lambda *a,**k: (_ for _ in ()).throw(OSError('delivery disk failure')))
    text=M.get_briefing()
    assert isinstance(text,str) and '저장 실패' in text
    assert JC.get_coverage(first['run_attempt_id'])['recording_status']=='recording_error'
    assert JC.get_coverage(second['run_attempt_id'])['recording_status']=='recorded'


def test_runner_refresh_is_read_only_and_catches_record_error(monkeypatch):
    setup_run(monkeypatch)
    runner=load_module('execution_runner',ROOT/'run.py')
    import execution_reconcile as ER
    monkeypatch.setattr(ER,'refresh_pending',lambda: {'status':'recorded','orders':[]})
    assert runner.refresh_executions()['orders']==[]
    monkeypatch.setattr(ER,'refresh_pending',lambda: (_ for _ in ()).throw(OSError('collector fixture failure')))
    assert runner.refresh_executions()['recording_status']=='recording_error'


def test_delayed_holding_never_backfills_a_previous_order_episode(monkeypatch):
    fake,_,_,args=planned(monkeypatch)
    assert M.place_order(**args)['ok']
    trace=M.get_execution_trace(order_attempt_id=args['order_attempt_id'])
    context=next(e for e in trace['events'] if e['kind']=='order_position_context')
    assert context['episode_status']=='episode_pending' and context['position_episode_id'] is None
    PE.observe({'holdings':[{'code':'000001','qty':1}]},source='later_account')
    later=M.get_execution_trace(order_attempt_id=args['order_attempt_id'])
    assert next(e for e in later['events'] if e['kind']=='order_position_context')==context


def test_invalid_optional_link_is_audit_failure_not_order_policy(monkeypatch):
    fake,_,_,args=planned(monkeypatch)
    args['judgment_id']='잘못된 판단 ID'
    result=M.place_order(**args)
    assert result['ok'] and len(fake.orders)==1
    assert JC.get_coverage()['counts']['accepted_unlinked']==1


def test_rule_exit_links_monitor_trigger_without_fabricating_ai_judgment(monkeypatch,capsys):
    fake,_=setup_run(monkeypatch,held=True)
    manifest=JC.start_attempt('offline-exit','exit',C.read_json(C.BRIEFING),fake.account_value)
    monkeypatch.setenv('RUN_ATTEMPT_ID',manifest['run_attempt_id'])
    monkeypatch.setenv('RUN_MODE','exit')
    M.get_account()
    C.write_json(C.POSITIONS, {'000001':{'name':'fixture','entry_date':'2026-11-05',
                                       'stop':19000,'take':22000,'max_days':5}})
    import monitor
    h=clone(fake.account_value['holdings'][0]); h['price']=15000
    trigger=monitor.evaluate_triggers([h])
    tid=trigger['rule_trigger_ids']['000001']
    monkeypatch.setenv('RULE_TRIGGER_IDS',json.dumps({'000001':tid}))
    plan=M.plan_order('000001','매도',.7,rule_exit=True)
    args=dict(code='000001',side='매도',qty=plan['qty'],confidence=.7,bull='rule',bear='rule',decision='rule',
              rule_exit=True,plan_id=plan['plan_id'],order_attempt_id=plan['order_attempt_id'])
    forwarded=hook_call(monkeypatch,capsys,args)['updatedInput']
    assert forwarded['rule_trigger_id']==tid
    assert M.place_order(**forwarded)['ok'] and len(fake.orders)==1
    trace=M.get_execution_trace(order_attempt_id=plan['order_attempt_id'])
    assert any(t['event_id']==tid and t['matched_rule']=='STOP_LOSS@v1' for t in trace['rule_triggers'])
    assert not trace['judgments'] and trace['position_episodes']
    assert JC.get_coverage()['expected_count']==0
