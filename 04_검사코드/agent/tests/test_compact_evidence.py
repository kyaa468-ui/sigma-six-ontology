"""Compact protocol contracts using real stores/checkers and synthetic values."""
import copy
import json
import os
import subprocess
import sys
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_ai_judgment import C, J, NOW, isolated, receipt, item, argument
import evidence_wire as W
import record_store as R
import storage_objects as O
from prompt_validation import FrozenTools

pytestmark = pytest.mark.compact_wire


@pytest.fixture(autouse=True)
def compact_context(isolated, monkeypatch):
    monkeypatch.setenv('RUN_MODE', 'decide')


def delivery(fields=None):
    row = receipt()
    row['delivery_id'] = 'compact-original'
    row['kind'] = 'briefing'
    row['evidence_catalog'][0]['snippet'] = 'additional source passage'
    prepared = W.prepare(row['evidence_catalog'], fields=fields)
    row.update(W.receipt_fields(prepared))
    row['text'] = json.dumps(prepared['payload'], ensure_ascii=False)
    J._archive('deliveries', row['delivery_id'], row)
    W.commit(prepared, row, output=row['text'])
    return row, prepared['payload']


def submission(payload, **changes):
    value = item()
    value.pop('delivery_ids')
    value.update(wire_format=W.FORMAT, bundle_refs=[payload['bundle_alias']])
    value['arguments'][0]['evidence'] = payload['bundle_alias'] + ':E1'
    value.update(changes)
    return value


def rows(payload):
    return [{**g['common'], **r, 'subject': g['subject']} for g in payload['groups'] for r in g['rows']]


def test_production_default_and_invalid_format(monkeypatch):
    monkeypatch.delenv(W.ENV)
    assert W.selected_format() == W.FORMAT
    monkeypatch.setenv(W.ENV, 'future')
    with pytest.raises(W.WireError, match='UNSUPPORTED_WIRE_FORMAT'):
        W.selected_format()


def test_roundtrip_preserves_values_semantics_and_types():
    original, payload = delivery()
    shown = rows(payload)
    assert len(shown) == len(original['evidence_catalog'])
    for source, row in zip(original['evidence_catalog'], shown):
        for key in ('value', 'unit', 'kind', 'status', 'observed_at', 'available_at'):
            assert row[key] == source[key]
        assert W.resolve(row['ref'], 'evidence')[0] == {
            'delivery_id': original['delivery_id'], 'evidence_id': source['evidence_id']}
    assert shown[6]['value'] is None and shown[7]['value'] == 0
    assert 'evidence_id' not in json.dumps(payload)
    assert W.resolve(payload['observations'][0]['ref'], 'observation')[0]['observation_id'] == 'obs-1'
    with pytest.raises(W.WireError, match='REFERENCE_TYPE_MISMATCH'):
        W.resolve(payload['observations'][0]['ref'], 'evidence')


def test_small_negative_missing_substitution_and_distinct_times_remain_visible():
    original = receipt()
    original['delivery_id'] = 'compact-edge-values'
    first, second = original['evidence_catalog'][:2]
    first.update(value=-0.000001, precision=0, observed_at='2026-11-07T09:57:00+09:00',
                 calculation='quantity * selection_price', period={'start': '2026-11-07', 'end': '2026-11-07'})
    second['observed_at'] = '2026-11-07T09:58:00+09:00'
    original['evidence_catalog'][6].update(effective_value=0, substituted=True, reason='provider_missing')
    prepared = W.prepare(original['evidence_catalog'])
    original.update(W.receipt_fields(prepared))
    J._archive('deliveries', original['delivery_id'], original)
    W.commit(prepared, original)
    shown = rows(prepared['payload'])
    assert shown[0]['value'] < 0 and shown[0]['observed_at'] != shown[1]['observed_at']
    assert shown[0]['period'] == first['period'] and shown[0]['kind'] == 'intraday_estimate'
    assert shown[6]['value'] is None and shown[6]['effective_value'] == 0 and shown[6]['substituted']
    assert shown[7]['value'] == 0 and shown[7]['status'] == 'observed'
    assert J.record_ai_judgments([submission(prepared['payload'])])[0]['status'] == 'accepted'
    wrong = submission(prepared['payload'], request_key='incorrect-zero')
    wrong['arguments'][0]['assertion']['direction'] = 'zero'
    assert J.record_ai_judgments([wrong])[0]['status'] == 'rejected'


@pytest.mark.parametrize('op,values,accepted', [
    ('sign', {'direction': 'sell'}, True), ('sign', {'direction': 'buy'}, False),
    ('value', {'value': -40, 'unit': '억 원'}, True),
    ('value', {'value': -40, 'unit': 'share'}, False)])
