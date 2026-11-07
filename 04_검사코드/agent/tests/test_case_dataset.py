"""Step 1→2→4→5 cases: local receipts, actual MCP entry points, no market calls."""
import json
from pathlib import Path

import pytest

from test_ontology import C, D, M, O, clone, row, brief, isolated
from test_judgment_integration import setup_run, submission
from test_close_evaluation import calendar, at, accepted, price_provider
import ai_judgment as J
import case_dataset as CD
import case_store as CS
import close_evaluation as E
import judgment_context as CX
import judgment_coverage as JC
import scoring_store as SS


def regime(value='횡보'):
    result = M.set_regime(value, 'local fixture classification')
    assert result['regime_recording_status'] == 'recorded'
    return result


def traced_delivery(monkeypatch, candidates=None, selection_regime='상승'):
    setup_run(monkeypatch)
    rows = candidates or [row(code='000001', inst_net=-4_000_000_000)]
    selection = O.SelectionTrace('prep', C.params(), selection_regime, 'fixture-selection')
    selected = D.screen(clone(rows), C.params(), True, selection_regime, trace=selection)
    selection.attach(selected)
    b = brief(selected)
    selection.finish(b)
    C.write_json(C.BRIEFING, b)
    text = M.get_briefing()
    did = text.split('브리핑 전달 ID: ')[-1].splitlines()[0]
    return text, J._load('deliveries', did)


def premise_item(text, key='origin'):
    item = submission(text, key=key)
    item['premises'] = [{'definition_id': 'MA20_MAINTENANCE@v2', 'days': 3,
                         'direction': 'above', 'claim_ids': ['inst']}]
    return item


def accept(item):
    result = M.record_ai_judgments([item])[0]
    assert result['status'] == 'accepted', result
    judgment = J.load_judgment(result['judgment_id'])
    return judgment, judgment['premise_applications'][0]


def freeze_now():
    return CD.build_cases(C.today().isoformat(), C.now().isoformat())


def score(monkeypatch, calendar, dates, changes=None):
    provider = price_provider(calendar, changes)
    for stamp in dates:
        at(monkeypatch, stamp)
        E.run_evaluation(calendar=calendar, provider=provider,
                         phase='prep' if 'T08:' in stamp else 'close')


def test_actual_briefing_judgment_case_has_origin_metadata_and_signed_features(monkeypatch):
    text, receipt = traced_delivery(monkeypatch, selection_regime='하락')
    regime('횡보')  # Change after delivery: these are three distinct contexts.
    judgment, app = accept(premise_item(text))
    context = CD.extract_case_context(judgment, app)
    assert context['feature_eligible'], context['feature_reasons']
    assert context['features']['regime'] == '횡보'
    assert context['decision_regime_snapshot']['classification_status'] == 'ai_classification_unverified'
    assert context['selection_regime'] == {'value': '하락', 'source': 'fixture-selection'}
    assert context['briefing_regime'] is None
    assert context['market_rule_hint'] == '상승'
    assert context['features']['gap'] == pytest.approx(.02)
    denominator = context['feature_provenance']['denominator']['value']
    assert context['features']['institution_ratio'] == -4_000_000_000 / denominator
    assert context['stratum']['flow_period'] == {'foreign_sessions': 2, 'institution_sessions': 2}
    assert context['stratum']['price_kind'] == 'daily_close'
    assert context['case_context_version'] == 'CASE_CONTEXT@v1'
    assert context['comparison_context']['delivery_id'] == receipt['delivery_id']
    # Neither later regime nor mutable briefing can replace original features.
    regime('하락')
    C.write_json(C.BRIEFING, brief([row(code='000001', close=1)]))
    assert CD.extract_case_context(judgment, app) == context
    dataset = freeze_now()
    assert len(dataset['cases']) == 1 and dataset['cases'][0]['features'] == context['features']
    assert dataset['cases'][0]['evaluation_status'] == 'unscored'


def test_no_registered_regime_is_unknown_and_cannot_come_from_confidence(monkeypatch):
    text, _ = traced_delivery(monkeypatch)
    judgment, app = accept(premise_item(text))
    assert judgment['confidence_at_submission']['details']
    context = CD.extract_case_context(judgment, app)
    assert context['features']['regime'] is None and not context['feature_eligible']
    regime()
    assert CD.extract_case_context(judgment, app)['features']['regime'] is None


