"""Case-review boundaries and tuning preflight through real MCP entry points."""


def read_stored(path):
    """Logical fixture reads support inline v1 and explicit committed v2 records."""
    import record_store
    from pathlib import Path
    path = Path(path)
    return record_store.load_input(path) if path.name in ('briefing.json','prep_rows.json') else record_store.load(path)

import contextlib
import importlib.util
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_ontology import C, M, P, ROOT, NOW, clone, isolated
import judgment_context
import case_store


def files():
    return {str(p.relative_to(C.WORK)): p.read_bytes()
            for p in Path(C.WORK).rglob('*') if p.is_file()}


@pytest.mark.parametrize('mode', ['decide', 'view', 'exit', 'review', 'tune', ''])
@pytest.mark.parametrize('name,args', [
    ('get_case_review', ('review-one',)),
    ('get_review_cases', ('review-one',)),
    ('record_case_review', ('review-one', 'request-one', [], [])),
])
def test_case_review_tools_not_visible_to_trading(monkeypatch, mode, name, args):
    monkeypatch.setenv('RUN_MODE', mode)
    monkeypatch.setenv('CASE_REVIEW_ID', 'review-one')
    before = files()
    result = getattr(M, name)(*args)
    assert result['error_code'] == 'CASE_REVIEW_MODE_REQUIRED'
    assert files() == before


@pytest.mark.parametrize('assigned,requested', [('', 'review-one'), ('review-one', 'review-two')])
@pytest.mark.parametrize('name,args', [
    ('get_case_review', ()), ('get_review_cases', ()),
    ('record_case_review', ('request-one', [], [])),
])
def test_case_review_tools_require_assigned_bundle(monkeypatch, assigned, requested, name, args):
    monkeypatch.setenv('RUN_MODE', 'case_review')
    monkeypatch.setenv('CASE_REVIEW_ID', assigned)
    before = files()
    result = getattr(M, name)(requested, *args)
    assert result['error_code'] == 'CASE_REVIEW_SCOPE_MISMATCH'
    assert files() == before


def test_case_review_mcp_forwards_only_assigned_frozen_arguments(monkeypatch):
    calls = []
    def helper(name):
        def call(*args):
            calls.append((name, args))
            return {'status': 'frozen', 'review_id': args[0]}
        return call
    module = SimpleNamespace(**{name: helper(name) for name in M.CASE_REVIEW_TOOLS})
    monkeypatch.setitem(sys.modules, 'case_review', module)
    monkeypatch.setenv('RUN_MODE', 'case_review')
    monkeypatch.setenv('CASE_REVIEW_ID', 'review-one')
    findings = [{'reference': 'count', 'value': 2}]
    hypotheses = [{'text': '사후 가설'}]
    assert M.get_case_review('review-one')['status'] == 'frozen'
    assert M.get_review_cases('review-one', 10, 20)['status'] == 'frozen'
    assert M.record_case_review('review-one', 'request-one', findings, hypotheses)['status'] == 'frozen'
    assert calls == [('get_case_review', ('review-one',)),
                     ('get_review_cases', ('review-one', 10, 20)),
                     ('record_case_review', ('review-one', 'request-one', findings, hypotheses))]


def frozen_review(monkeypatch):
    identifier = 'case-review-local-fixture'
    job = {'review_id': identifier, 'market_as_of': '2026-11-05',
           'knowledge_as_of': NOW.isoformat(), 'started_at': NOW.isoformat()}
    metrics = [
        {'metric_id': 'metric-rate', 'path': '/comparison/refutation_rate', 'value': .25,
         'display_value': .25, 'unit': 'ratio', 'precision': 6, 'status': 'computed'},
        {'metric_id': 'metric-coefficient', 'path': '/statistics/models/0/coefficient', 'value': -.3,
         'display_value': -.3, 'unit': 'number', 'precision': 6, 'status': 'computed'},
        {'metric_id': 'metric-missing', 'path': '/statistics/models/0/missing', 'value': None,
         'display_value': None, 'unit': 'text', 'precision': None, 'status': 'unknown'},
        {'metric_id': 'metric-status', 'path': '/statistics/status', 'value': 'insufficient_sample',
         'display_value': 'insufficient_sample', 'unit': 'text', 'precision': None, 'status': 'computed'},
    ]
    case_store.archive('jobs', identifier, job)
    case_store.archive('results', identifier, {**job, 'expected_count': 1, 'case_ids': ['p1'],
                       'analysis_status': 'complete', 'recording_status': 'recorded', 'metric_catalog': metrics})
    case_store.archive('datasets', identifier, {'cases': [{'application_id': 'p1', 'status': 'refuted'}],
                                              'target_set_incomplete': False})
    monkeypatch.setenv('RUN_MODE', 'case_review')
    monkeypatch.setenv('CASE_REVIEW_ID', identifier)
    return identifier


