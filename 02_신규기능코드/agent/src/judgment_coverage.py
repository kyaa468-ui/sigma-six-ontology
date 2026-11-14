"""Append-only AI judgment coverage, independent of order/risk decisions.

The denominator is created before the AI starts.  Calls are linked only to an
explicit, already accepted judgment; matching a later judgment never repairs a
missing ID.  Every public write is best effort and must not block a trade.
"""
import json
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import common as C
import record_store
import storage_objects as objects
import storage_errors
import snapshot_store as snapshots

STATUSES = ("not_submitted", "rejected", "accepted_unlinked", "linked", "recording_error")
_ERRORS = storage_errors.ErrorRegistry()  # Survives a failed disk write in the current process, never hides it.
_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _now():
    return C.now().isoformat(timespec="microseconds")


def _root():
    return C.ontology_root()


def _valid_id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _error(attempt_id, operation, exc):
    item = {"error_id": "error-" + uuid.uuid4().hex, "run_attempt_id": attempt_id,
            "operation": operation, "error": str(exc), "created_at": _now()}
    try:
        # A separate directory can survive an event-path failure in another process
        # (notably the order Hook). Total disk failure remains visible on stderr.
        _write("coverage_errors", item["error_id"], item)
    except Exception:
        storage_errors.retain(_ERRORS, (str(_root()), attempt_id), item)
    print(f"[judgment coverage recording_error] {operation}: {exc}", file=sys.stderr)
    return {"recording_status": "recording_error", "run_attempt_id": attempt_id, "errors": [item]}


def recording_error(operation, exc, attempt_id=None):
    """Public fail-open reporter for submission, MCP and Hook audit failures."""
    attempt_id = attempt_id or os.environ.get("RUN_ATTEMPT_ID")
    try:
        return _error(attempt_id, operation, exc)
    except Exception:
        # Reporting itself may fail (including a closed stderr). Keep the current
        # process from claiming success, but never interfere with a trading tool.
        item = {"error_id": "error-" + uuid.uuid4().hex, "run_attempt_id": attempt_id,
                "operation": operation, "error": "Audit failure; error reporting also failed"}
        storage_errors.retain(_ERRORS, (str(_root()), attempt_id), item)
        return {"recording_status": "recording_error", "run_attempt_id": attempt_id, "errors": [item]}


def _write(kind, identifier, data):
    if not _valid_id(identifier): raise ValueError('Invalid record ID')
    folder = _root() / kind
    if kind in ('coverage_events','coverage_errors') and _valid_id(data.get('run_attempt_id')):
        folder = folder / data['run_attempt_id']
    record_store.publish(folder / (identifier + '.json'), data, kind=kind)
    return data


def _event(attempt_id, event_type, **fields):
    event_id = "event-" + uuid.uuid4().hex
    row = {"event_id": event_id, "run_attempt_id": attempt_id,
           "event_type": event_type, "created_at": _now(), "recorded_at_ns": time.time_ns(), **fields}
    _write("coverage_events", event_id, row)
    return row


def _records(kind, attempt_id=None, fields=None):
    errors=[];root=_root()/kind
    rows=[]
    folders=[root,root/attempt_id] if attempt_id and _valid_id(attempt_id) else [root]
    for folder in folders:
        failures=[]
        rows.extend(record_store.iter_records(folder,filters={'run_attempt_id':attempt_id},fields=fields,errors=failures,recursive=not bool(attempt_id)))
        for error in failures:
            if error.get('record','').startswith(kind+'/'):error['record']=error['record'][len(kind)+1:]
            error.update(operation='read_'+kind,unscoped=folder==root)
        errors.extend(failures)
    return rows,errors


def _manifest(attempt_id):
    if not _valid_id(attempt_id):
        raise ValueError("Unknown or invalid run_attempt_id")
    row = snapshots.load(_root() / "attempts" / (attempt_id + ".json"))
    if not isinstance(row, dict) or row.get("run_attempt_id") != attempt_id:
        raise ValueError("Invalid attempt manifest")
    return row


