"""Execute the real browser script against a tiny DOM; actual UI smoke is separate.

These assertions catch source-field mismatches that syntax checks and raw JSON
panels miss (a known value must appear in the human-facing summary).
"""
import json
from pathlib import Path
import subprocess

SCRIPT=Path(__file__).resolve().parents[1]/'review/trace.js'


def fixture():
    target={'trace_id':'trace-one','run_attempt_id':'run-one','code':'000001','confidence_for':'매수',
            'coverage_status':'linked','connection_status':'linked','action':'매수'}
    observation={'observation_id':'obs-select','comparison_id':'compare-one',
                 'measurements':{'price':{'value':20400,'unit':'KRW','kind':'intraday_current'},
                    'ma20':{'value':20000,'unit':'KRW'},'foreign':{'value':3000000000,'unit':'KRW','kind':'intraday_estimate'},
                    'institution':{'value':-4500000000,'unit':'KRW','kind':'intraday_estimate'}},
                 'price_comparison':{'price':20400,'ma20':20000,'above':True,'gap':.02},
                 'flow_summary':{'same_period_confirmed':False,'directions_opposed':True},
                 'criteria':[{'criterion_id':'FLOW_RATIO','value':.3,'use':'candidate_filter'}]}
    judgment={'judgment_id':'judgment-one','recommended_action':'매수','confidence_for':'매수','confidence_raw':0,
              'decision':'유지 조건을 판단한다','decision_regime_snapshot':{'value':'상승'},
              'arguments':[{'kind':'fact_claim','fact_validation':'passed','server_summary':'기관 순매도 45억 원',
                            'proposer':'bear','adoption':'adopted'},
                           {'kind':'hypothesis','text':'매도 압력을 받아낼 것이다','adoption':'reference','proposer':'bull'}]}
    evaluation={'evaluation_id':'evaluation-one','application_id':'app-one','market_as_of':'2026-11-11',
                'verdict':'refuted','period_status':'ended','data_status':'complete',
                'comparisons':[{'date':'2026-11-10','close':'19700','ma20':'20050','status':'refuted','confirmation':'policy_complete'}]}
    case={'application_id':'app-one','code':'000001','trace_ids':['trace-one'],'evaluation':evaluation,
          'market_as_of':'2026-11-13','selected_market_as_of':'2026-11-11','stale_evaluation':True,'outcome_eligible':True,'verdict':'refuted'}
    trace={'target':target,'observations':[{'observation':observation}],'judgments':[judgment],
           'deliveries':[{'delivery_id':'delivery-one','quotes':[{'instrument_id':'000001','price':19800,
                'observation_id':'quote-one','kind':'intraday_current','available_at':'2026-11-07T10:31:00+09:00'}]}],
           'execution_context':{'orders':[{'order_attempt_id':'order-one','path_kind':'dispatched_order','link_status':'linked',
                 'summary':{'requested_qty':100,'confirmed_qty':40,'unconfirmed_qty':60,'confirmation_status':'confirmed_partial'}}]},
           'evaluations':[evaluation]}
    validations=[{'validation_id':'validation-one','result_id':'result-'+str(n),'result_hash':'hash-'+str(n),
                  'proposal_id':'proposal-one','status':'complete','result':{'mode':'replay','baseline_status':'passed',
                  'adoption_eligibility':{'status':'ineligible','reasons':['not_independent']}}} for n in (1,2)]
    projection={'targets':[target],'traces':{'trace-one':trace},'cases':[case],
                'proposals':[{'proposal_id':'proposal-one','title':'시험 변경안','version':2,'kind':'confidence_adjustment',
                              'condition':{'above':True},'effect':{'amount':.01},'support_case_ids':['app-one'],
                              'validation_ids':['validation-one']}], 'validations':validations,'policies':[], 'errors':[]}
    return {'status':'complete','view_id':'view-one','manifest':{},'projection':projection}


