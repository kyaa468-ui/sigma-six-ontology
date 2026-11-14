"""v2 physical sharing, logical isolation, publication and selected reads."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import common as C
import snapshot_store as old
import storage_objects as O
import record_store as R
import log_store as L


@pytest.fixture
def work(tmp_path,monkeypatch):
    monkeypatch.setattr(C,'WORK',str(tmp_path))
    monkeypatch.setattr(C,'BRIEFING',str(tmp_path/'briefing.json'))
    return tmp_path


def test_canonical_bytes_preserve_numbers_unicode_and_escape(work):
    data={'null':None,'zero':0,'a':['한글\\\n"',1.23456789,-0.0,1e-17], 'long':'한글"\\'*20000}
    assert b''.join(O.chunks(data))==old.canonical(data)
    with pytest.raises(ValueError):O.fingerprint({'x':float('nan')})


def test_references_share_payload_but_copies_do_not_share_mutation(work):
    value={'rows':[{'index':i,'values':[1,2,3]} for i in range(300)],'text':'same payload'*100000}
    original=O.freeze(value)
    refs=[O.put_object(original) for _ in range(100)]
    assert all(x==refs[0] for x in refs)
    before=set(old.root().glob('*.json'))
    twin=copy.deepcopy(original)
    twin['rows'][150]['values'].append(99)
    assert original['rows'][150]['values']==[1,2,3]
    assert twin['rows'][150]['values']==[1,2,3,99]
    assert set(old.root().glob('*.json'))==before
    assert json.loads(json.dumps(original))==value
    assert O.fingerprint(original)==O.fingerprint(value)


def test_external_ref_shape_is_literal(work):
    value={O.REF:{'version':2,'kind':'secret','sha256':'f'*64,'bytes':42}}
    assert O.materialize(O.freeze(value))==value


def test_selected_read_does_not_open_unrelated_payload(work):
    value={'wanted':{'price':10},'unused':{'text':'never open '*50000}}
    target=work/'ontology/judgments/test.json';R.publish(target,value)
    dependencies={}
    row=R.load(target,fields=['wanted'],dependencies=dependencies)
    assert row['wanted']['price']==10
    assert len(dependencies)<=2


def test_commit_required_and_corruption_never_repairs(work):
    path=work/'ontology/records/one.json';R.publish(path,{'x':'data'*20000})
    marker=R.commit_path(path);saved=marker.read_bytes();marker.unlink()
    with pytest.raises(FileNotFoundError):R.load(path)
    assert not marker.exists()
    marker.write_bytes(saved)
    value=R.manifest(path);blob=old._safe_path(value['object_ref'][O.REF]['sha256'])
    blob.write_text('{}')
    with pytest.raises(old.SnapshotError,match='HASH_MISMATCH'):R.load(path)
    assert blob.read_text()=='{}'


def test_v1_and_v2_reader_and_idempotency(work):
    old_path=work/'ontology/records/old.json';old_path.parent.mkdir(parents=True)
    payload={'nested':{'text':'old'*5000}}
    old_path.write_text(json.dumps(old.encode(payload)))
    assert R.load(old_path)==payload
    new=old_path.with_name('new.json');R.publish(new,payload,exclusive=False)
    R.publish(new,payload,exclusive=False)
    with pytest.raises(ValueError,match='conflict'):R.publish(new,{'x':1},exclusive=False)


def test_latest_input_is_explicit_and_historical_revision_survives(work):
    C.write_briefing({'candidates':[{'code':'A','price':10}]})
    pointer=C.read_json(C.BRIEFING)
    assert pointer['format']=='sigma-input@v2'
    first=C.load_briefing()
    C.write_briefing({'candidates':[{'code':'A','price':20}]})
    assert first['candidates'][0]['price']==10
    assert C.load_briefing()['candidates'][0]['price']==20


def test_log_projection_prefix_and_incremental_capture(work):
    path=work/'runs.jsonl'
    rows=[{'time':str(i),'raw':{'text':'x'*30000},'tokens':i} for i in range(20)]
    with path.open('w') as out:
        for row in rows:out.write(json.dumps(row)+'\n')
    assert list(L.iter_rows(path,fields=('time','tokens')))==[{'time':str(i),'tokens':i} for i in range(20)]
    prefix=L.view(path);captured=L.capture(prefix)
    path.write_text('{}\n')
    assert captured[10]==rows[10]
    assert len(captured)==20
    assert captured[-1]==rows[-1]


def test_cursor_union_and_filter_binding(work):
    data=[{'id':f'{i:04}','value':i} for i in reversed(range(137))]
    seen=[];cursor=None
    while True:
        result=R.page(iter(data),cursor=cursor,limit=23,scope='frozen-view')
        assert result['total']==137
        seen.extend(x['id'] for x in result['items'])
        cursor=result['next_cursor']
        if cursor is None:break
    assert seen==[f'{i:04}' for i in range(137)]
    cursor=R.page(iter(data),limit=1,scope='one')['next_cursor']
    with pytest.raises(ValueError):R.page(iter(data),cursor=cursor,scope='two')
