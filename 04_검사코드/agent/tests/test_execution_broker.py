"""Offline order transport and per-execution confirmation tests.

Every HTTP response is a local fixture. A balance delta is deliberately not a
confirmed execution, including when the legacy engine returns filled=True.
"""
import copy
import socket
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import broker as B
import common as C
import execution_reconcile as X
import execution_trace as E

NOW = datetime.fromisoformat("2026-11-06T10:01:00+09:00")
CODE = "000001"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden in execution tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(C, "WORK", str(tmp_path))
    monkeypatch.setattr(C, "ACCOUNT", str(tmp_path / "account.json"))
    monkeypatch.setattr(C, "now", lambda: NOW)
    monkeypatch.setattr(B, "QUOTES", str(tmp_path / "quotes.json"))
    monkeypatch.setattr(B, "MOCK_ACCOUNT", str(tmp_path / "mock_account.json"))
    monkeypatch.setattr(B.time, "sleep", lambda _: None)
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("RUN_ID", "offline-run")
    monkeypatch.setenv("RUN_ATTEMPT_ID", "attempt-local")
    monkeypatch.delenv("ONTOLOGY_ACCOUNT_SCOPE", raising=False)
    monkeypatch.delenv("ACCOUNT_NAME", raising=False)
    return tmp_path


def account(qty=0):
    return {"total": 10000000, "cash": 8000000, "holdings": [] if qty == 0 else [
        {"code": CODE, "qty": qty, "price": 20400, "avg_price": 20000}]}


def live(post=None, qty=100):
    obj = object.__new__(B.KiwoomBroker)
    obj.base, obj.env, obj._last = "https://mockapi.invalid", {}, 0
    obj.calls = []
    def post_json(url, body, headers, **kwargs):
        obj.calls.append({"url": url, "body": copy.deepcopy(body), "headers": dict(headers), **kwargs})
        return post(url, body, headers, **kwargs) if post else ({"return_code": 0, "ord_no": "0000123"}, {})
    obj.S = SimpleNamespace(get_token=lambda env: "local-fake-token", post_json=post_json)
    obj.account = lambda: account(qty)
    obj.quote = lambda code: 20400
    return obj


def reserve(obj=None):
    row = E.reserve_attempt(E.account_scope(obj))
    assert row["status"] == "reserved"
    return row["order_attempt_id"]


def send(obj, aid=None, qty=100, side="매수"):
    return obj.order(CODE, "로컬 종목", side, qty, order_attempt_id=aid)


def dispatched(qty=100, number="0000123", judgment_id=None):
    aid = reserve()
    E.dispatch_once(E.account_scope(), aid,
                    {"code": CODE, "side": "매수", "qty": qty, "judgment_id": judgment_id},
                    lambda: {"ok": True, "order_no": number, "broker_acknowledgment": "accepted"})
    return aid


def row(number="0000123", execution="0000001", qty="40", price="20400", **extra):
    return {"ord_no": number, "cntr_no": execution, "stk_cd": "A" + CODE,
            "dmst_stex_tp": "KRX", "io_tp_nm": "매수", "cntr_qty": qty,
            "cntr_uv": price, "cntr_tm": "10:02:03", "cnfm_qty": "100", **extra}


class QueryBroker:
    def __init__(self, rows, complete=True):
        self.rows, self.complete, self.calls = rows, complete, []

    def query_order_executions(self, date, **kwargs):
        self.calls.append((date, kwargs))
        return {"rows": copy.deepcopy(self.rows), "pages": [], "complete": self.complete,
                "reason": None if self.complete else "page_limit"}


def test_order_requires_reserved_id_before_transmission():
    obj = live()
    assert send(obj)["error_code"] == "ORDER_ATTEMPT_REQUIRED"
    assert obj.calls == []


def test_response_lost_after_acceptance_never_retransmits():
    def post(*args, **kwargs):
        raise TimeoutError("server received order; response lost")
    obj = live(post)
    aid = reserve(obj)
    first = send(obj, aid)
    assert first["submission_status"] == "submission_unknown"
    assert first["execution_quality"] == "unknown"
    assert send(obj, aid)["replayed"]
    assert send(obj, aid)["submission_status"] == "submission_unknown"
    assert len(obj.calls) == 1
    query = QueryBroker([])
    assert X.refresh_execution_status(aid, query)["query_reason"] == "order_number_unknown"
    assert query.calls == []


