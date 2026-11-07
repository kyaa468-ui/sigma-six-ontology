"""Local-only coverage tests: missing submissions are part of the denominator."""
import json
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import common as C
import judgment_coverage as V

NOW = datetime.fromisoformat("2026-11-06T10:01:00+09:00")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Coverage must never access a network or broker")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(C, "WORK", str(tmp_path))
    monkeypatch.setattr(C, "now", lambda: NOW)
    monkeypatch.delenv("RUN_ATTEMPT_ID", raising=False)
    monkeypatch.setenv("RUN_ID", "20261106-1001-decide")
    monkeypatch.setenv("RUN_MODE", "decide")
    judgments = {}
    monkeypatch.setattr(V, "_load_judgment", lambda identifier: judgments.get(identifier))
    yield {"path": tmp_path, "judgments": judgments, "monkeypatch": monkeypatch}


def start(fix, candidates=("A",), holdings=(), kind="decide", complete=True):
    briefing = {"candidates": [{"code": code} for code in candidates], "holdings": []}
    account = {"holdings": [{"code": code, "qty": 2} for code in holdings]} if complete else None
    row = V.start_attempt("20261106-1001-decide", kind, briefing, account)
    fix["monkeypatch"].setenv("RUN_ATTEMPT_ID", row["run_attempt_id"])
    return row["run_attempt_id"]


def submit(fix, attempt, code="A", side="매수", raw=0.7, status="accepted", action="매수", **extra):
    identifier = "judgment-" + uuid.uuid4().hex
    row = {"run_attempt_id": attempt, "run_id": "20261106-1001-decide", "code": code,
           "confidence_for": side, "confidence_raw": raw, "recommended_action": action,
           "judgment_id": identifier, "created_at": NOW.isoformat(), **extra}
    if status == "accepted":
        fix["judgments"][identifier] = row
    V._write("submissions", uuid.uuid4().hex, {**row, "status": status,
                                              "errors": [] if status == "accepted" else [{"code": "FACT_MISMATCH"}]})
    return identifier


def invoke(code="A", side="매수", raw=0.7, judgment_id=None, stage="plan_order", **kwargs):
    event = V.enter(stage, code, side, raw, judgment_id=judgment_id, **kwargs)
    assert event
    V.finish_event(event, outcome={"qty": 0}, boost={"confidence_raw": raw, "result": raw})
    return event


def statuses(report):
    return {(row["code"], row["confidence_for"]): row["status"] for row in report["targets"]}


def test_initial_denominator_does_not_depend_on_submission_or_regime(isolated):
    attempt = start(isolated, candidates=("A", "B"), holdings=("B", "C"))
    report = V.get_coverage(attempt)
    assert report["expected_count"] == 3
    assert statuses(report) == {("A", "매수"): "not_submitted", ("B", "매도"): "not_submitted",
                                ("C", "매도"): "not_submitted"}
    assert report["linked_rate"] == 0
    assert report["run_status"] == "unfinished"
    # No structured tool, even though a legacy order/decision was executed.
    invoke(stage="place_order")
    invoke(stage="record_decision", action="관망")
    V.finish_attempt(attempt, "normal")
    assert V.get_coverage(attempt)["counts"]["not_submitted"] == 3
    assert V.get_coverage(attempt)["run_status"] == "normal"


def test_rejection_then_link_retains_history_and_original_manifest(isolated):
    attempt = start(isolated)
    manifest_path = isolated["path"] / "ontology/attempts" / (attempt + ".json")
    before = manifest_path.read_bytes()
    submit(isolated, attempt, status="rejected")
    assert statuses(V.get_coverage(attempt))[("A", "매수")] == "rejected"
    accepted = submit(isolated, attempt)
    invoke(judgment_id=accepted)
    invoke(stage="hook:place_order", judgment_id=accepted)
    invoke(stage="place_order", judgment_id=accepted)
    V.finish_attempt(attempt, "normal")
    report = V.get_coverage(attempt)
    assert report["expected_count"] == 1
    assert report["counts"]["linked"] == 1
    assert len(report["rejection_history"]) == 1
    assert report["submission_attempts"] == 2
    assert report["linked_rate"] == 1
    assert manifest_path.read_bytes() == before
    assert len(report["targets"][0]["calls"]) == 3


