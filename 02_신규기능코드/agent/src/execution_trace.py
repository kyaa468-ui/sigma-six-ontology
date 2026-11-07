"""Immutable execution evidence and a durable, fail-closed order dispatch guard.

A dispatch claim is committed before the sender runs. A consumed claim is never
reopened, even when the sender or result persistence fails. This guarantees at
most one invocation of the sender for a preserved account/attempt key, not that
the broker necessarily received an order. Audit JSON writes remain fail-open.
"""
from __future__ import annotations

import copy
import ast
import hashlib
import inspect
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from datetime import timedelta, timezone
from pathlib import Path

import common as C
import snapshot_store as snapshots

SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}\Z")
PAYLOAD_FIELDS = ("code", "side", "qty", "confidence", "rule_exit", "judgment_id", "plan_id", "rule_trigger_id")


class GuardError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _now():
    return C.now().isoformat(timespec="microseconds")


def _root():
    return Path(C.WORK) / "ontology"


def _context():
    return {"run_id": os.environ.get("RUN_ID"), "run_attempt_id": os.environ.get("RUN_ATTEMPT_ID")}


def _id(value, label="identifier"):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value) or ".." in value:
        raise GuardError("INVALID_ID", f"Invalid {label}")
    return value


def _reference_status(value):
    if value is None:
        return "missing"
    return "valid" if isinstance(value, str) and SAFE_ID.fullmatch(value) and ".." not in value else "invalid"


