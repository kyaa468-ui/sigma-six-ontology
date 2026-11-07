"""Immutable step-6 records, independent of trading and judgment coverage.

Reading never creates directories or repairs history. Callers receive corruption
explicitly; an unreadable directory must not become a successful empty job.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import common as C
import snapshot_store as snapshots

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}\Z")
_ERRORS = {}


class StoreError(ValueError):
    pass


class StoreBusy(RuntimeError):
    pass


def root():
    return Path(C.WORK) / 'ontology' / 'learning_proposals'


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or '..' in value:
        raise StoreError('Invalid learning proposal identifier')
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _path(kind, identifier):
    return root() / _id(kind) / (_id(identifier) + '.json')


def load(kind, identifier):
    path = _path(kind, identifier)
    try:
        value = snapshots.load(path)
    except FileNotFoundError:
        return None
    except snapshots.SnapshotError as exc:
        raise StoreError(str(exc)) from exc
    if not isinstance(value, dict):
        raise StoreError(f'{kind}/{identifier}: expected object')
    if value.get('format') != 'immutable-learning-proposal-record@v1' or not isinstance(value.get('record'), dict):
        raise StoreError(f'{kind}/{identifier}: missing learning proposal integrity envelope')
    if digest(value['record']) != value.get('content_sha256'):
        raise StoreError(f'{kind}/{identifier}: learning proposal content hash mismatch')
    return value['record']


def archive(kind, identifier, body):
    """Publish exactly one complete value. Same key/content is an idempotent read."""
    if not isinstance(body, dict):
        raise StoreError('Learning proposal records must be objects')
    payload = canonical(snapshots.encode({'format': 'immutable-learning-proposal-record@v1', 'content_sha256': digest(body), 'record': body}))
    target = _path(kind, identifier)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.learning-proposal-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            previous = load(kind, identifier)
            if previous is None or canonical(previous) != canonical(body):
                raise StoreError(f'Immutable learning proposal key conflict: {kind}/{identifier}')
            return previous
        if os.name != 'nt':
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        os.unlink(temporary)
    return copy.deepcopy(body)


@contextmanager
def locked(identifier, timeout=30):
    """Serialize publication of one revision series across processes/threads."""
    path = root() / 'locks' / (_id(identifier) + '.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    # SQLite's reserved writer lock works on Windows and POSIX. These lock-only
    # databases live in research storage, completely separate from order guards.
    connection = sqlite3.connect(path, timeout=timeout)
    try:
        try:
            connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                raise StoreBusy(identifier) from exc
            raise
        yield
        connection.commit()
    finally:
        connection.close()


def records(kind):
    folder = root() / _id(kind)
    result, errors = [], []
    try:
        paths = sorted(folder.iterdir())
    except FileNotFoundError:
        return {'records': [], 'errors': []}
    except OSError as exc:
        return {'records': [], 'errors': [{'kind': kind, 'error': str(exc)}]}
    for path in paths:
        if path.suffix != '.json':
            continue
        try:
            value = load(kind, path.stem)
            if value is None:
                raise StoreError('Record disappeared during read')
            result.append(value)
        except (OSError, ValueError) as exc:
            errors.append({'kind': kind, 'record_id': path.stem, 'error': str(exc)})
    return {'records': result, 'errors': errors}


def record_error(job_id, operation, error):
    """A learning proposal error never changes the historical AI run's coverage status."""
    row = {'error_id': 'learning-proposal-error-' + uuid.uuid4().hex, 'proposal_run_id': job_id,
           'operation': operation, 'error': str(error), 'recorded_at': C.now().isoformat()}
    _ERRORS.setdefault(str(root()), []).append(copy.deepcopy(row))
    try:
        archive('errors', row['error_id'], row)
    except Exception:
        pass
    return {'recording_status': 'recording_error', **row}


def errors(job_id=None):
    stored = records('errors')
    by = {x['error_id']: x for x in stored['records'] if isinstance(x.get('error_id'), str)}
    by.update({x['error_id']: x for x in _ERRORS.get(str(root()), [])})
    return {'records': [x for x in by.values() if job_id is None or x.get('proposal_run_id') == job_id],
            'errors': stored['errors']}
