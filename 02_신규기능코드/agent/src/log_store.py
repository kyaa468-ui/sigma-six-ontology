"""Bounded JSON/JSONL parsing, frozen log prefixes and small runtime summaries."""
from __future__ import annotations
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path

import ijson
import storage_objects as objects

_SCOPE = ContextVar('journal_read_scope', default=None)


class PrefixReader:
    def __init__(self, stream, length): self.stream, self.remaining = stream, length
    def read(self, size=-1):
        size = self.remaining if size < 0 else min(size, self.remaining)
        value = self.stream.read(size); self.remaining -= len(value); return value
    def readinto(self, buffer):
        value = self.read(len(buffer)); buffer[:len(value)] = value; return len(value)


def iter_json(stream, *, fields=None):
    """Select top-level fields without materializing skipped subtrees."""
    if fields is None:
        yield from ijson.items(stream, '', multiple_values=True, use_float=True)
        return
    selected = set(fields)
    row, builder, selected_depth, depth, current = None, None, 0, 0, None
    for prefix, event, value in ijson.parse(stream, multiple_values=True, use_float=True):
        if event == 'start_map' and depth == 0: row = {}
        if event == 'map_key' and depth == 1: current = value
        if builder is not None:
            builder.event(event, value)
        elif depth == 1 and current in selected and event != 'map_key':
            if event in ('start_map','start_array'):
                builder = ijson.ObjectBuilder(); builder.event(event,value); selected_depth = depth
            elif event not in ('end_map','end_array'):
                row[current] = value
        if event in ('start_map','start_array'): depth += 1
        elif event in ('end_map','end_array'):
            depth -= 1
            if builder is not None and depth == selected_depth:
                row[current] = builder.value; builder = None
            if depth == 0:
                if row is not None: yield row
                row = None


def iter_rows(path, *, fields=None, size=None):
    try:
        with open(path, 'rb') as stream:
            length = os.fstat(stream.fileno()).st_size if size is None else size
            probe=stream.read(min(length,65536))
            while probe and not probe.strip() and stream.tell()<length:
                probe=stream.read(min(65536,length-stream.tell()))
            if not probe.strip():return
            stream.seek(0)
            yield from iter_json(PrefixReader(stream,length), fields=fields)
    except FileNotFoundError: return


class LogView(list):
    """Read-only replayable prefix. This view never retains all decoded rows."""
    def __init__(self, path, fields=None):
        super().__init__(); self.path=Path(path); self.fields=fields
        try:
            st=self.path.stat(); self.identity=(st.st_dev,st.st_ino); self.size=st.st_size
        except FileNotFoundError: self.identity=None; self.size=0
        self.digest=self._prefix_hash() if self.identity else None
        self.stamp=(st.st_mtime_ns,st.st_ctime_ns) if self.identity else None
    def _prefix_hash(self):
        digest=hashlib.sha256()
        with self.path.open('rb') as stream:
            reader=PrefixReader(stream,self.size)
            for block in iter(lambda:reader.read(256*1024),b''):digest.update(block)
        return digest.hexdigest()
    def _verify(self):
        if self.identity is None: return
        st=self.path.stat()
        if (st.st_dev,st.st_ino)!=self.identity or st.st_size<self.size:
            raise ValueError('LOG_PREFIX_REPLACED')
        if (st.st_mtime_ns,st.st_ctime_ns)!=self.stamp and self._prefix_hash()!=self.digest:
            raise ValueError('LOG_PREFIX_CHANGED')
    def __iter__(self):
        self._verify(); return iter_rows(self.path, fields=self.fields, size=self.size)
    def __len__(self): return sum(1 for _ in self)
    def __bool__(self): return next(iter(self),None) is not None
    def __getitem__(self,index):
        if isinstance(index,slice):
            if (index.start or 0)<0 and index.stop is None and index.step in (None,1):
                return list(deque(self,maxlen=-index.start))
            start,stop,step=index.indices(len(self))
            return [v for i,v in enumerate(self) if start<=i<stop and (i-start)%step==0]
        if index<0:
            values=deque(self,maxlen=-index)
            if len(values)<-index: raise IndexError(index)
            return values[0]
        for i,value in enumerate(self):
            if i==index:return value
        raise IndexError(index)
    def __reversed__(self):
        # Reverse line streaming keeps only the current line, capped at 1MiB.
        if not self.path.exists():return
        self._verify()
        with self.path.open('rb') as stream:
            position=self.size; pending=b''
            while position:
                count=min(65536,position);position-=count;stream.seek(position)
                parts=(stream.read(count)+pending).split(b'\n');pending=parts.pop(0)
                for raw in reversed(parts):
                    if raw.strip():yield next(iter_json(io.BytesIO(raw),fields=self.fields))
                if len(pending)>1024*1024:raise ValueError('REVERSE_LOG_ROW_BUDGET: use forward iterator')
            if pending.strip():yield next(iter_json(io.BytesIO(pending),fields=self.fields))
    def __deepcopy__(self,memo):
        # Audit callers must capture before returning; a live view is not immutable.
        return capture(self)


