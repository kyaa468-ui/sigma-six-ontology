"""Non-order actions remain linked to their original call, never by code guessing."""
from pathlib import Path
import json
import sys
from types import ModuleType
import pytest

from test_ontology import C, M, ROOT, isolated
from test_judgment_integration import setup_run, submission, watch
from test_execution_integration import planned, hook_call
import evaluation_context as EC
import execution_trace as E
import judgment_coverage as JC


def accepted(monkeypatch):
    fake,manifest=setup_run(monkeypatch)
    result=M.record_ai_judgments([submission(M.get_briefing())])[0]
    assert result['status']=='accepted'
    return fake,manifest,result['judgment_id']


def test_actual_watch_without_plan_has_original_call(monkeypatch):
    fake,manifest,jid=accepted(monkeypatch)
    before=JC.get_coverage(manifest['run_attempt_id'])
    assert watch(jid)=={'ok':True}
    result=EC.execution_context([jid],C.now().isoformat())
    assert result['recording_status']=='recorded',result['errors']
    assert not result['orders'] and not result['unlinked_references']
    assert len(result['non_order_paths'])==1
    row=result['non_order_paths'][0]
    assert row['path_kind']=='decision' and row['action']=='관망'
    assert row['original_call']['link']['status']=='linked'
    assert not row['financial_attribution'] and row['order_attempt_id'] is None
    assert not fake.orders


def test_plan_only_is_not_a_failed_or_filled_order(monkeypatch):
    fake,_,_,args=planned(monkeypatch)
    result=EC.execution_context([args['judgment_id']],C.now().isoformat())
    assert not result['orders'] and not result['unlinked_references']
    row=result['non_order_paths'][0]
    assert row['path_kind']=='plan_only'
    assert row['plan_id']==args['plan_id'] and row['link_status']=='linked'
    archived = [event for event in row['events'] if event['kind']=='order_plan']
    assert len(archived)==1 and archived[0]['inputs']['account']
    assert archived[0]['plan_id']==args['plan_id'] and archived[0]['definition']['definition_id']
    assert result['engine_definitions']
    assert all(x['status']=='recorded' for x in result['engine_definitions'].values())
    assert not fake.orders
    M.place_order(**args)
    afterwards=EC.execution_context([args['judgment_id']],C.now().isoformat())
    assert not afterwards['non_order_paths'] and len(afterwards['orders'])==1


def test_zero_quantity_plan_and_bad_original_call_stay_separate(monkeypatch):
    _,_,jid=accepted(monkeypatch)
    call=JC.enter('plan_order','000001','매수',.7,jid)
    E.record('tool_result',stage='plan_order',coverage_event_id=call,judgment_id=jid,code='000001',
             plan_id='plan-zero',order_attempt_id='attempt-zero',result={'qty':0,'side':'매수','confidence_raw':.7})
    bad=JC.enter('record_decision','000001','매수',.8,jid,action='관망')
    E.record('tool_result',stage='record_decision',coverage_event_id=bad,judgment_id=jid,code='000001',result={'ok':True})
    result=EC.execution_context([jid],C.now().isoformat())
    assert [x['path_kind'] for x in result['non_order_paths']]==['zero_quantity_plan']
    assert result['unlinked_non_order_references'][0]['reason']=='confidence_raw_mismatch'
    # A later correct record is a different event, not repair of the old one.
    watch(jid)
    later=EC.execution_context([jid],C.now().isoformat())
    assert len(later['unlinked_non_order_references'])==1
    assert len(later['non_order_paths'])==2


def test_readonly_and_missing_original_definition_not_current_repair(monkeypatch):
    _,_,_,args=planned(monkeypatch)
    for path in (Path(C.WORK)/'ontology/executions').glob('*/engine_definition/*.json'):
        path.unlink()
    before={str(p):p.read_bytes() for p in Path(C.WORK).rglob('*') if p.is_file()}
    result=EC.execution_context([args['judgment_id']],C.now().isoformat())
    assert all(x['status']=='unrecorded' for x in result['engine_definitions'].values())
    assert before=={str(p):p.read_bytes() for p in Path(C.WORK).rglob('*') if p.is_file()}


def test_direct_order_keeps_own_definition_without_borrowing_plan(monkeypatch,capsys):
    fake,_,jid=accepted(monkeypatch)
    args=dict(code='000001',side='매수',qty=1,confidence=.7,bull='local',bear='local',decision='local',judgment_id=jid)
    forwarded=hook_call(monkeypatch,capsys,args)['updatedInput']
    assert M.place_order(**forwarded)['ok'] and len(fake.orders)==1
    context=EC.execution_context([jid],C.now().isoformat())
    assert len(context['orders'])==1 and not context['non_order_paths']
    events=context['orders'][0]['events']
    assert not any(e['kind']=='order_plan' for e in events)
    actual=next(e for e in events if e['kind']=='risk_evaluation' and e['actor']=='server')
    assert actual['definition']['definition_id'] in context['engine_definitions']