def _clean(value):
    """Audit infinities are explicit, never JSON NaN or silently observed zeros."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"value": None, "status": "non_finite", "representation": str(value)}
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def account_scope(broker=None):
    """Same configured scope in Hook/server/broker; no credentials are inspected.

    This repository runs one configured team account. ONTOLOGY_ACCOUNT_SCOPE can
    explicitly distinguish another account within the same workspace. The
    optional broker argument is accepted for call-site convenience only.
    """
    alias = os.environ.get("ONTOLOGY_ACCOUNT_SCOPE") or os.environ.get("ACCOUNT_NAME") or "team-account"
    return "account-" + _hash([str(Path(C.ROOT).resolve()), C.mode(), alias])[:32]


def _payload(value):
    if not isinstance(value, dict):
        raise GuardError("INVALID_ORDER_PAYLOAD", "Order payload must be an object")
    out = {key: value.get(key) for key in PAYLOAD_FIELDS}
    if not isinstance(out["code"], str) or not out["code"].strip():
        raise GuardError("INVALID_ORDER_PAYLOAD", "Nonempty instrument code required")
    out["code"] = out["code"].strip()
    out["side"] = {"buy": "매수", "sell": "매도"}.get(out["side"], out["side"])
    if out["side"] not in ("매수", "매도"):
        raise GuardError("INVALID_ORDER_PAYLOAD", "Unknown order side")
    qty = out["qty"]
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty) or int(qty) != qty:
        raise GuardError("INVALID_ORDER_PAYLOAD", "Integer quantity required")
    out["qty"] = int(qty)
    if out["confidence"] is not None:
        score = out["confidence"]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise GuardError("INVALID_ORDER_PAYLOAD", "Finite raw confidence required")
        out["confidence"] = float(score)
    out["rule_exit"] = bool(out["rule_exit"])
    # Auxiliary metadata is hashed exactly, even if it is not a valid reference.
    # Its validation must not introduce a new trading veto. Only scope/attempt
    # IDs control the dispatch ledger or a path; links validate before loading.
    out.update(order_type="market", exchange="KRX")
    return out


def intent_fingerprint(payload):
    """Canonical logical intent, also used by legacy Hook attempt allocation."""
    return _hash(_payload(payload))


def _db_path():
    return _root() / "order_dispatch.sqlite3"


def _connect(create=False, readonly=False):
    path = _db_path()
    marker = path.with_suffix(".identity.json")
    if marker.exists() and not path.exists():
        raise OSError("Previously initialized dispatch ledger is missing; refusing to recreate it")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    # Inspection and dispatch cannot silently recreate a missing ledger.
    if create and readonly:
        raise ValueError("Read-only dispatch access cannot create a ledger")
    connection = sqlite3.connect(str(path) if create else path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                                 timeout=15, isolation_level=None, uri=not create)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=15000")
        if create and not marker.exists():
            # An existing nonempty ledger without its identity is not a fresh store.
            table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='order_attempts'").fetchone()
            if table and connection.execute("SELECT 1 FROM order_attempts LIMIT 1").fetchone():
                raise OSError("Dispatch ledger identity is missing; refusing to reset existing attempts")
            connection.execute("""CREATE TABLE IF NOT EXISTS order_attempts (
                account_scope TEXT NOT NULL, order_attempt_id TEXT NOT NULL,
                plan_id TEXT, run_id TEXT, run_attempt_id TEXT, created_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reserved','claimed','complete')),
                payload_hash TEXT, payload_json TEXT, claimed_at TEXT, completed_at TEXT,
                result_json TEXT, PRIMARY KEY(account_scope, order_attempt_id))""")
            connection.execute("CREATE TABLE IF NOT EXISTS guard_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO guard_meta VALUES(1,?)", (uuid.uuid4().hex,))
            identity = connection.execute("SELECT identity FROM guard_meta WHERE singleton=1").fetchone()[0]
            fd, temporary = tempfile.mkstemp(prefix=".dispatch-identity-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(_json({"identity": identity})); stream.flush(); os.fsync(stream.fileno())
                try: os.link(temporary, marker)
                except FileExistsError: pass
            finally:
                os.unlink(temporary)
        identity = connection.execute("SELECT identity FROM guard_meta WHERE singleton=1").fetchone()
        if identity is None or json.loads(marker.read_text(encoding="utf-8")).get("identity") != identity[0]:
            raise OSError("Dispatch ledger identity mismatch")
        return connection
    except BaseException:
        connection.close()
        raise


def _decode(row):
    if row is None:
        return None
    row = dict(row)
    payload_json, result_json = row.pop("payload_json"), row.pop("result_json")
    row["payload"] = json.loads(payload_json) if payload_json else None
    row["result"] = json.loads(result_json) if result_json else None
    row["issued_plan_id"] = row["plan_id"]
    row["submitted_plan_id"] = (row["payload"] or {}).get("plan_id")
    if row["payload"] is None:
        plan_status = "not_submitted"
    else:
        # Reservation is an issuance relationship, not evidence that the caller
        # used that plan. Never fill in an omitted submitted plan from it.
        row["plan_id"] = row["submitted_plan_id"]
        if row["issued_plan_id"] == row["submitted_plan_id"]:
            plan_status = "linked" if row["plan_id"] else "unplanned"
        else:
            plan_status = "missing" if row["submitted_plan_id"] is None else "mismatch"
    row["plan_link"] = {"status": plan_status, "issued_plan_id": row["issued_plan_id"],
                        "submitted_plan_id": row["submitted_plan_id"]}
    if any(_reference_status(value) == "invalid" for value in (row["issued_plan_id"], row["submitted_plan_id"])):
        row["plan_link"]["status"] = "invalid"
    row["reference_validation"] = {key: _reference_status((row["payload"] or {}).get(key))
                                   for key in ("plan_id", "judgment_id", "rule_trigger_id")}
    row["scope"] = row["account_scope"]
    row["date"] = (row.get("claimed_at") or row["created_at"])[:10]
    row["status"] = row["state"]
    return row


def _failure(exc, attempt_id=None, *, consumed=False):
    code = exc.code if isinstance(exc, GuardError) else "DISPATCH_GUARD_ERROR"
    if not isinstance(exc, GuardError):
        _report("dispatch_guard", exc)
    return {"ok": False, "status": "error", "error_code": code, "error": str(exc),
            "order_attempt_id": attempt_id,
            "execution_quality": "unknown",
            "submission_status": "submission_unknown" if consumed or code == "DISPATCH_GUARD_ERROR" and attempt_id else "not_sent",
            "sent_by_this_call": False,
            "recording_status": "recording_error" if code == "DISPATCH_GUARD_ERROR" else "recorded"}


def _report(operation, exc):
    try:
        import judgment_coverage as JC
        return JC.recording_error(operation, exc)
    except Exception:
        return {"recording_status": "recording_error", "error": str(exc)}


def reserve_attempt(scope, plan_id=None, attempt_id=None):
    """Issue a stable ID before dispatch. Existing consumed IDs never reset."""
    aid = attempt_id or "order-" + uuid.uuid4().hex
    db = None
    try:
        _id(scope, "account scope"); _id(aid, "order attempt")
        if plan_id is not None and not isinstance(plan_id, str):
            raise GuardError("INVALID_PLAN_METADATA", "Plan reference must be a string or null")
        db = _connect(create=True)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM order_attempts WHERE account_scope=? AND order_attempt_id=?", (scope, aid)).fetchone()
        if row:
            if row["plan_id"] != plan_id:
                raise GuardError("ORDER_ATTEMPT_CONFLICT", "Attempt was reserved for a different plan")
        else:
            context = _context()
            db.execute("INSERT INTO order_attempts(account_scope,order_attempt_id,plan_id,run_id,run_attempt_id,created_at,state) VALUES(?,?,?,?,?,?,?)",
                       (scope, aid, plan_id, context["run_id"], context["run_attempt_id"], _now(), "reserved"))
            row = db.execute("SELECT * FROM order_attempts WHERE account_scope=? AND order_attempt_id=?", (scope, aid)).fetchone()
        db.commit()
        return {**_decode(row), "recording_status": "recorded"}
    except Exception as exc:
        if db is not None and db.in_transaction:
            db.rollback()
        return _failure(exc, aid)
    finally:
        if db is not None:
            db.close()


def _inspect_or_claim(scope, attempt_id, payload, claim=False):
    _id(scope, "account scope")
    if not attempt_id:
        raise GuardError("ORDER_ATTEMPT_REQUIRED", "Obtain and preserve an order_attempt_id before sending")
    _id(attempt_id, "order attempt")
    normalized = _payload(payload)
    if claim and normalized["qty"] <= 0:
        raise GuardError("INVALID_ORDER_PAYLOAD", "Positive quantity required for broker dispatch")
    digest = _hash(normalized)
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM order_attempts WHERE account_scope=? AND order_attempt_id=?", (scope, attempt_id)).fetchone()
        if row is None:
            raise GuardError("UNKNOWN_ORDER_ATTEMPT", "Attempt must be reserved before sending")
        if row["payload_hash"] is not None and row["payload_hash"] != digest:
            raise GuardError("ORDER_ATTEMPT_CONFLICT", "Same attempt ID cannot change order content")
        if row["payload_hash"] is None:
            db.execute("UPDATE order_attempts SET payload_hash=?,payload_json=? WHERE account_scope=? AND order_attempt_id=?",
                       (digest, _json(normalized), scope, attempt_id))
        won = bool(claim and row["state"] == "reserved")
        if won:
            db.execute("UPDATE order_attempts SET state='claimed',claimed_at=? WHERE account_scope=? AND order_attempt_id=? AND state='reserved'",
                       (_now(), scope, attempt_id))
        fresh = db.execute("SELECT * FROM order_attempts WHERE account_scope=? AND order_attempt_id=?", (scope, attempt_id)).fetchone()
        # This commit is the point of no return; never call sender before it succeeds.
        db.commit()
        return _decode(fresh), won
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def probe_attempt(scope, attempt_id, payload):
    """Bind intent once and inspect its state, without consuming dispatch rights."""
    try:
        row, _ = _inspect_or_claim(scope, attempt_id, payload)
        return {**row, "recording_status": "recorded"}
    except Exception as exc:
        return _failure(exc, attempt_id)


def _store_result(scope, attempt_id, result):
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute("UPDATE order_attempts SET state='complete',result_json=?,completed_at=? WHERE account_scope=? AND order_attempt_id=? AND state='claimed'",
                            (_json(result), _now(), scope, attempt_id))
        if cursor.rowcount != 1:
            raise GuardError("DISPATCH_STATE_CONFLICT", "Dispatch claim is missing or already completed")
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def replay_result(row):
    if row.get("result") is not None:
        result = {**row["result"], "replayed": True, "order_attempt_id": row["order_attempt_id"]}
        if result.get("submission_status") == "submission_unknown":
            result.setdefault("execution_quality", "unknown")
        return result
    return {"ok": False, "order_attempt_id": row["order_attempt_id"], "replayed": True,
            "submission_status": "submission_unknown", "recording_status": "recorded",
            "execution_quality": "unknown",
            "error_code": "DISPATCH_ALREADY_CLAIMED",
            "error": "Dispatch rights were consumed; receipt unavailable. Query only, do not resend."}


def dispatch_once(scope, attempt_id, payload, sender):
    """Consume one durable right, then call sender at most once across processes."""
    try:
        row, won = _inspect_or_claim(scope, attempt_id, payload, claim=True)
    except Exception as exc:
        return _failure(exc, attempt_id)
    if not won:
        return replay_result(row)
    event = record("dispatch_claimed", order_attempt_id=attempt_id, plan_id=row["plan_id"],
                   issued_plan_id=row["issued_plan_id"], plan_link=row["plan_link"],
                   account_scope=scope, payload=row["payload"], judgment_id=row["payload"].get("judgment_id"))
    try:
        sent = sender()
        if not isinstance(sent, dict):
            raise ValueError("Broker sender returned no structured response")
        result = {**sent, "order_attempt_id": attempt_id, "replayed": False,
                  "submission_status": sent.get("submission_status") or ("accepted" if sent.get("ok") else "rejected"),
                  "recording_status": sent.get("recording_status", "recorded")}
    except Exception as exc:
        result = {"ok": False, "order_attempt_id": attempt_id, "replayed": False,
                  "submission_status": "submission_unknown", "recording_status": "recorded",
                  "execution_quality": "unknown",
                  "error_code": "ORDER_RESPONSE_UNKNOWN", "error": f"{type(exc).__name__}: {exc}"}
    if event.get("recording_status") != "recorded":
        result["recording_status"] = "recording_error"
    try:
        _store_result(scope, attempt_id, result)
    except Exception as exc:
        # Preserve the received result if available, but never advertise a durable
        # receipt. The committed claim survives and replay will be unknown.
        _report("dispatch_result", exc)
        result.update(recording_status="recording_error", result_persisted=False)
    audit = record("dispatch_result", order_attempt_id=attempt_id, plan_id=row["plan_id"],
                   issued_plan_id=row["issued_plan_id"], plan_link=row["plan_link"],
                   account_scope=scope, payload=row["payload"], result=result,
                   judgment_id=row["payload"].get("judgment_id"))
    if audit.get("recording_status") != "recorded":
        result["recording_status"] = "recording_error"
    return result


def _archive(kind, identifier, row):
    _id(kind, "record kind"); _id(identifier)
    run = row.get("run_attempt_id") or "unscoped"
    _id(run, "run attempt")
    path = _root() / "executions" / run / kind / (identifier + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".execution-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_json(snapshots.encode(_clean(row))))
            stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    return path


def record(kind, **fields):
    """Best-effort append-only evidence; dispatch safety does not depend on it."""
    identifier = "execution-" + uuid.uuid4().hex
    try:
        row = {"schema_version": "execution@v1", "event_id": identifier, "kind": kind,
               "created_at": _now(), **_context(), **copy.deepcopy(fields)}
        _archive(kind, identifier, row)
        return {**row, "recording_status": "recorded"}
    except Exception as exc:
        _report("execution_" + str(kind), exc)
        return {"event_id": identifier, "recording_status": "recording_error", "error": str(exc)}


def record_once(kind, request_key, **fields):
    """Archive an observation once for a caller-owned, content-stable key.

    This only deduplicates audit records. It grants no broker dispatch rights.
    """
    identifier = "execution-" + _hash([kind, request_key])
    try:
        body = _clean(copy.deepcopy(fields))
        row = {"schema_version": "execution@v1", "event_id": identifier, "kind": kind,
               "created_at": _now(), **_context(), **body, "content_hash": _hash(body)}
        try:
            _archive(kind, identifier, row)
        except FileExistsError:
            run = row.get("run_attempt_id") or "unscoped"
            _id(run, "run attempt"); _id(kind, "record kind")
            previous = snapshots.load(_root() / "executions" / run / kind / (identifier + ".json"))
            if previous.get("content_hash") != row["content_hash"]:
                raise GuardError("AUDIT_KEY_CONFLICT", "An immutable audit key already has different content")
            return {**previous, "recording_status": "recorded", "replayed": True}
        return {**row, "recording_status": "recorded", "replayed": False}
    except Exception as exc:
        _report("execution_" + str(kind), exc)
        return {"event_id": identifier, "recording_status": "recording_error", "error": str(exc)}


_VERIFIED_ENGINE_SOURCES = {}


def _literal_identity(value):
    """Serialize only known inert values; never invoke an object's repr hook."""
    if type(value) in (str, int, float, bool, type(None)):
        return {'type':type(value).__name__,'value':value}
    if type(value) in (tuple, list):
        return {'type':type(value).__name__,'value':[_literal_identity(x) for x in value]}
    if type(value) is dict and all(type(k) is str for k in value):
        return {'type':'dict','value':{k:_literal_identity(v) for k,v in value.items()}}
    if type(value) is timezone:
        return {'type':'timezone','offset_seconds':value.utcoffset(None).total_seconds(),'name':value.tzname(None)}
    raise ValueError('unsupported immutable global value')


