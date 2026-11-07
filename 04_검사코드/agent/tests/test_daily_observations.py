"""Offline daily completion policy. No broker, quote, model, or network is used."""
import copy
import socket
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import common as C
import daily_observations as D
import market_calendar as M
import scoring_store as S


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'WORK', str(tmp_path))
    monkeypatch.setattr(C, 'now', lambda: datetime.fromisoformat('2026-11-06T16:00:00+09:00'))
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('Network forbidden'))


def clock(monkeypatch, timestamp):
    monkeypatch.setattr(C, 'now', lambda: datetime.fromisoformat(timestamp))


def fixture_provider(start, end):
    # A deliberately synthetic calendar: Nov 9 is a fixture-only holiday.
    rows = []
    current = date.fromisoformat(start)
    while current <= date.fromisoformat(end):
        d = current.isoformat()
        if current.weekday() < 5 and d != '2026-11-09':
            close = '16:30' if d == '2026-11-19' else '15:30'
            rows.append({'date': d, 'open_at': d + 'T09:00:00+09:00', 'close_at': d + 'T' + close + ':00+09:00'})
        current += timedelta(days=1)
    return {'sessions': rows, 'source': 'local_fixture', 'version': 'fixture-v1',
            'coverage_start': start, 'coverage_end': end}


@pytest.fixture
def calendar():
    return M.load_calendar_snapshot('2026-09-01', '2026-12-10', fixture_provider)


def provider(calendar, changes=None, **metadata):
    changes = changes or {}
    def fetch(code, start, end, kind):
        return {'source': 'local:daily', 'version': 'fixture-v1',
                'price_basis': D.PRICE_BASIS[kind], 'provider_finality': 'not_provided', **metadata,
                'rows': [{'date': d, 'close': changes.get(d, 20000), 'volume': 0}
                         for d in M.session_dates(calendar) if start <= d <= end and changes.get(d, 0) != 'missing']}
    return fetch


def collect(calendar, provider_=None, **kw):
    return D.collect_daily_observations('005930', ['2026-11-06'], calendar,
                                        provider=provider_ or provider(calendar), **kw)


def test_calendar_window_is_immutable_and_skips_holiday(calendar):
    application = {'application_id': 'p1', 'judgment_date': '2026-11-06', 'days': 3}
    original = copy.deepcopy(application)
    result = M.resolve_window(application, calendar)
    assert result['sessions'] == ['2026-11-10', '2026-11-11', '2026-11-12']
    assert application == original
    assert result['end_trading_day'] == '2026-11-12'
    assert result['session_closes']['2026-11-12'] == '2026-11-12T15:30:00+09:00'
    assert M.resolve_window(application, calendar)['window_id'] == result['window_id']
    assert M.resolve_window({**application, 'days': 100}, calendar)['status'] == 'unknown'


def test_window_carries_special_and_unconfirmed_closing_times(calendar):
    application = {'application_id': 'special-p', 'judgment_date': '2026-11-18', 'days': 1}
    assert M.resolve_window(application, calendar)['session_closes'] == {'2026-11-19': '2026-11-19T16:30:00+09:00'}
    reviewed = M.load_calendar_snapshot('2026-11-01', '2026-11-30')
    assert M.resolve_window(application, reviewed)['session_closes'] == {'2026-11-19': None}


def test_bad_calendar_never_guesses_weekdays():
    broken = lambda *args: {'sessions': [], 'source': 'fixture', 'version': '1', 'status': 'unknown'}
    result = M.load_calendar_snapshot('2026-11-01', '2026-11-10', broken)
    assert result['status'] == 'unknown' and result['sessions'] == []
    def duplicate(start, end):
        body = fixture_provider(start, end)
        body['sessions'].append(body['sessions'][0])
        return body
    assert M.load_calendar_snapshot('2026-11-01', '2026-11-10', duplicate)['status'] == 'unknown'


def test_real_pinned_calendar_known_holiday_and_historic_special_close():
    result = M.load_calendar_snapshot('2020-12-01', '2020-12-28')
    assert result['status'] == 'complete'
    assert result['version'] == '4.13.2'
    assert '2020-12-25' not in M.session_dates(result)
    assert M.session_close(result, '2020-12-03').endswith('16:30:00+09:00')


