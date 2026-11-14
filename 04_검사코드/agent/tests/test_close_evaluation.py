"""Actual step 1–4 tool path, local receipts only; all paths are temporary."""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from test_ontology import C, D, M, O, ROOT, clone, isolated
from test_judgment_integration import setup_run, submission, load_module
import ai_judgment as J
import close_evaluation as E
import daily_observations as DO
import judgment_coverage as JC
import market_calendar as MC
import premise_evaluation as PE
import scoring_store as S


def at(monkeypatch, value):
    monkeypatch.setattr(C, 'now', lambda: datetime.fromisoformat(value))


@pytest.fixture
def calendar():
    def provider(start, end):
        rows, d = [], date.fromisoformat(start)
        while d <= date.fromisoformat(end):
            if d.weekday() < 5:
                s = d.isoformat()
                rows.append({'date': s, 'open_at': s+'T09:00:00+09:00', 'close_at': s+'T15:30:00+09:00'})
            d += timedelta(days=1)
        return {'source': 'local-fixture', 'version': '1', 'sessions': rows}
    return MC.load_calendar_snapshot('2026-09-01', '2026-12-15', provider)


def price_provider(calendar, changes=None):
    prices = {'2026-11-03': 20800, '2026-11-09': 20500,
              '2026-11-10': 19700, '2026-11-11': 20500, **(changes or {})}
    calls = []
    def fetch(code, start, end, kind):
        calls.append((code, start, end))
        return {'source': 'local-fixture', 'version': '1', 'price_basis': DO.PRICE_BASIS[kind],
                'provider_finality': 'not_provided',
                'rows': [{'date': d, 'close': prices.get(d, 20000)}
                         for d in MC.session_dates(calendar) if start <= d <= end]}
    fetch.calls = calls
    return fetch


def accepted(monkeypatch, *, version='v2', days=3, place=True):
    fake, manifest = setup_run(monkeypatch)
    briefing = C.load_briefing()
    selection = O.SelectionTrace('prep', C.params(), '상승', 'local_fixture')
    candidates = D.screen(clone(briefing['candidates']), C.params(), True, '상승', trace=selection)
    selection.attach(candidates)
    briefing['candidates'] = candidates
    selection.finish(briefing)
    C.write_json(C.BRIEFING, briefing)
    item = submission(M.get_briefing())
    item['premises'] = [{'definition_id': f'MA20_MAINTENANCE@{version}', 'days': days,
                         'direction': 'above', 'claim_ids': ['inst']}]
    result = M.record_ai_judgments([item])[0]
    assert result['status'] == 'accepted', result
    jid = result['judgment_id']
    application = J.load_judgment(jid)['premise_applications'][0]
    plan = None
    if place:
        plan = M.plan_order('000001', '매수', .7, judgment_id=jid)
        placed = M.place_order('000001', '매수', 1, .7, 'bull', 'bear', 'local',
            judgment_id=jid, plan_id=plan['plan_id'], order_attempt_id=plan['order_attempt_id'])
        assert placed['ok']
    return fake, manifest, application, jid, plan


def bytes_under(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}


def test_real_tools_to_confirmed_refutation_without_trading_side_effects(monkeypatch, calendar):
    fake, manifest, app, jid, plan = accepted(monkeypatch)
    baseline = JC.get_coverage(manifest['run_attempt_id'])
    protected = {p: p.read_bytes() for p in Path(C.WORK).rglob('*')
                 if p.is_file() and not p.name.startswith('storage_index.sqlite3') and '/scoring/' not in str(p)}
    provider = price_provider(calendar)
    at(monkeypatch, '2026-11-09T16:00:00+09:00')
    first = E.run_evaluation(calendar=calendar, provider=provider)
    assert first['expected_count'] == 1 and first['counts'] == {'pending': 1}
    at(monkeypatch, '2026-11-10T16:00:00+09:00')
    middle = E.run_evaluation(calendar=calendar, provider=provider)
    assert middle['counts'] == {'pending': 1}  # D2 is provisional
    at(monkeypatch, '2026-11-11T08:30:00+09:00')
    completed = E.run_evaluation(calendar=calendar, provider=provider, phase='prep')
    assert completed['counts'] == {'refuted': 1}
    result = M.get_premise_evaluations(application_id=app['application_id'])['evaluations'][-1]
    assert result['first_known_refutation']['date'] == '2026-11-10'
    assert result['first_known_refutation']['close'] == '19700'
    assert float(result['first_known_refutation']['ma20']) == 20050
    assert result['first_refutation_exact'] is True
    assert result['data_status'] != 'complete'  # D3 has not closed yet
    assert result['logical_decision_complete'] is True
    assert result['application_snapshot'] == app
    assert all(c['provider_finality'] == 'not_provided'
               for c in result['comparisons'] if c.get('source_snapshot'))
    job = M.get_close_evaluations(scoring_job_id=completed['scoring_job_id'])['jobs'][0]
    assert job['detail_required']
    full_job=S.load('job_results',completed['scoring_job_id'])
    assert full_job['items'][0]['execution_context']['orders']
    assert JC.get_coverage(manifest['run_attempt_id']) == baseline
    assert len(fake.orders) == 1
    assert all(p.read_bytes() == data for p, data in protected.items())
    before = bytes_under(Path(C.WORK))
    M.get_premise_evaluations(judgment_id=jid, scoring_job_id=completed['scoring_job_id'], include_history=True)
    M.get_close_evaluations()
    assert bytes_under(Path(C.WORK)) == before, 'query tools must be read-only'


