"""Explicit local calendar extension: reviewed source, time boundary and scoring."""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from test_ontology import C, clone, isolated
from test_close_evaluation import accepted, at, price_provider
import market_calendar as MC
import close_evaluation as E
import scoring_store as S


def reviewed(start='2027-01-01', end='2027-01-10', version='fixture-v1', overrides=None):
    # Synthetic schedule only; this helper makes no assertion about real 2027 KRX.
    rows, closed, d = [], [], date.fromisoformat(start)
    overrides = overrides or {}
    while d <= date.fromisoformat(end):
        value = d.isoformat()
        if d.weekday() >= 5 or overrides.get(value) == 'closed':
            closed.append({'date': value, 'reason': 'explicit synthetic closure'})
        else:
            rows.append({'date': value, 'open_at': value + 'T09:00:00+09:00',
                         'close_at': value + 'T' + overrides.get(value, '15:30') + ':00+09:00'})
        d += timedelta(days=1)
    return {'source': 'local synthetic fixture; not a KRX operating calendar', 'version': version,
            'reviewer': 'offline-test', 'coverage_start': start, 'coverage_end': end,
            'sessions': rows, 'closed_dates': closed}


def bytes_in_work():
    return {str(p.relative_to(C.WORK)): p.read_bytes() for p in Path(C.WORK).rglob('*') if p.is_file()}


def test_local_import_records_actual_availability_and_idempotent_first_receipt(monkeypatch, tmp_path):
    at(monkeypatch, '2026-12-20T10:00:00+09:00')
    body = reviewed(overrides={'2027-01-04': 'closed', '2027-01-05': '16:30'})
    body['available_at'] = '2020-01-01T00:00:00+09:00'
    source = tmp_path / 'reviewed.json'
    source.write_text(json.dumps(body))
    saved = MC.import_reviewed_calendar(source)
    assert saved['available_at'] == '2026-12-20T10:00:00+09:00'
    assert saved['reviewer'] == 'offline-test' and saved['source'] == body['source']
    assert MC.session_close(saved, '2027-01-05').endswith('16:30:00+09:00')
    assert '2027-01-04' not in MC.session_dates(saved)
    at(monkeypatch, '2026-12-21T10:00:00+09:00')
    again = MC.import_reviewed_calendar(source)
    assert again['calendar_id'] == saved['calendar_id'] and again['available_at'] == saved['available_at']
    before = bytes_in_work()
    assert MC.find_calendar_snapshot('2027-01-01', '2027-01-10', '2026-12-19T23:00:00+09:00')['status'] == 'unknown'
    assert MC.find_calendar_snapshot('2027-01-01', '2027-01-10', saved['available_at'])['calendar_id'] == saved['calendar_id']
    assert bytes_in_work() == before


@pytest.mark.parametrize('change', ['missing_day', 'duplicate', 'overlap', 'bounds', 'unknown_replacement', 'reviewer', 'version'])
def test_invalid_reviewed_snapshot_cannot_enter_history(change):
    body = reviewed()
    if change == 'missing_day': body['closed_dates'].pop()
    elif change == 'duplicate': body['sessions'].append(clone(body['sessions'][0]))
    elif change == 'overlap': body['closed_dates'].append({'date': body['sessions'][0]['date'], 'reason': 'overlap'})
    elif change == 'bounds': body['coverage_end'] = '2027-01-03'
    elif change == 'unknown_replacement': body['supersedes_calendar_ids'] = ['calendar-missing']
    else: body.pop(change)
    before = bytes_in_work()
    with pytest.raises((ValueError, KeyError)):
        MC.register_reviewed_calendar(body)
    assert bytes_in_work() == before


def test_conflict_requires_explicit_reviewed_revision_and_never_rewrites_past(monkeypatch):
    at(monkeypatch, '2026-12-20T10:00:00+09:00')
    first = MC.register_reviewed_calendar(reviewed())
    at(monkeypatch, '2026-12-21T10:00:00+09:00')
    second = MC.register_reviewed_calendar(reviewed(version='fixture-v2', overrides={'2027-01-05': '16:30'}))
    conflict = MC.find_calendar_snapshot('2027-01-01', '2027-01-10', C.now())
    assert conflict['reason'] == 'calendar_conflict' and conflict['sessions'] == []
    assert conflict['conflicts'][0]['date'] == '2027-01-05'
    corrected = reviewed(version='fixture-v3', overrides={'2027-01-05': '16:30'})
    corrected['supersedes_calendar_ids'] = [first['calendar_id'], second['calendar_id']]
    at(monkeypatch, '2026-12-22T10:00:00+09:00')
    third = MC.register_reviewed_calendar(corrected)
    assert MC.find_calendar_snapshot('2027-01-01', '2027-01-10', C.now())['calendar_id'] == third['calendar_id']
    historical = MC.find_calendar_snapshot('2027-01-01', '2027-01-10', first['available_at'])
    assert historical['calendar_id'] == first['calendar_id']
    assert MC.session_close(historical, '2027-01-05').endswith('15:30:00+09:00')