def test_2026_reviewed_amendments_and_unverified_close():
    result = M.load_calendar_snapshot('2026-01-01', '2026-12-31')
    assert result['status'] == 'complete'
    assert not {'2026-06-03', '2026-07-17', '2026-08-17'} & set(M.session_dates(result))
    assert M.session_close(result, '2026-11-19') is None
    assert next(x for x in result['sessions'] if x['date'] == '2026-01-02')['open_at'].endswith('10:00:00+09:00')
    assert M.load_calendar_snapshot('2027-01-01', '2027-02-01')['status'] == 'unknown'
    assert M.load_calendar_snapshot('2025-10-01', '2025-12-01')['status'] == 'unknown'


def test_calendar_first_availability_not_backdated_and_read_only_history(calendar, monkeypatch):
    original_time = calendar['available_at']
    assert M.find_calendar_snapshot('2026-10-01', '2026-12-01', '2026-11-05T16:00:00+09:00')['status'] == 'unknown'
    current = M.find_calendar_snapshot('2026-10-01', '2026-12-01', '2026-11-06T16:00:00+09:00')
    assert current['calendar_id'] == calendar['calendar_id']
    clock(monkeypatch, '2026-11-10T08:30:00+09:00')
    again = M.load_calendar_snapshot('2026-09-01', '2026-12-10', fixture_provider)
    assert again['calendar_id'] == calendar['calendar_id']
    assert again['available_at'] == original_time


def test_partial_calendar_can_resolve_actual_horizon_without_padding(calendar):
    # Archived data ends Dec10, although the scorer asks for convenience
    # padding through Dec31. The actual three-session premise is fully covered.
    cutoff = '2026-11-06T16:00:00+09:00'
    assert M.find_calendar_snapshot('2026-09-01', '2026-12-31', cutoff)['status'] == 'unknown'
    bounded = M.find_calendar_snapshot('2026-09-01', '2026-12-31', cutoff, allow_partial=True)
    assert bounded['status'] == 'complete'
    assert bounded['partial_requested_coverage'] is True
    assert bounded['coverage_end'] == '2026-12-10'
    assert bounded['calendar_id'] == calendar['calendar_id']
    application = {'application_id': 'partial-p', 'judgment_date': '2026-11-06', 'days': 3}
    assert M.resolve_window(application, bounded)['sessions'] == ['2026-11-10', '2026-11-11', '2026-11-12']
    assert M.resolve_window({**application, 'judgment_date': '2026-08-31'}, bounded)['status'] == 'unknown'
    assert M.resolve_window({**application, 'days': 100}, bounded)['status'] == 'unknown'
    assert M.find_calendar_snapshot('2026-09-01', '2026-12-31',
                                   '2026-11-05T16:00:00+09:00', allow_partial=True)['status'] == 'unknown'


def test_whole_window_next_session_confirmation_and_provider_finality(calendar, monkeypatch):
    first = collect(calendar)
    row = first['rows']['2026-11-06']
    assert row['status'] == 'provisional'
    assert row['ma20'] == 20000 and len(row['ma20_inputs']) == 20
    assert len(set(row['ma20_input_ids'])) == 20
    assert row['provider_finality'] == 'not_provided'
    # Repeating the same timestamp is idempotent and cannot self-confirm.
    assert collect(calendar)['rows']['2026-11-06']['status'] == 'provisional'
    assert len(S.records('daily_receipts')['records']) == 1
    clock(monkeypatch, '2026-11-08T16:00:00+09:00')
    assert collect(calendar)['rows']['2026-11-06']['status'] == 'provisional'
    clock(monkeypatch, '2026-11-09T16:00:00+09:00')
    assert collect(calendar)['rows']['2026-11-06']['status'] == 'provisional'
    clock(monkeypatch, '2026-11-10T08:30:00+09:00')
    row = collect(calendar)['rows']['2026-11-06']
    assert row['status'] == 'policy_complete'
    assert row['confirmation_status'] == 'matched'
    assert len(set(row['confirmation_receipt_ids'])) == 2
    assert row['provider_finality'] == 'not_provided'


def test_full_window_revision_even_with_same_target_close(calendar, monkeypatch):
    first = collect(calendar)
    original_id = first['observations'][0]['receipt_id']
    clock(monkeypatch, '2026-11-10T08:30:00+09:00')
    changed = provider(calendar, {'2026-11-05': 19000})
    row = collect(calendar, changed)['rows']['2026-11-06']
    assert row['close'] == 20000 and row['ma20'] == 19950
    assert row['status'] == 'provisional'
    assert row['supersedes_receipt_id'] == original_id
    clock(monkeypatch, '2026-11-11T08:30:00+09:00')
    assert collect(calendar, changed)['rows']['2026-11-06']['status'] == 'policy_complete'
    assert S.load('daily_receipts', original_id)['bars']['2026-11-05']['close'] == 20000


