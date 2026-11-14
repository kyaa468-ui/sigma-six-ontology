"""Offline, resumable storage conversion. Default action is a read-only scan."""
from __future__ import annotations
import argparse
import io
import json
from pathlib import Path
import sys
import sqlite3
import os
import shutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import ijson
import log_store as L
import record_store as R
import snapshot_store as V1
import storage_objects as O


def source_files(source):
    source=Path(source)
    if source.is_file():yield source;return
    for path in source.rglob('*'):
        if path.is_symlink():raise ValueError('MIGRATION_SOURCE_SYMLINK')
        if path.name=='storage_generation.json':continue
        if path.is_file() and path.suffix in ('.json','.jsonl') and not any(
            x in path.relative_to(source).parts for x in ('blobs','.venv','__pycache__')):
            yield path


def scan(source):
    count=size=largest=0
    for path in source_files(source):
        length=path.stat().st_size;count+=1;size+=length;largest=max(largest,length)
    physical=sum(p.stat().st_size for p in Path(source).rglob('*') if p.is_file()) if Path(source).is_dir() else size
    return {'schema_version':2,'action':'scan','source':str(Path(source).resolve()),
            'files':count,'source_bytes':size,'largest_file_bytes':largest,
            'physical_source_bytes':physical,'estimated_additional_bytes':physical*2,'available_bytes':shutil.disk_usage(source).free,'estimate':'conservative planning allowance; measured closure is reported after conversion',
            'source_modified':False}


def line_ranges(path):
    start=position=0
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(256*1024),b''):
            offset=0
            while True:
                newline=block.find(b'\n',offset)
                if newline<0:break
                end=position+newline+1
                yield start,end-start;start=end;offset=newline+1
            position+=len(block)
    if start<position:yield start,position-start


def json_reference(path,base):
    # Detect v1's trusted envelope without materializing its logical payload.
    with path.open('rb') as stream:
        header=next(L.iter_json(stream,fields=('format','logical_sha256','logical_bytes')), {})
    if header.get('format')==V1.VERSION:
        with path.open('rb') as stream:packed=next(ijson.items(stream,'payload',use_float=True))
        raw=io.BytesIO(V1.canonical(packed))
        ref=next(L.store_json_stream(raw,base=base,legacy_base=R.base_for(path)))
        actual=O.fingerprint(O.open_object(ref,base=base))
        if actual!=(header['logical_sha256'],header['logical_bytes']):raise ValueError('LEGACY_LOGICAL_HASH_MISMATCH')
        return ref
    if header.get('format')=='sigma-input@v2':return O.put_object(R.load_input(path),base=base)
    if header.get('format')==R.FORMAT:
        return O.put_object(R.load(path),base=base)
    if str(header.get('format','')).startswith(('sigma-record@','sigma-input@','sigma-snapshot@')):
        raise ValueError('UNSUPPORTED_STORAGE_VERSION')
    with path.open('rb') as stream:return next(L.store_json_stream(stream,base=base))