def test_partial_and_adjacent_snapshots_do_not_infer_unreviewed_dates():
    first = MC.register_reviewed_calendar(reviewed(end='2027-01-05'))
    partial = MC.find_calendar_snapshot('2027-01-01', '2027-01-10', C.now(), allow_partial=True)
    assert partial['partial_requested_coverage'] is True and partial['coverage_end'] == '2027-01-05'
    assert MC.find_calendar_snapshot('2027-01-01', '2027-01-10', C.now())['status'] == 'unknown'
    second = MC.register_reviewed_calendar(reviewed(start='2027-01-06'))
    combined = MC.find_calendar_snapshot('2027-01-01', '2027-01-10', C.now())
    assert combined['status'] == 'complete' and combined['partial_requested_coverage'] is False
    assert set(combined['component_calendar_ids']) == {first['calendar_id'], second['calendar_id']}
    assert MC.resolve_window({'application_id': 'local', 'judgment_date': '2027-01-05', 'days': 3}, combined)['sessions'] == ['2027-01-06', '2027-01-07', '2027-01-08']
    MC.register_reviewed_calendar(reviewed(start='2027-01-12', end='2027-01-20'))
    gap = MC.find_calendar_snapshot('2027-01-01', '2027-01-20', C.now(), allow_partial=True)
    assert gap['partial_requested_coverage'] is True
    assert not (gap['coverage_start'] <= '2027-01-11' <= gap['coverage_end'])


def test_explicit_review_overrides_package_without_using_future_revision(monkeypatch):
    at(monkeypatch, '2026-11-06T08:30:00+09:00')
    default = MC.load_calendar_snapshot('2026-11-01', '2026-11-30')
    at(monkeypatch, '2026-11-07T10:00:00+09:00')
    body = reviewed('2026-11-19', '2026-11-19', overrides={'2026-11-19': '16:30'})
    extension = MC.register_reviewed_calendar(body)
    current = MC.find_calendar_snapshot('2026-11-01', '2026-11-30', C.now())
    assert MC.session_close(current, '2026-11-19').endswith('16:30:00+09:00')
    assert set(current['component_calendar_ids']) == {extension['calendar_id'], default['calendar_id']}
    past = MC.find_calendar_snapshot('2026-11-01', '2026-11-30', default['available_at'])
    assert MC.session_close(past, '2026-11-19') is None


def test_unknown_future_calendar_never_calls_unreviewed_package(monkeypatch):
    calls = []
    monkeypatch.setattr(MC, '_package_calendar', lambda *args: calls.append(args))
    result = MC.select_calendar_snapshot('2027-01-01', '2027-04-01', C.now(), collect_default=True)
    assert result['status'] == 'unknown' and calls == []


def test_explicit_historical_selector_cannot_collect_todays_package(monkeypatch):
    calls = []
    monkeypatch.setattr(MC, '_package_calendar', lambda *args: calls.append(args))
    before = bytes_in_work()
    result = MC.select_calendar_snapshot('2026-11-01', '2026-11-30',
                                         '2026-11-05T16:00:00+09:00', collect_default=True)
    assert result['status'] == 'unknown' and calls == [] and bytes_in_work() == before


def test_existing_2026_default_stays_partial_for_future_requested_range():
    selected = MC.select_calendar_snapshot('2026-12-01', '2027-02-01', C.now(), collect_default=True)
    assert selected['status'] == 'complete' and selected['coverage_end'] == '2026-12-31'
    assert selected['partial_requested_coverage'] is True
    assert selected['requested_coverage_end'] == '2027-02-01'


def test_close_scoring_uses_reviewed_2027_calendar_without_changing_completion_policy(monkeypatch):
    at(monkeypatch, '2027-01-04T10:30:00+09:00')
    _, _, app, _, _ = accepted(monkeypatch, days=1, place=False)
    calendar = MC.register_reviewed_calendar(reviewed('2026-12-01', '2027-01-31'))
    provider = price_provider(calendar)
    monkeypatch.setattr(MC, '_package_calendar', lambda *a: pytest.fail('reviewed local schedule must take priority'))
    at(monkeypatch, '2027-01-05T16:00:00+09:00')
    first = E.run_evaluation(provider=provider)
    assert first['counts'] == {'unknown': 1}
    at(monkeypatch, '2027-01-06T08:30:00+09:00')
    confirmed = E.run_evaluation(provider=provider, phase='prep')
    assert confirmed['counts'] == {'maintained': 1}
    evaluation = E.get_premise_evaluations(application_id=app['application_id'])['evaluations'][-1]
    assert evaluation['window_snapshot']['sessions'] == ['2027-01-05']
    assert evaluation['application_snapshot'] == app
