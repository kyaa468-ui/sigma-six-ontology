"""Contemporaneous inputs for fixed-judgment portfolio replay.

This is telemetry only: it does not request prices, schedule extra checks, or
change a trading result. Unknown provider times/capacity remain explicitly
unknown. A coverage session is a predeclared schedule, never a retroactive
assertion that an unobserved interval was monitored.
"""
from __future__ import annotations

import copy
import math
from datetime import timedelta
from pathlib import Path

import common as C
import storage_objects as objects
import log_store
import execution_trace as E
from replay_context import instant

VERSION = 'PORTFOLIO_CAPTURE@v1'
KINDS = {'portfolio_state', 'portfolio_market', 'portfolio_session',
         'portfolio_monitor', 'portfolio_session_end'}


def _record(kind, **fields):
    try:
        return E.record(kind, capture_version=VERSION, account_scope=E.account_scope(), **fields)
    except Exception as exc:
        return {'recording_status': 'recording_error', 'error': str(exc)}


def account_metadata(snapshot, source):
    """A successful response alone does not prove pagination/field completeness."""
    supplied = snapshot.get('capture_metadata') or {}
    holdings = snapshot.get('holdings')
    totals_valid = all(type(snapshot.get(k)) in (int, float) and math.isfinite(snapshot[k])
                       and snapshot[k] >= 0 for k in ('total', 'stock_eval'))
    values_valid = isinstance(holdings, list) and all(isinstance(h, dict) and h.get('code')
        and type(h.get('qty')) is int and h['qty'] > 0 for h in holdings)
    values_valid = values_valid and len({h['code'] for h in holdings}) == len(holdings)
    complete = (supplied.get('pagination_complete') is True
                and supplied.get('required_fields_present') is True
                and values_valid and totals_valid)
    return {'complete': complete, 'captured_at': C.now().isoformat(),
            'capture_metadata': {**copy.deepcopy(supplied), 'source': supplied.get('source') or source,
                'received_at': C.now().isoformat(), 'completeness_status': 'complete' if complete else 'unknown'}}


def capture_state(account, *, purpose='order_plan'):
    """Read the original local state once at this call, even for an empty account."""
    try:
        positions = C.read_json(C.POSITIONS)
        journal_path = Path(C.JOURNAL)
        journal = log_store.capture(log_store.view(C.JOURNAL)) if journal_path.is_file() else None
        # A confirmed empty account needs no held-position metadata. Preserve
        # the observed file absence and this derivation instead of inventing it.
        positions_status = 'observed' if isinstance(positions, dict) else 'unrecorded'
        if positions is None and account.get('complete') is True and account.get('holdings') == []:
            positions, positions_status = {}, 'not_applicable_confirmed_empty_account'
        body = {'purpose': purpose, 'captured_at': C.now().isoformat(),
                'account': objects.freeze(account, kind='account'), 'positions': positions, 'journal': journal,
                'positions_status': positions_status,
                'journal_status': 'observed' if isinstance(journal, list) else 'unrecorded'}
        record = _record('portfolio_state', **body)
        return {**body, 'event_id': record.get('event_id'), 'account_scope': E.account_scope(),
                'recording_status': record.get('recording_status', 'recording_error')}
    except Exception as exc:
        return {'recording_status': 'recording_error', 'error': str(exc)}


def record_quote(code, price, *, source, provider_observed_at=None, price_basis=None,
                 executable_qty=None, capacity_source=None, provenance='provider_response', received_at=None):
    """Record what this process received; receipt time is not provider time.

    Capacity must be explicitly supplied with its source. Market volume is not
    executable capacity and is never converted into it here.
    """
    received = received_at or C.now().isoformat()
    reason = None
    try:
        if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
            reason = 'invalid_market_price'
        if instant(received) > C.now():
            reason = 'receipt_time_after_capture'
        if provider_observed_at is not None and instant(provider_observed_at) > instant(received):
            reason = 'provider_time_after_receipt'
        if executable_qty is not None and (type(executable_qty) is not int or executable_qty < 0 or not capacity_source):
            reason = 'capacity_without_valid_quantity_and_source'
    except (ValueError, TypeError):
        reason = 'invalid_provider_time'
    row = {'code': code, 'price': price, 'source': source, 'price_basis': price_basis,
           'observed_at': provider_observed_at or received, 'available_at': received,
           'provider_observed_at': provider_observed_at, 'received_at': received,
           'time_basis': 'provider' if provider_observed_at else 'received_at',
           'executable_qty': executable_qty if capacity_source else None,
           'capacity_source': capacity_source, 'provenance': provenance,
           'status': 'invalid' if reason else 'observed', 'reason': reason}
    record = _record('portfolio_market', observation=row)
    return {**row, 'observation_id': record.get('event_id'),
            'capture_status': record.get('recording_status', 'recording_error')}