def test_orders_log_failure_keeps_claim_and_received_number(monkeypatch):
    obj = live()
    aid = reserve(obj)
    def fail(*args, **kwargs):
        raise OSError("orders.jsonl disk failure")
    monkeypatch.setattr(C, "append_jsonl", fail)
    result = send(obj, aid)
    assert result["recording_status"] == "recording_error"
    assert result["execution_quality"] == "unknown"
    assert result["submission_status"] == "submission_unknown"
    assert result["broker_acknowledgment"] == "accepted"
    assert send(obj, aid)["replayed"] and len(obj.calls) == 1
    confirmed = X.refresh_execution_status(aid, QueryBroker([row()]))
    assert confirmed["confirmed_qty"] == 40


def test_post_response_guard_result_failure_never_retransmits(monkeypatch):
    obj = live()
    aid = reserve(obj)
    C.write_json(C.ACCOUNT, account())
    def fail(*args, **kwargs):
        raise OSError("result commit failed")
    monkeypatch.setattr(E, "_store_result", fail)
    first = send(obj, aid)
    assert first["recording_status"] == "recording_error"
    assert send(obj, aid)["submission_status"] == "submission_unknown"
    assert len(obj.calls) == 1
    assert X.refresh_execution_status(aid, QueryBroker([row()]))["confirmed_qty"] == 40


def test_account_save_failure_does_not_repeat_order(monkeypatch):
    obj = live()
    aid = reserve(obj)
    del obj.account  # Exercise the real account-to-save path after a local balance response.
    snapshot = {"total": 10000000, "stock_eval": 2040000, "holdings": [
        {"code": CODE, "name": "로컬 종목", "qty": 100, "price": 20400,
         "avg_price": 20000, "value": 2040000, "pnl_pct": .02}]}
    obj._balance = lambda: copy.deepcopy(snapshot)
    write = C.write_json
    def broken_write(path, value):
        if str(path) == C.ACCOUNT:
            raise OSError("save_account failed after balance read")
        return write(path, value)
    monkeypatch.setattr(C, "write_json", broken_write)
    result = send(obj, aid)
    assert result["qty"] == 100 and result["filled"] is None
    assert result["execution_quality"] == "unknown"
    assert result["confirmed_qty"] is None
    assert len(result["execution_observations"]) == 5
    assert send(obj, aid)["replayed"] and len(obj.calls) == 1
    snapshots = [e for e in X._events(aid)[0] if e["kind"] == "broker_balance_snapshot"]
    assert len(snapshots) == 5 and all(e["response"] == snapshot for e in snapshots)


@pytest.mark.parametrize("api,expected", [("kt10000", 1), ("kt10001", 1), ("kt00009", 2)])
def test_http429_retries_only_reads(api, expected):
    def post(*args, **kwargs):
        raise RuntimeError("HTTP 429 Too Many Requests")
    obj = live(post)
    with pytest.raises(RuntimeError):
        obj._call("/local", api, {})
    assert len(obj.calls) == expected


def test_explicit_rejection_is_distinct_from_unknown():
    obj = live(lambda *a, **k: ({"return_code": 20, "return_msg": "local rejection"}, {}))
    aid = reserve(obj)
    result = send(obj, aid)
    assert result["submission_status"] == "rejected"
    assert result["broker_acknowledgment"] == "rejected"
    assert send(obj, aid)["replayed"] and len(obj.calls) == 1


@pytest.mark.parametrize("before,after,side,legacy,attributed", [
    (0, 100, "매수", 100, 100), (100, 80, "매수", 20, None),
    (0, 120, "매수", 120, None), (100, 60, "매도", 40, 40),
])
def test_balance_inference_preserves_legacy_value_but_is_not_confirmation(before, after, side, legacy, attributed):
    C.write_json(C.ACCOUNT, account(before))
    obj = live(qty=after)
    result = send(obj, reserve(obj), side=side)
    assert result["qty"] == legacy and result["filled"] is True
    assert result["execution_quality"] == "balance_delta_inference"
    assert send(obj, result["order_attempt_id"], side=side)["execution_quality"] == "balance_delta_inference"
    assert result["confirmed_qty"] is None and result["unconfirmed_qty"] == 100
    observation = result["execution_observations"][-1]
    assert observation["observed_balance_delta"] == after - before
    assert observation["attributed_estimate_qty"] == attributed


def test_missing_previous_balance_is_not_observed_zero():
    obj = live(qty=100)
    result = send(obj, reserve(obj))
    observation = result["execution_observations"][0]
    assert result["qty"] == 100  # Existing engine's fallback remains unchanged.
    assert observation["before_qty"] is None
    assert observation["observed_balance_delta"] is None
    assert observation["attributed_estimate_qty"] is None
    assert observation["attribution_status"] == "unknown"


