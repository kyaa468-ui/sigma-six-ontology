"""Two independent ledgers driven by the same frozen historical AI decisions.

This is a fixed-judgment-stream experiment, not an adaptive AI backtest. Every
missing decision, price or execution assumption remains visible in coverage.
"""
from __future__ import annotations
import copy
import math
import random
from decimal import Decimal
from datetime import timedelta
import common as C
from replay_context import Engine, ReplayUnavailable, _effect, changed_params, instant, projection
from simulation_market import FrozenMarket

VERSION = 'PAIRED_FIXED_JUDGMENT_SIMULATION@v1'


def _finite(value, field, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or positive and value <= 0:
        raise ReplayUnavailable('invalid_' + field)
    return value


class Ledger:
    def __init__(self, initial, positions, journal, at):
        if not isinstance(initial, dict) or initial.get('complete') is not True:
            raise ReplayUnavailable('complete_initial_account_missing')
        if not initial.get('captured_at') or instant(initial['captured_at'])!=at:
            raise ReplayUnavailable('initial_account_time_mismatch')
        self.cash = _finite(initial['cash'], 'initial_cash')
        self.initial_total=_finite(initial['total'],'initial_total',True)
        self.initial_reconciled=False
        self.holdings = {}
        for h in copy.deepcopy(initial['holdings']):
            if type(h.get('qty')) is not int or h['qty'] <= 0 or h['code'] in self.holdings:
                raise ReplayUnavailable('invalid_initial_holding')
            _finite(h.get('avg_price'), 'initial_average_price', True)
            self.holdings[h['code']] = h
        if not isinstance(positions, dict) or not isinstance(journal, list):
            raise ReplayUnavailable('initial_virtual_state_missing')
        self.positions, self.journal = copy.deepcopy(positions), copy.deepcopy(journal)
        if any(code not in self.positions for code in self.holdings):
            raise ReplayUnavailable('initial_position_metadata_missing')
        self.start_at = at
        self.events, self.nav, self.pending, self.unknown = [], [], [], []
        self.used_capacity, self.monitor_fired = {}, {}
        self.monitor_date, self.permitted_exits, self.monitor_events = None, set(), []
        self.fees = self.taxes = self.turnover = 0.0
        self.day_start = None

    def settle(self, at):
        ready = [item for item in self.pending if instant(item['fill']['at']) <= at]
        for item in sorted(ready, key=lambda x: (x['fill']['at'], x['decision_id'])):
            self.pending.remove(item)
            fill, order = item['fill'], item['order']
            qty, code = fill['filled_qty'], order['code']
            if not qty:
                continue
            current = self.holdings.get(code)
            if order['side'] == '매수':
                cost = fill['amount'] + fill['fee']
                if cost > self.cash + 1e-8:
                    self.unknown.append({'reason': 'fill_exceeds_available_cash', 'decision_id': item['decision_id']})
                    continue
                oldqty = current['qty'] if current else 0
                avg = ((current['avg_price'] * oldqty if current else 0) + fill['amount']) / (oldqty + qty)
                self.holdings[code] = {'code': code, 'name': order.get('name', code), 'qty': oldqty + qty,
                                       'avg_price': avg, 'price': fill['price'], 'value': (oldqty + qty) * fill['price']}
                self.cash -= cost
                previous = self.positions.get(code, {})
                self.positions[code] = {**previous, 'entry_date': previous.get('entry_date') or fill['at'][:10],
                    'stop': order.get('stop'), 'take': order.get('take'), 'max_days': order.get('max_days'),
                    'index_dip': order.get('index_dip', False)}
            else:
                if not current or qty > current['qty']:
                    self.unknown.append({'reason': 'fill_exceeds_available_holding', 'decision_id': item['decision_id']})
                    continue
                self.cash += fill['amount'] - fill['fee'] - fill['tax']
                current['qty'] -= qty
                if not current['qty']:
                    del self.holdings[code]
                    self.positions.pop(code, None)
            self.fees += fill['fee']; self.taxes += fill['tax']; self.turnover += fill['amount']
            self.journal.append({'kind': 'fill', 'time': fill['at'], 'run': item.get('run_id'),
                'code': code, 'side': order['side'], 'qty': qty, 'price': fill['price']})

    def account(self, market, at):
        self.settle(at)
        holdings = []
        for code, h in self.holdings.items():
            q = market.quote(code, at)
            holdings.append({**h, 'price': q['price'], 'value': h['qty'] * q['price']})
        total = self.cash + sum(h['value'] for h in holdings)
        if not self.initial_reconciled:
            exact=Decimal(str(self.cash))+sum((Decimal(str(h['qty']))*Decimal(str(h['price'])) for h in holdings),Decimal(0))
            if exact != Decimal(str(self.initial_total)):
                raise ReplayUnavailable('initial_account_equity_not_reconciled')
            self.initial_reconciled=True
        if total <= 0:
            raise ReplayUnavailable('nonpositive_portfolio_equity')
        return {'cash': self.cash, 'total': total, 'holdings': holdings, 'complete': True}

    def snapshots(self, decision, account, at):
        # Non-state source files are frozen by the collector; the portfolio,
        # fills and day start always come from this arm's independent ledger.
        values = copy.deepcopy(decision.get('snapshots') or {})
        journal = copy.deepcopy(self.journal)
        if decision.get('regime'):
            journal.append({'kind': 'regime', 'time': at.isoformat(), 'regime': decision['regime']})
        values.update({'positions.json': self.positions, 'journal.jsonl': journal,
                       'day_start.json': self.day_start})
        if 'briefing' in decision:
            values['briefing.json'] = decision['briefing']
        return values


def monitor_due(holdings, metadata, at, fired, *, retry_seconds=600, trade_from=930):
    """Legacy weekday age and legacy retry key; episode IDs are deliberately absent."""
    at = instant(at) if isinstance(at, str) else at
    results = []
    for h in holdings:
        m = metadata.get(h['code'])
        if not m:
            continue
        key = f"{h['code']}@{m.get('entry_date')}/{m.get('stop')}/{m.get('take')}"
        rule = 'stop' if m.get('stop') and h['price'] <= m['stop'] else 'take' if m.get('take') and h['price'] >= m['take'] else None
        if not rule and m.get('entry_date') and C.business_days(m['entry_date'], at.date()) >= m.get('max_days', 99):
            rule = 'max_days'
        if rule:
            due = at.hour * 100 + at.minute >= trade_from and at.timestamp() - fired.get(key, 0) >= retry_seconds
            results.append({'code': h['code'], 'monitor_key': key, 'rule': rule, 'due': due})
    return results


def _decision(ledger, market, decision, spec, proposal):
    at = instant(decision['at']); account = ledger.account(market, at)
    if ledger.pending:
        raise ReplayUnavailable('pending_execution_before_next_decision')
    if ledger.day_start is None or ledger.day_start['date'] != at.date().isoformat():
        ledger.day_start = {'date': at.date().isoformat(), 'total': account['total']}
    action = decision.get('recommended_action')
    if action not in ('매수', '매도', '보유', '관망'):
        raise ReplayUnavailable('original_recommended_action_missing')
    if action in ('보유', '관망'):
        ledger.events.append({'decision_id': decision['decision_id'], 'at': at.isoformat(),
            'code': decision['code'], 'side': decision['side'], 'status': 'watch',
            'recommended_action': action, 'raw_confidence': decision['raw_confidence']})
        return
    if decision.get('monitor_exit') and decision['decision_id'] not in ledger.permitted_exits:
        ledger.events.append({'decision_id': decision['decision_id'], 'at': at.isoformat(),
            'code': decision['code'], 'side': decision['side'], 'status': 'monitor_not_triggered'})
        return
    p, r = decision['params'], decision['risk']
    effect, condition = _effect(proposal, decision.get('condition_context'))
    if effect and effect.get('operation') == 'prompt_guidance':
        raise ReplayUnavailable('prompt_pair_judgment_stream_required')
    p = changed_params(p, proposal, effect)
    delta = effect.get('amount', 0) if effect and effect.get('operation') == 'add_confidence' else 0
    runtime = {'at': at.isoformat(), 'module_state':decision.get('module_state'),
               'environment': {'RUN_MODE': decision.get('run_mode', 'decide'),
               'RUN_ID': decision.get('run_id', decision['decision_id'])}}
    engine = Engine(runtime, [], params=p, risk=r, strict=False,
                    snapshots=ledger.snapshots(decision, account, at), confidence_delta=delta,
                    policy_context=decision.get('policy_context') if proposal is None else {})
    code, side = decision['code'], decision['side']
    raw = _finite(decision['raw_confidence'], 'raw_confidence')
    quote = market.quote(code, at)
    if decision.get('quote_observation_id') != quote['observation_id']:
        raise ReplayUnavailable('decision_quote_reference_mismatch')
    detail = engine.common.conf_boost_details(code, side, raw, p)
    if side == '매수':
        plan = engine.sizing.plan_buy(code, detail['adjusted'], quote['price'], account, p, r,
                                     regime=decision.get('regime'), today_change=decision.get('today_change'))
    elif side == '매도':
        plan = engine.sizing.plan_sell(code, detail['adjusted'], quote['price'], account, p,
                                      rule_exit=decision.get('rule_exit', False))
    else:
        raise ReplayUnavailable('unresolved_confidence_direction')
    qty = plan['qty']
    if spec['execution']['quantity_policy'] == 'recorded_request':
        qty = decision['requested_qty']
    if type(qty) is not int or qty < 0:
        raise ReplayUnavailable('invalid_request_quantity')
    order = {**plan, 'qty': qty, 'confidence': raw, 'rule_exit': decision.get('rule_exit', False),
             'today_change': decision.get('today_change')}
    violations = engine.risk.check(order, account, quote['price'], p, r) if qty else []
    event = {'decision_id': decision['decision_id'], 'at': at.isoformat(), 'code': code, 'side': side,
             'condition_status': condition, 'plan': projection(plan), 'confidence': detail,
             'violations': [list(v) for v in violations], 'status': 'blocked' if violations else 'zero_quantity' if not qty else 'sent'}
    if qty and not violations:
        fill = market.fill(code, side, qty, at, spec['costs'], ledger.used_capacity)
        event['fill'] = fill
        if fill['status'] == 'unknown':
            ledger.unknown.append({'decision_id': decision['decision_id'], **fill})
        elif fill['filled_qty']:
            ledger.pending.append({'decision_id': decision['decision_id'], 'order': order, 'fill': fill,
                                   'run_id': runtime['environment']['RUN_ID']})
    ledger.events.append(event)


def _monitor(ledger, market, check, proposal):
    at = instant(check['at']); account = ledger.account(market, at)
    if ledger.monitor_date != at.date().isoformat():
        ledger.monitor_fired = {}; ledger.monitor_date = at.date().isoformat()
    p = check['params']
    # A global parameter proposal can alter exit levels. A judgment-dependent
    # condition unavailable to a monitoring tick must remain unknown.
    effect, condition = _effect(proposal, check.get('condition_context'))
    p = changed_params(p, proposal, effect)
    runtime = {'at': at.isoformat(), 'module_state':check.get('module_state'),
               'environment': {'RUN_MODE': 'decide', 'RUN_ID': check.get('check_id')}}
    engine = Engine(runtime, [], params=p, strict=False, snapshots=ledger.snapshots(check, account, at))
    metadata = engine.common.position_meta(account['holdings'], p)
    triggers = monitor_due(account['holdings'], metadata, at, ledger.monitor_fired)
    for trigger in triggers:
        if not trigger['due']:
            continue
        code = trigger['code']; identifier = check.get('exit_decision_ids', {}).get(code)
        if not identifier:
            ledger.unknown.append({'at': at.isoformat(), 'code': code,
                                   'reason': 'new_monitor_exit_requires_unrecorded_ai_judgment'})
            continue
        ledger.permitted_exits.add(identifier)
        returncode = check.get('process_returncodes', {}).get(code)
        if returncode is None:
            ledger.unknown.append({'at': at.isoformat(), 'code': code, 'reason': 'monitor_process_outcome_missing'})
        elif returncode == 0:
            ledger.monitor_fired[trigger['monitor_key']] = at.timestamp()
    ledger.monitor_events.append({'at': at.isoformat(), 'triggers': triggers, 'condition_status': condition,
                                  'retry_keys': copy.deepcopy(ledger.monitor_fired)})


def _metrics(ledger):
    values = [r['equity'] for r in ledger.nav]
    peak, drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value); drawdown = max(drawdown, 1 - value / peak)
    return {'initial_equity': values[0], 'terminal_equity': values[-1],
            'net_return': values[-1] / values[0] - 1, 'max_drawdown': drawdown,
            'fees': ledger.fees, 'taxes': ledger.taxes, 'gross_turnover': ledger.turnover,
            'turnover_ratio': ledger.turnover / values[0], 'decision_count': len(ledger.events),
            'filled_order_count': sum(bool(e.get('fill', {}).get('filled_qty')) for e in ledger.events)}


