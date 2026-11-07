"""Actual saved steps 1–7 -> operator view -> public staging, entirely offline."""
import json
import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from test_ontology import C, M, ROOT, isolated
from test_close_evaluation import calendar, bytes_under
from test_proposal_integration import ready, retain
from test_judgment_integration import load_module
import judgment_coverage as JC
import learning_proposals as LP
import validation_spec as VS
import policy_validation as PV


def chain(monkeypatch, calendar):
    fake, attempt, app, review, rid, context, page = ready(monkeypatch, calendar)
    proposed = retain(context, page)
    proposed.update(kind='conditional_rule_change',
        target_ref=next(x['target_ref'] for x in context['targets'] if x['target_id']=='confidence.conditional'),
        effect={'operation':'add_confidence','amount':-.04,'unit':'confidence','placement':'after_legacy_boost_before_cap'},
        validation_kind='deterministic_rule', validation_question='고정된 실제 입력의 계획 차이',
        expected_change='시험용 보정 비교이며 효과는 미검증', support_case_ids=[app['application_id']])
    proposed.pop('reason_kind')
    accepted = M.record_learning_proposals(rid, 'trace-proposal', [proposed])
    assert accepted['items'][0]['status']=='accepted', accepted
    pid = accepted['items'][0]['proposal_id']
    now=C.now()
    registered=VS.register_validation(pid,'trace-validation',{
        'mode':'retrospective','family_id':'trace-fixture',
        'period':{'start':(now-timedelta(days=10)).isoformat(),'end':now.isoformat(),'knowledge_as_of':now.isoformat()},
        'scope':{'codes':['*'],'confidence_for':['매수','매도']}})
    assert registered['status']=='registered', registered
    result=PV.run_validation(registered['validation_id'])
    assert result['result_id']
    return fake, attempt, app, review, pid, result


def forbid_runtime(monkeypatch):
    import broker, policy_adoption, close_evaluation, prompt_validation
    def forbidden(*args, **kwargs):
        pytest.fail('Opening a trace must not call a runtime action')
    for owner,name in [(broker,'get'),(M,'get_briefing'),(M,'get_account'),
                       (M,'refresh_execution_status'),(policy_adoption,'adopt_policy'),
                       (close_evaluation,'run_evaluation'),(prompt_validation,'run_prompt_validation')]:
        monkeypatch.setattr(owner,name,forbidden)


def protected_bytes():
    return {name:data for name,data in bytes_under(Path(C.WORK)).items()
            if not name.startswith('ontology/trace_view/')
            and not name.startswith('ontology/case_review/usage_events/')
            and not name.startswith('ontology/case_review/locks/')}


def assert_only_new_shared_blobs(before):
    after = protected_bytes()
    assert all(after.get(name) == value for name, value in before.items())
    for name in after.keys() - before.keys():
        assert name.startswith('ontology/snapshots/blobs/')
        assert Path(name).stem == hashlib.sha256(after[name]).hexdigest()


def test_real_chain_snapshot_keeps_original_calls_and_exact_validation(monkeypatch, calendar):
    import trace_snapshot as S
    import trace_export as X
    fake, attempt, app, review, pid, result=chain(monkeypatch, calendar)
    before=protected_bytes(); coverage=JC.get_coverage(attempt['run_attempt_id'])
    forbid_runtime(monkeypatch)
    built=S.build_view_snapshot('2026-11-13', C.now().isoformat())
    assert built.get('view_id'), built
    loaded=S.load_view_snapshot(built['view_id'])
    projection=loaded['projection']
    target=next(x for x in projection['targets'] if x.get('run_attempt_id')==attempt['run_attempt_id'] and x.get('confidence_for')=='매수')
    detail=projection['traces'][target['trace_id']]
    encoded=json.dumps(detail, ensure_ascii=False)
    for identifier in (app['application_id'], app['judgment_id'], review['review_id'], pid, result['result_id']):
        assert identifier in encoded, identifier
    assert detail['deliveries'] and detail['judgments'] and detail['evaluations']
    assert detail['execution_context']['orders']
    assert len(fake.orders)==1
    assert JC.get_coverage(attempt['run_attempt_id'])==coverage
    assert_only_new_shared_blobs(before)
    frozen=bytes_under(Path(C.WORK))
    assert S.load_view_snapshot(built['view_id'])==loaded
    assert bytes_under(Path(C.WORK))==frozen
    exported=X.export_trace_summary(built['view_id'])
    assert exported.get('published') is False, exported
    assert not Path(C.DOCS_DATA).exists()
    assert_only_new_shared_blobs(before)


@pytest.mark.parametrize('kind',['build-trace-view','trace-view','export-trace-summary'])
def test_cli_dry_never_enters_trading_or_writes(monkeypatch,kind,capsys):
    runner=load_module('trace_cli_'+kind.replace('-','_'),ROOT/'run.py')
    def forbidden(*args,**kwargs): pytest.fail('Trading/publication path entered')
    for name in ('acquire','refresh_account','publish','claude','py'):
        monkeypatch.setattr(runner,name,forbidden)
    before=bytes_under(Path(C.WORK))
    monkeypatch.setattr(sys,'argv',['run.py',kind,'--view-id','view-example','--dry'])
    runner.main()
    assert json.loads(capsys.readouterr().out)['status']=='dry_run'
    assert bytes_under(Path(C.WORK))==before


def test_view_commands_reject_ai_and_missing_view(monkeypatch):
    runner=load_module('trace_cli_errors',ROOT/'run.py')
    for argv in (['trace-view'], ['build-trace-view','--with-ai'],
                 ['trace-view','--view-id','v','--port','0']):
        monkeypatch.setattr(sys,'argv',['run.py',*argv])
        with pytest.raises(SystemExit) as error: runner.main()
        assert error.value.code==2


def test_trading_ai_cannot_read_view_or_export_paths():
    runner=load_module('trace_cli_permissions',ROOT/'run.py')
    permissions=json.loads((ROOT/'.claude/settings.json').read_text())['permissions']['deny']
    denied='Read(./work/ontology/trace_view/**)'
    assert denied in runner.TOOLS_DENY and denied in permissions
