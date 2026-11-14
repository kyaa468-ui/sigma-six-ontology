"""Future adoption consumer seam; no step-7 adoption is performed by these tests."""


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

from test_ontology import C, M, O, P, NOW, clone, isolated
from test_judgment_integration import setup_run
import ai_judgment as J
import judgment_context as JC
import lesson_registry as L


def lesson(**changes):
    value={'schema_version':L.VERSION,'lesson_version_id':'lesson-test-v1',
        'proposal_id':'proposal-test','proposal_version':1,'text':'기관 순매도와 외국인 순매수를 별도로 검토한다.',
        'source_case_ids':['premise-old'], 'source_refs':[{'review_id':'review-old','result_hash':'frozen-result'}],
        'scope':{'codes':['000001'],'confidence_for':['매수'],'regimes':['상승'],
                 'price_kinds':['daily_close'],'flow_methods':['daily_accumulated']},
        'condition':{'op':'gt','left':{'field':'features.gap'},'right':{'value':0,'unit':'ratio'}},
        'effective_from':(NOW-timedelta(days=1)).isoformat(),
        'effective_until':(NOW+timedelta(days=7)).isoformat(), 'priority':1,
        'base_ref':{'definition_hash':'test-definition','settings_hash':'test-settings'}}
    value.update(changes)
    return value


class Backend:
    """A fake verified stage-7 backend, never installed by production code."""
    def __init__(self, lessons=None):
        self.lessons=lessons if lessons is not None else [lesson()]
        self.limit=5; self.proof_changes={}
    def list_lessons(self, as_of):
        return {'lessons':copy.deepcopy(self.lessons),'display_limit':self.limit}
    def verify_adoption(self, item, as_of):
        proof={'schema_version':L.PROOF_VERSION,'status':'verified','verifier_id':'fixture-verifier-v1',
               'adoption_id':'adoption-test','validation_id':'validation-test',
               'lesson_version_id':item['lesson_version_id'],'proposal_id':item['proposal_id'],
               'proposal_version':item['proposal_version'],'lesson_hash':L.digest(item),
               'base_ref':copy.deepcopy(item['base_ref']),'current_base_ref':copy.deepcopy(item['base_ref']),
               'base_status':'current','adopted_at':(NOW-timedelta(hours=1)).isoformat(),
               'verified_at':as_of,'revoked_at':None}
        proof.update(self.proof_changes)
        return proof


def briefing(monkeypatch):
    setup_run(monkeypatch)
    b=C.load_briefing()
    trace=O.SelectionTrace('prep',P,'상승','fixture')
    for row in b['candidates']:
        trace.begin(row,row['close'])
    trace.attach(b['candidates']); trace.finish(b)
    C.write_json(C.BRIEFING,b)
    regime={'kind':'regime','regime':'상승','time':C.stamp(),'regime_event_id':'regime-fixture'}
    JC.record_regime(regime)
    monkeypatch.setattr(C,'latest_regime',lambda:copy.deepcopy(regime))
    return b


def install(monkeypatch, backend=None):
    backend=backend or Backend()
    monkeypatch.setattr(L,'_adoption_backend',lambda:backend)
    return backend


def delivered(text):
    did=text.split('브리핑 전달 ID: ')[-1].splitlines()[0]
    receipt=read_stored(J._path('deliveries',did))
    return did,receipt,[e for e in receipt['evidence_catalog'] if e['kind']=='adopted_lesson']


def judgment(did,e,*,key='lesson-judgment',code='000001',side='매수',kind='fact_claim'):
    argument={'id':'lesson-fact','kind':kind,'proposer':'lead','stance':'support','adoption':'adopted',
              'evidence':{'delivery_id':did,'evidence_id':e['evidence_id']}}
    if kind=='fact_claim':
        argument.update(measurement_kind='adopted_lesson',assertion={'op':'value','value':e['value'],'unit':'text'})
    else:
        argument['quote']=e['value']
    return {'request_key':key,'code':code,'recommended_action':'관망' if side=='매수' else '보유',
            'confidence_for':side,'confidence_raw':.7,'decision':'기록된 지침의 적용 범위를 검토했다.',
            'delivery_ids':[did],'arguments':[argument], 'premises':[]}