def paired_statistics(base, changed, spec):
    """Pair by date first, then resample contiguous date blocks jointly."""
    if [r['date'] for r in base] != [r['date'] for r in changed]:
        raise ReplayUnavailable('paired_date_coverage_mismatch')
    def daily(rows):
        # Multiple valuations in one session do not create extra market days.
        last={row['date']:row for row in rows}
        ends=[last[key] for key in dict.fromkeys(row['date'] for row in rows)]
        return [rows[0],*ends] if ends[0] is not rows[0] else ends
    base,changed=daily(base),daily(changed)
    differences = [changed[i]['equity'] / changed[i-1]['equity'] - base[i]['equity'] / base[i-1]['equity']
                   for i in range(1, len(base))]
    metrics = spec.get('metrics', {})
    length, repetitions = metrics.get('block_length'), metrics.get('bootstrap_repetitions')
    family = metrics.get('family_size')
    if not differences or not all(type(x) is int and x > 0 for x in (length, repetitions, family)):
        return {'status': 'insufficient_data', 'reason': 'registered_bootstrap_contract_missing', 'paired_sessions': len(differences)}
    if len(differences) < 2 * length:
        return {'status': 'insufficient_data', 'reason': 'fewer_than_two_date_blocks', 'paired_sessions': len(differences)}
    rng = random.Random(spec.get('seed', 0)); draws = []
    starts = list(range(len(differences) - length + 1))
    for _ in range(repetitions):
        sample = []
        while len(sample) < len(differences):
            start = rng.choice(starts); sample.extend(differences[start:start+length])
        draws.append(sum(sample[:len(differences)]) / len(differences))
    draws.sort(); alpha = .05 / family
    lo, hi = draws[int((alpha/2)*(repetitions-1))], draws[int((1-alpha/2)*(repetitions-1))]
    return {'status': 'complete', 'paired_sessions': len(differences), 'mean_daily_return_difference': sum(differences)/len(differences),
            'interval': [lo, hi], 'familywise_alpha': .05, 'family_size': family, 'method': 'paired_moving_date_block_bonferroni',
            'block_length': length, 'bootstrap_repetitions': repetitions, 'seed': spec.get('seed', 0)}