@pytest.mark.parametrize("mode,reason", [("missing", "missing_judgment_id"),
                                        ("wrong", "unknown_judgment_id"),
                                        ("raw", "confidence_raw_mismatch"),
                                        ("future", "judgment_not_available_at_call"),
                                        ("code", "code_mismatch"),
                                        ("attempt", "run_attempt_id_mismatch"),
                                        ("side", "confidence_for_mismatch")])
def test_explicit_link_checks(isolated, mode, reason):
    attempt = start(isolated)
    identifier = submit(isolated, attempt)
    passed = identifier
    raw = 0.7
    if mode == "missing":
        passed = None
    elif mode == "wrong":
        passed = "judgment-missing"
    elif mode == "raw":
        raw = 0.71
    elif mode == "future":
        isolated["judgments"][identifier]["created_at"] = "2026-11-06T10:02:00+09:00"
    elif mode == "code":
        isolated["judgments"][identifier]["code"] = "B"
    elif mode == "attempt":
        isolated["judgments"][identifier]["run_attempt_id"] = "another-attempt"
    elif mode == "side":
        isolated["judgments"][identifier]["confidence_for"] = "매도"
    invoke(judgment_id=passed, raw=raw)
    row = V.get_coverage(attempt)["targets"][0]
    assert row["status"] == "accepted_unlinked"
    assert reason in row["status_reasons"]


def test_later_submission_does_not_repair_previously_executed_call(isolated):
    attempt = start(isolated)
    # Even an ID supplied before it exists is frozen as unknown at call entry.
    invoke(judgment_id="judgment-later")
    identifier = submit(isolated, attempt)
    isolated["judgments"]["judgment-later"] = isolated["judgments"][identifier]
    report = V.get_coverage(attempt)
    assert report["counts"]["accepted_unlinked"] == 1
    assert report["targets"][0]["calls"][0]["link"]["reason"] == "unknown_judgment_id"


def test_partial_linkage_stays_unlinked(isolated):
    attempt = start(isolated)
    identifier = submit(isolated, attempt)
    invoke(judgment_id=identifier)
    invoke(stage="place_order")
    row = V.get_coverage(attempt)["targets"][0]
    assert row["status"] == "accepted_unlinked"
    assert "partial_linkage" in row["status_reasons"]


def test_hold_and_add_on_buy_are_separate_and_legacy_hold_is_unresolved(isolated):
    attempt = start(isolated, candidates=("A",), holdings=("A",))
    invoke(side="매도", stage="record_decision", action="보유")
    report = V.get_coverage(attempt)
    assert statuses(report) == {("A", "매도"): "not_submitted", ("A", "매수"): "not_submitted"}
    sell = next(row for row in report["targets"] if row["confidence_for"] == "매도")
    assert "direction_unresolved" in sell["status_reasons"]


def test_ambiguous_hold_has_visible_unlinked_event_without_guessing_score_direction(isolated):
    attempt = start(isolated, candidates=(), holdings=("A",))
    identifier = submit(isolated, attempt, side="매도", action="보유")
    invoke(side=None, stage="record_decision", action="보유", judgment_id=identifier)
    report = V.get_coverage(attempt)
    sell = next(row for row in report["targets"] if row["confidence_for"] == "매도")
    assert sell["status"] == "accepted_unlinked"
    assert "direction_unresolved" in sell["status_reasons"]
    assert sell["calls"][0]["confidence_for"] is None
    assert ("A", "매수") in statuses(report)