def _safe_global_value(node, known):
    """A small, explicit interpreter for constants, paths and fixed timezones."""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        pass
    if isinstance(node,ast.Name) and node.id in known:
        return known[node.id]
    if isinstance(node,ast.Call):
        names=[]; target=node.func
        while isinstance(target,ast.Attribute): names.insert(0,target.attr);target=target.value
        if isinstance(target,ast.Name): names.insert(0,target.id)
        allowed={'os.path.join':os.path.join,'os.path.dirname':os.path.dirname,'os.path.abspath':os.path.abspath,
                 'timezone':timezone,'timedelta':timedelta,'dict':dict}
        function=allowed.get('.'.join(names))
        if function is not None and all(k.arg is not None for k in node.keywords):
            args=[_safe_global_value(a,known) for a in node.args]
            kwargs={k.arg:_safe_global_value(k.value,known) for k in node.keywords}
            return function(*args,**kwargs)
    raise ValueError('unsupported global expression')


def _global_identity(module, source, path):
    """Bind top-level literals and defaults to loaded values without exec().

    Unknown assignments fail closed. Current call inputs still retain dynamic
    settings; they never establish that a changed module constant was loaded.
    """
    known={'__file__':str(path)}; actual={}; expected={}; unknown=[]; dynamic=[]
    tree=ast.parse(source)
    for node in tree.body:
        if isinstance(node,(ast.Assign,ast.AnnAssign)):
            targets=node.targets if isinstance(node,ast.Assign) else [node.target]
            if any(not isinstance(t,ast.Name) for t in targets):
                unknown.append('assignment_target');continue
            for target in targets:
                name=target.id
                try:
                    if path.name=='common.py' and name=='_EXCH' and ast.literal_eval(node.value) is None:
                        cached=getattr(module,name)
                        if cached is not None and type(cached) is not dict:raise ValueError('invalid exchange cache')
                        actual[name]=_literal_identity(cached)
                        known[name]=cached
                        dynamic.append('common._EXCH')
                        continue
                    known[name]=_safe_global_value(node.value,known)
                    expected[name]=_literal_identity(known[name])
                    actual[name]=_literal_identity(getattr(module,name))
                except (ValueError,TypeError,AttributeError,OverflowError) as exc:
                    unknown.append(name)
        elif isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            label='defaults:'+node.name
            try:
                expected[label]=_literal_identity((tuple(_safe_global_value(x,known) for x in node.args.defaults) or None,
                    {a.arg:_safe_global_value(v,known) for a,v in zip(node.args.kwonlyargs,node.args.kw_defaults) if v is not None} or None))
                original=inspect.unwrap(getattr(module,node.name))
                actual[label]=_literal_identity((original.__defaults__,original.__kwdefaults__))
            except (ValueError,TypeError,AttributeError,OverflowError):
                unknown.append(label)
        elif isinstance(node,(ast.If,ast.For,ast.While,ast.With,ast.Try)):
            # Conditional/nested initialization cannot be inferred from the
            # source alone. Common's stdout-only try block has no assignments.
            if any(isinstance(n,(ast.Assign,ast.AnnAssign,ast.AugAssign)) for n in ast.walk(node)):
                unknown.append('conditional_global_initialization')
    mismatches=sorted(k for k in expected if actual.get(k)!=expected[k])
    return {'loaded_values':actual,'matches_source':not unknown and not mismatches,
            'unknown_globals':sorted(set(unknown)),'mismatched_globals':mismatches,
            'captured_dynamic_globals':dynamic}