def _codes(snapshot, key):
    """Return known codes and whether this source supplied a complete list."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get(key), list):
        return set(), False
    codes, complete = set(), True
    for row in snapshot[key]:
        if not isinstance(row, dict) or not row.get("code"):
            complete = False
            continue
        if key == "holdings" and row.get("qty") == 0:
            continue
        codes.add(str(row["code"]))
    return codes, complete


def _snapshot(briefing, account):
    candidates, candidates_ok = _codes(briefing, "candidates")
    held, account_ok = _codes(account, "holdings")
    briefing_held, _ = _codes(briefing, "holdings")
    return {"candidates": sorted(candidates), "holdings": sorted(held | briefing_held),
            "candidates_known": candidates_ok, "holdings_known": account_ok}


def _side(side):
    return {"buy": "매수", "sell": "매도"}.get(side, side)


def _target(code, side, reason):
    return {"code": str(code), "confidence_for": side, "reasons": [reason]}


def start_attempt(run_id, kind, briefing, account):
    """Persist the expected set before process launch; same-minute runs differ."""
    attempt_id = "attempt-" + uuid.uuid4().hex
    try:
        snapshot = _snapshot(briefing, account)
        held = set(snapshot["holdings"])
        targets = [] if kind not in ("decide", "view") else (
            [_target(code, "매도", "initial_holding") for code in sorted(held)] +
            [_target(code, "매수", "initial_candidate") for code in snapshot["candidates"] if code not in held])
        manifest = {"schema_version": 1, "run_attempt_id": attempt_id, "run_id": run_id,
                    "kind": kind, "created_at": _now(), "recorded_at_ns": time.time_ns(), "initial_snapshot": snapshot,
                    "initial_targets": targets,
                    "target_set_incomplete": kind in ("decide", "view") and not (
                        snapshot["candidates_known"] and snapshot["holdings_known"]),
                    "recording_status": "recorded"}
        return _write("attempts", attempt_id, manifest)
    except Exception as exc:  # Audit must never change whether an AI run can start.
        return _error(attempt_id, "start_attempt", exc)


def observe_targets(briefing=None, account=None):
    """Add observations without removing the original expected subjects."""
    attempt_id = os.environ.get("RUN_ATTEMPT_ID")
    if not attempt_id:
        return {"recording_status": "not_active"}
    try:
        _manifest(attempt_id)
        event = _event(attempt_id, "targets", snapshot=_snapshot(briefing, account))
        return {"recording_status": "recorded", "event_id": event["event_id"]}
    except Exception as exc:
        return _error(attempt_id, "observe_targets", exc)


def register_review_targets(attempt_id, targets):
    """Append server-selected review directions without rewriting the manifest."""
    try:
        _manifest(attempt_id)
        rows = []
        for target in targets:
            code, side = str(target['code']), target['confidence_for']
            if not code or side not in ('매수', '매도'):
                raise ValueError('Invalid review target')
            rows.append(_target(code, side, 'semantic_registered_review'))
        event = _event(attempt_id, 'review_targets', targets=rows)
        return {'recording_status': 'recorded', 'event_id': event['event_id']}
    except Exception as exc:
        return _error(attempt_id, 'register_review_targets', exc)


def _load_judgment(judgment_id):
    # Lazy import keeps validation and coverage modules independent at import time.
    import ai_judgment
    return ai_judgment.load_judgment(judgment_id)


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _link(call, manifest):
    if call.get("confidence_for") not in ("매수", "매도"):
        return {"status": "unlinked", "reason": "direction_unresolved"}
    if not call.get("judgment_id"):
        reason = "direction_unresolved" if call.get("action") == "보유" else "missing_judgment_id"
        return {"status": "unlinked", "reason": reason}
    judgment = _load_judgment(call["judgment_id"])
    if not judgment:
        return {"status": "unlinked", "reason": "unknown_judgment_id"}
    expected = {"run_attempt_id": manifest["run_attempt_id"], "run_id": manifest["run_id"],
                "code": call["code"], "confidence_for": call["confidence_for"]}
    for field, value in expected.items():
        if judgment.get(field) != value:
            return {"status": "unlinked", "reason": field + "_mismatch"}
    if call["run_id"] != manifest["run_id"]:
        return {"status": "unlinked", "reason": "run_id_mismatch"}
    raw = _number(judgment.get("confidence_raw"))
    if raw is None or raw != _number(call.get("confidence_raw")):
        return {"status": "unlinked", "reason": "confidence_raw_mismatch"}
    try:
        submitted = datetime.fromisoformat(judgment["created_at"])
        invoked = datetime.fromisoformat(call["created_at"])
        if submitted.tzinfo is None or invoked.tzinfo is None or submitted > invoked:
            return {"status": "unlinked", "reason": "judgment_not_available_at_call"}
    except (KeyError, TypeError, ValueError):
        return {"status": "unlinked", "reason": "judgment_time_unknown"}
    return {"status": "linked", "reason": None,
            "recommended_action": judgment.get("recommended_action")}


def enter(stage, code, side, confidence, judgment_id=None, rule_exit=False, action=None):
    """Observe entry, including Hook rejection; returns ID or None on I/O failure."""
    attempt_id = os.environ.get("RUN_ATTEMPT_ID")
    if not attempt_id:
        return None
    try:
        manifest = _manifest(attempt_id)
        call = {"stage": stage, "code": str(code), "confidence_for": _side(side),
                "confidence_raw": confidence, "judgment_id": judgment_id,
                "rule_exit": bool(rule_exit), "action": action,
                "run_id": os.environ.get("RUN_ID", manifest["run_id"]), "created_at": _now()}
        try:
            call["link"] = _link(call, manifest)
        except Exception as exc:
            call["link"] = {"status": "recording_error", "reason": "judgment_lookup_failed", "error": str(exc)}
        event = _event(attempt_id, "call_enter", **call)
        return event["event_id"]
    except Exception as exc:
        _error(attempt_id, "enter", exc)
        return None


def finish_event(event_id, outcome=None, boost=None):
    """Append the actual result/boost; never replace a call's original linkage."""
    attempt_id = os.environ.get("RUN_ATTEMPT_ID")
    if not event_id:
        return {"recording_status": "not_active" if not attempt_id else "recording_error"}
    try:
        if not _valid_id(event_id):
            raise ValueError("Invalid event_id")
        path = _root() / "coverage_events" / (event_id + ".json")
        if attempt_id and _valid_id(attempt_id):
            scoped = _root() / "coverage_events" / attempt_id / (event_id + ".json")
            if scoped.exists():
                path = scoped
        call = snapshots.load(path)
        attempt_id = call["run_attempt_id"]
        if call.get("event_type") != "call_enter":
            raise ValueError("event_id must refer to call_enter")
        row = _event(attempt_id, "call_result", call_event_id=event_id, outcome=outcome, boost=boost)
        return {"recording_status": "recorded", "event_id": row["event_id"]}
    except Exception as exc:
        return _error(attempt_id, "finish_event", exc)


