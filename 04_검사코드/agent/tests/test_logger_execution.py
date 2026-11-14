"""Public projections preserve execution links and label balance reconciliation."""


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
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import common as C
import execution_trace as E
import logger


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('Network forbidden in logger tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(C, 'WORK', str(tmp_path))
    monkeypatch.setattr(C, 'ACCOUNT', str(tmp_path / 'account.json'))
    monkeypatch.setattr(C, 'now', lambda: datetime.fromisoformat('2026-11-06T15:35:00+09:00'))
    monkeypatch.setattr(logger, 'FIXES', {})
    monkeypatch.setenv('RUN_ID', 'logger-test')
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'attempt-logger')
    return tmp_path


def fill(qty=10, side='매수', **extra):
    return {'kind':'fill', 'time':'2026-11-06 10:01:00', 'code':'000001', 'name':'시험종목',
            'side':side, 'qty':qty, 'price':100, 'amount':qty*100, 'confidence':.8,
            'bull':'시험 매수 주장', 'bear':'시험 매도 주장', 'decision':'시험 판단', **extra}


def account(qty):
    C.write_json(C.ACCOUNT, {'observed_at':'2026-11-06T15:30:00+09:00',
                            'holdings':[{'code':'000001', 'qty':qty}], 'cash':1000000})


def archives(root):
    return sorted((root/'ontology/executions').glob('*/balance_reconciliation/*.json'))


def test_public_trade_and_decision_keep_exact_existing_links(isolated):
    refs={'judgment_id':'judgment-1', 'plan_id':'plan-1', 'order_attempt_id':'order-1',
          'position_episode_id':'position-1', 'order_no':'00000017',
          'execution_quality':'balance_delta_estimate', 'run_attempt_id':'attempt-original'}
    rows=[fill(**refs)]; original=copy.deepcopy(rows); account(10)
    public=logger.trades(rows)[0]
    assert {key:public[key] for key in refs}==refs
    assert rows==original and archives(isolated)==[]
    assert {key:logger.run_items(rows,{})[0][key] for key in refs}==refs
    decision={'kind':'decision','action':'관망',**{k:v for k,v in rows[0].items() if k!='kind'}}
    assert logger.run_items([decision],{})[0]['judgment_id']=='judgment-1'


def test_balance_correction_is_estimate_with_immutable_before_after_and_dedup(isolated,monkeypatch):
    refs={'judgment_id':'judgment-1', 'plan_id':'plan-1', 'order_attempt_id':'order-1',
          'position_episode_id':'position-1', 'order_no':'00000017',
          'execution_quality':'balance_delta_estimate', 'run_attempt_id':'attempt-original'}
    rows=[fill(5, ordered=10, **refs)]; original=copy.deepcopy(rows); account(10)
    public=logger.trades(rows)[0]
    assert (public['qty'],public['amount'])==(10,1000)
    assert public['execution_quality']=='balance_delta_estimate'
    assert public['reconciliation_quality']=='balance_reconciled_estimate'
    assert rows==original and 'ordered' not in public
    files=archives(isolated); assert len(files)==1
    before_bytes=files[0].read_bytes(); event=read_stored(files[0])
    assert event['before']=={'qty':5,'price':100,'amount':500}
    assert event['after']=={'qty':10,'price':100,'amount':1000}
    assert event['source_account']['holdings'][0]['qty']==10
    assert event['order_attempt_id']=='order-1' and event['journal_unchanged']
    assert event['execution_quality']=='balance_reconciled_estimate'
    monkeypatch.setenv('RUN_ATTEMPT_ID','attempt-later')
    again=logger.trades(rows)[0]
    assert public['reconciliation_event_id']==again['reconciliation_event_id']
    assert archives(isolated)==files and files[0].read_bytes()==before_bytes
    trace=E.get_execution_trace(order_attempt_id='order-1')
    assert any(x['kind']=='balance_reconciliation' for x in trace['events'])


@pytest.mark.parametrize('rows,held,expected',[
    ([fill(223,ordered=223),fill(43,'매도',ordered=112),fill(56,'매도',ordered=56),fill(7,'매도',ordered=7)],48,[223,112,56,7]),
    ([fill(5)],10,[10]),
    ([fill(10),fill(2,'매도')],5,[10,5]),
    ([fill(10)],0,[10]),  # 기존 q>0 조건: 0으로 강제 변경하지 않음
    ([fill(10),fill(10,'매도')],0,[10,10]),
])
def test_legacy_numeric_reconciliation_is_unchanged(isolated,rows,held,expected):
    account(held)
    result=logger.trades(rows)
    assert [x['qty'] for x in result]==expected
    assert [x['amount'] for x in result]==[qty*100 for qty in expected]
    for row in result:
        assert not any(key in row for key in logger.TRACE_FIELDS)
    for path in archives(isolated):
        event=read_stored(path)
        assert 'order_attempt_id' not in event and 'position_episode_id' not in event
        assert event['execution_quality']!='confirmed'


def test_recording_failure_preserves_legacy_numbers_without_success_label(isolated,monkeypatch):
    account(10)
    monkeypatch.setattr(E,'_archive',lambda *a,**kw:(_ for _ in ()).throw(OSError('disk full')))
    public=logger.trades([fill(5,ordered=10)])[0]
    assert (public['qty'],public['amount'])==(10,1000)
    assert public['reconciliation_recording_status']=='recording_error'
    assert 'reconciliation_event_id' not in public
    assert archives(isolated)==[]


def test_no_balance_difference_creates_no_reconciliation(isolated):
    account(10)
    public=logger.trades([fill(10)])[0]
    assert 'reconciliation_quality' not in public and archives(isolated)==[]
