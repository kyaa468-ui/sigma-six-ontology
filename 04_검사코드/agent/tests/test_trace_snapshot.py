"""Offline view preparation boundaries: no hidden writes on reads or future data."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_ontology import C, isolated
import case_store as CS
import research_evidence as RE
import trace_snapshot as V
import validation_holdout as VH


def bytes_under(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in Path(path).rglob('*') if p.is_file()}


@pytest.fixture
def projection(monkeypatch):
    source = Path(C.WORK) / 'ontology' / 'source_inputs' / 'one.json'
    source.parent.mkdir(parents=True)
    source.write_text('{"value":20400}', encoding='utf-8')
    trace_id = 'trace-' + 'a' * 32
    target = {'trace_id': trace_id, 'run_attempt_id': 'attempt-private', 'code': '000001',
              'created_at': C.now().isoformat(), 'confidence_for': '매수', 'coverage_status': 'linked',
              'connection_status': 'verified', 'action': '매수', 'visibility': 'visible',
              'judgment_ids': ['judgment-private']}
    detail = {'target': target, 'deliveries': [{'text': 'PRIVATE_SENTINEL', 'account': 'ACCOUNT_SECRET'}],
              'judgments': [{'judgment_id': 'judgment-private', 'final_reason': 'PRIVATE_SENTINEL'}],
              'premises': [{'application_id': 'p1'}], 'evaluations': [], 'reviews': [], 'proposals': [],
              'validations': [{'status': 'complete', 'params': {'secret': 'PRIVATE_SENTINEL'}}],
              'execution_context': {'orders': [{'order_attempt_id': 'order-private', 'order_no': 'ORDER_SECRET',
                                               'original_calls': [{'plan_id': 'plan-private'}], 'events': []}]},
              'errors': []}
    result = {'schema_version': 'TRACE_VIEW@v1', 'market_as_of': '2026-11-05',
              'knowledge_as_of': C.now().isoformat(), 'generated_at': C.now().isoformat(),
              'status': 'complete', 'targets': [target], 'traces': {trace_id: detail},
              'cases': [{'application_id': 'p1', 'recording_status': 'recorded', 'outcome_eligible': True,
                         'verdict': 'refuted', 'label': 1}], 'reviews': [], 'proposals': [], 'validations': [],
              'policies': [], 'errors': [], 'protected_count': 0, 'application_ids': ['p1'],
              'protection_scopes': [{'code': '000001', 'created_at': C.now().isoformat(),
                                     'confidence_for': '매수', 'application_id': 'p1'}],
              'source_manifest': [{'path': 'source_inputs/one.json',
                  'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'kind': 'source_inputs', 'id': 'one'}]}
    monkeypatch.setitem(sys.modules, 'trace_query', SimpleNamespace(build_projection=lambda *args: copy.deepcopy(result)))
    return result


def test_build_reserves_before_writing_then_all_gets_are_pure(projection, monkeypatch):
    reserve = RE.reserve_usage
    def check(*args, **kwargs):
        assert not list(V.root().glob('snapshots/*/projection.json'))
        return reserve(*args, **kwargs)
    monkeypatch.setattr(RE, 'reserve_usage', check)
    built = V.build_view_snapshot()
    assert built['status'] == 'complete', built
    usage = RE.usage_status(['p1'])
    assert usage['items'][0]['status'] == 'used'
    before = bytes_under(C.WORK)
    first = V.load_view_snapshot(built['view_id'])
    second = V.load_view_snapshot(built['view_id'])
    assert first == second
    assert first['projection'] == projection
    assert bytes_under(C.WORK) == before


def test_preparation_is_new_immutable_generation(projection):
    first, second = V.build_view_snapshot(), V.build_view_snapshot()
    assert first['view_id'] != second['view_id']
    assert first['manifest']['projection_hash'] == second['manifest']['projection_hash']
    assert V.load_view_snapshot(first['view_id'])['projection'] == projection


def test_old_view_is_pinned_after_original_source_changes(projection):
    first = V.build_view_snapshot()
    (Path(C.WORK) / 'ontology/source_inputs/one.json').write_text('{"value":19800}')
    assert V.load_view_snapshot(first['view_id'])['projection'] == projection
    second = V.build_view_snapshot()
    assert second['status'] == 'recording_error'
    assert 'CHANGED' in second['errors'][0]['reason']
    assert 'projection' not in V.load_view_snapshot(second['view_id'])


def test_usage_failure_never_archives_revealing_payload(projection, monkeypatch):
    def fail(*args, **kwargs): raise OSError('usage unavailable')
    monkeypatch.setattr(RE, 'reserve_usage', fail)
    built = V.build_view_snapshot()
    assert built['status'] == 'recording_error'
    assert not list(V.root().glob('snapshots/*/projection.json'))
    assert all(b'PRIVATE_SENTINEL' not in data for data in bytes_under(V.root()).values())


def test_incomplete_commit_is_unavailable_and_not_repaired(projection, monkeypatch):
    write = V._write_immutable
    def fail(path, body):
        if Path(path).name == 'commit.json': raise OSError('commit unavailable')
        return write(path, body)
    monkeypatch.setattr(V, '_write_immutable', fail)
    built = V.build_view_snapshot()
    assert built['status'] == 'recording_error'
    assert (V.root() / 'snapshots' / built['view_id'] / 'projection.json').exists()
    before = bytes_under(C.WORK)
    loaded = V.load_view_snapshot(built['view_id'])
    assert loaded['status'] == 'unfinished' and 'projection' not in loaded
    assert bytes_under(C.WORK) == before


@pytest.mark.parametrize('filename', ['projection.json', 'manifest.json', 'commit.json'])
def test_tampered_view_is_error_not_empty_success(projection, filename):
    built = V.build_view_snapshot()
    path = V.root() / 'snapshots' / built['view_id'] / filename
    value = read_stored(path)
    value['tampered'] = True
    if filename == 'commit.json': value['projection_hash'] = 'bad'
    path.write_text(json.dumps(value))
    before = bytes_under(C.WORK)
    loaded = V.load_view_snapshot(built['view_id'])
    assert loaded['status'] == 'recording_error' and 'projection' not in loaded
    assert bytes_under(C.WORK) == before


def test_removed_exposure_receipt_blocks_read_without_repair(projection):
    built = V.build_view_snapshot()
    (CS.root() / 'usage_events' / (built['manifest']['exposure_reservation_id'] + '.json')).unlink()
    before = bytes_under(C.WORK)
    loaded = V.load_view_snapshot(built['view_id'])
    assert loaded['status'] == 'recording_error' and 'projection' not in loaded
    assert bytes_under(C.WORK) == before


def test_current_protection_applies_to_old_view_and_frozen_origin(projection, monkeypatch):
    built = V.build_view_snapshot()
    monkeypatch.setattr(VH, 'assert_discovery_allowed', lambda *_: None)
    scopes = []
    def reserve(scope):
        scopes.append(scope)
        return ['validation-new']
    monkeypatch.setattr(VH, 'is_reserved_judgment', reserve)
    before = bytes_under(C.WORK)
    loaded = V.load_view_snapshot(built['view_id'])
    assert loaded['status'] == 'protected' and 'projection' not in loaded
    assert scopes[0]['created_at'] == projection['protection_scopes'][0]['created_at']
    assert bytes_under(C.WORK) == before


def test_source_change_during_usage_reservation_never_commits(projection, monkeypatch):
    reserve = RE.reserve_usage
    def change(*args, **kwargs):
        result = reserve(*args, **kwargs)
        (Path(C.WORK) / 'ontology/source_inputs/one.json').write_text('{"value":1}')
        return result
    monkeypatch.setattr(RE, 'reserve_usage', change)
    built = V.build_view_snapshot()
    assert built['status'] == 'recording_error'
    assert not list(V.root().glob('snapshots/*/commit.json'))


@pytest.mark.parametrize('path', ['../account.json', '/tmp/private.json'])
def test_source_path_escape_is_rejected_before_snapshot(projection, path):
    projection['source_manifest'][0]['path'] = path
    built = V.build_view_snapshot()
    assert built['status'] == 'recording_error'
    assert not list(V.root().glob('snapshots/*/projection.json'))


def test_invalid_missing_and_error_gets_do_not_create_storage():
    before = bytes_under(C.WORK)
    for identifier in ('../../account', 'view-'+'f'*32, None):
        result = V.load_view_snapshot(identifier)
        assert result['status'] in ('unavailable', 'recording_error')
        assert 'projection' not in result
    assert bytes_under(C.WORK) == before


def test_partial_view_keeps_missing_record_state(projection):
    projection['status'] = 'partial'
    projection['errors'] = [{'record_id': 'missing-judgment', 'reason': 'accepted_origin_missing'}]
    built = V.build_view_snapshot()
    loaded = V.load_view_snapshot(built['view_id'])
    assert built['status'] == loaded['status'] == 'partial'
    assert loaded['errors'] == projection['errors']


def test_committed_missing_component_differs_from_unfinished(projection):
    built = V.build_view_snapshot()
    (V.root() / 'snapshots' / built['view_id'] / 'projection.json').unlink()
    loaded = V.load_view_snapshot(built['view_id'])
    assert loaded['status'] == 'recording_error'
    assert loaded['errors'][0]['reason'] == 'COMMITTED_VIEW_COMPONENT_MISSING'


def test_ancestor_symlink_cannot_escape_snapshot_root(projection, tmp_path):
    built = V.build_view_snapshot()
    import shutil
    source = V.root() / 'snapshots'
    outside = tmp_path / 'outside_views'
    shutil.move(str(source), str(outside))
    source.symlink_to(outside, target_is_directory=True)
    before = bytes_under(outside)
    loaded = V.load_view_snapshot(built['view_id'])
    assert loaded['status'] == 'recording_error'
    assert 'SYMLINK' in loaded['errors'][0]['reason']
    assert bytes_under(outside) == before


def test_manifest_cannot_pin_new_bytes_after_independent_reader_used_old(projection, monkeypatch):
    import trace_query as Q
    def changed_during_query(*args):
        body = copy.deepcopy(projection)  # helper's result used the old input
        source = Path(C.WORK) / 'ontology/source_inputs/one.json'
        source.write_text('{"value":19800}')
        # Simulate the subsequent manifest scan pinning the new source bytes.
        body['source_manifest'][0]['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        return body
    monkeypatch.setattr(Q, 'build_projection', changed_during_query)
    built = V.build_view_snapshot()
    assert built['status'] == 'recording_error'
    assert built['errors'][0]['reason'] == 'SOURCE_NEW_OR_CHANGED_DURING_PROJECTION'
    assert not list(V.root().glob('snapshots/*/projection.json'))


def test_newly_selected_source_requires_new_preparation(projection, monkeypatch):
    import trace_query as Q
    def new_source(*args):
        body = copy.deepcopy(projection)
        path = Path(C.WORK) / 'ontology/source_inputs/two.json'
        path.write_text('{}')
        body['source_manifest'].append({'path': 'source_inputs/two.json',
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        return body
    monkeypatch.setattr(Q, 'build_projection', new_source)
    built = V.build_view_snapshot()
    assert built['status'] == 'recording_error'
    assert not list(V.root().glob('snapshots/*/commit.json'))


def test_missing_review_scope_cannot_expose_protected_aggregate():
    """An unreadable dataset is not evidence that an aggregate is outside holdout."""
    import trace_query as Q
    from test_trace_query import reservation
    reservation()
    rid = 'review-missing-scope'
    CS.archive('jobs', rid, {'review_id': rid, 'started_at': C.now().isoformat()})
    CS.archive('results', rid, {'review_id': rid, 'finished_at': C.now().isoformat(),
        'analysis_status': 'complete', 'comparison': {'summary': {'private_label': 'HELDOUT_AGGREGATE_SENTINEL'}}})
    result = Q.build_projection()
    assert 'HELDOUT_AGGREGATE_SENTINEL' not in json.dumps(result)
    assert result['reviews'][0]['status'] in ('recording_error', 'protected') or result['reviews'][0].get('visibility') == 'protected'


def test_scoring_read_change_cannot_receive_a_later_manifest_certificate(monkeypatch):
    import case_dataset as D
    import scoring_store as SS
    original = {'evaluation_id': 'evaluation-race', 'value': 'OLD_SOURCE_VALUE', 'created_at': C.now().isoformat()}
    SS.archive('evaluations', original['evaluation_id'], original)
    source = SS.root() / 'evaluations/evaluation-race.json'
    def changed_after_case_read(*args):
        newer = {**original, 'value': 'NEW_SOURCE_VALUE'}
        envelope = read_stored(source)
        envelope.update(record=newer, content_sha256=SS.digest(newer))
        source.write_text(json.dumps(envelope))
        return {'cases': [], 'errors': [], 'withheld_count': 0,
                'source_manifest': [{'kind': 'scoring/evaluations', 'id': original['evaluation_id'],
                                     'sha256': SS.digest(original)}]}
    monkeypatch.setattr(D, 'build_cases', changed_after_case_read)
    built = V.build_view_snapshot()
    assert built['status'] == 'recording_error', built
    assert 'projection' not in V.load_view_snapshot(built['view_id'])
