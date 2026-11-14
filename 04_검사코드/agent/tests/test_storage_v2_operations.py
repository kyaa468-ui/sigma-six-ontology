"""Operational contracts: migration, live paging, raw output, and bounded errors."""
import io,json,sys,hashlib
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import common as C,record_store as R,storage_objects as O,log_store as L
import migrate_storage_v2 as M,scoring_store as S,process_spool as P

@pytest.fixture
def work(tmp_path,monkeypatch):
    root=tmp_path/'source';root.mkdir();(root/'ontology').mkdir()
    monkeypatch.setattr(C,'WORK',str(root))
    return root

def test_resume_verify_pointer_and_new_event_rollback_guard(work):
    a=work/'ontology/events/a.json';a.parent.mkdir();a.write_text('{"event_id":"a","raw":"original"}')
    (a.parent/'b.json').write_text('{"event_id":"b","raw":"original"}')
    before={str(p):p.read_bytes() for p in work.rglob('*') if p.is_file()}
    gen=work.parent/'generation'
    assert M.convert(work,gen,max_records=1)['status']=='checkpointed'
    assert M.verify(work,gen)['status']=='incomplete'
    assert M.convert(work,gen)['converted']==2
    assert M.verify(work,gen)['status']=='verified'
    assert before=={str(p):p.read_bytes() for p in work.rglob('*') if p.is_file()}
    M.activate(work,gen);assert C.ontology_root()==gen/'ontology'
    assert R.load(C.ontology_root()/'events/a.json')['event_id']=='a'
    M.rollback(work);assert C.ontology_root()==work/'ontology'
    M.activate(work,gen)
    R.publish(C.ontology_root()/'events/new.json',{'event_id':'new'})
    with pytest.raises(ValueError,match='NEW_EVENTS'):M.rollback(work)
    assert C.ontology_root()==gen/'ontology'

def test_migration_rejects_changed_source_and_truncated_row(work):
    path=work/'runs.jsonl';path.write_text('{"x":1}\n{"broken":')
    gen=work.parent/'incomplete';result=M.convert(work,gen)
    assert result['converted']==1 and result['failed']==1
    error=list(L.iter_rows(gen/'conversion-errors.jsonl'))[0]
    assert error['offset']==8 and error['raw_sha256']==hashlib.sha256(b'{"broken":').hexdigest()
    assert M.verify(work,gen)['status']=='incomplete'
    with pytest.raises(ValueError):M.activate(work,gen)
    path.write_text('{"x":2}\n')
    with pytest.raises(ValueError,match='SOURCE_CHANGED'):M.convert(work,gen)

def test_streaming_duplicate_keys_and_unknown_storage_version(work):
    ref=next(L.store_json_stream(io.BytesIO(b'{"a":1,"b":2,"a":3}')))
    assert O.open_object(ref)=={'a':3,'b':2}
    path=work/'future.json';path.write_text('{"format":"sigma-record@v999"}')
    assert M.convert(work,work.parent/'future')['failed']==1

def test_live_cursor_ignores_later_backdated_records(work):
    folder=work/'ontology/events'
    for i in range(7):R.publish(folder/f'{i}.json',{'event_id':str(i)})
    first=R.versioned_page(R.iter_records(folder),scope='events',key=lambda r:r['event_id'],limit=2)
    R.publish(folder/'new.json',{'event_id':'25','created_at':'1999-01-01'})
    seen=first['items'];cursor=first['next_cursor']
    while cursor:
        page=R.versioned_page(R.iter_records(folder),scope='events',key=lambda r:r['event_id'],limit=2,cursor=cursor)
        assert page['total']==7
        seen+=page['items'];cursor=page['next_cursor']
    assert [r['event_id'] for r in seen]==[str(i) for i in range(7)]

def test_durable_errors_page_without_memory_retention(work):
    for i in range(105):S.record_error('job','test',f'error {i}')
    first=S.errors('job');assert len(first['records'])==50 and first['total']==105
    assert first['fallback_state']['retained_count']==0
    second=S.errors('job',cursor=first['next_cursor'],limit=100)
    assert len(second['records'])==55 and second['complete']
    assert len({r['error_id'] for r in first['records']+second['records']})==105