def test_query_pages_preserve_headers_raw_and_request_contract():
    responses = iter([
        ({"return_code": 0, "acnt_ord_cntr_prst_array": [row()]}, {"cont-yn": "Y", "next-key": "page-two"}),
        ({"return_code": 0, "acnt_ord_cntr_prst_array": [row(execution="2", qty="60")]}, {"Cont-Yn": "N"}),
    ])
    obj = live(lambda *a, **k: next(responses))
    result = obj.query_order_executions("2026-11-06")
    assert result["complete"] and len(result["rows"]) == 2
    assert len(result["pages"]) == 2
    assert all(c["headers"]["api-id"] == "kt00009" for c in obj.calls)
    assert obj.calls[0]["body"] == {"ord_dt": "20261106", "stk_bond_tp": "1", "mrkt_tp": "0",
                                      "sell_tp": "0", "qry_tp": "0", "stk_cd": "", "fr_ord_no": "", "dmst_stex_tp": "KRX"}
    assert obj.calls[1]["headers"]["next-key"] == "page-two"
    assert result["pages"][1]["headers"]["cont-yn"] == "N"
    assert 0 < obj.calls[0]["timeout"] <= 10


@pytest.mark.parametrize("body,headers,limit,reason,count", [
    ({"acnt_ord_cntr_prst_array": []}, {"cont-yn": "Y", "next-key": "same"}, 20, "repeated_or_missing_page_key", 2),
    ({"acnt_ord_cntr_prst_array": []}, {"cont-yn": "Y"}, 20, "repeated_or_missing_page_key", 1),
    ({"unexpected": []}, {}, 20, "invalid_response", 1),
    ({"acnt_ord_cntr_prst_array": [row()]}, {"cont-yn": "Y", "next-key": "next"}, 1, "page_limit", 1),
])
def test_incomplete_queries_never_claim_completeness(body, headers, limit, reason, count):
    obj = live(lambda *a, **k: (body, headers))
    result = obj.query_order_executions("2026-11-06", max_pages=limit)
    assert not result["complete"] and result["reason"] == reason
    assert len(obj.calls) == count


def test_query_deadline_stops_before_http_and_partial_failure_preserves_rows():
    obj = live()
    result = obj.query_order_executions("2026-11-06", max_seconds=0)
    assert result["reason"] == "time_limit" and obj.calls == []
    calls = []
    def post(*a, **k):
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("local query failure")
        return {"acnt_ord_cntr_prst_array": [row()]}, {"cont-yn": "Y", "next-key": "2"}
    result = live(post).query_order_executions("2026-11-06")
    assert result["reason"] == "query_error" and len(result["rows"]) == 1


@pytest.mark.parametrize("changes,reason", [
    ({"ord_no": "123"}, "different_or_unknown_order"),
    ({"dmst_stex_tp": "NXT"}, "exchange_unconfirmed"),
    ({"stk_cd": "A000002"}, "instrument_mismatch"),
    ({"io_tp_nm": "매도"}, "side_mismatch"),
    ({"cntr_no": "0"}, "execution_fields_missing"),
    ({"cntr_qty": "", "cnfm_qty": "100"}, "execution_fields_missing"),
    ({"cntr_qty": "1.5"}, "execution_fields_missing"),
    ({"cntr_uv": "NaN"}, "execution_fields_missing"),
    ({"cntr_tm": ""}, "execution_time_missing"),
])
def test_confirmation_requires_exact_execution_fields(changes, reason):
    aid = dispatched()
    order = next(o for o in E.pending_orders() if o["order_attempt_id"] == aid)
    execution, actual_reason = X.normalize_execution(row(**changes), order)
    assert execution is None and actual_reason == reason


def test_partial_execution_deduplicates_and_remaining_stays_unconfirmed():
    aid = dispatched()
    broker = QueryBroker([row(), row()])
    first = X.refresh_execution_status(aid, broker)
    again = X.refresh_execution_status(aid, broker)
    assert first["confirmed_qty"] == again["confirmed_qty"] == 40
    assert first["unconfirmed_qty"] == 60
    assert first["confirmation_status"] == "confirmed_partial"
    events, _ = X._events(aid)
    assert len([e for e in events if e["kind"] == "execution_observation"]) == 1
    assert first["executions"][0]["key"] == [E.account_scope(), "2026-11-06", "KRX", "0000123", "0000001"]


def test_empty_result_is_unknown_not_zero_and_never_matches_by_code_quantity():
    aid = dispatched()
    assert X.refresh_execution_status(aid, QueryBroker([]))["confirmed_qty"] is None
    result = X.refresh_execution_status(aid, QueryBroker([row(number="another-order", qty="100")]))
    assert result["confirmed_qty"] is None and result["unconfirmed_qty"] == 100


