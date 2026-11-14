"""Actual-model stage rehearsal against isolated fixtures; never a broker run.

--work must be an empty/new temporary directory. MCP blocks all network calls
and orders. Only the Claude CLI process talks to its model provider.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

AGENT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(AGENT/'src'), str(AGENT/'tests')]


def fixture_environment(work):
    # Reuse the same deterministic fixtures as the engine integration tests.
    from test_ontology import C, D, M, P, R, NOW, clone, row, brief
    from test_judgment_integration import LocalBroker
    C.load_env = lambda *a, **k: {}
    for key,file in [('WORK',''),('BRIEFING','briefing.json'),('JOURNAL','journal.jsonl'),
                     ('ACCOUNT','account.json'),('MARKETS','markets.json'),('POSITIONS','positions.json'),
                     ('RUNS','runs.jsonl'),('LIVE','live'),('DOCS_DATA','docs')]:
        setattr(C,key,str(work/file))
    C.now=lambda:NOW
    C.params=lambda:clone(P)
    C.risk_limits=lambda:clone(R)
    C.market_of=lambda *a,**k:'KOSPI'
    D.live_market=lambda *a,**k:None
    M._broker=LocalBroker()
    prior=M.tool_access_error
    def access(fn,args,kwargs):
        if fn.__name__ in {'plan_order','place_order','refresh_execution_status','apply_tuning','submit_review','record_external_evidence'}:
            return {'ok':False,'error_code':'REHEARSAL_EFFECT_FORBIDDEN'}
        return prior(fn,args,kwargs)
    M.tool_access_error=access
    import socket
    def forbidden(*a,**k): raise RuntimeError('REHEARSAL_NETWORK_FORBIDDEN')
    socket.socket.connect=forbidden
    socket.create_connection=forbidden
    return C,M,row,brief


def run(work, budget):
    if work.exists(): raise ValueError('Use a new directory to preserve earlier attempts')
    work.mkdir(parents=True)
    C,M,row,brief=fixture_environment(work)
    import judgment_coverage as JC
    import semantic_layer as S
    import semantic_runner as SR
    examples=[]
    for code,amount in [('000001',3_000_000_000),('000002',-4_000_000_000),('000003',None)]:
        sample=row(code=code,inst_net=amount)
        sample['_sources']['flow']['institution'].update(value=amount,status='unknown' if amount is None else 'observed')
        if amount is None:
            # The engine may require numeric ranking fields; the observation
            # explicitly remains unknown while its arithmetic fallback is zero.
            sample['inst_net']=0
        examples.append(sample)
    b=brief(examples)
    C.write_json(C.BRIEFING,b);C.write_json(C.ACCOUNT,M._broker.account_value)
    attempt=JC.start_attempt('semantic-model-rehearsal','view',b,M._broker.account_value)
    report={'kind':'actual_model_frozen_fixture_rehearsal','attempt':attempt,'max_budget_usd':budget,
            'order_tools':'blocked','market_network':'blocked','calls':[],
            'scenarios':['positive_flow','negative_flow_counterargument','missing_flow'],
            'status':'incomplete','limitations':['synthetic data','no investment-quality ground truth','no trading or deployment']}
    original='고정 가상 자료의 판단 갱신 시험입니다. 주문 없이 관망/보유 기록만 합니다. 000001은 양수 수급, 000002는 음수 수급, 000003은 결측 수급 사례입니다. 이 설명은 정답이 아니며 실제 전달값·상태를 확인하세요.'
    total=0.0
    try:
        for role in S.ROLES:
            remaining=budget-total
            if remaining<=0: raise RuntimeError('REHEARSAL_BUDGET_EXHAUSTED')
            env={**os.environ,'MODE':'mock','RUN_MODE':'view','RUN_ID':attempt['run_id'],
                 'RUN_ATTEMPT_ID':attempt['run_attempt_id'],'SION_SEMANTIC_ROLE':role,
                 'SION_SEMANTIC_MODE':'strict','SION_EVIDENCE_WIRE_FORMAT':'COMPACT_EVIDENCE@v1'}
            config={'mcpServers':{'kiwoom':{'command':sys.executable,
                    'args':[str(Path(__file__).resolve()),'--serve','--work',str(work)],
                    'env':{k:env[k] for k in ('MODE','RUN_MODE','RUN_ID','RUN_ATTEMPT_ID','SION_SEMANTIC_ROLE','SION_SEMANTIC_MODE','SION_EVIDENCE_WIRE_FORMAT')}}}}
            cfg=work/(role+'-mcp.json');cfg.write_text(json.dumps(config))
            allowed=[x for x in SR.tools_for(role,'view') if x!='WebSearch' and not any(n in x for n in ('record_external_evidence','refresh_execution_status'))]
            prompt=SR.role_prompt(role,original)
            prompt+='\n테스트 환경에서는 외부 검색·조회·주문이 금지됩니다. data의 모든 refs는 받은 claim/path ID입니다. fact_claim에는 기존 구조화 assertion만 쓰고 자유 설명은 hypothesis로 분리하세요. 선행 입력의 raw_arguments와 wire_declarations는 이미 검증된 자료이므로 중복 제출하지 않습니다.'
            cmd=['claude','-p',prompt,'--output-format','json','--no-session-persistence',
                 '--setting-sources','','--strict-mcp-config','--mcp-config',str(cfg),
                 '--tools','','--allowedTools',*allowed,'--permission-mode','dontAsk',
                 '--max-budget-usd',str(remaining)]
            start=time.monotonic()
            with (work/(role+'.stdout.json')).open('w') as out,(work/(role+'.stderr.txt')).open('w') as err:
                process=subprocess.run(cmd,cwd=work,env=env,stdout=out,stderr=err,timeout=360)
            raw=json.loads((work/(role+'.stdout.json')).read_text())
            cost=raw.get('total_cost_usd')
            call={'role':role,'returncode':process.returncode,'seconds':round(time.monotonic()-start,2),
                  'cost_usd':cost,'usage':raw.get('usage'),'model_usage':raw.get('modelUsage'),
                  'permission_denials':raw.get('permission_denials'),'is_error':raw.get('is_error')}
            report['calls'].append(call)
            if not isinstance(cost,(int,float)):raise RuntimeError('MODEL_COST_UNAVAILABLE')
            total+=cost
            print(json.dumps({k:v for k,v in call.items() if k not in ('usage','model_usage')},ensure_ascii=False),flush=True)
            if process.returncode or raw.get('is_error'):raise RuntimeError('MODEL_CALL_FAILED')
            with SR.identity(attempt,'view',role):
                missing=[t for t in S.targets() if not S._stage(S._chain_id(t['code'],t['confidence_for']),role)]
                if missing:raise RuntimeError('MISSING_ACCEPTED_STAGE: '+json.dumps(missing))
        report['status']='passed_explicit_stage_execution_only'
        with SR.identity(attempt,'view','lead'):
            report['traces']=[S.trace(t['code'],t['confidence_for']) for t in S.targets()]
    except Exception as exc:
        report['error']=str(exc)
        print(str(exc),flush=True)
    finally:
        report['total_cost_usd']=total
        (work/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n')
    return report['status'].startswith('passed')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--budget-usd',type=float,default=5)
    args=parser.parse_args()
    if args.serve:
        _,M,_,_=fixture_environment(args.work)
        M.mcp.run()
    else:
        raise SystemExit(0 if run(args.work,args.budget_usd) else 1)