@pytest.mark.parametrize('finding,expected', [
    ({'id': 'f1', 'metric_id': 'metric-rate', 'op': 'value', 'value': 75, 'unit': '%'}, 'FACT_MISMATCH'),
    ({'id': 'f1', 'metric_id': 'metric-rate', 'op': 'value', 'value': .25, 'unit': 'KRW'}, 'UNIT_MISMATCH'),
    ({'id': 'f1', 'metric_id': 'metric-coefficient', 'op': 'direction', 'direction': 'positive'}, 'FACT_MISMATCH'),
    ({'id': 'f1', 'metric_id': 'metric-missing', 'op': 'value', 'value': 0}, 'UNKNOWN_AS_ZERO'),
    ({'id': 'f1', 'metric_id': 'metric-status', 'op': 'value', 'value': 'fitted'}, 'FACT_MISMATCH'),
    ({'id': 'f1', 'metric_id': 'metric-rate', 'text': '반증률 75%'}, 'SCHEMA_ERROR'),
])
def test_actual_review_tools_reject_valid_id_with_wrong_fact(monkeypatch, finding, expected):
    identifier = frozen_review(monkeypatch)
    assert M.get_case_review(identifier)['ai_status'] == 'not_submitted'
    rejected = M.record_case_review(identifier, 'invalid', [finding], [])
    assert rejected['status'] == 'rejected'
    assert rejected['errors'][0]['code'] == expected
    assert M.get_case_review(identifier)['ai_status'] == 'rejected'


def test_actual_review_tools_omission_corrected_facts_hypotheses_and_retry(monkeypatch):
    identifier = frozen_review(monkeypatch)
    before = files()
    omitted = M.get_case_review(identifier)
    assert omitted['ai_status'] == 'not_submitted' and omitted['run_status'] == 'awaiting_ai'
    assert M.get_review_cases(identifier)['cases'][0]['application_id'] == 'p1'
    assert files() == before
    wrong = {'id': 'f1', 'metric_id': 'metric-rate', 'op': 'value', 'value': 75, 'unit': '%'}
    assert M.record_case_review(identifier, 'wrong', [wrong], [])['status'] == 'rejected'
    fact = {**wrong, 'value': 25}
    hypothesis = {'id': 'h1', 'text': '기관 매도 압력이 차이의 원인일 수 있다',
                  'finding_ids': ['f1'], 'case_ids': ['p1']}
    accepted = M.record_case_review(identifier, 'corrected', [fact], [hypothesis])
    assert accepted['status'] == 'accepted'
    assert accepted['findings'][0]['fact_validation'] == 'passed'
    assert accepted['findings'][0]['server_summary'].endswith('검증된 계산값, 인과 판단 아님')
    assert accepted['hypotheses'][0]['hypothesis_status'] == 'submitted_unverified'
    assert M.record_case_review(identifier, 'corrected', [fact], [hypothesis])['replayed'] is True
    assert M.record_case_review(identifier, 'corrected', [wrong], [hypothesis])['errors'][0]['code'] == 'IDEMPOTENCY_CONFLICT'
    final = M.get_case_review(identifier)
    assert final['ai_status'] == 'accepted' and final['run_status'] == 'complete'
    assert sorted(x['status'] for x in final['ai_submissions']) == ['accepted', 'rejected']
    assert not Path(C.JOURNAL).exists() and not Path(M.tuning.LESSONS).exists()


LEGACY_CALLS = [
    ('get_briefing', ()), ('get_account', ()), ('set_regime', ('상승', 'fixture')),
    ('plan_order', ('000001', '매수', .7)),
    ('place_order', ('000001', '매도', 1, .7, 'b', 's', 'fixture')),
    ('record_decision', ('000001', '관망', .7, 'b', 's', 'fixture')),
    ('record_ai_judgments', ([],)), ('get_ai_judgments', ()),
    ('record_external_evidence', ([],)), ('get_judgment_coverage', ()),
    ('get_execution_trace', ()), ('refresh_execution_status', ('order-one',)),
    ('get_close_evaluations', ()), ('get_premise_evaluations', ()),
    ('submit_review', (1, 'fixture', 'fixture', 'fixture')),
    ('get_tuning_context', ()), ('apply_tuning', ([], 'fixture', ['must not save'])),
]


