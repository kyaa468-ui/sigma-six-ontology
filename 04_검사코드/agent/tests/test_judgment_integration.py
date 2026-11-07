"""Actual MCP/Hook/runner paths, all market data and subprocesses local fakes."""
import importlib.util
import io
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_ontology import C, D, M, O, B, P, R, ROOT, FIX, clone, row, brief, isolated
import ai_judgment as J
import judgment_coverage as JC


class LocalBroker:
    name = 'fake'
    def __init__(self, holdings=()):
        self.account_value = {'total': 50_000_000, 'cash': 49_000_000, 'stock_value': 1_000_000, 'holdings': list(holdings)}
        self.orders = []
    def account(self):
        C.write_json(C.ACCOUNT, self.account_value)
        return clone(self.account_value)
    def quote_info(self, code):
        return {'price': 20400, 'change': .02, 'source': 'local-fixture', 'observed_at': None, 'available_at': O.stamp()}
    def order(self, code, name, side, qty):
        self.orders.append((code, side, qty))
        return {'ok': True, 'qty': qty, 'price': 20400, 'filled': True}


def setup_run(monkeypatch, held=False):
    h = {'code': '000001', 'name': 'fixture', 'qty': 30, 'avg_price': 20000, 'price': 20400, 'value': 612000}
    fake = LocalBroker([h] if held else [])
    monkeypatch.setattr(M, '_broker', fake)
    monkeypatch.setattr(D, 'live_market', lambda b: None)
    b = brief([row(code='000001', inst_net=-4_000_000_000)])
    C.write_json(C.BRIEFING, b)
    C.write_json(C.ACCOUNT, fake.account_value)
    manifest = JC.start_attempt('offline-ai-run', 'decide', b, fake.account_value)
    monkeypatch.setenv('RUN_ATTEMPT_ID', manifest['run_attempt_id'])
    monkeypatch.setenv('RUN_MODE', 'decide')
    return fake, manifest


def submission(text, direction='sell', key='first', side='매수', action='매수'):
    did = text.split('브리핑 전달 ID: ')[-1].splitlines()[0]
    receipt = json.loads((Path(C.WORK)/'ontology/deliveries'/f'{did}.json').read_text())
    e = next(e for e in receipt['evidence_catalog'] if e['subject']=='000001' and e['field']=='inst_net')
    return {'request_key': key, 'code': '000001', 'recommended_action': action, 'confidence_for': side,
            'confidence_raw': .7, 'decision': '모의 판단', 'delivery_ids': [did], 'premises': [],
            'arguments': [{'id': 'inst', 'kind': 'fact_claim', 'proposer': 'bear', 'stance': 'oppose', 'adoption': 'reference',
                           'evidence': {'delivery_id': did, 'evidence_id': e['evidence_id']}, 'measurement_kind': e['kind'],
                           'assertion': {'op': 'sign', 'direction': direction}}]}


def watch(jid=None, side='매수', action='관망'):
    return M.record_decision('000001', action, .7, 'bull', 'bear', 'local only', judgment_id=jid, confidence_for=side)


def test_actual_briefing_wrong_direction_and_corrected_judgment(monkeypatch):
    setup_run(monkeypatch)
    text = M.get_briefing()
    bad = M.record_ai_judgments([submission(text, 'buy')])[0]
    assert bad['status']=='rejected' and bad['errors'][0]['code']=='FACT_MISMATCH'
    item = submission(text, key='corrected')
    item['arguments'].append({'id':'h', 'kind':'hypothesis', 'proposer':'bull', 'stance':'support', 'adoption':'adopted',
                              'text':'매수세가 매도 압력을 받아낼 것이다', 'claim_ids':['inst']})
    good = M.record_ai_judgments([item])[0]
    assert good['status']=='accepted'
    assert watch(good['judgment_id'])=={'ok':True}
    cov = M.get_judgment_coverage()
    assert cov['counts']['linked']==1 and len(cov['rejection_history'])==1
    stored = M.get_ai_judgments(code='000001')['judgments'][0]
    assert stored['arguments'][1]['hypothesis_status']=='submitted_unverified'
    assert stored['decision_validation']=='submitted_unverified'
    assert cov['targets'][0]['calls'][0]['results'][0]['boost']['decision']['raw']==.7