def test_default_unavailable_ignores_forged_candidate_and_approved_flags(monkeypatch):
    b=briefing(monkeypatch)
    root=Path(C.WORK)/'ontology/learning_proposals/adoptions'; root.mkdir(parents=True)
    (root/'forged.json').write_text(json.dumps({'approved':True,'status':'adopted','lesson':lesson()}))
    before={p:p.read_bytes() for p in Path(C.WORK).rglob('*') if p.is_file()}
    selected=L.select_for_briefing(b)
    assert selected=={'lessons':[],'excluded':[],'recording_status':'not_applicable','status':'step7_verifier_unavailable'}
    assert all(p.read_bytes()==body for p,body in before.items())
    text=M.get_briefing()
    assert lesson()['text'] not in text
    assert not delivered(text)[2]


def test_valid_fixture_backend_real_briefing_and_judgment_adoption(monkeypatch):
    briefing(monkeypatch); install(monkeypatch)
    text=M.get_briefing()
    did,receipt,evidence=delivered(text)
    assert len(evidence)==1 and lesson()['text'] in text
    e=evidence[0]
    assert e['subject']=='000001' and e['lesson_version_id']=='lesson-test-v1'
    assert e['lesson_context']['applicability']['status']=='applicable'
    assert e['source_case_ids']==['premise-old']
    result=M.record_ai_judgments([judgment(did,e)])[0]
    assert result['status']=='accepted',result
    stored=J.load_judgment(result['judgment_id'])['arguments'][0]
    assert stored['adoption']=='adopted' and stored['fact_validation']=='passed'
    assert stored['verified_scope']=='source_assertion_only'
    assert stored['source_truth']=='unverified'
    assert '시장 사실 미검증' in stored['server_summary']
    assert stored['evidence_snapshots'][0]['adoption_id']=='adoption-test'
    assert receipt['context']['adopted_lessons'][0]['lesson_snapshot']==lesson()


def test_quote_is_only_stored_text_match_not_truth(monkeypatch):
    briefing(monkeypatch); install(monkeypatch)
    did,_,items=delivered(M.get_briefing())
    result=M.record_ai_judgments([judgment(did,items[0],kind='source_quote')])[0]
    claim=J.load_judgment(result['judgment_id'])['arguments'][0]
    assert claim['quote_validation']=='matched_stored_text' and claim['source_truth']=='unverified'


@pytest.mark.parametrize('alter,reason',[
    ({'status':'approved'},'adoption_not_verified'),
    ({'lesson_hash':'forged'},'lesson_version_hash_mismatch'),
    ({'proposal_id':'another'},'adoption_identity_mismatch'),
    ({'proposal_version':2},'adoption_identity_mismatch'),
    ({'base_status':'stale'},'stale_base'),
    ({'current_base_ref':{'definition_hash':'changed','settings_hash':'test-settings'}},'stale_base'),
    ({'revoked_at':NOW.isoformat()},'revoked'),
    ({'adopted_at':(NOW+timedelta(seconds=1)).isoformat()},'future_adoption'),
    ({'verified_at':(NOW-timedelta(hours=2)).isoformat()},'verification_before_adoption'),
    ({'validation_id':''},'adoption_proof_incomplete'),
])
def test_invalid_adoption_proof_never_delivered(monkeypatch,alter,reason):
    b=briefing(monkeypatch); backend=install(monkeypatch); backend.proof_changes.update(alter)
    selected=L.select_for_briefing(b)
    assert not selected['lessons'] and reason in str(selected['excluded'])
    assert not delivered(M.get_briefing())[2]


@pytest.mark.parametrize('changes,reason',[
    ({'proposal_version':'1'},'invalid_proposal_version'),
    ({'proposal_version':True},'invalid_proposal_version'),
    ({'effective_from':(NOW+timedelta(days=1)).isoformat()},'outside_effective_period'),
    ({'effective_until':NOW.isoformat()},'outside_effective_period'),
    ({'scope':{'codes':['999999'],'confidence_for':['매수']}},'code_outside_scope'),
    ({'scope':{'codes':['000001'],'confidence_for':['매수'],'regimes':['하락']}},'outside:features.regime'),
    ({'scope':{'codes':['000001'],'confidence_for':['매수'],'price_kinds':['intraday_current']}},'outside:stratum.price_kind'),
    ({'scope':{'codes':['000001'],'confidence_for':['매수'],'flow_methods':['intraday_estimate']}},'outside:stratum.flow_method'),
    ({'source_case_ids':[]},'missing_source_case_ids'),
])
def test_scope_time_and_provenance_required(monkeypatch,changes,reason):
    b=briefing(monkeypatch); install(monkeypatch,Backend([lesson(**changes)]))
    selected=L.select_for_briefing(b)
    assert not selected['lessons'] and reason in str(selected['excluded'])