def begin_session(start, end, *, interval_seconds=60, valuation_times=None, codes=None,
                  source='monitor.loop', max_lateness_seconds=5, cadence='fixed_slots',
                  valuation_policy='explicit_times'):
    now = C.now()
    start, end = instant(start) if start is not None else now, instant(end)
    if start < now or end <= start:
        raise ValueError('CAPTURE_SCHEDULE_MUST_BE_DECLARED_BEFORE_START')
    if type(interval_seconds) is not int or interval_seconds <= 0:
        raise ValueError('INVALID_CAPTURE_INTERVAL')
    if type(max_lateness_seconds) not in (int, float) or not 0 <= max_lateness_seconds < interval_seconds:
        raise ValueError('INVALID_CAPTURE_TOLERANCE')
    if cadence not in ('fixed_slots','sleep_after_check') or valuation_policy not in ('explicit_times','registered_window_boundaries_and_observed_checks'):
        raise ValueError('UNSUPPORTED_CAPTURE_SCHEDULE_POLICY')
    values = valuation_times or [start.isoformat(), end.isoformat()]
    values = [instant(value).isoformat() for value in values]
    if values != sorted(set(values)) or instant(values[0]) != start or instant(values[-1]) != end:
        raise ValueError('CAPTURE_VALUATIONS_MUST_COVER_WINDOW')
    if any(not start <= instant(value) <= end for value in values):
        raise ValueError('CAPTURE_VALUATION_OUTSIDE_WINDOW')
    record = _record('portfolio_session', start=start.isoformat(), end=end.isoformat(), declared_at=now.isoformat(),
        interval_seconds=interval_seconds, max_lateness_seconds=max_lateness_seconds,
        valuation_times=values, valuation_policy=valuation_policy, cadence=cadence,
        codes=sorted(set(codes or ['*'])), source=source,
        rule_exit_contract='frozen_explicit_exit_decisions')
    return {**record, 'session_id': record.get('event_id')}


def _monitor_payload(event):
    reads = (event.get('evaluation') or {}).get('actual_reads')
    branch_reads = event.get('counterfactual_reads') or []
    snapshots = {Path(row['path']).name: copy.deepcopy(row['value']) for row in [*branch_reads, *(reads or [])]
                 if row.get('path') and 'value' in row and row.get('status') != 'recording_error'}
    return {'at': event.get('at') or event.get('created_at'),
             'params': copy.deepcopy(event.get('params') or (event.get('evaluation') or {}).get('params')),
             'module_state': copy.deepcopy(event.get('module_state')),
             'check_id': event.get('event_id'), 'source_event_id': event.get('event_id'),
             'actual_reads': copy.deepcopy(reads), 'counterfactual_reads': copy.deepcopy(branch_reads), 'snapshots': snapshots,
             'process_returncodes': {c: event.get('returncode') for c in event.get('due', {})},
             'exit_decision_ids': {}}


def capture_monitor_inputs():
    """Freeze a possible changed-arm branch without claiming the engine read it.

    With no original holding, the legacy monitor does not open the briefing.
    A replay arm can hold a stock then. Record that local file at this check,
    separately from actual_reads, so later replay never fills it from today.
    """
    try:
        return [{'path': C.BRIEFING, 'value': C.load_briefing(),
                 'read_at': C.now().isoformat(), 'status': 'observed_for_counterfactual',
                 'usage': 'counterfactual_monitor_branch'}]
    except Exception:
        return [{'path': C.BRIEFING, 'status': 'recording_error'}]


def record_monitor_check(event):
    """Record actual checks after legacy decisions. No synthetic successful tick."""
    status = event.get('status')
    good = status in ('executed', 'skipped') and event.get('reason') != 'missing_recent_holdings'
    check = _monitor_payload(event)
    return _record('portfolio_monitor', check=check,
                   status='observed' if good and isinstance(check['params'], dict) else 'incomplete',
                   source_event_id=event.get('event_id'), source_sha256=E._hash({k:v for k,v in event.items() if k!='recording_status'}),
                   reason=event.get('reason') or (None if good else status))


def finish_session(session_id):
    # Completion is not asserted here; the reader proves each declared slot
    # from the immutable checks, which may be missing or failed.
    return _record('portfolio_session_end', session_id=session_id, ended_at=C.now().isoformat())


