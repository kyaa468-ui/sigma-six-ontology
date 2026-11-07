"""Read-only broker execution reconciliation; never submits or infers a new order.

Only per-execution broker rows are confirmations. Balance changes and the legacy
engine's effective qty/price remain separate observations in execution_trace.
"""
import copy
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import common as C
import execution_trace as E


def _record(kind, **fields):
    try:
        return E.record(kind, **fields)
    except Exception as exc:
        try:
            import judgment_coverage as JC
            JC.recording_error("execution_reconcile:" + kind, exc, fields.get("run_attempt_id"))
        except Exception:
            pass
        return {"recording_status": "recording_error", "error": str(exc)}


def _positive(value, integral=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value).strip().replace(",", ""))
        if not n.is_finite() or n <= 0 or integral and n != n.to_integral_value():
            return None
        return int(n) if integral or n == n.to_integral_value() else float(n)
    except (ValueError, InvalidOperation):
        return None


def normalize_execution(row, order):
    """Return a uniquely identified execution, or a precise non-confirmation reason."""
    if not isinstance(row, dict):
        return None, "invalid_row"
    oid = str(row.get("ord_no") or "").strip()
    expected = _order_number(order)
    if not expected or oid != expected:
        return None, "different_or_unknown_order"
    exchange = str(row.get("dmst_stex_tp") or "").strip()
    if exchange != "KRX":
        return None, "exchange_unconfirmed"
    code = str(row.get("stk_cd") or "").strip()
    if re.fullmatch(r"[AJQ]\d{6}", code):
        code = code[1:]
    payload = order.get("payload") or {}
    if code != str(payload.get("code", "")):
        return None, "instrument_mismatch"
    io = str(row.get("io_tp_nm") or "")
    direction = "매수" if "매수" in io else "매도" if "매도" in io else None
    if direction is not None and direction != payload.get("side"):
        return None, "side_mismatch"
    execution_id = str(row.get("cntr_no") or "").strip()
    qty, price = _positive(row.get("cntr_qty"), True), _positive(row.get("cntr_uv"))
    if not execution_id or not execution_id.strip("0") or qty is None or price is None:
        return None, "execution_fields_missing"
    tm = str(row.get("cntr_tm") or "").strip()
    try:
        datetime.strptime(tm, "%H:%M:%S")
    except ValueError:
        return None, "execution_time_missing"
    date = str(order.get("date") or str(order.get("created_at", ""))[:10])
    scope = order.get("account_scope") or order.get("scope")
    key = [scope, date, exchange, oid, execution_id]
    return {"key": key, "order_no": oid, "execution_no": execution_id,
            "code": code, "side": payload.get("side"), "qty": qty, "price": price,
            "order_date": date, "exchange": exchange, "provider_time_raw": tm,
            "observed_at": date + "T" + tm + "+09:00", "time_policy": "order_date_plus_provider_time_KST",
            "source": "kiwoom:kt00009", "confirmation": "broker_execution",
            "original_order_no": row.get("orig_ord_no"), "amend_cancel_type": row.get("mdfy_cncl_tp")}, None


def _order_number(order):
    result = order.get("result") or {}
    return str(order.get("order_no") or result.get("order_no") or "").strip()


def _events(attempt_id):
    trace = E.get_execution_trace(order_attempt_id=attempt_id)
    # The graph lookup also expands shared judgments/plans. Those neighboring
    # orders are not evidence for this particular broker transmission.
    return [e for e in trace.get("events", []) if e.get("order_attempt_id") == attempt_id], trace.get("recording_status")


def _recover_response(order, events):
    """An archived broker response may survive a later legacy log write failure."""
    if _order_number(order):
        return order
    matches = {str(e.get("order_no")) for e in events if e.get("kind") == "broker_response"
               and e.get("acknowledgment") == "accepted" and e.get("order_no")}
    matches.update(str(e["result"]["order_no"]) for e in events
                   if e.get("kind") == "dispatch_result" and isinstance(e.get("result"), dict)
                   and (e["result"].get("broker_acknowledgment") == "accepted"
                        or e["result"].get("submission_status") == "accepted")
                   and e["result"].get("order_no"))
    if len(matches) == 1:
        return {**order, "order_no": matches.pop()}
    return order


def summarize(order, observations, *, query_complete=False, query_reason=None):
    """Deduplicate execution identities. Conflicting rows never silently replace facts."""
    grouped = {}
    for execution in observations:
        key = json.dumps(execution["key"], ensure_ascii=False, separators=(",", ":"))
        # Receipt timestamps and raw response formatting do not create a new fill.
        grouped.setdefault(key, {})[json.dumps(execution, sort_keys=True, ensure_ascii=False)] = execution
    conflicts = [list(values.values()) for values in grouped.values() if len(values) > 1]
    distinct = [next(iter(values.values())) for values in grouped.values() if len(values) == 1]
    qty = sum(x["qty"] for x in distinct)
    requested = _positive((order.get("payload") or {}).get("qty"), True)
    excessive = requested is not None and qty > requested
    confirmed = None if conflicts or excessive or not distinct else qty
    prices = sum(x["price"] * x["qty"] for x in distinct)
    return {"order_attempt_id": order["order_attempt_id"], "order_no": _order_number(order) or None,
            "requested_qty": requested, "confirmed_qty": confirmed,
            "unconfirmed_qty": requested - confirmed if requested is not None and confirmed is not None else requested,
            "confirmed_average_price": prices / qty if confirmed is not None else None,
            "uncontested_execution_qty": qty, "executions": distinct, "conflicts": conflicts,
            "confirmation_status": "conflict" if conflicts or excessive else "confirmed_full" if confirmed == requested and confirmed is not None
                                   else "confirmed_partial" if confirmed is not None else "unknown",
            "query_complete": bool(query_complete), "query_reason": query_reason,
            "remaining_meaning": "not_confirmed; not proof of unfilled/cancelled/rejected"}