def _convert(source,destination,max_records=None):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if destination==source or destination.is_relative_to(source):raise ValueError('DESTINATION_MUST_BE_OUTSIDE_SOURCE')
    destination.mkdir(parents=True,exist_ok=True);base=destination/'ontology'
    checkpoint=destination/'conversion.jsonl';errors_path=destination/'conversion-errors.jsonl'
    # A checkpoint is append-only; replay verifies each original fingerprint.
    connection=sqlite3.connect(destination/'checkpoint.sqlite3')
    connection.execute('CREATE TABLE IF NOT EXISTS completed (key TEXT PRIMARY KEY, row TEXT NOT NULL)')
    for row in L.iter_rows(checkpoint):
        connection.execute('INSERT OR REPLACE INTO completed VALUES (?,?)',(row['key'],json.dumps(row)))
    connection.commit()
    converted=failed=0;inventory=0;source_count=0;stopped=False
    for path in source_files(source):
        sha,length=O.file_fingerprint(path)
        relative=str(path.relative_to(source)) if source.is_dir() else path.name
        source_count+=1;inventory^=int(__import__('hashlib').sha256((relative+'\0'+sha).encode()).hexdigest(),16)
        original_base=R.base_for(path)
        base=(destination/original_base.relative_to(source)) if source.is_dir() and original_base.is_relative_to(source) else destination/'ontology'
        ranges=line_ranges(path) if path.suffix=='.jsonl' else [(0,length)]
        for offset,size in ranges:
            key=relative+':'+str(offset)
            previous=connection.execute('SELECT row FROM completed WHERE key=?',(key,)).fetchone()
            if previous:
                row=json.loads(previous[0])
                if row['source_sha256']!=sha:raise ValueError('SOURCE_CHANGED_SINCE_CHECKPOINT:'+relative)
                value=R.load(destination/row['record'])
                if O.fingerprint(value)!=(row['logical_sha256'],row['logical_bytes']):raise ValueError('CHECKPOINT_LOGICAL_HASH_MISMATCH')
                converted+=1;continue
            target=(base/'converted_logs'/sha/(str(offset)+'.json') if path.suffix=='.jsonl' else
                    destination/relative if source.is_dir() and (source/'ontology').is_dir() else base/relative)
            try:
                if path.suffix=='.jsonl':
                    with path.open('rb') as stream:
                        stream.seek(offset);bounded=L.PrefixReader(stream,size)
                        refs=iter(L.store_json_stream(bounded,base=base));ref=next(refs,None)
                        if ref is None:continue
                        if next(refs,None) is not None:raise ValueError('MULTIPLE_VALUES_IN_LOG_ROW')
                else:ref=json_reference(path,base)
                value=O.open_object(ref,base=base)
                R.publish(target,value,base=base,kind='migrated',exclusive=False)
                logical_sha,logical_bytes=O.fingerprint(value)
                row={'key':key,'source':relative,'source_sha256':sha,'source_bytes':length,
                     'offset':offset,'bytes':size,'record':str(target.relative_to(destination)),
                     'logical_sha256':logical_sha,'logical_bytes':logical_bytes}
                with checkpoint.open('a',encoding='utf-8') as out:
                    out.write(json.dumps(row,ensure_ascii=False)+'\n');out.flush();os.fsync(out.fileno())
                connection.execute('INSERT INTO completed VALUES (?,?)',(key,json.dumps(row)))
                connection.commit()
                converted+=1
            except (ValueError,OSError,StopIteration) as exc:
                import hashlib
                digest=hashlib.sha256()
                with path.open('rb') as raw:
                    raw.seek(offset);bounded=L.PrefixReader(raw,size)
                    for part in iter(lambda:bounded.read(256*1024),b''):digest.update(part)
                error={'key':key,'source':relative,'offset':offset,'bytes':size,'raw_sha256':digest.hexdigest(),'error':str(exc)}
                with errors_path.open('a',encoding='utf-8') as out:
                    out.write(json.dumps(error,ensure_ascii=False)+'\n');out.flush();os.fsync(out.fileno())
                failed+=1
        if max_records is not None and converted+failed>=max_records:
            stopped=True;break
    connection.close()
    report={'schema_version':2,'action':'convert','converted':converted,'failed':failed,
            'status':'checkpointed' if stopped else 'incomplete' if failed else 'converted','source_modified':False,
            'source':str(source),'source_files':source_count,'source_inventory_sha256':f'{inventory:064x}',
            'destination_bytes':sum(p.stat().st_size for p in destination.rglob('*') if p.is_file())}
    O.publish_json(destination/'conversion-report.json',report,replace=True)
    return report


def convert(source,destination,max_records=None):
    with O.scope(Path(destination).resolve()/'ontology'):
        return _convert(source,destination,max_records)