def test_same_checker_results_and_original_submission(op, values, accepted, monkeypatch):
    original, payload = delivery()
    legacy = item(argument(op=op, **values), key='legacy')
    legacy['delivery_ids'] = [original['delivery_id']]
    legacy['arguments'][0]['evidence']['delivery_id'] = original['delivery_id']
    compact = submission(payload, request_key='compact')
    compact['arguments'][0]['assertion'] = {'op': op, **values}
    monkeypatch.setenv(W.ENV, 'legacy')
    old = J.record_ai_judgments([legacy])[0]
    monkeypatch.setenv(W.ENV, W.FORMAT)
    new = J.record_ai_judgments([compact])[0]
    assert old['status'] == new['status'] == ('accepted' if accepted else 'rejected'), (old, new)
    if accepted:
        saved = J.load_judgment(new['judgment_id'])
        assert saved['wire_submission']['original'] == compact
        assert saved['wire_submission']['original_hash'] == W.digest(compact)
        assert saved['arguments'][0]['evidence']['delivery_id'] == original['delivery_id']
        assert old['fact_summaries'] == new['fact_summaries']
    else:
        assert old['errors'][0]['code'] == new['errors'][0]['code']


def test_compare_right_and_observation_resolve():
    _, payload = delivery()
    value = submission(payload)
    arg = value['arguments'][0]
    arg.update(evidence='B1:E4', measurement_kind='intraday_current')
    arg['assertion'] = {'op': 'compare', 'operator': 'gt', 'right': 'B1:E5', 'result': True}
    value['comparison_context'] = 'B1:O1'
    result = J.record_ai_judgments([value])[0]
    assert result['status'] == 'accepted', result
    saved = J.load_judgment(result['judgment_id'])
    assert saved['comparison_context']['observation_id'] == 'obs-1'
    assert saved['arguments'][0]['assertion']['right']['evidence_id'] == 'ma20'


@pytest.mark.parametrize('change,code', [
    ('missing', 'UNKNOWN_BUNDLE'), ('type', 'REFERENCE_TYPE_MISMATCH'),
    ('undeclared', 'BUNDLE_NOT_DECLARED'), ('malformed', 'INVALID_SHORT_REFERENCE')])
def test_bad_aliases_rejected(change, code):
    _, payload = delivery()
    value = submission(payload)
    if change == 'missing': value['bundle_refs'] = ['B99']
    elif change == 'type': value['arguments'][0]['evidence'] = 'B1:O1'
    elif change == 'undeclared': value['arguments'][0]['evidence'] = 'B2:E1'
    else: value['arguments'][0]['evidence'] = 'E1'
    result = J.record_ai_judgments([value])[0]
    assert result['status'] == 'rejected', result
    assert result['errors'][0]['code'] == code


def test_other_attempt_and_mode_cannot_resolve(monkeypatch):
    delivery()
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'other')
    with pytest.raises(W.WireError, match='UNKNOWN_BUNDLE'):
        W.resolve('B1:E1', 'evidence')
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'attempt-test')
    monkeypatch.setenv('RUN_MODE', 'case_review')
    monkeypatch.setenv('CASE_REVIEW_ID', 'review-one')
    with pytest.raises(W.WireError, match='UNKNOWN_BUNDLE'):
        W.resolve('B1:E1', 'evidence')


def test_quote_hidden_until_detail_delivered_and_map_immutable():
    _, payload = delivery(fields=['value'])
    before = W.digest(W.load('B1'))
    value = submission(payload)
    value['arguments'][0] = {'id': 'q', 'kind': 'source_quote', 'proposer': 'bull',
        'stance': 'support', 'adoption': 'reference', 'evidence': 'B1:E1', 'quote': 'additional source passage'}
    rejected = J.record_ai_judgments([value])[0]
    assert rejected['errors'][0]['code'] == 'QUOTE_MISMATCH'
    details = W.detail('B1:E1', ['snippet'])
    assert details['bundle_alias'] == 'B2'
    assert W.digest(W.load('B1')) == before
    value.update(request_key='detail', bundle_refs=['B2'])
    value['arguments'][0]['evidence'] = 'B2:E1'
    accepted = J.record_ai_judgments([value])[0]
    assert accepted['status'] == 'accepted', accepted
    # Only the single newly delivered item is in this submission's display scope.
    assert len(J.load_judgment(accepted['judgment_id'])['unmentioned_evidence']) == 0


