"""Frozen legacy references and read-only, as-known execution context for step 4.

These records explain an evaluation. They neither change tuning inputs nor turn
legacy price returns or balance-inferred fills into verified financial results.
"""
import copy
import hashlib
import inspect
import json
from datetime import datetime
from pathlib import Path

import common as C
import execution_reconcile as ER
import tuning
import snapshot_store


def _hash(value):
    return hashlib.sha256(value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _instant(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result


def _read(path, default, errors, *, lines=False, missing_ok=False):
    path = Path(path)
    try:
        raw = path.read_bytes()
        dependencies = {}
        value = ([json.loads(line) for line in raw.decode().splitlines() if line.strip()] if lines
                 else snapshot_store.decode(json.loads(raw), dependencies=dependencies))
        source = {"path": str(path), "sha256": _hash(raw), "bytes": len(raw)}
        if dependencies:
            source['snapshot_dependencies'] = dependencies
        return value, source
    except FileNotFoundError:
        if missing_ok:
            return copy.deepcopy(default), {"path": str(path), "sha256": None,
                                            "status": "optional_source_absent", "used_default": True}
        errors.append({"path": str(path), "reason": "missing_source"})
    except Exception as exc:
        errors.append({"path": str(path), "reason": "source_read_error", "error": str(exc)})
    return copy.deepcopy(default), {"path": str(path), "sha256": None, "status": "unavailable"}


def capture_legacy_context(market_as_of, knowledge_as_of):
    """Read each mutable input once; preserve exactly the legacy calculation.

The file snapshot is an actual read now, not a reconstructed historical state.
An older market target never rewrites `today` or the host timezone quote rule.
"""
    cutoff = _instant(knowledge_as_of)
    captured_at = C.now().isoformat()
    errors, sources, values = [], {}, {}
    for key, path, default in (
        ("quotes", Path(C.WORK) / "quotes.json", {}),
        ("prep_rows", Path(C.WORK) / "prep_rows.json", []),
        ("briefing", C.BRIEFING, {}), ("account", C.ACCOUNT, {}),
    ):
        # Legacy outcomes permits absent caches before their first collection.
        # Capture that absence explicitly; it is not a failed archive write.
        values[key], sources[key] = _read(path, default, errors, missing_ok=True)
    rows, sources["journal"] = _read(C.JOURNAL, [], errors, lines=True, missing_ok=True)
    params, sources["params"] = _read(Path(C.CONFIG) / "params.json", {}, errors)
    # Journal timestamps describe the entry, not proof of when an old file was
    # available. Keep the original array and explicit source indices as well.
    selected, indices = [], []
    for index, row in enumerate(rows):
        try:
            # common.journal historically writes stamp() without an offset;
            # that format is explicitly KST, unlike arbitrary external times.
            when = datetime.fromisoformat(row["time"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=C.KST)
            if when <= cutoff:
                selected.append(row)
                indices.append(index)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append({"path": str(C.JOURNAL), "row_index": index, "reason": "unknown_journal_time", "error": str(exc)})
    start = (params.get("competition") or {}).get("start")
    selected_with_indices = [(r, i) for r, i in zip(selected, indices) if not start or r["time"][:10] >= start]
    selected = [r for r, _ in selected_with_indices]
    values.update(today=C.today().isoformat(), journal=rows, selected_journal=selected,
                  selected_journal_indices=[i for _, i in selected_with_indices], params=params)
    audit = {}
    try:
        outcomes = tuning.outcomes(selected, snapshot=values, audit=audit)
        trades = tuning.trade_results(selected, values["account"])
    except Exception as exc:
        outcomes, trades = None, []
        errors.append({"reason": "legacy_calculation_error", "error": f"{type(exc).__name__}: {exc}"})
    for item in audit.get("evaluations", []) + audit.get("excluded", []):
        item["journal_source_index"] = values["selected_journal_indices"][item["row_index"]]
    source = "\n\n".join(inspect.getsource(fn) for fn in (tuning.outcomes, tuning.trade_results, C.business_days))
    return {
        "market_as_of": str(market_as_of), "knowledge_as_of": cutoff.isoformat(), "captured_at": captured_at,
        "snapshot_role": "current_snapshot_reference",
        "historical_availability": "not_reconstructed; source timestamps retained only where present",
        "journal_time_policy": "legacy common.stamp timestamps interpreted in KST; not evidence of historical file availability",
        "inputs": values, "sources": sources, "input_hash": _hash(values),
        "missing_optional_sources": [key for key, item in sources.items() if item.get("status") == "optional_source_absent"],
        "decision_outcomes": outcomes, "legacy_trade_results": trades, "audit": audit,
        "legacy_trade_quality": {"quality": "legacy_reference_only", "net_pnl_verified": False,
            "limitations": ["journal_fill_may_be_balance_inference", "price_return_excludes_costs", "partial_sell_legacy_label_is_liquidation",
                            "code_level_book_not_position_episode_attribution", "holding_cost_return_is_account_report_only"]},
        "definition": {"version": "LEGACY_OUTCOMES_REFERENCE@v1", "source": source, "sha256": _hash(source.encode())},
        "recording_status": "recording_error" if errors else "recorded", "errors": errors,
    }


def _records(directory, cutoff, errors):
    """No side effects on corrupt files: report errors in this query only."""
    rows = []
    try:
        paths = sorted(directory.rglob("*.json")) if directory.exists() else []
    except OSError as exc:
        errors.append({"path": str(directory), "reason": "list_error", "error": str(exc)})
        return rows
    for path in paths:
        row, source = _read(path, {}, errors)
        if source.get("status") == "unavailable":
            continue
        try:
            if not isinstance(row, dict):
                raise ValueError("record must be an object")
            if row.get("kind") == "engine_definition":
                # Content-addressed source bundles are not timed observations.
                # The corresponding order_plan carries their original reference.
                continue
            if _instant(row["created_at"]) > cutoff:
                continue
            if row.get("kind") in ("execution_observation", "execution_query"):
                if _instant(row["available_at"]) > cutoff:
                    continue
            elif row.get("available_at") and _instant(row["available_at"]) > cutoff:
                continue
            rows.append({**row, "_source": source})
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({"path": str(path), "reason": "record_time_or_shape_unknown", "error": str(exc)})
    return sorted(rows, key=lambda r: (r["created_at"], r.get("recorded_at_ns", 0), r.get("event_id", "")))


def _judgment(row):
    return row.get("judgment_id") or (row.get("payload") or {}).get("judgment_id")


def _proof(event, calls, judgment_ids, payload):
    call = calls.get(event.get("coverage_event_id"))
    if not call:
        return None, "original_call_record_missing"
    if (call.get("link") or {}).get("status") != "linked":
        return call, (call.get("link") or {}).get("reason") or "original_call_unlinked"
    if call.get("judgment_id") not in judgment_ids or call.get("judgment_id") != _judgment(event):
        return call, "judgment_reference_mismatch"
    if call.get("run_attempt_id") != event.get("run_attempt_id"):
        return call, "run_attempt_mismatch"
    expected_stage = "hook" if event.get("kind") == "hook_entry" else event.get("stage")
    if call.get("stage") != expected_stage or _instant(call["created_at"]) > _instant(event["created_at"]):
        return call, "original_call_stage_or_time_mismatch"
    if "judgment_id" in payload and payload["judgment_id"] != call.get("judgment_id"):
        return call, "original_dispatch_judgment_mismatch"
    if payload:
        for field, key in (("code", "code"), ("confidence_for", "side"), ("confidence_raw", "confidence")):
            if payload.get(key) is not None and call.get(field) != payload[key]:
                return call, "original_call_payload_mismatch"
    return call, None


def _non_order_paths(events, calls, requested):
    """An explicit original call can be useful even when no order was made."""
    paths, unlinked = [], []
    sent = {r.get('order_attempt_id') for r in events
            if r.get('kind') in ('dispatch_claimed', 'dispatch_result', 'hook_entry')
            or r.get('stage') == 'place_order'} - {None}
    for event in events:
        stage = event.get('stage')
        if (event.get('kind') not in ('tool_result', 'tool_error') or stage not in ('record_decision', 'plan_order')
                or _judgment(event) not in requested):
            continue
        # A plan followed by a real order remains within that exact order's
        # event graph. It is not an additional plan-only outcome.
        if stage == 'plan_order' and event.get('order_attempt_id') in sent:
            continue
        result = event.get('result') or {}
        payload = {'code': event.get('code'), 'judgment_id': _judgment(event)}
        if stage == 'plan_order':
            payload.update(side=result.get('side'), confidence=result.get('confidence_raw'))
        call, reason = _proof(event, calls, requested, payload)
        row = {'event_id':event.get('event_id'), 'judgment_id':_judgment(event),
               'plan_id':event.get('plan_id'), 'order_attempt_id':event.get('order_attempt_id'),
               'stage':stage, 'original_call':call, 'event':event,
               'financial_attribution':False, 'result':result,
               'path_kind':'decision' if stage == 'record_decision' else
                           'zero_quantity_plan' if result.get('qty') == 0 else 'plan_only'}
        # Keep the frozen inputs/account/quote only through this explicit plan
        # and attempt. A nearby plan for the same code is never a substitute.
        own = [event]
        if stage == 'plan_order' and event.get('plan_id') and event.get('order_attempt_id'):
            own = [r for r in events if r.get('plan_id') == event['plan_id']
                   and r.get('order_attempt_id') == event['order_attempt_id']
                   and r.get('run_attempt_id') == event.get('run_attempt_id')
                   and _judgment(r) == _judgment(event)]
        row['events'] = own
        if reason:
            unlinked.append({**row,'link_status':'unlinked','reason':reason})
        else:
            paths.append({**row,'link_status':'linked', 'action':call.get('action'),
                          'confidence_for':call.get('confidence_for'), 'confidence_raw':call.get('confidence_raw')})
    return paths, unlinked


def _definitions(root, referenced_events, errors):
    refs = {}
    for event in referenced_events:
        reference = event.get('definition') or event.get('engine_definition')
        if isinstance(reference,dict) and reference.get('definition_id'):
            refs[reference['definition_id']] = reference
    found = {}
    for identifier, reference in refs.items():
        if not isinstance(identifier,str) or not identifier.startswith('definition-') or '/' in identifier or '..' in identifier:
            errors.append({'reason':'invalid_engine_definition_reference'}); continue
        paths = list((root/'executions').glob('*/engine_definition/'+identifier+'.json'))
        if not paths:
            found[identifier] = {'definition_id':identifier,'status':'unrecorded','reason':'original_definition_body_missing'}
            continue
        bodies = []
        for path in paths:
            body, source = _read(path, {}, errors)
            if source.get('status') == 'unavailable': continue
            actual = hashlib.sha256(json.dumps(body.get('sources'),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            identity_digest = actual
            if 'source_identity' in body:
                identity_digest = hashlib.sha256(json.dumps({'sources':body.get('sources'),'source_identity':body['source_identity']},
                    ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            if (body.get('definition_id') != identifier or identifier != 'definition-'+identity_digest
                    or actual != body.get('sha256') or actual != reference.get('sha256')):
                errors.append({'reason':'engine_definition_hash_mismatch','definition_id':identifier}); continue
            bodies.append({**body,'_source':source})
        if bodies:
            found[identifier] = {'status':'recorded','definition':bodies[0]}
    return found


def execution_context(judgment_ids, knowledge_as_of):
    """Resolve only exact attempts with a frozen, verified original-call link.

Shared judgments, plans and holding episodes never enlarge an order's fill set.
A valid later replay cannot repair the original dispatch's missing judgment ID.
"""
    cutoff, requested = _instant(knowledge_as_of), set(judgment_ids or [])
    errors = []
    root = Path(C.WORK) / "ontology"
    events = _records(root / "executions", cutoff, errors)
    calls = {r["event_id"]: r for r in _records(root / "coverage_events", cutoff, errors)
             if r.get("event_type") == "call_enter" and r.get("event_id")}
    order_events = [r for r in events if r.get('kind') in ('dispatch_claimed','dispatch_result','hook_entry')
                    or r.get('stage') == 'place_order']
    attempts = sorted({r.get("order_attempt_id") for r in order_events if _judgment(r) in requested} - {None})
    orders, unlinked, rule_context = [], [], []
    for aid in attempts:
        own = [r for r in events if r.get("order_attempt_id") == aid]
        trigger_ids = {r.get("rule_trigger_id") or (r.get("payload") or {}).get("rule_trigger_id") for r in own} - {None}
        rule_context.extend({"order_attempt_id": aid, "role": "rule_trigger_context_not_ai_correctness", "trigger": r}
                            for r in events if r.get("kind") == "rule_trigger" and r.get("event_id") in trigger_ids)
        dispatch = [r for r in own if r.get("kind") == "dispatch_claimed"]
        responses = [r for r in own if r.get("kind") == "dispatch_result"]
        payload = next((r["payload"] for r in dispatch + responses if isinstance(r.get("payload"), dict)), {})
        originals = [r for r in own if r.get("kind") == "tool_result" and r.get("stage") == "place_order"
                     and (r.get("result") or {}).get("replayed") is False]
        # Non-dispatched calls (risk rejection / zero quantity) are still context,
        # but have no financial execution attribution.
        if not dispatch and not originals:
            originals = [r for r in own if r.get("kind") in ("tool_result", "tool_error") and r.get("stage") == "place_order"
                         and not (r.get("result") or {}).get("replayed")]
        proof_kind = "original_server_call"
        if not originals:
            originals = [r for r in own if r.get("kind") == "hook_entry"
                         and (not dispatch or _instant(r["created_at"]) <= _instant(dispatch[0]["created_at"]))]
            proof_kind = "hook_only_original_call"
        proofs = [(e, *_proof(e, calls, requested, payload or e.get("payload") or {})) for e in originals]
        # Ambiguous original calls are deliberately not collapsed to the one
        # favorable link. This also preserves unlinked original server calls.
        good = proofs and all(reason is None for _, _, reason in proofs)
        if not good:
            refs = [r for r in own if _judgment(r) in requested]
            unlinked.append({"order_attempt_id": aid, "judgment_ids": sorted({_judgment(r) for r in refs}),
                             "reason": next((why for _, _, why in proofs if why), "original_verified_call_unavailable"),
                             "financial_attribution": False, "references": refs,
                             "original_calls": [call for _, call, _ in proofs if call]})
            continue
        result = (responses[-1].get("result") or {}) if responses else {}
        order = {"order_attempt_id": aid, "payload": payload or (originals[0].get("payload") or {}),
                 "result": result, "order_no": result.get("order_no")}
        if not order["order_no"]:
            order["order_no"] = next((r.get("order_no") for r in own if r.get("kind") == "broker_response" and r.get("order_no")), None)
        obs = [r for r in own if r.get("kind") == "execution_observation" and isinstance(r.get("execution"), dict)]
        queries = [r for r in own if r.get("kind") == "execution_query"]
        query = (queries[-1].get("query") or {}) if queries else {}
        try:
            summary = ER.summarize(order, [r["execution"] for r in obs], query_complete=query.get("complete", False),
                                   query_reason=query.get("reason", "no_query_at_cutoff" if not queries else None))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"order_attempt_id": aid, "reason": "execution_summary_error", "error": str(exc)})
            summary = {"order_attempt_id": aid, "confirmation_status": "unknown", "confirmed_qty": None}
        orders.append({"order_attempt_id": aid, "judgment_ids": sorted({call["judgment_id"] for _, call, _ in proofs}),
                       'path_kind':'dispatched_order' if dispatch else 'zero_quantity_order' if
                                   any((e.get('result') or {}).get('qty') == 0 for e in originals) else 'blocked_or_not_dispatched_order',
                       "link_status": "linked", "link_basis": proof_kind,
                       "financial_attribution": bool(dispatch) and proof_kind == "original_server_call",
                       "original_calls": [call for _, call, _ in proofs], "link_events": [e for e, _, _ in proofs],
                       "summary": summary, "events": own, "execution_observations": obs,
                       "legacy_fill_is_execution_confirmation": False})
    non_order, non_order_unlinked = _non_order_paths(events, calls, requested)
    own_events = [r for r in events if _judgment(r) in requested]
    definitions = _definitions(root, own_events, errors)
    return {"knowledge_as_of": cutoff.isoformat(), "judgment_ids": sorted(requested), "orders": orders,
            'non_order_paths': non_order, 'unlinked_non_order_references': non_order_unlinked,
            'engine_definitions':definitions,
            "unlinked_references": unlinked, "rule_context": rule_context,
            "recording_status": "recording_error" if errors else "recorded", "errors": errors,
            "scope": "exact_original_calls_and_order_attempts; no_neighbor_graph_aggregation; no_pnl_attribution"}
