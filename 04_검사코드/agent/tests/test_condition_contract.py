import math

import pytest

from test_ontology import isolated
import condition_contract as Q


def comparison(field='features.gap', value=0, unit='ratio', op='gt'):
    return {'op':op,'left':{'field':field},'right':{'value':value,'unit':unit}}


def test_shared_arithmetic_typed_unknown_and_nested_inputs():
    expr = comparison()
    expr['left']={'op':'div','left':{'field':'signed_flows.institution'},
                  'right':{'field':'signed_flows.foreign'}}
    assert Q.validate_condition(expr)=='bool'
    assert Q.evaluate_condition(expr,{'signed_flows':{'institution':-40,'foreign':30}}) is False
    assert Q.evaluate_condition(expr,{'signed_flows.foreign':0,'signed_flows.institution':0}) is None
    assert Q.evaluate_condition(expr,{'signed_flows.foreign':30}) is None
    assert Q.evaluate_condition(expr,{'signed_flows.foreign':math.inf,'signed_flows.institution':0}) is None


@pytest.mark.parametrize('op,known,expected',[('and',False,False),('and',True,None),('or',False,None),('or',True,True)])
def test_three_valued_logic(op,known,expected):
    expr={'op':op,'left':{'value':known,'unit':'bool'},'right':comparison()}
    assert Q.evaluate_condition(expr,{}) is expected


def test_all_branches_validated_and_no_future_adoption_at_briefing():
    invalid={'op':'or','left':{'value':True,'unit':'bool'},'right':comparison('result.label')}
    with pytest.raises(ValueError,match='FORBIDDEN'): Q.evaluate_condition(invalid,{})
    adopted=comparison('adoption.explicitly_uses_above_ma20_and_foreign_buy',True,'bool','eq')
    with pytest.raises(ValueError,match='UNAVAILABLE'): Q.validate_condition(adopted,stage='briefing')
    assert Q.evaluate_condition(adopted,{},stage='after_judgment') is None
    with pytest.raises(ValueError): Q.validate_condition(comparison(unit='KRW'))
    with pytest.raises(ValueError): Q.validate_condition(comparison(value=True))


def test_runtime_contract_preserves_distinct_weekday_and_exchange_sessions():
    expr={'op':'eq','left':{'value':3,'unit':'weekday_count'},'right':{'value':3,'unit':'trading_days'}}
    with pytest.raises(ValueError,match='UNIT'): Q.validate_condition(expr)