def run_pair(inputs, spec, proposal=None):
    """No external callbacks: all execution, valuation and policy inputs are frozen."""
    result = {'version': VERSION, 'status': 'incomplete', 'comparison_kind': 'fixed_judgment_stream',
              'adaptive_ai_verified': False, 'operational_execution': False, 'arms': {}, 'gaps': []}
    try:
        if not isinstance(inputs, dict) or inputs.get('complete') is not True:
            raise ReplayUnavailable('simulation_input_manifest_incomplete')
        required_costs = ('fee_rate', 'sell_tax_rate', 'slippage_bps', 'latency_seconds')
        if any(k not in spec.get('costs', {}) for k in required_costs):
            raise ReplayUnavailable('registered_costs_missing')
        for field in required_costs:
            if _finite(spec['costs'][field],field)<0:
                raise ReplayUnavailable('negative_execution_cost')
        if spec['costs']['fee_rate']>=1 or spec['costs']['sell_tax_rate']>=1 or spec['costs']['slippage_bps']>=10000:
            raise ReplayUnavailable('execution_cost_out_of_range')
        if spec['execution'].get('fill_model') != 'next_observation':
            raise ReplayUnavailable('unsupported_fill_model')
        if spec['execution'].get('terminal_holdings') != 'mark_to_market':
            raise ReplayUnavailable('terminal_liquidation_requires_registered_market_window')
        if inputs.get('monitor_coverage') != 'complete':
            raise ReplayUnavailable('monitor_schedule_or_input_coverage_missing')
        # Changed-state monitor exits require new AI invocations. A historical
        # exit decision may only be reused when the frozen schedule explicitly
        # includes it; never invent a model's sell confidence.
        if inputs.get('rule_exit_contract') != 'frozen_explicit_exit_decisions':
            raise ReplayUnavailable('rule_exit_judgment_contract_missing')
        start, end = instant(spec['period']['start']), instant(spec['period']['end'])
        market = FrozenMarket(inputs['market_observations'], end=end.isoformat(),
                              max_age_seconds=spec['execution']['max_quote_age_seconds'])
        decisions = inputs['decisions']
        if not isinstance(decisions, list) or not decisions or any(not d.get('decision_id') for d in decisions):
            raise ReplayUnavailable('frozen_decision_stream_missing')
        if len({d['decision_id'] for d in decisions}) != len(decisions):
            raise ReplayUnavailable('duplicate_decision_identity')
        if [instant(d['at']) for d in decisions] != sorted(instant(d['at']) for d in decisions):
            raise ReplayUnavailable('decision_stream_not_ordered')
        if any(not start <= instant(d['at']) <= end for d in decisions):
            raise ReplayUnavailable('decision_outside_registered_period')
        streams = inputs.get('arm_decisions') or {'base': decisions, 'changed': decisions}
        expected = [(d['decision_id'], d['at'], d['code']) for d in decisions]
        if set(streams) != {'base', 'changed'} or any([(d['decision_id'], d['at'], d['code']) for d in stream] != expected for stream in streams.values()):
            raise ReplayUnavailable('paired_arm_opportunity_mismatch')
        if inputs.get('arm_decisions'):
            result['comparison_kind'] = 'paired_frozen_ai_judgments'
        monitors = inputs.get('monitor_checks')
        if not isinstance(monitors, list):
            raise ReplayUnavailable('monitor_check_sequence_missing')
        if any(not start <= instant(t['at']) <= end for t in monitors):
            raise ReplayUnavailable('monitor_check_outside_period')
        dates = inputs['valuation_times']
        if not dates or instant(dates[0]) != start or instant(dates[-1]) != end:
            raise ReplayUnavailable('complete_valuation_window_missing')
        if list(dates) != sorted(set(dates)):
            raise ReplayUnavailable('valuation_schedule_not_unique_ordered')
        arms = {name: Ledger(inputs['initial_account'], inputs['initial_positions'], inputs['initial_journal'], start)
                for name in ('base', 'changed')}
        timeline = [(instant(d['at']), 2, i, 'decision', i) for i, d in enumerate(decisions)]
        timeline += [(instant(t['at']), 1, i, 'monitor', t) for i, t in enumerate(monitors)]
        timeline += [(instant(at), 0 if i == 0 else 3, i, 'valuation', at) for i, at in enumerate(dates)]
        for at, _, _, kind, payload in sorted(timeline, key=lambda x: x[:3]):
            for name, ledger in arms.items():
                try:
                    if kind == 'decision':
                        _decision(ledger, market, streams[name][payload], spec, proposal if name == 'changed' else None)
                    elif kind == 'monitor':
                        _monitor(ledger, market, payload, proposal if name == 'changed' else None)
                    else:
                        account = ledger.account(market, at)
                        ledger.nav.append({'date': at.date().isoformat(), 'at': at.isoformat(), 'equity': account['total'],
                            'cash': account['cash'], 'gross_exposure': sum(h['value'] for h in account['holdings']) / account['total'],
                            'max_position_weight': max((h['value']/account['total'] for h in account['holdings']), default=0)})
                except (ReplayUnavailable, KeyError, TypeError, ValueError) as exc:
                    ledger.unknown.append({'at': at.isoformat(), 'kind': kind, 'reason': str(exc)})
        for name, ledger in arms.items():
            if ledger.pending:
                ledger.unknown.append({'reason': 'unresolved_pending_execution'})
            result['arms'][name] = {'events': ledger.events, 'daily_equity': ledger.nav, 'gaps': ledger.unknown,
                                   'terminal_holdings': list(ledger.holdings.values()), 'monitor_events': ledger.monitor_events,
                                   'metrics': _metrics(ledger) if ledger.nav else None}
        coverage = {name: len(ledger.events) for name, ledger in arms.items()}
        result['coverage'] = {'expected_decisions': len(decisions), 'recorded_decisions': coverage,
                              'paired_completion_rate': min(coverage.values())/len(decisions) if decisions else None}
        if any(ledger.unknown for ledger in arms.values()) or any(count != len(decisions) for count in coverage.values()):
            result['gaps'].append('arm_or_market_coverage_incomplete')
            return result
        result['status'] = 'complete'
        result['net_equity_difference'] = result['arms']['changed']['metrics']['terminal_equity'] - result['arms']['base']['metrics']['terminal_equity']
        result['statistics'] = paired_statistics(arms['base'].nav, arms['changed'].nav, spec)
        return result
    except (ReplayUnavailable, KeyError, TypeError, ValueError) as exc:
        result['gaps'].append(str(exc)); return result
