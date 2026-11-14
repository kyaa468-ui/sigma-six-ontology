"""All approval/activation tests use temporary params and local proof records."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest

from test_ontology import C, P, NOW, clone, isolated
import policy_adoption as A
import policy_registry as S
import validation_store as V
import validation_spec as Q
import tuning as T


@pytest.fixture
def authority(monkeypatch,isolated):
    config=isolated/'config'; config.mkdir()
    params=config/'params.json'; params.write_text(json.dumps(P))
    rules=read_stored(Path(T.RULES_PATH))
    (config/'tuning.json').write_text(json.dumps(rules))
    monkeypatch.setattr(C,'CONFIG',str(config))
    monkeypatch.setattr(T,'PARAMS_PATH',str(params))
    monkeypatch.setattr(T,'RULES_PATH',str(config/'tuning.json'))
    monkeypatch.setattr(C,'params',lambda:read_stored(params))
    monkeypatch.setenv('RUN_MODE','operator')
    def base():
        value={'definition_hash':'source-v1','params':S.semantic_params(C.params()),'risk':C.risk_limits(),'tuning':rules}
        value['policy_base_ref']=clone(C.params().get(S.MARKER))
        value['settings_hash']=S.digest({k:value[k] for k in ('params','risk','tuning')})
        return value
    monkeypatch.setattr(S,'_current_base',base)
    return params


def trial(monkeypatch, *, effect=None, condition=None, invalid_gate=None):
    base=S._current_base()
    effect=effect or {'operation':'add_confidence','amount':-.1,'unit':'confidence','placement':'after_legacy_boost_before_cap'}
    proposal={'proposal_id':'proposal-test','version':1,'kind':'conditional_rule_change','effect':effect,
        'condition':condition or {'op':'gt','left':{'field':'features.gap'},'right':{'value':0,'unit':'ratio'}},
        'support_case_ids':['case-a'],'counter_case_ids':['case-b'],
        'source_refs':[{'review_id':'review-x','result_hash':'hash-x'}],
        'target_snapshot':{'operation':effect['operation'],'key':'decision.buy_min_confidence','unit':'confidence'}}
    spec={'scope':{'codes':['000001'],'confidence_for':['매수']}}
    if effect['operation']=='set_parameter': spec['scope']={'codes':['*'],'confidence_for':['매수','매도']}
    manifest={'validation_id':'validation-test','proposal_snapshot':proposal,'source_bundle':{'base_snapshot':base},
              'spec':spec,'criteria_complete':True}
    V.archive('specs','validation-test',manifest)
    V.archive('spec_commits','validation-test',{'spec_hash':V.digest(manifest)})
    result={'result_id':'result-test','validation_id':'validation-test','proposal':proposal,'spec':spec,
            'baseline_status':'passed','effect_status':'complete','performance':{'status':'complete'},
            'criteria':{'status':'passed'},'independent_validation':{'status':'verified'},
            'adoption_eligibility':{'status':'eligible'}}
    if invalid_gate:
        result[invalid_gate]='failed' if invalid_gate.endswith('_status') else {'status':'incomplete'}
    V.archive('results','result-test',result)
    V.archive('result_commits','result-test',{'result_hash':V.digest(result)})
    # Only the selection of an immutable result is stubbed, not its integrity.
    import policy_validation
    monkeypatch.setattr(policy_validation,'get_policy_validation',lambda *_:{**result,'validation_result_hash':V.digest(result)})
    return result


def prepare():
    return A.prepare_adoption('validation-test','prepare-1',operator='local-test',
        effective_from=(NOW-timedelta(minutes=1)).isoformat(),effective_until=(NOW+timedelta(days=7)).isoformat())


def activate(monkeypatch,**trial_args):
    trial(monkeypatch,**trial_args)
    prepared=prepare(); assert prepared['status']=='prepared',prepared
    result=A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],'adopt-1',operator='local-test')
    assert result['status']=='active',result
    return prepared,result


@pytest.mark.parametrize('gate',['baseline_status','effect_status','performance','criteria','independent_validation','adoption_eligibility'])
def test_all_completed_independent_effect_and_performance_gates_required(monkeypatch,authority,gate):
    before=authority.read_bytes(); trial(monkeypatch,invalid_gate=gate)
    result=prepare()
    assert result['status']=='rejected' and 'VALIDATION_NOT_ELIGIBLE' in result['error']
    assert authority.read_bytes()==before and not S.records('packages')['records']


def test_preparation_only_operator_activation_and_exact_retries(monkeypatch,authority):
    before=authority.read_bytes(); trial(monkeypatch)
    prepared=prepare()
    assert prepared['status']=='prepared' and authority.read_bytes()==before
    assert prepare()=={**prepared,'replayed':True}
    monkeypatch.setenv('RUN_MODE','decide')
    denied=A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],'a',operator='forged-operator')
    assert denied['error']=='MANUAL_OPERATOR_ONLY' and authority.read_bytes()==before
    monkeypatch.setenv('RUN_MODE','operator')
    result=A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],'a',operator='local-test')
    assert result['status']=='active'
    retried=A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],'a',operator='local-test')
    assert retried['replayed'] and retried['revision']==result['revision']
    assert S.snapshot()['status']=='active'
    assert S.semantic_params(C.params())==P
    assert S.records('packages')['records'][0]['candidate_policy_hash']==prepared['candidate_policy_hash']


def test_lost_response_after_atomic_commit_recovers_without_second_write(monkeypatch,authority):
    trial(monkeypatch); prepared=prepare(); archive=S.archive; calls=[]
    def fail_commit(kind,*args):
        if kind=='activation_commits': raise OSError('lost-after-commit')
        return archive(kind,*args)
    monkeypatch.setattr(S,'archive',fail_commit)
    result=A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],'retry',operator='local-test')
    assert result['status']=='recording_error' and S.snapshot()['status']=='active'
    snapshot=authority.read_bytes()
    monkeypatch.setattr(S,'archive',archive)
    monkeypatch.setattr(A,'_replace_params',lambda *_:calls.append('unexpected-second-write'))
    recovered=A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],'retry',operator='local-test')
    assert recovered['recovered_response'] and not calls and authority.read_bytes()==snapshot


def test_failed_pointer_write_preserves_old_and_retry_reuses_package(monkeypatch,authority):
    trial(monkeypatch); prepared=prepare(); before=authority.read_bytes(); replace=A._replace_params
    monkeypatch.setattr(A,'_replace_params',lambda *_:(_ for _ in ()).throw(OSError('disk-full')))
    result=A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],'retry',operator='local-test')
    assert result['status']=='recording_error' and authority.read_bytes()==before and S.snapshot()['status']=='inactive'
    monkeypatch.setattr(A,'_replace_params',replace)
    assert A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],'retry',operator='local-test')['status']=='active'
    assert len(S.records('packages')['records'])==1


def test_concurrent_adoptions_share_tuning_lock_and_only_one_revision_wins(monkeypatch,authority):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    trial(monkeypatch); prepared=prepare(); barrier=Barrier(2)
    def submit(key):
        barrier.wait()
        return A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],key,operator='local-test')
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(submit,['concurrent-a','concurrent-b']))
    assert sorted(r['status'] for r in results)==['active','rejected']
    assert next(r for r in results if r['status']=='rejected')['error']=='STALE_REVISION'
    assert len(S.records('packages')['records'])==1 and S.snapshot()['status']=='active'


def test_numeric_policy_atomic_tune_suspends_and_rollback_cannot_overwrite(monkeypatch,authority):
    value=P['decision']['buy_min_confidence']-.01
    prepared,result=activate(monkeypatch,effect={'operation':'set_parameter','value':value,'unit':'confidence'})
    assert C.params()['decision']['buy_min_confidence']==value
    with T._submission_lock(): T.write_params([{'key':'decision.buy_min_confidence','to':value-.01}])
    assert S.snapshot()['status']=='suspended'
    new_revision=S.revision()
    denied=A.rollback_policy(result['activation_id'],new_revision,'rollback',operator='local-test',reason='test')
    assert denied['error']=='ROLLBACK_WOULD_OVERWRITE_LATER_CHANGES'
    assert C.params()['decision']['buy_min_confidence']==value-.01
    denied=A.revoke_policy(result['revision'],'stale',operator='local-test',reason='test')
    assert denied['error']=='STALE_REVISION'


def test_rollback_restores_numeric_base_and_revocation_stops_extension(monkeypatch,authority):
    prepared,result=activate(monkeypatch,effect={'operation':'set_parameter','value':P['decision']['buy_min_confidence']-.01,'unit':'confidence'})
    rolled=A.rollback_policy(result['activation_id'],result['revision'],'rollback',operator='local-test',reason='test')
    assert rolled['status']=='rolled_back' and S.semantic_params(C.params())==P
    assert S.snapshot()['status']=='rolled_back'


def test_new_delta_added_before_single_cap_and_scope_missing_are_not_zero(monkeypatch,authority):
    p=clone(P); p['rsi_boost']={'buy_below':30,'sell_at':70,'add':.2};p['market_boost']=None
    authority.write_text(json.dumps(p))
    activate(monkeypatch)
    ctx={'status':'recorded','code':'000001','side':'매수','confidence_raw':.95,
         'policy':S.snapshot(),'inputs':{'features':{'gap':.02}}}
    b={'candidates':[{'code':'000001','rsi14':20}]}
    details=C.conf_boost_details('000001','매수',.95,p,b,policy_context=ctx)
    assert details['before_cap']==pytest.approx(1.05) and details['adjusted']==1.0
    missing=copy.deepcopy(ctx); missing['inputs']={}
    assert S.evaluate_adjustment(missing,'000001','매수',.95)['reason']=='condition_unknown'
    assert S.evaluate_adjustment(ctx,'999999','매수',.95)['amount']==0
    assert S.evaluate_adjustment(ctx,'000001','매수',.96)['reason']=='JUDGMENT_RAW_CONFIDENCE_MISMATCH'
    assert S.evaluate_adjustment(ctx,'000001','매수',.95,params=P)['reason']=='PINNED_PARAMS_MISMATCH'
    legacy=C.conf_boost_details('000001','매수',.95,p,b,policy_context={})
    assert len(legacy['adjustments'])==2 and legacy['before_cap']==pytest.approx(1.15)


def test_definition_drift_ends_proof_without_numeric_reversion(monkeypatch,authority):
    import execution_trace as E
    activate(monkeypatch); old=S._current_base; original=S.snapshot()
    monkeypatch.setattr(E,'load_plan',lambda _: {'inputs':{'policy':original}})
    monkeypatch.setattr(S,'_current_base',lambda:{**old(),'definition_hash':'source-v2'})
    assert S.snapshot()['status']=='stale_base' and S.semantic_params(C.params())==P
    assert S.check_plan_revision('plan-old')['error']=='STALE_POLICY_PLAN'


def test_active_lesson_proof_is_real_and_frozen_receipt_survives_revoke(monkeypatch,authority):
    import lesson_registry as L
    from test_lesson_registry import briefing
    b=briefing(monkeypatch)
    monkeypatch.setenv('RUN_MODE','operator')
    prepared,result=activate(monkeypatch,effect={'operation':'prompt_guidance','text':'기관 순매도와 외국인 순매수는 별도로 검토한다.'})
    selected=L.select_for_briefing(b)
    assert selected['lessons'],selected
    frozen=copy.deepcopy(selected['lessons'][0])
    assert A.revoke_policy(result['revision'],'revoke',operator='local-test',reason='test')['status']=='revoked'
    assert not L.select_for_briefing(b)['lessons']
    L.validate_frozen_lesson(frozen,frozen['code'],NOW.isoformat(),confidence_for='매수')


def test_forged_pointer_without_completed_validation_has_no_runtime_authority(monkeypatch,authority):
    p=C.params();p[S.MARKER]={'status':'active','package_id':'forged','package_hash':'forged','activation_id':'forged'}
    authority.write_text(json.dumps(p))
    assert S.snapshot()['status']=='recording_error'


def test_missing_judgment_is_unknown_and_live_plan_revision_is_checked(monkeypatch,authority):
    import execution_trace as E
    assert S.check_plan_revision(None) is None
    prepared,result=activate(monkeypatch)
    context=S.build_context('000001','매수')
    assert context['reason']=='JUDGMENT_REQUIRED'
    assert S.evaluate_adjustment(context,'000001','매수',.7)['amount']==0
    monkeypatch.setattr(E,'load_plan',lambda _: {'inputs':{'policy':context['policy']}})
    assert S.check_plan_revision('plan-x') is None
    revoked=A.revoke_policy(result['revision'],'revoke',operator='local-test',reason='test')
    assert revoked['status']=='revoked'
    with S.use_context(context):
        assert S.check_plan_revision('plan-x')['error']=='STALE_POLICY_PLAN'


def test_actual_params_reader_pins_value_with_audit_provenance(monkeypatch,authority):
    import audit_inputs
    # The isolation fixture replaces C.params; exercise the real function body
    # from the current module without mutating global function tables.
    import ast
    source=Path(C.__file__).read_text()
    fn=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='params')
    namespace=dict(vars(C));exec(compile(ast.Module(body=[fn],type_ignores=[]),C.__file__,'exec'),namespace)
    real_params=namespace['params']
    activate(monkeypatch)
    ctx={'policy':S.snapshot()}
    before=real_params()
    changed=clone(before);changed['decision']['buy_min_confidence']+=.01;authority.write_text(json.dumps(changed))
    with S.use_context(ctx),audit_inputs.capture() as reads:
        assert real_params()==before
    assert reads[-1]['status']=='policy_pinned' and reads[-1]['value']==before
    assert real_params()==changed


def followup_trial(monkeypatch):
    """A second immutable trial explicitly freezes the first active package."""
    manifest=clone(V.load('specs','validation-test'))
    manifest['validation_id']='validation-next'
    manifest['source_bundle']['base_snapshot']=S._current_base()
    manifest['proposal_snapshot']['effect']['amount']=.03
    result=clone(V.load('results','result-test'))
    result.update(validation_id='validation-next',result_id='result-next',proposal=manifest['proposal_snapshot'])
    V.archive('specs','validation-next',manifest)
    V.archive('spec_commits','validation-next',{'spec_hash':V.digest(manifest)})
    V.archive('results','result-next',result)
    V.archive('result_commits','result-next',{'result_hash':V.digest(result)})
    import policy_validation
    monkeypatch.setattr(policy_validation,'get_policy_validation',lambda *_:{**result,'validation_result_hash':V.digest(result)})
    return manifest


def prepare_next():
    return A.prepare_adoption('validation-next','next',operator='local-test',
            effective_from=(NOW-timedelta(minutes=1)).isoformat(),effective_until=(NOW+timedelta(days=7)).isoformat())


def test_active_package_replacement_requires_frozen_policy_base_and_does_not_stack(monkeypatch,authority):
    _,first=activate(monkeypatch)
    manifest=followup_trial(monkeypatch)
    prepared=prepare_next();assert prepared['status']=='prepared'
    second=A.adopt_policy(prepared['adoption_id'],prepared['expected_revision'],'next',operator='local-test')
    assert second['status']=='active' and second['package_id']!=first['package_id']
    policy=S.snapshot();assert policy['status']=='active'
    assert policy['package']['baseline_policy_ref']==manifest['source_bundle']['base_snapshot']['policy_base_ref']
    context={'status':'recorded','code':'000001','side':'매수','confidence_raw':.7,
             'policy':policy,'inputs':{'features':{'gap':.02}}}
    assert S.evaluate_adjustment(context,'000001','매수',.7)['amount']==.03
    assert S.semantic_params(C.params())==P


def test_revoking_frozen_active_base_invalidates_trial_even_if_numeric_hash_same(monkeypatch,authority):
    _,first=activate(monkeypatch)
    frozen=followup_trial(monkeypatch)['source_bundle']['base_snapshot']
    assert A.revoke_policy(first['revision'],'revoke',operator='local-test',reason='test')['status']=='revoked'
    assert S._current_base()['settings_hash']==frozen['settings_hash']
    denied=prepare_next()
    assert denied['status']=='rejected' and denied['error']=='STALE_POLICY_BASE'