def view(path, *, fields=None):
    cache=_SCOPE.get();key=(str(Path(path).absolute()),tuple(fields) if fields else None)
    if cache is None:return LogView(path,fields)
    if key not in cache:cache[key]=LogView(path,fields)
    return cache[key]


@contextmanager
def scope():
    token=_SCOPE.set(_SCOPE.get() or {})
    try:yield
    finally:_SCOPE.reset(token)


def _store_json_stream(stream, *, base=None, multiple=False, legacy_base=None, active=None):
    """Publish containers as the incremental parser closes them, not whole rows."""
    stack=[];active=set() if active is None else active
    def push_value(packed):
        if not stack:return packed
        frame=stack[-1]
        if frame['type']=='dict':
            key=frame.pop('key')
            # Match json.loads: the last value wins, original key order stays.
            if key in frame['keys']:frame['entries'][frame['keys'][key]][1]=packed
            else:
                frame['keys'][key]=len(frame['entries']);frame['entries'].append([key,packed])
        else:
            frame['entries'].append(packed);frame['length']+=1
            if len(frame['entries'])==objects.FANOUT:
                frame['chunks'].append(objects.pack_node({'format':objects.FORMAT,'type':'list',
                    'entries':frame['entries']},'sequence_chunk',base,inline=True));frame['entries']=[]
    for _,event,value in ijson.parse(stream,multiple_values=multiple):
        if event in ('start_map','start_array'):
            stack.append({'type':'dict' if event=='start_map' else 'list','entries':[], 'chunks':[], 'length':0,'keys':{}})
        elif event=='map_key':stack[-1]['key']=value
        elif event in ('end_map','end_array'):
            frame=stack.pop();body={'format':objects.FORMAT,'type':frame['type']}
            packed=None
            if legacy_base is not None and frame['type']=='dict':
                import snapshot_store as legacy
                entries=dict(frame['entries'])
                if set(entries)=={legacy.REF}:
                    identifier=entries[legacy.REF].get('literal')
                    if identifier in active or len(active)>=legacy.MAX_DEPTH:raise legacy.SnapshotError('SNAPSHOT_REFERENCE_CYCLE')
                    path=legacy._safe_path(identifier,legacy_base)
                    if objects.file_fingerprint(path)[0]!=identifier:raise legacy.SnapshotError('SNAPSHOT_BLOB_HASH_MISMATCH')
                    active.add(identifier)
                    try:
                        with path.open('rb') as source:
                            packed=next(store_json_stream(source,base=base,legacy_base=legacy_base,active=active))
                    finally:active.remove(identifier)
                elif set(entries)=={legacy.LITERAL}:
                    pairs=objects.open_object(entries[legacy.LITERAL],base=base)
                    if not isinstance(pairs,list):raise ValueError('INVALID_SNAPSHOT_LITERAL')
                    decoded={}
                    for pair in pairs:
                        if not isinstance(pair,list) or len(pair)!=2 or not isinstance(pair[0],str) or pair[0] in decoded:
                            raise ValueError('INVALID_SNAPSHOT_LITERAL')
                        decoded[pair[0]]=pair[1]
                    packed=objects.put_object(decoded,base=base)
            if packed is not None:
                if stack:push_value(packed)
                else:yield packed
                continue
            if frame['type']=='dict':body['entries']=frame['entries']
            elif frame['length']==objects.FANOUT and len(frame['chunks'])==1:
                child=objects.resolver_for(base).node(frame['chunks'][0])
                body['entries']=child['entries']
            elif frame['chunks']:
                if frame['entries']:frame['chunks'].append(objects.pack_node({'format':objects.FORMAT,
                    'type':'list','entries':frame['entries']},'sequence_chunk',base,inline=True))
                body.update(length=frame['length'],chunks=frame['chunks'])
            else:body['entries']=frame['entries']
            packed=objects.pack_node(body,'content',base,inline=bool(stack))
            if stack:push_value(packed)
            else:yield packed
        else:
            if isinstance(value,Decimal):value=float(value)
            if isinstance(value,str) and len(value)>16384:
                packed=objects.put_object(value,kind='content',base=base)
                if stack and objects.ref_info(packed)['bytes']<objects.legacy.MIN_BYTES:
                    packed={'inline':objects.resolver_for(base).node(packed)}
            else:packed={'literal':value}
            if stack:push_value(packed)
            else:yield objects.put_object(value,base=base)


