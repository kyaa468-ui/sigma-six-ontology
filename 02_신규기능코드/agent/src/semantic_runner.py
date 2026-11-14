"""Four isolated role invocations sharing one immutable judgment attempt."""
from __future__ import annotations
import os
import time
from contextlib import contextmanager
from pathlib import Path

import common as C
import judgment_coverage as JC
import semantic_layer as S

BASE_TOOLS = ('get_briefing', 'get_account', 'get_evidence_detail', 'record_external_evidence',
              'get_semantic_context', 'submit_semantic_stage', 'get_semantic_trace')
LEAD_TOOLS = ('plan_order','place_order','record_decision','get_judgment_coverage',
              'get_ai_judgments','get_judgment_detail','get_execution_trace','refresh_execution_status')


def tools_for(role, kind):
    names = list(BASE_TOOLS)
    if role == 'interpretation': names.append('set_regime')
    if role == 'lead': names.extend(n for n in LEAD_TOOLS if kind != 'view' or n not in ('place_order','plan_order'))
    return ['mcp__kiwoom__' + n for n in names] + (['WebSearch'] if role != 'lead' else [])


@contextmanager
def identity(attempt, kind, role):
    values = {'RUN_ID':attempt['run_id'], 'RUN_ATTEMPT_ID':attempt['run_attempt_id'],
              'RUN_MODE':kind, 'SION_SEMANTIC_ROLE':role}
    old = {k:os.environ.get(k) for k in values}
    os.environ.update(values)
    try: yield
    finally:
        for k,v in old.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v


def role_prompt(role, original):
    path = Path(C.AGENT) / '.claude/agents' / (role + '.md')
    guidance = path.read_text() if role in ('bull','bear') and path.exists() else ''
    if role == 'interpretation':
        path = Path(C.AGENT) / '.claude/agents/regime.md'
        guidance = path.read_text() if path.exists() else ''
    return f'''시멘틱 단계 실행: {role}. 이 호출의 역할은 실행기가 지정했습니다.
원래 회차 목적: {original}
일반 CLAUDE.md의 전체 거래 절차를 이번 호출에서 반복하지 마세요. 이 단계의 허용 도구만 사용합니다.
1. get_semantic_context()로 이번 대상 목록을 확인합니다.
2. interpretation은 먼저 get_briefing/get_account로 실제 자료를 받고 대상 목록을 다시 조회합니다. 새 보유 종목과 별도 매수 검토도 대상에 등록됩니다. 기존 지침으로 국면을 한 번 정한 뒤 set_regime 합니다. 이후 대상별 공통 맥락을 고정합니다.
3. 대상마다 get_semantic_context(code, confidence_for)를 호출해 반환된 공통 정의, 기존 실효 기준, 선행 평가, contract를 읽습니다.
4. submit_semantic_stage(input_id, data)로 해당 역할 결과를 제출하고 accepted를 확인합니다. 거절은 원인을 고쳐 같은 미수락 단계를 재시도합니다. 수락된 결과를 덮어쓰지 않습니다.
5. 기존 사실 주장 형식과 compact의 wire_format/bundle_refs를 사용합니다. 새 arguments를 작성하면 참조하는 근거 묶음을 선언합니다. 선행 주장 ID는 변경하지 않습니다.
6. 인과 경로는 id, claim_ids, assumptions, grouping_reason, grouping_status, direction, horizon을 갖습니다. 매수/매도 논거와 시나리오의 불확실성을 자연어로 적습니다. 숫자 위험 확률/가중치는 만들지 않습니다.
7. bull/bear는 받은 모든 경로를 평가하고 새 반론은 objections로 명시합니다. 별도 인과 경로가 필요하면 근거 arguments와 paths를 추가합니다. 사실 일치와 매수 해석 동의를 구별합니다.
8. lead는 모든 평가/반론의 처리 상태와 이유를 dispositions에 기록합니다. strong 구간은 양측 평가를 모두 채택한 공동 경로와 그 주장을 실제 채택해야 합니다. 기각·미검토 평가로 구간 자격을 얻을 수 없습니다. 기존 구간과 원확신도를 선택하고 반환된 judgment_id를 후속 도구에 사용합니다. 구간 내 숫자는 기존 AI 재량입니다.
9. lead만 원래 회차의 주문/관망 기록을 수행합니다. view는 기록만 합니다. 모든 대상·방향마다 성공한 record_decision 또는 place_order 결과가 필요하며 plan_order만으로 완료되지 않습니다. 주문이 없거나 차단된 경우 비주문 기록을 남깁니다. 수락된 판단 없이 일반 주문을 만들지 않습니다. 보호 청산은 별도 exit 실행의 기존 절차입니다.
양측 평가가 시작된 뒤 새 대상이 발견되면 SEMANTIC_TARGET_SET_CHANGED_RESTART_REQUIRED로 중단합니다. 이전 입력을 덮어쓰거나 선행 단계 없이 새 대상을 판단하지 않습니다. 이미 수행한 주문을 다시 실행하지 말고 새 회차에서 최신 계좌를 확인합니다.
한 대상의 자료 부족은 명시적 불확실성/유보로 평가합니다. 누락을 반대 없음이나 완료로 보고하지 마세요.
기존 역할 지침(도구 목록·전체 실행 지시는 위 단계 계약이 우선):
{guidance}
'''


