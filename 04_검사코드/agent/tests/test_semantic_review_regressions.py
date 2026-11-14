"""R1-R3 adversarial combinations through real local tools and orchestration."""
import copy

import pytest

from test_ontology import isolated, C, M, P, clone
from test_judgment_integration import setup_run, submission, read_stored
from test_semantic_layer import as_role, interpretation, through_bear, completed, evaluation
import ai_judgment as J
import judgment_coverage as JC
import semantic_layer as S
import semantic_runner as SR

pytestmark = pytest.mark.semantic


def agreed(monkeypatch):
    _, _, ctx, data = interpretation(monkeypatch)
    data['paths'][0]['direction'] = 'support'
    assert S.submit(ctx['input_id'], data)['status'] == 'accepted'
    for role in ('bull', 'bear'):
        as_role(monkeypatch, role)
        ctx = S.context('000001', '매수')
        ev = evaluation(role, agree='agree'); ev['stance'] = 'support'
        assert S.submit(ctx['input_id'], {'evaluations': [ev], 'objections': []})['status'] == 'accepted'
    as_role(monkeypatch, 'lead')
    return S.context('000001', '매수'), {
        'confidence_band_id': 'strong', 'confidence_raw': .81, 'recommended_action': '매수',
        'decision': '기존 구간 판단의 구조 검증용 합성 사례', 'adopted_claim_ids': ['h1'],
        'salient_risk_path_ids': [], 'dispositions': [
            {'evaluation_id': role+'-eval', 'state': 'adopted', 'reason': '해당 경로 채택'} for role in ('bull', 'bear')]}


@pytest.mark.parametrize('state,claims,accepted', [
    ('adopted', ['h1'], True), ('adopted', [], False), ('adopted', ['inst'], False),
    ('rejected', [], False), ('rejected', ['h1'], False),
    ('deferred', ['h1'], False), ('unreviewed', ['h1'], False)])
def test_strong_requires_adopted_assessments_and_path_claims(monkeypatch, state, claims, accepted):
    ctx, data = agreed(monkeypatch)
    data['dispositions'][1]['state'] = state
    data['adopted_claim_ids'] = claims
    result = S.submit(ctx['input_id'], data)
    assert (result['status'] == 'accepted') == accepted, result
    if accepted:
        semantic = J.load_judgment(result['judgment_id'])['semantic']
        assert semantic['adopted_joint_buy_interpretation_paths'] == ['p1']
    else:
        assert result['error_code'] == 'SEMANTIC_ADOPTED_JOINT_INTERPRETATION_REQUIRED'


def test_rejected_joint_agreement_is_history_not_final_support(monkeypatch):
    ctx, data = agreed(monkeypatch)
    data.update(confidence_band_id='balanced', confidence_raw=.5, adopted_claim_ids=[])
    for d in data['dispositions']: d['state'] = 'rejected'
    result = S.submit(ctx['input_id'], data)
    assert result['status'] == 'accepted', result
    semantic = J.load_judgment(result['judgment_id'])['semantic']
    assert semantic['joint_buy_interpretation_paths'] == ['p1']
    assert semantic['adopted_joint_buy_interpretation_paths'] == []


@pytest.mark.parametrize('state,claims', [('rejected', ['h1']), ('deferred', ['h1']),
                                        ('unreviewed', ['h1']), ('adopted', []), ('adopted', ['inst'])])
def test_resolved_risk_cannot_contradict_adoption(monkeypatch, state, claims):
    _, _, ctx, data = through_bear(monkeypatch)
    for d in data['dispositions']:
        if d['evaluation_id'].endswith('-eval'): d['state'] = state
    data['adopted_claim_ids'] = claims
    assert S.submit(ctx['input_id'], data)['error_code'] == 'SEMANTIC_RISK_ADOPTION_MISMATCH'


