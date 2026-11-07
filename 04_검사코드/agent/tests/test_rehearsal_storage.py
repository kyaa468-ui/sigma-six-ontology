"""A smaller report must still reject changed trading behavior."""
import copy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from rehearsal_storage import compare_rehearsals, summarize_storage


def test_storage_comparison_does_not_hide_a_changed_order(tmp_path):
    execution = {'prep_candidates': ['A', 'B'], 'intraday_candidates': ['B', 'A'],
        'main_price': 20400, 'ma20': 20000,
        'partial_fill': {'requested': 100, 'confirmed': 40, 'unconfirmed': 60},
        'coverage': {'linked': 1, 'not_submitted': 4},
        'response_loss': {'dispatches': 1}, 'receipt_loss': {'dispatches': 1}}
    before = {'steps': {'1-3': execution}, 'storage': {'total_bytes': 1000, 'projection_bytes': 500}}
    path = tmp_path / 'before.json'
    path.write_text(json.dumps(before))
    after = copy.deepcopy(before)
    after['storage'] = {'total_bytes': 100, 'projection_bytes': 50}
    good = compare_rehearsals(after, path)
    assert good['status'] == 'matched'
    assert good['storage']['total_bytes']['reduction_fraction'] == .9
    after['steps']['1-3']['partial_fill']['confirmed'] = 100
    bad = compare_rehearsals(after, path)
    assert bad['status'] == 'mismatch'
    assert bad['stable_behavior']['partial_fill']['equal'] is False


def test_physical_measurement_counts_shared_payload_once(tmp_path):
    blob = tmp_path / 'ontology/snapshots/blobs/payload.json'
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b'X' * 100)
    for name in ('a', 'b'):
        (tmp_path / f'{name}.json').write_bytes(b'REF')
    result = summarize_storage(tmp_path)
    assert result['total_bytes'] == 106
    assert result['shared_blob_count'] == 1
    assert result['shared_blob_bytes'] == 100
    assert compare_rehearsals({}, tmp_path / 'absent.json')['status'] == 'unavailable'


def test_projection_measurement_includes_shared_dependency_closure(tmp_path):
    import snapshot_store
    base = tmp_path / 'ontology'
    document = snapshot_store.encode({'source': {'payload': 'same-observation-' * 400}}, base=base)
    for name in ('a', 'b'):
        path = base / f'trace_view/snapshots/{name}/projection.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(document))
    result = summarize_storage(tmp_path)
    assert result['status'] == 'measured'
    assert result['projection_dependency_bytes'] == result['shared_blob_bytes']
    assert result['projection_closure_bytes'] == result['projection_bytes'] + result['shared_blob_bytes']
    assert result['ontology_bytes'] == result['total_bytes']
    assert result['ancillary_bytes'] == 0
