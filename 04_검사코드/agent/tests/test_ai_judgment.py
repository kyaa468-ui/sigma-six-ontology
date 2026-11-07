"""Local immutable-receipt validation; model, network and real state are unused."""
import copy
import json
import socket
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import common as C
import ai_judgment as J

NOW = datetime.fromisoformat('2026-11-07T10:00:00+09:00')


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'WORK', str(tmp_path))
    monkeypatch.setattr(C, 'now', lambda: NOW)
    monkeypatch.setattr(C, 'conf_boost_details', lambda *a, **k: {'raw': a[2], 'adjusted': a[2], 'note': None}, raising=False)
    monkeypatch.setenv('RUN_ID', 'ai-test')
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'attempt-test')
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('Network forbidden'))
    return tmp_path


def receipt(**changes):
    fields = [('institution', -4_000_000_000, 'KRW', 'intraday_estimate'),
              ('foreign', 3_000_000_000, 'KRW', 'intraday_estimate'),
              ('candidate_positive_flow_ratio', .3, 'ratio', 'derived'),
              ('price', 20400, 'KRW', 'intraday_current'), ('ma20', 20000, 'KRW', 'daily_close'),
              ('gap', .02, 'ratio', 'derived'), ('missing', None, 'KRW', 'intraday_estimate'),
              ('zero', 0, 'KRW', 'intraday_estimate'),
              ('news_title', '기관은 오늘 매도했으며 전망은 엇갈린다.', 'text', 'news')]
    catalog = [{'evidence_id': name, 'subject': '005930', 'field': name, 'value': value, 'unit': unit,
                'kind': kind, 'status': 'unknown' if value is None else 'reported_unverified' if kind == 'news' else 'observed',
                'observation_id': 'obs-1', 'available_at': '2026-11-07T09:59:00+09:00', 'observed_at': None}
               for name, value, unit, kind in fields]
    row = {'delivery_id': 'delivery-1', 'ai_run_id': 'ai-test', 'run_attempt_id': 'attempt-test',
           'created_at': '2026-11-07T09:59:00+09:00', 'evidence_catalog': catalog,
           'internal_snapshot': {'unshown': 123}}
    row.update(changes)
    path = J._path('deliveries', row['delivery_id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, ensure_ascii=False))
    return row


def argument(eid='institution', op='sign', **assertion):
    return {'id': 'fact-1', 'kind': 'fact_claim', 'proposer': 'bear', 'stance': 'oppose', 'adoption': 'adopted',
            'evidence': {'delivery_id': 'delivery-1', 'evidence_id': eid},
            'measurement_kind': 'intraday_estimate', 'assertion': {'op': op, **assertion}}


def item(arg=None, key='request-1', **changes):
    row = {'request_key': key, 'code': '005930', 'recommended_action': '관망', 'confidence_for': '매수',
           'confidence_raw': .5, 'decision': '기관 매도 압력을 받아낼지 더 지켜본다.',
           'delivery_ids': ['delivery-1'], 'arguments': [arg or argument(direction='sell')]}
    row.update(changes)
    return row


def result(row):
    return J.record_ai_judgments([row])[0]


def assert_rejected(row, code):
    output = result(row)
    assert output['status'] == 'rejected', output
    assert output['errors'][0]['code'] == code, output


def test_correct_reference_wrong_direction_rejected_per_item():
    receipt()
    outputs = J.record_ai_judgments([item(argument(direction='buy')), item(key='correct')])
    assert outputs[0]['errors'][0]['code'] == 'FACT_MISMATCH'
    assert outputs[1]['status'] == 'accepted'
    stored = J.load_judgment(outputs[1]['judgment_id'])
    assert '순매도' in stored['arguments'][0]['server_summary']
    assert stored['arguments'][0]['reference_validation'] == 'passed'
    assert stored['decision_validation'] == 'submitted_unverified'


@pytest.mark.parametrize('value,unit,valid', [(-40, '억 원', True), (-4_000_000_000, 'KRW', True),
    (40, '억 원', False), (-4, '억 원', False), (-4000000000, 'share', False)])
def test_units_and_sign(value, unit, valid):
    receipt()
    output = result(item(argument(op='value', value=value, unit=unit)))
    assert output['status'] == ('accepted' if valid else 'rejected')


def test_ratio_percent_and_comparison_same_observation():
    receipt()
    ratio = argument('gap', 'value', value=2, unit='%')
    ratio['measurement_kind'] = 'derived'
    compare = argument('price', 'compare', operator='gt', right={'delivery_id': 'delivery-1', 'evidence_id': 'ma20'}, result=True)
    compare['measurement_kind'] = 'intraday_current'
    compare['id'] = 'compare'
    out = result(item(ratio, arguments=[ratio, compare]))
    assert out['status'] == 'accepted', out
    compare['assertion']['result'] = False
    assert_rejected(item(compare, key='bad'), 'FACT_MISMATCH')


def test_comparison_forbids_distinct_observations():
    r = receipt()
    r['evidence_catalog'][4]['observation_id'] = 'obs-2'
    receipt(**r)
    arg = argument('price', 'compare', operator='gt', right={'delivery_id': 'delivery-1', 'evidence_id': 'ma20'}, result=True)
    arg['measurement_kind'] = 'intraday_current'
    assert_rejected(item(arg), 'OBSERVATION_MISMATCH')


def test_missing_zero_and_close_laundering():
    receipt()
    assert_rejected(item(argument('missing', 'value', value=0, unit='KRW')), 'UNKNOWN_AS_ZERO')
    assert result(item(argument('missing', 'unknown'), key='unknown'))['status'] == 'accepted'
    assert result(item(argument('zero', 'value', value=0, unit='KRW'), key='zero'))['status'] == 'accepted'
    arg = argument('price', 'value', value=20400, unit='KRW')
    arg['measurement_kind'] = 'daily_close'
    assert_rejected(item(arg, key='launder'), 'MEASUREMENT_KIND_MISMATCH')


def test_ratio_does_not_imply_both_actors_buy():
    receipt()
    arg = argument('candidate_positive_flow_ratio', 'sign', direction='buy')
    arg['measurement_kind'] = 'derived'
    assert_rejected(item(arg), 'DIRECTION_NOT_APPLICABLE')
    # Even the correctly signed numeric ratio cannot be relabelled as signed net total.
    arg = argument('candidate_positive_flow_ratio', 'value', value=.3, unit='ratio')
    arg.update(measurement_kind='derived', field='simultaneous_total')
    assert_rejected(item(arg, key='sum'), 'MEASUREMENT_KIND_MISMATCH')


def test_precision_never_zeroes_sign():
    r = receipt()
    r['evidence_catalog'][0].update(value=-.001, precision=0)
    receipt(**r)
    assert_rejected(item(argument(direction='zero')), 'FACT_MISMATCH')
    assert result(item(argument(direction='sell'), key='sell'))['status'] == 'accepted'


def test_hypothesis_and_adoption_remain_unverified():
    receipt()
    hypothesis = {'id': 'h1', 'kind': 'hypothesis', 'proposer': 'bull', 'stance': 'support',
                  'adoption': 'reference', 'text': '외국인 매수세가 기관 매도 압력을 받아낼 것이다', 'claim_ids': ['fact-1']}
    row = item(arguments=[argument(direction='sell'), hypothesis])
    assert result(row)['status'] == 'accepted'
    other = copy.deepcopy(row)
    other['request_key'] = 'other'
    other['arguments'][0]['adoption'] = 'reference'
    result(other)
    judgments = J.get_ai_judgments(delivery_id='delivery-1')
    assert {x['arguments'][0]['adoption'] for x in judgments} == {'adopted', 'reference'}
    assert all(x['arguments'][1]['hypothesis_status'] == 'submitted_unverified' for x in judgments)
    assert judgments[0]['unmentioned_evidence']


def test_fact_prose_and_hidden_catalog_forbidden():
    receipt()
    arg = argument(direction='sell')
    arg['text'] = '기관 순매수'
    assert_rejected(item(arg), 'STRUCTURED_ASSERTION_REQUIRED')
    arg = argument('unshown', 'value', value=123, unit='KRW')
    assert_rejected(item(arg, key='hidden'), 'NOT_DELIVERED')


@pytest.mark.parametrize('mutate,code', [
    ({'run_attempt_id': 'old-attempt'}, 'ATTEMPT_MISMATCH'),
    ({'ai_run_id': 'old-run'}, 'RUN_MISMATCH'),
    ({'created_at': '2026-11-07T10:10:00+09:00'}, 'FUTURE_EVIDENCE')])
def test_delivery_boundaries(mutate, code):
    receipt(**mutate)
    assert_rejected(item(), code)


def test_wrong_subject_or_claimed_time_forbidden():
    receipt()
    assert_rejected(item(code='000660'), 'SUBJECT_MISMATCH')
    arg = argument(direction='sell')
    arg['evidence']['observed_at'] = '2026-11-07T09:59:00+09:00'
    assert_rejected(item(arg, key='time'), 'TIME_MISMATCH')


def test_source_quote_checks_only_actual_text():
    receipt()
    arg = {'id': 'quote1', 'kind': 'source_quote', 'proposer': 'bull', 'stance': 'neutral', 'adoption': 'reference',
           'evidence': {'delivery_id': 'delivery-1', 'evidence_id': 'news_title'}, 'quote': '전망은 엇갈린다'}
    out = result(item(arg))
    assert out['status'] == 'accepted'
    assert J.load_judgment(out['judgment_id'])['arguments'][0]['source_truth'] == 'unverified'
    arg['quote'] = 'reported_unverified'
    assert_rejected(item(arg, key='metadata'), 'QUOTE_MISMATCH')


def test_external_evidence_never_launders_into_fact():
    ext = J.record_external_evidence([{'request_key': 'ext', 'subject': '005930', 'text': '기관 매수 가능성이 있다', 'url': 'https://example.test/article'}])[0]
    arg = {'id': 'q', 'kind': 'source_quote', 'proposer': 'bull', 'stance': 'support', 'adoption': 'reference',
           'evidence': {'delivery_id': ext['delivery_id'], 'evidence_id': ext['evidence_id']}, 'quote': '기관 매수 가능성'}
    output = result(item(arg, delivery_ids=[ext['delivery_id']]))
    assert output['status'] == 'accepted'
    arg = {'id': 'f', 'kind': 'fact_claim', 'proposer': 'bull', 'stance': 'support', 'adoption': 'adopted',
           'evidence': arg['evidence'], 'measurement_kind': 'external_unverified',
           'assertion': {'op': 'value', 'value': '기관 매수 가능성이 있다', 'unit': 'text'}}
    assert_rejected(item(arg, key='launder', delivery_ids=[ext['delivery_id']]), 'UNVERIFIED_SOURCE')


def test_premise_explicit_horizon_reuse_and_proposal():
    receipt()
    premise = {'definition_id': 'MA20_MAINTENANCE@v1', 'days': 4, 'direction': 'above', 'claim_ids': ['fact-1']}
    out = result(item(premises=[premise]))
    assert out['status'] == 'accepted'
    application = J.load_judgment(out['judgment_id'])['premise_applications'][0]
    assert application['definition_snapshot']['above_refuted_when'] == 'close < ma20'
    assert application['end_trading_day'] is None
    reuse = result(item(key='reuse', premises=[{'application_id': application['application_id']}]))
    assert reuse['premise_application_ids'] == out['premise_application_ids']
    assert_rejected(item(key='edit', premises=[{'application_id': application['application_id'], 'days': 6}]), 'IMMUTABLE_PREMISE')
    del premise['days']
    assert_rejected(item(key='missing', premises=[premise]), 'EXPLICIT_HORIZON_REQUIRED')
    proposal = result(item(key='proposal', premises=[{'text': '기관 매도를 외국인이 받아낸다', 'claim_ids': ['fact-1']}]))
    assert proposal['premise_proposal_ids']


def test_idempotency_snapshot_and_safe_identifiers():
    receipt()
    source = item()
    first = result(source)
    second = result(source)
    assert second['idempotent'] and second['judgment_id'] == first['judgment_id']
    source['decision'] = 'changed'
    assert_rejected(source, 'IDEMPOTENCY_CONFLICT')
    assert J.load_judgment('../account') is None
    arg = argument(direction='sell')
    arg['evidence']['delivery_id'] = '../account'
    assert_rejected(item(arg, key='path', delivery_ids=['../account']), 'INVALID_ID')


def test_storage_error_never_accepts_or_leaves_visible_judgment(monkeypatch):
    receipt()
    archive = J._archive
    def fail_event(kind, identifier, value):
        if kind == 'submissions':
            raise OSError('disk full')
        return archive(kind, identifier, value)
    monkeypatch.setattr(J, '_archive', fail_event)
    output = result(item())
    assert output['status'] == 'recording_error'
    assert J.get_ai_judgments() == []


def test_reference_validation_market_only_without_code():
    r = receipt()
    ref = {'delivery_id': 'delivery-1', 'evidence_id': 'institution'}
    assert J.validate_references([ref])['status'] == 'rejected'
    assert J.validate_references([ref], code='005930')['status'] == 'valid'


def test_rejected_key_is_not_silently_rewritten():
    receipt()
    bad = item(argument(direction='buy'))
    rejected = result(bad)
    assert rejected['errors'][0]['reference_validation'] == 'passed'
    assert rejected['errors'][0]['fact_validation'] == 'failed'
    assert_rejected(item(), 'IDEMPOTENCY_CONFLICT')
    good = result(item(key='corrected'))
    assert good['status'] == 'accepted'
    events = [json.loads(p.read_text()) for p in (Path(C.WORK) / 'ontology' / 'submissions').glob('*.json')]
    assert [e['status'] for e in events].count('rejected') == 2
    assert [e['status'] for e in events].count('accepted') == 1


def test_history_and_ai_classification_value_scope():
    r = receipt()
    r['evidence_catalog'][0].update(subject='market', field='ai_regime', value='하락', unit='text',
                                   kind='ai_classification', status='reported_unverified')
    receipt(**r)
    arg = argument(op='value', value='하락', unit='text')
    arg['measurement_kind'] = 'ai_classification'
    out = result(item(arg))
    assert out['status'] == 'accepted'
    fact = J.load_judgment(out['judgment_id'])['arguments'][0]
    assert fact['source_truth'] == 'unverified'
    assert fact['verified_scope'] == 'source_assertion_only'
    assert '시장 사실 미검증' in fact['server_summary']


def test_derived_fact_summary_preserves_temporal_limit():
    r = receipt()
    r['evidence_catalog'][0].update(field='flow_summary.available_snapshot_sum', kind='derived',
                                   temporal_scope='received_values_only', reason='provider_time_not_provided')
    receipt(**r)
    arg = argument(op='value', value=-40, unit='억원')
    arg['measurement_kind'] = 'derived'
    output = result(item(arg))
    assert '동시점 합계 아님' in output['fact_summaries'][0]


def test_python_rounding_and_arbitrary_fact_fields():
    r = receipt()
    r['evidence_catalog'][0].update(value=2, precision=0)
    receipt(**r)
    assert result(item(argument(op='value', value=2.5, unit='KRW')))['status'] == 'accepted'
    arg = argument(op='value', value=2, unit='KRW')
    arg['anything_else'] = '기관 매도 없음'
    assert_rejected(item(arg, key='prose'), 'STRUCTURED_ASSERTION_REQUIRED')


def test_snapshot_unchanged_after_delivery_and_settings_changes(monkeypatch):
    receipt()
    first = result(item())
    before = J.load_judgment(first['judgment_id'])
    r = receipt()
    r['evidence_catalog'][0]['value'] = 1
    receipt(**r)
    monkeypatch.setattr(C, 'conf_boost_details', lambda *args: {'adjusted': .99})
    after = J.load_judgment(first['judgment_id'])
    assert before == after
    assert after['arguments'][0]['evidence_snapshots'][0]['value'] == -4_000_000_000


def test_english_direction_normalized_and_trading_days_supported():
    r = receipt()
    r['evidence_catalog'][0].update(subject='account', field='held_days', value=2, unit='trading_day', kind='account')
    receipt(**r)
    arg = argument(op='value', value=2, unit='trading_day')
    arg['measurement_kind'] = 'account'
    output = result(item(arg, confidence_for='buy', recommended_action='watch'))
    assert output['status'] == 'accepted'
    stored = J.load_judgment(output['judgment_id'])
    assert stored['confidence_for'] == '매수' and stored['recommended_action'] == '관망'


def test_nan_and_infinity_cannot_enter_accepted_records():
    receipt()
    assert result(item(confidence_raw=float('nan')))['status'] == 'rejected'
    assert result(item(key='inf', confidence_raw=float('inf')))['status'] == 'rejected'


def test_rejected_retry_deduplicates_and_retains_original_assertion():
    receipt()
    bad = item(argument(direction='buy'))
    first = result(bad)
    second = result(bad)
    assert second['idempotent'] and second['submission_id'] == first['submission_id']
    events = list((Path(C.WORK) / 'ontology' / 'submissions').glob('*.json'))
    assert len(events) == 1
    stored = json.loads(events[0].read_text())
    assert stored['submitted_payload']['arguments'][0]['assertion']['direction'] == 'buy'
    assert stored['request_hash'] == J._digest(bad)


def test_storage_error_signals_coverage_even_if_submission_storage_fails(monkeypatch):
    import judgment_coverage as coverage
    calls = []
    monkeypatch.setattr(coverage, 'recording_error', lambda *a, **k: calls.append((a, k)), raising=False)
    receipt()
    monkeypatch.setattr(J, '_archive', lambda *args: (_ for _ in ()).throw(OSError('full')))
    assert result(item())['status'] == 'recording_error'
    assert calls and calls[0][0][0] == 'record_ai_judgments'


def test_actor_quantity_direction_and_reported_precision():
    r = receipt()
    r['evidence_catalog'][0].update(field='inst_qty', value=-225000, unit='share', kind='intraday_current')
    r['evidence_catalog'][5].update(value=20400 / 20000 - 1, precision=3,
                                   display_precision={'unit': 'percent', 'decimals': 1})
    receipt(**r)
    quantity = argument(direction='sell')
    quantity['measurement_kind'] = 'intraday_current'
    gap = argument('gap', 'value', value=2, unit='percent')
    gap.update(id='gap-claim', measurement_kind='derived')
    assert result(item(arguments=[quantity, gap]))['status'] == 'accepted'