def _code_hash(code):
    # marshal encodes string interning/object references, so equal loaded and
    # freshly compiled code can have different byte streams. Hash code fields.
    def constant(value):
        if inspect.iscode(value):
            return {'code': _code_hash(value)}
        if isinstance(value, (tuple, frozenset)):
            items = [constant(v) for v in value]
            return {type(value).__name__: sorted(items, key=_json) if isinstance(value, frozenset) else items}
        return {'type': type(value).__name__, 'value': repr(value)}
    fields = {key: getattr(code, key) for key in ('co_name', 'co_qualname', 'co_argcount',
              'co_posonlyargcount', 'co_kwonlyargcount', 'co_flags', 'co_firstlineno',
              'co_names', 'co_varnames', 'co_freevars', 'co_cellvars')}
    fields.update(bytecode=code.co_code.hex(), exceptions=code.co_exceptiontable.hex(),
                  constants=[constant(v) for v in code.co_consts])
    return _hash(fields)


def _runtime_source(name, path):
    """Match source to loaded code; a changed file is not proof of execution.

    Function code hashes also cover monkeypatches/reloads. A previously verified
    source can explain a still-running old module after its disk file changed.
    Without that match the disk body is kept only as an unverified reference.
    """
    module = sys.modules.get(name)
    if module is None:
        entry = sys.modules.get('__main__')
        entry_path = getattr(entry,'__file__',None)
        if entry_path and Path(entry_path).resolve() == path.resolve():
            module = entry
    source = path.read_text(encoding="utf-8")
    compiled = compile(source, str(path), "exec", dont_inherit=True, optimize=sys.flags.optimize)
    if name in ('data_prep','intraday'):
        # Only these pure functions are executed by candidate replay. Entry
        # point locals and collection paths cannot establish their identity,
        # and are deliberately outside this narrowly declared source scope.
        names = {'screen'} if name=='data_prep' else {'screen','rank_pct','fill_up','dips','etf'}
        expected = {code.co_name:_code_hash(code) for code in compiled.co_consts
                    if inspect.iscode(code) and code.co_name in names}
        loaded, defaults, invalid = {}, {}, []
        nodes = {node.name:node for node in ast.parse(source).body if isinstance(node,ast.FunctionDef) and node.name in names}
        for key in names:
            try:
                fn=inspect.unwrap(getattr(module,key));loaded[key]=_code_hash(fn.__code__)
                defaults[key]=_literal_identity((fn.__defaults__,fn.__kwdefaults__))
                node=nodes[key]
                expected_default=_literal_identity((tuple(ast.literal_eval(x) for x in node.args.defaults) or None,
                    {a.arg:ast.literal_eval(v) for a,v in zip(node.args.kwonlyargs,node.args.kw_defaults) if v is not None} or None))
                if defaults[key]!=expected_default:invalid.append(key)
            except (ValueError,TypeError,AttributeError,KeyError):
                invalid.append(key)
        matched = loaded==expected and not invalid
        return source, {'status':'verified_loaded_source' if matched else 'unverified_loaded_source',
            'loaded_function_hashes':loaded,'loaded_defaults':defaults,'unverified_functions':sorted(set(invalid)|{k for k in names if loaded.get(k)!=expected.get(k)}),
            'scope':'explicit_pure_candidate_functions_only; common engine is independently verified',
            'source_sha256':hashlib.sha256(source.encode()).hexdigest()}
    expected = {code.co_name: _code_hash(code)
                for code in compiled.co_consts if inspect.iscode(code) and code.co_name != '<module>'}
    runtime = {}
    for key in expected:
        value = getattr(module, key, None)
        try:
            original = inspect.unwrap(value)
            runtime[key] = _code_hash(original.__code__)
        except (AttributeError, ValueError):
            runtime[key] = None
    globals_now = _global_identity(module,source,path)
    loaded_hash = _hash({'functions':runtime,'globals':globals_now['loaded_values']})
    current_hash = hashlib.sha256(source.encode()).hexdigest()
    cache_key = (name, loaded_hash)
    if runtime and runtime == expected and globals_now['matches_source']:
        _VERIFIED_ENGINE_SOURCES[cache_key] = source
        chosen, status = source, 'verified_loaded_source'
    elif cache_key in _VERIFIED_ENGINE_SOURCES:
        cached = _VERIFIED_ENGINE_SOURCES[cache_key]
        # Re-evaluate cached bindings too: source edits or runtime mutation must
        # not reuse a formerly verified body with different loaded globals.
        cached_globals = _global_identity(module,cached,path)
        if cached_globals['matches_source']:
            chosen, status = cached, 'verified_loaded_source'
        else:
            chosen, status = source, 'unverified_loaded_source'
    else:
        chosen, status = source, 'unverified_loaded_source'
    chosen_globals = _global_identity(module,chosen,path)
    return chosen, {'status': status, 'loaded_function_hashes': runtime, 'globals':chosen_globals,
                    'loaded_code_hash': loaded_hash, 'disk_source_sha256': current_hash,
                    'source_sha256': hashlib.sha256(chosen.encode()).hexdigest(),
                    'disk_differs_from_loaded_source': chosen != source,
                    'unverified_functions': sorted(key for key in expected if runtime.get(key) != expected[key])
                        if status != 'verified_loaded_source' else []}