def completion(attempt_id, kind):
    """Require linked, successful terminal use of each current accepted lead."""
    expected = S.targets()
    coverage = JC.get_coverage(attempt_id)
    if (coverage.get('recording_status') != 'recorded' or coverage.get('target_set_incomplete')
            or coverage.get('global_storage_errors')):
        raise S.SemanticError('SEMANTIC_COMPLETION_COVERAGE_UNAVAILABLE')
    keys = {(t['code'], t['confidence_for']) for t in expected}
    if keys != {(t['code'], t['confidence_for']) for t in coverage['targets']}:
        raise S.SemanticError('SEMANTIC_COMPLETION_TARGET_MISMATCH')
    rows = []
    for target in coverage['targets']:
        cid = S._chain_id(target['code'], target['confidence_for'])
        stages = [S._stage(cid, role) for role in S.ROLES]
        lead = stages[-1] or {}
        terminal_stages = {'record_decision'} if kind == 'view' else {'record_decision', 'place_order'}
        successful = [call['event_id'] for call in target['calls']
                      if call.get('stage') in terminal_stages and call.get('link', {}).get('status') == 'linked'
                      and lead.get('judgment_id') and call.get('judgment_id') == lead['judgment_id']
                      and any(result.get('outcome', {}).get('ok') is True for result in call.get('results', []))]
        complete = (all(stage and stage.get('status') == 'accepted' for stage in stages)
                    and target['status'] == 'linked' and bool(successful))
        rows.append({'code': target['code'], 'confidence_for': target['confidence_for'],
                     'coverage_status': target['status'], 'complete': complete,
                     'judgment_id': lead.get('judgment_id'), 'successful_terminal_event_ids': successful})
    return {'complete': all(row['complete'] for row in rows), 'expected_count': len(rows),
            'counts': coverage['counts'], 'targets': rows}


def run(kind, prompt, invoke, dry=False):
    if dry:
        print('DRY semantic stages: interpretation -> bull -> bear -> lead; no model or order calls')
        return True
    run_id = f"{C.now().strftime('%Y%m%d-%H%M')}-{kind}"
    attempt = JC.start_attempt(run_id, kind, C.load_briefing(), C.read_json(C.ACCOUNT))
    if attempt.get('recording_status') != 'recorded' or attempt.get('target_set_incomplete'):
        if attempt.get('run_attempt_id'):
            JC.finish_attempt(attempt['run_attempt_id'],'error')
        return False
    import validation_holdout
    validation_holdout.enroll_attempt(attempt, C.load_briefing(), C.read_json(C.ACCOUNT))
    import runtime_inputs
    captured = runtime_inputs.capture_attempt(attempt['run_attempt_id'],kind,prompt,
                 sorted({tool for role in S.ROLES for tool in tools_for(role,kind)}),
                 {'semantic_stages':list(S.ROLES),'capture_kind':'orchestration_manifest_not_model_call'})
    if captured.get('recording_status') != 'recorded':
        JC.finish_attempt(attempt['run_attempt_id'],'error')
        return False
    state = {'attempt':attempt, 'budget':float(os.environ.get('MAX_BUDGET_USD','5')),
             'cost':0.0, 'deadline':time.monotonic()+25*60, 'calls':[], 'terminal':'error'}
    ok = False
    try:
        for role in S.ROLES:
            with identity(attempt, kind, role):
                S.targets()
                if state['cost'] >= state['budget'] or time.monotonic() >= state['deadline']:
                    raise S.SemanticError('SEMANTIC_EXECUTION_BUDGET_EXHAUSTED')
                if not invoke(kind, role_prompt(role,prompt), semantic_state=state, semantic_role=role):
                    raise S.SemanticError('SEMANTIC_MODEL_STAGE_FAILED',role)
                missing = [t for t in S.targets() if not S._stage(S._chain_id(t['code'],t['confidence_for']),role)]
                if missing:
                    raise S.SemanticError('SEMANTIC_EXPECTED_STAGE_MISSING', str(missing))
                if role == 'interpretation':
                    S.freeze_targets()
                if role == 'lead':
                    state['completion'] = completion(attempt['run_attempt_id'], kind)
                    if not state['completion']['complete']:
                        raise S.SemanticError('SEMANTIC_DOWNSTREAM_INCOMPLETE')
        state['terminal'] = 'normal'
        ok = True
    except Exception as exc:
        state['error'] = str(exc)
        if getattr(exc, 'code', '') == 'SEMANTIC_TARGET_SET_CHANGED_RESTART_REQUIRED':
            state['restart_required'] = True
        print('Semantic execution incomplete: ' + str(exc))
    finally:
        finished = JC.finish_attempt(attempt['run_attempt_id'],state['terminal'])
        if finished.get('recording_status') == 'recording_error':
            ok = False
            state.update(terminal='error', error='SEMANTIC_FINISH_RECORDING_FAILED')
        import record_store
        try:
            record_store.publish(C.ontology_root()/'semantic'/attempt['run_attempt_id']/'run.json',
                                 {k:v for k,v in state.items() if k!='deadline'},kind='semantic_run')
        except Exception as exc:
            JC.recording_error('semantic_run_result', exc, attempt['run_attempt_id'])
            JC.finish_attempt(attempt['run_attempt_id'], 'error')
            ok = False
    return ok