def test_partial_cli_output_is_preserved_after_parse_failure(work):
    spool=P.Spool('failed-attempt');raw=b'{"result":"partial \xff'
    spool.accept(raw,b'bad'*10000)
    assert len(spool.tail())==2000
    with pytest.raises(ValueError):spool.result()
    archive=spool.capture()
    assert archive['stdout']['sha256']==hashlib.sha256(raw).hexdigest()
    assert archive['stdout']['bytes']==len(raw)
    assert not spool.folder.exists()
    saved=R.load(work/'ontology/runtime_inputs/failed-attempt.stdio.json')
    assert saved['stdout']['sha256']==archive['stdout']['sha256']

def test_log_prefix_accepts_append_but_detects_in_place_edit(work):
    path=work/'log.jsonl';path.write_text('{"x":1}\n')
    view=L.LogView(path)
    with path.open('a') as stream:stream.write('{"x":2}\n')
    assert list(view)==[{'x':1}]
    with path.open('r+b') as stream:stream.seek(5);stream.write(b'9')
    with pytest.raises(ValueError,match='PREFIX_CHANGED'):list(view)

def test_calculation_aliases_share_changes_until_detached(work):
    import copy
    original=O.freeze({'nested':{'a':1,'b':2},'rows':[{'n':1},{'n':2}]})
    value=copy.deepcopy(original)
    a=value['nested'];b=value['nested'];a['a']=3;b['b']=4
    assert value['nested']=={'a':3,'b':4}
    value['nested']={'replacement':True};a['a']=99
    assert value['nested']=={'replacement':True}
    rows=value['rows'];first=rows[0];second=rows[0]
    first['n']=5;second['extra']=6
    assert rows[0]=={'n':5,'extra':6}
    rows.pop(0);first['n']=100
    assert rows==[{'n':2}] and original['nested']=={'a':1,'b':2}

def test_migrate_legacy_snapshot_with_float_and_literal_ref(work):
    import snapshot_store as V1
    payload={'price':1.25,'raw':{'text':'legacy'*3000},V1.REF:'literal-input'}
    path=work/'ontology/legacy.json'
    path.write_text(json.dumps(V1.encode(payload,base=work/'ontology')))
    gen=work.parent/'legacy-generation'
    assert M.convert(work,gen)['status']=='converted'
    assert M.verify(work,gen)['status']=='verified'
    assert R.load(gen/'ontology/legacy.json')==payload

def test_proposal_history_is_reachable_without_inline_full_output(work,monkeypatch):
    import mcp_server as M,learning_proposals as LP,runtime_inputs
    monkeypatch.setenv('RUN_MODE','proposal_review');monkeypatch.setenv('PROPOSAL_RUN_ID','proposal-run')
    monkeypatch.setattr(runtime_inputs,'record_tool_call',lambda *a,**k:None)
    data=O.freeze({'proposal_run_id':'proposal-run','run_status':'complete','submission_status':'accepted',
        'proposals':[{'proposal_id':'p','reason':'한글'*400000}],
        'submission_history':[{'submission_id':'s','raw_submission':'x'*2000000}],
        'ai_attempts':[],'ai_results':[],'errors':[]})
    monkeypatch.setattr(LP,'get_learning_proposals',lambda *a:data)
    page=M.get_learning_proposals('proposal-run')
    assert O.logical_size(page)<=R.PAGE_BYTES and 'submission_history' not in page
    assert page['counts']['submission_history']==1 and not page['complete']
    history=M.get_learning_proposals('proposal-run',section='submission_history')
    assert history['submission_history'][0]['submission_id']=='s'
    detail=M.get_learning_proposals('proposal-run',section='submission_history',detail_id='s')
    assert 'raw_submission' in detail['item']['_detail_links']
    chunk=M.get_learning_proposals('proposal-run',section='submission_history',detail_id='s',field='raw_submission')
    assert chunk['encoding']=='base64' and chunk['next_offset']==256*1024
    monkeypatch.setenv('PROPOSAL_RUN_ID','different')
    assert M.get_learning_proposals('proposal-run',section='submission_history',detail_id='s')['error_code']=='PROPOSAL_SCOPE_MISMATCH'