@pytest.mark.parametrize('name,args', LEGACY_CALLS)
def test_case_review_denies_every_other_tool_before_broker_or_audit(monkeypatch, name, args):
    monkeypatch.setenv('RUN_MODE', 'case_review')
    monkeypatch.setenv('CASE_REVIEW_ID', 'review-one')
    called = []
    monkeypatch.setattr(M, 'brk', lambda: called.append('broker'))
    monkeypatch.setattr(M.JC, 'enter', lambda *a, **k: called.append('coverage'))
    before = files()
    result = getattr(M, name)(*args)
    assert result['error_code'] == 'CASE_REVIEW_TOOL_FORBIDDEN'
    assert called == []
    assert files() == before


@pytest.mark.parametrize('mode', ['review', 'tune'])
@pytest.mark.parametrize('side,rule_exit', [('매수', False), ('매도', False), ('매도', True)])
def test_existing_nontrading_modes_cannot_plan_or_send_orders(monkeypatch, mode, side, rule_exit):
    monkeypatch.setenv('RUN_MODE', mode)
    called = []
    monkeypatch.setattr(M, 'brk', lambda: called.append('broker'))
    monkeypatch.setattr(M.JC, 'enter', lambda *a, **k: called.append('coverage'))
    before = files()
    plan = M.plan_order('000001', side, .7, rule_exit=rule_exit)
    order = M.place_order('000001', side, 1, .7, 'b', 's', 'fixture', rule_exit=rule_exit)
    assert plan['error_code'] == order['error_code'] == 'NON_TRADING_MODE'
    assert called == [] and files() == before


@pytest.mark.parametrize('mode', ['case_review', 'review', 'tune'])
def test_shared_risk_guard_runs_before_market_and_confidence_inputs(monkeypatch, mode):
    monkeypatch.setenv('RUN_MODE', mode)
    def forbidden():
        raise AssertionError('nontrading guard must run before settings access')
    monkeypatch.setattr(C, 'params', forbidden)
    audit = {}
    violations = M.risk.check({}, {}, 0, audit=audit)
    assert violations == [('실행 종류', '회고·튜닝 실행에서는 주문하지 않음')]
    assert audit['execution_mode_guard']['status'] == 'failed'


