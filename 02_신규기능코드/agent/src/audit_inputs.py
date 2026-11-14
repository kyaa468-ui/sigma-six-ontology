"""Capture actual allowlisted reads; never reread inputs or change their return value."""
import contextvars
import copy
import functools
import inspect
import os
import uuid
import storage_objects as objects
from contextlib import contextmanager

_ACTIVE = contextvars.ContextVar('execution_input_capture', default=())


def observed_read(path, value, status='read'):
    if not _ACTIVE.get():
        return
    try:
        import common as C
        path = os.path.abspath(os.fspath(path))
        allowed = {os.path.abspath(x) for x in (C.BRIEFING, C.ACCOUNT, C.POSITIONS, C.JOURNAL,
                   C.MARKETS, os.path.join(C.WORK, 'day_start.json'))}
        if path not in allowed and not (path.startswith(os.path.abspath(C.CONFIG) + os.sep) and path.endswith('.json')):
            return
        event = {'read_id': 'read-' + uuid.uuid4().hex, 'path': path,
                 'value': objects.freeze(value, kind='read_value'), 'status': status,
                 'read_at': C.now().isoformat(timespec='microseconds')}
        for sink in _ACTIVE.get():
            sink.append(event)
    except Exception:
        # Value capture cannot change the value used by the engine.
        for sink in _ACTIVE.get():
            sink.append({'status': 'recording_error'})


@contextmanager
def capture():
    rows = []
    token = _ACTIVE.set((*_ACTIVE.get(), rows))
    try:
        import log_store
        with objects.scope(), log_store.scope():
            yield rows
    finally:
        _ACTIVE.reset(token)


def captured(fn):
    sig = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        audit = bound.arguments.get('audit')
        if audit is None:
            return fn(*args, **kwargs)
        try:
            runtime = runtime_context()
        except Exception as exc:
            runtime = {'recording_status':'recording_error','error':str(exc)}
        runtime['explicit_inputs'] = {k: bound.arguments.get(k) is not None for k in ('p','r','regime')}
        runtime['argument_names'] = list(bound.arguments)
        with capture() as rows:
            try:
                return fn(*args, **kwargs)
            finally:
                audit['actual_reads'] = rows
                audit['runtime_context'] = runtime
    return wrapped


def runtime_context():
    """Call-entry clock and allowlisted environment, never credentials."""
    import common as C
    at=C.now()
    return {'context_version':'EXECUTION_RUNTIME@v1','at':at.isoformat(timespec='microseconds'),
            'date':at.date().isoformat(),
            'module_state':{'common._EXCH':copy.deepcopy(C._EXCH)},
            'environment':{k:os.environ.get(k) for k in ('RUN_ID','RUN_MODE','RUN_ATTEMPT_ID')}}


def active():
    return bool(_ACTIVE.get())