def _refresh_orders(orders, broker):
    if not orders:
        return []
    histories, history_errors = {}, {}
    effective = []
    for order in orders:
        events, status = _events(order["order_attempt_id"])
        histories[order["order_attempt_id"]] = events
        history_errors[order["order_attempt_id"]] = status == "recording_error"
        effective.append(_recover_response(order, events))
    number_known = any(_order_number(order) for order in effective)
    if number_known:
        query = broker.query_order_executions(orders[0]["date"], max_pages=20, max_seconds=30)
    else:
        query = {"rows": [], "pages": [], "complete": False, "reason": "order_number_unknown"}
    output = []
    for order in effective:
        aid = order["order_attempt_id"]
        context = {"order_attempt_id": aid, "run_attempt_id": order.get("run_attempt_id"),
                   "run_id": order.get("run_id"), "account_scope": order.get("account_scope", order.get("scope"))}
        event = _record("execution_query", **context, query=copy.deepcopy(query),
                        observed_at=None, available_at=C.now().isoformat())
        prior = [e["execution"] for e in histories[aid] if e.get("kind") == "execution_observation" and isinstance(e.get("execution"), dict)]
        seen = {json.dumps(e, sort_keys=True, ensure_ascii=False) for e in prior}
        observations = list(prior)
        ignored = []
        recording_error = history_errors[aid] or event.get("recording_status") == "recording_error"
        for row in query.get("rows", []):
            execution, reason = normalize_execution(row, order)
            if execution is None:
                if reason != "different_or_unknown_order":
                    ignored.append({"reason": reason, "row": row})
                continue
            serialized = json.dumps(execution, sort_keys=True, ensure_ascii=False)
            if serialized not in seen:
                seen.add(serialized)
                observations.append(execution)
                result = _record("execution_observation", **context, execution=execution,
                                 raw=copy.deepcopy(row), available_at=C.now().isoformat())
                recording_error |= result.get("recording_status") == "recording_error"
        summary = summarize(order, observations, query_complete=query.get("complete", False), query_reason=query.get("reason"))
        summary["ignored_rows"] = ignored
        stored = _record("execution_status", **context, summary=summary)
        recording_error |= stored.get("recording_status") == "recording_error"
        summary["recording_status"] = "recording_error" if recording_error else "recorded"
        output.append(summary)
    return output


def refresh_execution_status(order_attempt_id, broker=None):
    """Query an existing attempt; do not create an attempt or submit an order."""
    orders = [x for x in E.pending_orders() if x.get("order_attempt_id") == order_attempt_id]
    if not orders:
        return {"order_attempt_id": order_attempt_id, "status": "not_found", "confirmed_qty": None}
    if broker is None and C.mode() == "mock":
        return {"order_attempt_id": order_attempt_id, "status": "simulated", "confirmed_qty": None}
    if broker is None:
        import broker as B
        broker = B.get()
    order = orders[0]
    if order.get("account_scope", order.get("scope")) != E.account_scope(broker):
        return {"order_attempt_id": order_attempt_id, "status": "account_scope_mismatch", "confirmed_qty": None}
    try:
        return _refresh_orders([order], broker)[0]
    except Exception as exc:
        audit = _record("execution_query_error", order_attempt_id=order_attempt_id,
                        run_attempt_id=order.get("run_attempt_id"), error=f"{type(exc).__name__}: {exc}")
        return {"order_attempt_id": order_attempt_id, "status": "query_error", "confirmed_qty": None,
                "query_complete": False, "recording_status": audit.get("recording_status", "recording_error"),
                "error": str(exc)}


def refresh_pending(broker=None, date=None):
    """One date/account batch after a run; no new daemon and no order mutation."""
    date = date or C.today().isoformat()
    orders = E.pending_orders(date=date)
    orders = [o for o in orders if o.get("state") in ("claimed", "complete")
              and (o.get("result") or {}).get("broker_acknowledgment") not in ("rejected", "simulated")]
    pending = []
    for order in orders:
        events, status = _events(order["order_attempt_id"])
        observations = [e["execution"] for e in events if e.get("kind") == "execution_observation"
                        and isinstance(e.get("execution"), dict)]
        # Recompute from immutable observations: UUID filename order is not event
        # order, and a later conflicting observation must reopen reconciliation.
        summary = summarize(order, observations)
        if status == "recording_error" or summary.get("confirmation_status") != "confirmed_full":
            pending.append(order)
    if not pending or broker is None and C.mode() == "mock":
        return {"status": "not_applicable" if not pending else "simulated", "orders": []}
    if broker is None:
        import broker as B
        broker = B.get()
    scope = E.account_scope(broker)
    pending = [o for o in pending if o.get("account_scope", o.get("scope")) == scope]
    try:
        return {"status": "queried", "orders": _refresh_orders(pending, broker)}
    except Exception as exc:
        audits = []
        for order in pending:
            audits.append(_record("execution_query_error", order_attempt_id=order["order_attempt_id"],
                                  run_attempt_id=order.get("run_attempt_id"), error=f"{type(exc).__name__}: {exc}"))
        return {"status": "query_error", "orders": [], "query_complete": False,
                "recording_status": "recorded" if audits and all(a.get("recording_status") == "recorded" for a in audits)
                                    else "recording_error", "error": str(exc)}