def _pure_policy_sources():
    """Verify only the pure policy interpreter, excluding registry I/O/state.

    ContextVar instances and storage functions are deliberately outside this
    identity. They never execute in the historical replay environment.
    """
    specifications = {
        'policy_registry.py': (('semantic_params','evaluate_adjustment'), ('MARKER',)),
        'condition_contract.py': (('validate_condition','_field','_value','evaluate_condition'),
                                  ('VERSION','FIELDS','NUMERIC','PRE_JUDGMENT')),
    }
    sources, identities = {}, {}
    for name, (functions, constants) in specifications.items():
        path = Path(C.AGENT) / 'src' / name
        source = path.read_text(encoding='utf-8')
        module = __import__(name[:-3])
        compiled = compile(source,str(path),'exec',dont_inherit=True,optimize=sys.flags.optimize)
        expected = {code.co_name:_code_hash(code) for code in compiled.co_consts
                    if inspect.iscode(code) and code.co_name in functions}
        loaded = {key:_code_hash(inspect.unwrap(getattr(module,key)).__code__) for key in functions}
        literal_nodes = {target.id:node.value for node in ast.parse(source).body if isinstance(node,ast.Assign)
                         for target in node.targets if isinstance(target,ast.Name) and target.id in constants}
        def value(raw):
            if type(raw) is set: return {'set':sorted(raw)}
            return _literal_identity(raw)
        expected_constants = {key:value(ast.literal_eval(literal_nodes[key])) for key in constants}
        loaded_constants = {key:value(getattr(module,key)) for key in constants}
        matched = expected == loaded and expected_constants == loaded_constants
        sources[name] = source
        identities[name] = {'status':'verified_loaded_source' if matched else 'unverified_loaded_source',
            'functions':loaded,'constants':loaded_constants,
            'scope':'named_pure_functions_and_literal_constants_only'}
    return sources, {'status':'verified_loaded_source' if all(v['status']=='verified_loaded_source' for v in identities.values()) else 'unverified_loaded_source',
                     'modules':identities}