def test_repeat_job_reuses_evaluation_and_has_independent_job_id(monkeypatch, calendar):
    accepted(monkeypatch, place=False)
    at(monkeypatch, '2026-11-09T16:00:00+09:00')
    provider = price_provider(calendar)
    a = E.run_evaluation(calendar=calendar, provider=provider)
    b = E.run_evaluation(calendar=calendar, provider=provider)
    assert a['scoring_job_id'] != b['scoring_job_id']
    assert a['items'][0]['evaluation_id'] == b['items'][0]['evaluation_id']
    assert len(S.records('evaluations')['records']) == 1
    assert M.get_premise_evaluations(scoring_job_id=b['scoring_job_id'])['evaluations']


def test_explicit_past_cutoff_uses_archived_inputs_only(monkeypatch, calendar):
    accepted(monkeypatch, place=False)
    provider = price_provider(calendar)
    at(monkeypatch, '2026-11-09T16:00:00+09:00')
    original = E.run_evaluation(calendar=calendar, provider=provider)
    cutoff = C.now().isoformat()
    at(monkeypatch, '2026-11-12T08:30:00+09:00')
    def forbidden(*a): pytest.fail('historical replay must not fetch corrected current data')
    replay = E.run_evaluation('2026-11-09', cutoff, calendar=calendar, provider=forbidden)
    assert replay['items'][0]['evaluation_id'] == original['items'][0]['evaluation_id']
    assert M.get_premise_evaluations(knowledge_as_of=cutoff)['evaluations'][0]['verdict'] == 'pending'


def test_historical_default_calendar_uses_real_coverage_without_convenience_padding(monkeypatch, calendar):
    accepted(monkeypatch, place=False)
    at(monkeypatch, '2026-11-09T16:00:00+09:00')
    original = E.run_evaluation(calendar=calendar, provider=price_provider(calendar))
    cutoff = C.now().isoformat()
    at(monkeypatch, '2026-11-12T08:30:00+09:00')
    def forbidden(*a, **k): pytest.fail('historical replay may only read archived calendar/daily receipts')
    monkeypatch.setattr(MC, 'load_calendar_snapshot', forbidden)
    replay = E.run_evaluation('2026-11-09', cutoff, provider=forbidden)
    assert replay['items'][0]['evaluation_id'] == original['items'][0]['evaluation_id']


def test_old_v1_definition_is_not_filled_in_from_new_defaults(monkeypatch, calendar):
    _, _, app, _, _ = accepted(monkeypatch, version='v1', place=False)
    before = clone(app)
    at(monkeypatch, '2026-11-12T08:30:00+09:00')
    result = E.run_evaluation(calendar=calendar, provider=price_provider(calendar))
    assert result['counts'] == {'unknown': 1}
    row = M.get_premise_evaluations()['evaluations'][0]
    assert 'definition_incomplete' in row['reasons']
    assert row['application_snapshot'] == before


