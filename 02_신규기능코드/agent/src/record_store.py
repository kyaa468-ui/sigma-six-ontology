"""Trusted v1/v2 record adapters. Ordinary JSON readers never resolve refs."""
from __future__ import annotations
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys

import snapshot_store as legacy
import storage_objects as objects
import storage_index

FORMAT = 'sigma-record@v2'
PAGE_BYTES = 1024 * 1024
CHUNK_BYTES = 256 * 1024


def base_for(path):
    path = Path(path).absolute()
    for parent in path.parents:
        if parent.name == 'ontology': return parent
    import common as C
    for parent in path.parents:
        if (parent/'ontology').is_dir():return parent/'ontology'
    if path.is_relative_to(Path(C.WORK).absolute()):return C.ontology_root()
    return path.parent / 'ontology'


def _safe(path, base):
    path, base = Path(path).absolute(), Path(base).absolute()
    boundary = base.parent
    if not path.resolve().is_relative_to(boundary.resolve()):
        raise legacy.SnapshotError('RECORD_OUTSIDE_WORK')
    current = path
    while current != boundary:
        if current.is_symlink(): raise legacy.SnapshotError('RECORD_SYMLINK_FORBIDDEN')
        current = current.parent
    return path


def commit_path(path): return Path(str(path) + '.commit')


def manifest(path, *, base=None):
    base = base or base_for(path)
    path = _safe(path, base)
    with path.open(encoding='utf-8') as stream: value = json.load(stream)
    if isinstance(value, dict) and value.get('format') == FORMAT:
        commit = _safe(commit_path(path), base)
        with commit.open(encoding='utf-8') as stream: marker = json.load(stream)
        if objects.file_fingerprint(path) != (marker.get('manifest_sha256'), marker.get('manifest_bytes')):
            raise legacy.SnapshotError('RECORD_COMMIT_MISMATCH')
        if marker.get('object_ref') != value.get('object_ref'):
            raise legacy.SnapshotError('RECORD_COMMIT_REFERENCE_MISMATCH')
        objects.ref_info(value['object_ref'])
    elif isinstance(value, dict) and str(value.get('format', '')).startswith('sigma-record@'):
        raise legacy.SnapshotError('UNSUPPORTED_STORAGE_VERSION')
    elif commit_path(path).exists():
        raise legacy.SnapshotError('RECORD_FORMAT_CHANGED_AFTER_COMMIT')
    return value


def is_v2(path):
    value = manifest(path)
    return isinstance(value, dict) and value.get('format') == FORMAT


def load(path, *, base=None, resolver=None, fields=None, dependencies=None):
    base = base or base_for(path)
    value = manifest(path, base=base)
    if isinstance(value, dict) and value.get('format') == FORMAT:
        resolver = resolver or objects.resolver_for(base, dependencies=dependencies)
        with resolver.tracking(dependencies):
            result=objects.open_object(value['object_ref'],base=base,resolver=resolver,fields=fields)
        if isinstance(result,(objects.ObjectDict,objects.ObjectList)):
            result._record_order=commit_path(path).stat().st_mtime_ns
        return result
    result = legacy.decode(value, base=base, dependencies=dependencies)
    return {k: result[k] for k in fields if k in result} if fields is not None else result


def _publish_unlocked(path, value, *, base=None, kind='record', exclusive=True):
    base = base or base_for(path)
    path = _safe(path, base)
    sha, size = objects.fingerprint(value)
    if path.exists():
        if exclusive: raise FileExistsError(str(path))
        if not commit_path(path).exists():
            with path.open(encoding='utf-8') as stream:pending=json.load(stream)
            if pending.get('format')==FORMAT:
                if (pending.get('logical_sha256'),pending.get('logical_bytes'))!=(sha,size):
                    raise ValueError('Immutable record key conflict: '+str(path))
                objects.verify_closure(pending['object_ref'],base=base)
                if objects.fingerprint(objects.open_object(pending['object_ref'],base=base))!=(sha,size):
                    raise ValueError('Pending record logical hash mismatch')
                digest,length=objects.file_fingerprint(path)
                objects.publish_json(commit_path(path),{'format':'sigma-commit@v2','manifest_sha256':digest,'manifest_bytes':length,'object_ref':pending['object_ref']})
                storage_index.add(base,str(path.relative_to(base)),digest,pending['projection'])
        previous = manifest(path, base=base)
        if previous.get('format') == FORMAT:
            same = previous['logical_sha256'] == sha and previous['logical_bytes'] == size
        else: same = objects.fingerprint(legacy.decode(previous, base=base)) == (sha, size)
        if not same: raise ValueError('Immutable record key conflict: ' + str(path))
        return path
    ref = objects.put_object(value, kind=kind, base=base)
    objects.verify_closure(ref, base=base)
    metadata = storage_index.projection(value)
    body = {'format': FORMAT, 'object_ref': ref, 'logical_sha256': sha,
            'logical_bytes': size, 'projection': metadata}
    digest, length = objects.publish_json(path, body)
    objects.publish_json(commit_path(path), {'format': 'sigma-commit@v2',
        'manifest_sha256': digest, 'manifest_bytes': length, 'object_ref': ref})
    try:
        if path.is_relative_to(base):storage_index.add(base,str(path.relative_to(base)),digest,metadata)
    except Exception as exc:
        # A committed record remains authoritative; queries scan unindexed files.
        print('Storage index incomplete: ' + str(exc), file=sys.stderr)
    return path


