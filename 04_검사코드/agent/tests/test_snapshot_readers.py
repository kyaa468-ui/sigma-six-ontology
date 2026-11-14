"""Cross-store read contracts: shared physical bytes, original logical evidence."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from test_ontology import C, isolated
from test_trace_snapshot import projection, bytes_under
import evaluation_context as EC
import execution_trace as E
import snapshot_store as S
import trace_query as Q
import trace_snapshot as V


def repeated():
    return {'code': '000001', 'actual_reads': [{'path': 'fixture', 'value': {'price': 20400, 'basis': 'synthetic',
              'rows': [{'price': 20000 + i, 'observed_at': '2026-11-05'} for i in range(150)]}}] * 20}


def test_execution_and_projection_readers_bind_shared_blob_bytes():
    event = E.record('order_plan', code='000001', inputs=repeated())
    assert event['recording_status'] == 'recorded', event
    paths = list((Path(C.WORK) / 'ontology/executions').rglob(event['event_id'] + '.json'))
    assert len(paths) == 1
    path = paths[0]
    errors = []
    value, source = EC._read(path, {}, errors)
    assert not errors and value['inputs'] == repeated()
    assert source['snapshot_dependencies']
    reader = Q.Reader(C.now().isoformat())
    received = reader.read(path.relative_to(reader.root), required=True)
    assert received == value and not reader.errors
    blobs = [row for row in reader.manifest.values() if row['kind'] == 'snapshots/blobs']
    assert blobs
    for row in blobs:
        assert hashlib.sha256((reader.root / row['path']).read_bytes()).hexdigest() == row['sha256']


def test_missing_blob_is_an_error_without_repair_or_empty_success():
    event = E.record('order_plan', code='000001', inputs=repeated())
    path = next((Path(C.WORK) / 'ontology/executions').rglob(event['event_id'] + '.json'))
    next(S.root().glob('*.json')).unlink()
    before = bytes_under(C.WORK)
    errors = []
    _, source = EC._read(path, {}, errors)
    assert errors and source['status'] == 'unavailable'
    reader = Q.Reader(C.now().isoformat())
    assert reader.read(path.relative_to(reader.root), required=True) is None
    assert reader.errors
    assert bytes_under(C.WORK) == before


def test_private_view_deduplicates_and_preserves_original_logical_hash(projection):
    trace = next(iter(projection['traces'].values()))
    trace['execution_events'] = [repeated()] * 3
    original = copy.deepcopy(projection)
    first = V.build_view_snapshot()
    assert first['status'] == 'complete', first
    path = V.root() / 'snapshots' / first['view_id'] / 'projection.json'
    assert path.stat().st_size < len(V.canonical(original).encode()) / 5
    assert first['manifest']['projection_hash'] == V.digest(original)
    assert V.load_view_snapshot(first['view_id'])['projection'] == original
    before = {p.name for p in S.root().glob('*.json') if p.stat().st_size>=S.MIN_BYTES}
    second = V.build_view_snapshot()
    assert second['status'] == 'complete'
    assert {p.name for p in S.root().glob('*.json') if p.stat().st_size>=S.MIN_BYTES} == before
    frozen = bytes_under(C.WORK)
    assert V.load_view_snapshot(second['view_id'])['projection'] == original
    assert bytes_under(C.WORK) == frozen


def test_changed_dependency_prevents_committing_a_prepared_view(projection, monkeypatch):
    source = Path(C.WORK) / 'ontology/source_inputs/large.json'
    encoded = S.encode(repeated())
    source.write_text(json.dumps(encoded))
    dependencies = {}
    S.decode(encoded, dependencies=dependencies)
    for path, fingerprint in dependencies.items():
        projection['source_manifest'].append({'path': str(Path(path).relative_to(Path(C.WORK) / 'ontology')),
             'sha256': fingerprint, 'kind': 'snapshots/blobs', 'id': Path(path).stem})
    reserve = V.check_visibility
    def change_after_read(data):
        Path(next(iter(dependencies))).write_text('{"changed":true}')
        return reserve(data)
    monkeypatch.setattr(V, 'check_visibility', change_after_read)
    result = V.build_view_snapshot()
    assert result['status'] == 'recording_error'
    assert not (V.root() / 'snapshots' / result['view_id'] / 'commit.json').exists()
