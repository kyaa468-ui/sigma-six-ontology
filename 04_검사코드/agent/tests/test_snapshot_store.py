"""Storage-only deduplication: exact values, atomic references and old records."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import common as C
import snapshot_store as S
import scoring_store as SC
import case_store as CS
import proposal_store as PS
import validation_store as VS


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'WORK', str(tmp_path))
    return tmp_path


def large_value():
    return {'source': 'synthetic', 'rows': [{'date': f'2026-11-{i:02}', 'value': i * 77,
        'text': '외국인 순매수와 기관 순매도는 별도로 보관한다.' * 10} for i in range(1, 31)]}


def inventory(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}


def test_exact_hydration_no_aliases_or_write_on_read(isolated):
    value = {'first': large_value(), 'second': copy.deepcopy(large_value()),
             'missing': None, 'zero': 0, 'negative': -4000000000, 'fraction': .3}
    document = S.encode(value)
    assert document['format'] == S.VERSION
    before = inventory(isolated)
    dependencies = {}
    restored = S.decode(document, dependencies=dependencies)
    assert restored == value
    assert inventory(isolated) == before
    assert dependencies
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha for path, sha in dependencies.items())
    restored['first']['rows'][0]['value'] = 999
    assert restored['second']['rows'][0]['value'] == value['second']['rows'][0]['value']
    assert S.decode(document) == value


def test_cross_stage_records_reuse_large_input_and_preserve_logical_hashes(isolated):
    body = {'record_id': 'same-body', 'actual_reads': [
        {'path': 'briefing.json', 'value': copy.deepcopy(large_value()), 'read_at': str(i)}
        for i in range(24)]}
    original_bytes = 0
    for module in (SC, CS, PS, VS):
        module.archive('fixtures', 'same', body)
        assert module.load('fixtures', 'same') == body
        path = module._path('fixtures', 'same')
        physical = json.loads(path.read_text())
        envelope = S.decode(physical)
        assert envelope['content_sha256'] == module.digest(body)
        original_bytes += len(module.canonical(envelope).encode())
    physical_bytes = sum(len(raw) for raw in inventory(isolated).values())
    assert physical_bytes < original_bytes * .1


@pytest.mark.parametrize('store', [SC, CS, PS, VS])
def test_old_inline_envelopes_and_idempotent_archive_remain_valid(store):
    body = {'record_id': 'old', 'large': large_value()}
    store.archive('fixtures', 'old', body)
    path = store._path('fixtures', 'old')
    inline = S.decode(json.loads(path.read_text()))
    path.write_text(json.dumps(inline, ensure_ascii=False))
    before = path.read_bytes()
    assert store.load('fixtures', 'old') == body
    assert store.archive('fixtures', 'old', body) == body
    assert path.read_bytes() == before
    with pytest.raises(store.StoreError, match='conflict'):
        store.archive('fixtures', 'old', {**body, 'record_id': 'different'})


@pytest.mark.parametrize('collision', [
    {S.REF: 'f' * 64}, {S.LITERAL: [['key', 'value']]},
    {'format': S.VERSION, 'logical_sha256': 'f' * 64, 'logical_bytes': 0, 'payload': None},
])
def test_source_values_cannot_inject_blob_references(collision):
    value = {'data': large_value(), 'untrusted': collision}
    assert S.decode(S.encode(value)) == value
    assert S.decode(S.encode(collision)) == collision


def test_corrupt_or_missing_blob_never_becomes_empty_or_repaired():
    document = S.encode({'repeated': [large_value()] * 4})
    dependencies = {}
    S.decode(document, dependencies=dependencies)
    path = Path(next(iter(dependencies)))
    original = path.read_bytes()
    path.write_bytes(b'{}')
    with pytest.raises(S.SnapshotError, match='BLOB_HASH_MISMATCH'):
        S.decode(document)
    assert path.read_bytes() == b'{}'
    path.write_bytes(original)
    assert S.decode(document)
    path.unlink()
    with pytest.raises(S.SnapshotError, match='BLOB_MISSING'):
        S.decode(document)
    assert not path.exists()


def test_logical_hash_and_declared_size_cannot_be_changed():
    document = S.encode(large_value())
    with pytest.raises(S.SnapshotError, match='LOGICAL_HASH'):
        S.decode({**document, 'logical_sha256': '0' * 64})
    with pytest.raises(S.SnapshotError, match='EXPANSION_LIMIT'):
        S.decode({**document, 'logical_bytes': 1})


def test_atomic_publish_failure_does_not_publish_record(monkeypatch):
    original = S._publish
    calls = []
    def fail_second(payload, base=None):
        calls.append(payload)
        if len(calls) == 2:
            raise OSError('injected disk failure')
        return original(payload, base)
    monkeypatch.setattr(S, '_publish', fail_second)
    with pytest.raises(OSError, match='injected disk failure'):
        SC.archive('fixtures', 'failed', {'one': 'a' * 5000, 'two': 'b' * 5000})
    assert not SC._path('fixtures', 'failed').exists()
    assert not list(S.root().glob('.snapshot-*'))


def test_blob_recording_failure_does_not_change_dispatch_or_retry_rights(monkeypatch):
    import execution_trace as E
    scope = E.account_scope()
    attempt = E.reserve_attempt(scope)['order_attempt_id']
    payload = {'code': '000001', 'side': '매수', 'qty': 1}
    sends = []
    def sender():
        sends.append(True)
        return {'ok': True, 'order_no': 'synthetic-order'}
    def failed_encoding(*args, **kwargs):
        raise OSError('injected snapshot storage outage')
    monkeypatch.setattr(S, 'encode', failed_encoding)
    first = E.dispatch_once(scope, attempt, payload, sender)
    retried = E.dispatch_once(scope, attempt, payload, sender)
    assert first['ok'] is True and first['recording_status'] == 'recording_error'
    assert retried['replayed'] is True and retried['ok'] is True
    assert sends == [True]


def test_existing_damaged_blob_is_not_overwritten_by_archive():
    value = large_value()
    document = S.encode(value)
    deps = {}; S.decode(document, dependencies=deps)
    path = Path(next(iter(deps)))
    path.write_bytes(b'broken')
    with pytest.raises(S.SnapshotError, match='EXISTING_SNAPSHOT_HASH_MISMATCH'):
        S.encode(value)
    assert path.read_bytes() == b'broken'


def test_concurrent_same_record_and_blob_publication():
    value = {'shared': large_value()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: SC.archive('fixtures', 'concurrent', value), range(8)))
    assert rows == [value] * 8
    assert SC.load('fixtures', 'concurrent') == value
    assert not list(S.root().glob('.snapshot-*'))


def test_existing_blob_and_directory_chain_are_synced_before_reuse(monkeypatch):
    value = 'repeated-value' * 1000
    original = S.encode(value)
    calls = []
    monkeypatch.setattr(S, '_sync_directory', lambda path: calls.append(Path(path)))
    assert S.encode(value) == original
    folder = S.root()
    assert calls == [folder, folder.parent, folder.parent.parent, folder.parent.parent.parent]


def test_existing_blob_parent_sync_failure_prevents_record_publication(monkeypatch):
    body = {'value': 'shared-input' * 1000}
    S.encode(body)
    def fail_boundary(path):
        if Path(path) == S.root().parent.parent:
            raise OSError('injected directory durability failure')
    monkeypatch.setattr(S, '_sync_directory', fail_boundary)
    with pytest.raises(OSError, match='directory durability failure'):
        SC.archive('fixtures', 'not-durable', body)
    assert not SC._path('fixtures', 'not-durable').exists()


def test_symlink_and_path_reference_escape_rejected(isolated):
    document = S.encode(large_value())
    deps = {}; S.decode(document, dependencies=deps)
    path = Path(next(iter(deps)))
    elsewhere = isolated / 'outside.json'
    elsewhere.write_bytes(path.read_bytes())
    path.unlink(); path.symlink_to(elsewhere)
    with pytest.raises(S.SnapshotError, match='SYMLINK'):
        S.decode(document)
    with pytest.raises(S.SnapshotError, match='INVALID_SNAPSHOT_HASH'):
        S.decode({**document, 'payload': {S.REF: '../outside'}})


def test_depth_counts_json_levels_not_storage_references():
    value = 'payload' * 1000
    for _ in range(100):
        value = {'next': value}
    assert S.decode(S.encode(value)) == value
    for _ in range(30):
        value = {'next': value}
    with pytest.raises(S.SnapshotError, match='DEPTH_LIMIT'):
        S.encode(value)
