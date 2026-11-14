"""Run the real 1–8 pipeline with local source/AI/HTTP fixtures, never live services.

This is a development rehearsal, not a trading mode. Each run owns its config,
account, ledgers, predictions and UI snapshot. Production records are hashed
before and after; Python's audit hook additionally rejects live I/O and writes
outside the rehearsal directory. Run with the test environment (pytest needed).
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

sys.dont_write_bytecode = True
AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT / 'src'))
import common as C


def digest_files(roots, exclude=None):
    values = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob('*')):
            # Concurrent tests may refresh Python bytecode. It is neither an
            # operating input nor a persistent account/config record.
            if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                continue
            if path.is_file() and not (exclude and path.is_relative_to(exclude)):
                values[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def copy_runtime(output):
    """Copy executable inputs; derive common paths naturally from this checkout.

    Changing common.WORK/load_env/now would invalidate the loaded-source proof
    used by policy replay. A byte-identical checkout lets the production module
    initialize its own paths and defaults, without relaxing that proof.
    """
    target = output / 'runtime' / 'agent'
    target.mkdir(parents=True)
    omitted = {'work', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
               '.git', '.venv', 'venv', 'node_modules'}
    def ignore(directory, names):
        return [name for name in names if name in omitted or name.startswith('.env')
                or name.endswith(('.pyc', '.pyo')) or (Path(directory) / name).is_symlink()]
    for name in ('src', 'config', 'review', '.claude', 'tests', 'tools'):
        shutil.copytree(AGENT / name, target / name, ignore=ignore)
    for name in ('run.py', 'CLAUDE.md', 'requirements.txt'):
        shutil.copy2(AGENT / name, target / name)
    source_hashes = {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in target.rglob('*') if p.is_file()}
    assert all(hashlib.sha256((AGENT / name).read_bytes()).hexdigest() == digest
               for name, digest in source_hashes.items())
    return target, source_hashes


def load_runtime(mp, runtime_agent, clock):
    """Load pristine functions/constants; replace only the external clock class."""
    global C
    # The command is standalone, but explicit unloading also makes a focused
    # test safe when common or a helper was already imported by its test runner.
    own_module = sys.modules.get(__name__)
    for name, module in list(sys.modules.items()):
        path = getattr(module, '__file__', None)
        if not path or module is own_module:
            continue
        try:
            resolved = Path(path).resolve()
            local = any(resolved.is_relative_to(AGENT / folder) for folder in ('src', 'tools'))
        except (OSError, TypeError):
            local = False
        if local:
            mp.delitem(sys.modules, name, raising=False)
    mp.syspath_prepend(str(runtime_agent / 'tools'))
    mp.syspath_prepend(str(runtime_agent / 'src'))
    importlib.invalidate_caches()
    C = importlib.import_module('common')
    class FixtureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock['now']
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    mp.setattr(C, 'datetime', FixtureDateTime)
    assert Path(C.AGENT) == runtime_agent
    assert C.now.__module__ == 'common' and C.now.__name__ == 'now'
    assert C.now() == clock['now']
    return C


def _request(server, path, method='GET', headers=None):
    """Run the genuine HTTP parser/handler, without opening a network socket."""
    class Connection:
        def __init__(self, data):
            self.incoming, self.output = io.BytesIO(data), bytearray()
        def makefile(self, *_):
            return self.incoming
        def sendall(self, data):
            self.output.extend(data)
    fields = {'Host': '127.0.0.1:8765', **(headers or {})}
    data = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in fields.items()) + '\r\n'
    connection = Connection(data.encode())
    server.RequestHandlerClass(connection, ('127.0.0.1', 12345), server)
    head, body = bytes(connection.output).split(b'\r\n\r\n', 1)
    return int(head.split()[1]), body


def run_view(mp, execution, research, clock):
    import trace_snapshot as S
    import trace_export as X
    import trace_view as V
    from types import SimpleNamespace
    import importlib.util

    spec = importlib.util.spec_from_file_location('rehearsal_view_runner', Path(C.AGENT) / 'run.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    def cli(*args):
        out = io.StringIO()
        with mp.context() as scoped, contextlib.redirect_stdout(out):
            scoped.setattr(sys, 'argv', ['run.py', *args])
            runner.main()
        return json.loads(out.getvalue())
    built = cli('build-trace-view', '--as-of', '2026-11-13', '--knowledge-as-of', clock['now'].isoformat())
    assert built.get('view_id'), built
    loaded = S.load_view_snapshot(built['view_id'])
    projection = loaded['projection']
    # Only explicit links count; no same-code/time inference in this assertion.
    main = [x for x in projection['targets'] if x.get('run_attempt_id') == execution['run_attempt_id']
            and x.get('code') == execution['main_code'] and x.get('confidence_for') == '매수']
    assert len(main) == 1, main
    detail = projection['traces'][main[0]['trace_id']]
    encoded = json.dumps(detail, ensure_ascii=False)
    required = [execution['judgment_id'], *execution['application_ids'], research['review_id'],
                research['proposal_id'], research['result_id']]
    for identifier in required:
        assert identifier in encoded, f'Missing stage-8 explicit link: {identifier}'
    assert detail['deliveries'] and detail['judgments'] and detail['evaluations']
    assert detail['execution_context']['orders']
    before_reads = digest_files([Path(C.WORK)])
    with mp.context() as scoped:
        scoped.setattr(V, 'ThreadingHTTPServer', lambda address, handler: SimpleNamespace(
            server_address=address, server_port=8765, RequestHandlerClass=handler))
        server = V.create_server(built['view_id'])
        responses = {p: _request(server, p)[0] for p in ('/', '/trace.js', '/trace.css', '/api/view')}
        assert set(responses.values()) == {200}, responses
        assert _request(server, '/api/view', 'POST')[0] == 405
        assert _request(server, '/api/view', headers={'Origin': 'https://invalid.example'})[0] == 403
        assert _request(server, '/config/params.json')[0] == 404
        api = json.loads(_request(server, '/api/view')[1])
        assert api['view_id'] == built['view_id']
    assert before_reads == digest_files([Path(C.WORK)]), 'Viewing changed stored records'
    exported = cli('export-trace-summary', '--view-id', built['view_id'])
    assert exported.get('export_id') and exported.get('published') is False, exported
    return {'status': 'passed', 'snapshot_status': loaded['status'], 'view_id': built['view_id'],
            'trace_id': main[0]['trace_id'], 'linked_ids': required, 'http_routes': responses,
            'read_only_verified': True, 'export': exported,
            'target_count': len(projection['targets']), 'case_count': len(projection['cases']),
            'projection_errors': projection.get('errors', []),
            'browser_check': 'separate_manual_browser_rehearsal'}


def rehearse(output, baseline_report=None):
    import pytest
    output.mkdir(parents=True, exist_ok=False)
    runtime_agent, runtime_hashes = copy_runtime(output)
    work, config = runtime_agent / 'work', runtime_agent / 'config'
    work.mkdir()
    protected = [AGENT / 'config', AGENT / 'work', AGENT.parent / 'docs/data', AGENT.parent / 'live']
    original = digest_files(protected, output.parent if output.parent == AGENT / 'work/rehearsals' else output)
    guard = {'active': False, 'attempts': []}
    allowed = output.resolve()

    def check_write(raw):
        if isinstance(raw, int) or raw is None:
            return
        path = Path(os.fsdecode(raw)).absolute().resolve()
        if path == Path(os.devnull) or path.is_relative_to(allowed):
            return
        guard['attempts'].append({'kind': 'write_outside_rehearsal', 'path': str(path)})
        raise PermissionError(f'Rehearsal write outside isolated directory: {path}')

    def audit(event, args):
        if not guard['active']:
            return
        if event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system', 'os.posix_spawn'):
            guard['attempts'].append({'kind': event})
            raise PermissionError('External network/process forbidden in offline rehearsal')
        if event == 'open':
            path, mode, flags = args
            if (isinstance(mode, str) and any(x in mode for x in 'wax+')) or (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
                check_write(path)
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.truncate'):
            check_write(args[0])
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            check_write(args[0]); check_write(args[1])
        elif event == 'sqlite3.connect':
            from urllib.parse import urlsplit, unquote
            target = args[0]
            check_write(unquote(urlsplit(target).path) if str(target).startswith('file:') else target)
    sys.addaudithook(audit)
    clock = {'now': datetime(2026, 11, 6, 8, 30, tzinfo=C.KST)}
    report = {'format': 'SIGMA_SIX_OFFLINE_REHEARSAL@v1', 'status': 'running',
              'generated_at': datetime.now(C.KST).isoformat(), 'output': str(output), 'work': str(work),
              'runtime_agent': str(runtime_agent), 'runtime_source_sha256': runtime_hashes,
              'runtime_isolation': {'source_bytes_match_checkout': True,
                  'common_functions_and_path_constants': 'original_unmodified',
                  'clock_boundary': 'common.datetime.now uses fixed fixture time; common.now is unchanged',
                  'source_identity_scope': 'existing loaded function/default/supported global check; not real market/model correctness',
                  'excluded': ['.env files', 'operating work records', 'caches', 'virtual environments', 'symlinks']},
              'synthetic_data': True, 'external_ai_calls': 0, 'external_broker_calls': 0,
              'operational_adoption': False, 'published': False, 'steps': {}, 'phase_timings': {},
              'dependencies': {name: importlib.metadata.version(name) for name in
                  ('pytest', 'pandas', 'pykrx', 'gs-quant', 'mcp', 'statsmodels', 'scipy', 'exchange-calendars')},
              'limitations': ['실제 키움 접수·체결 API/인증은 검증하지 않음',
                              'AI는 고정 제출값과 가짜 CLI이며 실제 모델 판단 품질은 검증하지 않음',
                              '합성 거래일·시세이며 실제 수익성과 운영 반영의 근거가 아님']}
    with pytest.MonkeyPatch.context() as mp:
        temporary = output / 'tmp'
        temporary.mkdir()
        mp.setattr(tempfile, 'tempdir', str(temporary))
        mp.setenv('TMPDIR', str(temporary))
        mp.setenv('MPLCONFIGDIR', str(temporary / 'matplotlib'))
        mp.setenv('MPL_IGNORE_SYSTEM_FONTS', '1')
        mp.setenv('XDG_CACHE_HOME', str(temporary / 'cache'))
        for name in list(os.environ):
            if name.startswith(('KIWOOM_', 'KRX_', 'NAVER_', 'ANTHROPIC_', 'OPENAI_', 'RUN_', 'PROPOSAL_', 'CASE_REVIEW_')) or name in ('ACCOUNT_NAME', 'ONTOLOGY_ACCOUNT_SCOPE'):
                mp.delenv(name, raising=False)
        mp.setenv('MODE', 'mock')
        mp.setenv('RUN_ID', 'local-rehearsal')
        load_runtime(mp, runtime_agent, clock)
        def forbidden(*args, **kwargs):
            guard['attempts'].append({'kind': 'network_or_process_boundary'})
            raise AssertionError('External call forbidden in rehearsal')
        class BlockedProcess(subprocess.Popen):
            # Preserve Popen[bytes] used by MCP's import-time type annotations.
            def __init__(self, *args, **kwargs):
                forbidden(*args, **kwargs)
        mp.setattr(socket.socket, 'connect', forbidden)
        mp.setattr(socket, 'create_connection', forbidden)
        mp.setattr(subprocess, 'Popen', BlockedProcess)
        mp.setattr(subprocess, 'run', forbidden)
        guard['active'] = True
        @contextlib.contextmanager
        def phase(name):
            started = time.perf_counter()
            state = {'started_at': datetime.now(C.KST).isoformat(), 'status': 'running'}
            report['phase_timings'][name] = state
            try:
                yield
            except BaseException:
                state['status'] = 'failed'
                raise
            else:
                state['status'] = 'completed'
            finally:
                state['seconds'] = round(time.perf_counter() - started, 3)
                import resource
                state['process_peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
                import storage_objects
                state['storage_metrics']=dict(storage_objects.METRICS)
                save(output / 'report.json', report)
                print(f"Phase {name}: {state['status']} ({state['seconds']:.3f}s)", flush=True)
        try:
            with phase('imports'):
                from rehearsal_execution import run_execution
                from rehearsal_research import run_research
                from rehearsal_faults import run_faults
            print('Running actual candidate → judgment → execution functions with local boundary fixtures.', flush=True)
            with phase('1-3_execution'):
                execution = run_execution(mp, clock)
            report['steps']['1-3'] = execution['summary']
            save(output / 'report.json', report)
            print('Running actual scoring → review → proposal → policy validation functions.', flush=True)
            with phase('4-7_research'):
                research = run_research(mp, execution, clock)
            report['steps']['4-7'] = research['summary']
            save(output / 'report.json', report)
            print('Building the operator snapshot and public preparation through run.py commands.', flush=True)
            with phase('8_view'):
                report['steps']['8'] = run_view(mp, execution, research, clock)
            print('Replaying response/storage loss and legacy stop-monitor timing.', flush=True)
            with phase('fault_scenarios'):
                report['fault_scenarios'] = run_faults(mp, clock)
            assert not guard['attempts'], guard['attempts']
            report['status'] = 'passed_with_explicit_limits'
        except Exception as exc:
            report.update(status='failed', failure={'type': type(exc).__name__, 'message': str(exc)[:4000],
                                                   'traceback': traceback.format_exc()[-16000:]})
            print(report['failure']['traceback'], file=sys.stderr, flush=True)
        finally:
            guard['active'] = False
    after = digest_files(protected, output.parent if output.parent == AGENT / 'work/rehearsals' else output)
    report['isolation'] = {'forbidden_attempts': guard['attempts'], 'operating_files_unchanged': original == after,
                           'operating_file_count': len(original),
                           'fingerprint_exclusions': ['Python __pycache__ directories', '*.pyc', '*.pyo'],
                           'changed_paths': sorted(k for k in original.keys() | after.keys() if original.get(k) != after.get(k))}
    if original != after or guard['attempts']:
        report['status'] = 'failed'
    report['source_hashes'] = digest_files([AGENT / 'src', AGENT / 'tools', AGENT / 'review'])
    report['source_hashes'].update({str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [AGENT / 'run.py', AGENT.parent / 'docs/trace-summary.js',
                     *sorted((AGENT / 'tests/fixtures/ontology').rglob('baseline*.json'))]
        if path.is_file()})
    report['config_sha256'] = digest_files([config])
    from rehearsal_storage import summarize_storage, compare_rehearsals
    report['storage'] = summarize_storage(work)
    if baseline_report is not None:
        report['comparison_with_previous_rehearsal'] = compare_rehearsals(report, baseline_report)
        if report['comparison_with_previous_rehearsal']['status'] == 'mismatch':
            report['status'] = 'failed'
    report['completed_at'] = datetime.now(C.KST).isoformat()
    save(output / 'report.json', report)
    print(json.dumps({'status': report['status'], 'report': str(output / 'report.json'),
                      'operating_files_unchanged': original == after}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed_with_explicit_limits' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New isolated output directory; must not already exist')
    parser.add_argument('--serve', type=Path, help='Serve the saved rehearsal directory, without replaying it')
    parser.add_argument('--baseline-report', type=Path, help='Compare stable trading outputs and physical storage against a previous report')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.serve:
        report = json.loads((args.serve / 'report.json').read_text())
        if report.get('format') != 'SIGMA_SIX_OFFLINE_REHEARSAL@v1':
            parser.error('Not a rehearsal report')
        C.WORK = report['work']
        import trace_view
        trace_view.serve_view(report['steps']['8']['view_id'], args.port)
        return 0
    output = (args.output or AGENT / 'work/rehearsals' /
              (datetime.now(C.KST).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])).resolve()
    return rehearse(output, args.baseline_report)


if __name__ == '__main__':
    raise SystemExit(main())