def _definition_snapshot():
    captured = {name: _runtime_source(name[:-3], Path(C.AGENT) / "src" / name)
                for name in ("common.py", "sizing.py", "risk.py")}
    sources = {name: value[0] for name, value in captured.items()}
    try:
        policy_sources, policy_identity = _pure_policy_sources()
        sources.update(policy_sources)
    except Exception as exc:
        policy_identity = {'status':'unverified_loaded_source','reason':type(exc).__name__}
    identities = {name: value[1] for name, value in captured.items()}
    identity = {'version': 'LOADED_ENGINE_SOURCE@v1', 'modules': identities,
                'status': 'verified_loaded_source' if all(v['status'] == 'verified_loaded_source' for v in identities.values())
                          else 'unverified_loaded_source',
                'scope': 'loaded_module_function_code_and_supported_global_constants_and_defaults; dynamic settings are preserved in each call inputs',
                'policy_interpreter':policy_identity}
    identifier = "definition-" + _hash({'sources':sources, 'source_identity':identity})
    body = {"definition_id": identifier, "run_attempt_id": "definitions", "kind": "engine_definition",
            "sha256": _hash(sources), "sources": sources, "source_identity": identity,
            'policy_source_identity':policy_identity}
    try:
        _archive("engine_definition", identifier, body)
    except FileExistsError:
        pass
    return {"definition_id": identifier, "sha256": body["sha256"], 'source_identity_status': identity['status']}


