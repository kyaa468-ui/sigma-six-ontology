"""Immutable operator views of stored evidence; preparation and reading are separate.

Preparation may write only a view and research-exposure reservations. A browser
read never creates directories, repairs records, queries markets, or runs an AI.
The frozen view keeps its original result versions even as new records arrive.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

import common as C
import snapshot_store

VERSION = 'TRACE_SNAPSHOT@v1'
_ID = re.compile(r'view-[a-f0-9]{32}\Z')


class SnapshotError(ValueError):
    pass


def root():
    return Path(C.WORK) / 'ontology' / 'trace_view'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SnapshotError('INVALID_VIEW_ID')
    return value


def _write_immutable(path, body):
    """A complete file appears once; an interrupted write never becomes JSON."""
    path = Path(path)
    # Public exports remain standalone allowlisted JSON. Only the private view
    # shares immutable blobs with the records from which it was prepared.
    stored = snapshot_store.encode(body) if path.name == 'projection.json' and path.parent.parent == root() / 'snapshots' else body
    payload = canonical(stored).encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.trace-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise SnapshotError('IMMUTABLE_VIEW_CONFLICT')
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        os.unlink(temporary)


def _read(path):
    path = Path(path)
    # Snapshot IDs and server paths are controlled, but a replaced symlink must
    # not turn a local evidence link into access to an arbitrary private file.
    allowed = root().resolve()
    if (not path.resolve().is_relative_to(allowed)
            or any(parent.is_symlink() for parent in (path, *path.parents)
                   if parent != root() and root() in parent.parents)):
        raise SnapshotError('SYMLINK_VIEW_FORBIDDEN')
    value = snapshot_store.decode(json.loads(path.read_text(encoding='utf-8')))
    if not isinstance(value, dict):
        raise SnapshotError('VIEW_OBJECT_REQUIRED')
    return value


def _capture_sources():
    """Bound independent store readers to bytes present before projection starts.

    Research helpers currently read their own stores. A later manifest scan
    cannot certify a different version of a source they already used. Only JSON
    ledger sources are captured; views and mutable SQLite locks are excluded.
    """
    base = Path(C.WORK) / 'ontology'
    if not base.exists():
        return {}
    captured = {}
    for path in sorted(base.rglob('*.json')):
        relative = path.relative_to(base)
        if relative.parts[0] == 'trace_view':
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
            raise SnapshotError('SOURCE_PATH_OUTSIDE_ONTOLOGY')
        captured[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return captured


def _verify_prior_sources(sources, captured):
    if not isinstance(sources, list):
        raise SnapshotError('SOURCE_MANIFEST_REQUIRED')
    for source in sources:
        if not isinstance(source, dict) or captured.get(source.get('path')) != source.get('sha256'):
            raise SnapshotError('SOURCE_NEW_OR_CHANGED_DURING_PROJECTION')


def _verify_sources(sources):
    if not isinstance(sources, list):
        raise SnapshotError('SOURCE_MANIFEST_REQUIRED')
    base = (Path(C.WORK) / 'ontology').resolve()
    seen = {}
    for source in sources:
        if not isinstance(source, dict):
            raise SnapshotError('INVALID_SOURCE_MANIFEST')
        relative, expected = source.get('path'), source.get('sha256')
        if (not isinstance(relative, str) or Path(relative).is_absolute()
                or '..' in Path(relative).parts or not re.fullmatch(r'[a-f0-9]{64}', str(expected))):
            raise SnapshotError('INVALID_SOURCE_REFERENCE')
        path = base / relative
        if not path.resolve().is_relative_to(base) or path.is_symlink():
            raise SnapshotError('SOURCE_PATH_OUTSIDE_ONTOLOGY')
        if relative in seen and seen[relative] != expected:
            raise SnapshotError('CONFLICTING_SOURCE_HASH')
        seen[relative] = expected
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SnapshotError('SOURCE_CHANGED_DURING_PREPARATION')


def check_visibility(projection):
    """Use CURRENT protection, even when the displayed knowledge cutoff is old."""
    import validation_holdout as VH
    ids = projection.get('application_ids', [])
    if not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids):
        raise SnapshotError('INVALID_EXPOSURE_SCOPE')
    VH.assert_discovery_allowed(ids)
    scopes = projection.get('protection_scopes', [])
    if not isinstance(scopes, list) or any(not isinstance(x, dict) for x in scopes):
        raise SnapshotError('INVALID_PROTECTION_SCOPE')
    # Frozen origin metadata remains available even if a source premise is later
    # deleted. Merely finding no current premise must not permit old raw output.
    for scope in scopes:
        if VH.is_reserved_judgment(scope):
            raise SnapshotError('RESERVED_HOLDOUT_DISCOVERY_FORBIDDEN')


def _failure(view_id, operation, exc):
    result = {'status': 'recording_error', 'view_id': view_id,
              'errors': [{'operation': operation, 'reason': str(exc)}]}
    # Failure records contain no projection, raw claim, account or trial data.
    try:
        _write_immutable(root() / 'errors' / (uuid.uuid4().hex + '.json'),
                         {**result, 'recorded_at': C.now().isoformat()})
    except Exception:
        pass
    return result


def build_view_snapshot(market_as_of=None, knowledge_as_of=None):
    import trace_query as Q
    import research_evidence as RE
    view_id = 'view-' + uuid.uuid4().hex
    operation = 'projection'
    try:
        captured_sources = _capture_sources()
        projection = Q.build_projection(market_as_of, knowledge_as_of)
        if not isinstance(projection, dict) or projection.get('schema_version') != 'TRACE_VIEW@v1':
            raise SnapshotError('UNSUPPORTED_VIEW_SCHEMA')
        if projection.get('status') not in ('complete', 'partial'):
            raise SnapshotError('PROJECTION_NOT_READABLE')
        # Do not archive any revealing bytes until the pre-presentation usage
        # reservation is durable. Preparation itself counts as research use.
        operation = 'source_binding'
        _verify_prior_sources(projection.get('source_manifest'), captured_sources)
        operation = 'exposure_reservation'
        check_visibility(projection)
        reservation = RE.reserve_usage(view_id, projection.get('application_ids', []),
                                       source_refs=projection.get('source_manifest', []),
                                       purpose='step8_view_preparation')
        if not isinstance(reservation, dict) or not reservation.get('usage_id'):
            raise SnapshotError('EXPOSURE_RESERVATION_UNCONFIRMED')
        operation = 'source_verification'
        _verify_sources(projection.get('source_manifest'))
        check_visibility(projection)
        manifest = {'schema_version': VERSION, 'view_id': view_id, 'created_at': C.now().isoformat(),
                    'market_as_of': projection.get('market_as_of'),
                    'knowledge_as_of': projection.get('knowledge_as_of'),
                    'projection_schema': projection['schema_version'], 'projection_hash': digest(projection),
                    'source_manifest': copy.deepcopy(projection['source_manifest']),
                    'exposure_reservation_id': reservation['usage_id'],
                    'exposure_reservation_hash': digest(reservation),
                    'status': projection['status']}
        folder = root() / 'snapshots' / view_id
        operation = 'snapshot_publication'
        _write_immutable(folder / 'projection.json', projection)
        _write_immutable(folder / 'manifest.json', manifest)
        # A current reservation introduced during preparation may leave private
        # incomplete files, but never a committed view the server will return.
        check_visibility(projection)
        _verify_sources(projection['source_manifest'])
        _write_immutable(folder / 'commit.json', {'schema_version': VERSION, 'view_id': view_id,
                         'manifest_hash': digest(manifest), 'projection_hash': digest(projection),
                         'committed_at': C.now().isoformat()})
        return {'status': manifest['status'], 'view_id': view_id, 'manifest': manifest,
                'errors': copy.deepcopy(projection.get('errors', []))}
    except Exception as exc:
        return _failure(view_id, operation, exc)


def load_view_snapshot(view_id):
    """Pure, fail-closed read. Never re-read live inputs to rewrite old facts."""
    try:
        identifier = _identifier(view_id)
        folder = root() / 'snapshots' / identifier
        if not folder.exists():
            return {'status': 'unavailable', 'view_id': identifier, 'errors': [{'reason': 'VIEW_NOT_FOUND'}]}
        if not (folder / 'commit.json').exists():
            return {'status': 'unfinished', 'view_id': identifier, 'errors': [{'reason': 'VIEW_NOT_COMMITTED'}]}
        commit = _read(folder / 'commit.json')
        manifest = _read(folder / 'manifest.json')
        projection = _read(folder / 'projection.json')
        if (manifest.get('schema_version') != VERSION or commit.get('schema_version') != VERSION
                or manifest.get('view_id') != identifier or commit.get('view_id') != identifier
                or manifest.get('status') not in ('complete', 'partial')
                or projection.get('schema_version') != 'TRACE_VIEW@v1'
                or commit.get('manifest_hash') != digest(manifest)
                or commit.get('projection_hash') != digest(projection)
                or manifest.get('projection_hash') != digest(projection)
                or manifest.get('status') != projection.get('status')):
            raise SnapshotError('VIEW_INTEGRITY_MISMATCH')
        # Confirm the durable pre-presentation reservation has not disappeared.
        import case_store as CS
        reservation = CS.load('usage_events', manifest['exposure_reservation_id'])
        if (not reservation or digest(reservation) != manifest.get('exposure_reservation_hash')
                or reservation.get('owner_id') != identifier
                or sorted(reservation.get('application_ids', [])) != sorted(projection.get('application_ids', []))):
            raise SnapshotError('EXPOSURE_RESERVATION_MISSING_OR_CHANGED')
        check_visibility(projection)
        return {'status': manifest['status'], 'view_id': identifier, 'manifest': manifest,
                'projection': projection, 'errors': copy.deepcopy(projection.get('errors', []))}
    except FileNotFoundError:
        return {'status': 'recording_error', 'view_id': view_id,
                'errors': [{'reason': 'COMMITTED_VIEW_COMPONENT_MISSING'}]}
    except Exception as exc:
        return {'status': 'protected' if 'HOLDOUT' in str(exc) else 'recording_error',
                'view_id': view_id, 'errors': [{'reason': str(exc)}]}