@pytest.mark.parametrize('mode,expected', [('absent','not_submitted'), ('rejected','rejected'), ('missing','accepted_unlinked'), ('wrong','accepted_unlinked'), ('valid','linked')])
def test_recording_status_never_changes_order_result(monkeypatch, mode, expected):
    fake, manifest = setup_run(monkeypatch)
    jid = None
    if mode != 'absent':
        result = M.record_ai_judgments([submission(M.get_briefing(), 'buy' if mode=='rejected' else 'sell')])[0]
        if mode=='valid': jid = result['judgment_id']
        if mode=='wrong': jid = 'nonexistent'
    planned = M.plan_order('000001', '매수', .7, judgment_id=jid)
    assert planned['qty'] > 0
    order = M.place_order('000001', '매수', planned['qty'], .7, 'bull', 'bear', 'local', judgment_id=jid,
                          plan_id=planned['plan_id'], order_attempt_id=planned['order_attempt_id'])
    assert order['ok'] and len(fake.orders)==1
    cov = M.get_judgment_coverage(manifest['run_attempt_id'])
    assert cov['counts'][expected]==1
    assert len(cov['targets'][0]['calls'])==2
    if mode=='valid':
        boosts = cov['targets'][0]['calls'][1]['results'][0]['boost']
        assert boosts['risk']['confidence']['adjusted']==boosts['order_logging']['adjusted']


def test_account_delivery_and_held_two_directions(monkeypatch):
    setup_run(monkeypatch, held=True)
    account = M.get_account()
    assert account['recording_status']=='recorded'
    assert any(e['field']=='qty' and e['subject']=='000001' for e in account['evidence_catalog'])
    text = M.get_briefing()
    sell = M.record_ai_judgments([submission(text, side='매도', action='보유')])[0]
    assert watch(sell['judgment_id'], '매도', '보유')['ok']
    buy = M.record_ai_judgments([submission(text, key='add-on', action='보유')])[0]
    assert watch(buy['judgment_id'], '매수', '보유')['ok']
    cov = M.get_judgment_coverage()
    assert cov['expected_count']==2 and cov['counts']['linked']==2
    # Legacy record_decision still computes its action-based sell boost for '보유'.
    events = [t['calls'][0] for t in cov['targets'] if t['confidence_for']=='매수']
    assert events[0]['results'][0]['boost']['decision']['side']=='매도'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.object(C, 'load_env', lambda: None): spec.loader.exec_module(module)
    return module


def test_hook_block_is_visible_without_server_or_submission(monkeypatch):
    setup_run(monkeypatch)
    hook = load_module('test_hook', ROOT/'.claude/hooks/risk_check.py')
    event = {'tool_name': 'mcp__kiwoom__place_order', 'tool_input': {'code':'000001','side':'매수','qty':0,'confidence':.7}}
    monkeypatch.setattr(hook.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(event).encode())))
    monkeypatch.setattr(B, 'cached_quote', lambda code:20400)
    monkeypatch.setattr(B, 'cached_change', lambda code:0)
    with pytest.raises(SystemExit): hook.main()
    cov=M.get_judgment_coverage()
    assert cov['counts']['not_submitted']==1
    call=cov['targets'][0]['calls'][0]
    assert call['stage']=='hook' and call['results'][0]['outcome']['status']=='blocked'


def test_zero_plan_and_broker_failure_can_still_link(monkeypatch):
    fake,_=setup_run(monkeypatch)
    good=M.record_ai_judgments([submission(M.get_briefing())])[0]['judgment_id']
    monkeypatch.setattr(M.sizing,'plan_buy',lambda *a,**k:{'qty':0, 'limited_by':'local_zero'})
    planned = M.plan_order('000001','매수',.7,judgment_id=good)
    assert planned['qty']==0
    assert watch(good)['ok']
    assert M.get_judgment_coverage()['counts']['linked']==1
    monkeypatch.setattr(fake,'order',lambda *a,**k:{'ok':False,'error':'local_failure'})
    # Real risk policy still runs; use one share within legacy limits.
    res=M.place_order('000001','매수',1,.7,'b','s','d',judgment_id=good,
                      plan_id=planned['plan_id'],order_attempt_id=planned['order_attempt_id'])
    assert not res['ok']
    assert M.get_judgment_coverage()['counts']['linked']==1


def test_runner_regime_only_success_is_not_coverage_success(monkeypatch):
    setup_run(monkeypatch)
    runner=load_module('ontology_test_runner', ROOT/'run.py')
    seen=[]
    class Process:
        returncode=0
        def __init__(self,cmd,**kw):
            seen.append(kw['env']['RUN_ATTEMPT_ID'])
            manifest=Path(C.WORK)/'ontology/attempts'/f"{seen[-1]}.json"
            assert manifest.exists(), 'denominator must exist before AI launch'
        def communicate(self,timeout): return json.dumps({'result':'상승 / 매수 0건 / 매도 0건 / 관망 1건'}), ''
    monkeypatch.setattr(runner.subprocess,'Popen',Process)
    monkeypatch.setattr(runner,'did_work',lambda *a:True) # existing regime-only success rule
    assert runner.claude('decide','local')
    assert runner.claude('decide','local')
    assert len(set(seen))==2
    for aid in seen:
        cov=JC.get_coverage(aid)
        assert cov['counts']['not_submitted']==1 and cov['run_status']=='normal'