def test_retry_rejected_changed_and_concurrent_acceptance():
    _, payload = delivery()
    value = submission(payload)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda _: J.record_ai_judgments([value])[0], range(4)))
    assert all(r['status'] == 'accepted' for r in outputs), outputs
    assert len({r['judgment_id'] for r in outputs}) == 1
    changed = copy.deepcopy(value); changed['confidence_raw'] = .6
    assert J.record_ai_judgments([changed])[0]['errors'][0]['code'] == 'IDEMPOTENCY_CONFLICT'
    bad = submission(payload, request_key='bad')
    bad['arguments'][0]['assertion']['direction'] = 'buy'
    first = J.record_ai_judgments([bad])[0]
    assert first['status'] == 'rejected'
    assert J.record_ai_judgments([bad])[0]['idempotent'] is True
    bad['arguments'][0]['assertion']['direction'] = 'sell'
    assert J.record_ai_judgments([bad])[0]['errors'][0]['code'] == 'IDEMPOTENCY_CONFLICT'


def test_uncommitted_reservation_never_citable_or_reused(monkeypatch):
    reserved = W.reserve(W.scope())
    assert reserved == 'B1'
    with pytest.raises(W.WireError, match='UNKNOWN_BUNDLE'):
        W.load(reserved)
    assert W.reserve(W.scope()) == 'B2'


def test_multi_process_allocation(isolated):
    script = ('import sys; import common as C; import evidence_wire as W; '
              'C.WORK=sys.argv[1]; print(W.reserve(W.scope()))')
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    def allocate(_):
        return subprocess.check_output([sys.executable, '-c', script, str(isolated)], env=env, text=True).strip()
    with ThreadPoolExecutor(max_workers=4) as pool:
        aliases = list(pool.map(allocate, range(8)))
    assert set(aliases) == {f'B{i}' for i in range(1, 9)}


def test_corruption_is_explicit():
    delivery()
    path = W.root() / 'bundles' / 'B1.json'
    path.write_text('{}')
    with pytest.raises(W.WireError, match='WIRE_INTEGRITY_ERROR'):
        W.resolve('B1:E1', 'evidence')


def test_frozen_only_uses_delivered_archived_maps(monkeypatch):
    original, payload = delivery()
    stored = W.freeze_receipt(original, NOW)
    request = {'receipts': [stored], 'target': {'code': '005930', 'confidence_for': '매수',
                'run_id': 'ai-test', 'run_attempt_id': 'attempt-test'}, 'as_of': NOW.isoformat()}
    frozen = FrozenTools(request)
    value = submission(payload)
    assert frozen.call('record_ai_judgments', {'items': [value]})[0]['errors'][0]['code'] == 'NOT_DELIVERED'
    assert frozen.call('get_briefing')['delivery'] == original['text']
    # Even if a live lookup fails, the frozen map and source closure suffice.
    monkeypatch.setattr(W, 'root', lambda *a, **k: pytest.fail('live map read'))
    value['request_key'] = 'after-read'
    assert frozen.call('record_ai_judgments', {'items': [value]})[0]['status'] == 'accepted'
    assert frozen.call('get_evidence_detail', {'ref': 'B1:E1', 'fields': ['snippet']})['status'] == 'unavailable'


def test_unknown_usage_remains_unmeasured():
    import runtime_inputs
    result = runtime_inputs.usage_observation({'usage': {'input_tokens': 20}})
    assert result['provider_usage'] == {'input_tokens': 20}
    assert 'cache_read_input_tokens' in result['missing_fields']


def test_format_is_pinned_until_next_attempt(monkeypatch):
    delivery()
    monkeypatch.setenv(W.ENV, 'legacy')
    with pytest.raises(W.WireError, match='ATTEMPT_WIRE_FORMAT_CHANGED'):
        W.pin_format()
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'next-attempt')
    assert W.pin_format() == 'legacy'


