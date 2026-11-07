"""Public staging has an explicit schema and cannot publish private trace data."""
import copy
import json
from pathlib import Path

import pytest

from test_ontology import C, isolated
from test_trace_snapshot import projection, bytes_under
import research_evidence as RE
import trace_snapshot as V
import trace_export as X


def test_export_is_allowlisted_private_staging_and_explicit_refs_only(projection):
    built = V.build_view_snapshot()
    exported = X.export_trace_summary(built['view_id'])
    assert exported['status'] == 'complete', exported
    assert exported['published'] is False
    assert Path(exported['staging_path']).is_relative_to(V.root())
    assert not Path(C.DOCS_DATA).exists()
    loaded = X.load_public_export()
    assert loaded['status'] == 'complete'
    raw = json.dumps(loaded['summary'])
    for secret in ('PRIVATE_SENTINEL', 'ACCOUNT_SECRET', 'ORDER_SECRET', 'judgment-private', 'order-private',
                   'plan-private', 'attempt-private', 'source_manifest', 'params', 'account', 'source_inputs'):
        assert secret not in raw
    trace = next(iter(loaded['summary']['traces'].values()))
    expected = X.opaque_reference('order_attempt_id', 'order-private')
    assert {'kind': 'order_attempt_id', 'ref': expected} in trace['origin_refs']
    assert trace['premise_states'] == [{'status': 'complete_refuted', 'count': 1}]


def test_export_and_preview_retries_are_identical_and_read_only(projection):
    built = V.build_view_snapshot()
    first = X.export_trace_summary(built['view_id'])
    before = bytes_under(C.WORK)
    second = X.export_trace_summary(built['view_id'])
    assert first == second
    X.load_public_export(first['export_id'])
    X.load_public_export()
    assert bytes_under(C.WORK) == before


def test_unknown_free_text_is_not_allowed_inside_status_fields(projection):
    target = projection['targets'][0]
    target.update(coverage_status='PRIVATE_SENTINEL', connection_status='PRIVATE_SENTINEL',
                  action='PRIVATE_SENTINEL', code='PRIVATE_SENTINEL', created_at='PRIVATE_SENTINEL',
                  confidence_for='PRIVATE_SENTINEL')
    projection['traces'][target['trace_id']]['target'] = copy.deepcopy(target)
    projection['traces'][target['trace_id']]['validations'][0]['status'] = 'PRIVATE_SENTINEL'
    built = V.build_view_snapshot()
    X.export_trace_summary(built['view_id'])
    summary = X.load_public_export()['summary']
    assert 'PRIVATE_SENTINEL' not in json.dumps(summary)
    assert summary['targets'][0]['coverage_status'] == 'unknown'


def test_partial_view_is_not_ready_public_generation(projection):
    projection['status'] = 'partial'
    built = V.build_view_snapshot()
    result = X.export_trace_summary(built['view_id'])
    assert result['status'] == 'unavailable'
    assert not (V.root() / 'public/manifest.json').exists()


def test_public_reservation_failure_keeps_prior_generation(projection, monkeypatch):
    first = V.build_view_snapshot()
    X.export_trace_summary(first['view_id'])
    previous = (V.root() / 'public/manifest.json').read_bytes()
    second = V.build_view_snapshot()
    def fail(*args, **kwargs): raise OSError('usage unavailable')
    monkeypatch.setattr(RE, 'reserve_usage', fail)
    result = X.export_trace_summary(second['view_id'])
    assert result['status'] == 'recording_error'
    assert (V.root() / 'public/manifest.json').read_bytes() == previous


def test_partial_export_write_never_switches_generation(projection, monkeypatch):
    first = V.build_view_snapshot()
    X.export_trace_summary(first['view_id'])
    previous = (V.root() / 'public/manifest.json').read_bytes()
    second = V.build_view_snapshot()
    write = V._write_immutable
    def fail(path, value):
        if '/public/' in str(path) and Path(path).name == 'commit.json': raise OSError('commit failed')
        return write(path, value)
    monkeypatch.setattr(V, '_write_immutable', fail)
    result = X.export_trace_summary(second['view_id'])
    assert result['status'] == 'recording_error'
    assert (V.root() / 'public/manifest.json').read_bytes() == previous


def test_public_summary_hash_failure_is_not_sample_fallback(projection):
    built = V.build_view_snapshot()
    result = X.export_trace_summary(built['view_id'])
    path = Path(result['staging_path']) / 'summary.json'
    value = json.loads(path.read_text()); value['targets'] = []
    path.write_text(json.dumps(value))
    before = bytes_under(C.WORK)
    loaded = X.load_public_export()
    assert loaded['status'] == 'recording_error' and 'summary' not in loaded
    assert bytes_under(C.WORK) == before


def test_protected_target_has_only_protection_status(projection):
    target = projection['targets'][0]
    target['visibility'] = 'protected'
    projection['traces'][target['trace_id']]['visibility'] = 'protected'
    # Query supplies only harmless metadata for this target. Export's own
    # allowlist must still omit any accidental private detail fields.
    projection['application_ids'] = []
    projection['protection_scopes'] = []
    built = V.build_view_snapshot()
    X.export_trace_summary(built['view_id'])
    trace = next(iter(X.load_public_export()['summary']['traces'].values()))
    assert trace['code'] is None and trace['created_at'] is None
    assert trace['origin_refs'] == trace['premise_states'] == trace['validation_states'] == []
    assert trace['stage_counts'] == {}


def test_current_visibility_blocks_prepared_public_preview(projection, monkeypatch):
    built = V.build_view_snapshot()
    X.export_trace_summary(built['view_id'])
    def denied(*args): raise ValueError('RESERVED_HOLDOUT_DISCOVERY_FORBIDDEN')
    monkeypatch.setattr(V, 'check_visibility', denied)
    before = bytes_under(C.WORK)
    result = X.load_public_export()
    assert result['status'] == 'unavailable' and 'summary' not in result
    assert bytes_under(C.WORK) == before


def test_removed_public_usage_receipt_blocks_preview(projection):
    built = V.build_view_snapshot()
    result = X.export_trace_summary(built['view_id'])
    commit = json.loads((Path(result['staging_path']) / 'commit.json').read_text())
    import case_store as CS
    (CS.root() / 'usage_events' / (commit['exposure_reservation_id'] + '.json')).unlink()
    before = bytes_under(C.WORK)
    loaded = X.load_public_export()
    assert loaded['status'] == 'recording_error' and 'summary' not in loaded
    assert bytes_under(C.WORK) == before


def test_rule_exit_does_not_inflate_general_coverage(projection):
    rule = copy.deepcopy(projection['targets'][0])
    rule.update(trace_id='trace-'+'b'*32, coverage_status='rule_exit', outside_expected_denominator=True)
    projection['targets'].append(rule)
    projection['traces'][rule['trace_id']] = {'target': rule}
    built = V.build_view_snapshot()
    X.export_trace_summary(built['view_id'])
    counts = X.load_public_export()['summary']['counts']
    assert counts['targets'] == 2 and counts['expected_general_judgments'] == 1
    assert counts['rule_exit_count'] == 1 and counts['linked_rate'] == 1


def test_incomplete_target_set_never_reports_perfect_coverage(projection):
    projection['coverage'] = [{'target_set_incomplete': True}]
    built = V.build_view_snapshot()
    X.export_trace_summary(built['view_id'])
    counts = X.load_public_export()['summary']['counts']
    assert counts['linked_rate'] is None and counts['target_set_incomplete']