def test_sigma_contract_publishes_independent_matching_summaries(work,monkeypatch):
    import logger
    path=work/'runs.jsonl';monkeypatch.setattr(C,'RUNS',str(path))
    monkeypatch.setattr(logger,'_write_all',lambda note:None)
    rows=[{'time':'2026-11-13 10:00','kind':'decide','run_attempt_id':'actual-a','ok':True,'usage':{'input_tokens':7},'sec':3},
          {'time':'2026-11-13 10:05','kind':'decide','run_attempt_id':'actual-b','ok':False,'usage':{'input_tokens':2}}]
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    logger.write_all()
    # An external consumer can read these with the standard JSON library only.
    runtime=json.loads((work/'runtime_summary.json').read_text());daily=json.loads((work/'daily_summary.json').read_text())
    assert runtime['schema_version']==daily['schema_version']==2
    assert runtime['complete'] and daily['complete'] and runtime['source_watermark']==daily['source_watermark']
    assert runtime['latest']['decide']['run_attempt_id']=='actual-b'
    assert daily['items'][0]['runs']==2 and daily['items'][0]['seconds_known_runs']==1
    assert daily['items'][0]['usage']=={'input_tokens':9}
    assert '$sigma_object_ref' not in json.dumps([runtime,daily])
    path.write_text('{truncated')
    logger.write_all()
    assert not json.loads((work/'runtime_summary.json').read_text())['complete']

def test_actual_prep_close_operations_never_invent_ai_usage(work,monkeypatch):
    import runtime_inputs as RI
    monkeypatch.setattr(C,'RUNS',str(work/'runs.jsonl'))
    with RI.operation('prep') as state:state['source_process_ok']=True
    with pytest.raises(RuntimeError):
        with RI.operation('close'):raise RuntimeError('offline failure')
    runtime=json.loads((work/'runtime_summary.json').read_text());daily=json.loads((work/'daily_summary.json').read_text())
    assert runtime['operations']['prep']['status']=='completed'
    assert runtime['operations']['close']['status']=='failed'
    assert runtime['operation_watermark']['records']==4
    assert runtime['latest']=={} and daily['items']==[]

@pytest.mark.parametrize('kind',['prep','close'])
def test_runner_non_ai_operation_records_actual_process_result(work,monkeypatch,kind):
    import importlib.util
    monkeypatch.setattr(C,'load_env',lambda:{})
    monkeypatch.setattr(C,'RUNS',str(work/'runs.jsonl'))
    spec=importlib.util.spec_from_file_location('storage_runner_'+kind,Path(__file__).resolve().parents[1]/'run.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    monkeypatch.setattr(runner,'refresh_account',lambda:None)
    monkeypatch.setattr(runner,'refresh_executions',lambda:None)
    monkeypatch.setattr(runner,'py',lambda *args:False)
    monkeypatch.setattr(runner,'publish',lambda *args:None)
    monkeypatch.setattr(runner,'score_close',lambda *args,**kwargs:None)
    monkeypatch.setattr(sys,'argv',['run.py',kind])
    runner.main()
    runtime=json.loads((work/'runtime_summary.json').read_text())
    assert runtime['operations'][kind]['status']=='failed'

def test_multiple_domain_reads_share_one_request_cache(work):
    import trace_query
    folder=work/'ontology/events'
    for i in range(3):R.publish(folder/f'{i}.json',{'id':i,'raw':'cache'*10000})
    with O.scope() as resolver:
        reader=trace_query.Reader('2026-11-14T00:00:00+09:00')
        assert reader.resolver is resolver
        for i in range(3):assert reader.read(f'events/{i}.json')._resolver is resolver
        first=list(R.iter_records(folder));second=list(R.iter_records(folder))
        assert all(row._resolver is resolver for row in first+second)
        assert resolver.used<=64*1024*1024