def capture_definition():
    """Research metadata for this call; a failure never changes order permission.

    The original plan snapshot is retained. Hooks and direct orders additionally
    bind their own definition instead of borrowing a possibly older plan's code.
    """
    try:
        return {**_definition_snapshot(), "recording_status": "recorded"}
    except Exception as exc:
        print(f"execution definition unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return {"definition_id": None, "recording_status": "recording_error", "error": str(exc)}


def load_plan(plan_id):
    """Load only an explicit immutable plan ID; never guess from code or time."""
    try:
        _id(plan_id, "plan")
        for path in (_root() / "executions").glob("*/order_plan/*.json"):
            try:
                row = snapshots.load(path)
            except (OSError, ValueError):
                continue
            if row.get("plan_id") == plan_id:
                return row
        return None
    except (GuardError, OSError, ValueError):
        return None


def save_plan(plan, inputs, judgment_id=None, rule_trigger_id=None, scope=None):
    pid = "plan-" + uuid.uuid4().hex
    scope = scope or account_scope()
    reserved = reserve_attempt(scope, plan_id=pid)
    if reserved.get("status") == "error":
        return {"plan_id": pid, **reserved}
    aid = reserved["order_attempt_id"]
    try:
        definition = _definition_snapshot()
    except Exception as exc:
        _report("execution_definitions", exc)
        definition = {"recording_status": "recording_error", "error": str(exc)}
    event = record("order_plan", plan_id=pid, order_attempt_id=aid, account_scope=scope,
                   definition=definition, position_episode_id=inputs.get("position_episode_id"),
                   judgment_id=judgment_id, rule_trigger_id=rule_trigger_id,
                   plan=copy.deepcopy(plan), inputs=copy.deepcopy(inputs))
    status = "recording_error" if definition.get("recording_status") == "recording_error" else event["recording_status"]
    return {"plan_id": pid, "order_attempt_id": aid, "recording_status": status,
            "event_id": event["event_id"]}


def pending_orders(date=None):
    """Return consumed attempts; callers determine confirmed execution separately."""
    if not _db_path().exists():
        if _db_path().with_suffix(".identity.json").exists():
            raise OSError("Previously initialized dispatch ledger is missing")
        return []
    db = _connect()
    try:
        rows = [_decode(row) for row in db.execute("SELECT * FROM order_attempts WHERE state!='reserved' ORDER BY created_at,order_attempt_id")]
        return [row for row in rows if (date is None or row["date"] == str(date)) and
                not (row.get("result") or {}).get("submission_status") == "rejected"]
    finally:
        db.close()


def _read_events():
    events, errors = [], []
    root = _root() / "executions"
    if not root.exists():
        return events, errors
    for path in sorted(root.glob("*/*/*.json")):
        try:
            if path.parent.parent.name == "definitions":
                continue
            row = snapshots.load(path)
            if not isinstance(row, dict):
                raise ValueError("Execution event is not an object")
            events.append(row)
        except (OSError, ValueError) as exc:
            errors.append({"run_attempt_id": path.parent.parent.name, "record": path.name, "error": str(exc)})
    return events, errors


def _load_json(kind, identifier):
    _id(identifier)
    path = _root() / kind / (identifier + ".json")
    return snapshots.load(path)


def get_execution_trace(plan_id=None, order_attempt_id=None, judgment_id=None, position_episode_id=None, rule_trigger_id=None):
    """Stored-only graph lookup. Missing origins stay missing; never query a broker."""
    filters = {key: value for key, value in locals().copy().items() if value is not None}
    try:
        for key, value in filters.items(): _id(value, key)
    except GuardError as exc:
        return {"recording_status": "recording_error", "error_code": exc.code, "error": str(exc), "events": [], "plans": [], "dispatch": []}
    all_events, errors = _read_events()
    dispatch = []
    if _db_path().exists():
        try:
            db = _connect(readonly=True)
            try: dispatch = [_decode(row) for row in db.execute("SELECT * FROM order_attempts ORDER BY created_at")]
            finally: db.close()
        except Exception as exc:
            errors.append({"record": "order_dispatch.sqlite3", "error": str(exc)})
    elif _db_path().with_suffix(".identity.json").exists():
        errors.append({"record": "order_dispatch.sqlite3", "error": "Previously initialized dispatch ledger is missing"})
    # Expand explicit graph edges only. A matching instrument/time is never an edge.
    keys = ("plan_id", "order_attempt_id", "judgment_id", "position_episode_id", "rule_trigger_id")
    ids = {key: {filters[key]} if key in filters else set() for key in keys}
    def links(row):
        combined = {**(row.get("payload") or {}), **row}
        if row.get("kind") == "rule_trigger":
            combined["rule_trigger_id"] = row.get("event_id")
        return {key: combined.get(key) if _reference_status(combined.get(key)) == "valid" else None for key in keys}
    if filters:
        changed = True
        while changed:
            changed = False
            for row in all_events + dispatch:
                refs = links(row)
                if any(value in ids[key] for key, value in refs.items() if value is not None):
                    for key, value in refs.items():
                        if value is not None and value not in ids[key]: ids[key].add(value); changed = True
        events = [row for row in all_events if any(value in ids[key] for key, value in links(row).items() if value is not None)]
        dispatch = [row for row in dispatch if any(value in ids[key] for key, value in links(row).items() if value is not None)]
    else:
        run = os.environ.get("RUN_ATTEMPT_ID")
        events = [row for row in all_events if not run or row.get("run_attempt_id") == run]
        dispatch = [row for row in dispatch if not run or row.get("run_attempt_id") == run]
    own_runs = {row.get("run_attempt_id") for row in events + dispatch}
    global_errors = [error for error in errors if error.get("run_attempt_id") not in own_runs]
    errors = [error for error in errors if error.get("run_attempt_id") in own_runs or error.get("record") == "order_dispatch.sqlite3"]
    judgments, deliveries, missing = [], [], []
    for row in dispatch:
        if row["plan_link"]["status"] in ("missing", "mismatch", "invalid"):
            missing.append({"kind": "submitted_plan_link", "order_attempt_id": row["order_attempt_id"],
                            **row["plan_link"], "reason": "Issued plan is not an explicit matching submitted plan"})
        for key, state in row["reference_validation"].items():
            if state == "invalid":
                missing.append({"kind": key, "id": row["payload"].get(key), "reason": "Invalid auxiliary reference"})
    for key, value in filters.items():
        if not any(links(row).get(key) == value for row in events + dispatch):
            missing.append({"kind": key, "id": value, "reason": "No stored execution reference"})
    plan_ids = {links(row).get("plan_id") for row in events + dispatch} - {None}
    stored_plans = {row.get("plan_id") for row in events if row.get("kind") == "order_plan"}
    for pid in sorted(plan_ids - stored_plans):
        missing.append({"kind": "plan", "id": pid, "reason": "Referenced plan was not archived"})
    jids = {links(row).get("judgment_id") for row in events + dispatch}
    if judgment_id: jids.add(judgment_id)
    for jid in sorted(jid for jid in jids if jid):
        try:
            import ai_judgment as J
            judgment = J.load_judgment(jid)
            if judgment is None: raise ValueError("Judgment not committed or missing")
            judgments.append(judgment)
        except Exception as exc:
            missing.append({"kind": "judgment", "id": jid, "reason": str(exc)})
    for did in sorted({did for row in judgments for did in row.get("delivery_ids", [])}):
        try: deliveries.append(_load_json("deliveries", did))
        except Exception as exc: missing.append({"kind": "delivery", "id": did, "reason": str(exc)})
    def selection_refs(value):
        refs = {value.get("selection_run_id"), (value.get("ontology") or {}).get("selection_run_id")}
        for candidate in value.get("candidates", []) + value.get("holdings", []):
            evidence = candidate.get("_ontology") or candidate.get("evidence") or {}
            refs.add((evidence.get("observation") or {}).get("run_id"))
            refs.add((evidence.get("admission") or {}).get("run_id"))
        return {ref for ref in refs if isinstance(ref, str) and ref}
    selections, visited = [], set()
    selection_ids = set()
    for receipt in deliveries:
        selection_ids.update(selection_refs(receipt))
        selection_ids.update(selection_refs(receipt.get("briefing_snapshot") or {}))
    while selection_ids - visited:
        sid = sorted(selection_ids - visited)[0]
        visited.add(sid)
        try:
            selection = _load_json("selections", sid)
            selections.append(selection)
            selection_ids.update(selection_refs(selection))
        except Exception as exc:
            missing.append({"kind": "selection", "id": sid, "reason": str(exc)})
    triggers = [row for row in events if row.get("kind") == "rule_trigger"]
    trigger_ids = {links(row).get("rule_trigger_id") for row in events + dispatch} - {None}
    for tid in trigger_ids - {row["event_id"] for row in triggers}:
        missing.append({"kind": "rule_trigger", "id": tid, "reason": "Referenced trigger was not archived"})
    episodes = []
    episode_ids = {links(row).get("position_episode_id") for row in events + dispatch} - {None}
    for eid in sorted(episode_ids):
        episode_events = [row for row in events if row.get("position_episode_id") == eid and row.get("kind") in ("position_opened", "position_closed")]
        state, db = None, None
        path = _root() / "position_episodes.sqlite3"
        try:
            if path.exists():
                db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
                if row:
                    state = dict(row)
                    if state.get("source"):
                        state["source"] = json.loads(state["source"])
        except Exception as exc:
            missing.append({"kind": "position_episode", "id": eid, "reason": str(exc)})
        finally:
            if db is not None:
                db.close()
        if state is None and not episode_events:
            missing.append({"kind": "position_episode", "id": eid, "reason": "Referenced holding interval was not archived"})
        episodes.append({"position_episode_id": eid, "stored_state": state, "events": episode_events})
    definitions = []
    for row in events:
        if row.get("kind") == "order_plan" and row.get("definition", {}).get("recording_status") == "recording_error":
            errors.append({"record": "engine_definition", "plan_id": row.get("plan_id"),
                           "error": row["definition"].get("error", "Definition snapshot was not archived")})
    for ref in {row.get("definition", {}).get("definition_id") for row in events if row.get("kind") == "order_plan"} - {None}:
        try:
            _id(ref)
            definitions.append(snapshots.load(_root() / "executions/definitions/engine_definition" / (ref + ".json")))
        except Exception as exc:
            missing.append({"kind": "definition", "id": ref, "reason": str(exc)})
    return {"recording_status": "recording_error" if errors else "partial" if missing else "recorded",
            "events": events, "plans": [row for row in events if row.get("kind") == "order_plan"],
            "dispatch": dispatch, "judgments": judgments, "deliveries": deliveries, "selections": selections, "definitions": definitions,
            "rule_triggers": triggers, "position_episodes": episodes,
            "missing_links": missing, "recording_errors": errors, "unrelated_store_errors": global_errors}