RUNNER=r'''
const fs=require('fs'),vm=require('vm'),input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(tag){this.tag=tag;this.children=[];this._text='';this.value='';this.listeners={};this.classList={toggle(){}};}
 set textContent(v){this._text=String(v);this.children=[];} get textContent(){return this._text+this.children.map(x=>x.textContent??String(x)).join('');}
 append(...values){this.children.push(...values);} replaceChildren(...values){this._text='';this.children=values;}
 setAttribute(k,v){this[k]=v;} getAttribute(k){return this[k];} addEventListener(k,fn){this.listeners[k]=fn;}
}
const ids={};const document={getElementById(id){return ids[id]??=(new Element('div'));},createElement:t=>new Element(t),createElementNS:(_,t)=>new Element(t),querySelectorAll:()=>[]};
const requests=[];const context={TextDecoder,Uint8Array,atob,Node:Element,document,location:{hash:input.route},window:{addEventListener(){}},URLSearchParams,console,
 fetch:async url=>{requests.push(url);const value=input.responses?input.responses[url.split('?')[0]]:input.snapshot;return {ok:!!value,json:async()=>value||{status:'not_found'}};}};
vm.runInNewContext(fs.readFileSync(input.script,'utf8'),context);
setImmediate(()=>{
 if(input.openRaw){const walk=n=>{if(n.tag==='details'&&n.children[0]?.textContent===input.openRaw)return n;for(const child of n.children||[]){const match=walk(child);if(match)return match;}};const detail=walk(ids.content);if(!detail)throw Error('raw details missing');detail.open=true;detail.listeners.toggle();detail.open=false;detail.listeners.toggle();detail.open=true;detail.listeners.toggle();}
 process.stdout.write(JSON.stringify({content:ids.content.textContent,tree:ids.content,requests},(k,v)=>typeof v==='function'?undefined:v));
});
'''


