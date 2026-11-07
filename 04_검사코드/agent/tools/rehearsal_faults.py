"""Offline fault rehearsal using the production dispatch and monitor entrypoints.

The caller supplies an isolated WORK directory and a MonkeyPatch instance. Each
scenario gets its own durable ledger, so these deliberately broken attempts do
not become ordinary targets in the main demonstration graph.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import broker as B
import common as C
import execution_trace as E
import monitor as M
import position_episodes as PE


FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/ontology"
BASELINE = "25f7da455f025720564ccd56ced36df4ea3ece1b"


def _paths(mp, folder):
    folder.mkdir(parents=True, exist_ok=True)
    for key, name in (("WORK", ""), ("ACCOUNT", "account.json"),
                      ("POSITIONS", "positions.json"), ("MARKETS", "markets.json"),
                      ("BRIEFING", "briefing.json"), ("JOURNAL", "journal.jsonl")):
        mp.setattr(C, key, str(folder / name))
    mp.setattr(B, "QUOTES", str(folder / "quotes.json"))
    mp.setattr(B, "MOCK_ACCOUNT", str(folder / "mock_account.json"))
    mp.setattr(M, "STATE", str(folder / "monitor_state.json"))
    mp.setenv("RUN_ID", "offline-fault-rehearsal")
    mp.setenv("RUN_ATTEMPT_ID", "fault-" + folder.name)
    mp.setenv("RUN_MODE", "decide")
    mp.setenv("ONTOLOGY_ACCOUNT_SCOPE", "offline-fault-rehearsal")


def _account(qty=0):
    return {"total": 10000000, "cash": 8000000, "holdings": [] if not qty else [
        {"code": "000001", "qty": qty, "price": 20400, "avg_price": 20000}]}


def _fault_order(mp, folder, kind):
    with mp.context() as local:
        _paths(local, folder)
        # This is the real live implementation. Only token/HTTP, balance, quote,
        # and waiting are replaced; there are no usable credentials or URLs.
        local.setenv("MODE", "live")
        local.setattr(B.time, "sleep", lambda _: None)
        broker = object.__new__(B.KiwoomBroker)
        broker.base, broker.env, broker._last = "https://offline.invalid", {}, 0
        calls = []

        def post_json(url, body, headers, **kwargs):
            calls.append({"api_id": headers.get("api-id"), "body": copy.deepcopy(body)})
            if kind == "response_loss":
                raise TimeoutError("synthetic broker received order; response lost")
            return {"return_code": 0, "ord_no": "0000123"}, {}

        broker.S = SimpleNamespace(get_token=lambda _: "offline-no-credential", post_json=post_json)
        broker.account = lambda: _account(100)
        broker.quote = lambda _: 20400
        C.write_json(C.ACCOUNT, _account())
        reserved = E.reserve_attempt(E.account_scope(broker))
        assert reserved["status"] == "reserved", reserved
        attempt_id = reserved["order_attempt_id"]

        def fail(*_args, **_kwargs):
            raise OSError("synthetic " + kind)

        if kind == "order_log_failure":
            local.setattr(C, "append_jsonl", fail)
        elif kind == "dispatch_result_failure":
            local.setattr(E, "_store_result", fail)
        elif kind == "guard_failure_before_send":
            local.setattr(E, "_connect", fail)
        elif kind != "response_loss":
            raise ValueError(kind)

        def order():
            return broker.order("000001", "로컬 장애 시험", "매수", 100,
                                order_attempt_id=attempt_id)

        error_output = io.StringIO()
        with redirect_stderr(error_output):
            first = order()
            retries = [order(), order(), order()]
        expected_sends = 0 if kind == "guard_failure_before_send" else 1
        assert len(calls) == expected_sends, (kind, calls)
        if expected_sends:
            if kind != "dispatch_result_failure":
                assert first["submission_status"] == "submission_unknown", first
                assert first["execution_quality"] == "unknown", first
            # A received acceptance remains present in this call's memory even
            # if its durable result commit fails. Reprocessing must recover an
            # unknown result, never treat that memory as a persisted receipt.
            assert first.get("confirmed_qty") in (None, 0), first
            assert all(x.get("replayed") and x["submission_status"] == "submission_unknown"
                       for x in retries), retries
        else:
            assert first["error_code"] == "DISPATCH_GUARD_ERROR", first
        if "failure" in kind and expected_sends:
            assert first["recording_status"] == "recording_error", first
        result = {
            "scenario": kind, "status": "passed", "order_attempt_id": attempt_id,
            "entrypoint": "KiwoomBroker.order -> execution_trace.dispatch_once",
            "transmission_boundary": "local fake post_json; no HTTP request",
            "broker_send_count": len(calls), "same_attempt_retry_count": len(retries),
            "broker_calls": calls, "first_result": first, "retry_results": retries,
            "expected_fault_log": error_output.getvalue(),
        }
    C.write_json(str(folder / "result.json"), result)
    return result


def _monitor_scenario(mp, clock, folder, *, legacy=False, recording_failure=False):
    fixture_path = FIXTURES / "execution_baseline_source.json"
    fixture = json.loads(fixture_path.read_text())
    assert fixture["baseline_commit"] == BASELINE
    with mp.context() as local:
        _paths(local, folder)
        local.setenv("MODE", "mock")
        params = json.loads((FIXTURES / "params.json").read_text())
        params["decision"]["stop_loss_pct"] = -.05
        local.setattr(C, "params", lambda: copy.deepcopy(params))
        local.setattr(M.time, "time", lambda: clock["now"].timestamp())
        state = {"returncode": 0, "qty": 20, "avg": 20000}

        def holdings():
            return [{"code": "000001", "name": "fixture", "qty": state["qty"],
                     "avg_price": state["avg"], "price": 18900, "value": state["qty"] * 18900}]

        C.write_json(C.POSITIONS, {"000001": {"name": "fixture", "entry_date": "2026-11-05"}})
        local.setattr(M, "holdings", holdings)
        namespace = dict(vars(M)) if legacy else vars(M)
        if legacy:
            # Repository-owned frozen pre-step3 source, never user/AI input.
            exec(compile(fixture["functions"]["monitor"], "<frozen-before-step3>", "exec"), namespace)
        if recording_failure:
            def fail(*_args, **_kwargs):
                raise OSError("synthetic monitor telemetry outage")
            local.setattr(E, "record", fail)

        calls = []

        def fake_exit(command, **_kwargs):
            calls.append({"at": clock["now"].isoformat(),
                          "codes": command[command.index("--codes") + 1],
                          "why": command[command.index("--why") + 1],
                          "has_trigger_ids": "--rule-trigger-ids" in command,
                          "returncode": state["returncode"]})
            return SimpleNamespace(returncode=state["returncode"])

        local.setattr(M.subprocess, "run", fake_exit)
        steps = [
            ("2026-11-06T09:29:00+09:00", 0, None, None),
            ("2026-11-06T09:30:00+09:00", 3, None, None),
            ("2026-11-06T09:31:00+09:00", 0, None, None),
            ("2026-11-06T09:32:00+09:00", 0, None, None),
            ("2026-11-06T09:33:00+09:00", 0, 21000, None),
            ("2026-11-06T09:34:00+09:00", 0, None, -.06),
            ("2026-11-06T09:43:59+09:00", 0, None, None),
            ("2026-11-06T09:44:00+09:00", 3, None, None),
            ("2026-11-06T09:44:01+09:00", 0, None, None),
            ("2026-11-06T09:45:00+09:00", 0, None, None),
            ("2026-11-09T09:30:00+09:00", 0, None, None),
        ]
        episode_ids, output, error_output = [], io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error_output):
            for at, rc, avg, stop in steps:
                clock["now"], state["returncode"] = datetime.fromisoformat(at), rc
                if avg:
                    state.update(avg=avg, qty=40)
                if stop:
                    params["decision"]["stop_loss_pct"] = stop
                namespace["check_once"]()
                if not legacy:
                    episode_ids.append(PE.current("000001"))
        result = {"exit_calls": calls, "retry_state": C.read_json(M.STATE),
                  "position_episode_ids": sorted(set(episode_ids)), "console": output.getvalue(),
                  "expected_fault_log": error_output.getvalue(),
                  "baseline_commit": BASELINE,
                  "baseline_source_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest()}
    C.write_json(str(folder / "result.json"), result)
    return result


def run_faults(mp, clock):
    """Return concrete dispatch and before/after monitor results, fully offline."""
    original_time = clock["now"]
    root = Path(C.WORK) / "rehearsal_faults"
    try:
        orders = [_fault_order(mp, root / name, name) for name in (
            "response_loss", "order_log_failure", "dispatch_result_failure", "guard_failure_before_send")]
        baseline = _monitor_scenario(mp, clock, root / "monitor_baseline", legacy=True)
        current = _monitor_scenario(mp, clock, root / "monitor_current")
        broken = _monitor_scenario(mp, clock, root / "monitor_recording_failure", recording_failure=True)
        comparable = lambda rows: [{k: v for k, v in x.items() if k != "has_trigger_ids"} for x in rows]
        for result in (current, broken):
            assert comparable(result["exit_calls"]) == comparable(baseline["exit_calls"])
            assert result["retry_state"] == baseline["retry_state"]
            assert len(result["position_episode_ids"]) == 1 and result["position_episode_ids"][0]
        assert all(x["has_trigger_ids"] for x in current["exit_calls"])
        assert not any(x["has_trigger_ids"] for x in broken["exit_calls"])
        monitor = {"status": "passed", "entrypoint": "monitor.check_once",
                   "exit_boundary": "local subprocess.run recorder; no AI/exit process started",
                   "baseline": baseline, "current": current, "recording_failure": broken,
                   "same_exit_call_times_and_retry_state": True,
                   "position_episode_does_not_replace_retry_key": True}
        result = {"status": "passed", "orders": orders, "monitor": monitor,
                  "artifact_root": str(root)}
        C.write_json(str(root / "summary.json"), result)
        return result
    finally:
        clock["now"] = original_time
