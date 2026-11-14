"""Immutable step-4 records, independent of trading and judgment coverage.

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
import record_store as records_v2
import storage_objects as objects
import storage_errors
import snapshot_store as snapshots

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}\Z")
_ERRORS = storage_errors.ErrorRegistry()


class StoreError(ValueError):
    pass


def root():
    return C.ontology_root() / 'scoring'


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or '..' in value:
        raise StoreError('Invalid scoring identifier')
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return objects.fingerprint(value)[0]


def _path(kind, identifier):
    return root() / _id(kind) / (_id(identifier) + '.json')


def load(kind, identifier):
    path = _path(kind, identifier)
    try:
        value = records_v2.load(path)
    except FileNotFoundError as exc:
        if path.exists():raise StoreError("INCOMPLETE_RECORD: "+str(path)) from exc
        return None
    except snapshots.SnapshotError as exc:
        raise StoreError(str(exc)) from exc
    if not isinstance(value, dict):
        raise StoreError(f'{kind}/{identifier}: expected object')
    if value.get('format') != 'immutable-scoring-record@v1' or not isinstance(value.get('record'), dict):
        raise StoreError(f'{kind}/{identifier}: missing scoring integrity envelope')
    if not isinstance(value, objects.ObjectDict) and digest(value['record']) != value.get('content_sha256'):
        raise StoreError(f'{kind}/{identifier}: scoring content hash mismatch')
    record=value['record']
    if isinstance(record,objects.ObjectDict):record._record_order=getattr(value,'_record_order',0)
    return record


def archive(kind, identifier, body):
    """Freeze once, publish a committed v2 record, retain old logical hashes."""
    if not isinstance(body, dict): raise StoreError('Records must be objects')
    target = _path(kind, identifier)
    envelope = {'format':'immutable-scoring-record@v1', 'content_sha256':digest(body), 'record':body}
    try:
        records_v2.publish(target, envelope, kind=kind, exclusive=False)
        return records_v2.load(target)['record']
    except ValueError as exc:
        raise StoreError(str(exc)) from exc


@contextmanager
def locked(identifier):
    """Serialize publication of one revision series across processes/threads."""
    path = root() / 'locks' / (_id(identifier) + '.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    # SQLite's reserved writer lock works on Windows and POSIX. These lock-only
    # databases live in scoring storage, completely separate from order guards.
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute('BEGIN IMMEDIATE')
        yield
        connection.commit()
    finally:
        connection.close()


def records(kind):
    errors = []
    rows = list(iter_records(kind, errors=errors))
    return {'records': rows, 'errors': errors}


def iter_records(kind, *, filters=None, fields=None, errors=None):
    for envelope in records_v2.iter_records(root() / _id(kind), filters=filters, errors=errors):
        value = envelope['record']
        if not isinstance(envelope, objects.ObjectDict) and digest(value) != envelope.get('content_sha256'):
            if errors is None: raise StoreError('Record content hash mismatch')
            errors.append({'kind':kind,'error':'Record content hash mismatch'})
            continue
        if isinstance(value,objects.ObjectDict):value._record_order=getattr(envelope,'_record_order',0)
        yield {k:value[k] for k in fields if k in value} if fields is not None else value


def record_error(job_id, operation, error):
    """A scoring error never changes the historical AI run's coverage status."""
    row = {'error_id': 'scoring-error-' + uuid.uuid4().hex, 'scoring_job_id': job_id,
           'operation': operation, 'error': str(error), 'recorded_at': C.now().isoformat()}
    try:
        archive('errors', row['error_id'], row)
    except Exception:
        storage_errors.retain(_ERRORS, str(root()), row)
        print('Unpersisted storage error: ' + str(row), file=__import__('sys').stderr)
    return {'recording_status': 'recording_error', **row}


def errors(job_id=None, *, cursor=None, limit=50):
    failures=[]
    persisted=iter_records('errors',filters={'scoring_job_id':job_id},errors=failures)
    pending=[x for x in _ERRORS.get(str(root()),[]) if job_id is None or x.get('scoring_job_id')==job_id]
    from itertools import chain
    page=records_v2.versioned_page(chain(persisted,pending),scope=str((root(),'errors',job_id)),
        cursor=cursor,limit=limit,key=lambda row:row['error_id'])
    rows=page.pop('items')
    return {**page,'records':rows,'errors':failures,'fallback_state':_ERRORS.state()}