def test_uncertain_deferred_path_remains_unknown(monkeypatch):
    _, _, ctx, data = interpretation(monkeypatch)
    data['paths'][0]['grouping_status'] = 'uncertain'
    assert S.submit(ctx['input_id'], data)['status'] == 'accepted'
    for role in ('bull', 'bear'):
        as_role(monkeypatch, role); ctx = S.context('000001', '매수')
        assert S.submit(ctx['input_id'], {'evaluations': [evaluation(role)], 'objections': []})['status'] == 'accepted'
    as_role(monkeypatch, 'lead'); ctx = S.context('000001', '매수')
    final = {'confidence_band_id':'balanced', 'confidence_raw':.5, 'recommended_action':'관망',
             'decision':'미확정 경로는 채택된 확정 악재가 아니다', 'adopted_claim_ids':['inst'],
             'salient_risk_path_ids':['p1'], 'dispositions':[
                 {'evaluation_id':r+'-eval','state':'deferred','reason':'자료 부족'} for r in ('bull','bear')]}
    result = S.submit(ctx['input_id'], final)
    assert result['status'] == 'accepted', result
    assert J.load_judgment(result['judgment_id'])['semantic']['salient_risk_count'] is None


def new_holding(fake):
    fake.account_value['holdings'].append({'code':'999999','name':'new holding','qty':1,
                                          'avg_price':20000,'price':20400,'value':20400})


def test_account_discovery_registers_both_directions_without_rewriting_start(monkeypatch):
    fake, manifest = setup_run(monkeypatch)
    original = copy.deepcopy(manifest)
    as_role(monkeypatch, 'interpretation')
    new_holding(fake); assert M.get_account()['recording_status'] == 'recorded'
    assert 'input_id' in M.get_semantic_context('999999', '매도')
    assert 'input_id' in M.get_semantic_context('999999', '매수')
    keys = lambda rows: {(r['code'],r['confidence_for']) for r in rows}
    assert keys(S.targets()) == keys(JC.get_coverage()['targets']) == {
        ('000001','매수'),('999999','매도'),('999999','매수')}
    assert JC._manifest(manifest['run_attempt_id']) == original


def test_late_discovery_blocks_consumption_preserving_prior_result(monkeypatch):
    fake, manifest, _, _, jid = completed(monkeypatch)
    before = copy.deepcopy(J.load_judgment(jid))
    new_holding(fake); M.get_account()
    assert 'SEMANTIC_TARGET_SET_CHANGED_RESTART_REQUIRED' in M.get_semantic_context('999999','매도')['error']
    with pytest.raises(S.SemanticError, match='TARGET_SET_CHANGED_RESTART_REQUIRED'):
        S.consume('000001','매수',.7,jid)
    assert J.load_judgment(jid) == before
    assert any(t['code']=='999999' for t in JC.get_coverage()['targets'])


