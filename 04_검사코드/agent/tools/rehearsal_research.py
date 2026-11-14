"""Local step 4–7 rehearsal using real stores and tools, synthetic external data.

The caller owns filesystem isolation and the network/process deny guards.  No
model, calendar, daily provider or execution proof is silently substituted in
the production modules.  Fixed AI submissions are labelled as fixtures here.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta

import common as C
import mcp_server as M
import ai_judgment as J
import close_evaluation as CE
import daily_observations as DO
import market_calendar as MC
import case_review as CR
import case_statistics as ST
import learning_proposals as LP
import validation_spec as VS
import validation_dataset as VD
import policy_validation as PV


def _at(clock, timestamp):
    clock['now'] = datetime.fromisoformat(timestamp)


def _calendar():
    """Explicit fictional weekday tape; not a claim about the real KRX calendar."""
    sessions, closed = [], []
    d, end = date(2026, 9, 1), date(2026, 12, 15)
    while d <= end:
        s = d.isoformat()
        if d.weekday() < 5:
            sessions.append({'date': s, 'open_at': s+'T09:00:00+09:00',
                             'close_at': s+'T15:30:00+09:00'})
        else:
            closed.append({'date': s, 'reason': 'synthetic_weekend'})
        d += timedelta(days=1)
    return MC.register_reviewed_calendar({
        'source': 'LOCAL_REHEARSAL_SYNTHETIC_CALENDAR', 'version': '1',
        'reviewer': 'local-rehearsal-fixture-not-exchange-verification',
        'coverage_start': '2026-09-01', 'coverage_end': '2026-12-15',
        'sessions': sessions, 'closed_dates': closed})


def _daily_provider(calendar):
    prices = {'2026-11-06': 20400, '2026-11-09': 20500,
              '2026-11-10': 19700, '2026-11-11': 20500}
    calls = []
    def fetch(code, start, end, kind):
        calls.append({'code': code, 'start': start, 'end': end, 'kind': kind,
                      'received_at': C.now().isoformat()})
        return {'source': 'LOCAL_REHEARSAL_SYNTHETIC_DAILY', 'version': '1',
                'price_basis': DO.PRICE_BASIS[kind],
                'provider_finality': 'not_provided',
                'rows': [{'date': d, 'close': prices.get(d, 20000)}
                         for d in MC.session_dates(calendar) if start <= d <= end]}
    return fetch, calls


def _evaluation_for(job, application_id):
    rows = CE.get_premise_evaluations(scoring_job_id=job['scoring_job_id'],
                                    application_id=application_id)['evaluations']
    assert len(rows) == 1, rows
    return rows[0]


def _synthetic_model():
    """Separate mathematics exercise, never archived as real judgments/models."""
    import numpy as np
    rng = np.random.default_rng(52)
    axis, current = [], date(2026, 1, 5)
    while len(axis) < 110:
        if current.weekday() < 5:
            axis.append(current.isoformat())
        current += timedelta(days=1)
    calendar = {'calendar_id': 'rehearsal-synthetic-model-calendar', 'hash': 'synthetic',
                'status': 'complete', 'available_at': '2026-01-01T00:00:00+09:00',
                'sessions': [{'date': d, 'open_at': d+'T09:00:00+09:00',
                              'close_at': d+'T15:30:00+09:00'} for d in axis]}
    raw = rng.normal(size=(480, 4))
    logits = .7*raw[:, 0]-.65*raw[:, 3]+.2*raw[:, 1]+.15*raw[:, 0]*raw[:, 1]
    labels = (rng.random(480) < 1/(1+np.exp(-logits))).astype(int)
    cases = []
    for i, (x, label) in enumerate(zip(raw, labels)):
        d = axis[i//6]
        cases.append({'application_id': f'synthetic-only-{i:05}', 'code': f'{i%12:06}',
            'judgment_date': d, 'origin_at': d+'T10:00:00+09:00',
            'origin_judgment_id': f'synthetic-judgment-{i}', 'case_context_version': 'CASE_CONTEXT@v1',
            'stratum': {'definition_id': 'MA20_MAINTENANCE@v2', 'days': 1,
                        'direction': 'above', 'measurement': 'synthetic', 'instrument_kind': 'equity'},
            'features': {'gap': float(x[0]/100), 'slope': float(x[1]/100),
                         'foreign_ratio': float(x[2]), 'institution_ratio': float(x[3]),
                         'regime': ['상승', '횡보', '하락'][i%3]},
            'outcome_eligible': True, 'model_eligible': True, 'label': int(label),
            'label_available_at': axis[i//6+2]+'T08:30:00+09:00',
            'evaluation_record_available_at': axis[i//6+2]+'T08:31:00+09:00',
            'evaluation_id': f'synthetic-evaluation-{i}',
            'window_snapshot': {'sessions': [axis[i//6+1]], 'status': 'resolved'}})
    result = ST.fit_groups(cases, calendar, C.now().isoformat(), uncertainty=False)
    assert len(result['models']) == 1, result
    model = result['models'][0]
    assert model['coefficients'][1] > 0 and model['coefficients'][4] < 0
    predicted, baseline = ST.predict(model, cases[0])
    assert 0 <= predicted <= 1
    return {'status': 'passed', 'source': 'explicit_synthetic_480_cases',
            'case_count': len(cases), 'symbol_count': 12, 'judgment_dates': 80,
            'fit_status': result['status'], 'coefficients': model['coefficients'],
            'prediction': predicted, 'baseline_probability': baseline,
            'uncertainty': 'not_run_in_this_rehearsal',
            'separate_uncertainty_test': 'test_case_statistics.py::test_production_1000_draw_policy_on_synthetic_known_association',
            'published_as_operational_model': False, 'real_cases_created': 0}


def _synthetic_portfolio():
    """Exercise two real virtual accounts on an explicitly invented price tape.

    This standalone mathematical scenario is not inserted into the historical
    validation store, nor represented as proof of continuous live monitoring.
    """
    import simulation
    start, end = '2026-11-06T10:00:00+09:00', '2026-11-06T10:03:00+09:00'
    params, limits = copy.deepcopy(C.params()), copy.deepcopy(C.risk_limits())
    params['rsi_boost'] = None
    params['market_boost'] = None
    decision = {'decision_id': 'synthetic-portfolio-decision', 'at': start,
        'code': '000001', 'side': '매수', 'recommended_action': '매수',
        'module_state': {'common._EXCH': None}, 'raw_confidence': .8,
        'params': params, 'risk': limits, 'quote_observation_id': 'synthetic-quote',
        'regime': '상승', 'briefing': {'candidates': [{'code': '000001', 'rsi14': 50, 'news': []}]},
        'condition_context': {'features': {'gap': .02}}}
    spec = {'period': {'start': start, 'end': end},
        'costs': {'fee_rate': .001, 'sell_tax_rate': .002, 'slippage_bps': 0, 'latency_seconds': 1},
        'execution': {'fill_model': 'next_observation', 'quantity_policy': 'follow_plan',
                      'terminal_holdings': 'mark_to_market', 'max_quote_age_seconds': 1000},
        'metrics': {'block_length': 1, 'bootstrap_repetitions': 100, 'family_size': 1}, 'seed': 7}
    def quote(identifier, at, price):
        return {'observation_id': identifier, 'code': '000001', 'observed_at': at,
                'available_at': at, 'price': price, 'price_basis': 'unadjusted',
                'source': 'EXPLICIT_SYNTHETIC_PORTFOLIO_TAPE', 'executable_qty': 10000}
    inputs = {'complete': True, 'initial_account': {'complete': True, 'captured_at': start,
        'cash': 10_000_000, 'total': 10_000_000, 'holdings': []},
        'initial_positions': {}, 'initial_journal': [], 'decisions': [decision],
        'market_observations': [quote('synthetic-quote', start, 20000),
            quote('synthetic-fill', '2026-11-06T10:00:02+09:00', 20000),
            quote('synthetic-end', end, 22000)], 'valuation_times': [start, end],
        'monitor_checks': [], 'monitor_coverage': 'complete',
        'rule_exit_contract': 'frozen_explicit_exit_decisions'}
    proposal = {'kind': 'conditional_adjustment',
        'condition': {'op': 'gt', 'left': {'field': 'features.gap'},
                      'right': {'value': 0, 'unit': 'ratio'}},
        'effect': {'operation': 'add_confidence', 'amount': -.2}}
    before = copy.deepcopy(inputs)
    result = simulation.run_pair(inputs, spec, proposal)
    assert result['status'] == 'complete', result
    base, changed = result['arms']['base'], result['arms']['changed']
    assert base['events'][0]['plan']['qty'] > changed['events'][0]['plan']['qty']
    assert base['metrics']['fees'] > changed['metrics']['fees'] > 0
    assert result['net_equity_difference'] < 0
    assert before == inputs
    # The same production engine must retain uncertainty about missing capacity.
    missing_capacity = copy.deepcopy(inputs)
    missing_capacity['market_observations'][1]['executable_qty'] = None
    missing = simulation.run_pair(missing_capacity, spec, proposal)
    assert missing['status'] == 'incomplete', missing
    return {'status': 'passed', 'source': 'separate_explicit_synthetic_portfolio',
        'comparison_status': result['status'], 'baseline_qty': base['events'][0]['plan']['qty'],
        'changed_qty': changed['events'][0]['plan']['qty'],
        'baseline_metrics': base['metrics'], 'changed_metrics': changed['metrics'],
        'net_equity_difference': result['net_equity_difference'],
        'uncertainty_status': result['statistics']['status'],
        'missing_liquidity_status': missing['status'],
        'missing_liquidity_reasons': missing['arms']['base']['gaps'],
        'assumptions': ['invented_complete_three_minute_tape', 'fixed_synthetic_fill_capacity',
                        'declared_synthetic_no_rule_exits', 'fixed_simulation_costs_not_live_costs'],
        'historical_validation_records_created': 0, 'operational_activation': False}


def _captured_input_controls(inputs, spec, proposal):
    """Remove captured proof from private copies; do not edit the archived run."""
    import simulation
    original = copy.deepcopy(inputs)
    results = {}
    for missing in ('account_completeness', 'quote_metadata', 'liquidity', 'monitor_coverage'):
        damaged = copy.deepcopy(inputs)
        if missing == 'account_completeness':
            damaged['initial_account']['complete'] = False
        elif missing == 'quote_metadata':
            for quote in damaged['market_observations']:
                quote['price_basis'] = 'unknown'
        elif missing == 'liquidity':
            for quote in damaged['market_observations']:
                quote['executable_qty'] = None
        else:
            damaged['monitor_coverage'] = 'unknown'
        result = simulation.run_pair(damaged, spec, proposal)
        assert result['status'] == 'incomplete', {
            'removed': missing, 'status': result['status'], 'gaps': result.get('gaps')}
        results[missing] = {'status': result['status'], 'gaps': result.get('gaps', []),
            'arm_gaps': {name: arm.get('gaps', []) for name, arm in result.get('arms', {}).items()}}
    assert original == inputs
    # At the original decision instant, a later received observation must not
    # become the decision price even if its provider timestamp looks earlier.
    from simulation_market import FrozenMarket
    decision = inputs['decisions'][0]
    original_quote = next(q for q in inputs['market_observations']
                          if q['observation_id'] == decision['quote_observation_id'])
    future = {**copy.deepcopy(original_quote), 'observation_id': 'explicit-future-receipt-control',
              'price': 999999, 'observed_at': decision['at'],
              'available_at': (datetime.fromisoformat(decision['at']) + timedelta(seconds=1)).isoformat()}
    market = FrozenMarket([*copy.deepcopy(inputs['market_observations']), future],
        end=spec['period']['end'], max_age_seconds=spec['execution']['max_quote_age_seconds'])
    selected = market.quote(decision['code'], decision['at'])
    assert selected['observation_id'] == decision['quote_observation_id']
    results['future_receipt'] = {'status': 'excluded_at_original_decision',
        'selected_observation_id': selected['observation_id'], 'future_price_selected': False}
    return {'source': 'private_copies_of_captured_inputs_with_explicitly_removed_fields',
            'archived_records_changed': False, 'results': results}


def _proposal_items(context, page, application_id):
    import evidence_wire as W
    if context.get('wire_format') == W.FORMAT:
        declaration = {'wire_format': W.FORMAT, 'bundle_refs': [context['bundle_alias'], page['bundle_alias']]}
        context, page = W.public_document(context), W.public_document(page)
        metric = next(x for x in context['metric_catalog'] if x['value'] is not None)
    else:
        declaration = {'delivery_ids': [context['delivery_id'], page['delivery_id']]}
        metric = next(x for x in context['catalog'] if x['value'] is not None)
    common = {**declaration,
        'fact_claims': [{'id': 'f1', 'reference': metric['reference'], 'op': 'value',
                         'value': metric['value'], 'unit': metric['unit']}],
        'stratum_ids': [c['stratum_id'] for c in page['cases'] if c['application_id'] == application_id],
        'condition': {'op': 'eq', 'left': {'field': 'stratum.instrument_kind'},
                      'right': {'value': 'equity', 'unit': 'text'}},
        'reason': '로컬 모의 사례 하나로 운영 개선을 주장하지 않음',
        'support_case_ids': [], 'counter_case_ids': []}
    keep = {**copy.deepcopy(common), 'item_id': 'keep', 'kind': 'retain_current',
            'title': '표본 부족으로 현행 유지', 'reason_kind': 'insufficient_evidence',
            'validation_kind': 'none'}
    change = {**copy.deepcopy(common), 'item_id': 'replay-only', 'kind': 'conditional_rule_change',
        'title': '모의 검증 전용 확신도 보정안',
        'target_ref': next(t['target_ref'] for t in context['targets'] if t['target_id'] == 'confidence.conditional'),
        'effect': {'operation': 'add_confidence', 'amount': -.04, 'unit': 'confidence',
                   'placement': 'after_legacy_boost_before_cap'},
        'validation_kind': 'deterministic_rule', 'support_case_ids': [application_id],
        'validation_question': '당시 원본 호출을 재현하고 시험용 -0.04의 계획 차이를 계산할 수 있는가',
        'expected_change': '구현 연결 확인용이며 매매 개선 효과는 미검증'}
    return keep, change


def run_research(monkeypatch, execution_result, clock):
    """Exercise stages 4–7 and return serializable evidence, not operation approval."""
    main_code = execution_result.get('main_code', execution_result.get('code', '000010'))
    judgment = J.load_judgment(execution_result['judgment_id'])
    app = judgment['premise_applications'][0]
    app_id = app['application_id']
    calendar = _calendar()
    provider, daily_calls = _daily_provider(calendar)
    checkpoints = []
    for timestamp, phase, verdict, full in (
        ('2026-11-09T16:00:00+09:00', 'close', 'pending', False),
        ('2026-11-10T16:00:00+09:00', 'close', 'pending', False),
        ('2026-11-11T08:30:00+09:00', 'prep', 'refuted', False),
        ('2026-11-11T16:00:00+09:00', 'close', 'refuted', False),
        ('2026-11-12T08:30:00+09:00', 'prep', 'refuted', True)):
        _at(clock, timestamp)
        job = CE.run_evaluation(calendar=calendar, provider=provider, phase=phase)
        evaluation = _evaluation_for(job, app_id)
        assert evaluation['verdict'] == verdict, evaluation
        assert (evaluation['data_status'] == 'complete') == full, evaluation
        checkpoints.append({'at': timestamp, 'phase': phase,
            'scoring_job_id': job['scoring_job_id'], 'evaluation_id': evaluation['evaluation_id'],
            'verdict': evaluation['verdict'], 'data_status': evaluation['data_status'],
            'period_status': evaluation.get('period_status'),
            'first_known_refutation': evaluation.get('first_known_refutation')})
    assert evaluation['first_known_refutation']['date'] == '2026-11-10'
    assert all(c.get('provider_finality') == 'not_provided' for c in evaluation['comparisons']
               if c.get('source_snapshot'))
    stage4 = {'status': 'passed', 'application_id': app_id, 'calendar_id': calendar['calendar_id'],
              'calendar_is_synthetic': True, 'provider_finality': 'not_provided',
              'checkpoints': checkpoints, 'daily_provider_calls': daily_calls}

    _at(clock, '2026-11-14T10:00:00+09:00')
    review = CR.build_review()
    assert review['analysis_status'] == 'complete', review
    review_id = review['review_id']
    monkeypatch.setenv('RUN_MODE', 'case_review')
    monkeypatch.setenv('CASE_REVIEW_ID', review_id)
    monkeypatch.setenv('CASE_REVIEW_ATTEMPT_ID', 'rehearsal-case-review')
    before = M.get_case_review(review_id)
    page = M.get_review_cases(review_id)
    import evidence_wire as W
    review_wire = ({'wire_format': W.FORMAT, 'bundle_refs': [before['bundle_alias'], page['bundle_alias']]}
                   if before.get('wire_format') == W.FORMAT else {})
    before, page = W.public_document(before), W.public_document(page)
    case = next(c for c in page['cases'] if c['application_id'] == app_id)
    assert case['outcome_eligible'] is True, case
    metric = next(m for m in before['metric_catalog']
                  if m['path'] == '/comparison/summary/refutation_rate')
    fact = {'id': 'refutation-rate', 'metric_id': metric['metric_id'], 'op': 'value',
            'value': metric['value'], 'unit': metric['unit']}
    rejected = M.record_case_review(review_id, 'fixture-wrong-rate',
                                   [{**fact, 'value': 1-float(metric['value'])}], [], **review_wire)
    assert rejected['status'] == 'rejected', rejected
    accepted = M.record_case_review(review_id, 'fixture-corrected-rate', [fact], [{
        'id': 'fixture-hypothesis', 'text': '모의 사례의 기관 매도가 반증에 관련됐을 수 있다는 사후 가설',
        'finding_ids': [fact['id']], 'case_ids': [app_id]}], **review_wire)
    assert accepted['status'] == 'accepted', accepted
    assert accepted['hypotheses'][0]['hypothesis_status'] == 'submitted_unverified'
    repeated = M.record_case_review(review_id, 'fixture-corrected-rate', [fact], [{
        'id': 'fixture-hypothesis', 'text': '모의 사례의 기관 매도가 반증에 관련됐을 수 있다는 사후 가설',
        'finding_ids': [fact['id']], 'case_ids': [app_id]}], **review_wire)
    assert repeated['replayed'] is True
    final_review = W.public_document(M.get_case_review(review_id))
    assert final_review['ai_status'] == 'accepted'
    assert not final_review['statistics']['models'], 'A rehearsal case must not produce a real research model'
    stage5 = {'status': 'passed', 'review_id': review_id, 'expected_count': review['expected_count'],
        'comparison': review['comparison'], 'statistics': review['statistics'],
        'ai_source': 'fixed_local_submission_no_model_call',
        'ai_states': [before['ai_status'], rejected['status'], final_review['ai_status']],
        'fact_rejection': rejected, 'accepted_submission': accepted,
        'idempotent_retry': repeated['replayed'], 'synthetic_model_exercise': _synthetic_model()}

    context_run = LP.build_context(review_id)
    assert context_run['context_status'] == 'ready', context_run
    proposal_run_id = context_run['proposal_run_id']
    monkeypatch.setenv('RUN_MODE', 'proposal_review')
    monkeypatch.setenv('PROPOSAL_RUN_ID', proposal_run_id)
    monkeypatch.setenv('PROPOSAL_ATTEMPT_ID', 'rehearsal-proposal')
    context, proposal_cases = M.get_proposal_context(proposal_run_id), M.get_proposal_cases(proposal_run_id)
    keep, change = _proposal_items(context, proposal_cases, app_id)
    proposed = M.record_learning_proposals(proposal_run_id, 'fixture-proposals', [keep, change])
    assert all(x['status'] == 'accepted' for x in proposed['items']), proposed
    queried = M.get_learning_proposals(proposal_run_id)
    proposal_id = next(x['proposal_id'] for x in proposed['items'] if x['item_id'] == 'replay-only')
    bundle = LP.export_validation_bundle(proposal_id)
    assert bundle['proposal']['active'] is False
    forbidden = M.place_order(main_code, '매수', 1, .7, '', '', 'must be blocked')
    assert forbidden['error_code'] == 'PROPOSAL_TOOL_FORBIDDEN', forbidden
    stage6 = {'status': 'passed', 'proposal_run_id': proposal_run_id, 'proposal_id': proposal_id,
        'submissions': proposed, 'proposal_count': len(queried['proposals']),
        'operational_activation': False, 'research_order_block': forbidden['error_code'],
        'ai_source': 'fixed_local_submission_no_model_call'}

    # Narrow scope is explicit: all other candidates remain in the separately
    # reported step-2 coverage denominator, not erased by this local trial.
    origin = datetime.fromisoformat(judgment['created_at'])
    spec = {'mode': 'retrospective', 'family_id': 'local-rehearsal-one-conditional-change',
        'description': '모의 원본 입력 재현만 수행; 독립 검증/운영 채택 없음',
        'period': {'start': origin.replace(second=0, microsecond=0).isoformat(),
                   'end': (origin+timedelta(minutes=2)).isoformat(),
                   'knowledge_as_of': C.now().isoformat()},
        'scope': {'codes': [main_code], 'confidence_for': ['매수']},
        'costs': {**{name: C.params()['costs'][name] for name in ('fee_rate', 'sell_tax_rate')},
                  'slippage_bps': 0, 'latency_seconds': 1},
        'execution': {'fill_model': 'next_observation', 'quantity_policy': 'follow_plan',
                      'terminal_holdings': 'mark_to_market', 'max_quote_age_seconds': 120},
        'metrics': {'primary': 'net_equity_difference', 'family_size': 1,
                    'bootstrap_repetitions': 100, 'block_length': 1}, 'seed': 7}
    registered = VS.register_validation(proposal_id, 'fixture-validation', spec)
    assert registered['status'] == 'registered', registered
    validation_id = registered['validation_id']
    replay = PV.run_validation(validation_id)
    assert replay.get('result_id'), replay
    screening = PV.run_validation(validation_id, mode='screen_only')
    portfolio = PV.run_validation(validation_id, mode='portfolio')
    frozen = VD.get_inputs(validation_id)
    assert replay['adoption_eligibility']['status'] == 'ineligible'
    assert portfolio['operational_activation'] is False
    captured_controls = None
    if execution_result.get('capture_session_id'):
        assert (frozen.get('simulation_inputs') or {}).get('complete') is True, (frozen.get('simulation_inputs') or {}).get('gaps')
        # Only the source-copy rehearsal can establish loaded-code identity.
        # Small unit fixtures deliberately patch common functions and must keep
        # the original incomplete-proof outcome rather than bypass that gate.
        if replay['baseline_status'] == 'passed':
            performance = portfolio['performance']
            assert performance['status'] == 'complete', {
                'status': performance['status'], 'gaps': performance.get('gaps'),
                'arm_gaps': {name: arm.get('gaps') for name, arm in performance.get('arms', {}).items()}}
            captured_controls = _captured_input_controls(frozen['simulation_inputs'], portfolio['spec'], portfolio['proposal'])
    assert PV.run_validation(validation_id)['result_id'] == replay['result_id']
    stage7 = {'status': 'passed' if replay['baseline_status'] == 'passed' else 'limited_by_original_inputs',
        'validation_id': validation_id, 'proposal_id': proposal_id,
        'replay_result_id': replay['result_id'], 'replay_status': replay['status'],
        'baseline_status': replay['baseline_status'], 'effect_status': replay['effect_status'],
        'coverage': replay['coverage'], 'gaps': replay['gaps'],
        'event_replays': replay['events'], 'selection_replays': replay['selection_replays'],
        'screen_only': {'result_id': screening.get('result_id'), 'status': screening['status'],
                        'baseline_status': screening.get('baseline_status')},
        'portfolio': {'result_id': portfolio.get('result_id'), 'status': portfolio['status'],
                      'performance': portfolio.get('performance'),
                      'simulation_input_gaps': (frozen.get('simulation_inputs') or {}).get('gaps', [])},
        'adoption_eligibility': portfolio['adoption_eligibility'],
        'synthetic_portfolio_exercise': _synthetic_portfolio(),
        'captured_input_negative_controls': captured_controls,
        'real_ai_calls': 0, 'operational_activation': False}
    stages = {'4': stage4, '5': stage5, '6': stage6, '7': stage7}
    return {'stages': stages, 'summary': stages,
            'review_id': review_id, 'proposal_run_id': proposal_run_id, 'proposal_id': proposal_id,
            'validation_id': validation_id, 'validation_result_id': portfolio.get('result_id'),
            'result_id': portfolio.get('result_id'),
            'replay_result_id': replay['result_id'], 'application_id': app_id,
            'calendar_id': calendar['calendar_id'], 'finished_at': C.now().isoformat()}
