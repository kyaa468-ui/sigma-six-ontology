"""Step-4 premise scoring with fixed local observations and immutable records."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import copy
import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ai_judgment as J
import common as C
import premise_evaluation as P
import scoring_store as S
from test_ai_judgment import receipt, item, result

NOW = datetime.fromisoformat('2026-11-16T10:00:00+09:00')
CUTOFF = NOW.isoformat()
SESSIONS = ['2026-11-09', '2026-11-10', '2026-11-11']


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'WORK', str(tmp_path))
    monkeypatch.setattr(C, 'now', lambda: NOW)
    monkeypatch.setattr(C, 'conf_boost_details', lambda *a, **k: {'raw': a[2], 'adjusted': a[2], 'note': None})
    monkeypatch.setenv('RUN_ID', 'ai-test')
    monkeypatch.setenv('RUN_ATTEMPT_ID', 'attempt-test')
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('Network forbidden'))
    return tmp_path


def application(direction='above', version='v2'):
    definition = J.PREMISE_DEFINITIONS['MA20_MAINTENANCE@' + version]
    return {'application_id': 'premise-test', 'judgment_id': 'judgment-test', 'code': '005930',
            'definition_id': definition['definition_id'], 'definition_snapshot': copy.deepcopy(definition),
            'judgment_date': '2026-11-06', 'created_at': '2026-11-06T10:00:00+09:00',
            'days': 3, 'direction': direction}


def window():
    return {'window_id': 'window-test', 'calendar_id': 'calendar-test', 'status': 'resolved',
            'sessions': SESSIONS, 'start_trading_day': SESSIONS[0], 'end_trading_day': SESSIONS[-1]}


def daily_row(day, close=20000, status='policy_complete', previous=20000):
    target = date.fromisoformat(day)
    dates = []
    while len(dates) < 20:
        if target.weekday() < 5:
            dates.append(target.isoformat())
        target -= timedelta(days=1)
    dates.reverse()
    values = [previous] * 19 + [close]
    exact = sum(Decimal(str(x)) for x in values) / Decimal(20)
    return {'date': day, 'status': status, 'close': close, 'ma20': float(exact), 'ma20_exact': str(exact),
            'available_at': '2026-11-12T08:30:00+09:00', 'instrument_kind': 'equity',
            'observation_ids': ['daily-' + day], 'ma20_input_ids': ['input-' + d for d in dates],
            'ma20_inputs': [{'date': d, 'close': v} for d, v in zip(dates, values)],
            'source': 'fixture', 'source_version': 'fixture-v1', 'price_basis': 'adjusted_close', 'provider_finality': 'not_provided',
            'calendar_id': 'calendar-test', 'calculation_version': 'session_aligned_sma20_decimal@v1',
            'completion_policy': J.PREMISE_DEFINITION_V2['completion_policy'],
            'confirmation_status': 'matched', 'confirmation_receipt_ids': ['first-' + day, 'second-' + day],
            'confirmed_at': '2026-11-12T08:30:00+09:00'}


def evaluate(rows, direction='above', market_as_of='2026-11-11', app=None, cutoff=CUTOFF):
    return P.evaluate_premise(app or application(direction), window(), rows, market_as_of, cutoff)


@pytest.mark.parametrize('direction', ['above', 'below'])
def test_equality_is_maintained_for_both_directions(direction):
    out = evaluate({d: daily_row(d) for d in SESSIONS}, direction)
    assert (out['verdict'], out['period_status'], out['data_status']) == ('maintained', 'ended', 'complete')
    assert all(x['equality'] for x in out['comparisons'])
    assert all(x['provider_finality'] == 'not_provided' for x in out['comparisons'])


def test_early_refutation_does_not_require_ended_horizon():
    rows = {SESSIONS[0]: daily_row(SESSIONS[0], 20400), SESSIONS[1]: daily_row(SESSIONS[1], 19700)}
    out = evaluate(rows, market_as_of=SESSIONS[1])
    assert out['verdict'] == 'refuted'
    assert out['period_status'] == 'in_progress'
    assert out['logical_decision_complete'] is True
    assert out['first_known_refutation']['date'] == SESSIONS[1]
    assert out['first_refutation_exact'] is True
    assert out['comparisons'][-1]['status'] == 'not_due'


def test_gap_before_known_refutation_prevents_exact_first_claim():
    rows = {SESSIONS[1]: daily_row(SESSIONS[1], 19700)}
    out = evaluate(rows)
    assert out['verdict'] == 'refuted'
    assert out['first_refutation_exact'] is False
    assert out['first_known_refutation']['observation_ids'] == ['daily-' + SESSIONS[1]]
    assert out['missing_sessions'] == [SESSIONS[0], SESSIONS[2]]
    assert 'earlier_sessions_unconfirmed' in out['reasons']


def test_below_refutes_when_close_is_above_same_day_average():
    rows = {d: daily_row(d) for d in SESSIONS}
    rows[SESSIONS[1]] = daily_row(SESSIONS[1], 20100)
    out = evaluate(rows, 'below')
    assert out['verdict'] == 'refuted'
    assert out['first_known_refutation']['operator'] == '>'


def test_provisional_counterexample_is_not_a_refutation():
    rows = {d: daily_row(d) for d in SESSIONS}
    rows[SESSIONS[1]] = daily_row(SESSIONS[1], 19000, 'provisional')
    out = evaluate(rows)
    assert out['verdict'] == 'unknown'
    assert out['period_status'] == 'ended'
    assert out['data_status'] == 'incomplete'
    assert out['first_known_refutation'] is None
    assert 'daily_provisional' in out['reasons']


def test_pending_period_and_data_status_are_independent():
    out = evaluate({}, market_as_of=SESSIONS[0])
    assert (out['verdict'], out['period_status'], out['data_status']) == ('pending', 'in_progress', 'incomplete')
    before = evaluate({}, market_as_of='2026-11-06')
    assert before['verdict'] == 'pending'
    assert before['period_status'] == 'not_started'


def test_last_session_before_special_close_is_still_in_progress():
    scheduled = window()
    scheduled['session_closes'] = {d: d + 'T16:30:00+09:00' for d in SESSIONS}
    morning = P.evaluate_premise(application(), scheduled, {}, SESSIONS[-1], SESSIONS[-1] + 'T08:30:00+09:00')
    ordinary_close = P.evaluate_premise(application(), scheduled, {}, SESSIONS[-1], SESSIONS[-1] + 'T15:30:00+09:00')
    after_close = P.evaluate_premise(application(), scheduled, {}, SESSIONS[-1], SESSIONS[-1] + 'T16:30:00+09:00')
    assert morning['period_status'] == ordinary_close['period_status'] == 'in_progress'
    assert morning['verdict'] == 'pending'
    assert after_close['period_status'] == 'ended'
    assert after_close['verdict'] == 'unknown'


def test_unverified_final_close_does_not_claim_the_period_ended():
    scheduled = window()
    scheduled['session_closes'] = {SESSIONS[-1]: None}
    out = P.evaluate_premise(application(), scheduled, {}, SESSIONS[-1], CUTOFF)
    assert out['period_status'] == 'calendar_unknown'
    assert 'final_session_close_unknown' in out['reasons']
    assert out['verdict'] == 'unknown'


@pytest.mark.parametrize('change,reason', [
    ({'available_at': '2026-11-17T09:00:00+09:00'}, 'future_availability'),
    ({'ma20_exact': '9999'}, 'ma20_calculation_mismatch'),
    ({'price_basis': 'provider_published_close'}, 'unknown_price_basis_or_source'),
    ({'confirmation_status': 'missing'}, 'completion_confirmation_missing'),
    ({'confirmation_receipt_ids': ['one', 'one']}, 'completion_confirmation_missing'),
])
def test_invalid_or_future_data_stays_unknown(change, reason):
    rows = {d: daily_row(d) for d in SESSIONS}
    rows[SESSIONS[1]].update(change)
    out = evaluate(rows)
    assert out['verdict'] == 'unknown'
    assert reason in out['reasons']


def test_unrounded_decimal_average_drives_comparison():
    rows = {d: daily_row(d) for d in SESSIONS}
    row = daily_row(SESSIONS[1], close='20000.000000001', previous='20000.000000002')
    row['ma20'] = 20000  # lossy compatibility field must not decide equality
    rows[SESSIONS[1]] = row
    out = evaluate(rows)
    assert out['verdict'] == 'refuted'
    assert Decimal(out['first_known_refutation']['ma20']) > Decimal(out['first_known_refutation']['close'])


def test_legacy_contract_is_not_filled_from_current_defaults():
    app = application(version='v1')
    before = copy.deepcopy(app)
    out = evaluate({}, app=app)
    assert out['verdict'] == 'unknown'
    assert out['reasons'] == ['definition_incomplete']
    assert app == before
    assert J.PREMISE_DEFINITION['scoring'] == 'not_implemented'
    full_legacy = copy.deepcopy(J.PREMISE_DEFINITION_V2)
    full_legacy['definition_id'] = 'MA20_MAINTENANCE@v1'
    app['definition_snapshot'] = full_legacy
    assert P.definition_compatibility(app)['binding'] == 'explicit_v1_full_contract'
    app['definition_snapshot']['equality'] = 'refuted'
    assert P.definition_compatibility(app)['reason'] == 'definition_conflict'


def submit_premise(key='first', **changes):
    receipt()
    spec = {'definition_id': 'MA20_MAINTENANCE@v2', 'days': 3, 'direction': 'above', 'claim_ids': ['fact-1']}
    spec.update(changes)
    out = result(item(key=key, premises=[spec]))
    assert out['status'] == 'accepted', out
    return out, J.load_judgment(out['judgment_id'])['premise_applications'][0]


def test_registration_stores_full_v2_and_reuse_preserves_original(monkeypatch):
    first, app = submit_premise()
    original = copy.deepcopy(app)
    monkeypatch.setattr(C, 'now', lambda: NOW + timedelta(days=1))
    second = result(item(key='reuse', premises=[{'application_id': app['application_id']}]))
    assert second['status'] == 'accepted', second
    targets = J.committed_premises((NOW + timedelta(days=1)).isoformat())
    assert not targets['target_set_incomplete']
    assert len(targets['applications']) == 1
    assert targets['applications'][0]['application'] == original
    assert set(targets['applications'][0]['judgment_ids']) == {first['judgment_id'], second['judgment_id']}
    assert app['definition_snapshot']['completion_policy'] == J.PREMISE_DEFINITION_V2['completion_policy']
    past = J.committed_premises(CUTOFF)
    assert past['applications'][0]['judgment_ids'] == [first['judgment_id']]


def test_orphan_premise_ignored_and_uncommitted_origin_not_reused():
    first, app = submit_premise()
    orphan = copy.deepcopy(app)
    orphan['application_id'] = 'premise-orphan'
    J._archive('premises', orphan['application_id'], orphan)
    targets = J.committed_premises(CUTOFF)
    assert [x['application']['application_id'] for x in targets['applications']] == [app['application_id']]
    reuse = result(item(key='bad-orphan', premises=[{'application_id': orphan['application_id']}]))
    assert reuse['status'] == 'rejected'
    assert reuse['errors'][0]['code'] == 'UNKNOWN_PREMISE'


@pytest.mark.parametrize('damage', ['missing_judgment', 'missing_commit', 'corrupt_premise'])
def test_broken_committed_targets_are_reported_not_silently_dropped(damage):
    output, app = submit_premise()
    judgment = J.load_judgment(output['judgment_id'])
    if damage == 'missing_judgment':
        J._path('judgments', output['judgment_id']).unlink()
    elif damage == 'missing_commit':
        J._path('submissions', judgment['acceptance_event_id']).unlink()
    else:
        J._path('premises', app['application_id']).write_text('{broken')
    targets = J.committed_premises(CUTOFF)
    assert targets['applications'] == []
    assert targets['target_set_incomplete'] is True
    assert targets['errors']


def test_readers_do_not_create_files_and_report_corruption(isolated):
    assert J.committed_premises(CUTOFF)['applications'] == []
    assert S.load('evaluations', 'missing') is None
    assert S.records('evaluations') == {'records': [], 'errors': []}
    assert list(isolated.iterdir()) == []
    S.archive('evaluations', 'broken', {'evaluation_id': 'broken'})
    (S.root() / 'evaluations' / 'broken.json').write_text('{')
    before = {p: p.stat().st_mtime_ns for p in isolated.rglob('*')}
    assert S.records('evaluations')['errors']
    assert before == {p: p.stat().st_mtime_ns for p in isolated.rglob('*')}


def test_store_atomic_concurrent_idempotence_and_conflict():
    def write(_):
        return S.archive('fixtures', 'one', {'value': 1})
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(x == {'value': 1} for x in pool.map(write, range(24)))
    with pytest.raises(S.StoreError):
        S.archive('fixtures', 'one', {'value': 2})
    assert S.load('fixtures', 'one') == {'value': 1}
    assert not list(S.root().rglob('*.tmp'))


def test_evaluation_dedup_revisions_and_definition_source_archive():
    rows = {d: daily_row(d) for d in SESSIONS}
    original = P.record_evaluation(evaluate(rows), 'job-first')
    assert original['recording_status'] == 'recorded'
    repeated = P.record_evaluation(evaluate(rows, cutoff=(NOW + timedelta(hours=1)).isoformat()), 'job-second')
    assert repeated['evaluation_id'] == original['evaluation_id']
    assert repeated['knowledge_as_of'] == original['knowledge_as_of']
    rows[SESSIONS[1]] = daily_row(SESSIONS[1], 19000)
    corrected = P.record_evaluation(evaluate(rows), 'job-correction')
    assert corrected['evaluation_id'] != original['evaluation_id']
    assert corrected['supersedes'] == [original['evaluation_id']]
    assert S.load('evaluations', original['evaluation_id'])['verdict'] == 'maintained'
    definition = S.load('definitions', corrected['evaluator_definition_id'])
    assert 'def evaluate_premise' in definition['sources']['premise_evaluation.py']
    assert definition['premise_definition']['definition_id'].endswith('@v2')


def test_evaluation_concurrent_duplicate_has_one_record():
    evaluated = evaluate({d: daily_row(d) for d in SESSIONS})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: P.record_evaluation(evaluated, 'job-' + str(n)), range(16)))
    assert all(x['recording_status'] == 'recorded' for x in results), results
    assert len({x['evaluation_id'] for x in results}) == 1
    assert len(S.records('evaluations')['records']) == 1


def test_revision_a_b_a_is_not_a_replay_of_ancestor():
    rows = {d: daily_row(d) for d in SESSIONS}
    first = P.record_evaluation(evaluate(rows), 'first-a')
    changed = copy.deepcopy(rows)
    changed[SESSIONS[1]] = daily_row(SESSIONS[1], 19000)
    second = P.record_evaluation(evaluate(changed), 'b')
    third = P.record_evaluation(evaluate(rows), 'second-a')
    assert len({x['evaluation_id'] for x in [first, second, third]}) == 3
    assert third['semantic_hash'] == first['semantic_hash']
    assert third['supersedes'] == [second['evaluation_id']]
    assert third['verdict'] == 'maintained'


def test_historical_backfill_does_not_replay_or_supersede_future_cutoff():
    rows = {d: daily_row(d) for d in SESSIONS}
    future = P.record_evaluation(evaluate(rows, cutoff=(NOW + timedelta(hours=2)).isoformat()), 'latest')
    historical = P.record_evaluation(evaluate(rows), 'historical')
    assert historical['evaluation_id'] != future['evaluation_id']
    assert historical['knowledge_as_of'] == CUTOFF
    assert future['evaluation_id'] not in historical['supersedes']
    assert historical['newer_revision_ids'] == [future['evaluation_id']]
    assert S.load('evaluations', future['evaluation_id']) == future


def test_store_detects_valid_json_content_corruption():
    S.archive('fixtures', 'one', {'value': 1})
    path = S.root() / 'fixtures' / 'one.json'
    payload = read_stored(path)
    payload['record']['value'] = 2
    path.write_text(json.dumps(payload))
    with pytest.raises(S.StoreError, match='hash mismatch|RECORD_FORMAT_CHANGED_AFTER_COMMIT'):
        S.load('fixtures', 'one')
    assert S.records('fixtures')['errors']


def test_failed_scoring_storage_does_not_mark_prior_ai_coverage(monkeypatch):
    import judgment_coverage as JC
    monkeypatch.setattr(JC, 'recording_error', lambda *a, **k: pytest.fail('Scoring cannot mutate AI coverage'))
    archive = S.archive
    def fail_evaluations(kind, identifier, body):
        if kind == 'evaluations':
            raise OSError('fixture storage failure')
        return archive(kind, identifier, body)
    monkeypatch.setattr(S, 'archive', fail_evaluations)
    out = P.record_evaluation(evaluate({d: daily_row(d) for d in SESSIONS}), 'scoring-job-test')
    assert out['recording_status'] == 'recording_error'
    assert 'evaluation_id' not in out
    assert S.errors('scoring-job-test')['records'][0]['operation'] == 'record_evaluation'
    assert S.records('evaluations')['records'] == []