@pytest.mark.parametrize("kind", ["review", "tune"])
def test_non_judgment_modes_are_not_in_coverage_denominator(isolated, kind):
    attempt = start(isolated, candidates=("A",), holdings=("B",), kind=kind, complete=False)
    invoke()
    report = V.get_coverage(attempt)
    assert report["expected_count"] == 0
    assert report["rate_status"] == "not_applicable"
    assert not report["target_set_incomplete"]


def test_atomic_archive_never_replaces_existing_record(isolated):
    V._write("coverage_events", "fixed-id", {"value": 1})
    with pytest.raises(FileExistsError):
        V._write("coverage_events", "fixed-id", {"value": 2})
    path = isolated["path"] / "ontology/coverage_events/fixed-id.json"
    assert json.loads(path.read_text())["value"] == 1
    assert not list(path.parent.glob("*.tmp"))


def test_accepted_non_sell_judgment_adds_buy_before_execution(isolated):
    attempt = start(isolated, candidates=(), holdings=("A",))
    identifier = submit(isolated, attempt, side="매도", action="보유")
    assert statuses(V.get_coverage(attempt)) == {("A", "매도"): "accepted_unlinked", ("A", "매수"): "not_submitted"}
    invoke(side="매도", judgment_id=identifier, stage="record_decision", action="보유")
    buyer = submit(isolated, attempt, action="보유")
    invoke(judgment_id=buyer, stage="record_decision", action="보유")
    assert V.get_coverage(attempt)["counts"]["linked"] == 2


def test_target_union_never_removes_sold_holdings(isolated):
    attempt = start(isolated, candidates=("A",), holdings=("B",))
    V.observe_targets(briefing={"candidates": [{"code": "B"}, {"code": "C"}]}, account={"holdings": []})
    report = V.get_coverage(attempt)
    assert set(statuses(report)) == {("A", "매수"), ("B", "매도"), ("C", "매수")}
    invoke(code="D")  # Later tool identifies an unlisted target.
    assert ("D", "매수") in statuses(V.get_coverage(attempt))


def test_rule_exit_is_separate_and_flag_does_not_hide_ordinary_target(isolated):
    attempt = start(isolated, candidates=(), holdings=("A",))
    invoke(side="매도", rule_exit=True)
    invoke(side="매도", rule_exit=True, stage="place_order")
    report = V.get_coverage(attempt)
    assert report["counts"]["not_submitted"] == 1
    assert report["rule_exit"]["target_count"] == 1
    assert report["rule_exit"]["call_count"] == 2
    system = start(isolated, candidates=("A",), holdings=("A",), kind="exit")
    invoke(side="매도", rule_exit=True)
    report = V.get_coverage(system)
    assert report["expected_count"] == 0
    assert report["linked_rate"] is None
    assert report["rate_status"] == "not_applicable"
    assert report["rule_exit"]["target_count"] == 1


def test_incomplete_is_not_empty_success_and_same_minute_attempts_differ(isolated):
    first = start(isolated, candidates=(), complete=False)
    report = V.get_coverage(first)
    assert report["target_set_incomplete"]
    assert report["linked_rate"] is None
    assert report["rate_status"] == "target_set_incomplete"
    second = start(isolated, candidates=())
    assert first != second
    assert V.get_coverage(second)["rate_status"] == "not_applicable"
    isolated["monkeypatch"].delenv("RUN_ATTEMPT_ID")
    assert V.get_coverage()["run_attempt_id"] == second


@pytest.mark.parametrize("terminal", ["normal", "error", "timeout"])
def test_terminal_status_and_crash_reconstruction(isolated, terminal):
    attempt = start(isolated)
    assert V.get_coverage(attempt)["run_status"] == "unfinished"
    V.finish_attempt(attempt, terminal)
    assert V.get_coverage(attempt)["run_status"] == terminal