def run_fixture(monkeypatch, *, kind='view', terminal='record', discovery=None, held=False):
    fake, _ = setup_run(monkeypatch, held=held)
    calls, observed = [], {}
    def invoke(kind, prompt, semantic_state, semantic_role):
        calls.append(semantic_role)
        if discovery == semantic_role:
            new_holding(fake)
        if semantic_role == 'interpretation':
            text = M.get_briefing()
            account = M.get_account()
            observed['source'] = submission(text)
            observed['account'] = account
        elif discovery == semantic_role:
            M.get_account()
        for t in S.targets():
            code, side = t['code'], t['confidence_for']
            ctx = S.context(code, side)
            if semantic_role == 'interpretation':
                if code == '000001':
                    data = copy.deepcopy(observed['source'])
                else:
                    account = observed['account']
                    e = next(e for e in account['evidence_catalog'] if e['subject']==code and e['field']=='qty')
                    data = {'delivery_ids':[account['delivery_id']], 'arguments':[
                        {'id':'inst','kind':'fact_claim','stance':'oppose',
                         'evidence':{'delivery_id':account['delivery_id'],'evidence_id':e['evidence_id']},
                         'measurement_kind':e['kind'], 'assertion':{'op':'value','value':e['value'],'unit':e['unit']}}]}
                data['arguments'].append({'id':'h1','kind':'hypothesis','text':'합성 시험의 미확인 손실 경로',
                                          'claim_ids':['inst'],'stance':'oppose'})
                data['paths'] = [{'id':'p1','claim_ids':['inst','h1'],'assumptions':['시험 가정'],
                                  'grouping_reason':'단일 경로','grouping_status':'resolved','direction':'oppose','horizon':'기존 기간'}]
            elif semantic_role in ('bull','bear'):
                data = {'evaluations':[evaluation(semantic_role)], 'objections':[]}
            else:
                data = {'confidence_band_id':'clear_risk' if side=='매수' else 'existing_sell_discretion',
                        'confidence_raw':.7, 'recommended_action':'매수' if side=='매수' else '보유',
                        'decision':'격리된 검증용 판단', 'adopted_claim_ids':['inst','h1'], 'salient_risk_path_ids':['p1'],
                        'dispositions':[{'evaluation_id':r+'-eval','state':'adopted','reason':'시험 경로 채택'} for r in ('bull','bear')]}
            result = S.submit(ctx['input_id'], data)
            assert result['status'] == 'accepted', result
            if semantic_role != 'lead': continue
            jid = result['judgment_id']
            if terminal in ('plan','order'):
                plan = M.plan_order(code,side,.7,judgment_id=jid)
                assert 'plan_id' in plan, plan
                if terminal == 'order':
                    assert M.place_order(code,side,plan['qty'],.7,'bull','bear','fixture',judgment_id=jid,
                                         plan_id=plan['plan_id'],order_attempt_id=plan['order_attempt_id'])['ok']
            elif terminal in ('record','failed_record','partial'):
                if terminal == 'partial' and side == '매수': continue
                if terminal == 'failed_record':
                    def fail(*args, **kwargs): raise OSError('simulated journal failure')
                    monkeypatch.setattr(C,'journal',fail)
                out = M.record_decision(code,'관망' if side=='매수' else '보유',.7,'bull','bear','fixture',
                                        judgment_id=jid,confidence_for=side)
                assert bool(out.get('ok')) == (terminal != 'failed_record'), out
        semantic_state['cost'] += .1
        return True
    ok = SR.run(kind, '격리된 단계·후속 결과 검증', invoke)
    path = next((C.ontology_root()/'semantic').glob('*/run.json'))
    run = read_stored(path)
    return ok, run, JC.get_coverage(run['attempt']['run_attempt_id']), calls, fake


@pytest.mark.parametrize('kind,terminal,expected', [
    ('view','none',False), ('view','record',True), ('view','failed_record',False),
    ('decide','plan',False), ('decide','record',True), ('decide','order',True)])
def test_completion_requires_successful_terminal_use(monkeypatch, kind, terminal, expected):
    ok, run, coverage, calls, fake = run_fixture(monkeypatch,kind=kind,terminal=terminal)
    assert ok is expected
    assert run['terminal'] == ('normal' if expected else 'error')
    assert calls == list(S.ROLES)
    assert coverage['expected_count'] == 1
    assert coverage['counts']['linked'] == (0 if terminal=='none' else 1)
    assert run['completion']['complete'] is expected
    assert bool(fake.orders) == (terminal=='order')


def test_one_unrecorded_direction_cannot_disappear_from_denominator(monkeypatch):
    ok, run, coverage, _, _ = run_fixture(monkeypatch, held=True, terminal='partial')
    assert not ok and coverage['expected_count'] == 2
    assert coverage['counts']['linked'] == coverage['counts']['accepted_unlinked'] == 1


def test_new_holding_during_interpretation_completes_all_three_targets(monkeypatch):
    ok, run, coverage, _, fake = run_fixture(monkeypatch, discovery='interpretation')
    assert ok and coverage['expected_count'] == coverage['counts']['linked'] == 3
    assert len(run['completion']['targets']) == 3 and not fake.orders


@pytest.mark.parametrize('role', ['bull','bear','lead'])
def test_late_discovery_explicitly_requires_restart(monkeypatch, role):
    ok, run, coverage, calls, fake = run_fixture(monkeypatch, discovery=role)
    assert not ok and run['restart_required'] is True
    assert coverage['run_status'] == 'error' and coverage['expected_count'] >= 2
    assert calls[-1] == role and not fake.orders


def test_disabled_add_on_still_finishes_conditional_review_without_buying(monkeypatch):
    p = clone(P); p['decision']['add_on']['enabled'] = False
    monkeypatch.setattr(C, 'params', lambda:p)
    ok, _, coverage, _, fake = run_fixture(monkeypatch, held=True)
    assert ok and coverage['counts']['linked'] == 2 and not fake.orders