@pytest.mark.parametrize('mode', ['case_review', 'review', 'tune'])
def test_hook_denies_nontrading_before_retry_reservation_or_live_reads(monkeypatch, mode):
    spec = importlib.util.spec_from_file_location('case_review_risk_hook', ROOT / '.claude/hooks/risk_check.py')
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setenv('RUN_MODE', mode)
    calls = []
    monkeypatch.setattr(C, 'load_env', lambda: calls.append('env'))
    monkeypatch.setattr(M.JC, 'enter', lambda *a, **k: calls.append('coverage'))
    monkeypatch.setattr(M.E, 'probe_attempt', lambda *a, **k: calls.append('retry_probe'))
    monkeypatch.setattr(M.E, 'reserve_attempt', lambda *a, **k: calls.append('reservation'))
    monkeypatch.setattr(M.B, 'cached_quote', lambda *a, **k: calls.append('quote'))
    payload = {'tool_name': 'mcp__kiwoom__place_order', 'tool_input': {
        'code': '000001', 'side': '매도', 'qty': 1, 'confidence': .7,
        'rule_exit': True, 'order_attempt_id': 'already-claimed-fixture'}}
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    before = files()
    output = io.StringIO()
    with contextlib.redirect_stdout(output), pytest.raises(SystemExit):
        hook.main()
    assert json.loads(output.getvalue())['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert calls == [] and files() == before


def tuning_files(monkeypatch):
    target = Path(C.WORK) / 'params.json'
    target.write_text(json.dumps(P, ensure_ascii=False))
    monkeypatch.setattr(M.tuning, 'PARAMS_PATH', str(target))
    C.write_json(M.tuning.LESSONS, {'time': 'past', 'lessons': ['unchanged']})
    monkeypatch.setenv('RUN_MODE', 'tune')
    monkeypatch.setenv('RUN_ID', 'test-tune')
    return target


@pytest.mark.parametrize('denied', ['wrong_mode', 'weekend', 'duplicate'])
def test_denied_tuning_preserves_lessons_journal_and_params(monkeypatch, denied):
    tuning_files(monkeypatch)
    if denied == 'wrong_mode':
        monkeypatch.setenv('RUN_MODE', 'review')
    elif denied == 'weekend':
        monkeypatch.setattr(C, 'now', lambda: NOW + timedelta(days=(5 - NOW.weekday()) % 7))
    else:
        C.journal('tune', summary='first', applied=[], rejected=[])
    before = files()
    result = M.apply_tuning([], 'must reject', ['must not replace previous lessons'])
    assert result['ok'] is False
    assert files() == before


def test_valid_tuning_retains_partial_numeric_policy_and_saves_lessons(monkeypatch):
    target = tuning_files(monkeypatch)
    changes = [{'key': 'decision.buy_min_confidence', 'to': P['decision']['buy_min_confidence'] + .01,
                'reason': 'allowed fixture'},
               {'key': 'risk_limits.max_positions', 'to': 999, 'reason': 'forbidden fixture'}]
    expected_ok, expected_bad = M.tuning.check(changes, clone(P), M.tuning.rules(), [])
    result = M.apply_tuning(changes, 'local fixture', ['new lesson'])
    assert result['ok'] is True
    assert result['applied'] == expected_ok and result['rejected'] == expected_bad
    assert read_stored(target)['decision']['buy_min_confidence'] == expected_ok[0]['to']
    assert C.read_json(M.tuning.LESSONS)['lessons'] == ['new lesson']
    assert [x['kind'] for x in C.read_jsonl(C.JOURNAL)] == ['tune', 'lessons']
    before = files()
    assert M.apply_tuning([], 'duplicate', ['replacement'])['ok'] is False
    assert files() == before


def test_parallel_duplicate_tuning_has_one_authorized_submission(monkeypatch):
    tuning_files(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        # Call the shared writer directly: it also guards non-MCP callers.
        results = list(pool.map(lambda n: M.tuning.apply([], str(n), [str(n)]), [1, 2]))
    assert sorted(x['ok'] for x in results) == [False, True]
    assert [x['kind'] for x in C.read_jsonl(C.JOURNAL)] == ['tune', 'lessons']


def test_registered_regime_id_links_original_row_and_frozen_classification(monkeypatch):
    monkeypatch.setenv('RUN_MODE', 'decide')
    result = M.set_regime('횡보', 'local classification')
    assert result['regime'] == '횡보' and result['regime_recording_status'] == 'recorded'
    latest = C.latest_regime()
    assert latest['regime_event_id'] == result['regime_event_id']
    archive = case_store.load('regimes', result['regime_event_id'])
    assert archive['journal_snapshot'] == latest
    snapshot = judgment_context.snapshot_regime()
    assert snapshot['classification_status'] == 'ai_classification_unverified'
    assert snapshot['value'] == '횡보'
    original = clone(snapshot)
    second = M.set_regime('하락', 'next classification')
    assert second['regime_event_id'] != result['regime_event_id']
    assert case_store.load('regimes', result['regime_event_id']) == original['event_snapshot']


def test_regime_archive_failure_preserves_original_policy_and_coverage(monkeypatch):
    monkeypatch.setenv('RUN_MODE', 'decide')
    def failed(row):
        raise OSError('local fixture storage failure')
    monkeypatch.setattr(judgment_context, 'record_regime', failed)
    audit = []
    monkeypatch.setattr(M.JC, 'recording_error', lambda *a, **k: audit.append((a, k)))
    result = M.set_regime('하락', 'still the registered regime')
    assert result['regime'] == '하락' and result['regime_recording_status'] == 'recording_error'
    assert C.latest_regime()['regime'] == '하락'
    assert judgment_context.snapshot_regime()['status'] == 'unknown'
    assert audit == []


def test_prediction_path_and_case_review_agent_capabilities_are_isolated():
    permissions = read_stored(ROOT / '.claude/settings.json')['permissions']
    assert 'Read(./work/ontology/case_review/**)' in permissions['deny']
    prompt = (ROOT / '.claude/agents/case_review.md').read_text()
    tools_line = next(x for x in prompt.splitlines() if x.startswith('tools:'))
    assert {s.strip() for s in tools_line.removeprefix('tools:').split(',')} == {
        'mcp__kiwoom__' + name for name in M.CASE_REVIEW_TOOLS}