def finish_attempt(attempt_id, status):
    """Record normal/error/timeout termination without rewriting the start."""
    try:
        _manifest(attempt_id)
        if status not in ("normal", "error", "timeout"):
            raise ValueError("Expected normal, error, or timeout")
        row = _event(attempt_id, "attempt_finish", status=status)
        return {"recording_status": "recorded", "event_id": row["event_id"]}
    except Exception as exc:
        return _error(attempt_id, "finish_attempt", exc)


def _summarize(manifest, events, submissions, errors, judgment_loader=None, summary=False):
    attempt_id = manifest["run_attempt_id"]
    separate = manifest["kind"] == "exit"
    general = manifest["kind"] in ("decide", "view")
    targets = {}
    held = set(manifest["initial_snapshot"]["holdings"])

    def add(code, side, reason):
        if not general or not code or side not in ("매수", "매도"):
            return
        key = (str(code), side)
        target = targets.setdefault(key, _target(code, side, reason))
        if reason not in target["reasons"]:
            target["reasons"].append(reason)

    for target in manifest["initial_targets"]:
        add(target["code"], target["confidence_for"], target["reasons"][0])
    results = {}
    calls, rule_calls, finish = [], [], None
    for event in sorted(events, key=lambda e: (e.get("recorded_at_ns", 0), e.get("created_at", ""), e.get("event_id", ""))):
        kind = event.get("event_type")
        if kind == "targets":
            snapshot = event["snapshot"]
            held.update(snapshot["holdings"])
            for code in snapshot["holdings"]:
                add(code, "매도", "observed_holding")
            for code in snapshot["candidates"]:
                if code not in held:
                    add(code, "매수", "observed_candidate")
        elif kind == 'review_targets':
            for target in event['targets']:
                add(target['code'], target['confidence_for'], 'semantic_registered_review')
        elif kind == "call_enter":
            if separate or event.get("rule_exit"):
                rule_calls.append(event)
                # A newly discovered subject in an ordinary run still belongs
                # to its expected set; a rule flag cannot conceal that review.
                add(event.get("code"), event.get("confidence_for"), "rule_exit_subject_observed")
            elif general:
                calls.append(event)
                add(event.get("code"), event.get("confidence_for"), "execution_observed")
                if event.get("action") == "보유" and event.get("confidence_for") not in ("매수", "매도"):
                    # The target is a holding review; the score direction itself
                    # remains unknown and is never inferred for linking.
                    held.add(event["code"])
                    add(event["code"], "매도", "holding_direction_unresolved")
                    event["coverage_target_direction"] = "매도"
                if event.get("code") in held and event.get("action") in ("보유", "관망"):
                    add(event["code"], "매수", "holding_not_sold")
        elif kind == "call_result":
            results.setdefault(event["call_event_id"], []).append(event)
        elif kind == "attempt_finish":
            finish = event

    for submission in submissions:
        add(submission.get("code"), submission.get("confidence_for"), "submission_observed")
        if submission.get("status") == "accepted" and submission.get("confidence_for") == "매도":
            try:
                judgment = (judgment_loader or _load_judgment)(submission.get("judgment_id"))
                if (judgment and judgment.get("run_attempt_id") == attempt_id and
                        judgment.get("recommended_action") in ("보유", "관망") and
                        submission.get("code") in held):
                    add(submission["code"], "매수", "accepted_holding_not_sold")
            except Exception as exc:
                errors.append({"operation": "read_accepted_judgment", "error": str(exc)})

    rows, counts = [], dict.fromkeys(STATUSES, 0)
    for key, target in sorted(targets.items()):
        attempts = [s for s in submissions if (str(s.get("code")), s.get("confidence_for")) == key]
        accepted = [s for s in attempts if s.get("status") == "accepted"]
        target_calls = [e for e in calls if (
            e.get("code"), e.get("coverage_target_direction", e.get("confidence_for"))) == key]
        linked = [e for e in target_calls if e["link"]["status"] == "linked"]
        missing_result = any(e["event_id"] not in results for e in target_calls)
        linked_ids = {e.get("judgment_id") for e in linked}
        accepted_ids = {s.get("judgment_id") for s in accepted}
        reasons = []
        if errors or (finish and missing_result) or any(s.get("status") == "recording_error" for s in attempts) or any(
                e["link"]["status"] == "recording_error" for e in target_calls):
            status = "recording_error"
        elif not attempts:
            status = "not_submitted"
        elif not accepted:
            status = "rejected"
        elif target_calls and not missing_result and len(linked) == len(target_calls) and linked_ids <= accepted_ids:
            status = "linked"
        else:
            status = "accepted_unlinked"
            if not target_calls:
                reasons.append("no_execution_path")
            if linked and len(linked) < len(target_calls):
                reasons.append("partial_linkage")
        reasons.extend(e["link"]["reason"] for e in target_calls if e["link"].get("reason"))
        for event in target_calls:
            event["results"] = results.get(event["event_id"], [])
            if not event["results"]:
                reasons.append("call_result_missing")
        rows.append({**target, "status": status, "status_reasons": sorted(set(reasons)),
                     "submissions": attempts, "calls": target_calls})
        counts[status] += 1

    for event in rule_calls:
        event["results"] = results.get(event["event_id"], [])
    incomplete = manifest["target_set_incomplete"]
    denominator = len(rows)
    recording_error = bool(errors or counts["recording_error"])
    linked_rate = counts["linked"] / denominator if denominator and not incomplete and not recording_error else None
    result = {"schema_version": 1, "run_attempt_id": attempt_id, "run_id": manifest["run_id"],
            "kind": manifest["kind"], "run_status": finish["status"] if finish else "unfinished",
            "target_set_incomplete": incomplete, "expected_count": denominator,
            "counts": counts, "linked_rate": linked_rate,
            "rate_status": "recording_error" if recording_error else "target_set_incomplete" if incomplete else
                           "not_applicable" if not denominator else "available",
            "targets": rows, "submission_attempts": len(submissions),
            "rejection_history": [s for s in submissions if s.get("status") == "rejected"],
            "rule_exit": {"target_count": len({e.get("code") for e in rule_calls}),
                          "call_count": len(rule_calls), "events": rule_calls},
            "recording_status": "recording_error" if recording_error else "recorded",
            "recording_errors": errors}
    if summary:
        result['schema_version'] = 2
        for row in result['targets']:
            row['call_count'] = len(row.pop('calls'))
            row['submission_count'] = len(row.pop('submissions'))
            row['detail_ref'] = {'run_attempt_id':attempt_id,'code':row['code'], 'confidence_for':row['confidence_for']}
        result['rejection_count'] = len(result.pop('rejection_history'))
        result['rule_exit'].pop('events', None)
        result['recording_error_count'] = len(result['recording_errors'])
        result['recording_errors'] = result['recording_errors'][:50]
        result['detail_ref'] = {'run_attempt_id':attempt_id}
    return result