@pytest.mark.parametrize('missing', [0, None, 'missing', 'nan', 'inf', -100])
def test_missing_invalid_close_not_padding_or_zero(calendar, missing):
    row = collect(calendar, provider(calendar, {'2026-11-05': missing}))['rows']['2026-11-06']
    assert row['status'] == 'unknown'
    assert row['ma20'] is None
    assert row['missing_dates'] == ['2026-11-05']


def test_special_close_not_fixed_1530(calendar, monkeypatch):
    calls = []
    clock(monkeypatch, '2026-11-19T15:45:00+09:00')
    result = D.collect_daily_observations('005930', ['2026-11-19'], calendar,
                                         provider=lambda *args: calls.append(args))
    assert not calls and result['rows']['2026-11-19']['status'] == 'unknown'
    clock(monkeypatch, '2026-11-19T16:45:00+09:00')
    result = D.collect_daily_observations('005930', ['2026-11-19'], calendar, provider=provider(calendar))
    assert result['rows']['2026-11-19']['status'] == 'provisional'


def test_asof_replay_cannot_fetch_today_as_old_data(calendar, monkeypatch):
    collect(calendar)
    clock(monkeypatch, '2026-11-10T08:30:00+09:00')
    collect(calendar)
    def forbidden(*args):
        pytest.fail('Historical replay must not fetch new data')
    old = collect(calendar, forbidden, knowledge_as_of='2026-11-06T16:00:00+09:00')
    assert old['rows']['2026-11-06']['status'] == 'provisional'
    assert old['observations'] == []


def test_source_basis_and_duplicates(calendar, monkeypatch):
    collect(calendar)
    clock(monkeypatch, '2026-11-10T08:30:00+09:00')
    assert collect(calendar, provider(calendar, price_basis='raw_close'))['rows']['2026-11-06']['status'] == 'unknown'
    clock(monkeypatch, '2026-11-11T08:30:00+09:00')
    assert collect(calendar, provider(calendar, source='different'))['rows']['2026-11-06']['status'] == 'provisional'
    def conflicting(*args):
        body = provider(calendar)(*args)
        body['rows'] += [{'date': '2026-11-06', 'close': 19999}, {'date': '2026-11-06', 'close': 20000}]
        return body
    clock(monkeypatch, '2026-11-11T08:31:00+09:00')
    assert collect(calendar, conflicting)['rows']['2026-11-06']['status'] == 'unknown'


def test_latest_failure_does_not_reuse_old_complete(calendar, monkeypatch):
    collect(calendar)
    clock(monkeypatch, '2026-11-10T08:30:00+09:00')
    assert collect(calendar)['rows']['2026-11-06']['status'] == 'policy_complete'
    clock(monkeypatch, '2026-11-11T08:30:00+09:00')
    def failed(*args):
        raise RuntimeError('fixture provider failed')
    result = collect(calendar, failed)
    assert result['rows']['2026-11-06']['status'] == 'unknown'
    assert result['rows']['2026-11-06']['reason'] == 'provider_error'


def test_storage_failure_does_not_claim_completion(calendar, monkeypatch):
    def failed(*args):
        raise OSError('fixture full disk')
    monkeypatch.setattr(S, 'archive', failed)
    result = collect(calendar)
    assert result['recording_status'] == 'recording_error'
    assert result['rows']['2026-11-06']['status'] == 'unknown'


def test_provider_version_required_and_numeric_equivalence(calendar, monkeypatch):
    assert collect(calendar, provider(calendar, version=None))['rows']['2026-11-06']['reason'] == 'provider_version_unrecorded'
    clock(monkeypatch, '2026-11-06T16:01:00+09:00')
    collect(calendar)
    clock(monkeypatch, '2026-11-10T08:30:00+09:00')
    decimal_text = {d: '20000.000' for d in M.session_dates(calendar)}
    assert collect(calendar, provider(calendar, decimal_text))['rows']['2026-11-06']['status'] == 'policy_complete'


