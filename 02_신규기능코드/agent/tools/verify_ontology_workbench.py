"""Run synthetic cases through the actual workbench evaluator, without reading/writing its business state."""
import argparse
import copy
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--workbench',required=True);args=ap.parse_args()
    sys.path.insert(0,args.workbench)
    from schema import normalize,validate
    from runtime import empty,owner,validate_state
    from decision_evals import run_suite
    model=normalize(json.loads((ROOT/'config/ontology/sigma-six.ir.json').read_text())['ontology'])
    validate(model)
    state=empty()
    state['records']=[{'id':'fixture-A','node':'observation','values':{'id':'A','name':'가상 A','price':20400,'ma5':20200,'ma20':20000,
                    'foreign':3000000000,'institution':-4500000000,'trading_value_20d':10000000000,'previous_close':19800,'previous_ma20':19900}}]
    validate_state(model,state)
    before=copy.deepcopy(state)
    cases=[('PRICE_ABOVE_MA20',True,[]),('MA20_GAP',.02,[]),('FLOW_SUM',-1500000000,[]),
           ('POSITIVE_FLOW_RATIO',.3,[]),('INSTITUTION_NET_SELL',True,[]),
           ('PRICE_ABOVE_MA20',False,[{'record':'fixture-A','property':'price','value':20000}]),
           ('CLOSE_CROSS_MA20',True,[])]
    results=[]
    for i,(fn,want,overrides) in enumerate(cases):
        suite={'id':f'sigma-{i}','name':f'가상 비교 {i}','record':'fixture-A','function':fn,
               'cases':[{'id':f'case-{i}','name':fn,'reason':'로컬 가상 입력으로 기준 의미 확인','overrides':overrides,
                         'expected':{'status':'value','value':want,**({'tolerance':1e-12} if type(want) is float else {})}}]}
        result=run_suite(model,state,owner(),suite)
        if result['passed']!=result['total']: raise AssertionError(result['rows'])
        results.append(result)
    assert state==before
    dest=ROOT/'tests/fixtures/ontology/workbench-verification.json'
    dest.write_text(json.dumps({'mode':'isolated','business_state_changed':False,'runs':results},ensure_ascii=False,indent=2)+'\n')
    print(f'{len(results)} workbench cases passed; business state untouched')

if __name__=='__main__': main()