@pytest.mark.parametrize('failure_kind', ['legacy', 'calendar', 'daily', 'evaluation', 'finish'])
def test_scoring_failure_does_not_modify_judgment_coverage(monkeypatch, calendar, failure_kind):
    fake, manifest, _, _, _ = accepted(monkeypatch)
    before = JC.get_coverage(manifest['run_attempt_id'])
    at(monkeypatch, '2026-11-09T16:00:00+09:00')
    def fail(*a, **k): raise OSError('local injected scoring failure')
    if failure_kind == 'legacy':
        import evaluation_context
        monkeypatch.setattr(evaluation_context, 'capture_legacy_context', fail)
    elif failure_kind == 'calendar':
        monkeypatch.setattr(MC, 'resolve_window', fail)
    elif failure_kind == 'daily':
        monkeypatch.setattr(DO, 'collect_daily_observations', fail)
    elif failure_kind == 'evaluation':
        monkeypatch.setattr(PE, 'record_evaluation', fail)
    else:
        original = S.archive
        monkeypatch.setattr(S, 'archive', lambda kind, *a, **k: fail() if kind == 'job_results' else original(kind, *a, **k))
    report = E.run_evaluation(calendar=calendar, provider=price_provider(calendar))
    assert report['run_status'] != 'completed'
    assert JC.get_coverage(manifest['run_attempt_id']) == before
    assert len(fake.orders) == 1
    if failure_kind == 'finish':
        job = M.get_close_evaluations(scoring_job_id=report['scoring_job_id'])['jobs'][0]
        assert job['run_status'] == 'unfinished' and job['conclusive_rate'] is None


def test_budget_does_not_change_original_horizon(monkeypatch, calendar):
    _, _, app, _, _ = accepted(monkeypatch, place=False)
    at(monkeypatch, '2026-11-12T08:30:00+09:00')
    provider = price_provider(calendar)
    report = E.run_evaluation(calendar=calendar, provider=provider, max_symbols=0)
    assert not provider.calls and report['counts'] == {'unknown': 1}
    row = M.get_premise_evaluations()['evaluations'][0]
    assert row['application_snapshot'] == app
    assert row['window_snapshot']['sessions'] == ['2026-11-09','2026-11-10','2026-11-11']
    assert row['collection_errors'][0]['reason'] == 'collection_budget'


def test_empty_and_corrupt_target_sets_are_not_100_percent(monkeypatch, calendar):
    setup_run(monkeypatch)
    report = E.run_evaluation(calendar=calendar)
    assert report['expected_count'] == 0 and report['rate_status'] == 'not_applicable'
    directory = Path(C.WORK)/'ontology/judgments'
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'broken.json').write_text('{')
    report = E.run_evaluation(calendar=calendar)
    assert report['target_set_incomplete'] and report['conclusive_rate'] is None
    assert report['rate_status'] == 'unknown'


@pytest.mark.parametrize('kind', ['prep', 'close'])
def test_runner_account_failure_still_attempts_independent_scoring(monkeypatch, kind):
    runner = load_module('step4_runner_'+kind, ROOT/'run.py')
    events = []
    def failed_account(): raise RuntimeError('offline account unavailable')
    monkeypatch.setattr(runner, 'refresh_account', failed_account)
    monkeypatch.setattr(runner, 'score_close', lambda phase, *a, **k: events.append(phase))
    monkeypatch.setattr(runner.sys, 'argv', ['run.py', kind])
    with pytest.raises(RuntimeError, match='account unavailable'): runner.main()
    assert events == [kind]


def test_explicit_score_dry_and_read_tools_do_not_enter_trade_or_tuning(monkeypatch):
    runner = load_module('step4_runner_score', ROOT/'run.py')
    def fail(*a, **k): pytest.fail('unexpected execution')
    for name in ('refresh_account','refresh_executions','publish','claude','acquire'):
        monkeypatch.setattr(runner, name, fail)
    monkeypatch.setattr(E, 'run_evaluation', fail)
    monkeypatch.setattr(runner.sys, 'argv', ['run.py','score','--dry'])
    runner.main()
    assert 'mcp__kiwoom__get_close_evaluations' not in runner.TOOLS_ALLOW
    assert 'mcp__kiwoom__get_premise_evaluations' not in runner.TOOLS_ALLOW
    assert M.get_close_evaluations()['jobs'] == []
    assert M.get_premise_evaluations()['evaluations'] == []


def test_new_scoring_does_not_change_tuning_inputs(monkeypatch, calendar):
    accepted(monkeypatch, place=False)
    at(monkeypatch, '2026-11-12T08:30:00+09:00')
    before = M.get_tuning_context()
    E.run_evaluation(calendar=calendar, provider=price_provider(calendar))
    assert M.get_tuning_context() == before
    assert not Path(M.tuning.LESSONS).exists()


