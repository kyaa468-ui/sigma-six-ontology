"""Local-only contracts for the evidence actually displayed to an AI invocation."""
import copy
import hashlib
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

# Reuse the existing isolated/offline harness and its fixed source fixtures.
from test_ontology import isolated, row, brief, C, D, I, O, B, M, P, R


def receipt(tmp_path, delivery_id):
    return json.loads((tmp_path / 'ontology' / 'deliveries' / f'{delivery_id}.json').read_text())


def item(catalog, subject, field, **where):
    return next(e for e in catalog if e['subject'] == subject and e['field'] == field
                and all(e.get(k) == v for k, v in where.items()))


def test_display_catalog_is_the_receipt_whitelist(isolated, monkeypatch):
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'attempt-offline-evidence')
    candidate = row()
    candidate['secret_internal_note'] = 'not a displayed fact'
    candidate['news'] = [{'date': '11-05', 'title': '기업 실적 발표', 'risk': False,
                          'published_at': '2026-11-05T15:00:00+09:00', 'url': 'https://example.invalid/article',
                          'available_at': '2026-11-06T08:30:00+09:00'}]
    held = row(1)
    held['rsi14_live'] = 36.2
    held['_sources']['rsi14_live'] = {'calculation_version': 'live-rsi14@v1', 'window': 14,
                                     'closes': [100, 102, 101], 'price': {'value': 103, 'observed_at': None},
                                     'calculated_at': '2026-11-06T10:29:00+09:00'}
    b = brief([candidate])
    b['holdings'] = [held]
    b['market'].update(close=2500, us=[{'symbol': '^IXIC', 'close': 20000, 'chg': -.02, 'date': '2026-11-05'}])
    quotes = [{'instrument_id': candidate['code'], 'observation_id': 'quote-exact', 'price': 19800, 'change': -.01,
               'kind': 'intraday_current', 'source': 'cached:quote', 'available_at': '2026-11-06T10:28:00+09:00'}]
    output = O.deliver('human briefing', b, quotes, P, R,
                       context={'lessons': ['과거 교훈'], 'prior_judgments': '지난 판단', 'regime': '하락'})
    stored = json.loads(next((isolated / 'ontology' / 'deliveries').glob('*.json')).read_text())
    assert stored['schema_version'] == 2
    assert stored['run_attempt_id'] == 'attempt-offline-evidence'
    assert stored['ai_run_id'] == 'offline-ai-run'
    assert stored['text'] == output
    assert stored['sha256'] == hashlib.sha256(output.encode()).hexdigest()
    rendered = [json.loads(line) for line in output.split('```jsonl\n', 1)[1].split('\n```', 1)[0].splitlines()]
    assert rendered == stored['evidence_catalog']
    assert len({e['evidence_id'] for e in rendered}) == len(rendered)
    assert not any('secret_internal_note' in e['field'] for e in rendered)
    assert item(rendered, held['code'], 'rsi14_live')['kind'] == 'intraday_estimate'
    assert item(rendered, candidate['code'], 'price', scope='briefing_quote')['available_at'] == quotes[0]['available_at']
    assert item(rendered, candidate['code'], 'price', scope='briefing_quote')['observed_at'] is None
    assert item(rendered, candidate['code'], 'news.0.title')['status'] == 'reported_unverified'
    assert item(rendered, 'market', 'ai_regime')['kind'] == 'ai_classification'
    assert item(rendered, 'history', 'prior_judgments')['status'] == 'reported_unverified'
    assert item(rendered, 'config', 'params.decision.buy_min_confidence')['unit'] == 'ratio'
    # Actual RSI inputs are in the immutable snapshot, not invented from synthetic dates.
    assert stored['briefing_snapshot']['holdings'][0]['_sources']['rsi14_live']['closes'] == [100, 102, 101]


def test_observed_zero_missing_and_selection_price_are_separate(isolated):
    candidate = row(foreign_net=0, inst_net=None)
    candidate['_sources']['flow']['foreign']['value'] = 0
    candidate['_sources']['flow']['institution'].update(value=None, status='unknown', reason='api_failure')
    trace = O.SelectionTrace('prep', P)
    trace.begin(candidate, 20400)
    trace.attach([candidate])
    b = brief([candidate])
    catalog = O.briefing_catalog('example', b, [], P, R)
    assert item(catalog, candidate['code'], 'foreign')['value'] == 0
    assert item(catalog, candidate['code'], 'foreign')['status'] == 'observed'
    assert item(catalog, candidate['code'], 'institution')['value'] is None
    assert item(catalog, candidate['code'], 'institution')['status'] == 'unknown'
    assert item(catalog, candidate['code'], 'price')['comparison_id'] == item(catalog, candidate['code'], 'ma20')['comparison_id']
    assert item(catalog, candidate['code'], 'price')['value'] == 20400