def publish(path, value, *, base=None, kind='record', exclusive=True):
    """Serialize publication, including a manifest's short pre-commit interval."""
    import sqlite3
    base=Path(base or base_for(path))
    locks=base/'locks';locks.mkdir(parents=True,exist_ok=True)
    lock=locks/'storage_publish.sqlite3'
    if lock.is_symlink():raise legacy.SnapshotError('RECORD_LOCK_SYMLINK_FORBIDDEN')
    connection=sqlite3.connect(lock,timeout=30)
    try:
        connection.execute('BEGIN IMMEDIATE')
        with objects.scope(base):
            result=_publish_unlocked(path,value,base=base,kind=kind,exclusive=exclusive)
        connection.commit()
        return result
    finally:connection.close()


def write_input(path, value, *, kind='engine_input'):
    """Latest input pointer: immutable revision then one atomic pointer replace."""
    base = base_for(path)
    sha, _ = objects.fingerprint(value)
    revision = Path(base) / 'inputs' / Path(path).stem / (sha + '.json')
    publish(revision, value, base=base, kind=kind, exclusive=False)
    pointer = {'format': 'sigma-input@v2', 'record': str(revision.relative_to(base)),
               'logical_sha256': sha}
    objects.publish_json(path, pointer, replace=True)
    return pointer


def load_input(path, *, fields=None):
    base = base_for(path)
    with open(path, encoding='utf-8') as stream: value = json.load(stream)
    if isinstance(value,dict) and value.get('format')==FORMAT:
        return load(path,base=base,fields=fields)
    if isinstance(value, dict) and value.get('format') == 'sigma-input@v2':
        record = _safe(Path(base) / value['record'], base)
        info = manifest(record, base=base)
        if info.get('logical_sha256') != value.get('logical_sha256'):
            raise legacy.SnapshotError('INPUT_REVISION_MISMATCH')
        return load(record, base=base, fields=fields)
    if isinstance(value, dict) and str(value.get('format','')).startswith('sigma-input@'):
        raise legacy.SnapshotError('UNSUPPORTED_STORAGE_VERSION')
    return {k:value[k] for k in fields if k in value} if fields is not None else value


def iter_records(folder, *, filters=None, fields=None, errors=None, base=None, recursive=True, with_path=False):
    """Filter manifest metadata first. Legacy files use the same bounded walk.

    The filesystem pass also discovers commits missed by the derived index.
    Each selected record is verified independently; no successful empty result
    is substituted for an unreadable directory or an incomplete publication.
    """
    base = base or base_for(Path(folder) / 'record.json')
    filters = {k:v for k,v in (filters or {}).items() if v is not None}
    resolver = objects.resolver_for(base)
    prefix=str(Path(folder).relative_to(base))+'/'
    def candidates():
        # Metadata remains verified against each small committed manifest. The
        # fallback scans metadata only, never restores unselected originals.
        indexed={}
        try:
            for relative,sha,metadata in storage_index.entries(base,prefix):
                candidate=Path(base)/relative
                if not recursive and candidate.parent!=Path(folder):continue
                if not candidate.is_file():
                    if errors is not None:errors.append({'record':relative,'error':'INDEX_SOURCE_MISSING'})
                    continue
                if any(k in metadata and metadata[k]!=v for k,v in filters.items()):continue
                actual=manifest(candidate,base=base)
                if objects.file_fingerprint(candidate)[0]!=sha or actual.get('projection')!=metadata:continue
                yield candidate
        except Exception as exc:
            if errors is not None:errors.append({'error':'INDEX_FALLBACK','reason':str(exc)})
        for candidate in storage_index.files(folder,recursive=recursive):
            relative=str(candidate.relative_to(base))
            try:
                entry=storage_index.entry(base,relative)
                actual=manifest(candidate,base=base)
                valid=(entry is not None and objects.file_fingerprint(candidate)[0]==entry[0]
                       and actual.get('projection')==entry[1])
                if valid:continue
            except Exception:pass
            yield candidate
    for path in candidates():
        try:
            meta = manifest(path, base=base)
            if isinstance(meta, dict) and meta.get('format') == FORMAT:
                projection = meta['projection']
                if any(k in projection and projection[k] != v for k,v in filters.items()): continue
            value = load(path, base=base, resolver=resolver)
            row = value.get('record', value) if isinstance(value, dict) else value
            if filters and (not isinstance(row, dict) or any(row.get(k,(row.get('payload') or {}).get(k)) != v for k,v in filters.items())): continue
            result={k:row[k] for k in fields if k in row} if fields is not None else value
            yield (path,result) if with_path else result
        except (OSError, ValueError) as exc:
            if errors is None: raise
            errors.append({'record': str(path.relative_to(base)), 'error': str(exc)})


