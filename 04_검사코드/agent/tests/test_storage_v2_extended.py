import copy,json,sys,hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import common as C,record_store as R,storage_objects as O,storage_index as I,log_store as L
import snapshot_store as V1,storage_errors as E,trace_pages as T

@pytest.fixture
def work(tmp_path,monkeypatch):
    monkeypatch.setattr(C,'WORK',str(tmp_path))
    monkeypatch.setattr(C,'BRIEFING',str(tmp_path/'briefing.json'))
    return tmp_path

def test_concurrent_idempotency_and_conflicting_identity(work):
    path=work/'ontology/events/one.json'
    def writer(i):
        try:R.publish(path,{'id':1,'payload':'x'*100000},exclusive=False);return True
        except Exception:return False
    with ThreadPoolExecutor(max_workers=8) as pool:assert all(pool.map(writer,range(16)))
    assert R.load(path)['id']==1
    with pytest.raises(ValueError):R.publish(path,{'id':2},exclusive=False)

def test_publication_resume_after_interrupted_commit(work,monkeypatch):
    path=work/'ontology/events/one.json';publish=O.publish_json
    def interrupted(target,*a,**k):
        if str(target).endswith('.commit'):raise OSError('power loss')
        return publish(target,*a,**k)
    monkeypatch.setattr(O,'publish_json',interrupted)
    with pytest.raises(OSError):R.publish(path,{'id':1})
    with pytest.raises(FileNotFoundError):R.load(path)
    monkeypatch.setattr(O,'publish_json',publish)
    with pytest.raises(ValueError):R.publish(path,{'id':2},exclusive=False)
    R.publish(path,{'id':1},exclusive=False)
    assert R.load(path)=={'id':1}

def test_index_corruption_missing_and_read_only_fallback(work):
    import sqlite3
    folder=work/'ontology/events'
    R.publish(folder/'a.json',{'event_id':'a','code':'A'})
    R.publish(folder/'b.json',{'event_id':'b','code':'B'})
    db=sqlite3.connect(I.path(work/'ontology'));db.execute('UPDATE records SET metadata=? WHERE path=?',(json.dumps({'code':'B'}),'events/a.json'));db.commit();db.close()
    before={str(p):O.file_fingerprint(p) for p in work.rglob('*') if p.is_file()}
    assert [r['event_id'] for r in R.iter_records(folder,filters={'code':'A'})]==['a']
    assert before=={str(p):O.file_fingerprint(p) for p in work.rglob('*') if p.is_file()}
    I.path(work/'ontology').unlink()
    assert sorted(r['event_id'] for r in R.iter_records(folder))==['a','b']
    assert not I.path(work/'ontology').exists()

def test_symlink_and_unknown_version_rejected(work):
    path=work/'ontology/events/one.json';R.publish(path,{'id':1})
    link=path.with_name('link.json');link.symlink_to(path)
    with pytest.raises(ValueError):R.load(link)
    unknown=path.with_name('future.json');unknown.write_text('{"format":"sigma-record@v99"}')
    with pytest.raises(ValueError):R.load(unknown)

def test_cache_bound_and_independent_scope_roots(work):
    a=work/'a/ontology';b=work/'b/ontology';ra=O.put_object({'text':'x'*100000},base=a);rb=O.put_object({'n':2},base=b)
    resolver=O.Resolver(a,budget=32768);assert O.open_object(ra,resolver=resolver)['text']=='x'*100000
    assert resolver.used<=32768
    with O.scope(a):assert O.open_object(rb,base=b)['n']==2

def test_derived_unmentioned_is_equivalent_and_closure_checked(work):
    catalog=[{'evidence_id':f'e-{i}','value':'x'*5000} for i in range(50)]
    result=O.unmentioned_evidence([('d',catalog)],{('d','e-3')})
    expected=[{'delivery_id':'d','evidence_id':r['evidence_id'],'adoption':'unrecorded'} for r in catalog if r['evidence_id']!='e-3']
    assert result==expected and O.fingerprint(result)==O.fingerprint(expected)
    assert len(O.verify_closure(result._ref))>1

def test_empty_and_whitespace_logs(work):
    path=work/'empty.jsonl';path.write_text('')
    assert list(L.iter_rows(path))==[]
    path.write_text(' \n\t\n');assert list(L.iter_rows(path))==[]

def test_error_registry_100000_failures_is_bounded():
    registry=E.ErrorRegistry()
    for i in range(100000):E.retain(registry,str(i),{'error_id':str(i),'recorded_at':str(i)})
    state=registry.state()
    assert state['unpersisted_count']==100000 and state['retained_count']==1024
    assert state['overflow'] and len(registry)==1024
    assert state['first_at']=='0' and state['last_at']=='99999'

def test_large_details_and_chunks_do_not_expand_unselected_fields(work):
    value=O.freeze({'id':'a','small':7,'raw':'한글'*200000,'events':[{'n':i} for i in range(20000)]})
    view={'view_id':'view','projection':{'traces':{'a':value}}}
    result=T.detail(view,'trace','a')
    assert O.logical_size(result)<=1024*1024 and result['item']['events']==[]
    assert set(result['item']['_detail_links'])=={'raw','events'}
    data=[];offset=0
    import base64
    while True:
        part=T.chunk(view,'trace','a','raw',offset)
        raw=base64.b64decode(part['data']);assert len(raw)<=256*1024;data.append(raw)
        offset=part['next_offset']
        if offset is None:break
    assert b''.join(data)==b''.join(O.field_chunks(value,'raw'))

