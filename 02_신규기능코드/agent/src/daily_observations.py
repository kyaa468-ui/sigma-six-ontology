"""Immutable daily receipts and full-window confirmation for premise scoring.

This module does not reuse rounded candidate MA20 values. A historical query only
reads receipts already available at that time; it cannot manufacture old evidence
by downloading today's corrected history.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from importlib.metadata import version

import common as C
import market_calendar as MC
import scoring_store as S

COMPLETION_POLICY = 'matching_full_20_close_window_on_later_trading_session'
CALCULATION_VERSION = 'session_aligned_sma20_decimal@v1'
PRICE_BASIS = {'equity': 'adjusted_close', 'etf': 'provider_published_close'}


def pykrx_provider(code, start, end, instrument_kind):
    """Explicit provider and adjustment semantics; imports do not fetch prices."""
    from pykrx import stock
    args = (start.replace('-', ''), end.replace('-', ''), code)
    if instrument_kind == 'etf':
        frame = stock.get_etf_ohlcv_by_date(*args)
        source, options = 'pykrx:krx_etf_ohlcv', {'freq': 'd'}
    else:
        frame = stock.get_market_ohlcv_by_date(*args, adjusted=True)
        source, options = 'pykrx:naver_ohlcv', {'freq': 'd', 'adjusted': True}
    rows = [] if frame is None else [{'date': str(idx.date()),
                                      'close': None if row.get('종가') is None else str(row.get('종가')),
                                      'volume': None if row.get('거래량') is None else str(row.get('거래량'))}
                                     for idx, row in frame.iterrows()]
    return {'rows': rows, 'source': source, 'version': version('pykrx'),
            'price_basis': PRICE_BASIS[instrument_kind], 'provider_finality': 'not_provided',
            'request_parameters': {'start': start, 'end': end, 'code': code, **options}}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _window(trading_day, calendar):
    dates = MC.session_dates(calendar)
    if trading_day not in dates:
        return []
    offset = dates.index(trading_day)
    return dates[offset - 19:offset + 1] if offset >= 19 else []


def _normalize(payload):
    result, raw, errors = {}, [], []
    for row in payload.get('rows', []):
        # Preserve invalid raw fields without introducing NaN JSON values.
        item = {str(k): (None if v is None else str(v)) for k, v in row.items()}
        raw.append(item)
        try:
            d = MC.day(row['date'])
            close = _number(row.get('close'))
            normalized = {'date': d, 'close': float(close) if close is not None and close > 0 else None,
                          'close_exact': str(close) if close is not None and close > 0 else None,
                          'status': 'observed' if close is not None and close > 0 else 'unknown',
                          'reason': None if close is not None and close > 0 else 'missing_or_invalid_close'}
            if d in result and result[d] != normalized:
                normalized.update(close=None, close_exact=None, status='unknown', reason='conflicting_rows')
            # Once conflicting, another duplicate must not erase the conflict.
            if result.get(d, {}).get('reason') != 'conflicting_rows':
                result[d] = normalized
        except Exception as exc:
            errors.append({'reason': 'invalid_provider_row', 'detail': str(exc), 'raw': item})
    return result, raw, errors


def calculate_ma20(trading_day, receipt, calendar):
    """Exactly the 20 scheduled closes ending on trading_day, with no fill/padding."""
    result = {'date': trading_day, 'status': 'unknown', 'close': None, 'ma20': None,
              'instrument_kind': receipt.get('instrument_kind'), 'price_basis': receipt.get('price_basis'),
              'source': receipt.get('source'), 'source_version': receipt.get('source_version'),
              'provider_finality': receipt.get('provider_finality', 'not_provided'),
              'available_at': receipt.get('available_at'), 'observed_at': trading_day,
              'receipt_id': receipt.get('receipt_id'), 'calendar_id': calendar.get('calendar_id'),
              'observation_ids': [], 'ma20_input_ids': [], 'ma20_inputs': [],
              'completion_policy': COMPLETION_POLICY, 'calculation_version': CALCULATION_VERSION,
              'confirmation_status': 'unconfirmed'}
    dates = _window(trading_day, calendar)
    if len(dates) != 20:
        return {**result, 'reason': 'insufficient_calendar_window'}
    if receipt.get('status') != 'observed':
        return {**result, 'reason': receipt.get('reason', 'provider_unavailable')}
    if receipt.get('price_basis') != PRICE_BASIS.get(receipt.get('instrument_kind')) or not receipt.get('source'):
        return {**result, 'reason': 'price_basis_unknown_or_mismatched'}
    if not receipt.get('source_version'):
        return {**result, 'reason': 'provider_version_unrecorded'}
    closing = MC.session_close(calendar, trading_day)
    if closing is None:
        return {**result, 'reason': 'session_close_unknown'}
    if MC.instant(receipt['available_at']) < MC.instant(closing):
        return {**result, 'reason': 'session_not_closed'}
    bars = receipt.get('bars', {})
    missing = [d for d in dates if bars.get(d, {}).get('status') != 'observed' or _number(bars[d].get('close_exact')) is None]
    if missing:
        return {**result, 'reason': 'incomplete_20_close_window', 'missing_dates': missing}
    values = [Decimal(bars[d]['close_exact']) for d in dates]
    inputs = [{'date': d, 'close': float(v), 'close_exact': str(v),
               'observation_id': f"{receipt['receipt_id']}:{d}"} for d, v in zip(dates, values)]
    ids = [v['observation_id'] for v in inputs]
    average = sum(values) / Decimal(20)
    return {**result, 'status': 'provisional', 'reason': 'awaiting_later_session_reread',
            'close': float(values[-1]), 'close_exact': str(values[-1]), 'ma20': float(average), 'ma20_exact': str(average),
            'ma20_inputs': inputs, 'ma20_input_ids': ids, 'observation_ids': ids,
            'input_hash': S.digest({'source': receipt['source'], 'source_version': receipt.get('source_version'),
                                    'price_basis': receipt['price_basis'],
                                    'inputs': list(zip(dates, (format(v.normalize(), 'f') for v in values)))})}


def confirm_daily_observations(code, dates, calendar, receipts, knowledge_as_of, instrument_kind=None):
    """Pure as-of projection over archived receipts; never performs a reread itself."""
    cutoff = MC.instant(knowledge_as_of)
    if calendar.get('available_at') and MC.instant(calendar['available_at']) > cutoff:
        return {target: {'date': target, 'status': 'unknown', 'reason': 'calendar_unavailable_as_of',
                         'close': None, 'ma20': None, 'observation_ids': [], 'ma20_input_ids': []}
                for target in sorted(set(dates))}
    ordered = sorted([r for r in receipts if r.get('code') == code
                      and (instrument_kind is None or r.get('instrument_kind') == instrument_kind)
                      and MC.instant(r['available_at']) <= cutoff],
                     key=lambda r: (r['available_at'], r['receipt_id']))
    output = {}
    sessions = MC.session_dates(calendar)
    for target in sorted(set(dates)):
        applicable = [r for r in ordered if target in r.get('requested_dates', [])]
        if not applicable:
            output[target] = {'date': target, 'status': 'unknown', 'reason': 'no_available_observation',
                              'close': None, 'ma20': None, 'observation_ids': [], 'ma20_input_ids': []}
            continue
        current = applicable[-1]
        row = calculate_ma20(target, current, calendar)
        simultaneous = [calculate_ma20(target, r, calendar) for r in applicable if r['available_at'] == current['available_at']]
        if len({S.digest({'status': r['status'], 'input_hash': r.get('input_hash'), 'reason': r.get('reason')})
                for r in simultaneous}) > 1:
            output[target] = {**row, 'status': 'unknown', 'reason': 'conflicting_same_time_receipts'}
            continue
        if row['status'] == 'provisional':
            # Walking backward through identical same-session receipts is safe;
            # any conflicting/missing receipt breaks stability instead of being skipped.
            matching = [current]
            for prior in reversed(applicable[:-1]):
                previous = calculate_ma20(target, prior, calendar)
                if previous.get('input_hash') != row.get('input_hash'):
                    row['reason'] = 'revised_or_unavailable_previous_window'
                    row['supersedes_receipt_id'] = prior['receipt_id']
                    break
                matching.append(prior)
            # A weekend reread cannot create confirmation. It can retain an
            # earlier confirmed pair only while every intervening receipt still
            # agrees on the entire input window (including source and basis).
            for later in matching:
                later_day = MC.instant(later['available_at']).date().isoformat()
                earlier = next((r for r in reversed(matching)
                                if MC.instant(r['available_at']).date().isoformat() < later_day), None)
                if later_day in sessions and target < later_day and earlier:
                    row.update(status='policy_complete', reason=None, confirmation_status='matched',
                               confirmed_at=later['available_at'],
                               confirmation_receipt_ids=[earlier['receipt_id'], later['receipt_id']])
                    break
        output[target] = row
    return output


def collect_daily_observations(code, dates, calendar, provider=None, knowledge_as_of=None, instrument_kind='equity', *, collect_live=False):
    """Read once per code, archive the response, then project the requested cutoff.

    A caller replaying an old cutoff gets only existing receipts. Tests control
    C.now() explicitly; passing an old cutoff never timestamps a new fetch as old.
    """
    now = MC.instant(C.now())
    cutoff = MC.instant(knowledge_as_of or now)
    dates = sorted({MC.day(d) for d in dates})
    saved = S.records('daily_receipts')
    errors = list(saved['errors'])
    receipts = saved['records']
    eligible = [d for d in dates if MC.session_close(calendar, d) and MC.instant(MC.session_close(calendar, d)) <= now]
    result = {'observations': [], 'errors': errors, 'recording_status': 'recorded'}
    if instrument_kind not in PRICE_BASIS:
        errors.append({'reason': 'unsupported_instrument_kind', 'instrument_kind': instrument_kind})
        eligible = []
    if (collect_live or knowledge_as_of is None) and eligible and calendar.get('status') == 'complete':
        windows = [_window(d, calendar) for d in eligible]
        all_dates = sorted({d for w in windows for d in w})
        receipt = {'code': code, 'instrument_kind': instrument_kind, 'requested_dates': eligible,
                   'calendar_id': calendar.get('calendar_id'), 'available_at': now.isoformat(),
                   'status': 'unknown', 'source': None, 'source_version': None,
                   'price_basis': None, 'provider_finality': 'not_provided', 'bars': {}, 'raw_rows': []}
        if all_dates:
            try:
                payload = (provider or pykrx_provider)(code, all_dates[0], all_dates[-1], instrument_kind)
                bars, raw, issues = _normalize(payload)
                receipt.update(status='observed', source=payload.get('source'), source_version=payload.get('version'),
                               price_basis=payload.get('price_basis'), provider_finality=payload.get('provider_finality', 'not_provided'),
                               request_parameters=copy.deepcopy(payload.get('request_parameters', {})),
                               bars=bars, raw_rows=raw, errors=issues)
                errors.extend(issues)
            except Exception as exc:
                receipt.update(reason='provider_error', detail=str(exc))
                errors.append({'reason': 'provider_error', 'detail': str(exc)})
        else:
            receipt['reason'] = 'insufficient_calendar_window'
        # Availability is response receipt time, never request start or the
        # historical cutoff a caller wants to inspect.
        receipt['available_at'] = MC.instant(C.now()).isoformat()
        if collect_live or knowledge_as_of is None:
            cutoff = MC.instant(receipt['available_at'])
        receipt['receipt_id'] = 'daily-' + S.digest(receipt)[:32]
        try:
            S.archive('daily_receipts', receipt['receipt_id'], receipt)
            if not any(x.get('receipt_id') == receipt['receipt_id'] for x in receipts):
                receipts.append(receipt)
            result['observations'] = [receipt]
        except Exception as exc:
            errors.append({'reason': 'observation_recording_error', 'detail': str(exc)})
            result['recording_status'] = 'recording_error'
    result['rows'] = confirm_daily_observations(code, dates, calendar, receipts, cutoff, instrument_kind=instrument_kind)
    result['effective_knowledge_as_of'] = cutoff.isoformat()
    if saved['errors'] or result['recording_status'] == 'recording_error':
        result['recording_status'] = 'recording_error'
        for row in result['rows'].values():
            row.update(status='unknown', reason='recording_error')
    return result