def test_explicit_context_rejects_other_instrument_and_resolves_ambiguous_deliveries(monkeypatch):
    text, receipt = traced_delivery(monkeypatch, [row(code='000001'), row(code='000002')])
    item = premise_item(text, 'wrong-context')
    other = next(x for x in receipt['evidence_catalog'] if x['subject'] == '000002' and x.get('observation_id'))
    item['comparison_context'] = {'delivery_id': receipt['delivery_id'], 'observation_id': other['observation_id']}
    bad = M.record_ai_judgments([item])[0]
    assert bad['status'] == 'rejected' and bad['errors'][0]['code'] == 'INVALID_COMPARISON_CONTEXT'
    # A second original selection is a different observation of the same code.
    selection = O.SelectionTrace('prep', C.params(), '상승', 'later-selection')
    rows = D.screen([row(code='000001', close=20600)], C.params(), True, '상승', trace=selection)
    selection.attach(rows)
    b = brief(rows); selection.finish(b); C.write_json(C.BRIEFING, b)
    text2 = M.get_briefing()
    did2 = text2.split('브리핑 전달 ID: ')[-1].splitlines()[0]
    item = premise_item(text, 'ambiguous')
    item['delivery_ids'].append(did2)
    judgment, app = accept(item)
    assert judgment['comparison_snapshot']['reason'] == 'ambiguous_observation'
    assert CD.extract_case_context(judgment, app)['features']['gap'] is None
    item['request_key'] = 'explicit'
    own = next(x for x in receipt['evidence_catalog'] if x['subject'] == '000001' and x['field'] == 'ma20')
    item['comparison_context'] = {'delivery_id': receipt['delivery_id'], 'observation_id': own['observation_id']}
    judgment, app = accept(item)
    assert CD.extract_case_context(judgment, app)['features']['gap'] == pytest.approx(.02)


def test_reuse_three_times_and_multiple_evaluations_remain_one_origin_case(monkeypatch, calendar):
    regime('횡보')
    _, _, app, jid, _ = accepted(monkeypatch, place=False)
    origin = J.load_judgment(jid)
    for n in range(2):
        regime('하락')
        item = submission(M.get_briefing(), key='reuse-' + str(n))
        item['premises'] = [{'application_id': app['application_id']}]
        accept(item)
    score(monkeypatch, calendar, ['2026-11-09T16:00:00+09:00', '2026-11-10T16:00:00+09:00',
                                  '2026-11-11T08:30:00+09:00'])
    early = freeze_now()['cases'][0]
    assert early['verdict'] == 'refuted' and not early['outcome_eligible']
    assert CD.compare_cases([early])['summary']['states'] == {'early_refuted': 1}
    score(monkeypatch, calendar, ['2026-11-11T16:00:00+09:00', '2026-11-12T08:30:00+09:00'])
    dataset = freeze_now()
    assert len(dataset['cases']) == 1
    case = dataset['cases'][0]
    assert len(case['judgment_ids']) == 3 and len(case['evaluation_history']) >= 4
    assert case['outcome_eligible'] and case['model_eligible'] and case['label'] == 1
    assert case['features']['regime'] == '횡보'
    assert case['origin_judgment_id'] == origin['judgment_id']
    assert case['adoption']['claims'][0]['adoption'] == 'reference'
    assert not case['adoption']['claims'][0]['directly_adopted_for_premise']
    # Friday's cut-off is stale explicitly; no inferred extension to the period.
    at(monkeypatch, '2026-11-14T10:00:00+09:00')
    frozen = CD.build_cases('2026-11-13', C.now().isoformat())['cases'][0]
    assert frozen['stale_evaluation'] and frozen['selected_market_as_of'] == '2026-11-11'


def evaluation(identifier, market='2026-11-11', known='2026-11-12T08:30:00+09:00', **extra):
    return {'evaluation_id': identifier, 'application_id': 'app-one', 'market_as_of': market,
            'knowledge_as_of': known, 'created_at': known, 'verdict': 'maintained',
            'period_status': 'ended', 'data_status': 'complete', 'recording_status': 'recorded',
            'window_snapshot': {'sessions': ['2026-11-09', '2026-11-10', '2026-11-11']},
            'supersedes': [], 'newer_revision_ids': [], **extra}


