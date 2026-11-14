"""Explicit v2 immutable content storage and copy-on-write logical views.

Only a trusted record reader may open references. User JSON is always encoded
as data, including objects whose keys resemble a reference. Views resolve one
container at a time; deepcopy copies overlays, not the immutable source graph.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import snapshot_store as legacy

REF = '$sigma_object_ref'
FORMAT = 'sigma-object@v2'
CACHE_BYTES = 64 * 1024 * 1024
CHUNK_SIZE = 256 * 1024
FANOUT = 128
_CURRENT = ContextVar('storage_resolver', default=None)
METRICS={'canonical_bytes':0,'blob_loads':0,'blob_loaded_bytes':0,'published_files':0,'published_bytes':0}


def chunks(value, *, separators=(',', ':')):
    """Canonical UTF-8, with bounded string escaping and no whole JSON copy."""
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), allow_nan=False)
    active = set()
    def emit(node, depth=0):
        if depth > legacy.MAX_DEPTH:
            raise legacy.SnapshotError('SNAPSHOT_DEPTH_LIMIT')
        if isinstance(node,(ObjectDict,ObjectList)) and not node._dirty and separators==(',',':'):
            yield from object_chunks(node._ref, resolver=node._resolver)
            return
        if isinstance(node, str):
            yield b'"'
            for start in range(0, len(node), 16384):
                yield encoder.encode(node[start:start + 16384])[1:-1].encode('utf-8')
            yield b'"'
            return
        container = isinstance(node, (dict, list, tuple))
        if container:
            if id(node) in active:
                raise legacy.SnapshotError('SNAPSHOT_CYCLE')
            active.add(id(node))
        try:
            if isinstance(node, dict):
                if any(not isinstance(k, str) for k in node):
                    raise legacy.SnapshotError('SNAPSHOT_OBJECT_KEYS_MUST_BE_STRINGS')
                yield b'{'
                for i, key in enumerate(sorted(node)):
                    if i: yield separators[0].encode()
                    yield from emit(key, depth + 1)
                    yield separators[1].encode()
                    yield from emit(node[key], depth + 1)
                yield b'}'
            elif isinstance(node, (list, tuple)):
                yield b'['
                for i, child in enumerate(node):
                    if i: yield separators[0].encode()
                    yield from emit(child, depth + 1)
                yield b']'
            else:
                for part in encoder.iterencode(node):
                    yield part.encode('utf-8')
        finally:
            if container: active.remove(id(node))
    yield from emit(value)


def fingerprint(value):
    digest, size = hashlib.sha256(), 0
    for part in chunks(value):
        digest.update(part); size += len(part)
        METRICS['canonical_bytes']+=len(part)
    return digest.hexdigest(), size


def file_fingerprint(path):
    digest, size = hashlib.sha256(), 0
    with open(path, 'rb') as stream:
        for part in iter(lambda: stream.read(CHUNK_SIZE), b''):
            digest.update(part); size += len(part)
    return digest.hexdigest(), size


def publish_json(path, value, *, replace=False):
    path = Path(path)
    if path.is_symlink(): raise legacy.SnapshotError('RECORD_SYMLINK_FORBIDDEN')
    legacy._mkdir_durable(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.v2-', dir=path.parent)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, 'wb') as stream:
            for part in chunks(value):
                digest.update(part); size += len(part); stream.write(part)
            stream.flush(); os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        legacy._sync_directory(path.parent)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    METRICS['published_files']+=1;METRICS['published_bytes']+=size
    return digest.hexdigest(), size


def _size_node(node, base):
    # Nodes contain bounded containers; logical child data is already durable.
    if 'logical_bytes' not in node:
        def size(packed):
            if 'literal' in packed: return fingerprint(packed['literal'])[1]
            if 'inline' in packed: return packed['inline']['logical_bytes']
            current=_CURRENT.get()
            if current is not None and legacy.root(current.base).resolve()==legacy.root(base).resolve():
                return current.node(packed)['logical_bytes']
            path = legacy._safe_path(ref_info(packed)['sha256'], base)
            with path.open(encoding='utf-8') as stream: return json.load(stream)['logical_bytes']
        if node['type']=='dict' and 'base_ref' in node:
            entries=list(resolver_for(base).entries({'inline':node}))
            node['logical_bytes']=2+max(0,len(entries)-1)+sum(fingerprint(k)[1]+1+size(v) for k,v in entries)
        elif node['type'] == 'dict':
            node['logical_bytes'] = 2 + max(0,len(node['entries'])-1) + sum(
                fingerprint(k)[1]+1+size(v) for k,v in node['entries'])
        elif node['type'] == 'list':
            node['logical_bytes'] = (2 + max(0,len(node['chunks'])-1) + sum(size(v)-2 for v in node['chunks'])
                if 'chunks' in node else 2+max(0,len(node['entries'])-1)+sum(size(v) for v in node['entries']))
        elif node['type'] == 'string': node['logical_bytes'] = 2+sum(size(v)-2 for v in node['chunks'])
        else: node['logical_bytes'] = fingerprint(node['literal'])[1]


def _publish_node(node, kind, base):
    _size_node(node,base)
    sha, size = fingerprint(node)
    path = legacy._safe_path(sha, base)
    ref={REF: {'version': 2, 'kind': kind, 'sha256': sha, 'bytes': size}}
    current=_CURRENT.get()
    if current is not None and legacy.root(current.base).resolve()==legacy.root(base).resolve():
        cached=current.cache.get(sha)
        if cached and path.exists():
            stat=path.stat()
            if cached[0]==(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns):return ref
    else:current=None
    if path.exists():
        if file_fingerprint(path) != (sha, size):
            raise legacy.SnapshotError('EXISTING_SNAPSHOT_HASH_MISMATCH:' + sha)
    else:
        try:
            publish_json(path, node)
        except FileExistsError:
            if file_fingerprint(path) != (sha, size):
                raise legacy.SnapshotError('EXISTING_SNAPSHOT_HASH_MISMATCH:' + sha)
    legacy._sync_blob_chain(path.parent)
    if current is not None:current.node(ref)
    return ref


def ref_info(ref):
    if not isinstance(ref, dict) or set(ref) != {REF}:
        raise legacy.SnapshotError('INVALID_OBJECT_REFERENCE')
    info = ref[REF]
    if (not isinstance(info, dict) or set(info) != {'version', 'kind', 'sha256', 'bytes'}
            or info['version'] != 2 or not isinstance(info['kind'], str)
            or not legacy._SHA.fullmatch(str(info['sha256']))
            or type(info['bytes']) is not int or info['bytes'] < 0):
        raise legacy.SnapshotError('INVALID_OBJECT_REFERENCE')
    return info


def put_object(value, *, kind='content', base=None):
    """Freeze now. No identity-only memoization of mutable caller input."""
    active = set()
    def pack(node, depth=0, label='content'):
        if depth > legacy.MAX_DEPTH: raise legacy.SnapshotError('SNAPSHOT_DEPTH_LIMIT')
        if isinstance(node, (ObjectDict, ObjectList)) and not node._dirty:
            # A reference from another root must be copied, never silently rebound.
            if legacy.root(node._resolver.base).resolve() == legacy.root(base).resolve():
                node._resolver.node(node._ref)
                return node._ref
        container = isinstance(node, (dict, list, tuple))
        if container and id(node) in active: raise legacy.SnapshotError('SNAPSHOT_CYCLE')
        if container: active.add(id(node))
        try:
            if isinstance(node,ObjectDict) and node._dirty and legacy.root(node._resolver.base).resolve()==legacy.root(base).resolve():
                original=node._resolver.node(node._ref)
                overrides=dict(original.get('overrides',()))
                overrides.update((k,pack(v,depth+1)) for k,v in node._changes.items())
                for key in node._deleted:overrides.pop(key,None)
                deleted=(set(original.get('deleted',()))|node._deleted)-set(node._changes)
                body={'format':FORMAT,'type':'dict','base_ref':original.get('base_ref',node._ref),
                      'overrides':list(overrides.items()),'deleted':sorted(deleted),'order':list(node)}
                return pack_node(body,label,base,inline=bool(depth))
            if isinstance(node, dict):
                if any(not isinstance(k, str) for k in node):
                    raise legacy.SnapshotError('SNAPSHOT_OBJECT_KEYS_MUST_BE_STRINGS')
                entries = [[key, pack(node[key], depth + 1)] for key in node]
                body = {'type': 'dict', 'entries': entries}
            elif isinstance(node, (list, tuple)):
                if len(node) > FANOUT:
                    refs = [pack(node[i:i + FANOUT], depth + 1, 'sequence_chunk')
                            for i in range(0, len(node), FANOUT)]
                    body = {'type': 'list', 'length': len(node), 'chunks': refs}
                else:
                    body = {'type': 'list', 'entries': [pack(v, depth + 1) for v in node]}
            elif isinstance(node, str) and len(node) > 16384:
                body = {'type': 'string', 'chunks': [_publish_node(
                    {'format':FORMAT, 'type':'scalar', 'literal':node[i:i + 16384]},
                    'text_chunk', base) for i in range(0, len(node), 16384)]}
            else:
                # A tagged literal cannot be confused with a trusted reference.
                return {'literal': node}
            body['format'] = FORMAT
            return pack_node(body,label,base,inline=bool(depth))
        finally:
            if container: active.remove(id(node))
    result = pack(value, label=kind)
    if REF not in result:
        result = _publish_node(result['inline'] if 'inline' in result else
                               {'format': FORMAT, 'type': 'scalar', **result}, kind, base)
    return result


def _memory_size(value):
    if isinstance(value, dict):
        return sys.getsizeof(value) + sum(_memory_size(k) + _memory_size(v) for k, v in value.items())
    if isinstance(value, list): return sys.getsizeof(value) + sum(map(_memory_size, value))
    return sys.getsizeof(value)


class Resolver:
    """Request-local, verified, bounded decoded-node cache; never a permission."""
    def __init__(self, base=None, *, budget=CACHE_BYTES, dependencies=None):
        self.base, self.budget = base, budget
        self.cache = OrderedDict()
        self.trackers=[]
        self.used = 0
        self.dependencies = dependencies if dependencies is not None else {}
        self.metrics = {'loads': 0, 'loaded_bytes': 0, 'peak_cache_bytes': 0}

    def node(self, ref):
        if set(ref) == {'inline'}: return ref['inline']
        info = ref_info(ref)
        path = legacy._safe_path(info['sha256'], self.base)
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        for target in self.trackers:target[str(path.absolute())]=info['sha256']
        cached = self.cache.get(info['sha256'])
        if cached and cached[0] == identity:
            self.cache.move_to_end(info['sha256']); return cached[1]
        if file_fingerprint(path) != (info['sha256'], info['bytes']):
            raise legacy.SnapshotError('OBJECT_HASH_MISMATCH:' + info['sha256'])
        if info['bytes'] > self.budget:
            raise legacy.SnapshotError('OBJECT_NODE_BUDGET_EXCEEDED')
        with path.open(encoding='utf-8') as stream: body = json.load(stream)
        if body.get('format') != FORMAT: raise legacy.SnapshotError('INVALID_OBJECT_NODE')
        size = _memory_size(body)
        if cached:
            self.used -= self.cache.pop(info['sha256'])[2]
        while self.cache and self.used + size > self.budget:
            self.used -= self.cache.popitem(last=False)[1][2]
        if size <= self.budget:
            self.cache[info['sha256']] = (identity, body, size); self.used += size
        self.metrics['loads'] += 1; self.metrics['loaded_bytes'] += info['bytes']
        METRICS['blob_loads']+=1;METRICS['blob_loaded_bytes']+=info['bytes']
        self.metrics['peak_cache_bytes'] = max(self.metrics['peak_cache_bytes'], self.used)
        self.dependencies[str(path.absolute())] = info['sha256']
        return body

    @contextmanager
    def tracking(self,dependencies):
        if dependencies is not None:self.trackers.append(dependencies)
        try:yield
        finally:
            if dependencies is not None:self.trackers.pop()

    def entries(self,ref,depth=0):
        if depth>legacy.MAX_DEPTH:raise legacy.SnapshotError('OBJECT_REFERENCE_DEPTH')
        body=self.node(ref)
        if 'base_ref' not in body:return body['entries']
        entries=dict(self.entries(body['base_ref'],depth+1));entries.update(body['overrides'])
        for key in body['deleted']:entries.pop(key,None)
        return [(key,entries[key]) for key in body['order']]

    def value(self, packed, callback=None, ancestors=()):
        if set(packed) == {'literal'}: return packed['literal']
        if set(packed) != {'inline'}:
            info = ref_info(packed)
            if info['sha256'] in ancestors or len(ancestors) >= legacy.MAX_DEPTH:
                raise legacy.SnapshotError('OBJECT_REFERENCE_CYCLE')
            ancestors = (*ancestors, info['sha256'])
        body = self.node(packed)
        if body['type'] == 'dict': return ObjectDict(packed, self, callback, ancestors)
        if body['type'] == 'list': return ObjectList(packed, self, callback, ancestors)
        if body['type'] == 'string':
            return ''.join(self.value(r, ancestors=ancestors) for r in body['chunks'])
        if body['type'] == 'scalar': return body['literal']
        raise legacy.SnapshotError('INVALID_OBJECT_TYPE')


from weakref import WeakValueDictionary


class ObjectDict(dict):
    """A dict-compatible logical view; only mutated paths are retained in memory."""
    def __init__(self, ref, resolver, callback=None, ancestors=()):
        self._ref, self._resolver, self._callback = ref, resolver, callback
        self._ancestors, self._changes, self._deleted = ancestors, {}, set()
        self._dirty = False
        self._children=WeakValueDictionary()
        # Keys stay small. Values are loaded explicitly, never embedded here.
        super().__init__((k, None) for k, _ in resolver.entries(ref))

    def _touch(self):
        self._dirty = True
        if self._callback: self._callback(self)

    def __iter__(self): return dict.__iter__(self)
    def keys(self): return dict.keys(self)
    def packed_field(self,key):
        if key in self._changes or key not in self: return None
        return next(v for k,v in self._resolver.entries(self._ref) if k==key)
    def __getitem__(self, key):
        if key not in self: raise KeyError(key)
        if key in self._changes: return self._changes[key]
        child=self._children.get(key)
        if child is not None:return child
        for k, packed in self._resolver.entries(self._ref):
            if k == key:
                def changed(child):
                    if key in self and (self._changes.get(key) is child or key not in self._changes and self._children.get(key) is child):self[key]=child
                child=self._resolver.value(packed,changed,self._ancestors)
                if isinstance(child,(ObjectDict,ObjectList)):self._children[key]=child
                return child
        raise KeyError(key)
    def get(self, key, default=None): return self[key] if key in self else default
    def items(self): return ((k, self[k]) for k in self)
    def values(self): return (self[k] for k in self)
    def __setitem__(self, key, value):
        self._children.pop(key,None)
        dict.__setitem__(self, key, None); self._changes[key] = value
        self._deleted.discard(key); self._touch()
    def __delitem__(self, key):
        self._children.pop(key,None)
        dict.__delitem__(self, key); self._changes.pop(key, None)
        self._deleted.add(key); self._touch()
    def update(self, other=(), **kwargs):
        for key, value in (other.items() if hasattr(other, 'items') else other): self[key] = value
        for key, value in kwargs.items(): self[key] = value
    def setdefault(self, key, default=None):
        if key not in self: self[key] = default
        return self[key]
    def pop(self, key, *default):
        if key not in self:
            if default: return default[0]
            raise KeyError(key)
        value = self[key]; del self[key]; return value
    def popitem(self):
        if not self: raise KeyError('empty dictionary')
        key = next(reversed(self)); return key, self.pop(key)
    def clear(self):
        for key in list(self): del self[key]
    def copy(self): return {k: v for k, v in self.items()}
    def __copy__(self): return self.copy()
    def __deepcopy__(self, memo):
        clone = ObjectDict(self._ref, self._resolver, ancestors=self._ancestors)
        memo[id(self)] = clone
        for key in self._deleted: del clone[key]
        for key, value in self._changes.items(): clone[key] = copy.deepcopy(value, memo)
        return clone
    def __eq__(self, other):
        return isinstance(other, dict) and self.keys() == other.keys() and all(self[k] == other[k] for k in self)
    def __ne__(self, other): return not self == other
    def __or__(self, other): return {**self, **other}
    def __ior__(self, other): self.update(other); return self
    def __repr__(self): return repr(dict(self.items()))


class ObjectList(list):
    def __init__(self, ref, resolver, callback=None, ancestors=()):
        self._ref, self._resolver, self._callback = ref, resolver, callback
        self._ancestors, self._changes, self._replacement = ancestors, {}, None
        self._dirty = False
        self._children=WeakValueDictionary()
        # The base list has no logical values; all operations use the view.
        super().__init__()
    def _touch(self):
        self._dirty = True
        if self._callback: self._callback(self)
    def __len__(self):
        if self._replacement is not None: return len(self._replacement)
        body = self._resolver.node(self._ref)
        return body.get('length', len(body.get('entries', ())))
    def __bool__(self): return len(self) != 0
    def __reversed__(self):return (self[i] for i in range(len(self)-1,-1,-1))
    def __iter__(self):
        body=self._resolver.node(self._ref)
        if not self._dirty and 'derivation' in body:return self._derived(body)
        return (self[i] for i in range(len(self)))
    def _derived(self,body):
        excluded={tuple(pair) for pair in body['mentioned']}
        for delivery,ref in body['catalogs']:
            for item in self._resolver.value(ref):
                if (delivery,item['evidence_id']) not in excluded:
                    yield {'delivery_id':delivery,'evidence_id':item['evidence_id'],'adoption':'unrecorded'}
    def __getitem__(self, index):
        if isinstance(index, slice): return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0: index += len(self)
        if not 0 <= index < len(self): raise IndexError(index)
        if self._replacement is not None: return self._replacement[index]
        if index in self._changes: return self._changes[index]
        child=self._children.get(index)
        if child is not None:return child
        body = self._resolver.node(self._ref)
        if 'derivation' in body:
            return next(value for i,value in enumerate(self._derived(body)) if i==index)
        def callback(child):
            if self._replacement is None and (self._changes.get(index) is child or index not in self._changes and self._children.get(index) is child):self[index]=child
        if 'chunks' in body:
            segment = self._resolver.value(body['chunks'][index // FANOUT], ancestors=self._ancestors)
            value = segment[index % FANOUT]
            if isinstance(value, (ObjectDict, ObjectList)): value._callback = callback
        else:value=self._resolver.value(body['entries'][index],callback,self._ancestors)
        if isinstance(value,(ObjectDict,ObjectList)):self._children[index]=value
        return value
    def __setitem__(self, index, value):
        if isinstance(index, slice): self._materialize(); self._replacement[index] = value
        elif self._replacement is not None: self._replacement[index] = value
        else:
            if index < 0: index += len(self)
            if not 0 <= index < len(self): raise IndexError(index)
            self._changes[index] = value
        self._touch()
    def _materialize(self):
        if self._replacement is None:
            self._replacement = list(self); self._changes.clear();self._children.clear()
            for value in self._replacement:
                if isinstance(value,(ObjectDict,ObjectList)):value._callback=None
    def __delitem__(self, index): self._materialize(); del self._replacement[index]; self._touch()
    def append(self, value): self._materialize(); self._replacement.append(value); self._touch()
    def extend(self, values): self._materialize(); self._replacement.extend(values); self._touch()
    def insert(self, index, value): self._materialize(); self._replacement.insert(index, value); self._touch()
    def pop(self, index=-1): self._materialize(); value=self._replacement.pop(index); self._touch(); return value
    def remove(self, value): self._materialize(); self._replacement.remove(value); self._touch()
    def clear(self): self._replacement=[]; self._changes.clear(); self._touch()
    def sort(self, *args, **kwargs): self._materialize(); self._replacement.sort(*args, **kwargs); self._touch()
    def reverse(self): self._materialize(); self._replacement.reverse(); self._touch()
    def count(self, value): return sum(v == value for v in self)
    def index(self, value, start=0, stop=None):
        for i in range(start, len(self) if stop is None else min(stop, len(self))):
            if self[i] == value: return i
        raise ValueError(value)
    def __contains__(self, value): return any(v == value for v in self)
    def __add__(self, other): return [*self, *other]
    def __radd__(self, other): return [*other, *self]
    def __iadd__(self, other): self.extend(other); return self
    def __mul__(self, count): return list(self) * count
    __rmul__ = __mul__
    def __imul__(self,count):self._materialize();self._replacement*=count;self._touch();return self
    def copy(self): return list(self)
    def __deepcopy__(self, memo):
        clone = ObjectList(self._ref, self._resolver, ancestors=self._ancestors); memo[id(self)] = clone
        if self._replacement is not None:
            clone._replacement = copy.deepcopy(self._replacement, memo); clone._dirty = True
        else:
            clone._changes = copy.deepcopy(self._changes, memo); clone._dirty = self._dirty
        return clone
    def __eq__(self, other):
        return isinstance(other, (list, tuple)) and len(self) == len(other) and all(a == b for a,b in zip(self,other))
    def __ne__(self, other): return not self == other
    def __repr__(self): return repr(list(self))


def open_object(ref, *, base=None, resolver=None, fields=None):
    resolver = resolver or resolver_for(base)
    value = resolver.value(ref)
    return {key: value[key] for key in fields if key in value} if fields is not None else value


def freeze(value, *, kind='content', base=None, resolver=None):
    return open_object(put_object(value, kind=kind, base=base), base=base, resolver=resolver)


def verify_closure(ref, *, base=None, dependencies=None, resolver=None):
    """Verify physical dependencies without constructing the logical document."""
    resolver = resolver or resolver_for(base)
    captured={} if dependencies is None else dependencies
    visited, active = set(), set()
    def visit(packed):
        if set(packed) == {'literal'}: return
        identifier = ref_info(packed)['sha256'] if REF in packed else None
        if identifier in active: raise legacy.SnapshotError('OBJECT_REFERENCE_CYCLE')
        if identifier in visited: return
        if identifier: active.add(identifier)
        body = resolver.node(packed)
        if body['type']=='dict' and 'base_ref' in body:children=iter([body['base_ref'],*(v for _,v in body['overrides'])])
        elif body['type'] == 'dict': children = (v for _,v in body['entries'])
        elif 'derivation' in body: children = (r for _,r in body['catalogs'])
        elif body['type'] in ('list','string'): children = iter(body.get('chunks',body.get('entries',())))
        else: children = iter(())
        for child in children: visit(child)
        if identifier: active.remove(identifier); visited.add(identifier)
    with resolver.tracking(captured):visit(ref)
    return captured


def materialize(value):
    if isinstance(value, dict): return {k: materialize(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)): return [materialize(v) for v in value]
    return value


def logical_size(value):
    if isinstance(value,(ObjectDict,ObjectList)) and not value._dirty:
        return value._resolver.node(value._ref)['logical_bytes']
    if isinstance(value,dict):return 2+max(0,len(value)-1)+sum(fingerprint(k)[1]+1+logical_size(v) for k,v in value.items())
    if isinstance(value,(list,tuple)):return 2+max(0,len(value)-1)+sum(logical_size(v) for v in value)
    return fingerprint(value)[1]


def field_size(value,key):
    packed=value.packed_field(key) if isinstance(value,ObjectDict) else None
    if packed is not None:
        return fingerprint(packed['literal'])[1] if 'literal' in packed else value._resolver.node(packed)['logical_bytes']
    return logical_size(value[key])


def field_chunks(value,key):
    packed=value.packed_field(key) if isinstance(value,ObjectDict) else None
    return object_chunks(packed,resolver=value._resolver) if packed is not None else chunks(value[key])


def object_chunks(ref, *, resolver=None, base=None):
    resolver=resolver or Resolver(base)
    active=set()
    def emit(packed, interior=False):
        if 'literal' in packed:
            yield from chunks(packed['literal']);return
        identifier=ref_info(packed)['sha256'] if REF in packed else None
        if identifier in active:raise legacy.SnapshotError('OBJECT_REFERENCE_CYCLE')
        if identifier:active.add(identifier)
        try:
            body=resolver.node(packed);kind=body['type']
            if kind=='scalar':yield from chunks(body['literal'])
            elif kind=='dict':
                yield b'{'
                for i,(key,value) in enumerate(sorted(resolver.entries(packed),key=lambda pair:pair[0])):
                    if i:yield b','
                    yield from chunks(key);yield b':';yield from emit(value)
                yield b'}'
            elif kind=='list' and 'derivation' in body:
                yield b'['
                for i,value in enumerate(ObjectList(packed,resolver)):
                    if i:yield b','
                    yield from chunks(value)
                yield b']'
            elif kind=='list':
                if not interior:yield b'['
                for i,value in enumerate(body.get('chunks',body.get('entries',()))):
                    if i:yield b','
                    yield from emit(value,interior='chunks' in body)
                if not interior:yield b']'
            elif kind=='string':
                yield b'"'
                for value in body['chunks']:
                    text=resolver.value(value)
                    encoded=json.dumps(text,ensure_ascii=False,allow_nan=False).encode('utf-8')
                    yield encoded[1:-1]
                yield b'"'
            else:raise legacy.SnapshotError('INVALID_OBJECT_TYPE')
        finally:
            if identifier:active.remove(identifier)
    yield from emit(ref)


def resolver_for(base=None, *, dependencies=None):
    current=_CURRENT.get()
    if current is not None and legacy.root(current.base).resolve()==legacy.root(base).resolve():
        return current
    return Resolver(base,dependencies=dependencies)


def unmentioned_evidence(catalogs, mentioned, *, base=None):
    body={'format':FORMAT,'type':'list','derivation':'unmentioned_evidence@v1',
          'catalogs':[[delivery,put_object(catalog,base=base)] for delivery,catalog in catalogs],
          'mentioned':sorted([list(pair) for pair in mentioned])}
    excluded=set(mentioned);length=0;size=2
    for delivery,ref in body['catalogs']:
        for item in open_object(ref,base=base):
            if (delivery,item['evidence_id']) in excluded:continue
            row={'delivery_id':delivery,'evidence_id':item['evidence_id'],'adoption':'unrecorded'}
            size+=fingerprint(row)[1]+bool(length);length+=1
    body.update(length=length,logical_bytes=size)
    return open_object(_publish_node(body,'unmentioned_evidence',base),base=base)


@contextmanager
def scope(base=None):
    existing = _CURRENT.get()
    resolver = existing if existing is not None and legacy.root(existing.base).resolve()==legacy.root(base).resolve() else Resolver(base)
    token = _CURRENT.set(resolver)
    try: yield resolver
    finally: _CURRENT.reset(token)


def scoped(fn):
    import functools
    @functools.wraps(fn)
    def wrapped(*args,**kwargs):
        with scope():return fn(*args,**kwargs)
    return wrapped


def field_kind(value,key):
    packed=value.packed_field(key) if isinstance(value,ObjectDict) else None
    if packed is not None and 'literal' not in packed:return value._resolver.node(packed)['type']
    child=value[key]
    return 'list' if isinstance(child,list) else 'dict' if isinstance(child,dict) else 'scalar'


def pack_node(body,kind,base=None,*,inline=False):
    _size_node(body,base)
    if inline and fingerprint(body)[1]<legacy.MIN_BYTES:return {'inline':body}
    return _publish_node(body,kind,base)
