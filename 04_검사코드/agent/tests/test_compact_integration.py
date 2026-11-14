"""Actual MCP producers/consumers, offline fixtures, and both wire contracts."""
import copy
import base64
import json
from pathlib import Path

import pytest

from test_ontology import C, D, M, O, P, R, row, brief, FakeBroker, isolated
from test_learning_proposals import source, item
import evidence_wire as W
import case_review as CR
import case_store as CS
import learning_proposals as L
import proposal_store as PS
import ai_judgment as J

pytestmark = pytest.mark.compact_wire


@pytest.fixture(autouse=True)
def attempt(isolated, monkeypatch):
    monkeypatch.setenv('RUN_MODE', 'decide')
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'compact-integration')


def unpack(payload):
    return [{**g['common'], **r, 'subject': g['subject']} for g in payload['groups'] for r in g['rows']]


@pytest.mark.parametrize('empty', [False, True])
def test_briefing_actual_mcp_and_semantics(monkeypatch, empty):
    b = brief([] if empty else [row()]); b['holdings'] = [row(1)]
    b['holdings'][0]['news'] = [{'date': '11-05', 'risk': True, 'title': '모의 뉴스: 기관 매도'}]
    C.write_json(C.BRIEFING, b)
    monkeypatch.setattr(M, '_broker', FakeBroker())
    monkeypatch.setattr(D, 'live_market', lambda *_: None)
    compact = M.get_briefing()
    assert compact.startswith('# 판단 자료\n'), compact
    payload = json.loads(compact.split('\n', 1)[1])
    records = unpack(payload)
    assert any(r['value'] == '모의 뉴스: 기관 매도' for r in records)
    assert payload['metadata']['candidate_count'] == (0 if empty else 1)
    assert payload['metadata']['holding_count'] == 1
    assert payload['metadata']['news_window_days'] == D.NEWS_DAYS
    assert '동시점' in str(payload['metadata']['notes'])
    assert '인버스' in str(payload['metadata']['policy_instructions'])
    bundle = W.load(payload['bundle_alias'])
    original = __import__('storage_objects').open_object(bundle['canonical_receipt_ref'])
    assert len(records) == len(original['evidence_catalog'])
    by_ref = {r['ref']: r for r in records}
    for key, entry in bundle['entries'].items():
        if entry['type'] != 'evidence': continue
        actual = next(e for e in original['evidence_catalog'] if e['evidence_id'] == entry['target'])
        shown = by_ref[payload['bundle_alias'] + ':' + key]
        for field in ('value', 'unit', 'kind', 'status', 'observed_at', 'available_at'):
            assert shown[field] == actual[field]
    monkeypatch.setenv(W.ENV, 'legacy')
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'legacy-comparison')
    legacy = M.get_briefing()
    assert '```jsonl' in legacy
    assert len(compact.encode()) < len(legacy.encode())


def test_account_values_once_and_execution_ids_preserved():
    payload = {'mode': 'mock', 'total': 99999, 'cash': 88888, 'stock_value': 11111,
               'holdings': [{'code': '005930', 'name': 'fixture', 'qty': 7, 'price': 20000,
                            'position_episode_id': 'episode-original', 'future_field': 'keep'}]}
    output = O.account_delivery(payload)
    rows = unpack(output)
    assert next(r for r in rows if r['field'] == 'cash')['value'] == 88888
    assert next(r for r in rows if r['field'] == 'position_episode_id')['value'] == 'episode-original'
    assert output['metadata']['holding_links'][0]['future_field'] == 'keep'
    assert 'evidence_catalog' not in output and 'holdings' not in output


