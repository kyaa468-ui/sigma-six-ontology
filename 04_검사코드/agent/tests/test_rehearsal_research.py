"""The reusable local walkthrough preserves incomplete historical proof."""
import sys

from test_ontology import C, ROOT, isolated
from test_close_evaluation import accepted

sys.path.insert(0, str(ROOT / 'tools'))
from rehearsal_research import run_research


def test_research_walkthrough_uses_tools_and_does_not_invent_runtime(monkeypatch):
    fake, attempt, app, judgment_id, plan = accepted(monkeypatch)
    clock = {'now': C.now()}
    monkeypatch.setattr(C, 'now', lambda: clock['now'])
    result = run_research(monkeypatch, {'judgment_id': judgment_id, 'main_code': '000001'}, clock)
    stages = result['stages']
    assert [stages[s]['status'] for s in ('4', '5', '6')] == ['passed'] * 3
    assert [p['data_status'] == 'complete' for p in stages['4']['checkpoints']] == [False] * 4 + [True]
    assert stages['5']['ai_states'] == ['not_submitted', 'rejected', 'accepted']
    assert stages['5']['synthetic_model_exercise']['published_as_operational_model'] is False
    # accepted() intentionally predates the actual run.claude capture.  A local
    # walkthrough must retain this missing proof instead of manufacturing it.
    assert stages['7']['status'] == 'limited_by_original_inputs'
    assert any(g['reason'] == 'original_ai_runtime_inputs_unrecorded' for g in stages['7']['gaps'])
    assert stages['7']['adoption_eligibility']['status'] == 'ineligible'
    synthetic = stages['7']['synthetic_portfolio_exercise']
    assert synthetic['comparison_status'] == 'complete'
    assert synthetic['baseline_qty'] > synthetic['changed_qty']
    assert synthetic['missing_liquidity_status'] == 'incomplete'
    assert synthetic['historical_validation_records_created'] == 0
    assert len(fake.orders) == 1
