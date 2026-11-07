"""A finite, immutable raw-price tape. This module never collects live prices."""
from __future__ import annotations
import copy
import math
from datetime import timedelta
from replay_context import ReplayUnavailable, instant

VERSION = 'FROZEN_RAW_MARKET@v1'


class FrozenMarket:
    def __init__(self, observations, *, end, max_age_seconds):
        if not isinstance(observations, list) or not observations:
            raise ReplayUnavailable('raw_market_tape_missing')
        if type(max_age_seconds) not in (int, float) or max_age_seconds <= 0:
            raise ReplayUnavailable('quote_age_policy_missing')
        self.end, self.max_age = instant(end), max_age_seconds
        self.rows, seen = [], set()
        for original in observations:
            row = copy.deepcopy(original)
            identifier = row.get('observation_id')
            if not identifier or identifier in seen:
                raise ReplayUnavailable('market_observation_identity_missing_or_duplicate')
            seen.add(identifier)
            if row.get('price_basis') != 'unadjusted' or not row.get('source'):
                raise ReplayUnavailable('raw_price_basis_or_source_unverified')
            if type(row.get('price')) not in (int, float) or not math.isfinite(row['price']) or row['price'] <= 0:
                raise ReplayUnavailable('invalid_market_price')
            row['_at'], row['_available'] = instant(row['observed_at']), instant(row['available_at'])
            if row['_available'] < row['_at']:
                raise ReplayUnavailable('market_availability_before_observation')
            capacity = row.get('executable_qty')
            if capacity is not None and (type(capacity) is not int or capacity < 0):
                raise ReplayUnavailable('invalid_executable_quantity')
            self.rows.append(row)
        self.rows.sort(key=lambda r: (r['_available'], r['_at'], r['observation_id']))

    @staticmethod
    def public(row):
        return {key: value for key, value in row.items() if not key.startswith('_')}

    def quote(self, code, at):
        at = instant(at) if isinstance(at, str) else at
        if at > self.end:
            raise ReplayUnavailable('valuation_outside_registered_period')
        eligible = [r for r in self.rows if r['code'] == code and r['_available'] <= at and r['_at'] <= at]
        if not eligible:
            raise ReplayUnavailable('price_missing:' + code)
        row = max(eligible, key=lambda r: (r['_at'], r['_available'], r['observation_id']))
        if (at - row['_at']).total_seconds() > self.max_age:
            raise ReplayUnavailable('price_stale:' + code)
        return self.public(row)

    def fill(self, code, side, qty, at, costs, used=None):
        """One next-observation IOC; unknown liquidity is never a full fill."""
        at = instant(at) if isinstance(at, str) else at
        threshold = at + timedelta(seconds=costs['latency_seconds'])
        used = used if used is not None else {}
        candidates = [r for r in self.rows if r['code'] == code and r['_at'] > at and r['_at'] >= threshold
                      and r['_available'] >= threshold and r['_available'] <= self.end]
        if not candidates:
            return {'status': 'unknown', 'reason': 'next_raw_observation_missing', 'requested_qty': qty, 'filled_qty': 0}
        row = candidates[0]
        if (row['_at'] - at).total_seconds() > self.max_age:
            return {'status': 'unknown', 'reason': 'next_observation_stale', 'requested_qty': qty, 'filled_qty': 0}
        capacity = row.get('executable_qty')
        if capacity is None:
            return {'status': 'unknown', 'reason': 'executable_quantity_unavailable',
                    'requested_qty': qty, 'filled_qty': 0, 'observation_id': row['observation_id']}
        left = max(0, capacity - used.get(row['observation_id'], 0))
        filled = min(qty, left)
        used[row['observation_id']] = used.get(row['observation_id'], 0) + filled
        price = row['price'] * (1 + (1 if side == '매수' else -1) * costs['slippage_bps'] / 10000)
        if price <= 0:
            raise ReplayUnavailable('nonpositive_slipped_price')
        gross = filled * price
        return {'status': 'filled' if filled == qty else 'partial' if filled else 'unfilled',
                'requested_qty': qty, 'filled_qty': filled, 'remaining_qty': qty - filled,
                'remaining_policy': 'cancel_ioc', 'price': price, 'amount': gross,
                'fee': gross * costs['fee_rate'], 'tax': gross * costs['sell_tax_rate'] if side == '매도' else 0,
                'at': row['_available'].isoformat(), 'observation_id': row['observation_id'],
                'assumption': 'registered_next_observation_and_explicit_capacity'}
