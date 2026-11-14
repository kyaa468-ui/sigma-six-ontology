"""Generate the isolated Sigma Six design. Optional --workbench validates/exports with its actual compiler."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def prop(name, typ='double'):
    return {'id': name, 'name': name, 'type': typ}

def node(id, label, fields):
    return {'id': id, 'label': label, 'status': 'reviewed', 'primaryKey': 'id', 'titleKey': 'name',
            'properties': [prop('id', 'string'), prop('name', 'string')] + [prop(*f) if isinstance(f, tuple) else prop(f) for f in fields]}

def literal(v): return {'op': 'literal', 'value': v}
def field(v): return {'op': 'property', 'property': v}
def binary(op, a, b): return {'op': op, 'left': a, 'right': b}
def positive(v): return {'op': 'if', 'condition': binary('gt', field(v), literal(0)), 'then': field(v), 'else': literal(0)}

NODES = [
    node('instrument', '종목', [('code','string'), ('market','string')]),
    node('run', '판단 회차', [('kind','string'), ('created_at','timestamp'), ('snapshot','object')]),
    node('observation', '회차별 종목 관측', ['price','ma5','ma20','ma20_slope5','previous_close','previous_ma20',
         'foreign','institution','trading_value_20d',('measurements','object')]),
    node('criterion', '판단 기준', [('meaning','string')]),
    node('criterion_version', '기준 버전', [('version','string'), ('definition','object')]),
    node('criterion_evaluation', '기준 평가', [('status','string'), ('inputs','object'), ('result','object')]),
    node('rule_version', '판정 규칙 버전', [('version','string'), ('definition','object')]),
    node('rule_evaluation', '규칙 평가', [('use','string'), ('status','string'), ('inputs','object')]),
    node('delivery', '브리핑 전달 기록', [('ai_run_id','string'), ('run_attempt_id','string'), ('text','string'), ('sha256','string'), ('evidence_catalog','object'), ('wire_format','string'), ('bundle_alias','string'), ('wire_scope','object')]),
    node('evidence_wire', 'AI 표시 자료와 짧은 참조', [('wire_format','string'), ('renderer_version','string'), ('field_dictionary_version','string'), ('scope','object'), ('bundle_alias','string'), ('canonical_receipt_ref','object'), ('visible_payload_ref','object'), ('visible_payload_hash','string'), ('alias_map_hash','string'), ('entries','object'), ('status','string')]),
    node('ai_judgment', 'AI 판단', [('code','string'), ('confidence_for','string'), 'confidence_raw', ('recommended_action','string'), ('decision_validation','string'), ('comparison_snapshot','object'), ('decision_regime_snapshot','object')]),
    node('argument', 'AI 주장', [('kind','string'), ('assertion','object'), ('reference_validation','string'), ('fact_validation','string'), ('hypothesis_status','string'), ('adoption','string')]),
    node('premise_definition', '전제 정의', [('version','string'), ('definition','object')]),
    node('premise_application', '종목별 전제 적용', [('code','string'), ('judgment_date','date'), ('direction','string'), 'days', ('status','string')]),
    node('premise_proposal', '자유 전제 제안', [('proposal','object'), ('status','string')]),
    node('run_attempt', 'AI 실행 시도', [('run_id','string'), ('kind','string'), ('initial_targets','object'), ('target_set_incomplete','boolean')]),
    node('submission', '판단 제출 시도', [('status','string'), ('request_hash','string'), ('errors','object')]),
    node('execution_event', '실행과 판단 연결', [('stage','string'), ('link','object'), ('outcome','object'), ('boost','object')]),
    node('order_plan', '주문 계획', [('inputs','object'), ('plan','object'), ('definition','object')]),
    node('order_attempt', '주문 전송 시도', [('account_scope','string'), ('payload_hash','string'), ('state','string'), ('result','object')]),
    node('execution_observation', '주문별 체결 관측', [('confirmation','string'), ('source','string'), ('observed_at','timestamp'), ('available_at','timestamp'), 'qty', 'price']),
    node('position_episode', '보유 구간', [('code','string'), ('origin','string'), ('opened_at','timestamp'), ('closed_at','timestamp')]),
    node('rule_trigger', '규칙 매도 계기', [('code','string'), ('retry_key','string'), ('inputs','object'), ('result','object')]),
    node('scoring_job', '사후 평가 회차', [('market_as_of','date'), ('knowledge_as_of','timestamp'), ('expected_targets','object'), ('run_status','string'), ('recording_status','string')]),
    node('calendar_snapshot', '거래일 달력 버전', [('source','string'), ('version','string'), ('sessions','object'), ('hash','string')]),
    node('premise_window', '전제 거래일 구간', [('start_trading_day','date'), ('end_trading_day','date'), ('sessions','object'), ('status','string')]),
    node('daily_receipt', '일봉 수신 자료', [('code','string'), ('available_at','timestamp'), ('source','string'), ('price_basis','string'), ('rows','object')]),
    node('daily_confirmation', '일봉 정책상 완료 확인', [('date','date'), ('status','string'), ('provider_finality','string'), ('confirmation_receipt_ids','object')]),
    node('premise_evaluation', '전제 사후 평가', [('verdict','string'), ('period_status','string'), ('data_status','string'), ('recording_status','string'), ('market_as_of','date'), ('knowledge_as_of','timestamp'), ('comparisons','object')]),
    node('legacy_outcome_context', '기존 결과 비교 자료', [('decision_outcomes','object'), ('legacy_trade_results','object'), ('inputs','object'), ('audit','object')]),
    node('regime_event', '서버 등록 AI 국면', [('regime','string'), ('source','string'), ('recorded_at','timestamp'), ('classification_status','string')]),
    node('case_review', '별도 사례 비교 실행', [('market_as_of','date'), ('knowledge_as_of','timestamp'), ('analysis_status','string'), ('ai_status','string'), ('definition','object'), ('source_manifest','object')]),
    node('review_case', '전제 적용별 비교 사례', [('origin_judgment_id','string'), ('stratum','object'), ('features','object'), ('outcome_eligible','boolean'), ('exclusions','object')]),
    node('research_model', '반증 예측 모형', [('version','string'), ('status','string'), ('training_cutoff','timestamp'), ('transform','object'), ('coefficients','object'), ('uncertainty','object')]),
    node('model_publication', '주간 모형 공개', [('available_at','timestamp'), ('valid_from','timestamp'), ('valid_until','timestamp'), ('commit','object')]),
    node('premise_prediction', '전제 사전 예측', [('kind','string'), ('origin_at','timestamp'), ('status','string'), 'probability', 'baseline_probability']),
    node('retrospective_hypothesis', 'AI 사후 가설', [('text','string'), ('hypothesis_status','string'), ('validated_findings','object')]),
    node('research_usage', '분석 사용 이력', [('purpose','string'), ('used_at','timestamp'), ('application_ids','object')]),
    node('proposal_run', '교훈·변경안 제안 실행', [('review_id','string'), ('context_hash','string'), ('base_snapshot','object'), ('submission_status','string'), ('ai_execution_status','string')]),
    node('proposal_delivery', '제안 자료 전달', [('body_hash','string'), ('catalog','object'), ('case_ids','object'), ('created_at','timestamp'), ('wire_format','string'), ('bundle_alias','string'), ('wire_scope','object')]),
    node('learning_proposal', '교훈·변경안 버전', [('kind','string'), ('version','integer'), ('target_ref','object'), ('condition','object'), ('effect','object'), ('server_cohort','object'), ('reason_status','string'), ('active','boolean')]),
    node('validation_contract', '후속 검증 입력 계약', [('validation_kind','string'), ('readiness','object'), ('base_status','string'), ('bundle_hash','string'), ('stage7_completed','boolean')]),
    node('lesson_version', '검증·채택 교훈 버전', [('proposal_id','string'), ('validation_id','string'), ('adoption_id','string'), ('effective_from','timestamp'), ('effective_until','timestamp'), ('applicability','object'), ('verification_status','string')]),
    node('runtime_input', 'AI 실행 당시 입력', [('source_hashes','object'), ('tool_contracts','object'), ('cmd_prompt','string'), ('provider_internal_state','string')]),
    node('policy_validation', '변경안 사전 검증', [('spec','object'), ('source_bundle_hash','string'), ('independence','string'), ('status','string')]),
    node('validation_opportunity', '검증 판단 기회', [('run_attempt_id','string'), ('code','string'), ('confidence_for','string'), ('gaps','object')]),
    node('validation_result', '변경안 검증 결과', [('baseline_status','string'), ('performance','object'), ('criteria','object'), ('adoption_eligibility','boolean')]),
    node('validation_holdout', '사전 예약 검증 자료', [('period','object'), ('reserved_at','timestamp'), ('released_at','timestamp'), ('status','string')]),
    node('prompt_experiment', '격리 AI 비교 실험', [('model','string'), ('trials','object'), ('budget','object'), ('status','string')]),
    node('simulation_execution', '모의 계좌 실행', [('arm','string'), ('inputs','object'), ('fills','object'), ('unconfirmed','object')]),
    node('policy_adoption', '사람의 변경안 채택', [('operator','string'), ('expected_revision','string'), ('validation_result_hash','string'), ('status','string')]),
    node('policy_package', '운영 정책 버전', [('baseline_snapshot_hash','string'), ('candidate_policy_hash','string'), ('deployed_hash','string'), ('effective_until','timestamp')]),
    node('trace_view', '고정된 근거 조회 화면', [('schema_version','string'), ('market_as_of','date'), ('knowledge_as_of','timestamp'), ('generated_at','timestamp'), ('source_manifest','object'), ('status','string')]),
    node('trace_target', '조회 판단 대상', [('run_attempt_id','string'), ('code','string'), ('confidence_for','string'), ('coverage_status','string'), ('connection_status','string'), ('visibility','string')]),
    node('trace_export', '공개 요약 준비 묶음', [('view_id','string'), ('schema_version','string'), ('source_hash','string'), ('files','object'), ('published','boolean')]),
]
EDGES = []
for src,dst,card in [('instrument','observation','1:N'),('run','observation','1:N'),('criterion','criterion_version','1:N'),
                     ('delivery','evidence_wire','1:N'),('proposal_delivery','evidence_wire','1:N'),
                     ('criterion_version','criterion_evaluation','1:N'),('observation','criterion_evaluation','1:N'),
                     ('rule_version','criterion_version','N:N'),('observation','rule_evaluation','1:N'),
                     ('rule_version','rule_evaluation','1:N'),('rule_evaluation','criterion_evaluation','N:N'),
                     ('delivery','observation','N:N'),('delivery','criterion_evaluation','N:N'),('delivery','run','N:N'),
                     ('run_attempt','delivery','1:N'),('run_attempt','submission','1:N'),('run_attempt','execution_event','1:N'),
                     ('submission','ai_judgment','N:1'),('ai_judgment','argument','1:N'),('argument','delivery','N:N'),
                     ('ai_judgment','premise_application','N:N'),('premise_definition','premise_application','1:N'),
                     ('ai_judgment','premise_proposal','1:N'),('execution_event','ai_judgment','N:1'),
                     ('ai_judgment','order_plan','1:N'),('order_plan','order_attempt','1:N'),
                     ('order_attempt','execution_event','1:N'),('order_attempt','execution_observation','1:N'),
                     ('instrument','position_episode','1:N'),('position_episode','observation','1:N'),
                     ('position_episode','ai_judgment','1:N'),('position_episode','order_plan','1:N'),
                     ('position_episode','rule_trigger','1:N'),('rule_trigger','order_attempt','1:N'),
                     ('scoring_job','premise_evaluation','N:N'),('scoring_job','legacy_outcome_context','1:1'),
                     ('premise_application','premise_window','1:N'),('calendar_snapshot','premise_window','1:N'),
                     ('premise_application','premise_evaluation','1:N'),('premise_window','premise_evaluation','1:N'),
                     ('daily_receipt','daily_confirmation','N:N'),('calendar_snapshot','daily_confirmation','1:N'),
                     ('daily_confirmation','premise_evaluation','N:N'),('premise_evaluation','order_attempt','N:N'),
                     ('ai_judgment','regime_event','N:1'),('case_review','review_case','N:N'),
                     ('review_case','premise_application','N:1'),('review_case','ai_judgment','N:1'),
                     ('review_case','delivery','N:N'),('review_case','premise_evaluation','N:1'),
                     ('review_case','order_attempt','N:N'),('case_review','research_model','1:N'),
                     ('research_model','review_case','N:N'),('research_model','model_publication','1:N'),
                     ('model_publication','premise_prediction','1:N'),('premise_application','premise_prediction','1:N'),
                     ('case_review','retrospective_hypothesis','1:N'),('retrospective_hypothesis','review_case','N:N'),
                     ('case_review','research_usage','1:1'),('research_usage','premise_application','N:N'),
                     ('case_review','proposal_run','1:N'),('proposal_run','proposal_delivery','1:N'),
                     ('proposal_run','learning_proposal','1:N'),('proposal_delivery','review_case','N:N'),
                     ('learning_proposal','proposal_delivery','N:N'),('learning_proposal','review_case','N:N'),
                     ('learning_proposal','validation_contract','1:1'),('proposal_run','research_usage','1:N'),
                     ('learning_proposal','lesson_version','1:N'),('lesson_version','delivery','N:N'),
                     ('argument','lesson_version','N:N'),('run_attempt','runtime_input','1:1'),
                     ('learning_proposal','policy_validation','1:N'),('policy_validation','validation_opportunity','1:N'),
                     ('validation_opportunity','run_attempt','N:1'),('validation_opportunity','delivery','N:N'),
                     ('validation_opportunity','execution_event','N:N'),('policy_validation','validation_result','1:N'),
                     ('policy_validation','validation_holdout','1:N'),('validation_holdout','research_usage','1:N'),
                     ('policy_validation','prompt_experiment','1:N'),('policy_validation','simulation_execution','1:N'),
                     ('validation_result','policy_adoption','1:N'),('policy_adoption','policy_package','1:1'),
                     ('policy_package','order_plan','1:N'),('policy_package','lesson_version','1:N')]:
    EDGES.append({'id':f'{src}-{dst}','source':src,'target':dst,'label':'참조','inverse':'참조됨','cardinality':card})
EDGES.append({'id':'premise-evaluation-supersedes','source':'premise_evaluation','target':'premise_evaluation',
              'label':'이전 평가를 대체','inverse':'이후 평가가 대체','cardinality':'N:N'})
EDGES.append({'id':'learning-proposal-supersedes','source':'learning_proposal','target':'learning_proposal',
              'label':'명시적으로 수정','inverse':'후속 수정','cardinality':'N:1'})
for src,dst,card in [('trace_view','trace_target','1:N'),('trace_target','run_attempt','N:1'),
                     ('trace_target','ai_judgment','N:N'),('trace_target','delivery','N:N'),
                     ('trace_target','order_attempt','N:N'),('trace_target','premise_application','N:N'),
                     ('trace_view','review_case','N:N'),('trace_view','learning_proposal','N:N'),
                     ('trace_view','validation_result','N:N'),('trace_view','policy_package','N:N'),
                     ('trace_view','research_usage','1:N'),('trace_view','trace_export','1:N')]:
    EDGES.append({'id':f'{src}-{dst}','source':src,'target':dst,'label':'보관된 참조','inverse':'조회에 포함','cardinality':card})
DEFS = {
 'PRICE_ABOVE_MA20': ('20일선 위', 'boolean', binary('gt',field('price'),field('ma20'))),
 'MA20_GAP': ('20일선 이격률', 'double', binary('sub',binary('div',field('price'),field('ma20')),literal(1))),
 'PRICE_ABOVE_MA5': ('5일선 위','boolean',binary('gt',field('price'),field('ma5'))),
 'MA5_ABOVE_MA20': ('5일선 > 20일선','boolean',binary('gt',field('ma5'),field('ma20'))),
 'FOREIGN_NET_BUY': ('외국인 순매수','boolean',binary('gt',field('foreign'),literal(0))),
 'INSTITUTION_NET_BUY': ('기관 순매수','boolean',binary('gt',field('institution'),literal(0))),
 'INSTITUTION_NET_SELL': ('기관 순매도','boolean',binary('lt',field('institution'),literal(0))),
 'FLOW_SUM': ('두 주체 수급 합계','double',binary('add',field('foreign'),field('institution'))),
 'POSITIVE_FLOW_RATIO': ('양수 부분만 합산한 후보용 수급 비율','double',binary('div',binary('add',positive('foreign'),positive('institution')),field('trading_value_20d'))),
 'CLOSE_CROSS_MA20': ('완료 일봉 기준 상향 돌파','boolean',binary('and',binary('lte',field('previous_close'),field('previous_ma20')),binary('gt',field('price'),field('ma20')))),
}
MODEL = {'title':'Sigma Six 후보 근거 v1','nodes':NODES,'edges':EDGES,'functions':[
    {'id':k, 'name':name, 'node':'observation','resultType':typ,'expression':expr} for k,(name,typ,expr) in DEFS.items()], 'actionTypes':[]}
BINDINGS = {'version':'1','effective_from':'on_first_execution', 'criteria':[
    {'criterion_id':k,'criterion_version_id':f'{k}@v1','function_id':k,'name':name,'meaning':name,
     'missing_policy':'unknown; engine substitutions are recorded separately'} for k,(name,_,_) in DEFS.items()],
 'rules':[
    {'rule_version_id':'TREND@v1','use':'candidate_filter','criteria':['PRICE_ABOVE_MA20','PRICE_ABOVE_MA5','MA5_ABOVE_MA20'],
     'definition':'falling_trend.regimes: price > ma20; otherwise price > ma5 > ma20 (short circuit)'},
    {'rule_version_id':'FLOW@v1','use':'candidate_filter','criteria':['FOREIGN_NET_BUY','INSTITUTION_NET_BUY'],
     'definition':'reject iff both effective amounts <= 0; prep guard login and foreign_net is not None; intraday require_flow'},
    {'rule_version_id':'RANK@v1','use':'ranking','criteria':['MA20_GAP','POSITIVE_FLOW_RATIO'],
     'definition':'legacy percentile ranks, rounding and quotas; see captured engine source and params'},
    {'rule_version_id':'ADMISSION@v1','use':'admission','criteria':[], 'definition':'legacy filter order, quotas, fill_up, dips, ETF, previous-list retention'},
    {'rule_version_id':'REFERENCE@v1','use':'briefing_reference','criteria':list(DEFS), 'definition':'does not imply AI adoption'},
 ]}

if __name__ == '__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--workbench');args=ap.parse_args()
    if args.workbench:
        sys.path.insert(0,args.workbench)
        from schema import compile_ir
        ir=compile_ir(MODEL)
    else:
        raise SystemExit('Use --workbench PATH to export with the actual workbench compiler')
    dest=ROOT/'config/ontology';dest.mkdir(parents=True,exist_ok=True)
    for name,value in [('sigma-six.ir.json',ir),('bindings.json',BINDINGS)]:
        (dest/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print('Exported isolated Sigma Six design; no workbench state modified')
