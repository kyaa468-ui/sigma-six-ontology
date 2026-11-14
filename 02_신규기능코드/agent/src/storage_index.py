"""Rebuildable metadata index. The immutable record/commit remains authoritative."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3

FIELDS = ('run_attempt_id', 'run_id', 'account_scope', 'code', 'instrument_id',
          'judgment_id', 'plan_id', 'order_attempt_id', 'application_id','position_episode_id','rule_trigger_id',
          'review_id', 'validation_id', 'proposal_id', 'scoring_job_id',
          'event_type', 'kind', 'status', 'created_at', 'recorded_at',
          'available_at', 'market_as_of', 'knowledge_as_of', 'event_id',
          'recorded_at_ns', 'confidence_for', 'recommended_action')


def path(base): return Path(base) / 'storage_index.sqlite3'


def projection(value):
    body = value.get('record', value) if isinstance(value, dict) else {}
    if not isinstance(body,dict):return {}
    if isinstance(body.get('payload'),dict):body={**body['payload'],**body}
    return {key: body[key] for key in FIELDS if key in body and
            isinstance(body[key], (str, int, float, bool, type(None)))}


def add(base, relative, manifest_sha, metadata):
    db = path(base)
    if db.is_symlink(): raise ValueError('INDEX_SYMLINK_FORBIDDEN')
    db.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(db, timeout=30)
    try:
        con.execute('PRAGMA journal_mode=DELETE')
        con.execute('CREATE TABLE IF NOT EXISTS records (path TEXT PRIMARY KEY, sha TEXT NOT NULL, metadata TEXT NOT NULL)')
        con.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)',
                    (relative, manifest_sha, json.dumps(metadata, ensure_ascii=False)))
        con.commit()
    finally:con.close()


def entries(base, prefix=''):
    """No creation on read, including the absent-index path."""
    db = path(base)
    if not db.exists(): return
    if db.is_symlink(): raise ValueError('INDEX_SYMLINK_FORBIDDEN')
    con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        # Prefix is a path bound, not a caller-provided SQL fragment.
        for relative, sha, raw in con.execute('SELECT path,sha,metadata FROM records WHERE path>=? AND path<? ORDER BY path',
                                              (prefix, prefix + '\uffff')):
            yield relative, sha, json.loads(raw)
    finally: con.close()


def select(base, prefix, filters=None):
    db=path(base)
    if not db.exists():return
    if db.is_symlink():raise ValueError('INDEX_SYMLINK_FORBIDDEN')
    con=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        clauses=['path>=?','path<?'];params=[prefix,prefix+'\uffff']
        for key,value in (filters or {}).items():
            if key not in FIELDS:continue
            clauses.append('(json_extract(metadata,?)=? OR json_type(metadata,?) IS NULL)')
            params.extend(['$.'+key,value,'$.'+key])
        for relative,sha in con.execute('SELECT path,sha FROM records WHERE '+' AND '.join(clauses)+' ORDER BY path',params):
            yield relative,sha
    finally:con.close()


def indexed_hash(base,relative):
    db=path(base)
    if not db.exists():return None
    con=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        row=con.execute('SELECT sha FROM records WHERE path=?',(relative,)).fetchone()
        return row[0] if row else None
    finally:con.close()


def files(folder, *, recursive=True):
    """Bounded filesystem walk; no rglob list or sorting of the entire archive."""
    folder = Path(folder)
    if not folder.exists(): return
    if folder.is_symlink(): raise ValueError('RECORD_DIRECTORY_SYMLINK_FORBIDDEN')
    with os.scandir(folder) as stream:
        for item in stream:
            if item.is_symlink():
                if item.name.endswith('.json') or item.is_dir():
                    raise ValueError('RECORD_SYMLINK_FORBIDDEN')
            elif item.is_dir(follow_symlinks=False):
                if recursive and item.name not in ('snapshots', 'locks', 'trace_view'):
                    yield from files(item.path)
            elif item.name.endswith('.json'):
                yield Path(item.path)


def entry(base,relative):
    db=path(base)
    if not db.exists():return None
    if db.is_symlink():raise ValueError('INDEX_SYMLINK_FORBIDDEN')
    con=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        row=con.execute('SELECT sha,metadata FROM records WHERE path=?',(relative,)).fetchone()
        return (row[0],json.loads(row[1])) if row else None
    finally:con.close()