def test_pure_projection_gates_actual_creation_and_preserves_conflicts():
    old = evaluation('e-old')
    future = evaluation('e-new', created_at='2026-11-19T08:30:00+09:00',
                        verdict='refuted', supersedes=['e-old'])
    case = {'application_id': 'app-one', 'feature_eligible': True, 'evaluation_history': [old, future]}
    before = CD.project_case(case, '2026-11-13', '2026-11-14T10:00:00+09:00')
    assert before['label'] == 0 and before['evaluation_id'] == 'e-old'
    after = CD.project_case(case, '2026-11-20', '2026-11-21T10:00:00+09:00')
    assert after['label'] == 1 and after['label_available_at'] == future['created_at']
    competing = evaluation('e-branch', verdict='refuted')
    case['evaluation_history'] = [old, competing]
    conflict = CD.project_case(case, '2026-11-13', '2026-11-14T10:00:00+09:00')
    assert conflict['evaluation_status'] == 'conflict' and conflict['label'] is None
    assert conflict['conflicting_evaluation_ids'] == ['e-branch', 'e-old']


def test_unfinished_original_scoring_commit_is_not_a_completed_label():
    row = evaluation('saved-before-terminal', scoring_job_id='job-one',
                     created_at='2026-11-12T08:30:02+09:00')
    case = {'application_id': 'app-one', 'feature_eligible': True, 'evaluation_history': [row],
            'scoring_history': [{'scoring_job_id': 'job-one', 'market_as_of': '2026-11-11',
                                 'available_at': '2026-11-12T08:30:00+09:00',
                                 'recording_status': 'recording_error', 'reason': 'unfinished_scoring_job'}]}
    result = CD.project_case(case, '2026-11-13', '2026-11-14T10:00:00+09:00')
    assert result['recording_status'] == 'recording_error' and result['label'] is None
    assert result['evaluation_id'] == row['evaluation_id']  # visible, but incomplete publication


def test_build_hides_future_revisions_and_missing_premise_stays_as_failed_target(monkeypatch, calendar):
    _, _, app, jid, _ = accepted(monkeypatch, place=False)
    score(monkeypatch, calendar, ['2026-11-09T16:00:00+09:00'])
    actual = SS.records('evaluations')['records'][0]
    later = {**clone(actual), 'evaluation_id': 'future-evaluation',
             'created_at': '2026-11-19T08:30:00+09:00', 'supersedes': [actual['evaluation_id']]}
    SS.archive('evaluations', later['evaluation_id'], later)
    at(monkeypatch, '2026-11-14T10:00:00+09:00')
    dataset = CD.build_cases('2026-11-13', C.now().isoformat())
    assert all(x['evaluation_id'] != 'future-evaluation' for x in dataset['cases'][0]['evaluation_history'])
    assert not any(x.get('id') == 'future-evaluation' for x in dataset['source_manifest'])
    J._path('premises', app['application_id']).write_text('{broken')
    dataset = freeze_now()
    assert dataset['target_set_incomplete'] and len(dataset['cases']) == 1
    assert dataset['cases'][0]['application_id'] == app['application_id']
    assert dataset['cases'][0]['recording_status'] == 'recording_error'


def test_corrupt_unscored_accepted_target_visible_but_orphan_not_counted(monkeypatch):
    _, _, app, _, _ = accepted(monkeypatch, place=False)
    J._path('premises', app['application_id']).write_text('{broken')
    J._archive('premises', 'orphan-application', {'application_id': 'orphan-application'})
    dataset = freeze_now()
    assert dataset['target_set_incomplete']
    assert [c['application_id'] for c in dataset['cases']] == [app['application_id']]
    assert dataset['cases'][0]['recording_status'] == 'recording_error'