def test_live_new_calendar_does_not_force_completed_data_reread(monkeypatch, calendar):
    accepted(monkeypatch, place=False, days=1)
    provider = price_provider(calendar)
    at(monkeypatch, '2026-11-09T16:00:00+09:00')
    E.run_evaluation(calendar=calendar, provider=provider)
    at(monkeypatch, '2026-11-10T08:30:00+09:00')
    assert E.run_evaluation(calendar=calendar, provider=provider)['counts'] == {'maintained': 1}
    cursor = [datetime.fromisoformat('2026-11-11T08:30:00+09:00')]
    def ticking():
        cursor[0] += timedelta(microseconds=1)
        return cursor[0]
    monkeypatch.setattr(C, 'now', ticking)
    def new_calendar(*a, **k):
        return {**calendar, 'calendar_id': 'calendar-new-bounds', 'available_at': C.now().isoformat()}
    monkeypatch.setattr(MC, 'load_calendar_snapshot', new_calendar)
    def forbidden(*a): pytest.fail('complete rows should be reused in automatic prep')
    report = E.run_evaluation(provider=forbidden, phase='prep')
    assert report['counts'] == {'maintained': 1}
    assert report['recording_status'] == 'recorded'


def test_budget_rotates_pending_symbols_and_does_not_starve_new_targets(monkeypatch, calendar):
    _, _, application, jid, _ = accepted(monkeypatch, place=False)
    second = {**application, 'code': '000002', 'application_id': 'local-second-application'}
    monkeypatch.setattr(PE, 'committed_premises', lambda **kw: {
        'applications': [{'application': a, 'judgment_ids': [jid]} for a in (application, second)],
        'errors': [], 'target_set_incomplete': False})
    at(monkeypatch, '2026-11-09T16:00:00+09:00')
    provider = price_provider(calendar)
    E.run_evaluation(calendar=calendar, provider=provider, max_symbols=1)
    assert [x[0] for x in provider.calls] == ['000001']
    at(monkeypatch, '2026-11-09T16:01:00+09:00')
    E.run_evaluation(calendar=calendar, provider=provider, max_symbols=1)
    assert [x[0] for x in provider.calls] == ['000001', '000002']


@pytest.mark.parametrize('where', ['calendar','window','daily_receipts'])
def test_returned_recording_failure_is_not_reported_as_recorded(monkeypatch, calendar, where):
    accepted(monkeypatch, place=False)
    at(monkeypatch, '2026-11-09T16:00:00+09:00')
    if where == 'calendar':
        calendar = {**calendar, 'status': 'unknown', 'recording_status': 'recording_error', 'reason': 'calendar_save_failed'}
    elif where == 'window':
        monkeypatch.setattr(MC, 'resolve_window', lambda *a: {'status': 'unknown', 'recording_status': 'recording_error'})
    else:
        directory = S.root()/'daily_receipts'
        directory.mkdir()
        (directory/'broken.json').write_text('{')
    report = E.run_evaluation(calendar=calendar, provider=price_provider(calendar))
    assert report['recording_status'] == 'recording_error'
    assert report['conclusive_rate'] is None
    assert report['counts'] in ({'unknown':1}, {'pending':1})


def test_explicit_score_runner_uses_actual_scorer_with_local_daily_provider(monkeypatch, calendar):
    fake, _, _, _, _ = accepted(monkeypatch, place=False)
    at(monkeypatch, '2026-11-09T16:00:00+09:00')
    monkeypatch.setattr(MC, 'load_calendar_snapshot', lambda *a: calendar)
    monkeypatch.setattr(DO, 'pykrx_provider', price_provider(calendar))
    runner = load_module('step4_score_actual', ROOT/'run.py')
    def forbidden(*a, **k): pytest.fail('score may not trade, call AI, publish, or refresh account')
    for name in ('refresh_account','refresh_executions','publish','claude','acquire'):
        monkeypatch.setattr(runner, name, forbidden)
    monkeypatch.setattr(runner.sys, 'argv', ['run.py','score','--as-of','2026-11-09'])
    runner.main()
    assert M.get_close_evaluations()['jobs'][0]['counts'] == {'pending':1}
    assert not fake.orders