def cursor_cutoff(cursor, scope):
    import time
    if not cursor:return time.time_ns()
    try:
        state=json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if state['scope']!=scope:raise ValueError('CURSOR_SCOPE_MISMATCH')
        return state['cutoff_ns']
    except Exception as exc:raise ValueError('INVALID_CURSOR') from exc


def page(rows, *, cursor=None, limit=50, scope='', key=None, budget=PAGE_BYTES-8192, reverse=False):
    """Stable keyset pages. Callers bind scope to immutable view and filters."""
    limit = min(100, max(1, int(limit)))
    import time
    after = None;cutoff_ns=time.time_ns()
    if cursor:
        try:
            state = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if state['scope'] != scope: raise ValueError('CURSOR_SCOPE_MISMATCH')
            after = state['after'];cutoff_ns=state.get('cutoff_ns',cutoff_ns)
        except Exception as exc: raise ValueError('INVALID_CURSOR') from exc
    key = key or (lambda row: str(row.get('event_id') or row.get('judgment_id') or row.get('id')))
    # Keep at most one page in memory, even for a legacy unordered iterator.
    import bisect
    selected, total = [], 0
    for row in rows:
        if row.get('_record_order',getattr(row,'_record_order',0))>cutoff_ns:continue
        total += 1; identifier = key(row)
        if after is not None and (identifier >= after if reverse else identifier <= after):continue
        selected.append((identifier,row))
        selected.sort(key=lambda item:item[0],reverse=reverse)
        if len(selected)>limit+1:selected.pop()
    items, used, last = [], 512, after
    for identifier, row in selected[:limit]:
        size = objects.logical_size(row)
        if used + size > budget:
            if not items:
                row = {'id': identifier, 'status':'detail_required', 'logical_bytes':size, 'detail_ref':{'id':identifier,'scope':scope}}
                size = objects.fingerprint(row)[1]
            else: break
        if '_record_order' in row:row={k:v for k,v in row.items() if k!='_record_order'}
        items.append(row); used += size; last = identifier
    more = bool(selected and last != selected[-1][0])
    next_cursor = base64.urlsafe_b64encode(json.dumps({'scope':scope,'after':last,'cutoff_ns':cutoff_ns}).encode()).decode() if more else None
    return {'schema_version':2, 'items':items, 'total':total, 'limit':limit,
            'next_cursor':next_cursor,'page_complete':not more,
            'complete':not more and not any(row.get('status')=='detail_required' for row in items)}


def compact_detail(row, reference):
    if objects.logical_size(row)<=256*1024:return row
    selected={};remaining=200*1024
    if isinstance(row,dict):
        for k in row:
            size=objects.field_size(row,k)
            if size<=min(16384,remaining):selected[k]=row[k];remaining-=size
    return {**selected,'detail_required':True,'detail_ref':reference}


def versioned_page(rows, *, scope, key, cursor=None, limit=50, reverse=False):
    def entries():
        for row in rows:
            identifier=key(row)
            order=row.get('_record_order',getattr(row,'_record_order',0))
            if '_record_order' in row:row={k:v for k,v in row.items() if k!='_record_order'}
            yield {'id':identifier,'value':compact_detail(row,{'scope':scope,'id':identifier}),
                   '_record_order':order}
    result=page(entries(),scope=scope,cursor=cursor,limit=limit,reverse=reverse)
    result['items']=[row.get('value',row) for row in result['items']]
    result['complete']=result['complete'] and not any(row.get('detail_required') for row in result['items'])
    return result


def sequence_page(rows, *, scope, cursor=None, limit=50, offset=0, budget=PAGE_BYTES-8192):
    """Page an immutable ordered relation; old offsets remain an explicit adapter."""
    def entries():
        for index,row in enumerate(rows):
            if not cursor and index<offset:continue
            if objects.logical_size(row)>256*1024:
                if isinstance(row,dict):
                    selected={};remaining=200*1024
                    for k in row:
                        size=objects.field_size(row,k)
                        if size<=min(16384,remaining):selected[k]=row[k];remaining-=size
                else:selected={}
                row={**selected,'detail_required':True,'detail_ref':{'scope':scope,'index':index}}
            yield {'id':f'{index:016}','value':row}
    result=page(entries(),cursor=cursor,limit=limit,scope=scope,budget=budget)
    result['items']=[row['value'] if 'value' in row else row for row in result['items']]
    result['total']=len(rows)
    result['complete']=result['complete'] and not any(isinstance(row,dict) and row.get('detail_required') for row in result['items'])
    return result
