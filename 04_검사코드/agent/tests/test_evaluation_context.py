"""Step 4 context cannot change legacy tuning or infer a judgment/order link."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from test_ontology import C, M, ROOT, clone, isolated
from test_execution_integration import planned
import execution_trace as E
import judgment_coverage as JC
import evaluation_context as EC
import tuning


def legacy_outcomes():
    baseline = read_stored(ROOT / 'tests/fixtures/ontology/step4/baseline.json')
    scope = {}
    exec(baseline['legacy_tuning_source'], scope)
    return scope['outcomes']


def save_snapshot(snapshot):
    for name, key in [('quotes.json', 'quotes'), ('prep_rows.json', 'prep_rows'), ('briefing.json', 'briefing'), ('account.json', 'account')]:
        C.write_json(str(Path(C.WORK) / name), snapshot[key])


def test_legacy_outcome_boundaries_equal_frozen_baseline():
    snapshot = {'today': C.today().isoformat(), 'quotes': {}, 'prep_rows': [],
                'briefing': {'asof': '2026-11-05', 'candidates': []}, 'account': {}}
    rows = []
    for action in ('관망', '보유', '매수', '매도'):
        for price in (9700, 9799, 9800, 9999, 10000, 10001, 10199, 10200, 10201):
            code = f'{action}-{price}'
            rows.append({'kind': 'decision' if action in ('관망', '보유') else 'fill', 'mode': 'decide',
                         'action': action, 'side': action, 'time': '2026-11-05 10:30', 'code': code,
                         'name': code, 'price': 10000, 'confidence': .7})
            snapshot['prep_rows'].append({'code': code, 'closes': [price]})
    rows += [dict(rows[0], code='today', time='2026-11-06 10:00'),
             dict(rows[0], code='view', mode='view'), dict(rows[0], code='missing'),
             dict(rows[0], code='zero', price=0), dict(rows[0], confidence=.8)]
    save_snapshot(snapshot)
    expected = legacy_outcomes()(clone(rows))
    audit = {}
    assert tuning.outcomes(clone(rows)) == expected
    assert tuning.outcomes(clone(rows), snapshot=clone(snapshot), audit=audit) == expected
    assert {'today_or_future', 'not_legacy_outcome_target', 'superseded_same_day_action', 'missing_or_zero_price'} <= {x['reason'] for x in audit['excluded']}
    buys_zero = next(x for x in audit['evaluations'] if x['code'] == '매수-10000')
    assert buys_zero['verdict'] == '틀림 (조정)'
    assert next(x for x in audit['evaluations'] if x['code'] == '매도-10000')['verdict'] == '중립'


def test_legacy_priority_and_snapshot_is_current_reference(monkeypatch):
    # Legacy quote selection deliberately uses the host date from timestamp.
    stamp = datetime(2026, 11, 6, 10, 30).timestamp()
    snapshot = {'today': '2026-11-06', 'quotes': {'a': {'price': 110, 'ts': stamp}},
                'account': {'holdings': [{'code': 'a', 'price': 105}, {'code': 'b', 'price': 106}]},
                'briefing': {'asof': '2026-11-05', 'candidates': [{'code': c, 'price': 107} for c in 'abc']},
                'prep_rows': [{'code': c, 'closes': [100, 108]} for c in 'abcd']}
    rows = [{'kind': 'decision', 'mode': 'decide', 'time': '2026-11-04 10:30', 'code': c,
             'name': c, 'action': '관망', 'confidence': .7} for c in 'abcd']
    save_snapshot(snapshot)
    C.write_json(str(Path(C.WORK) / 'params.json'), {'competition': {'start': '2026-01-01'}})
    monkeypatch.setattr(C, 'CONFIG', C.WORK)
    for row in rows:
        C.append_jsonl(C.JOURNAL, row)
    frozen = EC.capture_legacy_context('2026-10-01', C.now().isoformat())
    assert frozen['recording_status'] == 'recorded'
    assert frozen['snapshot_role'] == 'current_snapshot_reference'
    assert frozen['inputs']['today'] == '2026-11-06'
    assert frozen['decision_outcomes'] == legacy_outcomes()(rows)
    assert [x['p1'] for x in frozen['audit']['evaluations']] == [110, 106, 107, 108]
    assert all(x['p0'] == 100 for x in frozen['audit']['evaluations'])
    assert frozen['definition']['sha256'] and frozen['sources']['journal']['sha256']
    C.write_json(C.BRIEFING, {'candidates': []})
    assert tuning.outcomes(frozen['inputs']['selected_journal'], snapshot=frozen['inputs']) == frozen['decision_outcomes']
    assert not frozen['legacy_trade_quality']['net_pnl_verified']


def test_legacy_partial_sell_keeps_behavior_and_quality_limit(monkeypatch):
    snapshot = {'today': '2026-11-06', 'quotes': {}, 'prep_rows': [], 'briefing': {},
                'account': {'holdings': [{'code': '000001', 'price': 110, 'avg_price': 100, 'pnl_pct': .09}]}}
    save_snapshot(snapshot)
    C.write_json(str(Path(C.WORK) / 'params.json'), {'competition': {'start': '2026-01-01'}})
    monkeypatch.setattr(C, 'CONFIG', C.WORK)
    rows = [{'kind': 'fill', 'code': '000001', 'name': 'local', 'side': side, 'qty': qty, 'price': price,
             'time': when} for side, qty, price, when in
            [('매수', 100, 100, '2026-11-04 10:30'), ('매도', 40, 110, '2026-11-05 10:30')]]
    for row in rows:
        C.append_jsonl(C.JOURNAL, row)
    frozen = EC.capture_legacy_context('2026-11-05', C.now().isoformat())
    assert frozen['legacy_trade_results'] == tuning.trade_results(rows, snapshot['account'])
    assert [r['status'] for r in frozen['legacy_trade_results']] == ['청산', '보유 중']
    assert frozen['legacy_trade_quality']['quality'] == 'legacy_reference_only'
    assert not frozen['legacy_trade_quality']['net_pnl_verified']


def test_absent_optional_legacy_files_are_empty_but_corrupt_files_are_errors():
    first = EC.capture_legacy_context('2026-11-06', C.now().isoformat())
    assert first['recording_status'] == 'recorded'
    assert first['decision_outcomes']['summary'] == {}
    assert 'journal' in first['missing_optional_sources']
    assert first['sources']['journal']['status'] == 'optional_source_absent'
    Path(C.JOURNAL).write_text('{invalid')
    broken = EC.capture_legacy_context('2026-11-06', C.now().isoformat())
    assert broken['recording_status'] == 'recording_error'
    assert any(x['reason'] == 'source_read_error' for x in broken['errors'])
    assert 'journal' not in broken['missing_optional_sources']


def observation(aid, qty=40, number='one', *, available_at=None, created_at=None):
    return E.record('execution_observation', order_attempt_id=aid,
                    available_at=available_at or C.now().isoformat(),
                    **({'created_at': created_at} if created_at else {}),
                    execution={'key': ['fake', '2026-11-06', 'KRX', aid, number],
                               'order_no': aid, 'execution_no': number, 'code': '000001', 'side': '매수',
                               'qty': qty, 'price': 20400, 'confirmation': 'broker_execution'})


def no_write(monkeypatch):
    def fail(*a, **k):
        raise AssertionError('Read query attempted a mutation or database initialization')
    monkeypatch.setattr(JC, 'recording_error', fail)
    monkeypatch.setattr(E, 'record', fail)
    monkeypatch.setattr(E, '_connect', fail)


def test_exact_attempt_confirmed_40_unconfirmed_60_and_late_evidence(monkeypatch):
    fake, _, _, args = planned(monkeypatch)
    args['qty'] = 100
    assert M.place_order(**args)['ok']
    observation(args['order_attempt_id'])
    # Provider fill time could be old: availability controls what was known.
    observation(args['order_attempt_id'], 60, 'two', available_at='2026-11-06T11:00:00+09:00')
    no_write(monkeypatch)
    result = EC.execution_context([args['judgment_id']], C.now().isoformat())
    assert result['recording_status'] == 'recorded', result['errors']
    assert len(result['orders']) == 1
    order = result['orders'][0]
    assert order['financial_attribution']
    assert (order['summary']['requested_qty'], order['summary']['confirmed_qty'], order['summary']['unconfirmed_qty']) == (100, 40, 60)
    later = EC.execution_context([args['judgment_id']], '2026-11-06T11:01:00+09:00')
    assert later['orders'][0]['summary']['confirmed_qty'] == 100


def test_same_judgment_multiple_orders_not_summed(monkeypatch):
    fake, _, _, args = planned(monkeypatch)
    args['qty'] = 100
    M.place_order(**args)
    observation(args['order_attempt_id'], 40)
    plan2 = M.plan_order('000001', '매수', .7, judgment_id=args['judgment_id'])
    second = {**args, 'qty': 20, 'order_attempt_id': plan2['order_attempt_id'], 'plan_id': plan2['plan_id']}
    M.place_order(**second)
    observation(second['order_attempt_id'], 20)
    result = EC.execution_context([args['judgment_id']], C.now().isoformat())
    assert len(result['orders']) == 2
    assert sorted((o['summary']['requested_qty'], o['summary']['confirmed_qty']) for o in result['orders']) == [(20, 20), (100, 40)]


def test_valid_id_with_wrong_original_score_never_gets_financial_attribution(monkeypatch):
    _, _, _, args = planned(monkeypatch)
    args['confidence'] = .8
    M.place_order(**args)
    observation(args['order_attempt_id'])
    result = EC.execution_context([args['judgment_id']], C.now().isoformat())
    assert not result['orders']
    assert result['unlinked_references'][0]['reason'] == 'confidence_raw_mismatch'
    assert not result['unlinked_references'][0]['financial_attribution']


def test_later_good_replay_cannot_backfill_original_unlinked_call(monkeypatch):
    _, _, _, args = planned(monkeypatch)
    jid = args['judgment_id']
    args['judgment_id'] = None
    M.place_order(**args)
    # A later valid linked call with this attempt is a replay, not a new send.
    call = JC.enter('place_order', '000001', '매수', .7, jid)
    E.record('tool_result', stage='place_order', judgment_id=jid, code='000001',
             coverage_event_id=call, order_attempt_id=args['order_attempt_id'], result={'replayed': True})
    result = EC.execution_context([jid], C.now().isoformat())
    assert not result['orders']
    assert result['unlinked_references'] and not result['unlinked_references'][0]['financial_attribution']


def test_hook_only_and_rule_trigger_are_context_not_financial_result(monkeypatch):
    fake, _, _, args = planned(monkeypatch)
    trigger = E.record('rule_trigger', matched_rule='STOP_LOSS@v1', comparisons=[{'price': 90, 'stop_price': 95}])
    call = JC.enter('hook', args['code'], args['side'], args['confidence'], args['judgment_id'])
    E.record('hook_entry', order_attempt_id=args['order_attempt_id'], judgment_id=args['judgment_id'],
             coverage_event_id=call, payload={**args, 'rule_trigger_id': trigger['event_id']})
    result = EC.execution_context([args['judgment_id']], C.now().isoformat())
    assert result['orders'][0]['link_basis'] == 'hook_only_original_call'
    assert not result['orders'][0]['financial_attribution']
    assert result['orders'][0]['summary']['confirmed_qty'] is None
    assert result['rule_context'][0]['role'] == 'rule_trigger_context_not_ai_correctness'
    assert not fake.orders


def test_corrupt_records_reported_without_mutating_coverage(monkeypatch):
    folder = Path(C.WORK) / 'ontology/executions/local/broken'
    folder.mkdir(parents=True)
    (folder / 'broken.json').write_text('{invalid')
    before = {str(p): p.read_bytes() for p in Path(C.WORK).rglob('*') if p.is_file()}
    no_write(monkeypatch)
    result = EC.execution_context(['not-real'], C.now().isoformat())
    assert result['recording_status'] == 'recording_error' and result['errors']
    assert before == {str(p): p.read_bytes() for p in Path(C.WORK).rglob('*') if p.is_file()}