def test_conflicting_execution_identity_keeps_both_and_does_not_sum():
    aid = dispatched()
    broker = QueryBroker([row(qty="100")])
    assert X.refresh_execution_status(aid, broker)["confirmation_status"] == "confirmed_full"
    broker.rows = [row(qty="100", price="20500")]
    conflict = X.refresh_execution_status(aid, broker)
    assert conflict["confirmation_status"] == "conflict"
    assert conflict["confirmed_qty"] is None and len(conflict["conflicts"][0]) == 2
    assert X.refresh_pending(broker)["orders"][0]["confirmation_status"] == "conflict"


def test_grouped_refresh_queries_once_and_preserves_original_run(monkeypatch):
    first = dispatched(number="1")
    second = dispatched(number="2")
    monkeypatch.setenv("RUN_ATTEMPT_ID", "later-attempt")
    broker = QueryBroker([row(number="1", qty="100"), row(number="2", qty="60")])
    result = X.refresh_pending(broker)
    assert len(broker.calls) == 1 and len(result["orders"]) == 2
    states = {x["order_attempt_id"]: x for x in result["orders"]}
    assert states[first]["confirmed_qty"] == 100 and states[second]["confirmed_qty"] == 60
    assert all(e["run_attempt_id"] == "attempt-local" for aid in (first, second)
               for e in X._events(aid)[0] if e["kind"] == "execution_observation")
    assert len(X.refresh_pending(broker)["orders"]) == 1


def test_automatic_collection_is_date_scoped_and_manual_refresh_can_revisit(monkeypatch):
    aid = dispatched()
    monkeypatch.setattr(C, "now", lambda: datetime.fromisoformat("2026-11-07T10:00:00+09:00"))
    broker = QueryBroker([row(qty="100")])
    assert X.refresh_pending(broker)["status"] == "not_applicable" and broker.calls == []
    assert X.refresh_execution_status(aid, broker)["confirmed_qty"] == 100
    assert broker.calls[0][0] == "2026-11-06"


def test_shared_judgment_does_not_mix_attempt_observations():
    first = dispatched(number="1", judgment_id="judgment-shared")
    second = dispatched(number="2", judgment_id="judgment-shared")
    broker = QueryBroker([row(number="1", qty="100")])
    assert X.refresh_execution_status(first, broker)["confirmed_qty"] == 100
    assert X.refresh_execution_status(second, broker)["confirmed_qty"] is None
    assert all(e.get("order_attempt_id") == second for e in X._events(second)[0])


def test_failed_observation_storage_not_reported_as_recorded(monkeypatch):
    aid = dispatched()
    original = E.record
    def record(kind, **fields):
        return {"recording_status": "recording_error"} if kind == "execution_observation" else original(kind, **fields)
    monkeypatch.setattr(E, "record", record)
    result = X.refresh_execution_status(aid, QueryBroker([row(qty="100")]))
    assert result["recording_status"] == "recording_error"
    assert len(X.refresh_pending(QueryBroker([row()]))["orders"]) == 1


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("audit_fails", [False, True])
def test_query_failure_preserves_audit_failure_state(monkeypatch, batch, audit_fails):
    aid = dispatched()
    class BrokenQuery:
        def query_order_executions(self, *args, **kwargs):
            raise RuntimeError("local read failure")
    if audit_fails:
        original = E.record
        def record(kind, **fields):
            if kind == "execution_query_error":
                raise OSError("audit disk failure")
            return original(kind, **fields)
        monkeypatch.setattr(E, "record", record)
    result = X.refresh_pending(BrokenQuery()) if batch else X.refresh_execution_status(aid, BrokenQuery())
    assert result["status"] == "query_error" and result["query_complete"] is False
    assert result["recording_status"] == ("recording_error" if audit_fails else "recorded")


def test_unreadable_history_keeps_recording_error(isolated):
    aid = dispatched()
    corrupt = isolated / "ontology/executions/attempt-local/execution_observation/corrupt.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{", encoding="utf-8")
    result = X.refresh_execution_status(aid, QueryBroker([row(qty="100")]))
    assert result["recording_status"] == "recording_error"
    assert len(X.refresh_pending(QueryBroker([row(qty="100")]))["orders"]) == 1


def test_mock_execution_is_simulated_and_replay_does_not_change_holdings(monkeypatch):
    monkeypatch.setenv("MODE", "mock")
    obj = object.__new__(B.MockBroker)
    obj.state = {"cash": 10000000, "positions": {}}
    obj.quote = lambda code: 20400
    aid = reserve(obj)
    first = send(obj, aid)
    again = send(obj, aid)
    assert first["execution_confirmation"] == "simulated" and first["confirmed_qty"] is None
    assert first["execution_quality"] == again["execution_quality"] == "simulated"
    assert first["simulated_qty"] == 100 and again["replayed"]
    assert obj.state["positions"][CODE]["qty"] == 100
    assert X.refresh_pending()["status"] == "not_applicable"
