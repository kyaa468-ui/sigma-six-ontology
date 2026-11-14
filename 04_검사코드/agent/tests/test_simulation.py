"""Paired, isolated virtual portfolios with explicit price/fill assumptions."""


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
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import simulation as Sim
from simulation_market import FrozenMarket
from replay_context import ReplayUnavailable

ROOT=Path(__file__).resolve().parents[1]
START='2026-11-06T10:00:00+09:00';END='2026-11-06T10:03:00+09:00'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a,**k):raise AssertionError('Live access forbidden')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)


def obs(identifier,at,price=20000,code='000001',qty=10000):
    return {'observation_id':identifier,'observed_at':at,'available_at':at,'code':code,'price':price,
            'price_basis':'unadjusted','source':'synthetic_local_fixture','executable_qty':qty}


def fixture():
    p=read_stored(ROOT/'config/params.json');r=read_stored(ROOT/'config/risk_limits.json')
    p['rsi_boost']=None;p['market_boost']=None
    decision={'decision_id':'one','at':START,'code':'000001','side':'매수','recommended_action':'매수',
        'module_state':{'common._EXCH':None},
        'raw_confidence':.8,'params':p,'risk':r,'quote_observation_id':'q1','regime':'상승',
        'briefing':{'candidates':[{'code':'000001','rsi14':50,'news':[]}]},
        'condition_context':{'features':{'gap':.02}}}
    spec={'period':{'start':START,'end':END},'costs':{'fee_rate':.001,'sell_tax_rate':.002,'slippage_bps':0,'latency_seconds':1},
          'execution':{'fill_model':'next_observation','quantity_policy':'follow_plan','terminal_holdings':'mark_to_market','max_quote_age_seconds':1000},
          'metrics':{'block_length':1,'bootstrap_repetitions':100,'family_size':1},'seed':7}
    inputs={'complete':True,'initial_account':{'complete':True,'captured_at':START,'cash':10_000_000,'total':10_000_000,'holdings':[]},
        'initial_positions':{},'initial_journal':[],'decisions':[decision],
        'market_observations':[obs('q1',START),obs('f1','2026-11-06T10:00:02+09:00'),obs('end',END,22000)],
        'valuation_times':[START,END],'monitor_checks':[], 'monitor_coverage':'complete',
        'rule_exit_contract':'frozen_explicit_exit_decisions'}
    proposal={'kind':'conditional_adjustment','condition':{'op':'gt','left':{'field':'features.gap'},'right':{'value':0,'unit':'ratio'}},
              'effect':{'operation':'add_confidence','amount':-.2}}
    return inputs,spec,proposal


def test_two_arms_use_independent_cash_costs_and_positions():
    inputs,spec,p=fixture();before=copy.deepcopy(inputs)
    result=Sim.run_pair(inputs,spec,p)
    assert result['status']=='complete',result
    base,changed=(result['arms'][a] for a in ('base','changed'))
    assert base['events'][0]['plan']['qty']>changed['events'][0]['plan']['qty']
    assert base['metrics']['fees']>changed['metrics']['fees']>0
    assert result['net_equity_difference']<0
    assert inputs==before
    assert result['statistics']['status']=='insufficient_data'


def test_partial_fill_is_not_requested_fill():
    inputs,spec,p=fixture();inputs['market_observations'][1]['executable_qty']=2
    result=Sim.run_pair(inputs,spec,p)
    assert result['status']=='complete',result
    event=result['arms']['base']['events'][0]
    assert event['fill']['status']=='partial' and event['fill']['filled_qty']==2
    assert result['arms']['base']['terminal_holdings'][0]['qty']==2


def test_unknown_liquidity_blocks_performance_not_zero_fill_success():
    inputs,spec,p=fixture();inputs['market_observations'][1]['executable_qty']=None
    result=Sim.run_pair(inputs,spec,p)
    assert result['status']=='incomplete'
    assert result['arms']['base']['gaps'][0]['reason']=='executable_quantity_unavailable'


def test_price_and_adjustment_gaps_remain_unknown():
    inputs,spec,p=fixture();inputs['market_observations'][2]['price_basis']='adjusted'
    assert Sim.run_pair(inputs,spec,p)['gaps']==['raw_price_basis_or_source_unverified']
    inputs,spec,p=fixture();spec['execution']['max_quote_age_seconds']=30
    inputs['market_observations'].pop()
    result=Sim.run_pair(inputs,spec,p)
    assert result['status']=='incomplete'
    assert any('price_stale' in g['reason'] for g in result['arms']['base']['gaps'])


