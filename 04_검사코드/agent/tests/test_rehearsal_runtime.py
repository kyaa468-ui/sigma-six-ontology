"""A copied checkout keeps replay identity honest while the clock is synthetic."""
from datetime import datetime
import importlib.util
from pathlib import Path


def test_operating_fingerprint_excludes_only_python_cache(tmp_path):
    path = Path(__file__).resolve().parents[1] / 'tools/run_ontology_rehearsal.py'
    spec = importlib.util.spec_from_file_location('rehearsal_digest_test', path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    (tmp_path / '__pycache__').mkdir()
    bytecode = tmp_path / '__pycache__/snapshot.cpython-312.pyc'
    bytecode.write_bytes(b'first import')
    account = tmp_path / 'account.json'
    account.write_text('{"cash": 1}')
    before = runner.digest_files([tmp_path])
    bytecode.write_bytes(b'concurrent import')
    assert before == runner.digest_files([tmp_path])
    account.write_text('{"cash": 2}')
    assert before != runner.digest_files([tmp_path])


def test_source_copy_preserves_original_common_and_loaded_definition(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'tools/run_ontology_rehearsal.py'
    spec = importlib.util.spec_from_file_location('rehearsal_runtime_test', path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runtime, hashes = runner.copy_runtime(tmp_path)
    clock = {'now': datetime.fromisoformat('2026-11-06T10:30:00+09:00')}
    copied = runner.load_runtime(monkeypatch, runtime, clock)
    assert copied.WORK == str(runtime / 'work')
    assert copied.CONFIG == str(runtime / 'config')
    assert copied.load_env.__defaults__ == (str(runtime / '.env'),)
    assert copied.now() == clock['now']
    assert copied.now.__code__.co_filename == str(runtime / 'src/common.py')
    assert not list(runtime.rglob('.env'))
    assert 'src/common.py' in hashes
    import execution_trace as E
    import sizing
    import risk
    body = E._definition_snapshot()
    assert body['source_identity_status'] == 'verified_loaded_source', body
    # Advance the external clock without replacing a production function.
    clock['now'] = datetime.fromisoformat('2026-11-09T10:30:00+09:00')
    assert copied.today().isoformat() == '2026-11-09'
    assert E._definition_snapshot()['source_identity_status'] == 'verified_loaded_source'
