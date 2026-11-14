"""Offline execution rehearsal: production entrypoints, synthetic external replies.

The caller supplies a pytest MonkeyPatch and an already isolated common.WORK /
config tree.  This helper never opens a real network connection or model process.
It intentionally does not replace candidate, sizing, risk, judgment, execution,
or reconciliation functions.  It is a local rehearsal tool, not a trading mode.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime
import importlib.util
import io
import json
import os
import time as system_time
from pathlib import Path
from types import SimpleNamespace


AGENT = Path(__file__).resolve().parents[1]
MAIN = "000010"
NAMES = {MAIN: "모의 반도체", "000020": "모의 장비", "000030": "모의 소재"}


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hook(mp, args, invocation):
    hook = _module("rehearsal_risk_hook", AGENT / ".claude/hooks/risk_check.py")
    event = {"tool_name": "mcp__kiwoom__place_order", "session_id": "offline-rehearsal",
             "tool_use_id": invocation, "tool_input": args}
    output = io.StringIO()
    with mp.context() as local:
        local.setattr(hook.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(event).encode())))
        with redirect_stdout(output):
            try:
                hook.main()
            except SystemExit as exc:
                assert exc.code in (None, 0)
    return json.loads(output.getvalue())["hookSpecificOutput"]


def _daily(code):
    import pandas as pd
    # Last twenty closes average exactly 20,000 and last five average 20,200.
    # Oscillating earlier bars give real GS Quant computations nonconstant data.
    closes = [19000 + (i % 7) * 55 + i * 6 for i in range(80)]
    closes += [19900] * 10 + [20000] * 5 + [20000, 20100, 20200, 20300, 20400]
    if code == "000020":
        closes = [x * 1.5 for x in closes]
    elif code == "000030":
        closes = [x * 2 for x in closes]
    index = pd.bdate_range(end="2026-11-05", periods=len(closes))
    return pd.DataFrame({"시가": closes, "고가": [x * 1.01 for x in closes],
                         "저가": [x * .99 for x in closes], "종가": closes,
                         "거래량": [1_000_000] * len(closes),
                         "거래대금": [10_000_000_000] * len(closes),
                         "등락률": [1.] * len(closes)}, index=index)


class SyntheticTransport:
    """Fake transport/source responses consumed by the real Kiwoom adapter."""
    def __init__(self):
        self.calls, self.orders, self.executions = [], [], []
        self.quantity = 0
        self.prices = {MAIN: 20400, "000020": 30600, "000030": 40800,
                       "114800": 20400, "069500": 20400}
        self.next_mode = "partial"
        self.order_acknowledged = lambda: None

    def balance(self, *_):
        import common as C
        hs = []
        if self.quantity:
            px = self.prices[MAIN]
            hs = [{"code": MAIN, "name": NAMES[MAIN], "qty": self.quantity,
                   "avg_price": 20400, "price": px, "value": self.quantity * px,
                   "pnl_pct": (px / 20400 - 1) * 100}]
        value = sum(x["value"] for x in hs)
        return {"total": 50_000_000, "stock_eval": value, "holdings": hs,
                "capture_metadata": {"source": "synthetic:balance", "pagination_complete": True,
                    "required_fields_present": True, "provider_observed_at": C.now().isoformat(),
                    "cash_validation": {"status": "confirmed_synthetic", "value": 50_000_000 - value,
                                        "source": "synthetic:known_initial_cash"},
                    "holding_prices": [{"code": h["code"], "source": "synthetic:balance_price",
                        "received_at": C.now().isoformat(), "provider_observed_at": C.now().isoformat(),
                        "executable_qty": 10000, "capacity_source": "synthetic_declared_capacity"} for h in hs],
                    "provenance": "synthetic"}}

    def post(self, url, body, headers, **kwargs):
        api = headers["api-id"]
        # The fake token is never persisted, just like actual broker audit logs.
        self.calls.append({"api": api, "body": deepcopy(body)})
        if api == "ka10032":
            rows = [{"stk_cd": c, "stk_nm": NAMES[c], "cur_prc": str(self.prices[c]),
                     "flu_rt": "2", "now_trde_qty": "1000000"} for c in NAMES]
            return {"return_code": 0, "trde_prica_upper": rows}, {}
        if api == "ka10065":
            foreign = body["orgn_tp"] == "9000"
            values = {MAIN: "150000" if foreign else "-225000",
                      "000020": "0" if foreign else "120000"}
            # 000030 has no institution rank entry: absence must remain unknown.
            if foreign:
                values["000030"] = "50000"
            return {"return_code": 0, "opmr_invsr_trde_upper": [
                {"stk_cd": c, "stk_nm": NAMES[c], "netslmt": q} for c, q in values.items()]}, {}
        if api == "ka10001":
            import common as C
            return {"return_code": 0, "cur_prc": str(self.prices[body["stk_cd"]]), "flu_rt": "2",
                "capture_metadata": {"source": "synthetic:quote", "provider_observed_at": C.now().isoformat(),
                    "price_basis": "unadjusted", "executable_qty": 10000,
                    "capacity_source": "synthetic_declared_capacity", "provenance": "synthetic"}}, {}
        if api == "kt00009":
            return {"return_code": 0, "acnt_ord_cntr_prst_array": deepcopy(self.executions)}, {"cont-yn": "N"}
        if api in ("kt10000", "kt10001"):
            ordinal = len(self.orders) + 1
            row = {"api": api, "body": deepcopy(body), "order_no": f"{ordinal:07d}", "mode": self.next_mode}
            self.orders.append(row)
            if self.next_mode == "response_lost":
                raise TimeoutError("synthetic broker accepted request; reply lost")
            requested = int(body["ord_qty"])
            filled = min(40, requested) if self.next_mode == "partial" else requested
            self.quantity += filled if api == "kt10000" else -filled
            self.executions.append({"ord_no": row["order_no"], "cntr_no": f"{ordinal:07d}",
                "stk_cd": "A" + body["stk_cd"], "dmst_stex_tp": "KRX",
                "io_tp_nm": "매수" if api == "kt10000" else "매도", "cntr_qty": str(filled),
                "cntr_uv": str(self.prices[body["stk_cd"]]), "cntr_tm": "10:30:01",
                "cnfm_qty": str(requested)})
            self.order_acknowledged()
            return {"return_code": 0, "ord_no": row["order_no"]}, {}
        raise AssertionError(f"Unexpected fake broker API: {api}")


def _install_sources(mp):
    import common as C
    import data_prep as D
    import broker as B
    import intraday as I
    import mcp_server as M
    import pandas as pd
    transport = SyntheticTransport()
    broker = object.__new__(B.KiwoomBroker)
    broker.base, broker.env, broker._last = "https://mockapi.rehearsal.invalid", {}, 0
    broker.S = SimpleNamespace(get_token=lambda *_: "offline-fake-token", post_json=transport.post,
                               get_balance=transport.balance, num=I.num)
    mp.setattr(B, "get", lambda: broker)
    mp.setattr(M, "_broker", broker)
    # Clock/cache expiration is an external boundary. The broker's original
    # quote functions and their source identity remain unchanged.
    mp.setattr(B, "time", SimpleNamespace(time=lambda: C.now().timestamp(),
        sleep=lambda *_: None, monotonic=system_time.monotonic))
    mp.setattr(D.time, "sleep", lambda *_: None)
    mp.setenv("MODE", "live")  # Actual live adapter with transport entirely local.
    for key in ("KRX_ID", "KRX_PW", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"):
        mp.setenv(key, "offline-fixture")

    def market(*args, **kwargs):
        if "market" in kwargs:
            codes = list(NAMES) if kwargs["market"] == "KOSPI" else []
            return pd.DataFrame({"거래대금": [20_000_000_000] * len(codes)}, index=pd.Index(codes, dtype=str))
        return _daily(args[2])

    def flows(*_):
        return pd.DataFrame({"외국인합계": [600_000_000] * 5, "기관합계": [-800_000_000] * 5},
                            index=pd.bdate_range(end="2026-11-05", periods=5))

    class Reply(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): self.close()

    def urlopen(request, **_):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "query1.finance.yahoo.com" in url:
            body = {"chart": {"result": [{"timestamp": [1793836800, 1793923200], "meta": {"gmtoffset": -18000},
                    "indicators": {"quote": [{"close": [20000, 20100]}]}}]}}
        elif "naverapihub" in url:
            body = {"items": [{"title": "모의 반도체 신규 공급 계약 발표 — 검증용 뉴스",
                     "pubDate": "Thu, 05 Nov 2026 09:00:00 +0900", "originallink": "https://fixture.invalid/news/1"}]}
        else:
            raise AssertionError(f"Unexpected fixture market URL: {url}")
        return Reply(json.dumps(body).encode())

    mp.setattr(D.stock, "get_market_ohlcv", market)
    mp.setattr(D.stock, "get_etf_ohlcv_by_date", lambda start, end, code: _daily(code))
    mp.setattr(D.stock, "get_index_ohlcv", lambda *_: _daily("1001"))
    mp.setattr(D.stock, "get_market_ticker_name", lambda c: NAMES.get(c, "모의 ETF"))
    mp.setattr(D.stock, "get_market_trading_value_by_date", flows)
    mp.setattr(D.urllib.request, "urlopen", urlopen)
    return broker, transport


def _submission(text, *, key, direction="sell"):
    import common as C
    delivery = text.split("브리핑 전달 ID: ")[-1].splitlines()[0]
    receipt = __import__("record_store").load(Path(C.WORK) / "ontology/deliveries" / (delivery + ".json"))
    evidence = next(e for e in receipt["evidence_catalog"] if e["subject"] == MAIN and e["field"] == "inst_net")
    return {"request_key": key, "code": MAIN, "recommended_action": "매수", "confidence_for": "매수",
            "confidence_raw": .7, "decision": "고정 모의 판단: 상반된 수급을 인정하고 추세 유지 가설을 제출함",
            "delivery_ids": [delivery],
            "premises": [{"definition_id": "MA20_MAINTENANCE@v2", "days": 3, "direction": "above", "claim_ids": ["inst"]}],
            "arguments": [{"id": "inst", "kind": "fact_claim", "proposer": "bear", "stance": "oppose", "adoption": "reference",
                "evidence": {"delivery_id": delivery, "evidence_id": evidence["evidence_id"]},
                "measurement_kind": evidence["kind"], "assertion": {"op": "sign", "direction": direction}},
                {"id": "hypothesis", "kind": "hypothesis", "proposer": "bull", "stance": "support", "adoption": "adopted",
                 "text": "외국인 매수세가 기관 매도 압력을 받아낼 것이다", "claim_ids": ["inst"]}]}


def _monitor_tick(transport):
    """Supply the live-snapshot boundary, then run the original monitor check."""
    import common as C
    import monitor
    snapshot = transport.balance()
    C.write_json(str(Path(C.LIVE) / "last_snapshot.json"),
        {"epoch": C.now().timestamp(), "holdings": snapshot["holdings"],
         "capture_metadata": snapshot["capture_metadata"]})
    monitor.check_once()


def run_execution(mp, clock):
    """Run prep → intraday → runner/MCP → guarded send → reconciliation.

    Returns serializable identifiers/summary plus ``runtime`` transport handles.
    Root should omit runtime when writing its public rehearsal report.
    """
    import common as C
    import data_prep as D
    import intraday as I
    import mcp_server as M
    import ai_judgment as J
    import judgment_coverage as JC
    import execution_trace as E
    import execution_reconcile as X
    import portfolio_capture as PC
    import monitor
    from datetime import timedelta

    broker, transport = _install_sources(mp)
    def acknowledge_fixture_order():
        # A response and its subsequent balance observations arrive after the
        # decision, even when no real exchange or wall-clock waiting is used.
        clock["now"] += timedelta(seconds=1)
    transport.order_acknowledged = acknowledge_fixture_order
    mp.setattr(monitor, "time", SimpleNamespace(time=lambda: C.now().timestamp(), sleep=lambda *_: None))
    clock["now"] = datetime.fromisoformat("2026-11-06T08:30:00+09:00")
    # Explicit initial fixture state, before any decision or capture. A missing
    # production file is never retrospectively declared empty by this helper.
    C.write_json(C.POSITIONS, {})
    Path(C.JOURNAL).parent.mkdir(parents=True, exist_ok=True)
    Path(C.JOURNAL).write_text("", encoding="utf-8")
    broker.account()
    prep = D.build()
    assert next(r for r in prep["candidates"] if r["code"] == MAIN)["ma20"] == 20000
    prep_codes = [r["code"] for r in prep["candidates"]]
    clock["now"] = datetime.fromisoformat("2026-11-06T10:30:00+09:00")
    session = PC.begin_session(clock["now"].isoformat(), "2026-11-06T10:32:00+09:00",
        interval_seconds=60, valuation_times=[clock["now"].isoformat(), "2026-11-06T10:32:00+09:00"],
        codes=[MAIN], source="synthetic_rehearsal")
    assert session.get("recording_status") == "recorded", session
    broker.account()
    _monitor_tick(transport)
    I.run()
    I.live_rsi()
    briefing = C.load_briefing()
    assert not briefing["intraday"].get("kept_prep"), briefing["intraday"]
    main = next(r for r in briefing["candidates"] if r["code"] == MAIN)
    assert main["price"] == 20400 and main["ma20"] == 20000
    runner = _module("rehearsal_actual_runner", AGENT / "run.py")
    out = {"main_code": MAIN, "judgment_date": "2026-11-06", "summary": {},
           "capture_session_id": session["session_id"]}

    class FixedCLI:
        returncode = 0
        pid = -1
        def __init__(self, cmd, **kwargs):
            self.env = kwargs["env"]
            out["run_attempt_id"] = self.env["RUN_ATTEMPT_ID"]
            assert (Path(C.WORK) / "ontology/attempts" / (out["run_attempt_id"] + ".json")).exists()
        def communicate(self, timeout):
            with mp.context() as child:
                for key in ("RUN_ID", "RUN_MODE", "RUN_ATTEMPT_ID"):
                    child.setenv(key, self.env[key])
                text = M.get_briefing()
                assert "+2.0%" in text and "20,400" in text and "20,000" in text
                out["briefing_text"] = text
                out["account_delivery"] = M.get_account()
                regime = M.set_regime("상승", "고정 모의 국면 분류")
                assert regime.get("regime") == "상승", regime
                bad = M.record_ai_judgments([_submission(text, key="wrong-sign", direction="buy")])[0]
                assert bad["status"] == "rejected" and any(e["code"] == "FACT_MISMATCH" for e in bad["errors"])
                good = M.record_ai_judgments([_submission(text, key="corrected-sign")])[0]
                assert good["status"] == "accepted", good
                out["judgment_id"] = good["judgment_id"]
                judgment = J.load_judgment(good["judgment_id"])
                out["application_ids"] = [p["application_id"] for p in judgment["premise_applications"]]
                out["delivery_id"] = judgment["delivery_ids"][0]
                plan = M.plan_order(MAIN, "매수", .7, judgment_id=good["judgment_id"])
                assert plan["qty"] >= 100, plan
                args = {"code": MAIN, "side": "매수", "qty": 100, "confidence": .7,
                        "bull": "검증용 추세 유지 가설", "bear": "기관 순매도", "decision": "모의 100주 요청",
                        "judgment_id": good["judgment_id"], "plan_id": plan["plan_id"],
                        "order_attempt_id": plan["order_attempt_id"]}
                hook = _hook(child, args, "partial-first")
                assert hook["permissionDecision"] == "allow", hook
                result = M.place_order(**hook["updatedInput"])
                assert result["ok"], result
                replay = M.place_order(**hook["updatedInput"])
                assert replay["replayed"] and len(transport.orders) == 1
                status = M.refresh_execution_status(plan["order_attempt_id"])
                assert status["confirmed_qty"] == 40 and status["unconfirmed_qty"] == 60, status
                out.update(plan_id=plan["plan_id"], order_attempt_id=plan["order_attempt_id"],
                           execution_status=status, order_result=result, rejected_submission=bad)
                # The omission is deliberate, and remains visible for other candidates.
                M.record_decision("000030", "관망", .4, "검증용", "근거 부족", "미제출 검증", confidence_for="매수")
            return json.dumps({"result": "상승 / 매수 1건 / 매도 0건 / 관망 1건", "total_cost_usd": 0,
                               "usage": {"input_tokens": 0, "output_tokens": 0}}), ""

    with mp.context() as external:
        external.setattr(runner.subprocess, "Popen", FixedCLI)
        assert runner.claude("decide", runner.PROMPTS["decide"].format(time="10:30")), C.read_jsonl(C.RUNS)[-1].get("note")
    mp.setenv("RUN_ATTEMPT_ID", out["run_attempt_id"])
    mp.setenv("RUN_ID", "20261106-1030-decide")
    mp.setenv("RUN_MODE", "decide")
    out["coverage"] = JC.get_coverage(out["run_attempt_id"])
    assert out["coverage"]["counts"]["linked"] >= 1
    assert out["coverage"]["counts"]["not_submitted"] >= 1
    out["summary"].update(prep_candidates=prep_codes, intraday_candidates=[r["code"] for r in briefing["candidates"]],
                           main_price=main["price"], ma20=main["ma20"], core_runner="run.claude",
                           partial_fill={"requested": 100, "confirmed": 40, "unconfirmed": 60},
                           corrected_fact_rejected_first=True, coverage=out["coverage"]["counts"])
    out["period_start"] = "2026-11-06T10:30:00+09:00"
    out["period_end"] = "2026-11-06T10:32:00+09:00"

    # The future tape is collected when the synthetic clock reaches each time.
    # Cached values retain their original timestamps; no history is backfilled.
    clock["now"] = datetime.fromisoformat("2026-11-06T10:30:31+09:00")
    transport.prices[MAIN] = 20500
    first_future = broker.quote_info(MAIN)
    assert first_future.get("available_at", "").startswith("2026-11-06T10:30:31"), first_future
    for timestamp in ("2026-11-06T10:31:00+09:00", "2026-11-06T10:32:00+09:00"):
        clock["now"] = datetime.fromisoformat(timestamp)
        broker.quote_info(MAIN)
        _monitor_tick(transport)
    end = PC.finish_session(session["session_id"])
    assert end.get("recording_status") == "recorded", end
    # Catch a synthetic-clock collision before the more expensive research
    # phases. Post-response balance prices must not become the decision quote.
    import snapshot_store
    from simulation_market import FrozenMarket
    plan_record = E.load_plan(out['plan_id'])
    decision_at = plan_record['inputs']['sizing']['runtime_context']['at']
    selected_quote_id = plan_record['inputs']['quote']['observation_id']
    tape = []
    for path in (Path(C.WORK) / 'ontology/executions').glob('*/portfolio_market/*.json'):
        record = snapshot_store.load(path)
        observation = record.get('observation') or {}
        tape.append({**observation, 'observation_id': record['event_id']})
    frozen_market = FrozenMarket(tape, end=out['period_end'], max_age_seconds=120)
    assert frozen_market.quote(MAIN, decision_at)['observation_id'] == selected_quote_id
    eligible = [q for q in tape if q['code'] == MAIN
                and datetime.fromisoformat(q['available_at']) <= datetime.fromisoformat(decision_at)]
    assert [q['observation_id'] for q in eligible] == [selected_quote_id]
    out["summary"]["portfolio_capture"] = {"session_id": session["session_id"],
        "start": out["period_start"], "end": out["period_end"],
        "monitor_ticks": ["2026-11-06T10:30:00+09:00", "2026-11-06T10:31:00+09:00", "2026-11-06T10:32:00+09:00"],
        "source": "explicit_synthetic_provider_metadata_at_collection", "backfilled": False,
        "first_future_quote_at": first_future["available_at"],
        "decision_quote_unique_before_acknowledgment": True,
        "completion_status": "reader_must_verify_all_original_ticks"}

    # Fault drills use a later target/time, outside the happy-path validation window.
    clock["now"] += timedelta(minutes=15)
    fault_attempt = JC.start_attempt("rehearsal-transport-faults", "decide", briefing, broker.account())
    mp.setenv("RUN_ATTEMPT_ID", fault_attempt["run_attempt_id"])
    mp.setenv("RUN_ID", "rehearsal-transport-faults")
    transport.next_mode = "response_lost"
    aid = E.reserve_attempt(E.account_scope(broker))["order_attempt_id"]
    before = len(transport.orders)
    first = broker.order(MAIN, NAMES[MAIN], "매수", 1, order_attempt_id=aid)
    second = broker.order(MAIN, NAMES[MAIN], "매수", 1, order_attempt_id=aid)
    assert first["submission_status"] == "submission_unknown" and second["replayed"]
    assert len(transport.orders) - before == 1
    lost_status = X.refresh_execution_status(aid, broker)
    assert lost_status["query_reason"] == "order_number_unknown", lost_status
    out["summary"]["response_loss"] = {"order_attempt_id": aid, "dispatches": 1,
                                      "submission_status": first["submission_status"], "query": lost_status}

    transport.next_mode = "accepted"
    aid2 = E.reserve_attempt(E.account_scope(broker))["order_attempt_id"]
    before = len(transport.orders)
    with mp.context() as failure:
        def lost_receipt(*args, **kwargs):
            raise OSError("synthetic result-store failure after broker response")
        failure.setattr(E, "_store_result", lost_receipt)
        recorded = broker.order(MAIN, NAMES[MAIN], "매수", 1, order_attempt_id=aid2)
    retry = broker.order(MAIN, NAMES[MAIN], "매수", 1, order_attempt_id=aid2)
    assert len(transport.orders) - before == 1 and retry["replayed"]
    assert recorded["recording_status"] == "recording_error", recorded
    out["summary"]["receipt_loss"] = {"order_attempt_id": aid2, "dispatches": 1,
                                     "recording_status": recorded["recording_status"]}
    JC.finish_attempt(fault_attempt["run_attempt_id"], "normal")
    out["fault_attempt_id"] = fault_attempt["run_attempt_id"]
    out["runtime"] = {"broker": broker, "transport": transport}
    # This convenience index is not an additional ledger. The full source
    # records already live under ontology/, and are reachable through these IDs.
    index_keys = ("main_code", "judgment_date", "run_attempt_id", "judgment_id", "application_ids",
                  "delivery_id", "plan_id", "order_attempt_id", "fault_attempt_id", "summary",
                  "period_start", "period_end", "capture_session_id")
    C.write_json(str(Path(C.WORK) / "rehearsal_execution.json"),
                 {k: out[k] for k in index_keys if k in out})
    return out