def test_definition_keeps_loaded_body_when_disk_changes(monkeypatch,tmp_path):
    src=tmp_path/'engine'/'src';src.mkdir(parents=True)
    monkeypatch.setattr(C,'AGENT',str(src.parent))
    monkeypatch.setattr(E,'_VERIFIED_ENGINE_SOURCES',{})
    original='def calculate(value):\n    return value + 1\n'
    for name in ('common','sizing','risk'):
        path=src/(name+'.py');path.write_text(original)
        module=ModuleType(name);exec(compile(original,str(path),'exec',dont_inherit=True),module.__dict__)
        monkeypatch.setitem(sys.modules,name,module)
    first=E.capture_definition()
    assert first['source_identity_status']=='verified_loaded_source'
    (src/'risk.py').write_text(original.replace('+ 1','+ 2'))
    second=E.capture_definition()
    assert second['source_identity_status']=='verified_loaded_source'
    path=next((Path(C.WORK)/'ontology/executions').glob('*/engine_definition/'+second['definition_id']+'.json'))
    import snapshot_store
    saved=snapshot_store.load(path)
    assert saved['sources']['risk.py']==original
    assert saved['source_identity']['modules']['risk.py']['disk_differs_from_loaded_source']
    # A process without an earlier matching source must not claim the new disk
    # body was executed simply because it can read that body now.
    monkeypatch.setattr(E,'_VERIFIED_ENGINE_SOURCES',{})
    unknown=E.capture_definition()
    assert unknown['source_identity_status']=='unverified_loaded_source'


def test_definition_capture_failure_does_not_change_original_order_permission(monkeypatch):
    fake,_,_,args=planned(monkeypatch)
    before=JC.get_coverage()['recording_status']
    def fail(*a,**k): raise OSError('definition source unavailable')
    monkeypatch.setattr(E,'_definition_snapshot',fail)
    assert M.place_order(**args)['ok'] and len(fake.orders)==1
    assert JC.get_coverage()['recording_status']==before
    context=EC.execution_context([args['judgment_id']],C.now().isoformat())
    actual=next(e for e in context['orders'][0]['events'] if e['kind']=='risk_evaluation' and e['actor']=='server')
    assert actual['definition']['recording_status']=='recording_error'


def test_loaded_identity_is_bound_to_original_definition_id(monkeypatch):
    _,_,_,args=planned(monkeypatch)
    for path in (Path(C.WORK)/'ontology/executions').glob('*/engine_definition/*.json'):
        import snapshot_store
        body=snapshot_store.load(path)
        body['source_identity']['status']='forged_runtime_identity'
        from storage_fixture_helpers import replace_committed_fixture
        replace_committed_fixture(path,body)
    result=EC.execution_context([args['judgment_id']],C.now().isoformat())
    assert result['recording_status']=='recording_error'
    assert not result['engine_definitions']
    assert any(e['reason']=='engine_definition_hash_mismatch' for e in result['errors'])


def test_constant_only_edit_cannot_claim_new_constant_was_loaded(monkeypatch,tmp_path):
    path=tmp_path/'example_engine.py'
    original='LIMIT = 1\ndef calculate(value):\n    return value + LIMIT\n'
    path.write_text(original)
    module=ModuleType('example_engine')
    exec(compile(original,str(path),'exec',dont_inherit=True),module.__dict__)
    monkeypatch.setitem(sys.modules,'example_engine',module)
    monkeypatch.setattr(E,'_VERIFIED_ENGINE_SOURCES',{})
    source,identity=E._runtime_source('example_engine',path)
    assert identity['status']=='verified_loaded_source' and module.calculate(1)==2
    path.write_text(original.replace('LIMIT = 1','LIMIT = 2'))
    source,identity=E._runtime_source('example_engine',path)
    assert source==original and identity['status']=='verified_loaded_source'
    assert identity['disk_differs_from_loaded_source']
    assert identity['globals']['loaded_values']['LIMIT']['value']==1
    monkeypatch.setattr(E,'_VERIFIED_ENGINE_SOURCES',{})
    source,identity=E._runtime_source('example_engine',path)
    assert identity['status']=='unverified_loaded_source'
    assert identity['globals']['mismatched_globals']==['LIMIT']
    # Runtime mutation also invalidates the formerly cached body.
    path.write_text(original)
    assert E._runtime_source('example_engine',path)[1]['status']=='verified_loaded_source'
    module.LIMIT=3
    assert E._runtime_source('example_engine',path)[1]['status']=='unverified_loaded_source'


def test_unknown_global_expression_is_not_executed_to_verify(monkeypatch,tmp_path):
    path=tmp_path/'example_engine.py'
    source='LIMIT = dangerous()\ndef calculate(value):\n    return value + LIMIT\n'
    path.write_text(source)
    module=ModuleType('example_engine');module.LIMIT=1
    exec(compile('def calculate(value):\n    return value + LIMIT\n',str(path),'exec',dont_inherit=True),module.__dict__)
    module.dangerous=lambda: pytest.fail('metadata must never execute arbitrary initializer')
    monkeypatch.setitem(sys.modules,'example_engine',module)
    monkeypatch.setattr(E,'_VERIFIED_ENGINE_SOURCES',{})
    identity=E._runtime_source('example_engine',path)[1]
    assert identity['status']=='unverified_loaded_source' and identity['globals']['unknown_globals']==['LIMIT']