def get_coverage(attempt_id=None, *, summary=False):
    """Recompute even an unfinished run from its immutable start and events."""
    attempt_id = attempt_id or os.environ.get("RUN_ATTEMPT_ID")
    try:
        if not attempt_id:
            manifests, errors = _records("attempts")
            if not manifests:
                return {"recording_status": "recording_error" if errors else "not_found",
                        "run_status": "unknown", "expected_count": None,
                        "linked_rate": None, "target_set_incomplete": True, "recording_errors": errors}
            attempt_id = max(manifests, key=lambda m: (m.get("recorded_at_ns", 0), m.get("created_at", "")))["run_attempt_id"]
        manifest = _manifest(attempt_id)
        fields = ('event_id','run_attempt_id','event_type','recorded_at_ns','created_at','snapshot','targets',
                  'rule_exit','code','confidence_for','action','link','judgment_id','call_event_id','status') if summary else None
        events, event_errors = _records("coverage_events", attempt_id, fields=fields)
        submission_fields = ('event_id','run_attempt_id','code','confidence_for','status','judgment_id','created_at') if summary else None
        submissions, submission_errors = _records("submissions", attempt_id, fields=submission_fields)
        stored_errors, error_read_errors = _records("coverage_errors", attempt_id,fields=("error_id","run_attempt_id","operation","code","confidence_for","recorded_at_ns") if summary else None)
        events = [e for e in events if e.get("run_attempt_id") == attempt_id]
        submissions = [s for s in submissions if s.get("run_attempt_id") == attempt_id]
        own_errors = [e for e in stored_errors if e.get("run_attempt_id") == attempt_id]
        own_errors.extend(_ERRORS.get((str(_root()), attempt_id), []))
        own_errors = list({e["error_id"]: e for e in own_errors}.values())
        read_errors = event_errors + submission_errors + error_read_errors
        global_errors = [e for e in read_errors if e.get("unscoped")]
        errors = [e for e in read_errors if not e.get("unscoped")] + own_errors
        result = _summarize(manifest, events, submissions, errors, summary=summary)
        result["global_storage_errors"] = global_errors
        result["fallback_state"] = _ERRORS.state()
        return result
    except Exception as exc:
        # A read must not create an error file or alter coverage history. Writers
        # still use _error(); a missing/damaged origin is returned to this caller.
        failure = {"recording_status": "recording_error", "run_attempt_id": attempt_id,
                   "errors": [{"operation": "get_coverage", "error": str(exc)}]}
        return {**failure, "run_status": "unknown", "expected_count": None,
                "linked_rate": None, "target_set_incomplete": True, "recording_errors": failure["errors"]}
