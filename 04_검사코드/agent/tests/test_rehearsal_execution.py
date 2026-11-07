"""Real early-pipeline rehearsal with transport-only fixtures."""
import importlib.util
from datetime import datetime
from pathlib import Path
import shutil

from test_ontology import C, M, ROOT, isolated


def test_rehearsal_runs_real_entrypoints(monkeypatch, tmp_path):
    # Use the current operating numbers in an isolated config copy; no tuning.
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    monkeypatch.setattr(C, "CONFIG", str(config))
    monkeypatch.setattr(C, "params", lambda: C.read_json(str(config / "params.json")))
    monkeypatch.setattr(C, "risk_limits", lambda: C.read_json(str(config / "risk_limits.json")))
    monkeypatch.setattr(M.tuning, "PARAMS_PATH", str(config / "params.json"))
    clock = {"now": datetime.fromisoformat("2026-11-06T08:30:00+09:00")}
    monkeypatch.setattr(C, "now", lambda: clock["now"])
    spec = importlib.util.spec_from_file_location("rehearsal_execution", ROOT / "tools/rehearsal_execution.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run_execution(monkeypatch, clock)
    assert result["summary"]["partial_fill"] == {"requested": 100, "confirmed": 40, "unconfirmed": 60}
    assert result["summary"]["response_loss"]["dispatches"] == 1
    assert result["summary"]["receipt_loss"]["dispatches"] == 1
    assert result["application_ids"]
    assert (Path(C.WORK) / "rehearsal_execution.json").exists()
    # This checks the collector against actual monitor outputs, not a claimed
    # successful coverage flag written by the rehearsal itself.
    import portfolio_capture as PC
    import execution_trace as E
    import snapshot_store
    records = [snapshot_store.load(path) for path in (Path(C.WORK) / 'ontology/executions').rglob('*.json')
               if path.parent.name in (*PC.KINDS, 'monitor_check')]
    coverage = PC.coverage(records, result['period_start'], result['period_end'], E.account_scope(), [result['main_code']])
    assert coverage['complete'], coverage['gaps']
    assert len(coverage['checks']) == 3
    without_middle_tick = [r for r in records if r.get('kind') != 'portfolio_monitor'
                           or not r['check']['at'].startswith('2026-11-06T10:31:')]
    missing = PC.coverage(without_middle_tick, result['period_start'], result['period_end'], E.account_scope(), [result['main_code']])
    assert missing['complete'] is False
    assert any(g['reason'] == 'scheduled_monitor_check_missing' for g in missing['gaps'])