@pytest.mark.parametrize('confidence', [.7, .1])
def test_canonical_judgment_keeps_quantity_and_zero_block_across_formats(monkeypatch, confidence):
    from test_judgment_integration import setup_run, submission as legacy_submission
    plans = []
    for wire in ('legacy', W.FORMAT):
        monkeypatch.setenv(W.ENV, wire)
        fake, _ = setup_run(monkeypatch)
        text = M.get_briefing()
        if wire == 'legacy':
            value = legacy_submission(text)
        else:
            payload = json.loads(text.split('\n', 1)[1])
            evidence = next(r for r in unpack(payload) if r['field'] == 'inst_net' and r['subject'] == '000001')
            value = {'request_key': 'first', 'code': '000001', 'recommended_action': '매수',
                'confidence_for': '매수', 'confidence_raw': confidence, 'decision': '모의 판단',
                'wire_format': W.FORMAT, 'bundle_refs': [payload['bundle_alias']], 'premises': [],
                'arguments': [{'id': 'inst', 'kind': 'fact_claim', 'proposer': 'bear', 'stance': 'oppose',
                    'adoption': 'reference', 'evidence': evidence['ref'], 'measurement_kind': evidence['kind'],
                    'assertion': {'op': 'sign', 'direction': 'sell'}}]}
        value['confidence_raw'] = confidence
        accepted = M.record_ai_judgments([value])[0]
        assert accepted['status'] == 'accepted', accepted
        plan = M.plan_order('000001', '매수', confidence, judgment_id=accepted['judgment_id'])
        plans.append({k: v for k, v in plan.items() if k not in ('plan_id', 'order_attempt_id', 'judgment_id', 'event_id')})
        assert not fake.orders
    assert plans[0] == plans[1]
    assert (plans[0]['qty'] > 0) == (confidence == .7)


def research_setup(monkeypatch):
    # Fixture construction is the historical source, not the AI's new input.
    monkeypatch.setenv(W.ENV, 'legacy')
    run, old_context, old_page = source(monkeypatch)
    monkeypatch.setenv(W.ENV, W.FORMAT)
    monkeypatch.setenv('RUN_MODE', 'proposal_review')
    monkeypatch.setenv('PROPOSAL_RUN_ID', run['proposal_run_id'])
    monkeypatch.setenv('PROPOSAL_ATTEMPT_ID', 'proposal-attempt-compact')
    return run, old_context, old_page


def test_review_schema_failure_preserves_compact_header(monkeypatch):
    run, _, _ = research_setup(monkeypatch)
    result = CR.record_case_review(run['review_id'], '', [], [], W.FORMAT, ['B17'])
    assert result['status'] == 'rejected'
    assert result['raw_submission']['bundle_refs'] == ['B17']
    assert result['original_hash'] == W.digest(result['raw_submission'])
    assert result['normalized_hash'] is None


def test_selected_proposal_case_detail_issues_visible_metric_refs(monkeypatch):
    run, _, _ = research_setup(monkeypatch)
    detail = M.get_proposal_cases(run['proposal_run_id'], detail_index=0)
    assert detail['wire_format'] == W.FORMAT, detail
    shown = unpack(detail)
    assert shown and all(row['application_id'] == 'a' for row in shown)
    assert W.public_document(detail)['item']['features']['institution_ratio'] == -.4
    assert W.load(detail['bundle_alias'])['case_ids'] == ['a']


@pytest.mark.parametrize('kind', ['proposal', 'coverage'])
def test_chunk_queries_filter_internal_snapshots_before_encoding(monkeypatch, kind):
    hidden = {'metric_snapshot': {'secret': 'SERVER_ONLY'}, 'evidence_catalog': [{'secret': 'SERVER_ONLY'}], 'value': 1}
    if kind == 'proposal':
        monkeypatch.setenv('RUN_MODE', 'proposal_review')
        monkeypatch.setenv('PROPOSAL_RUN_ID', 'proposal-chunk')
        monkeypatch.setattr(L, 'get_learning_proposals', lambda *a: {'proposals': [{'proposal_id': 'p', 'fact_claims': [hidden]}]})
        result = M.get_learning_proposals('proposal-chunk', detail_id='p', field='fact_claims')
    else:
        monkeypatch.setattr(M.JC, 'get_coverage', lambda *a: {'targets': [{'code': '000001', 'calls': [hidden]}]})
        result = M.get_coverage_detail('compact-integration', field='calls')
    assert result['complete'], result
    decoded = json.loads(base64.b64decode(result['data']))
    assert decoded == [{'value': 1}]


def test_wire_rejection_is_kept_in_runtime_tool_capture():
    result = M.get_evidence_detail('B999:E1', ['value'])
    assert result['errors'][0]['code'] == 'UNKNOWN_BUNDLE'
    import record_store
    captures = list((C.ontology_root() / 'runtime_inputs' / 'compact-integration' / 'tools').glob('*.json'))
    assert len(captures) == 1
    recorded = record_store.load(captures[0])['record']
    assert recorded['error'] == result and recorded['result'] == result


