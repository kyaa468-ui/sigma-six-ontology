"""Content-addressed storage for immutable JSON snapshots, never engine inputs.

Records keep their original logical shape and hash. Only their physical JSON
representation changes: large repeated subtrees live once in a shared blob
directory. Every referenced blob is published and fsynced before a caller can
publish its document. Reading old inline JSON remains supported and never writes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

VERSION = 'content-addressed-snapshot@v1'
REF = '$sigma_snapshot_ref'
LITERAL = '$sigma_snapshot_literal'
MIN_BYTES = 4096
MAX_DEPTH = 128
MAX_REFERENCES = 200000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_CACHED_VALUE_BYTES = 1024 * 1024
_SHA = re.compile(r'[a-f0-9]{64}\Z')


class SnapshotError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def root(base=None):
    """base, when supplied, is the ontology directory rather than WORK."""
    if base is None:
        import common as C
        base = Path(C.WORK) / 'ontology'
    return Path(base) / 'snapshots' / 'blobs'


def _safe_path(identifier, base=None):
    if not isinstance(identifier, str) or not _SHA.fullmatch(identifier):
        raise SnapshotError('INVALID_SNAPSHOT_HASH')
    folder = root(base)
    target = folder / (identifier + '.json')
    # The caller controls ontology root. No blob or directory below it may
    # redirect a reference into an unrelated file, even if its bytes would hash.
    boundary = folder.parent.parent
    for part in (target, folder, folder.parent):
        if part.is_symlink():
            raise SnapshotError('SNAPSHOT_SYMLINK_FORBIDDEN')
    if not target.resolve().is_relative_to(boundary.resolve()):
        raise SnapshotError('SNAPSHOT_OUTSIDE_ONTOLOGY')
    return target


def _sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _mkdir_durable(folder):
    missing = []
    current = folder
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir(exist_ok=True)
        _sync_directory(path.parent)


def _sync_blob_chain(folder):
    # Another writer may have linked this blob or created these directories
    # immediately before we observed them. Existing entries still need our own
    # durability barrier before a referencing record can become visible.
    # The last flush persists the ontology entry itself; never walk above its
    # caller-controlled parent (WORK for the default store).
    for path in (folder, folder.parent, folder.parent.parent, folder.parent.parent.parent):
        _sync_directory(path)


def _publish(payload, base=None):
    identifier = hashlib.sha256(payload).hexdigest()
    target = _safe_path(identifier, base)
    _mkdir_durable(target.parent)
    # Verify even existing blobs. A damaged old blob must not silently acquire
    # new references or be overwritten with a fresh, apparently healthy copy.
    if target.exists():
        if target.read_bytes() != payload:
            raise SnapshotError('EXISTING_SNAPSHOT_HASH_MISMATCH:' + identifier)
        _sync_blob_chain(target.parent)
        return identifier
    fd, temporary = tempfile.mkstemp(prefix='.snapshot-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise SnapshotError('EXISTING_SNAPSHOT_HASH_MISMATCH:' + identifier)
        _sync_blob_chain(target.parent)
    finally:
        os.unlink(temporary)
    return identifier


def encode(value, *, base=None):
    """Return the physical document after durable publication of its blobs.

    A failed call can leave unused immutable blobs, but no document is returned
    until all of its dependencies exist. There is deliberately no garbage
    collection or mutation of historical records in this first version.
    """
    original = canonical(value)
    if len(original) > MAX_EXPANDED_BYTES:
        raise SnapshotError('SNAPSHOT_EXPANSION_LIMIT')
    collision = isinstance(value, dict) and value.get('format') == VERSION
    if len(original) < MIN_BYTES and not collision:
        return copy.deepcopy(value)
    references = 0
    active = set()
    def pack(node, depth=0):
        nonlocal references
        if depth > MAX_DEPTH:
            raise SnapshotError('SNAPSHOT_DEPTH_LIMIT')
        container = isinstance(node, (dict, list, tuple))
        if container and id(node) in active:
            raise SnapshotError('SNAPSHOT_CYCLE')
        if container:
            active.add(id(node))
        try:
            if isinstance(node, dict):
                if any(not isinstance(k, str) for k in node):
                    raise SnapshotError('SNAPSHOT_OBJECT_KEYS_MUST_BE_STRINGS')
                entries = {key: pack(child, depth + 1) for key, child in node.items()}
                # Escape reserved reference shapes in source values. Arbitrary
                # user data can never cause a filesystem read during decoding.
                packed = ({LITERAL: [[key, child] for key, child in entries.items()]}
                          if set(entries) in ({REF}, {LITERAL}) else entries)
            elif isinstance(node, (list, tuple)):
                packed = [pack(child, depth + 1) for child in node]
            else:
                packed = node
            payload = canonical(packed)
            if len(payload) >= MIN_BYTES:
                references += 1
                if references > MAX_REFERENCES:
                    raise SnapshotError('SNAPSHOT_REFERENCE_LIMIT')
                return {REF: _publish(payload, base)}
            return packed
        finally:
            if container:
                active.remove(id(node))
    packed = pack(value)
    # A large JSON value can shrink entirely through encoded child references;
    # the wrapper always tells a reader that reference resolution is required.
    return {'format': VERSION, 'logical_sha256': hashlib.sha256(original).hexdigest(),
            'logical_bytes': len(original), 'payload': packed}


def decode(document, *, base=None, dependencies=None):
    """Hydrate a physical document; return old inline JSON unchanged.

    dependencies, if supplied, receives absolute blob paths and their physical
    SHA-256 hashes. Callers can include them in a frozen source manifest. Cache
    lifetime is one call: changing a blob is detected on the next read.
    """
    if not isinstance(document, dict) or document.get('format') != VERSION:
        return document
    if (set(document) != {'format', 'logical_sha256', 'logical_bytes', 'payload'}
            or not isinstance(document['logical_sha256'], str)
            or not _SHA.fullmatch(document['logical_sha256'])
            or type(document['logical_bytes']) is not int
            or not 0 <= document['logical_bytes'] <= MAX_EXPANDED_BYTES):
        raise SnapshotError('INVALID_SNAPSHOT_DOCUMENT')
    cache, packed_cache, visiting = {}, {}, set()
    references, used = 0, 0
    def charge(size):
        nonlocal used
        used += size
        if used > MAX_EXPANDED_BYTES or used > document['logical_bytes']:
            raise SnapshotError('SNAPSHOT_EXPANSION_LIMIT')
    def unpack(node, depth=0, reference_depth=0):
        nonlocal references
        if depth > MAX_DEPTH or reference_depth > MAX_DEPTH:
            raise SnapshotError('SNAPSHOT_DEPTH_LIMIT')
        if isinstance(node, dict) and set(node) == {REF}:
            references += 1
            if references > MAX_REFERENCES:
                raise SnapshotError('SNAPSHOT_REFERENCE_LIMIT')
            identifier = node[REF]
            path = _safe_path(identifier, base)
            if identifier in visiting:
                raise SnapshotError('SNAPSHOT_REFERENCE_CYCLE')
            if identifier in cache:
                value, size = cache[identifier]
                charge(size)
                return copy.deepcopy(value)
            if identifier in packed_cache:
                packed = packed_cache[identifier]
            else:
                try:
                    raw = path.read_bytes()
                except FileNotFoundError as exc:
                    raise SnapshotError('SNAPSHOT_BLOB_MISSING:' + identifier) from exc
                if len(raw) > MAX_EXPANDED_BYTES or hashlib.sha256(raw).hexdigest() != identifier:
                    raise SnapshotError('SNAPSHOT_BLOB_HASH_MISMATCH:' + identifier)
                if dependencies is not None:
                    dependencies[str(path.absolute())] = identifier
                try:
                    packed = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise SnapshotError('INVALID_SNAPSHOT_BLOB:' + identifier) from exc
                packed_cache[identifier] = packed
            visiting.add(identifier)
            before = used
            try:
                # Resolving a storage reference does not add a level to the
                # original JSON. Pure reference chains have a separate bound.
                value = unpack(packed, depth, reference_depth + 1)
            finally:
                visiting.remove(identifier)
            # Avoid keeping copies of every large ancestor of a hydrated report.
            # Large nodes reuse their small encoded tree; only bounded subtrees
            # get a value cache, and each occurrence remains independently mutable.
            if used - before <= MAX_CACHED_VALUE_BYTES:
                cache[identifier] = (copy.deepcopy(value), used - before)
            return value
        if isinstance(node, dict):
            if set(node) == {LITERAL}:
                pairs = node[LITERAL]
                if (not isinstance(pairs, list) or any(not isinstance(p, list) or len(p) != 2
                        or not isinstance(p[0], str) for p in pairs)
                        or len({p[0] for p in pairs}) != len(pairs)):
                    raise SnapshotError('INVALID_SNAPSHOT_LITERAL')
            else:
                pairs = list(node.items())
            charge(2 + max(0, len(pairs)-1) + sum(len(canonical(k)) + 1 for k, _ in pairs))
            return {key: unpack(child, depth + 1) for key, child in pairs}
        if isinstance(node, list):
            charge(2 + max(0, len(node)-1))
            return [unpack(child, depth + 1) for child in node]
        charge(len(canonical(node)))
        return node
    value = unpack(document['payload'])
    raw = canonical(value)
    if len(raw) != document['logical_bytes'] or hashlib.sha256(raw).hexdigest() != document['logical_sha256']:
        raise SnapshotError('SNAPSHOT_LOGICAL_HASH_MISMATCH')
    return value


def load(path, *, base=None, dependencies=None):
    """Read and hydrate only. Missing records keep FileNotFoundError semantics."""
    return decode(json.loads(Path(path).read_text(encoding='utf-8')), base=base, dependencies=dependencies)