def test_unknown_observation_and_future_measurement_do_not_become_zero(monkeypatch):
    b=briefing(monkeypatch); install(monkeypatch)
    measures=b['candidates'][0]['_ontology']['observation']['measurements']
    measures['price']['value']=None
    selected=L.select_for_briefing(b)
    assert not selected['lessons'] and selected['excluded'][0]['status']=='unknown'
    measures['price']['value']=20400
    measures['price']['available_at']=(NOW+timedelta(minutes=1)).isoformat()
    assert not L.select_for_briefing(b)['lessons']
    measures['price']['available_at']=NOW.isoformat()
    measures['price']['unit']='USD'
    assert not L.select_for_briefing(b)['lessons']
    measures['price']['unit']='KRW'
    measures['price']['status']='recording_error'
    assert not L.select_for_briefing(b)['lessons']


def test_unrecorded_regime_remains_unknown_for_lesson_scope(monkeypatch):
    b=briefing(monkeypatch); install(monkeypatch)
    monkeypatch.setattr(C,'latest_regime',lambda:{'kind':'regime','regime':'상승','time':C.stamp(),
                                               'regime_event_id':'unrecorded-event'})
    selected=L.select_for_briefing(b)
    assert not selected['lessons']
    assert selected['excluded'][0]['status']=='unknown'
    assert selected['excluded'][0]['inputs']['regime_snapshot']['reason']=='regime_event_unrecorded'


def test_wrong_subject_and_confidence_direction_rejected(monkeypatch):
    briefing(monkeypatch); install(monkeypatch)
    did,_,items=delivered(M.get_briefing())
    wrong_subject=M.record_ai_judgments([judgment(did,items[0],code='999999')])[0]
    assert wrong_subject['status']=='rejected' and wrong_subject['errors'][0]['code']=='SUBJECT_MISMATCH'
    wrong_side=M.record_ai_judgments([judgment(did,items[0],side='매도',key='wrong-direction')])[0]
    assert wrong_side['status']=='rejected' and wrong_side['errors'][0]['code']=='LESSON_APPLICABILITY_MISMATCH'


def test_revocation_does_not_rewrite_original_receipt_or_adoption(monkeypatch):
    briefing(monkeypatch); backend=install(monkeypatch)
    did,receipt,items=delivered(M.get_briefing())
    accepted=M.record_ai_judgments([judgment(did,items[0])])[0]
    stored=J.load_judgment(accepted['judgment_id'])
    backend.proof_changes['revoked_at']=NOW.isoformat()
    assert not delivered(M.get_briefing())[2]
    assert J.load_judgment(accepted['judgment_id'])==stored
    assert read_stored(J._path('deliveries',did))==receipt
    # Validity at delivery remains usable; today's revocation cannot change a
    # historical reference into a mismatch on a later audit/submission.
    another=M.record_ai_judgments([judgment(did,items[0],key='old-receipt-after-revocation')])[0]
    assert another['status']=='accepted',another


def test_current_backend_failure_preserves_briefing_and_candidate_order(monkeypatch):
    b=briefing(monkeypatch); backend=install(monkeypatch)
    def broken(*args): raise OSError('verifier temporarily unavailable')
    backend.list_lessons=broken
    selected=L.select_for_briefing(b)
    assert selected['recording_status']=='recording_error' and not selected['lessons']
    text=M.get_briefing(); assert '브리핑 전달 ID' in text and not delivered(text)[2]
    assert C.load_briefing()['candidates']==b['candidates']


def test_literal_units_and_unregistered_or_future_fields_are_not_guessed(monkeypatch):
    b=briefing(monkeypatch)
    bad={'op':'gt','left':{'field':'features.gap'},'right':{'value':0,'unit':'KRW'}}
    install(monkeypatch,Backend([lesson(condition=bad)]))
    assert not L.select_for_briefing(b)['lessons']
    future={'op':'eq','left':{'field':'verdict'},'right':{'value':'refuted','unit':'text'}}
    install(monkeypatch,Backend([lesson(condition=future)]))
    assert 'unsupported_field' in str(L.select_for_briefing(b)['excluded'])
    not_yet={'op':'eq','left':{'field':'adoption.explicitly_uses_above_ma20_and_foreign_buy'},'right':{'value':True,'unit':'bool'}}
    install(monkeypatch,Backend([lesson(condition=not_yet)]))
    assert L.select_for_briefing(b)['excluded'][0]['status']=='unknown'