def test_summary_preserves_unknown_seconds_and_raw_usage(work):
    path=work/'runs.jsonl';rows=[{'time':'2026-11-13T01:00:00','kind':'view','ok':True,'usage':{'input_tokens':2,'cache_read_input_tokens':3}},
                              {'time':'2026-11-13T02:00:00','kind':'view','ok':False,'usage':{'input_tokens':4}}]
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    runtime,daily=L.runtime_summaries(path)
    assert daily['items'][0]['runs']==2 and daily['items'][0]['seconds'] is None
    assert daily['items'][0]['usage']=={'input_tokens':6,'cache_read_input_tokens':3}
    assert runtime['source_watermark']['rows']==2 and len(runtime['latest'])==1

def test_exact_space_hash_and_insertion_order(work):
    value={'z':{'b':1,'a':2},'a':3}
    stored=O.freeze(value)
    assert list(stored)==['z','a'] and list(stored['z'])==['b','a']
    assert b''.join(O.chunks(stored,separators=(', ',': ')))==json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()
    assert json.dumps(stored)==json.dumps(value)


def test_intraday_overlays_stay_flat_and_isolated(work):
    original=O.freeze({'price':100,'source':{'prices':list(range(1000))},'old':1})
    current=original
    for i in range(30):
        edited=copy.deepcopy(current);edited['price']=101+i
        if i==0:del edited['old']
        current=O.freeze(edited)
        body=current._resolver.node(current._ref)
        assert body['base_ref']==original._ref
        assert len(body['overrides'])==1 and body['deleted']==['old']
        assert current['source']['prices']==list(range(1000))
    assert original['price']==100 and original['old']==1
    assert O.fingerprint(current)==O.fingerprint({'price':130,'source':{'prices':list(range(1000))}})
    O.verify_closure(current._ref)


def test_stream_parser_reuses_canonical_payload_layout(work):
    import io
    for n in (12,128,129,300):
        value={'rows':[{'x':i,'nested':['text',None]} for i in range(n)],'text':'original'*10000}
        direct=O.put_object(value)
        streamed=next(L.store_json_stream(io.BytesIO(json.dumps(value).encode())))
        assert direct[O.REF]['sha256']==streamed[O.REF]['sha256']

def test_large_trace_keeps_display_values_and_original_chunk_link(work):
    huge=O.freeze({'target':{'code':'A'},'judgments':[{'judgment_id':'j','confidence_raw':.7,'recommended_action':'매수','confidence_for':'매수',
        'decision':'관측된 조건에 따른 판단','arguments':[{'kind':'hypothesis','text':'미검증 가설','adoption':'reference'}],
        'unmentioned_evidence':[{'id':str(i)} for i in range(500)],'context':'x'*1000000}],
        'execution_context':{'orders':[{'order_attempt_id':'o','path_kind':'dispatched_order','link_status':'linked',
            'summary':{'requested_qty':100,'confirmed_qty':40,'unconfirmed_qty':60,'confirmation_status':'confirmed_partial'},'raw':'x'*1000000}]}})
    view={'view_id':'v','projection':{'traces':{'t':huge}}}
    detail=T.detail(view,'trace','t')
    assert not detail['complete'] and O.logical_size(detail)<=1024*1024
    assert detail['item']['judgments'][0]['confidence_raw']==.7
    assert detail['item']['judgments'][0]['unmentioned_evidence_count']==500
    assert detail['item']['execution_context']['orders'][0]['summary']['confirmed_qty']==40
    assert detail['item']['_detail_links']['execution_context']['preview_only']


def test_large_validation_and_case_keep_selected_summary(work):
    result={'mode':'event_replay','baseline_status':'passed','effect_status':'completed',
            'performance':{'status':'insufficient_data','gaps':['portfolio_required'],'raw':'x'*200000},
            'adoption_eligibility':{'status':'ineligible','reasons':['holdout_missing']},
            'independent_validation':{'status':'unavailable'},'operational_activation':False}
    evaluation={'verdict':'refuted','period_status':'ended','data_status':'complete',
                'first_known_refutation':{'date':'2026-11-10'},'raw':'x'*200000}
    view={'view_id':'v','projection':O.freeze({'validations':[{'result_id':'r','result':result}],
                                            'cases':[{'application_id':'a','evaluation':evaluation}]})}
    validation=T.detail(view,'validation','r')
    assert not validation['complete']
    assert validation['item']['result']['mode']=='event_replay'
    assert validation['item']['result']['baseline_status']=='passed'
    assert validation['item']['result']['adoption_eligibility']==result['adoption_eligibility']
    assert validation['item']['result']['performance']['gaps']==['portfolio_required']
    assert 'raw' not in validation['item']['result']['performance']
    assert validation['item']['_detail_links']['result']['preview_only']
    case=T.detail(view,'case','a')
    assert case['item']['evaluation']['verdict']=='refuted'
    assert case['item']['evaluation']['first_known_refutation']['date']=='2026-11-10'
    assert not case['complete']