def test_proposals_actual_mcp_scope_replay_and_unseen_page(monkeypatch):
    run, old_context, old_page = research_setup(monkeypatch)
    rid = run['proposal_run_id']
    context = M.get_proposal_context(rid)
    assert context['wire_format'] == W.FORMAT, context
    assert context['document']['comparison']['summary']['refutation_rate'] == {'ref': context['bundle_alias'] + ':M1'}
    value = item(old_context, old_page)
    value.pop('delivery_ids')
    value.update(wire_format=W.FORMAT, bundle_refs=[context['bundle_alias']])
    value['fact_claims'][0]['reference'] = context['bundle_alias'] + ':M1'
    rejected = M.record_learning_proposals(rid, 'unseen', [value])
    assert rejected['items'][0]['error']['code'] == 'CASE_NOT_DELIVERED', rejected
    page = M.get_proposal_cases(rid)
    value['bundle_refs'].append(page['bundle_alias'])
    accepted = M.record_learning_proposals(rid, 'seen', [value])
    assert accepted['status'] == 'accepted', accepted
    assert 'raw_submission' not in accepted
    assert M.record_learning_proposals(rid, 'seen', [value])['idempotent'] is True
    pid = accepted['items'][0]['proposal_id']
    stored = PS.load('proposals', pid)
    assert stored['wire_submission']['original'] == value
    assert stored['active'] is False
    assert M.get_evidence_detail(context['bundle_alias'] + ':M1', ['denominator'])['wire_format'] == W.FORMAT
    monkeypatch.setenv('PROPOSAL_RUN_ID', 'wrong-scope')
    assert M.get_evidence_detail(context['bundle_alias'] + ':M1', ['denominator'])['errors'][0]['code'] == 'UNKNOWN_BUNDLE'


def test_case_review_readonly_helper_and_recorded_mcp_deliveries(monkeypatch):
    research_setup(monkeypatch)
    monkeypatch.setenv('RUN_MODE', 'case_review')
    monkeypatch.setenv('CASE_REVIEW_ID', 'research-one')
    monkeypatch.setenv('CASE_REVIEW_ATTEMPT_ID', 'case-attempt-compact')
    before = sorted(str(p) for p in Path(C.WORK).rglob('*'))
    raw = CR.get_case_review('research-one')
    assert sorted(str(p) for p in Path(C.WORK).rglob('*')) == before
    response = M.get_case_review('research-one')
    assert response['wire_format'] == W.FORMAT, response
    metric = next(r for r in unpack(response) if r['path'] == '/comparison/summary/refutation_rate')
    fact = {'id': 'rate', 'metric_id': metric['ref'], 'op': 'value', 'value': .5, 'unit': 'ratio'}
    hypotheses = [{'id': 'h', 'text': '미검증 사후 해석', 'finding_ids': ['rate'], 'case_ids': ['a']}]
    result = M.record_case_review('research-one', 'unseen', [fact], hypotheses, W.FORMAT, [response['bundle_alias']])
    assert result['status'] == 'rejected', result
    assert result['errors'][0]['code'] == 'CASE_NOT_DELIVERED'
    page = M.get_review_cases('research-one', limit=1)
    result = M.record_case_review('research-one', 'seen', [fact], hypotheses, W.FORMAT,
                                  [response['bundle_alias'], page['bundle_alias']])
    assert result['status'] == 'accepted', result
    assert 'raw_submission' not in result
    assert 'metric_snapshot' not in result['findings'][0]
    assert raw['metric_catalog'][0]['metric_id'] == 'rate'


def test_regime_returns_summary_and_keeps_internal_verification(monkeypatch):
    result = O.account_delivery({'regime': '하락', 'cash': 0})
    cash = next(r for r in unpack(result) if r['field'] == 'cash')
    out = M.set_regime('하락', '합성 사례', [cash['ref']])
    assert out['reference_validation']['status'] == 'valid', out
    assert 'evidence' not in out['reference_validation']
    assert out['reference_validation']['references'] == [cash['ref']]


def test_external_registration_idempotency_and_detail_permissions(monkeypatch):
    external = {'request_key': 'external', 'subject': '005930', 'text': '자료의 원문 인용',
                'url': 'https://fixture.invalid/story', 'title': '미검증 기사'}
    first = M.record_external_evidence([external])[0]
    assert first['status'] == 'recorded_unverified', first
    second = M.record_external_evidence([external])[0]
    assert first['evidence_ref'] == second['evidence_ref']
    assert second['idempotent'] is True
    monkeypatch.setenv('RUN_MODE', 'prompt_validation')
    assert M.get_evidence_detail(first['evidence_ref'], ['value'])['error_code'] == 'WIRE_TOOL_FORBIDDEN'
