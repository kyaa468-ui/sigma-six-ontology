"""Pure historical reader and cross-stage lineage; all stores and orders local."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest

from test_ontology import C, M, isolated
from test_close_evaluation import accepted, bytes_under
from test_judgment_integration import setup_run, submission, watch
import ai_judgment as J
import judgment_coverage as JC
import trace_query as Q
import validation_store as VS
import policy_validation as PV


def test_policy_context_scan_matches_legacy_text_and_stops_at_first_match(monkeypatch):
    for value in ({'policy_id':'p'}, {'text':'정책 policy 근거'},
                  {'text':'po\\licy'}, {'POLICY':False}, {'x':['before',{'policy':None}]}):
        assert Q._has_policy_context(value)==('policy' in json.dumps(value))
    def split_chunks(value):
        yield b'{"text":"po'
        yield b'licy"'
        raise AssertionError('Unneeded large tail was restored')
    monkeypatch.setattr(Q.objects,'chunks',split_chunks)
    assert Q._has_policy_context({'large_tail':'unselected'})


def test_empty_holdout_scope_does_not_restore_nested_source():
    class Unopened(dict):
        def get(self,*args):raise AssertionError('Source unnecessarily restored')
        def values(self):raise AssertionError('Source unnecessarily restored')
    assert not Q._blocked(Unopened(), [])


def test_application_id_scan_preserves_nested_ids_without_loading_large_text(tmp_path):
    import storage_objects as O
    value={'application_id':'a','raw':'x'*1000000,'nested':[{'application_ids':['b','c'],
        'premise_application_id':'d','support_case_ids':['e'],'counter_case_ids':['f'],
        'matched_case_ids':['g'],'case_ids':['h'],'deeper':{'application_id':'i'}}]}
    ref=O.put_object(value,base=tmp_path/'ontology')
    resolver=O.Resolver(tmp_path/'ontology');stored=O.open_object(ref,resolver=resolver)
    assert Q._application_ids(stored)==set('abcdefghi')
    assert resolver.metrics['loaded_bytes']<128*1024
    assert Q._application_ids(value)==set().union(*(Q._ids(value,key) for key in
        ('application_id','application_ids','premise_application_id','support_case_ids','counter_case_ids','matched_case_ids','case_ids')))


def test_case_lineage_survives_reader_cache_eviction(monkeypatch):
    from collections import OrderedDict
    original=Q.Reader
    class TinyCache(OrderedDict):
        def __setitem__(self,key,value):
            super().__setitem__(key,value)
            while len(self)>1:self.popitem(last=False)
    class SmallReader(original):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);self.cache=TinyCache()
    monkeypatch.setattr(Q,'Reader',SmallReader)
    _,_,application,judgment_id,_=accepted(monkeypatch,place=False)
    projection=Q.build_projection()
    assert any(row['application_id']==application['application_id'] for row in projection['cases'])
    assert not any(error.get('reason')=='case_projection_failed' for error in projection['errors'])
    reader=SmallReader(C.now());relative='judgments/'+judgment_id+'.json'
    assert reader.read(relative)
    before=reader.manifest[relative]['sha256']
    reader.cache.clear()
    path=J._path('judgments',judgment_id);value=read_stored(path);value['decision']='changed'
    from storage_fixture_helpers import replace_committed_fixture
    replace_committed_fixture(path,value)
    assert reader.read(relative) is None
    assert reader.manifest[relative]['sha256']==before
    assert any('SOURCE_CHANGED_AFTER_CACHE_EVICTION' in error.get('error','') for error in reader.errors)


def reservation(code='000001'):
    now = C.now()
    row = {'validation_id': 'validation-protected', 'registered_at': (now-timedelta(days=2)).isoformat(),
           'spec': {'scope': {'codes': [code], 'confidence_for': ['매수', '매도']},
                    'period': {'start': (now-timedelta(days=1)).isoformat(), 'end': (now+timedelta(days=1)).isoformat()}}}
    VS.archive('holdout_reservations', row['validation_id'], row)
    return row


def test_empty_read_missing_coverage_and_episode_never_write():
    import position_episodes as PE
    before = bytes_under(Path(C.WORK))
    result = Q.build_projection()
    assert result['targets'] == [] and result['cases'] == []
    assert JC.get_coverage('missing')['recording_status'] == 'recording_error'
    assert PE.read_episode('missing')['status'] == 'not_found'
    assert bytes_under(Path(C.WORK)) == before


def test_no_submission_target_and_rule_exit_dont_disappear(monkeypatch):
    fake, manifest = setup_run(monkeypatch)
    watch()
    before = bytes_under(Path(C.WORK))
    result = Q.build_projection()
    target = next(t for t in result['targets'] if t['run_attempt_id'] == manifest['run_attempt_id'])
    assert target['coverage_status'] == 'not_submitted'
    assert result['traces'][target['trace_id']]['calls']
    assert bytes_under(Path(C.WORK)) == before
    assert fake.orders == []


@pytest.mark.parametrize('failure', ['deleted', 'corrupt', 'commit_missing'])
def test_accepted_missing_original_retained_as_error(monkeypatch, failure):
    _, manifest, _, jid, _ = accepted(monkeypatch, place=False)
    value = J.load_judgment(jid)
    path = J._path('judgments', jid)
    if failure == 'deleted': path.unlink()
    elif failure == 'corrupt': path.write_text('{broken')
    else: J._path('submissions', value['acceptance_event_id']).unlink()
    before = bytes_under(Path(C.WORK))
    result = Q.build_projection()
    assert result['status'] == 'partial'
    target = next(t for t in result['targets'] if t['run_attempt_id'] == manifest['run_attempt_id'])
    assert target['connection_status'] == 'recording_error'
    assert result['traces'][target['trace_id']]['unavailable_judgments'][0]['judgment_id'] == jid
    robust = J.read_ai_judgments()
    assert robust['unavailable'][0]['judgment_id'] == jid
    assert bytes_under(Path(C.WORK)) == before


def test_historical_cutoff_excludes_later_delivery_judgment_and_call(monkeypatch):
    _, manifest = setup_run(monkeypatch)
    old = C.now()
    monkeypatch.setattr(C, 'now', lambda: old+timedelta(hours=1))
    submitted = M.record_ai_judgments([submission(M.get_briefing())])[0]
    watch(submitted['judgment_id'])
    result = Q.build_projection(knowledge_as_of=old.isoformat())
    target = next(t for t in result['targets'] if t['run_attempt_id'] == manifest['run_attempt_id'])
    assert target['coverage_status'] == 'not_submitted'
    detail = result['traces'][target['trace_id']]
    assert not detail['judgments'] and not detail['deliveries'] and not detail['calls']
    assert submitted['judgment_id'] not in json.dumps(result)


def test_current_holdout_protects_even_older_requested_cutoff(monkeypatch):
    _, manifest, app, jid, _ = accepted(monkeypatch, place=False)
    cutoff = C.now()
    monkeypatch.setattr(C, 'now', lambda: cutoff+timedelta(minutes=30))
    reservation()
    before = bytes_under(Path(C.WORK))
    result = Q.build_projection(knowledge_as_of=cutoff.isoformat())
    target = next(t for t in result['targets'] if t['run_attempt_id'] == manifest['run_attempt_id'])
    detail = Q.get_trace(result, target['trace_id'])
    assert target['visibility'] == detail['visibility'] == 'protected'
    assert 'deliveries' not in detail and 'judgments' not in detail
    assert app['application_id'] not in result['application_ids']
    assert not result['cases'] and not result['protection_scopes']
    assert bytes_under(Path(C.WORK)) == before


def test_shared_execution_and_receipt_cannot_leak_other_reserved_subject(monkeypatch):
    _, manifest, _, jid, _ = accepted(monkeypatch, place=False)
    judgment = J.load_judgment(jid)
    did = judgment['delivery_ids'][0]
    path = J._path('deliveries', did)
    receipt = read_stored(path)
    receipt['text'] = 'RESERVED_SENTINEL'
    receipt['evidence_catalog'].append({'subject': '000002', 'value': 'RESERVED_SENTINEL'})
    from storage_fixture_helpers import replace_committed_fixture
    replace_committed_fixture(path,receipt)
    reservation('000002')
    import evaluation_context as EC
    monkeypatch.setattr(EC, 'execution_context', lambda *a, **k: {'orders': [{'payload': {
        'code': '000002', 'private': 'RESERVED_SENTINEL'}, 'created_at': C.now().isoformat()}]})
    result = Q.build_projection()
    detail = result['traces'][next(t['trace_id'] for t in result['targets'] if t['code'] == '000001')]
    assert detail['deliveries'][0]['visibility'] == 'protected'
    assert detail['execution_context']['visibility'] == 'protected'
    assert 'RESERVED_SENTINEL' not in json.dumps(result)


def test_validation_start_and_commit_gap_not_reported_as_not_run():
    VS.archive('attempts', 'attempt-started', {'attempt_id': 'attempt-started',
        'validation_id': 'validation-one', 'result_id': 'result-one', 'started_at': C.now().isoformat()})
    before = bytes_under(Path(C.WORK))
    status = PV.get_policy_validation('validation-one')
    assert status['status'] == 'unfinished'
    page = PV.get_validation_cases('validation-one')
    assert page['total'] is None and page['count_known'] is False
    result = Q.build_projection()
    assert next(v for v in result['validations'] if v['validation_id'] == 'validation-one')['status'] == 'unfinished'
    assert bytes_under(Path(C.WORK)) == before


def test_committed_result_missing_input_is_not_complete_projection():
    row = {'validation_id': 'validation-one', 'result_id': 'result-one', 'created_at': C.now().isoformat(),
           'status': 'complete', 'input_id': 'input-one', 'input_hash': 'missing', 'cases': []}
    VS.archive('results', 'result-one', row)
    VS.archive('result_commits', 'result-one', {'validation_id': 'validation-one', 'result_id': 'result-one',
        'result_hash': VS.digest(row), 'committed_at': C.now().isoformat()})
    result = Q.build_projection()
    trial = result['validations'][0]
    assert trial['status'] == 'recording_error'
    assert trial['historical_result_status'] == 'complete'
    assert result['status'] == 'partial'


def test_source_ref_hashes_exact_bytes_and_queries_cannot_escape(monkeypatch):
    accepted(monkeypatch, place=False)
    before = bytes_under(Path(C.WORK))
    result = Q.build_projection()
    import hashlib
    for ref in result['source_manifest']:
        path = Path(C.WORK)/'ontology'/ref['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == ref['sha256']
    assert Q.get_trace(result, '../../account.json')['status'] == 'not_found'
    with pytest.raises(ValueError): Q.list_judgment_targets(result, limit=100000)
    assert bytes_under(Path(C.WORK)) == before


def test_current_policy_snapshot_is_distinct_from_past_execution():
    result = Q.build_projection()
    current = result['policies'][0]['current_applicability']
    assert current['scope'] == 'current_applicability_not_historical_execution'
    assert current['authority_snapshot']['params_hash'] == Q.digest(C.params())
    assert current['authority_snapshot']['captured_at'] == C.now().isoformat()


def test_changed_source_during_case_read_is_not_published_as_consistent(monkeypatch):
    _, _, app, _, _ = accepted(monkeypatch, place=False)
    import case_dataset as CD
    original = CD.build_cases
    def changed(*args):
        data = original(*args)
        path = J._path('premises', app['application_id'])
        body = read_stored(path); body['days'] = 999
        from storage_fixture_helpers import replace_committed_fixture
        replace_committed_fixture(path,body)
        return data
    monkeypatch.setattr(CD, 'build_cases', changed)
    result = Q.build_projection()
    assert result['status'] == 'recording_error'
    assert result['cases'] == []
    assert all('judgments' not in t for t in result['traces'].values())
    assert any(e.get('reason') == 'sources_changed_during_projection' for e in result['errors'])


def test_unknown_review_scope_is_withheld_before_aggregate_exposure():
    import case_store as CS
    CS.archive('jobs', 'review-orphan', {'review_id': 'review-orphan', 'created_at': C.now().isoformat()})
    CS.archive('results', 'review-orphan', {'review_id': 'review-orphan', 'created_at': C.now().isoformat(),
        'comparison': {'private_note': 'RESERVED_AGGREGATE_SENTINEL'}, 'analysis_status': 'complete'})
    reservation()
    result = Q.build_projection()
    review = result['reviews'][0]
    assert review['visibility'] == 'protected'
    assert review['reason'] == 'research_scope_unavailable_during_holdout'
    assert 'RESERVED_AGGREGATE_SENTINEL' not in json.dumps(result)


def test_malformed_manifest_retains_expected_target_from_submission(monkeypatch):
    _, manifest, _, jid, _ = accepted(monkeypatch, place=False)
    path = Path(C.WORK)/'ontology/attempts'/(manifest['run_attempt_id']+'.json')
    path.write_text('{}')
    result = Q.build_projection()
    assert result['status'] == 'partial'
    target = next(t for t in result['targets'] if t['run_attempt_id'] == manifest['run_attempt_id'])
    assert target['coverage_status'] == 'recording_error'
    assert jid in target['judgment_ids']
    assert result['errors']


def test_shared_receipt_subjects_have_future_holdout_recheck_scopes(monkeypatch):
    _, _, _, jid, _ = accepted(monkeypatch, place=False)
    did = J.load_judgment(jid)['delivery_ids'][0]
    path = J._path('deliveries', did)
    data = read_stored(path)
    data['evidence_catalog'].append({'subject': '000002', 'value': 4})
    from storage_fixture_helpers import replace_committed_fixture
    replace_committed_fixture(path,data)
    result = Q.build_projection()
    assert any(s['code'] == '000002' for s in result['protection_scopes'])


def test_untimed_dynamic_records_cannot_appear_as_historical_accepted_evidence(monkeypatch):
    _, _, _, jid, _ = accepted(monkeypatch, place=False)
    path = J._path('judgments', jid)
    body = read_stored(path); body.pop('created_at')
    from storage_fixture_helpers import replace_committed_fixture
    replace_committed_fixture(path,body)
    result = Q.build_projection()
    assert result['status'] == 'partial'
    assert not any(t.get('judgments') for t in result['traces'].values())
    assert any('SOURCE_AVAILABILITY_UNRECORDED' in e.get('error', '') for e in result['errors'])


def test_deleted_committed_trial_result_never_disappears():
    VS.archive('result_commits', 'result-lost', {'validation_id': 'validation-lost', 'result_id': 'result-lost',
        'result_hash': 'retained-proof', 'committed_at': C.now().isoformat()})
    before = bytes_under(Path(C.WORK))
    result = Q.build_projection()
    trial = next(v for v in result['validations'] if v['validation_id'] == 'validation-lost')
    assert trial['status'] == 'recording_error'
    assert trial['result_id'] == 'result-lost' and trial['reason'] == 'committed_result_missing'
    assert result['status'] == 'partial'
    assert bytes_under(Path(C.WORK)) == before