def render(route, snapshot=None, open_raw=None):
    result=subprocess.run(['node','-e',RUNNER],input=json.dumps({'script':str(SCRIPT),'route':route,'snapshot':snapshot or fixture(),'openRaw':open_raw}),
                          capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def test_actual_observation_claim_confidence_and_fill_keys_are_readable():
    text=render('#trace/trace-one')['content']
    for expected in ['20일선 위 · 이격률 +2.0%','compare-one','순매수 · 3,000,000,000원','순매도 · -4,500,000,000원',
                     'FLOW_RATIO0.3candidate_filter','원래 권고 / 확신도 방향매수 / 매수','AI 원점수0',
                     '판단 국면 · AI 분류상승','기관 순매도 45억 원','AI 가설 · 미검증','매도 압력을 받아낼 것이다',
                     '100 / 40 / 60','부분 확인','브리핑 호출 시 현재가 · 선별 가격과 별도 관측','19,800원','19700 / 20050']:
        assert expected in text,expected


def test_case_list_uses_selected_market_date_and_stale_marker():
    text=render('#cases')['content']
    assert '2026-11-11 · 이전 평가' in text
    assert '2026-11-13' not in text
    detail=render('#case/app-one')['content']
    assert '19700 / 20050' in detail
    assert '선택한 평가 기준일2026-11-11' in detail
    assert '요청한 화면 기준일2026-11-13' in detail


def test_proposal_links_all_exact_results_and_validation_id_requires_choice():
    proposal=render('#proposal/proposal-one')
    encoded=json.dumps(proposal['tree'],ensure_ascii=False)
    assert '#validation/result-1' in encoded and '#validation/result-2' in encoded
    assert '제목시험 변경안' in proposal['content']
    choice=render('#validation/validation-one');chooser=choice['content']
    assert '검증 결과 버전을 선택하세요' in chooser
    assert '#validation/result-1' in json.dumps(choice['tree']) and '#validation/result-2' in json.dumps(choice['tree'])
    exact=render('#validation/result-2',open_raw='기록 식별자·해시')['content']
    assert 'result-2' in exact and 'hash-2' in exact


def test_protected_trace_has_no_raw_stage_rendering():
    value=fixture();value['projection']['traces']['trace-one']={'target':value['projection']['targets'][0],'visibility':'protected'}
    text=render('#trace/trace-one',value)['content']
    assert '독립 검증용으로 보호 중' in text
    assert '실제 전달 원문' not in text


PUBLIC_RUNNER=r'''
const fs=require('fs'),vm=require('vm'),crypto=require('crypto'),input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(tag){this.tag=tag;this.children=[];this._text='';this.listeners={};this.style={};}
 set textContent(v){this._text=String(v);this.children=[];} get textContent(){return this._text+this.children.map(x=>x.textContent??String(x)).join('');}
 append(...values){this.children.push(...values);} replaceChildren(...values){this._text='';this.children=values;}
 addEventListener(k,fn){this.listeners[k]=fn;} showModal(){this.open=true;} close(){this.open=false;}
}
const body=new Element('body'),requests=[],summary=JSON.stringify(input.summary),hash=crypto.createHash('sha256').update(summary).digest('hex');
const manifest={schema_version:'TRACE_PUBLIC@v1',export_id:'export-one',view_id:'view-one',status:'complete',files:{'summary.json':input.badHash?'0'.repeat(64):hash}};
const document={body,createElement:t=>new Element(t),getElementById:()=>null};
const context={document,window:{},crypto:crypto.webcrypto,TextDecoder,Uint8Array,navigator:{clipboard:{writeText:async()=>{}}},console,
 fetch:async url=>{requests.push(url);return url.endsWith('manifest.json')?{ok:!input.missing,json:async()=>manifest}:{ok:true,arrayBuffer:async()=>new TextEncoder().encode(summary).buffer};}};
vm.runInNewContext(fs.readFileSync(input.script,'utf8'),context);
context.window.SigmaTrace.open(input.row).then(()=>process.stdout.write(JSON.stringify({content:body.textContent,requests})));
'''


def public_render(row, **extra):
    trace={'trace_id':'trace-public-one','code':'000001','coverage_status':'linked','connection_status':'linked',
           'origin_refs':[{'kind':'judgment_id','ref':'public-ref-one'}], 'server_summary':'검증된 연결 요약'}
    summary={'schema_version':'TRACE_PUBLIC@v1','export_id':'export-one','view_id':'view-one',
             'targets':[trace], 'traces':{'trace-public-one':trace}}
    payload={'script':str(SCRIPT.parents[2]/'docs/trace-summary.js'),'summary':summary,'row':row,**extra}
    result=subprocess.run(['node','-e',PUBLIC_RUNNER],input=json.dumps(payload),capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def test_public_summary_resolves_only_explicit_opaque_refs():
    good=public_render({'origin_refs':[{'kind':'judgment_id','ref':'public-ref-one'}]})
    assert '검증된 연결 요약' in good['content']
    assert all(url.startswith('data/ontology/') for url in good['requests'])
    unknown=public_render({'code':'000001','time':'2026-11-07'})
    assert '연결 근거가 보관되지 않았습니다' in unknown['content']
    assert '검증된 연결 요약' not in unknown['content']


def test_public_missing_or_corrupt_generation_never_substitutes_sample():
    broken=public_render({'trace_id':'trace-public-one'},badHash=True)
    assert '무결성 확인에 실패' in broken['content']
    assert '검증된 연결 요약' not in broken['content']
    missing=public_render(None,missing=True)
    assert '아직 게시되지 않았습니다' in missing['content']
    assert missing['requests']==['data/ontology/manifest.json']


def test_claim_compact_summary_keeps_daily_and_intraday_measurements_distinct():
    value=fixture();trace=value['projection']['traces']['trace-one']
    trace['observations'][0]['observation']['measurements']['institution']['value']=-4590000000
    claim=trace['judgments'][0]['arguments'][0]
    claim.update(server_summary='기관 순매도 (-4000000000 KRW)', measurement_kind='daily_accumulated',
        references=[{'delivery_id':'delivery-daily','evidence_id':'evidence-daily'}],
        evidence_snapshots=[{'evidence_id':'evidence-daily','observation_id':'observation-daily',
            'kind':'daily_accumulated','value':-4000000000,'unit':'KRW','source':'pykrx:trading_value',
            'period':['2026-10-30','2026-11-02','2026-11-03','2026-11-04','2026-11-05'],
            'observed_at':'2026-11-05','available_at':'2026-11-06T08:30:00+09:00'}])
    text=render('#trace/trace-one',value)['content']
    for expected in ['순매도 · -4,590,000,000원 · 장중 수량×가격 추정액',
                     '측정: 일봉 누적 금액 · 전달값 -4,000,000,000원',
                     '집계 기간: 2026-10-30 ~ 2026-11-05 (5개 관측일)',
                     '기준시각/일자: 2026-11-05','수신·가용시각: 2026-11-06T08:30:00+09:00',
                     '출처: pykrx:trading_value','관측 ID: observation-daily',
                     '근거 ID: evidence-daily','전달 ID: delivery-daily']:
        assert expected in text,expected


def test_actual_nonapplied_boosts_and_unknown_explanation_are_not_confused():
    value=fixture();trace=value['projection']['traces']['trace-one']
    detail={'raw':.7,'adjusted':.7,'note':None,'cap':1,'capped':.7,'round_digits':3,
            'adjustments':[{'rule':'rsi','amount':0,'applied':False,'reason':'condition_false'},
                           {'rule':'market','amount':0,'applied':False,'reason':'condition_false'}]}
    trace['execution_events']=[{'stage':'place_order','detail':detail}]
    trace['judgments'][0]['confidence_at_submission']={'details':detail}
    text=render('#trace/trace-one',value)['content']
    assert 'RSI: 미적용 (조건 불충족) · 보정량 0' in text
    assert '시장 국면: 미적용 (조건 불충족) · 보정량 0' in text
    assert '상한 1 · 상한 적용 후 0.7 / 소수 3자리 반올림' in text
    assert 'place_order / detail0.70.7RSI: 미적용' in text
    trace['execution_events']=[{'stage':'place_order','detail':{'raw':.7,'adjusted':.7,'note':None}}]
    text=render('#trace/trace-one',value)['content']
    assert 'place_order / detail0.70.7보정 근거 미보관' in text


def test_raw_records_render_only_on_first_open_and_preserve_payload():
    value=fixture();trace=value['projection']['traces']['trace-one']
    trace['judgments'][0]['large_original']='UNOPENED_RAW_PAYLOAD_'+'x'*10000
    closed=render('#trace/trace-one',value)
    assert 'UNOPENED_RAW_PAYLOAD_' not in closed['content']
    assert '\"tag\": \"pre\"' not in json.dumps(closed['tree'])
    opened=render('#trace/trace-one',value,open_raw='수락 판단과 채택 상태')
    assert opened['content'].count('UNOPENED_RAW_PAYLOAD_')==1
    assert 'x'*10000 in opened['content']


def test_calculation_completion_is_separate_from_incomplete_portfolio_result():
    value=fixture();v=value['projection']['validations'][0]
    v['status']='insufficient_data';v['result'].update(mode='portfolio',effect_status='complete',
        performance={'status':'incomplete','gaps':['simulation_input_manifest_incomplete']})
    text=render('#validation/result-1',value)['content']
    assert '변경안의 계산 비교 상태완료' in text
    assert '포트폴리오·성과 검증 상태미완료' in text
    assert '성과 검증 자료 부족 사유["simulation_input_manifest_incomplete"]' in text
    assert '계산 비교 완료는 포트폴리오 성과 검증 완료나 운영 수익 개선의 증거가 아닙니다.' in text


def test_trial_list_shows_meaning_and_does_not_promote_completion_to_adoption():
    value=fixture();rows=value['projection']['validations']
    rows[0]['result'].update(mode='screen_only',effect_status='complete')
    rows[1]['result'].update(mode='portfolio',effect_status='complete',performance={'status':'incomplete'})
    view=render('#validations',value)
    for text in ['후보 선별 비교','가상 포트폴리오 비교','기존 동작 재현 · 통과','포트폴리오 계산 · 미완료','채택 불가','시험 변경안']:
        assert text in view['content']
    for hidden in ['result-1','hash-1','proposal-one']:
        assert hidden not in view['content']
    assert '#validation/result-1' in json.dumps(view['tree'])
    rows[0]['visibility']='protected'
    rows[1]['result']={}
    safe=render('#validations',value)['content']
    assert '보호된 시험' in safe and '시험 방식 미보관' in safe
    assert '채택 검토 가능' not in safe


def test_other_lists_use_titles_and_keep_raw_ids_searchable_but_not_visible():
    for route,hidden in [('#proposals','proposal-one'),('#cases','app-one'),('#targets','run-one')]:
        assert hidden not in render(route)['content']
    assert '시험 변경안' in render('#proposals')['content']

def render_v2(route):
    import trace_pages as T
    original=fixture();original['manifest']={};original['projection']['schema_version']='TRACE_VIEW@v1'
    responses={'/api/view':T.summary(original),'/api/targets':T.list_page(original,'targets'),'/api/cases':T.list_page(original,'cases')}
    for kind,identifier,relation in [('trace','trace-one','cases'),('case','app-one','proposals'),('proposal','proposal-one','validations')]:
        responses[f'/api/{kind}/{identifier}']=T.detail(original,kind,identifier)
        responses[f'/api/relations/{kind}/{identifier}/{relation}']=T.relations(original,kind,identifier,relation)
    for identifier in ('validation-one','result-1','result-2'):
        responses['/api/validation/'+identifier]=T.detail(original,'validation',identifier)
    result=subprocess.run(['node','-e',RUNNER],input=json.dumps({'script':str(SCRIPT),'route':route,'responses':responses}),capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)

def test_v2_requests_only_selected_detail_and_related_metadata():
    result=render_v2('#proposal/proposal-one')
    assert [url.split('?')[0] for url in result['requests']]==['/api/view','/api/proposal/proposal-one','/api/relations/proposal/proposal-one/validations']
    text=json.dumps(result['tree'],ensure_ascii=False)
    assert '#validation/result-1' in text and '#validation/result-2' in text
    assert '제목시험 변경안' in result['content']
    selected=render_v2('#trace/trace-one')
    assert '100 / 40 / 60' in selected['content']
    assert '#case/app-one' in json.dumps(selected['tree'])

def test_v2_ambiguous_validation_requires_exact_result_selection():
    result=render_v2('#validation/validation-one')
    assert '검증 결과 버전을 선택하세요' in result['content']
    assert '#validation/result-2' in json.dumps(result['tree'])
    exact=render_v2('#validation/result-2')
    assert '기존 동작 재현통과' in exact['content']


def test_v2_case_list_preserves_verdict_and_selected_evaluation_date():
    result=render_v2('#cases')
    assert '반증' in result['content'] and '2026-11-11 · 이전 평가' in result['content']
    assert [url.split('?')[0] for url in result['requests']]==['/api/view','/api/cases']


def test_semantic_panel_distinguishes_missing_role_unreviewed_and_unknown_loss():
    value=fixture()
    value['projection']['traces']['trace-one']['semantic_execution']={
        'eligibility':{'status':'reassessment_required'},
        'stages':{'interpretation':{'status':'accepted'},'bull':{'status':'accepted','evaluations':[
            {'path_id':'path-one','evidence_clarity':'원자료 확인','causal_clarity':'가정 포함',
             'likelihood':'판단하기 어려움','loss_magnitude':'규모 자료 부족','uncertainties':['지속성 미확인']}]},
            'bear':None,'lead':{'status':'accepted','confidence_raw':0,'confidence_band_id':'opposed',
                              'decision':{'salient_risk_count':None,'unreviewed_ids':['objection-one'],
                                          'joint_buy_interpretation_paths':['joint-history-only'],
                                          'adopted_joint_buy_interpretation_paths':[],
                                          'dispositions':[{'evaluation_id':'objection-one','state':'unreviewed','reason':'미검토'}]}}}}
    text=render('#trace/trace-one',value)['content']
    for expected in ('공통 의미 위의 판단 단계','미제출·미확인','반대 없음으로 해석하지 않음','판단하기 어려움','규모 자료 부족','미검토'):
        assert expected in text
    assert '원확신도0' in text
    assert '양측이 매수 해석에 동의한 경로["joint-history-only"]' in text
    assert '총괄이 채택한 공동 경로[]' in text
