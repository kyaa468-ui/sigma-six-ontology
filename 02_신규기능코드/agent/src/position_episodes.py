"""Observed holding intervals, independent of monitor retry/cooldown keys.

This audit store never decides whether a monitor runs or a trade is allowed.
Missing/incomplete snapshots cannot prove that a holding was closed.
"""
import json
import math
import sqlite3
import uuid
from pathlib import Path

import common as C


def _scope():
    import execution_trace as E
    return E.account_scope()


def _db():
    path = Path(C.WORK) / 'ontology' / 'position_episodes.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY, scope TEXT, code TEXT, opened TEXT, closed TEXT, origin TEXT, source TEXT)')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS active_episode ON episodes(scope,code) WHERE closed IS NULL')
    db.execute('CREATE TABLE IF NOT EXISTS scope_state (scope TEXT PRIMARY KEY, observed_complete INTEGER NOT NULL)')
    return db


def _report(operation, error):
    try:
        import judgment_coverage as JC
        JC.recording_error(operation, error)
    except Exception:
        pass


def _event(kind, **data):
    try:
        import execution_trace as E
        return E.record(kind, **data)
    except Exception as exc:
        _report('position_episode_event', exc)


def observe(account, source=None):
    """Return positive observed holdings' IDs; incomplete lists never close others."""
    if not isinstance(account, dict) or not isinstance(account.get('holdings'), list):
        return {}
    if account.get('error') or account.get('ok') is False:
        return {}
    complete = account.get('complete', account.get('holdings_complete', True)) is True
    held = {}
    for h in account['holdings']:
        if not isinstance(h, dict) or not h.get('code'):
            complete = False
            continue
        qty = h.get('qty')
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty < 0:
            complete = False
            continue
        if str(h['code']) in held:
            complete = False
        held[str(h['code'])] = qty
    events, result = [], {}
    db = None
    try:
        scope, at = _scope(), C.now().isoformat(timespec='microseconds')
        db = _db()
        db.execute('BEGIN IMMEDIATE')
        active = {x['code']: dict(x) for x in db.execute('SELECT * FROM episodes WHERE scope=? AND closed IS NULL', (scope,))}
        state = db.execute('SELECT observed_complete FROM scope_state WHERE scope=?', (scope,)).fetchone()
        previously_complete = bool(state and state['observed_complete'])
        for code, qty in held.items():
            if qty <= 0:
                continue
            episode = active.get(code)
            if episode is None:
                episode = {'id': 'position-' + uuid.uuid4().hex, 'scope': scope, 'code': code,
                           'opened': at, 'closed': None, 'origin': 'observed_transition' if previously_complete else 'origin_unrecorded',
                           'source': json.dumps(source, ensure_ascii=False)}
                db.execute('INSERT INTO episodes VALUES (:id,:scope,:code,:opened,:closed,:origin,:source)', episode)
                events.append(('position_opened', {'position_episode_id': episode['id'], 'code': code,
                                                   'origin': episode['origin'], 'observed_qty': qty, 'source': source, 'observed_at': at}))
            result[code] = episode['id']
        if complete:
            for code, episode in active.items():
                if held.get(code, 0) == 0:
                    db.execute('UPDATE episodes SET closed=? WHERE id=?', (at, episode['id']))
                    events.append(('position_closed', {'position_episode_id': episode['id'], 'code': code,
                                                       'source': source, 'observed_at': at, 'closure_basis': 'complete_account_snapshot'}))
            db.execute('INSERT OR REPLACE INTO scope_state VALUES (?,1)', (scope,))
        db.commit()
    except Exception as exc:
        if db is not None:
            db.rollback()
        _report('position_episodes.observe', exc)
        return {}
    finally:
        if db is not None:
            db.close()
    for kind, data in events:
        _event(kind, **data)
    return result


def current(code):
    db = None
    try:
        db = _db()
        row = db.execute('SELECT id FROM episodes WHERE scope=? AND code=? AND closed IS NULL', (_scope(), str(code))).fetchone()
        return row['id'] if row else None
    except Exception as exc:
        _report('position_episodes.current', exc)
        return None
    finally:
        if db is not None:
            db.close()


def get(position_episode_id):
    db = None
    try:
        db = _db()
        row = db.execute('SELECT * FROM episodes WHERE id=? AND scope=?', (position_episode_id, _scope())).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        _report('position_episodes.get', exc)
        return None
    finally:
        if db is not None:
            db.close()


def read_episode(position_episode_id):
    """Current stored state only; never create a database or report by writing.

    Historical projections must use position_opened/position_closed events.
    """
    path = Path(C.WORK) / 'ontology' / 'position_episodes.sqlite3'
    if not path.exists():
        return {'status': 'not_found', 'episode': None}
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM episodes WHERE id=? AND scope=?', (position_episode_id, _scope())).fetchone()
            return {'status': 'recorded' if row else 'not_found', 'episode': dict(row) if row else None,
                    'scope': 'current_stored_state_not_historical'}
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {'status': 'recording_error', 'episode': None, 'error': str(exc)}
