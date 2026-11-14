"""Offline differential and briefing-contract tests. Run separately from legacy test_rules.py."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import copy
import hashlib
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import common as C
# Imports must not load machine credentials or trigger network login.
with patch.object(C,'load_env',lambda *a,**k: {}), patch.object(socket.socket,'connect',side_effect=AssertionError('network during import')):
    import data_prep as D
    import intraday as I
    import ontology as O
    import ontology_adapter as A
    import broker as B
    import mcp_server as M
import pandas as pd

FIX=ROOT/'tests/fixtures/ontology'
CASES=read_stored(FIX/'cases.json')
SOURCES=read_stored(FIX/'baseline_source.json')['functions']
P=read_stored(FIX/'params.json')
R=read_stored(FIX/'risk_limits.json')
NOW=datetime.fromisoformat(CASES['now'])


def clone(v): return copy.deepcopy(v)
def projected(rows):
    return [{k:v for k,v in row.items() if k in ('code','score','high_vol','from_prep','dip','index','inverse')} for row in rows]

def baseline(module, name):
    ns=dict(vars(D if module=='data_prep' else I))
    ns['C'] = SimpleNamespace(**{**vars(C), 'read_json': lambda path, default=None: read_stored(path) if Path(path).exists() else default})
    for key,src in SOURCES[module].items(): exec(src,ns)
    return ns[name]


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    attempts=[]
    def blocked(*a,**k):
        attempts.append(True)
        raise AssertionError('External network/order forbidden')
    monkeypatch.setattr(socket.socket,'connect',blocked)
    monkeypatch.setattr(socket,'create_connection',blocked)
    monkeypatch.setattr(C,'load_env',lambda *a,**k: {})
    for key,file in [('WORK',''),('BRIEFING','briefing.json'),('JOURNAL','journal.jsonl'),('ACCOUNT','account.json'),('MARKETS','markets.json'),('POSITIONS','positions.json'),('RUNS','runs.jsonl'),('LIVE','live'),('DOCS_DATA','docs')]:
        monkeypatch.setattr(C,key,str(tmp_path/file))
    monkeypatch.setattr(C,'now',lambda:NOW)
    monkeypatch.setattr(C,'params',lambda:clone(P))
    monkeypatch.setattr(C,'risk_limits',lambda:clone(R))
    monkeypatch.setattr(C,'market_of',lambda code,**kw:'KOSPI')
    monkeypatch.setattr(I,'PREP_ROWS',str(tmp_path/'prep_rows.json'))
    monkeypatch.setattr(B,'QUOTES',str(tmp_path/'quotes.json'))
    monkeypatch.setattr(B,'MOCK_ACCOUNT',str(tmp_path/'mock_account.json'))
    monkeypatch.setattr(M.tuning,'LESSONS',str(tmp_path/'lessons.json'))
    monkeypatch.setattr(D.time,'sleep',lambda *_:None)
    monkeypatch.setenv('RUN_ID','offline-ai-run')
    monkeypatch.setenv('MODE','mock')
    for key in ('KRX_ID','KRX_PW','NAVER_CLIENT_ID','NAVER_CLIENT_SECRET'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr(B,'get',blocked)
    monkeypatch.setattr(B.KiwoomBroker,'place',blocked,raising=False)
    monkeypatch.setattr(M,'_broker',None)
    yield tmp_path
    assert not attempts, 'A forbidden call was attempted, even if production code caught its error'


def row(n=0,**overrides):
    v=clone(CASES['rows'][n]);v.update(overrides)
    v['_sources']={'daily':{'asof':'2026-11-05','available_at':'2026-11-06T08:30:00+09:00',
                          'cross_bars':[{'date':'2026-11-04','close':19800,'ma20':19900},{'date':'2026-11-05','close':20400,'ma20':20000}]},
       'flow':{a:O.measurement(v[k],'KRW','pykrx:trading_value',period=['2026-11-04','2026-11-05'],available_at='2026-11-06T08:30:00+09:00') for a,k in [('foreign','foreign_net'),('institution','inst_net')]}}
    return v


def brief(candidates):
    return {'created':C.stamp(),'asof':'2026-11-05','source':'krx','market':{'source':'KOSPI','rule_hint':'상승','asof':'2026-11-05'},
            'candidates':candidates,'holdings':[]}


def prepare(monkeypatch,rows=None):
    rows=rows or [row()]
    monkeypatch.setattr(D,'krx_login',lambda:True)
    monkeypatch.setattr(D,'market_summary',lambda:brief([])['market'])
    monkeypatch.setattr(D,'universe_krx',lambda p,a:[(r['code'],r['name']) for r in rows])
    monkeypatch.setattr(D,'analyze',lambda code,*args:clone(next(r for r in rows if r['code']==code)))
    monkeypatch.setattr(D,'inverse_rows',lambda *args,**kw:[])
    monkeypatch.setattr(D,'news',lambda rows:{})
    return rows


class FakeBroker:
    name='live'  # exercise run's live branch, but _call is a strictly local fake
    def __init__(self, responses=None, price=19800): self.responses=responses or {};self.calls=[];self.price=price
    def _call(self,path,api,body):
        self.calls.append((api,body))
        key='value' if api=='ka10032' else 'foreign' if body['orgn_tp']=='9000' else 'inst'
        return clone(self.responses.get(key,{}))
    def quote_info(self,code):
        self.calls.append(('quote',code))
        return {'price':self.price,'change':-.01,'source':'fixture:quote','observed_at':None,'available_at':'2026-11-06T10:31:00+09:00'}
    def order(self,*a,**k): raise AssertionError('order forbidden')


def responses(rows):
    return {'value':{'trde_prica_upper':[{'stk_cd':r['code'],'stk_nm':r['name'],'cur_prc':str(r.get('price') or r['close']),
                       'now_trde_qty':'250000','flu_rt':str(r.get('chg',0)*100)} for r in rows]},
            **{tag:{'opmr_invsr_trde_upper':[{'stk_cd':r['code'],'stk_nm':r['name'],'netslmt':str(r[tag+'_qty'])} for r in rows if r.get(tag+'_qty') is not None]} for tag in ('foreign','inst')}}


@pytest.mark.parametrize('case',CASES['cases'])
def test_frozen_screen_outputs(case):
    p=clone(P);p['screen']['intraday']['require_flow']=case['enabled']
    trace=O.SelectionTrace(case['stage'],p,case['regime'],'fixture')
    rows=clone(CASES['rows'])
    if case['stage']=='prep': out=D.screen(rows,p,case['enabled'],case['regime'],trace=trace)
    else: out=I.screen(rows,p,R,.5,case['regime'],trace=trace)
    assert projected(out)==case['expected']
    assert not trace.errors


def test_seeded_differential_scores_and_ties():
    import random
    rng=random.Random(20261106)
    for _ in range(20):
        rows=[]
        for n in range(25):
            rows.append(row(code=f'{n:05d}0',price=rng.choice([19900,20400,20400,20800,None]),
                close=rng.choice([19900,20400,20800]),ret20=rng.choice([.1,.1,.2]),
                ret5=rng.choice([.02,.3]),foreign_net=rng.choice([None,0,100000000,-100000000]),
                foreign_qty=rng.choice([None,0,1000,-1000]),inst_qty=rng.choice([None,0,1000,-1000]),vol20=rng.choice([.4,1.8,3.5])))
        # Original prep requires institution whenever foreign is available.
        p=clone(P);p['screen']['max_candidates']=rng.choice([2,15]);reg=rng.choice(['上昇','하락'])
        assert projected(D.screen(clone(rows),p,True,reg))==projected(baseline('data_prep','screen')(clone(rows),p,True,reg))
        assert projected(I.screen(clone(rows),p,R,.5,reg))==projected(baseline('intraday','screen')(clone(rows),p,R,.5,reg))


def test_build_matches_original_and_archives(monkeypatch,isolated):
    prepare(monkeypatch,[row(),row(1)])
    before=baseline('data_prep','build')()
    after=D.build()
    assert projected(before['candidates'])==projected(after['candidates'])
    assert after['ontology']['status']=='recorded'
    saved=read_stored(next((isolated/'ontology/selections').glob('*.json')))
    assert saved['definitions']['ir']['format']=='local-ontology-ir'
    assert saved['engine_sources']['data_prep.py']
    assert saved['observations'][0]['measurements']['price']['kind']=='daily_close'


def test_run_fetch_to_get_briefing(monkeypatch,isolated):
    rs=prepare(monkeypatch,[row(),row(1)])
    D.build()
    fake=FakeBroker(responses(rs))
    monkeypatch.setattr(B,'get',lambda:fake)
    monkeypatch.setattr(M,'_broker',fake)
    before=clone(C.load_briefing());cache=clone(list(C.iter_prep_rows()))
    baseline('intraday','run')()
    expected=projected(C.load_briefing()['candidates'])
    C.write_json(C.BRIEFING,before);C.write_json(I.PREP_ROWS,cache)
    I.run()
    b=C.load_briefing()
    assert projected(b['candidates'])==expected
    assert b['ontology']['status']=='recorded',b['ontology']
    text=M.get_briefing()
    assert isinstance(text,str),text
    for token in ['20,400원 > 20일선 20,000원','+2.0%','같은 비교','외국인 +30억 원','기관 -45억 원','방향이 엇갈림','30.0%','-15억 원','동시점 합계로 확정하지 않음','19,800','후보 선별 가격과 별도 관측']:
        assert token in text,token
    delivery=read_stored(next((isolated/'ontology/deliveries').glob('*.json')))
    assert delivery['text']==text
    assert delivery['sha256']==hashlib.sha256(text.encode()).hexdigest()
    assert delivery['ai_run_id']=='offline-ai-run'
    assert all(o['available_at']=='2026-11-06T10:31:00+09:00' for o in delivery['quotes'])
    ob=next(c['_ontology']['observation'] for c in b['candidates'] if c['code']==rs[0]['code'])
    ids={e['comparison_id'] for e in ob['criteria'] if e['criterion_id'] in ('PRICE_ABOVE_MA20','MA20_GAP')}
    assert ids=={ob['comparison_id']}
    old=clone(delivery)
    monkeypatch.setattr(C,'params',lambda:{**clone(P),'screen':{**P['screen'],'min_trading_value':123}})
    text2=M.get_briefing()
    assert text2!=text
    assert read_stored(isolated/'ontology/deliveries'/f"{old['delivery_id']}.json")==old
    assert len(list((isolated/'ontology/deliveries').glob('*.json')))==2


@pytest.mark.parametrize('raw,known,value',[
    ({'netslmt':'0'},True,0),({'netslmt':0},True,0),({},False,None),({'netslmt':''},False,None),
    ({'buy_qty':'30','sel_qty':'45'},True,-15),({'buy_qty':'30'},False,None)])
def test_quantity_missing_before_legacy_conversion(raw,known,value):
    m=O.raw_quantity(raw,I.num,'2026-11-06T10:30:01+09:00')
    assert (m['status']=='observed')==known
    assert m['value']==value
    assert m['observed_at'] is None
    assert m['available_at'].endswith('01+09:00')


def test_missing_is_not_zero_in_briefing(monkeypatch):
    rs=[row(foreign_qty=10,inst_qty=None),row(1,foreign_qty=10,inst_qty=0)]
    raw={};pool,_=I.fetch(FakeBroker(responses(rs)),I.num,details=raw)
    merged=[{**r,**pool[r['code']],'_sources':{**r['_sources'],**pool[r['code']]['_sources']}} for r in rs]
    t=O.SelectionTrace('intraday',P,'상승','fixture')
    out=I.screen(merged,P,R,.5,'상승',trace=t)
    b=brief(out);b['intraday']={'time':'10:30'};t.finish(b)
    fake=FakeBroker();monkeypatch.setattr(M,'_broker',fake);C.write_json(C.BRIEFING,b)
    text=M.get_briefing()
    assert '기관 미확인' in text and '기관 +0억 원 (실제 0' in text
    assert '기관 원천 수급 미확인; 엔진 계산용 대체값 0원' in text
    assert out[0]['_ontology']['observation']['measurements']['institution']['substituted']


def test_completed_indicators_flow_provenance(monkeypatch):
    dates=pd.bdate_range(end=NOW.date(),periods=70)
    close=pd.Series([18000+i*30 for i in range(70)],index=dates)
    frame=pd.DataFrame({'종가':close,'거래량':1000000,'거래대금':20000000000,'등락률':.1})
    completed=D.completed(frame)
    assert str(completed.index[-1].date())=='2026-11-05'
    monkeypatch.setattr(D,'ohlcv',lambda code:completed)
    flow=pd.DataFrame({'외국인합계':[0,100,None],'기관합계':[-10,0,-5]},index=dates[-4:-1])
    monkeypatch.setattr(D.stock,'get_market_trading_value_by_date',lambda *a:flow)
    r=D.analyze('000010','가상',P,NOW.date(),True)
    assert r['_sources']['daily']['asof']=='2026-11-05'
    assert r['_sources']['flow']['foreign']['value'] is None
    assert r['foreign_net']==100 # legacy pandas sum is intentionally preserved
    direct=D.indicators(completed['종가'])
    assert all(r[k]==v for k,v in direct.items())
    raw=r['_sources']['daily']['ma20_raw']
    assert r['ma20_slope5']==round(raw[-1]['value']/raw[0]['value']-1,4)


def test_equal_ma_missing_bars_and_close_fallback():
    r=row(price=None,close=20000)
    r['_sources']['daily'].pop('cross_bars')
    t=O.SelectionTrace('intraday',P,'하락','fixture')
    assert not I.screen([r],P,R,.5,'하락',trace=t)
    ob=t.records[r['code']]
    assert ob['measurements']['price']['kind']=='close_fallback'
    assert ob['price_comparison']=={'price':20000,'ma20':20000,'gap':0,'above':False}
    assert next(e for e in ob['criteria'] if e['criterion_id']=='CLOSE_CROSS_MA20')['status']=='unknown'
    assert ob['today_close_cross']=='unconfirmed'


def test_trend_actual_short_circuit():
    for regime,px,expected,ids in [('상승',20100,False,['PRICE_ABOVE_MA5']),('상승',20400,True,['PRICE_ABOVE_MA5','MA5_ABOVE_MA20']),('하락',20100,True,['PRICE_ABOVE_MA20'])]:
        result=C.trend_evaluation(px,row(),regime,P)
        assert result['passed']==expected
        assert [e['criterion_id'] for e in result['comparisons']]==ids


def test_retain_previous_intraday_and_error(monkeypatch,isolated):
    prepare(monkeypatch);D.build()
    fake=FakeBroker(responses([row()]));monkeypatch.setattr(B,'get',lambda:fake)
    I.run();before=C.load_briefing()
    old=clone(before['candidates'])
    fake.responses=responses([row(price=1000)])
    I.run();after=C.load_briefing()
    assert after['candidates']==old
    snap=read_stored(isolated/'ontology/selections'/f"{after['ontology']['selection_run_id']}.json")
    assert snap['update_route']=='previous_list_retained' and snap['general_count']==0
    def fail(*a,**k): raise RuntimeError('fixture failure')
    monkeypatch.setattr(fake,'_call',fail)
    I.run();after=C.load_briefing()
    assert after['candidates']==old
    assert after['ontology']['status']=='incomplete'
    assert '직전 후보 목록' in D.to_text(after)


def test_routes_fill_dip_etf_and_no_false_passes():
    t=O.SelectionTrace('prep',P,'상승','fixture')
    prep=D.screen([row(),row(1)],P,True,'상승',trace=t)
    it=O.SelectionTrace('intraday',P,'상승','fixture')
    normal=I.screen([row(chg=-.06,value_rank=1)],P,R,.5,'상승',trace=it)
    filled=I.fill_up(normal,prep,set(),2,it.id)
    assert next(x for x in filled if x.get('from_prep'))['_ontology']['admission']['route']=='prep_fill'
    dip=row(2,price=19000,chg=-.09,value_rank=1)
    I.screen([dip],P,R,.5,'상승',trace=it)
    extra=I.dips([normal[0],dip],filled,set(),P,NOW.replace(hour=15,minute=15))
    it.attach(extra,'dip')
    assert normal[0]['dip'] and normal[0]['_ontology']['admission']['route']=='general'
    assert extra[0]['_ontology']['admission']['route']=='dip'
    assert not any(x['name']=='trend' and x['status']=='passed' for x in extra[0]['_ontology']['observation']['rules'])
    etf=row(code='069500',index=True,score=None)
    et=O.SelectionTrace('prep',P);et.begin(etf,etf['close']);et.attach([etf],'index')
    assert etf['_ontology']['observation']['rules']==[]
    assert I.fill_up([], [etf],set(),5)[0]['_ontology']['admission']['route']=='index'


def test_adapter_rejects_unsupported_and_cycles():
    bundle=A.load_definition_bundle()
    assert A.evaluate(bundle,'MA20_GAP',{'price':20400,'ma20':20000})==pytest.approx(.02)
    for expr in [{'op':'eval','value':'anything'},{'op':'function','function':'PRICE_ABOVE_MA20'},{'op':'property','property':'missing'}]:
        b=clone(bundle);b['ir']['ontology']['functions'][0]['expression']=expr
        with pytest.raises(ValueError): A.validate_bindings(b['ir'],b['bindings'])
    with pytest.raises(ValueError): A.evaluate(bundle,'MA20_GAP',{'price':20400,'ma20':0})


def test_delivery_failure_is_explicit_and_no_overwrite(monkeypatch,isolated):
    O.archive('deliveries','fixed',{'old':1})
    with pytest.raises(FileExistsError): O.archive('deliveries','fixed',{'new':2})
    assert read_stored(isolated/'ontology/deliveries/fixed.json')=={'old':1}
    monkeypatch.setattr(O,'archive',lambda *a:(_ for _ in ()).throw(OSError('disk full')))
    assert '저장 실패 (미보관)' in O.deliver('text',brief([]),[],P,R)
    t=O.SelectionTrace('prep',P);b=brief([]);t.finish(b)
    assert b['ontology']['status']=='unrecorded'


def test_quote_cache_preserves_original_receive_time(monkeypatch):
    B.cache_quote('000010',20400,.02,'kiwoom:ka10001')
    initial=B.quote_metadata('000010')
    obj=object.__new__(B.KiwoomBroker)
    monkeypatch.setattr(obj,'_call',lambda *a:pytest.fail('cache hit must not refetch'))
    q=obj.quote_info('000010')
    assert q['price']==20400 and q['available_at']==initial['available_at']
    assert q['observed_at'] is None


def test_legacy_briefing_is_not_backfilled(monkeypatch):
    C.write_json(C.BRIEFING,brief([row()]))
    monkeypatch.setattr(M,'_broker',FakeBroker())
    assert '온톨로지 근거 미보관' in M.get_briefing()


def test_separate_response_times_and_raw_numeric_zero(monkeypatch):
    times=iter(['2026-11-06T10:30:01+09:00','2026-11-06T10:30:03+09:00','2026-11-06T10:30:05+09:00'])
    monkeypatch.setattr(O,'stamp',lambda:next(times))
    fake=FakeBroker(responses([row()]))
    fake.responses['foreign']['opmr_invsr_trde_upper'][0].update(netslmt=0,buy_qty='10',sel_qty='1')
    raw={};pool,_=I.fetch(fake,I.num,raw)
    sources=pool['000010']['_sources']
    assert sources['price']['available_at'].endswith('01+09:00')
    assert sources['foreign_qty']['available_at'].endswith('03+09:00')
    assert sources['inst_qty']['available_at'].endswith('05+09:00')
    assert sources['foreign_qty']['value']==0
    assert pool['000010']['foreign_qty']==9 # preserve legacy numeric-zero fallback, without calling it source truth
    assert all(sources[k]['observed_at'] is None for k in ['price','foreign_qty','inst_qty'])


def test_zero_pass_skips_dips_and_fill(monkeypatch):
    prepare(monkeypatch);D.build()
    fake=FakeBroker(responses([row(price=1000,chg=-.9)]));monkeypatch.setattr(B,'get',lambda:fake)
    monkeypatch.setattr(I,'dips',lambda *a:pytest.fail('legacy early return must skip dips'))
    monkeypatch.setattr(I,'fill_up',lambda *a:pytest.fail('legacy early return must skip fill'))
    I.run()
    assert C.load_briefing()['intraday']['kept_prep']


def test_every_recorded_input_reference_resolves_to_actual_value():
    r=row(foreign_qty=10,inst_qty=None)
    t=O.SelectionTrace('intraday',P,'상승','fixture')
    I.screen([r],P,R,.5,'상승',trace=t)
    ob=t.records[r['code']]
    for ev in ob['criteria']:
        for key,ref in ev['input_refs'].items():
            value=ob
            for part in ref[len(ob['observation_id'])+1:].split('.'):
                value=value[int(part)] if isinstance(value,list) else value[part]
            assert value==ev['inputs'][key],(ev['criterion_id'],key,ref)
    # Completed close crossing must not reference the current intraday price.
    cross=next(e for e in ob['criteria'] if e['criterion_id']=='CLOSE_CROSS_MA20')
    assert '.cross_bars.1.close' in cross['input_refs']['price']


def test_version_upgrade_does_not_rewrite_previous_records(monkeypatch,isolated):
    t=O.SelectionTrace('prep',P,'상승','fixture');b=brief(D.screen([row()],P,True,'상승',trace=t));t.finish(b)
    path=isolated/'ontology/selections'/f'{t.id}.json'
    old=path.read_bytes()
    bundle=A.load_definition_bundle()
    for c in bundle['bindings']['criteria']: c['criterion_version_id']=c['criterion_version_id'].replace('@v1','@v2')
    for r in bundle['bindings']['rules']: r['rule_version_id']=r['rule_version_id'].replace('@v1','@v2')
    bundle['hash']=A.digest({'ir':bundle['ir'],'bindings':bundle['bindings']})
    monkeypatch.setattr(O,'load_definition_bundle',lambda:clone(bundle))
    nt=O.SelectionTrace('prep',P,'상승','fixture');nb=brief(D.screen([row()],P,True,'상승',trace=nt));nt.finish(nb)
    assert path.read_bytes()==old
    assert all(c['criterion_version_id'].endswith('@v2') for c in nt.records['000010']['criteria'])
    assert all(r['rule_version_id'].endswith('@v2') for r in nt.records['000010']['rules'])


def test_definition_disagreement_is_not_silently_used(monkeypatch):
    bundle=A.load_definition_bundle()
    bundle['ir']['ontology']['functions'][0]['expression']={'op':'literal','value':False}
    monkeypatch.setattr(O,'load_definition_bundle',lambda:clone(bundle))
    t=O.SelectionTrace('prep',P,'하락','fixture')
    out=D.screen([row()],P,True,'하락',trace=t)
    assert out # unchanged engine still passes
    assert t.errors
    ob=t.records['000010']
    ev=next(e for e in ob['criteria'] if e['criterion_id']=='PRICE_ABOVE_MA20')
    assert ev['status']=='unknown'


def test_source_absence_and_failure_do_not_become_day_flow_zero(monkeypatch):
    frames=pd.DataFrame({'외국인합계':[],'기관합계':[]},index=pd.DatetimeIndex([]))
    monkeypatch.setattr(D.stock,'get_market_trading_value_by_date',lambda *a:frames)
    info={};assert D.flows('000010',NOW.date(),5,info)==(0,0)
    assert all(v['value'] is None for v in info.values())
    def fail(*a):raise RuntimeError('fixture fail')
    monkeypatch.setattr(D.stock,'get_market_trading_value_by_date',fail)
    info={};assert D.flows('000010',NOW.date(),5,info)==(None,None)
    assert all(v['reason']=='RuntimeError' for v in info.values())


def test_same_period_sum_vs_mismatched_source_times():
    r=row()
    t=O.SelectionTrace('prep',P);ob=t.begin(r,r['close'])
    assert ob['flow_summary']['sum']==-1500000000
    r['_sources']['flow']['institution']['period']=['2026-11-04']
    t=O.SelectionTrace('prep',P);ob=t.begin(r,r['close'])
    assert ob['flow_summary']['sum'] is None
    assert ob['flow_summary']['available_snapshot_sum']==-1500000000