def test_no_future_quote_is_used_at_decision_time():
    inputs,spec,_=fixture()
    market=FrozenMarket(inputs['market_observations'],end=END,max_age_seconds=1000)
    assert market.quote('000001',START)['price']==20000
    with pytest.raises(ReplayUnavailable,match='price_missing'):market.quote('000002',START)


def test_monitor_legacy_key_weekdays_and_retry_timing():
    hs=[{'code':'000001','price':99}];meta={'000001':{'entry_date':'2026-11-05','stop':100,'take':120,'max_days':5}}
    assert not Sim.monitor_due(hs,meta,'2026-11-06T09:29:00+09:00',{})[0]['due']
    hit=Sim.monitor_due(hs,meta,'2026-11-06T09:30:00+09:00',{})[0]
    assert hit['monitor_key']=='000001@2026-11-05/100/120'
    from replay_context import instant
    fired={hit['monitor_key']:instant('2026-11-06T09:30:00+09:00').timestamp()}
    assert not Sim.monitor_due(hs,meta,'2026-11-06T09:39:59+09:00',fired)[0]['due']
    assert Sim.monitor_due(hs,meta,'2026-11-06T09:40:00+09:00',fired)[0]['due']
    # Changing a legacy stop changes its retry key immediately, as production does.
    meta['000001']['stop']=101
    assert Sim.monitor_due(hs,meta,'2026-11-06T09:31:00+09:00',fired)[0]['due']
    hs[0]['price']=110;meta['000001'].update(stop=90,max_days=1,entry_date='2026-11-06')
    assert not Sim.monitor_due(hs,meta,'2026-11-08T10:00:00+09:00',{})
    assert Sim.monitor_due(hs,meta,'2026-11-09T10:00:00+09:00',{})[0]['rule']=='max_days'


def test_prompt_arm_watch_does_not_place_order():
    inputs,spec,p=fixture();a=copy.deepcopy(inputs['decisions']);b=copy.deepcopy(a)
    b[0]['recommended_action']='관망'
    inputs['arm_decisions']={'base':a,'changed':b}
    result=Sim.run_pair(inputs,spec,None)
    assert result['status']=='complete',result
    assert result['comparison_kind']=='paired_frozen_ai_judgments'
    assert result['arms']['changed']['events'][0]['status']=='watch'
    assert result['arms']['changed']['metrics']['filled_order_count']==0


def test_arm_opportunity_disagreement_cannot_hide_missing_target():
    inputs,spec,p=fixture();inputs['arm_decisions']={'base':inputs['decisions'],'changed':[]}
    assert 'paired_arm_opportunity_mismatch' in Sim.run_pair(inputs,spec,p)['gaps']


def test_paired_blocks_keep_dates_and_seed():
    base=[{'date':str(i),'equity':100+i} for i in range(12)]
    changed=[{'date':str(i),'equity':100+2*i} for i in range(12)]
    spec={'metrics':{'block_length':3,'bootstrap_repetitions':1000,'family_size':2},'seed':17}
    a=Sim.paired_statistics(base,changed,spec);b=Sim.paired_statistics(base,changed,spec)
    assert a==b and a['status']=='complete' and a['interval'][0]>0
    with pytest.raises(ReplayUnavailable):Sim.paired_statistics(base,changed[:-1],spec)


def test_costs_and_monitor_schedule_cannot_be_implicit():
    inputs,spec,p=fixture();spec['costs'].pop('sell_tax_rate')
    assert 'registered_costs_missing' in Sim.run_pair(inputs,spec,p)['gaps']
    inputs,spec,p=fixture();inputs.pop('monitor_coverage')
    assert 'monitor_schedule_or_input_coverage_missing' in Sim.run_pair(inputs,spec,p)['gaps']


def test_unreconciled_initial_equity_is_not_silently_replaced():
    inputs,spec,p=fixture();inputs['initial_account']['total']+=1000
    result=Sim.run_pair(inputs,spec,p)
    assert result['status']=='incomplete'
    assert result['arms']['base']['gaps'][0]['reason']=='initial_account_equity_not_reconciled'


def test_many_intraday_marks_do_not_inflate_market_sample_count():
    base=[{'date':'one','equity':100},{'date':'one','equity':101},{'date':'one','equity':102},
          {'date':'two','equity':103},{'date':'two','equity':104},{'date':'three','equity':105}]
    changed=[{**row,'equity':row['equity']+index} for index,row in enumerate(base)]
    spec={'metrics':{'block_length':1,'bootstrap_repetitions':100,'family_size':1},'seed':1}
    result=Sim.paired_statistics(base,changed,spec)
    assert result['paired_sessions']==3
