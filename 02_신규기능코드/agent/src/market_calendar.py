"""Versioned KRX sessions for scoring; never replace an unknown calendar with weekdays."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import common as C
import scoring_store as S

KST = ZoneInfo('Asia/Seoul')
CALENDAR_VERSION = '4.13.2'
REVIEWED_THROUGH = '2026-12-31'


def _dates(start, end):
    current, finish = date.fromisoformat(start), date.fromisoformat(end)
    while current <= finish:
        yield current.isoformat()
        current += timedelta(days=1)


def register_reviewed_calendar(snapshot):
    """Explicit local review only. Every civil date is a session or a declared closure.

    Required: source/version/reviewer/coverage_start/coverage_end/sessions and
    closed_dates=[{date, reason}]. Optional supersedes_calendar_ids resolves a
    reviewed revision; available_at always comes from this process's clock.
    This records a review assertion, not an external verification of its truth.
    """
    if not isinstance(snapshot, dict):
        raise ValueError('Reviewed calendar must be an object')
    for key in ('source', 'version', 'reviewer', 'coverage_start', 'coverage_end'):
        if not isinstance(snapshot.get(key), str) or not snapshot[key].strip():
            raise ValueError(f'Reviewed calendar requires {key}')
    start, end = day(snapshot['coverage_start']), day(snapshot['coverage_end'])
    if start > end or (date.fromisoformat(end) - date.fromisoformat(start)).days > 36600:
        raise ValueError('Invalid or excessive calendar coverage')
    if not isinstance(snapshot.get('sessions'), list) or not isinstance(snapshot.get('closed_dates'), list):
        raise ValueError('Sessions and explicit closed_dates arrays required')
    rows, closures, seen = [], [], set()
    for row in snapshot['sessions']:
        d = day(row['date'])
        opening = instant(row['open_at'])
        closing = instant(row['close_at']) if row.get('close_at') else None
        if (d in seen or not start <= d <= end or opening.date().isoformat() != d
                or (closing is not None and (closing.date().isoformat() != d or closing <= opening))
                or (closing is None and not row.get('close_status'))):
            raise ValueError('Invalid reviewed session')
        seen.add(d)
        rows.append({**copy.deepcopy(row), 'date': d, 'open_at': opening.isoformat(),
                     'close_at': closing.isoformat() if closing else None})
    for row in snapshot['closed_dates']:
        d = day(row['date'])
        if d in seen or not start <= d <= end or not isinstance(row.get('reason'), str) or not row['reason'].strip():
            raise ValueError('Invalid or duplicate reviewed closure')
        seen.add(d)
        closures.append(copy.deepcopy(row))
    missing = sorted(set(_dates(start, end)) - seen)
    if missing:
        raise ValueError('Unclassified calendar dates: ' + ','.join(missing[:10]))
    supersedes = snapshot.get('supersedes_calendar_ids', [])
    if not isinstance(supersedes, list) or any(not isinstance(x, str) for x in supersedes):
        raise ValueError('supersedes_calendar_ids must be an array of IDs')
    now = instant(C.now()).isoformat()
    for identifier in supersedes:
        old = S.load('calendars', identifier)
        if not old or instant(old['available_at']) > instant(now):
            raise ValueError('Superseded calendar must already exist')
    body = {k: copy.deepcopy(snapshot[k]) for k in ('source', 'version', 'reviewer')}
    body.update(status='complete', coverage_start=start, coverage_end=end,
                sessions=sorted(rows, key=lambda x: x['date']), closed_dates=sorted(closures, key=lambda x: x['date']),
                review_status='explicit_local_review', supersedes_calendar_ids=sorted(set(supersedes)),
                limitations=['Locally submitted reviewed schedule; no external verification performed.'])
    if snapshot.get('source_published_at'):
        body['source_published_at'] = instant(snapshot['source_published_at']).isoformat()
    body['hash'] = S.digest(body)
    body['calendar_id'] = 'calendar-' + body['hash'][:32]
    existing = S.load('calendars', body['calendar_id'])
    body['available_at'] = existing['available_at'] if existing else now
    S.archive('calendars', body['calendar_id'], body)
    return {**body, 'recording_status': 'recorded'}


def import_reviewed_calendar(path):
    """Read an explicit local JSON file; never fetch a URL or infer sessions."""
    return register_reviewed_calendar(json.loads(Path(path).read_text(encoding='utf-8')))


def day(value):
    return date.fromisoformat(str(value)[:10]).isoformat()


def instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('An observation time must include its timezone')
    return result.astimezone(KST)


def _package_calendar(start, end):
    # This reads the installed calendar package, never a market-data endpoint.
    import exchange_calendars as xc
    if xc.__version__ != CALENDAR_VERSION:
        raise ValueError(f'Expected exchange-calendars {CALENDAR_VERSION}, got {xc.__version__}')
    if end > REVIEWED_THROUGH:
        raise ValueError('Calendar beyond the reviewed 2026 coverage requires a new source snapshot')
    if start < '2026-01-01' and end >= '2021-01-01':
        raise ValueError('2021–2025 special closing hours are not reviewed in this calendar snapshot')
    cal = xc.get_calendar('XKRX', start=start, end=end)
    rows = [{'date': str(d.date()),
                          'open_at': row['open'].tz_convert('Asia/Seoul').isoformat(),
                          'close_at': row['close'].tz_convert('Asia/Seoul').isoformat()}
                         for d, row in cal.schedule.loc[start:end].iterrows()]
    # Reviewed KRX 2026 amendments missing from the pinned package. Keep their
    # provenance in every snapshot, rather than editing installed package data.
    overrides = [{'date': d, 'kind': 'closed', 'source': 'https://strn.krx.co.kr/corebbs5/BHPSTRN0401/list'}
                 for d in ('2026-06-03', '2026-07-17', '2026-08-17')]
    rows = [r for r in rows if r['date'] not in {o['date'] for o in overrides}]
    for row in rows:
        if row['date'] == '2026-11-19':
            # The exam date is official; a KRX notice establishing the day's
            # closing hour was not verified. A customary 16:30 is not evidence.
            row.update(close_at=None, close_status='unconfirmed_special_session',
                       source='https://www.moe.go.kr/boardCnts/viewRenew.do?boardID=294&boardSeq=100526&lev=0&m=020402&opType=N&s=moe&statusYN=W')
    return {'sessions': rows,
            'source': 'exchange_calendars:XKRX', 'version': xc.__version__,
            'coverage_start': start, 'coverage_end': end,
            'override_version': 'krx-2026-reviewed-2026-11-07@v1', 'overrides': overrides,
            'limitations': ['Package calendar; exchange amendments require a new versioned snapshot.']}


def load_calendar_snapshot(start, end, provider=None):
    """provider(start,end) supplies a complete, explicitly bounded session schedule."""
    body = {'status': 'unknown', 'sessions': [], 'source': 'unavailable', 'version': None}
    try:
        start, end = day(start), day(end)
        if start > end:
            raise ValueError('Calendar start is after end')
        body.update(coverage_start=start, coverage_end=end)
        supplied = (provider or _package_calendar)(start, end)
        if not isinstance(supplied, dict) or supplied.get('status', 'complete') != 'complete':
            raise ValueError('Calendar provider did not declare a complete schedule')
        if not supplied.get('source') or not supplied.get('version'):
            raise ValueError('Calendar source/version missing')
        if day(supplied.get('coverage_start', start)) > start or day(supplied.get('coverage_end', end)) < end:
            raise ValueError('Calendar coverage is insufficient')
        rows, seen = [], set()
        for row in supplied['sessions']:
            d = day(row['date'])
            opening = instant(row['open_at'])
            closing = instant(row['close_at']) if row.get('close_at') else None
            if (d in seen or opening.date().isoformat() != d or
                    (closing is not None and (closing.date().isoformat() != d or closing <= opening)) or
                    (closing is None and not row.get('close_status'))):
                raise ValueError('Duplicate or invalid calendar session')
            seen.add(d)
            if start <= d <= end:
                rows.append({**copy.deepcopy(row), 'date': d, 'open_at': opening.isoformat(),
                             'close_at': closing.isoformat() if closing else None})
        body.update(copy.deepcopy(supplied), sessions=sorted(rows, key=lambda x: x['date']),
                    coverage_start=start, coverage_end=end, status='complete')
    except Exception as exc:
        body.update(status='unknown', reason='calendar_unknown', detail=str(exc), sessions=[])
    body['hash'] = S.digest(body)
    body['calendar_id'] = 'calendar-' + body['hash'][:32]
    try:
        existing = S.load('calendars', body['calendar_id'])
        # The first time this exact content was available is immutable. Loading
        # it again must neither manufacture earlier knowledge nor hide its age.
        body['available_at'] = existing['available_at'] if existing else instant(C.now()).isoformat()
        S.archive('calendars', body['calendar_id'], body)
    except Exception as exc:
        return {**body, 'status': 'unknown', 'reason': 'calendar_recording_error',
                'recording_status': 'recording_error', 'detail': str(exc)}
    return {**body, 'recording_status': 'recorded'}


def find_calendar_snapshot(start, end, knowledge_as_of, *, allow_partial=False):
    """Read-only historical lookup; today's calendar cannot become past knowledge."""
    start, end, cutoff = day(start), day(end), instant(knowledge_as_of)
    if start > end or (date.fromisoformat(end) - date.fromisoformat(start)).days > 36600:
        raise ValueError('Invalid or excessive requested calendar coverage')
    saved = S.records('calendars')
    unknown = {'calendar_id': None, 'status': 'unknown', 'sessions': [],
               'coverage_start': start, 'coverage_end': end, 'reason': 'calendar_unavailable_as_of',
               'recording_status': 'not_recorded', 'errors': saved['errors']}
    if saved['errors']:
        return {**unknown, 'reason': 'calendar_recording_error', 'recording_status': 'recording_error'}
    eligible = []
    for row in saved['records']:
        if row.get('status') != 'complete' or not row.get('available_at'):
            continue
        try:
            if (instant(row['available_at']) <= cutoff and row['coverage_start'] <= end
                    and row['coverage_end'] >= start):
                eligible.append(row)
        except (KeyError, ValueError, TypeError):
            return {**unknown, 'reason': 'invalid_calendar_history', 'recording_status': 'recording_error'}
    if not eligible:
        return unknown
    maps = {r['calendar_id']: {x['date']: x for x in r['sessions']} for r in eligible}
    by_id = {r['calendar_id']: r for r in eligible}
    # Supersession applies only where both snapshots actually cover the date.
    # An amendment with narrow bounds cannot erase the rest of a prior year.
    def replaces(row, identifier, seen=None):
        seen = set() if seen is None else seen
        if row['calendar_id'] in seen:
            return False
        seen.add(row['calendar_id'])
        parents = row.get('supersedes_calendar_ids', [])
        return identifier in parents or any(replaces(by_id[p], identifier, seen) for p in parents if p in by_id)
    selected, conflicts = {}, []
    for d in _dates(max(start, min(r['coverage_start'] for r in eligible)),
                    min(end, max(r['coverage_end'] for r in eligible))):
        choices = [r for r in eligible if r['coverage_start'] <= d <= r['coverage_end']]
        reviewed = [r for r in choices if r.get('review_status') == 'explicit_local_review']
        choices = reviewed or choices
        choices = [r for r in choices if not any(replaces(other, r['calendar_id']) for other in choices if other is not r)]
        if not choices:
            continue
        def meaning(r):
            session = maps[r['calendar_id']].get(d)
            return (session.get('open_at'), session.get('close_at'), session.get('close_status') if not session.get('close_at') else None) if session else ('closed',)
        if len({meaning(r) for r in choices}) > 1:
            conflicts.append({'date': d, 'calendar_ids': sorted(r['calendar_id'] for r in choices)})
            continue
        selected[d] = max(choices, key=lambda r: (instant(r['available_at']), r['calendar_id']))
    if conflicts:
        return {**unknown, 'reason': 'calendar_conflict', 'conflicts': conflicts,
                'recording_status': 'recorded'}
    dates = sorted(selected)
    complete = bool(dates) and dates[0] == start and dates[-1] == end and len(dates) == (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if not complete and not allow_partial:
        return {**unknown, 'reason': 'calendar_partial_coverage', 'available_ranges': [
            {'start': r['coverage_start'], 'end': r['coverage_end'], 'calendar_id': r['calendar_id']} for r in eligible]}
    if not dates:
        return unknown
    groups = []
    for d in dates:
        if not groups or date.fromisoformat(d) - date.fromisoformat(groups[-1][-1]) != timedelta(days=1):
            groups.append([])
        groups[-1].append(d)
    # Never bridge an unreviewed gap as if those dates were holidays.
    dates = max(groups, key=lambda group: (sum(d in maps[selected[d]['calendar_id']] for d in group), len(group), group[-1]))
    source_ids = sorted({selected[d]['calendar_id'] for d in dates})
    if len(source_ids) == 1:
        result = copy.deepcopy(by_id[source_ids[0]])
    else:
        result = {'status': 'complete', 'source': 'stored_reviewed_calendar_composition', 'version': 'composition@v1',
                  'coverage_start': dates[0], 'coverage_end': dates[-1],
                  'sessions': [copy.deepcopy(maps[selected[d]['calendar_id']][d]) for d in dates if d in maps[selected[d]['calendar_id']]],
                  'component_calendar_ids': source_ids,
                  'selection_policy': 'explicit_review_then_explicit_supersession_conflicts_unknown@v1',
                  'available_at': max((by_id[x]['available_at'] for x in source_ids), key=instant)}
        result['hash'] = S.digest(result)
        result['calendar_id'] = 'calendar-composed-' + result['hash'][:32]
    return {**result, 'recording_status': 'recorded', 'partial_requested_coverage': not complete,
            'requested_coverage_start': start, 'requested_coverage_end': end}


def select_calendar_snapshot(start, end, knowledge_as_of, *, collect_default=False):
    """Prefer archived reviewed coverage, optionally add only the reviewed package range."""
    cutoff = instant(C.now() if knowledge_as_of is None else knowledge_as_of)
    found = find_calendar_snapshot(start, end, cutoff, allow_partial=True)
    if found.get('reason') in ('calendar_conflict', 'calendar_recording_error', 'invalid_calendar_history'):
        return found
    if found.get('status') == 'complete':
        return found
    if not collect_default:
        return found
    if knowledge_as_of is not None and cutoff < instant(C.now()):
        return found  # Freshly imported package data is not historical knowledge.
    # This fallback is intentionally still bounded by the package's reviewed
    # range. The requested range itself is never clipped or relabelled complete.
    bounded_start, bounded_end = max(day(start), '2026-01-01'), min(day(end), REVIEWED_THROUGH)
    if bounded_start > bounded_end:
        return found
    default = load_calendar_snapshot(bounded_start, bounded_end)
    if default.get('status') != 'complete':
        return default
    if knowledge_as_of is not None and instant(default['available_at']) > cutoff:
        return found
    return {**default, 'partial_requested_coverage': bounded_start != day(start) or bounded_end != day(end),
            'requested_coverage_start': day(start), 'requested_coverage_end': day(end)}


def session_dates(calendar):
    if calendar.get('status') != 'complete':
        return []
    return [x['date'] for x in calendar.get('sessions', [])]


def session_close(calendar, trading_day):
    if calendar.get('status') != 'complete':
        return None
    return next((x['close_at'] for x in calendar.get('sessions', []) if x['date'] == day(trading_day)), None)


def resolve_window(application, calendar):
    """Resolve N sessions after the original judgment date without editing the premise."""
    body = {'application_id': application.get('application_id'), 'calendar_id': calendar.get('calendar_id'),
            'component_calendar_ids': calendar.get('component_calendar_ids', [calendar.get('calendar_id')]),
            'status': 'unknown', 'sessions': [], 'start_trading_day': None, 'end_trading_day': None}
    try:
        n = application.get('days')
        if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
            raise ValueError('Explicit positive trading-day horizon required')
        original = day(application['judgment_date'])
        if calendar.get('status') != 'complete' or calendar['coverage_start'] > original:
            raise ValueError('Calendar does not cover the original judgment date')
        selected = [d for d in session_dates(calendar) if d > original][:n]
        if len(selected) != n:
            raise ValueError('Calendar does not cover the full premise horizon')
        body.update(status='resolved', judgment_date=original, days=n, sessions=selected,
                    start_trading_day=selected[0], end_trading_day=selected[-1],
                    session_closes={d: session_close(calendar, d) for d in selected})
    except Exception as exc:
        body.update(reason='calendar_unknown', detail=str(exc))
    body['window_id'] = 'window-' + S.digest(body)[:32]
    try:
        S.archive('windows', body['window_id'], body)
    except Exception as exc:
        return {**body, 'status': 'unknown', 'reason': 'window_recording_error',
                'recording_status': 'recording_error', 'detail': str(exc)}
    return {**body, 'recording_status': 'recorded'}