def coverage(records, start, end, scope, codes):
    """Compute coverage from original schedule/checks at the reader's cutoff."""
    start, end = instant(start), instant(end)
    sessions = [r for r in records if r.get('kind') == 'portfolio_session'
                and r.get('account_scope') == scope and instant(r.get('declared_at', r['created_at'])) <= start
                and instant(r['start']) <= start and end <= instant(r['end'])
                and ('*' in r.get('codes', []) or set(codes) <= set(r.get('codes', [])))]
    if not sessions:
        return {'complete': False, 'gaps': [{'reason': 'predeclared_monitor_schedule_missing'}], 'checks': [], 'valuation_times': []}
    # Never select whichever competing schedule produces the best coverage.
    sessions.sort(key=lambda x: (x['created_at'], x['event_id']))
    session = sessions[0]
    checks = [r for r in records if r.get('kind') == 'portfolio_monitor' and r.get('account_scope') == scope
              and start <= instant((r.get('check') or {})['at']) <= end]
    originals={r['event_id']:{k:v for k,v in r.items() if k not in ('opportunity_ids','recording_status','_source')}
               for r in records if r.get('kind')=='monitor_check' and r.get('account_scope')==scope}
    gaps = []
    selected = []
    def accept(check):
        if check.get('status') != 'observed':
            gaps.append({'reason': 'scheduled_monitor_check_incomplete', 'event_id': check['event_id']})
        else:
            original=originals.get(check.get('source_event_id'))
            if (not original or check.get('source_sha256')!=E._hash(original)
                    or _monitor_payload(original)!=check['check']):
                gaps.append({'reason':'monitor_source_missing_or_mismatched','event_id':check['event_id']})
            else:
                selected.append(copy.deepcopy(check['check']))
    if session.get('cadence','fixed_slots')=='sleep_after_check':
        ordered=sorted(checks,key=lambda x:(x['check']['at'],x['event_id']))
        # The legacy loop sleeps after a check finishes. Do not force its
        # accumulating execution duration onto a wall-clock minute grid.
        maximum=timedelta(seconds=session['interval_seconds']+session['max_lateness_seconds'])
        if not ordered or instant(ordered[0]['check']['at'])-start>maximum:
            gaps.append({'reason':'monitor_window_start_uncovered'})
        if not ordered or end-instant(ordered[-1]['check']['at'])>maximum:
            gaps.append({'reason':'monitor_window_end_uncovered'})
        for previous,current in zip(ordered,ordered[1:]):
            if instant(current['check']['at'])-instant(previous['check']['at'])>maximum:
                gaps.append({'reason':'monitor_gap_exceeds_declared_interval','after':previous['check']['at'],
                             'before':current['check']['at']})
        for check in ordered:accept(check)
    else:
        slots=[];slot=instant(session['start'])
        while slot<=end:
            if slot>=start:slots.append(slot)
            slot+=timedelta(seconds=session['interval_seconds'])
        if not slots or slots[0]!=start or slots[-1]!=end:
            gaps.append({'reason':'registered_period_not_aligned_to_capture_schedule'})
        for slot in slots:
            own=sorted((r for r in checks if slot<=instant(r['check']['at'])<=slot+timedelta(seconds=session['max_lateness_seconds'])),
                       key=lambda x:(x['check']['at'],x['event_id']))
            if not own:gaps.append({'reason':'scheduled_monitor_check_missing','scheduled_at':slot.isoformat()})
            else:accept(own[0])
    if session.get('valuation_policy')=='registered_window_boundaries_and_observed_checks':
        valuations=sorted({start.isoformat(),end.isoformat(),*[x['at'] for x in selected]})
    else:
        valuations = [x for x in session['valuation_times'] if start <= instant(x) <= end]
    if not valuations or instant(valuations[0]) != start or instant(valuations[-1]) != end:
        gaps.append({'reason': 'valuation_schedule_does_not_cover_registered_window'})
    ends = [r for r in records if r.get('kind') == 'portfolio_session_end' and r.get('account_scope') == scope
            and r.get('session_id') == session['event_id'] and instant(r['ended_at']) >= end]
    if not ends:
        gaps.append({'reason': 'capture_session_unfinished'})
    return {'complete': not gaps, 'gaps': gaps, 'checks': selected, 'valuation_times': valuations,
            'session_id': session['event_id'], 'source_ids': [session['event_id'], *[x['event_id'] for x in checks], *[x['event_id'] for x in ends]],
            'rule_exit_contract': session['rule_exit_contract']}