def test_coverage_write_failure_preserves_legacy_watch(monkeypatch):
    setup_run(monkeypatch)
    monkeypatch.setattr(JC,'_write',lambda *a,**k: (_ for _ in ()).throw(OSError('fixture disk failure')))
    assert watch()['ok']
    cov=M.get_judgment_coverage()
    assert cov['recording_status']=='recording_error' and cov['linked_rate'] is None


def test_confidence_details_match_frozen_engine():
    ns=dict(vars(C))
    exec(json.loads((FIX/'baseline_conf_boost.json').read_text())['source'],ns)
    for side,raw,rsi,regime,bad in itertools.product(['매수','매도','invalid'],[0,.6,.955,1.1],[None,20,50,85],['하락','상승'],[True,False]):
        b=brief([row(code='000001',rsi14=rsi,news=[{'risk':bad}])])
        old=ns['conf_boost']('000001',side,raw,P,b,regime)
        assert C.conf_boost('000001',side,raw,P,b,regime)==old
        d=C.conf_boost_details('000001',side,raw,P,b,regime)
        assert (d['adjusted'],d['note'])==old


def test_later_audit_exception_cannot_leave_false_full_coverage(monkeypatch):
    setup_run(monkeypatch)
    jid=M.record_ai_judgments([submission(M.get_briefing())])[0]['judgment_id']
    watch(jid)
    assert M.get_judgment_coverage()['linked_rate']==1
    monkeypatch.setattr(JC,'enter',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('audit extension failed')))
    assert watch(jid)['ok']
    cov=M.get_judgment_coverage()
    assert cov['recording_status']=='recording_error' and cov['linked_rate'] is None


@pytest.mark.parametrize('failure',['timeout','launch_error'])
def test_runner_failure_finalizes_coverage(monkeypatch,failure):
    setup_run(monkeypatch)
    runner=load_module('ontology_failure_runner',ROOT/'run.py')
    class Process:
        pid=123
        def __init__(self,*a,**k):
            if failure=='launch_error': raise OSError('local launch failure')
        def communicate(self,timeout):
            if timeout==25*60: raise runner.subprocess.TimeoutExpired('local',timeout)
            return '', ''
    monkeypatch.setattr(runner.subprocess,'Popen',Process)
    monkeypatch.setattr(runner.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    assert runner.claude('decide','local') is False
    rec=C.read_jsonl(C.RUNS)[-1]
    assert rec['judgment_coverage']['run_status']==('timeout' if failure=='timeout' else 'error')
    assert rec['judgment_coverage']['counts']['not_submitted']==1


def test_registered_mcp_tool_schemas_expose_optional_links():
    import asyncio
    schemas={t.name:t.inputSchema for t in asyncio.run(M.mcp.list_tools())}
    assert {'record_ai_judgments','get_ai_judgments','record_external_evidence','get_judgment_coverage',
            'get_execution_trace','refresh_execution_status'} <= schemas.keys()
    for tool in ('plan_order','place_order','record_decision'):
        assert 'judgment_id' in schemas[tool]['properties']
        assert 'judgment_id' not in schemas[tool].get('required',[])
    assert {'plan_id','order_attempt_id','rule_trigger_id'} <= schemas['place_order']['properties'].keys()


def test_hook_cache_error_is_recorded_before_denial(monkeypatch):
    setup_run(monkeypatch)
    hook=load_module('cache_error_hook',ROOT/'.claude/hooks/risk_check.py')
    event={'tool_name':'mcp__kiwoom__place_order','tool_input':{'code':'000001','side':'매수','qty':1,'confidence':.7}}
    monkeypatch.setattr(hook.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(event).encode())))
    monkeypatch.setattr(B,'cached_change',lambda code:(_ for _ in ()).throw(KeyError('malformed cache')))
    with pytest.raises(KeyError): hook.main() # __main__ retains existing deny-on-error handling
    cov=M.get_judgment_coverage()
    assert cov['counts']['not_submitted']==1
    assert cov['targets'][0]['calls'][0]['results'][0]['outcome']['status']=='error'