def test_storage_failure_does_not_return_citable_bundle(monkeypatch):
    source = {'mode': 'mock', 'cash': 42}
    import ontology
    real = W.commit
    monkeypatch.setattr(W, 'commit', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    result = ontology.account_delivery(source)
    assert result['citable'] is False and 'bundle_alias' not in result
    with pytest.raises(W.WireError, match='UNKNOWN_BUNDLE'):
        W.load('B1')
    monkeypatch.setattr(W, 'commit', real)
    assert ontology.account_delivery(source)['bundle_alias'] == 'B2'


def test_external_failure_retry_finishes_original_alias(monkeypatch):
    external = {'request_key': 'external', 'subject': '005930', 'url': 'https://fixture.invalid', 'text': '원문'}
    real = W.commit
    monkeypatch.setattr(W, 'commit', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    first = J.record_external_evidence([external])[0]
    assert first['status'] == 'recording_error', first
    monkeypatch.setattr(W, 'commit', real)
    retried = J.record_external_evidence([external])[0]
    assert retried['evidence_ref'] == 'B1:E1' and retried['idempotent'] is True
    assert len(list((W.root() / 'reservations').glob('B*.json'))) == 1


def test_frozen_detail_visibility_and_future_response(monkeypatch):
    original, payload = delivery(fields=['value'])
    later = NOW + timedelta(minutes=1)
    monkeypatch.setattr(C, 'now', lambda: later)
    details = W.detail('B1:E1', ['snippet'])
    archived = W.freeze_receipt(original, later)
    context = {'code': '005930', 'confidence_for': '매수', 'run_id': 'ai-test', 'run_attempt_id': 'attempt-test'}
    frozen = FrozenTools({'receipts': [archived], 'target': context, 'as_of': NOW.isoformat()})
    frozen.call('get_briefing')
    denied = frozen.call('get_evidence_detail', {'ref': 'B1:E1', 'fields': ['snippet']})
    assert denied['errors'][0]['code'] == 'FUTURE_EVIDENCE'
    frozen = FrozenTools({'receipts': [archived], 'target': context, 'as_of': later.isoformat()})
    frozen.call('get_briefing')
    assert frozen.call('get_evidence_detail', {'ref': 'B1:E1', 'fields': ['snippet']}) == details
    value = submission(details, request_key='detail-only')
    value['arguments'][0] = {'id': 'q', 'kind': 'source_quote', 'proposer': 'bull',
        'stance': 'support', 'adoption': 'reference', 'evidence': 'B2:E1', 'quote': 'additional source passage'}
    assert frozen.call('record_ai_judgments', {'items': [value]})[0]['status'] == 'accepted'


def test_frozen_direct_entry_cannot_read_live_map(monkeypatch):
    original, payload = delivery()
    context = {'code': '005930', 'confidence_for': '매수', 'run_id': 'ai-test', 'run_attempt_id': 'attempt-test'}
    monkeypatch.setattr(W, 'root', lambda *a, **k: pytest.fail('live map lookup'))
    result = J.validate_frozen_submission(submission(payload), {original['delivery_id']: original}, context, NOW.isoformat())
    assert result['errors'][0]['code'] == 'NOT_DELIVERED'


def test_frozen_missing_bundle_is_recorded_as_unavailable():
    original, _ = delivery()
    frozen = FrozenTools({'receipts': [original], 'as_of': NOW.isoformat()})
    result = frozen.call('get_briefing')
    assert result == {'status': 'unavailable', 'reason': 'original_wire_bundle_not_archived'}
    assert len(frozen.calls) == 1 and frozen.calls[0]['result'] == result
    assert not frozen.delivered and not frozen.wire_bundles


def test_trace_reader_verifies_research_wire_commits(monkeypatch):
    import trace_query
    monkeypatch.setenv('RUN_MODE', 'case_review')
    monkeypatch.setenv('CASE_REVIEW_ID', 'review-wire')
    monkeypatch.setenv('CASE_REVIEW_ATTEMPT_ID', 'review-wire-attempt')
    W.pin_format()
    W.reserve(W.scope())
    reader = trace_query.Reader(C.now())
    rows = reader.scan('case_review/evidence_wire')
    assert len(rows) == 2 and not reader.errors
    path = W.root() / 'inline.json'
    path.write_text('{}')
    assert reader.read(path.relative_to(C.ontology_root())) is None
    assert reader.errors[-1]['error'] == 'SOURCE_INTEGRITY_ENVELOPE_REQUIRED'


def test_trace_views_include_cited_detail_with_original_visibility():
    import trace_query
    original, _ = delivery(fields=['value'])
    detail = W.detail('B1:E1', ['snippet'])
    normalized = W.normalize_judgment(submission(detail))
    reader = trace_query.Reader(C.now())
    views = trace_query.wire_views(reader, original, [normalized])
    assert [v['bundle_alias'] for v in views] == ['B1', 'B2']
    assert 'snippet' not in views[0]['aliases']['E1']['visible_fields']
    assert 'snippet' in views[1]['aliases']['E1']['visible_fields']
    assert views[1]['visible_payload'] == detail and not reader.errors


def test_raw_canonical_submission_cannot_bypass_compact_declaration():
    original, _ = delivery()
    value = item()
    value['delivery_ids'] = [original['delivery_id']]
    value['arguments'][0]['evidence']['delivery_id'] = original['delivery_id']
    result = J.record_ai_judgments([value])[0]
    assert result['errors'][0]['code'] == 'WIRE_FORMAT_REQUIRED'


def test_logger_reports_partial_usage_without_inventing_zeros(monkeypatch):
    import logger
    monkeypatch.setattr(logger.log_store, 'iter_rows', lambda *a, **k: iter([
        {'time': '2026-11-07 10:00', 'ok': True, 'tokens': 20, 'usage': {'input_tokens': 20, 'cache_read_input_tokens': 100}},
        {'time': '2026-11-07 11:00', 'ok': True, 'tokens': 0}]))
    stats = logger.system()[0]['provider_usage_totals']
    assert stats['cache_read_input_tokens'] == {'measured_total': 100, 'reported_runs': 1, 'unreported_runs': 1}
    assert stats['output_tokens']['measured_total'] is None