@pytest.mark.parametrize('stored_time,expected_time',[
    (C.stamp(NOW),NOW.replace(second=0,microsecond=0).isoformat()),
    (NOW.strftime('%Y-%m-%d %H:%M:%S'),NOW.replace(microsecond=0).isoformat()),
    ('unrecognized date',None),
])
def test_legacy_lessons_keep_unverified_source_scope(monkeypatch,stored_time,expected_time):
    briefing(monkeypatch)
    C.write_json(M.tuning.LESSONS,{'time':stored_time,'lessons':['기존 문자열 교훈']})
    did,receipt,_=delivered(M.get_briefing())
    e=next(e for e in receipt['evidence_catalog'] if e['field']=='lessons.0')
    assert e['available_at']==expected_time
    argument={'id':'legacy','kind':'source_quote','proposer':'lead','stance':'neutral','adoption':'reference',
              'evidence':{'delivery_id':did,'evidence_id':e['evidence_id']},'quote':'기존 문자열 교훈'}
    value=judgment(did,e); value['arguments']=[argument]
    result=M.record_ai_judgments([value])[0]
    assert result['status']=='accepted',result
    claim=J.load_judgment(result['judgment_id'])['arguments'][0]
    assert claim['source_truth']=='unverified' and e['kind']=='history'


def test_display_budget_is_explicit_and_only_shown_rows_are_citable(monkeypatch):
    b=briefing(monkeypatch)
    backend=install(monkeypatch,Backend([lesson(lesson_version_id='lesson-low',priority=1),
                                        lesson(lesson_version_id='lesson-high',priority=2)]))
    backend.limit=None
    assert L.select_for_briefing(b)['recording_status']=='recording_error'
    backend.limit=1
    selected=L.select_for_briefing(b)
    assert [x['lesson_version_id'] for x in selected['lessons']]==['lesson-high']
    assert selected['excluded']==[{'lesson_version_id':'lesson-low','code':'000001','reason':'display_limit'}]
    _,_,items=delivered(M.get_briefing())
    assert [x['lesson_version_id'] for x in items]==['lesson-high']
    _,receipt,_=delivered(M.get_briefing())
    assert receipt['context']['lesson_exclusions']==selected['excluded']
    assert receipt['context']['lesson_selected_at']==NOW.isoformat(timespec='microseconds')


def test_conflicting_version_or_ambiguous_observation_is_not_first_row_wins(monkeypatch):
    b=briefing(monkeypatch)
    backend=install(monkeypatch,Backend([lesson(),lesson(text='다른 내용')]))
    assert not L.select_for_briefing(b)['lessons']
    backend.lessons=[lesson()]
    b['holdings']=[copy.deepcopy(b['candidates'][0])]
    b['holdings'][0]['_ontology']['observation']['measurements']['price']['value']=1
    selected=L.select_for_briefing(b)
    assert not selected['lessons'] and selected['excluded'][0]['reason']=='ambiguous_briefing_observation'


def test_verification_storage_failure_is_not_recorded_as_success(monkeypatch):
    b=briefing(monkeypatch); backend=install(monkeypatch)
    def failed(*args): raise OSError('adoption proof unavailable')
    backend.verify_adoption=failed
    selected=L.select_for_briefing(b)
    assert not selected['lessons'] and selected['recording_status']=='recording_error'
    assert 'adoption proof unavailable' in str(selected['excluded'])


def test_frozen_subject_metadata_and_applicability_are_checked_again(monkeypatch):
    briefing(monkeypatch); install(monkeypatch)
    did,receipt,items=delivered(M.get_briefing())
    lesson_item=next(e for e in receipt['evidence_catalog'] if e['kind']=='adopted_lesson')
    lesson_item['lesson_context']['applicability']['inputs']['values']['features.regime']='하락'
    from storage_fixture_helpers import replace_committed_fixture
    replace_committed_fixture(J._path('deliveries',did),receipt)
    result=M.record_ai_judgments([judgment(did,items[0])])[0]
    assert result['status']=='rejected' and result['errors'][0]['code']=='LESSON_APPLICABILITY_MISMATCH'


def test_malformed_backend_or_renderer_cannot_break_existing_briefing(monkeypatch):
    b=briefing(monkeypatch)
    def failed(*args,**kwargs): raise TypeError('malformed lesson response')
    monkeypatch.setattr(L,'select_for_briefing',failed)
    text=M.get_briefing()
    assert '교훈 조회에 오류' in text
    _,receipt,items=delivered(text)
    assert not items and receipt['context']['lesson_selection_status']=='recording_error'
    assert receipt['context']['lesson_selection_state']=='unavailable'
    assert C.load_briefing()['candidates']==b['candidates']