def verify(source,destination):
    source=Path(source).resolve();destination=Path(destination).resolve();count=0;errors=[];checked={}
    for row in L.iter_rows(destination/'conversion.jsonl'):
        try:
            path=source/row['source'] if source.is_dir() else source
            if str(path) not in checked:checked[str(path)]=O.file_fingerprint(path)[0]
            if checked[str(path)]!=row['source_sha256']:raise ValueError('SOURCE_CHANGED')
            target=destination/row['record'];value=R.load(target)
            if O.fingerprint(value)!=(row['logical_sha256'],row['logical_bytes']):raise ValueError('LOGICAL_HASH_MISMATCH')
            O.verify_closure(R.manifest(target)['object_ref'],base=R.base_for(target))
            count+=1
        except (OSError,ValueError) as exc:errors.append({'key':row['key'],'error':str(exc)})
    conversion=destination/'conversion-report.json'
    prior=json.loads(conversion.read_text()) if conversion.exists() else {}
    if prior.get('status')!='converted' or prior.get('failed') or prior.get('converted')!=count:errors.append({'error':'CONVERSION_INCOMPLETE'})
    inventory=0;source_count=0
    for path in source_files(source):
        sha=O.file_fingerprint(path)[0];relative=str(path.relative_to(source)) if source.is_dir() else path.name
        source_count+=1;inventory^=int(__import__('hashlib').sha256((relative+'\0'+sha).encode()).hexdigest(),16)
    if source_count!=prior.get('source_files') or f'{inventory:064x}'!=prior.get('source_inventory_sha256'):
        errors.append({'error':'SOURCE_INVENTORY_CHANGED'})
    report={'schema_version':2,'action':'verify','source':str(source),'status':'verified' if not errors else 'incomplete','verified_records':count,'errors':errors}
    O.publish_json(destination/'verification.json',report,replace=True)
    return report


def export(source,destination):
    """An explicit standalone logical JSON, with no storage references."""
    source=Path(source);destination=Path(destination)
    if source.resolve()==destination.resolve():raise ValueError('EXPORT_MUST_NOT_OVERWRITE_SOURCE')
    value=R.load_input(source) if source.name in ('briefing.json','prep_rows.json') else R.load(source)
    digest,size=O.publish_json(destination,value)
    return {'action':'export','logical_sha256':digest,'bytes':size,'destination':str(destination),'source_modified':False}


def activate(work,generation):
    """Explicit offline pointer switch; caller must stop all writers first."""
    work=Path(work);generation=Path(generation).resolve()
    report=json.loads((generation/'verification.json').read_text())
    if report.get('status')!='verified':raise ValueError('GENERATION_NOT_VERIFIED')
    if verify(report['source'],generation)['status']!='verified':raise ValueError('GENERATION_CHANGED_AFTER_VERIFICATION')
    pointer=work/'storage_generation.json'
    previous=json.loads(pointer.read_text()) if pointer.exists() else {'version':1,'path':str(work.resolve())}
    value={'version':2,'path':str(generation),'previous':previous}
    O.publish_json(pointer,value,replace=True);return value


def rollback(work):
    pointer=Path(work)/'storage_generation.json';current=json.loads(pointer.read_text())
    if 'previous' not in current:raise ValueError('NO_PREVIOUS_GENERATION')
    # Do not strand post-switch events: an explicit compatibility export is
    # required before reversing a generation with newly published records.
    gen=Path(current['path']);report=json.loads((gen/'verification.json').read_text())
    expected={str((gen/row['record']).resolve()) for row in L.iter_rows(gen/'conversion.jsonl')}
    metadata={'conversion.jsonl','conversion-errors.jsonl','conversion-report.json','verification.json'}
    actual={str(p.resolve()) for p in source_files(gen) if str(p.relative_to(gen)) not in metadata}
    if not actual <= expected:
        raise ValueError('NEW_EVENTS_REQUIRE_COMPATIBILITY_EXPORT')
    O.publish_json(pointer,current['previous'],replace=True);return current['previous']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--action',choices=('scan','convert','verify','export','activate','rollback'),default='scan')
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--max-records',type=int,help='Stop after a completed source file for a checkpoint/resume rehearsal')
    args=parser.parse_args()
    if args.action not in ('scan','rollback') and args.destination is None:parser.error('--destination is required')
    result=(convert(args.source,args.destination,args.max_records) if args.action=='convert' else
            globals()[args.action](args.source) if args.action in ('scan','rollback') else globals()[args.action](args.source,args.destination))
    print(json.dumps(result,ensure_ascii=False))
    return 1 if result.get('status')=='incomplete' else 0


if __name__=='__main__':raise SystemExit(main())