def test_missing_vs_actual_zero_flow_and_period_dates_not_model_strata(monkeypatch):
    text, _ = traced_delivery(monkeypatch, [row(code='000001', inst_net=0)])
    regime()
    item = premise_item(text); item['arguments'][0]['assertion']['direction'] = 'zero'
    judgment, app = accept(item)
    actual_zero = CD.extract_case_context(judgment, app)
    assert actual_zero['signed_flows']['institution_direction'] == 'zero'
    assert actual_zero['features']['institution_ratio'] == 0
    # Unit-level receipt conversion: no current source can fill an unknown value.
    modified = clone(judgment)
    modified['comparison_snapshot']['status'] = 'unknown'
    modified['comparison_snapshot']['reason'] = 'ambiguous_observation'
    modified['comparison_snapshot']['catalog'] = []
    missing = CD.extract_case_context(modified, app)
    assert missing['features']['institution_ratio'] is None
    assert missing['signed_flows']['institution_direction'] == 'unknown'
    assert '2026-11-04' not in json.dumps(actual_zero['stratum'])


def test_prediction_hook_once_and_failure_cannot_change_judgment_coverage(monkeypatch):
    import case_prediction
    text, _ = traced_delivery(monkeypatch)
    calls = []
    def fail(judgment):
        calls.append(judgment['judgment_id'])
        raise OSError('shadow forecast disk unavailable')
    monkeypatch.setattr(case_prediction, 'record_new_premises', fail)
    item = premise_item(text)
    judgment, app = accept(item)
    accepted_again = M.record_ai_judgments([item])[0]
    assert accepted_again['status'] == 'accepted' and accepted_again['idempotent']
    assert calls == [judgment['judgment_id']]
    assert JC.get_coverage()['recording_status'] == 'recorded'
    assert JC.get_coverage()['counts']['accepted_unlinked'] == 1
    assert CS.errors()['records'][0]['operation'] == 'new_premise_prediction'


def test_actual_corrected_daily_window_keeps_one_case_and_past_projection(monkeypatch, calendar):
    regime()
    _, _, app, _, _ = accepted(monkeypatch, place=False)
    for n in range(2):
        item = submission(M.get_briefing(), key='maintain-' + str(n))
        item['premises'] = [{'application_id': app['application_id']}]
        accept(item)
    score(monkeypatch, calendar, ['2026-11-11T16:00:00+09:00', '2026-11-12T08:30:00+09:00'])
    original_cutoff = C.now().isoformat()
    original = CD.build_cases('2026-11-11', original_cutoff)['cases'][0]
    assert original['label'] == 1
    corrected = price_provider(calendar, {'2026-11-10': 20700})
    for stamp in ('2026-11-12T16:00:00+09:00', '2026-11-13T08:30:00+09:00'):
        at(monkeypatch, stamp)
        E.run_evaluation('2026-11-11', calendar=calendar, provider=corrected, phase='manual')
    dataset = CD.build_cases('2026-11-11', C.now().isoformat())
    assert len(dataset['cases']) == 1
    latest = dataset['cases'][0]
    assert latest['label'] == 0 and latest['evaluation_id'] != original['evaluation_id']
    assert len(latest['judgment_ids']) == 3
    assert latest['evaluation']['supersedes']
    past = CD.project_case(latest, '2026-11-11', original_cutoff)
    assert past['label'] == 1 and past['evaluation_id'] == original['evaluation_id']
    assert CD.compare_cases([latest], calendar)['summary']['case_count'] == 1