def test_call_result_failure_cannot_claim_full_coverage(isolated):
    attempt = start(isolated)
    identifier = submit(isolated, attempt)
    V.enter("place_order", "A", "매수", 0.7, judgment_id=identifier)
    assert V.get_coverage(attempt)["counts"]["accepted_unlinked"] == 1
    V.finish_attempt(attempt, "normal")
    report = V.get_coverage(attempt)
    assert report["counts"]["recording_error"] == 1
    assert report["linked_rate"] is None


def test_write_failure_is_fail_open_and_visible_cross_process(isolated):
    attempt = start(isolated)
    identifier = submit(isolated, attempt)
    original = V._write

    def broken(kind, *args):
        if kind == "coverage_events":
            raise PermissionError("test directory denied")
        return original(kind, *args)

    isolated["monkeypatch"].setattr(V, "_write", broken)
    assert V.enter("place_order", "A", "매수", 0.7, judgment_id=identifier) is None
    # Simulate querying from another process after Hook exits.
    V._ERRORS.clear()
    report = V.get_coverage(attempt)
    assert report["counts"]["recording_error"] == 1
    assert report["linked_rate"] is None
    assert report["recording_errors"][0]["operation"] == "enter"


def test_failed_start_or_missing_manifest_is_explicit(isolated):
    isolated["monkeypatch"].setattr(V, "_write", lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")))
    manifest = V.start_attempt("run", "decide", {"candidates": []}, {"holdings": []})
    assert manifest["recording_status"] == "recording_error"
    report = V.get_coverage(manifest["run_attempt_id"])
    assert report["expected_count"] is None
    assert report["recording_status"] == "recording_error"
    assert report["linked_rate"] is None


def test_public_error_report_prevents_false_success_after_linked_call(isolated):
    attempt = start(isolated)
    identifier = submit(isolated, attempt)
    invoke(judgment_id=identifier)
    assert V.get_coverage(attempt)["linked_rate"] == 1
    status = V.recording_error("submission_archive", OSError("fixture failure"))
    assert status["recording_status"] == "recording_error"
    V._ERRORS.clear()  # Reporter must be visible from another process too.
    report = V.get_coverage(attempt)
    assert report["linked_rate"] is None
    assert report["counts"]["recording_error"] == 1


def test_public_error_report_itself_is_fail_open(isolated):
    attempt = start(isolated)
    isolated["monkeypatch"].setattr(V, "_error", lambda *a: (_ for _ in ()).throw(OSError("stderr closed")))
    status = V.recording_error("hook", RuntimeError("original"))
    assert status["recording_status"] == "recording_error"
    assert V.get_coverage(attempt)["recording_status"] == "recording_error"


def test_corrupt_record_is_never_silently_skipped(isolated):
    attempt = start(isolated)
    directory = isolated["path"] / "ontology/coverage_events" / attempt
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text("{invalid", encoding="utf-8")
    report = V.get_coverage(attempt)
    assert report["counts"]["recording_error"] == 1
    assert report["recording_status"] == "recording_error"


def test_foreign_or_unscoped_damage_does_not_poison_this_run(isolated):
    attempt = start(isolated)
    foreign = isolated["path"] / "ontology/coverage_events/another-attempt"
    foreign.mkdir(parents=True)
    (foreign / "bad.json").write_text("{invalid", encoding="utf-8")
    report = V.get_coverage(attempt)
    assert report["counts"]["not_submitted"] == 1
    assert report["recording_status"] == "recorded"
    legacy = isolated["path"] / "ontology/submissions"
    legacy.mkdir(parents=True)
    (legacy / "bad.json").write_text("{invalid", encoding="utf-8")
    report = V.get_coverage(attempt)
    assert report["counts"]["not_submitted"] == 1
    assert report["global_storage_errors"]


def test_inactive_calls_preserve_legacy_operation(isolated):
    assert V.enter("plan_order", "A", "매수", 0.7) is None
    assert V.finish_event(None)["recording_status"] == "not_active"
    assert V.observe_targets()["recording_status"] == "not_active"
    assert V.get_coverage()["recording_status"] == "not_found"
