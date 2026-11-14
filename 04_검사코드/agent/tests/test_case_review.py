"""Offline step 1–5 integration, research lifecycle and immutable publication."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from test_ontology import C, M, ROOT, isolated
from test_close_evaluation import accepted, at, calendar, price_provider, bytes_under
from test_judgment_integration import load_module
import close_evaluation as E
import case_dataset as D
import case_prediction as P
import case_statistics as ST
import case_store as S
import case_review as R
import judgment_coverage as JC


def completed_case(monkeypatch, calendar):
    fake, manifest, app, jid, plan = accepted(monkeypatch)
    provider = price_provider(calendar)
    at(monkeypatch, '2026-11-11T16:00:00+09:00')
    E.run_evaluation(calendar=calendar, provider=provider)
    at(monkeypatch, '2026-11-12T08:30:00+09:00')
    E.run_evaluation(calendar=calendar, provider=provider, phase='prep')
    at(monkeypatch, '2026-11-14T10:00:00+09:00')
    return fake, manifest, app


def runner():
    return load_module('case_review_runner_test', ROOT/'run.py')


def protect_trading():
    return {p: p.read_bytes() for p in Path(C.WORK).rglob('*')
            if p.is_file() and not p.name.startswith('storage_index.sqlite3') and '/case_review/' not in str(p)}


def test_real_step1_to_step5_preserves_trading_and_traces_one_case(monkeypatch, calendar):
    fake, manifest, app = completed_case(monkeypatch, calendar)
    protected, coverage = protect_trading(), JC.get_coverage(manifest['run_attempt_id'])
    report = runner().run_case_review(no_ai=True)
    assert report['analysis_status'] == 'complete', report.get('errors')
    assert report['run_status'] == 'analysis_only'
    assert report['expected_count'] == 1
    assert report['comparison']['summary']['refutation_rate'] == 1
    assert report['statistics']['models'] == []  # small competition sample is expected
    assert report['statistics']['groups'][0]['status'] in ('insufficient_data', 'unavailable')
    case = R.get_review_cases(report['review_id'])['cases'][0]
    assert case['application_id'] == app['application_id']
    assert case['origin_judgment_id'] == app['judgment_id']
    assert case['outcome_eligible'] is True
    assert case['evaluation_id']
    assert case.get('detail_required') or case.get('source_manifest')
    assert S.load('datasets',report['review_id'])['cases'][0]['source_manifest']
    assert JC.get_coverage(manifest['run_attempt_id']) == coverage
    assert len(fake.orders) == 1
    assert all(p.read_bytes() == b for p,b in protected.items())
    before = bytes_under(S.root())
    again = runner().run_case_review(no_ai=True)
    assert again['review_id'] == report['review_id']
    assert bytes_under(S.root()) == before
    usage = S.load('research_usage', report['review_id'])
    assert usage['application_ids'] == [app['application_id']]


def test_early_refutation_not_in_main_rate(monkeypatch, calendar):
    accepted(monkeypatch, place=False)
    provider = price_provider(calendar)
    at(monkeypatch, '2026-11-10T16:00:00+09:00')
    E.run_evaluation(calendar=calendar, provider=provider)
    at(monkeypatch, '2026-11-11T08:30:00+09:00')
    E.run_evaluation(calendar=calendar, provider=provider, phase='prep')
    report = R.build_review('2026-11-10', C.now().isoformat())
    assert report['expected_count'] == 1
    assert report['comparison']['summary']['refutation_rate'] is None
    assert report['comparison']['summary']['states'] == {'early_refuted': 1}


def test_revision_is_new_frozen_report_and_no_model_leak_on_error(monkeypatch, calendar):
    completed_case(monkeypatch, calendar)
    first = R.build_review()
    original = S.load('results', first['review_id'])
    second = R.build_review(revision='corrected-inputs')
    assert second['review_id'] != first['review_id']
    assert second['supersedes'] == first['review_id']
    assert S.load('results', first['review_id']) == original
    assert second['weekly_changes']['new'] == []
    monkeypatch.setattr(ST, 'fit_groups', lambda *a, **k: {'models': [{'model_id':'partial-model', 'status':'ready'}],
                       'groups': [], 'errors': [], 'status':'timeout'})
    partial = R.build_review(revision='timeout-test')
    assert partial['run_status'] == 'partial'
    assert S.load('publication_commits', partial['review_id']) is None


def test_zero_cases_no_ai_and_dry_no_write(monkeypatch):
    at(monkeypatch, '2026-11-14T10:00:00+09:00')
    run = runner()
    monkeypatch.setattr(run, 'case_review_ai', lambda *_: pytest.fail('no AI for no targets'))
    before = bytes_under(Path(C.WORK))
    run.run_case_review(dry=True)
    assert bytes_under(Path(C.WORK)) == before
    result = run.run_case_review()
    assert result['expected_count'] == 0
    assert result['ai_status'] == 'not_applicable'
    assert result['run_status'] == 'complete'


def test_source_corruption_never_becomes_empty_success(monkeypatch):
    import ai_judgment as J
    root = J._path('judgments', 'broken')
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text('{bad')
    at(monkeypatch, '2026-11-14T10:00:00+09:00')
    result = R.build_review()
    assert result['target_set_incomplete']
    assert result['analysis_status'] == 'partial'
    assert result['ai_status'] != 'not_applicable'
    assert S.load('publication_commits', result['review_id']) is None


def test_publication_commit_required_on_crash(monkeypatch):
    cutoff = datetime.fromisoformat('2026-11-14T10:00:00+09:00')
    archive = S.archive
    def fail(kind, key, value):
        if kind == 'publication_commits': raise OSError('disk unavailable')
        return archive(kind, key, value)
    monkeypatch.setattr(S, 'archive', fail)
    with pytest.raises(OSError):
        R._publish_models('review-partial', [{'model_id': 'model-one', 'status': 'ready'}], cutoff)
    assert len(S.records('model_publications')['records']) == 1
    assert S.load('publication_commits', 'review-partial') is None
    case = {'stratum': {}}
    assert P._choose_model(case, '2026-11-16T10:00:00+09:00') == (None, None)


def test_immutable_store_rejects_corruption_and_conflicting_retry():
    body = {'record_id':'one', 'value':4}
    assert S.archive('test', 'one', body) == body
    assert S.archive('test', 'one', body) == body
    with pytest.raises(S.StoreError): S.archive('test', 'one', {'value':5})
    path = S.root()/'test/one.json'
    raw = read_stored(path); raw['record']['value'] = 5
    path.write_text(json.dumps(raw))
    read = S.records('test')
    assert not read['records'] and read['errors']


@pytest.mark.parametrize('timestamp,expected', [
    ('2026-11-14T09:59:59+09:00', '2026-11-07T10:00:00+09:00'),
    ('2026-11-14T10:00:00+09:00', '2026-11-14T10:00:00+09:00'),
    ('2026-11-16T11:00:00+09:00', '2026-11-14T10:00:00+09:00')])
def test_weekly_cutoff(timestamp, expected):
    assert R.weekly_cutoff(timestamp).isoformat() == expected


@pytest.mark.parametrize('terminal', ['normal', 'timeout', 'error'])
def test_real_runner_ai_omission_timeout_error(monkeypatch, terminal):
    from test_case_review_tools import frozen_review
    rid = frozen_review(monkeypatch)
    run = runner()
    captured = {}
    class Fake:
        pid = 999999
        returncode = 0 if terminal != 'error' else 1
        def __init__(self, cmd, **kwargs): captured.update(cmd=cmd, **kwargs)
        def communicate(self, timeout):
            if terminal == 'timeout' and timeout > 30: raise subprocess.TimeoutExpired('fake', timeout)
            return '{}', ''
    monkeypatch.setattr(run.subprocess, 'Popen', Fake)
    monkeypatch.setattr(run.os, 'killpg', lambda *a: None)
    result = run.case_review_ai(rid)
    assert result['ai_status'] == 'not_submitted'
    assert result['ai_execution_status'] == terminal
    assert result['run_status'] == ('awaiting_ai' if terminal == 'normal' else 'partial')
    assert captured['env']['RUN_MODE'] == 'case_review'
    cmd = captured['cmd']
    assert cmd[cmd.index('--tools')+1] == ''
    allowed = cmd[cmd.index('--allowedTools')+1:cmd.index('--disallowedTools')]
    assert allowed == run.CASE_REVIEW_TOOLS
    denied = cmd[cmd.index('--disallowedTools')+1:]
    assert 'Read' in denied and 'WebSearch' in denied
    assert 'mcp__kiwoom__place_order' in denied
    before = bytes_under(S.root())
    run.case_review_ai(rid)
    assert bytes_under(S.root()) == before


def test_interrupted_ai_and_build_visible_without_repair(monkeypatch):
    from test_case_review_tools import frozen_review
    rid = frozen_review(monkeypatch)
    S.archive('ai_attempts', 'unfinished-ai', {'review_id':rid, 'attempt_id':'unfinished-ai'})
    before = bytes_under(S.root())
    assert R.get_case_review(rid)['ai_execution_status'] == 'unfinished'
    assert R.get_case_review(rid)['run_status'] == 'partial'
    S.archive('jobs','unfinished-build', {'review_id':'unfinished-build'})
    assert R.get_case_review('unfinished-build')['run_status'] == 'unfinished'
    assert all((S.root()/p).read_bytes() == b for p,b in before.items())


def test_recording_failure_not_coverage_failure(monkeypatch):
    from test_case_review_tools import frozen_review
    rid = frozen_review(monkeypatch)
    before = protect_trading()
    monkeypatch.setattr(S, 'archive', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    response = R.record_case_review(rid, 'disk', [{'id':'f','metric_id':'metric-rate','value':.25}], [])
    assert response['status'] == 'recording_error'
    report = R.get_case_review(rid)
    assert report['run_status'] == 'partial'
    assert report['recording_status'] == 'recording_error'
    assert all(p.read_bytes() == b for p,b in before.items())


def test_weekly_highlights_ignore_report_clock_but_track_selected_revision():
    original = {'application_id':'p1','features':{'gap':.02}, 'evaluation_id':'e1',
                'market_as_of':'2026-11-13', 'knowledge_as_of':'2026-11-14T10:00:00+09:00',
                'stale_evaluation':False, 'scoring_history':[{'attempt':'first'}]}
    S.archive('datasets', 'previous-week', {'cases':[original]})
    later = {**original, 'market_as_of':'2026-11-20', 'knowledge_as_of':'2026-11-21T10:00:00+09:00',
             'stale_evaluation':True, 'scoring_history':[{'attempt':'second'}]}
    diff = R._changes([later], None, {'review_id':'previous-week'})
    assert diff['unchanged'] == ['p1'] and not diff['changed']
    later['evaluation_id'] = 'e2'
    assert R._changes([later],None,{'review_id':'previous-week'})['changed'] == ['p1']


def test_calendar_cli_import_no_trading_or_collection(monkeypatch, tmp_path):
    from test_calendar_extension import reviewed
    import market_calendar as MC
    import sys
    source = tmp_path/'calendar.json'; source.write_text(json.dumps(reviewed()))
    run = runner()
    monkeypatch.setattr(run, 'refresh_account', lambda: pytest.fail('no broker'))
    monkeypatch.setattr(run, 'publish', lambda *a: pytest.fail('no trading publication'))
    monkeypatch.setattr(sys, 'argv', ['run.py', 'import-calendar', '--calendar-file', str(source), '--dry'])
    before = bytes_under(Path(C.WORK)); run.main()
    assert bytes_under(Path(C.WORK)) == before
    monkeypatch.setattr(sys, 'argv', ['run.py', 'import-calendar', '--calendar-file', str(source)])
    run.main()
    assert MC.find_calendar_snapshot('2027-01-01','2027-01-10',C.now().isoformat())['status'] == 'complete'


@pytest.mark.parametrize('key,findings,hypotheses', [('',[],[]),('a',{},[]),('a',[],{}),('a',[float('nan')],[])])
def test_schema_rejection_is_not_submission_omission(monkeypatch,key,findings,hypotheses):
    from test_case_review_tools import frozen_review
    rid = frozen_review(monkeypatch)
    result = R.record_case_review(rid,key,findings,hypotheses)
    assert result['status'] == 'rejected'
    report = R.get_case_review(rid)
    assert report['ai_status'] == 'rejected'
    assert report['ai_submissions'][0]['errors'][0]['code'] == 'SCHEMA_ERROR'


def test_concurrent_weekly_worker_does_not_poison_active_job(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    at(monkeypatch, '2026-11-14T10:00:00+09:00')
    entered, proceed = Event(), Event()
    build = D.build_cases
    def paused(*a):
        entered.set()
        assert proceed.wait(10)
        return build(*a)
    monkeypatch.setattr(D, 'build_cases', paused)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(R.build_review)
        assert entered.wait(10)
        second = R.build_review()
        assert second['run_status'] == 'unfinished'
        other = R.build_review(revision='concurrent-other-revision')
        assert other['run_status'] == 'busy'
        assert not S.records('errors')['records']
        proceed.set()
        done = first.result(timeout=10)
    assert done['run_status'] == 'complete'
    assert not S.records('job_failures')['records']


def test_concurrent_ai_worker_does_not_record_failure(monkeypatch):
    from test_case_review_tools import frozen_review
    from concurrent.futures import ThreadPoolExecutor
    rid = frozen_review(monkeypatch)
    run = runner()
    with S.locked('ai-'+rid):
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(run.case_review_ai,rid).result(timeout=5)
    assert result['worker_status'] == 'busy'
    assert not S.records('errors')['records']
    assert not S.records('ai_attempts')['records']


def test_result_before_publication_commit_stays_unfinished(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    at(monkeypatch, '2026-11-14T10:00:00+09:00')
    entered, proceed = Event(), Event()
    publish = R._publish_models
    def pause(*a):
        entered.set(); assert proceed.wait(10)
        return publish(*a)
    monkeypatch.setattr(R, '_publish_models', pause)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(R.build_review)
        assert entered.wait(10)
        halfway = R.build_review()
        assert halfway['stored_analysis_status'] == 'complete'
        assert halfway['analysis_status'] == 'unfinished'
        assert halfway['run_status'] == 'unfinished'
        proceed.set()
        assert future.result(timeout=10)['run_status'] == 'complete'