def test_origin_fact_adoption_is_not_inferred_from_display_or_hypothesis(monkeypatch):
    text, receipt = traced_delivery(monkeypatch)
    regime()
    item = premise_item(text)
    def claim(identifier, field, assertion, adoption):
        entry = next(e for e in receipt['evidence_catalog'] if e['subject'] == '000001' and e['field'] == field)
        return {'id': identifier, 'kind': 'fact_claim', 'proposer': 'bull', 'stance': 'support',
                'adoption': adoption, 'evidence': {'delivery_id': receipt['delivery_id'], 'evidence_id': entry['evidence_id']},
                'measurement_kind': entry['kind'], 'assertion': assertion}
    item['arguments'].extend([
        claim('above', 'price_comparison.above', {'op': 'value', 'value': True, 'unit': 'bool'}, 'adopted'),
        claim('foreign', 'foreign', {'op': 'sign', 'direction': 'buy'}, 'reference'),
        {'id': 'forecast', 'kind': 'hypothesis', 'proposer': 'bull', 'stance': 'support',
         'adoption': 'adopted', 'text': '수급이 가격을 지지할 수 있다', 'claim_ids': ['foreign']}])
    item['premises'][0]['claim_ids'] = ['above', 'forecast']
    judgment, app = accept(item)
    adoption = CD.extract_case_context(judgment, app)['adoption']
    claims = {x['claim_id']: x for x in adoption['claims']}
    assert claims['above']['directly_adopted_for_premise']
    assert claims['foreign']['adoption'] == 'reference'
    assert claims['foreign']['supports_adopted_hypothesis']
    assert not claims['foreign']['directly_adopted_for_premise']
    assert not adoption['explicitly_uses_above_ma20_and_foreign_buy']
    assert adoption['unmentioned_evidence']


def test_pairing_outcome_blind_and_no_ordinary_weekday_fallback(calendar):
    def case(identifier, side, label, gap=.01, day='2026-11-06'):
        return {'application_id': identifier, 'code': identifier, 'judgment_date': day,
                'stratum': {'days': 3}, 'stratum_id': 'one', 'feature_eligible': True,
                'features': {'gap': gap, 'slope': .005, 'regime': '상승'},
                'signed_flows': {'foreign_direction': 'buy', 'institution_direction': side},
                'outcome_eligible': True, 'label': label, 'verdict': 'refuted' if label else 'maintained',
                'recording_status': 'recorded'}
    rows = [case('sell-a', 'sell', 1), case('buy-b', 'buy', 1), case('buy-a', 'buy', 0)]
    initial = CD.compare_cases(rows, calendar)
    assert initial['pairs'][0]['buy_case_id'] == 'buy-a'
    for r in rows: r['label'] = 1-r['label']
    assert CD.compare_cases(rows, calendar)['pairs'] == initial['pairs']
    assert CD.compare_cases(rows)['pairs'] == []
    assert CD.compare_cases(rows)['pairing_status'] == 'calendar_unavailable'
    # Pairing does not add unapproved foreign-buy or above-MA20 filters.
    for r in rows:
        r['features']['gap'] = -.01
        r['signed_flows']['foreign_direction'] = 'sell'
    assert CD.compare_cases(rows, calendar)['pairs'][0]['buy_case_id'] == 'buy-a'


def test_reference_return_requires_original_verified_order_and_judgment():
    journal = [{'order_attempt_id': 'ok-order', 'judgment_id': 'origin'},
               {'order_attempt_id': 'unlinked', 'judgment_id': 'origin'},
               {'order_attempt_id': 'ok-order', 'judgment_id': 'different'}]
    context = {'captured_at': '2026-11-12T08:30:00+09:00', 'market_as_of': '2026-11-11',
               'recording_status': 'recorded', 'inputs': {'journal': journal},
               'snapshot_role': 'current_snapshot_reference',
               'audit': {'evaluations': [{'journal_source_index': i, 'return_unrounded': .1+i} for i in range(3)]}}
    execution = {'orders': [{'order_attempt_id': 'ok-order', 'financial_attribution': True, 'link_status': 'linked'},
                            {'order_attempt_id': 'unlinked', 'financial_attribution': False, 'link_status': 'unlinked'}]}
    result = CD._reference_returns([context], execution, {'origin'}, CD._time('2026-11-14T10:00:00+09:00'), '2026-11-13')
    assert len(result) == 1 and result[0]['reference_return'] == .1
    assert result[0]['net_pnl_verified'] is False


def test_failed_regime_archive_freezes_unknown_without_coverage_failure(monkeypatch):
    text, _ = traced_delivery(monkeypatch)
    monkeypatch.setattr(CX, 'record_regime', lambda *_: (_ for _ in ()).throw(OSError('local disk failure')))
    assert M.set_regime('상승', 'fixture')['regime_recording_status'] == 'recording_error'
    judgment, _ = accept(premise_item(text))
    assert judgment['decision_regime_snapshot']['reason'] == 'regime_event_unrecorded'
    assert JC.get_coverage()['recording_status'] == 'recorded'