def test_actual_briefing_gap_accepts_displayed_percent_after_float_calculation(isolated, monkeypatch):
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'attempt-gap-from-actual-receipt')
    candidate = row(ma20=20000, close=20400)
    trace = O.SelectionTrace('prep', P)
    trace.begin(candidate, 20400)
    trace.attach([candidate])
    b = brief([candidate])
    trace.finish(b)
    C.write_json(C.BRIEFING, b)
    monkeypatch.setattr(M, '_broker', SimpleNamespace(quote_info=lambda code: {
        'price': 19800, 'change': -.01, 'source': 'local_fixture', 'available_at': O.stamp()}))
    text = M.get_briefing()
    did = text.split('브리핑 전달 ID: ')[-1].splitlines()[0]
    catalog = receipt(isolated, did)['evidence_catalog']
    gap = item(catalog, candidate['code'], 'price_comparison.gap')
    assert gap['value'] == 20400 / 20000 - 1  # binary float remains the actual calculation
    assert gap['precision'] == 3  # the human display is +2.0%, not an exact decimal representation
    submission = {'request_key': 'gap-percent', 'code': candidate['code'], 'recommended_action': '관망',
                  'confidence_for': '매수', 'confidence_raw': .5, 'decision': '테스트 판단', 'delivery_ids': [did],
                  'arguments': [{'id': 'gap', 'kind': 'fact_claim', 'proposer': 'bull', 'stance': 'support',
                                 'adoption': 'reference', 'measurement_kind': 'derived',
                                 'evidence': {'delivery_id': did, 'evidence_id': gap['evidence_id']},
                                 'assertion': {'op': 'value', 'value': 2, 'unit': '%'}}]}
    out = M.record_ai_judgments([submission])[0]
    assert out['status'] == 'accepted', out


def test_account_keeps_payload_and_archives_exact_delivered_values(isolated):
    payload = {'total': 10000000, 'cash': 9000000, 'stock_value': 1000000, 'daily_pnl': -.01,
               'regime': '하락', 'equity_cap': .6, 'mode': 'local_fake',
               'holdings': [{'code': '000660', 'name': '테스트', 'qty': 10, 'price': 100000, 'avg_price': 110000,
                             'value': 1000000, 'pnl_pct': -.09, 'stop': None, 'held_days': 1}]}
    before = copy.deepcopy(payload)
    delivered = O.account_delivery(payload)
    assert payload == before
    for key, value in payload.items():
        assert delivered[key] == value
    stored = receipt(isolated, delivered['delivery_id'])
    assert stored['payload'] == delivered
    expected = json.dumps(O.clean(delivered), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    assert stored['sha256'] == hashlib.sha256(expected.encode()).hexdigest()
    assert item(delivered['evidence_catalog'], '000660', 'stop')['status'] == 'unknown'
    assert item(delivered['evidence_catalog'], '000660', 'qty')['unit'] == 'share'


def test_account_recording_failure_is_not_reported_complete(isolated, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('simulated local disk failure')
    monkeypatch.setattr(O, 'archive', fail)
    out = O.account_delivery({'cash': 42, 'holdings': []})
    assert out['cash'] == 42
    assert out['recording_status'] == 'error'
    assert out['recording_error']['code'] == 'RECORDING_ERROR'


def test_live_rsi_records_actual_inputs_without_an_extra_quote(isolated, monkeypatch):
    candidate = row()
    candidate['closes'] = [20000 + i * 10 for i in range(60)]
    candidate.pop('activity', None)
    held = copy.deepcopy(candidate)
    b = brief([candidate])
    b['holdings'] = [held]
    C.write_json(C.BRIEFING, b)
    calls = []
    def quote(code):
        calls.append(code)
        return {'price': 20400, 'change': .01, 'source': 'fake:local', 'observed_at': None,
                'available_at': '2026-11-06T10:29:30+09:00'}
    monkeypatch.setattr(B, 'get', lambda: SimpleNamespace(name='live', quote_info=quote))
    I.live_rsi()
    updated = C.read_json(C.BRIEFING)
    rs = updated['candidates'][0]['_sources']['rsi14_live']
    assert calls == [candidate['code']]
    assert rs['closes'] == candidate['closes']
    assert rs['price']['value'] == 20400
    assert rs['price']['observed_at'] is None
    assert rs['price']['available_at'] == '2026-11-06T10:29:30+09:00'
    assert rs['result'] == I.rsi_now(candidate['closes'], 20400)
    assert updated['holdings'][0]['_sources']['rsi14_live'] == rs
    assert 'synthetic' in rs['index_policy']


def test_news_keeps_source_url_publication_and_receipt_time(isolated, monkeypatch):
    monkeypatch.setenv('NAVER_CLIENT_ID', 'local-placeholder')
    monkeypatch.setenv('NAVER_CLIENT_SECRET', 'local-placeholder')
    raw = {'items': [{'title': '테스트 실적 발표', 'pubDate': 'Thu, 05 Nov 2026 15:00:00 +0900',
                      'originallink': 'https://example.invalid/original', 'link': 'https://example.invalid/naver'}]}
    monkeypatch.setattr(D.urllib.request, 'urlopen', lambda *args, **kwargs: BytesIO(json.dumps(raw).encode()))
    result = D.news([{'code': '000660', 'name': '테스트'}])['000660'][0]
    assert result['date'] == '11-05'
    assert result['published_at'] == '2026-11-05T15:00:00+09:00'
    assert result['url'] == 'https://example.invalid/original'
    assert result['available_at'] == O.stamp()


def test_legacy_snapshot_never_gains_historical_observation_time(isolated):
    legacy = row()
    legacy.pop('_sources')
    catalog = O.briefing_catalog('legacy', brief([legacy]), [], {}, {})
    close = item(catalog, legacy['code'], 'close')
    assert close['observed_at'] is None
    assert close['available_at'] is None
    assert close['observation_id'].startswith('legacy:candidates:')