def _capture(rows, *, base=None):
    """Capture the exact open prefix and reuse immutable row/chunk payloads."""
    if not isinstance(rows,LogView):return objects.freeze(rows,kind='journal',base=base)
    rows._verify();refs=[];length=0;group=[]
    with rows.path.open('rb') as stream:
        for ref in store_json_stream(PrefixReader(stream,rows.size),base=base,multiple=True):
            group.append(ref);length+=1
            if len(group)==objects.FANOUT:
                refs.append(objects._publish_node({'format':objects.FORMAT,'type':'list','entries':group},'log_block',base));group=[]
    rows._verify()
    if group:refs.append(objects._publish_node({'format':objects.FORMAT,'type':'list','entries':group},'log_block',base))
    ref=objects._publish_node({'format':objects.FORMAT,'type':'list','length':length,'chunks':refs},'journal_prefix',base)
    return objects.open_object(ref,base=base)


RUN_FIELDS=('time','run_id','run_attempt_id','kind','ok','tokens','usage','cost_usd','sec','note','judgment_coverage')


def runtime_summaries(runs_path):
    daily={};latest={};count=0
    rows=LogView(runs_path,fields=RUN_FIELDS[:-1])
    for row in rows:
        count+=1;kind=row.get('kind');when=row.get('time');latest[kind]={k:row.get(k) for k in RUN_FIELDS[:-1]}
        key=((when or '')[:10],kind)
        total=daily.setdefault(key,{'date':key[0],'kind':kind,'runs':0,'successes':0,'failures':0,'tokens':0,'seconds':0,'seconds_known_runs':0,'tokens_known_runs':0,'usage':{}})
        total['runs']+=1;total['successes']+=bool(row.get('ok'));total['failures']+=not bool(row.get('ok'))
        if isinstance(row.get('tokens'),(int,float)):
            total['tokens']+=row['tokens'];total['tokens_known_runs']+=1
        if isinstance(row.get('sec'),(int,float)):
            total['seconds']+=row['sec'];total['seconds_known_runs']+=1
        for field,value in (row.get('usage') or {}).items():
            if isinstance(value,(int,float)):total['usage'][field]=total['usage'].get(field,0)+value
    for total in daily.values():
        if not total['seconds_known_runs']:total['seconds']=None
        if not total['tokens_known_runs']:total['tokens']=None
    rows._verify()
    watermark={'sha256':rows.digest,'bytes':rows.size,'rows':count}
    return ({'schema_version':2,'source_watermark':watermark,'complete':True,'latest':latest},
            {'schema_version':2,'source_watermark':watermark,'complete':True,'items':list(daily.values())})


class _LeadingByteReader:
    def __init__(self,first,stream):self.first=first;self.stream=stream
    def read(self,size=-1):
        if size==0:return b''
        first=self.first;self.first=b''
        return first+self.stream.read(-1 if size<0 else max(0,size-len(first)))
    def readinto(self,buffer):
        value=self.read(len(buffer));buffer[:len(value)]=value;return len(value)


def store_json_stream(stream, **kwargs):
    first=stream.read(1)
    while first and first.isspace():first=stream.read(1)
    if not first:return
    try:yield from _store_json_stream(_LeadingByteReader(first,stream), **kwargs)
    except ijson.JSONError as exc:raise ValueError('INVALID_JSON_STREAM: '+str(exc)) from exc


def capture(rows, *, base=None):
    import copy
    cache=_SCOPE.get()
    key=('captured',str(rows.path),rows.size,rows.identity,str(base)) if isinstance(rows,LogView) else None
    if cache is not None and key is not None:
        if key not in cache:cache[key]=_capture(rows,base=base)
        return copy.deepcopy(cache[key])
    return _capture(rows,base=base)


def aggregate(rows,date):
    cache=_SCOPE.get()
    if isinstance(rows,objects.ObjectList):key=('aggregate',str(rows._resolver.base),str(rows._ref),date)
    elif isinstance(rows,LogView):key=('aggregate',str(rows.path),rows.size,rows.identity,date)
    else:key=None
    if cache is not None and key in cache:return cache[key]
    import common
    result=common._journal_totals(rows,date)
    if cache is not None and key is not None:cache[key]=result
    return result