def test_live_read_records_actual_receipt_time(calendar, monkeypatch):
    start = '2026-11-06T16:00:00+09:00'
    received = '2026-11-06T16:00:02+09:00'
    def delayed(*args):
        clock(monkeypatch, received)
        return provider(calendar)(*args)
    result = collect(calendar, delayed, knowledge_as_of=start, collect_live=True)
    assert result['observations'][0]['available_at'] == received
    assert result['effective_knowledge_as_of'] == received
    # An explicit as-of, even equal to the clock, only projects archived data.
    def forbidden(*args):
        pytest.fail('Explicit as-of cannot collect without collect_live')
    assert collect(calendar, forbidden, knowledge_as_of=received)['observations'] == []


def test_pykrx_provider_explicit_equity_and_etf_contract(monkeypatch):
    from types import SimpleNamespace
    import pandas as pd
    calls = []
    frame = pd.DataFrame({'종가': [20400], '거래량': [0]}, index=pd.to_datetime(['2026-11-06']))
    def eq(*args, **kwargs):
        calls.append(('equity', args, kwargs))
        return frame
    def etf(*args, **kwargs):
        calls.append(('etf', args, kwargs))
        return frame
    monkeypatch.setitem(sys.modules, 'pykrx', SimpleNamespace(stock=SimpleNamespace(
        get_market_ohlcv_by_date=eq, get_etf_ohlcv_by_date=etf)))
    equity = D.pykrx_provider('005930', '2026-11-01', '2026-11-06', 'equity')
    fund = D.pykrx_provider('069500', '2026-11-01', '2026-11-06', 'etf')
    assert calls == [('equity', ('20261101', '20261106', '005930'), {'adjusted': True}),
                     ('etf', ('20261101', '20261106', '069500'), {})]
    assert equity['price_basis'] == 'adjusted_close'
    assert fund['price_basis'] == 'provider_published_close'
    assert equity['rows'][0]['close'] == '20400'
    assert equity['rows'][0]['volume'] == '0'
    assert equity['provider_finality'] == 'not_provided'


def test_same_code_different_kinds_never_share_confirmation(calendar, monkeypatch):
    collect(calendar)
    clock(monkeypatch, '2026-11-06T16:01:00+09:00')
    collect(calendar, instrument_kind='etf')
    clock(monkeypatch, '2026-11-10T08:30:00+09:00')
    etf = collect(calendar, instrument_kind='etf')
    assert etf['rows']['2026-11-06']['status'] == 'policy_complete'
    # The equity group must retain its own single provisional receipt even if
    # a later, policy-complete ETF history has the exact same code and prices.
    equity = collect(calendar, knowledge_as_of=C.now().isoformat())
    row = equity['rows']['2026-11-06']
    assert row['status'] == 'provisional'
    assert row['instrument_kind'] == 'equity'
    assert row['price_basis'] == 'adjusted_close'
    projected = D.confirm_daily_observations('005930', ['2026-11-06'], calendar,
        S.records('daily_receipts')['records'], C.now(), instrument_kind='equity')
    assert projected['2026-11-06']['status'] == 'provisional'


def test_unchanged_weekend_preserves_prior_confirmation_but_revision_does_not(calendar, monkeypatch):
    collect(calendar)
    clock(monkeypatch, '2026-11-10T08:30:00+09:00')
    confirmed = collect(calendar)['rows']['2026-11-06']
    assert confirmed['status'] == 'policy_complete'
    clock(monkeypatch, '2026-11-15T08:30:00+09:00')
    unchanged = collect(calendar)['rows']['2026-11-06']
    assert unchanged['status'] == 'policy_complete'
    assert unchanged['confirmation_receipt_ids'] == confirmed['confirmation_receipt_ids']
    assert unchanged['confirmed_at'] == confirmed['confirmed_at']
    assert unchanged['available_at'] == '2026-11-15T08:30:00+09:00'
    clock(monkeypatch, '2026-11-15T08:31:00+09:00')
    revised_provider = provider(calendar, {'2026-11-05': 19999})
    assert collect(calendar, revised_provider)['rows']['2026-11-06']['status'] == 'provisional'
    clock(monkeypatch, '2026-11-16T08:30:00+09:00')
    revised = collect(calendar, revised_provider)['rows']['2026-11-06']
    assert revised['status'] == 'policy_complete'
    assert revised['confirmation_receipt_ids'] != confirmed['confirmation_receipt_ids']
