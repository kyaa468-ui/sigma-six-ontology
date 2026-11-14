"""Immutable AI invocation inputs; no credentials or inferred provider defaults.

Recording is best effort and independent of judgment coverage and trade policy.
The provider's actual internal prompt/model state is not claimed to be captured.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

import common as C
import record_store
import storage_objects as objects


def _digest(value):
    digest=hashlib.sha256()
    for part in objects.chunks(value,separators=(', ',': ')):digest.update(part)
    return digest.hexdigest()


def _sync_directory(path):
    if os.name != 'nt':
        directory=os.open(path,os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def capture_attempt(attempt_id, kind, prompt, allowed_tools, options=None):
    try:
        import execution_trace as E
        E._id(attempt_id)
        root = Path(C.AGENT)
        files = [root/'CLAUDE.md', root/'.claude/settings.json', root/'src/mcp_server.py', root/'run.py']
        files += sorted((root/'.claude/agents').glob('*.md'))
        sources = {}
        for path in files:
            sources[str(path.relative_to(root))] = path.read_text(encoding='utf-8') if path.is_file() else None
        parsed = ast.parse(sources['src/mcp_server.py'] or '')
        schemas = []
        for node in parsed.body:
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and any(ast.unparse(d)=='mcp.tool()' for d in node.decorator_list):
                schemas.append({'name':node.name,'arguments':ast.unparse(node.args),
                                'returns':ast.unparse(node.returns) if node.returns else None,
                                'description':ast.get_docstring(node)})
        body = {'format':'AI_EXECUTION_INPUT@v1','run_attempt_id':attempt_id,'kind':kind,
                'created_at':C.now().isoformat(),'cmd_prompt':prompt,'sources':sources,
                'source_hashes':{k:_digest(v) for k,v in sources.items()},'tool_contracts':schemas,
                'allowed_tools':list(allowed_tools),'options':options or {},
                'known_model_setting':{k:os.environ[k] for k in ('ANTHROPIC_MODEL','CLAUDE_MODEL') if os.environ.get(k)},
                'provider_internal_state':'not_captured','model_default':'unspecified_unless_explicitly_configured'}
        import evidence_wire
        body['wire_format'] = (options or {}).get('wire_format', evidence_wire.selected_format())
        path = C.ontology_root()/'runtime_inputs'/f'{attempt_id}.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        envelope={'record':body,'sha256':_digest(body)}
        try:
            record_store.publish(path, envelope, kind='runtime_input')
        except FileExistsError:
            old=load_attempt(attempt_id)
            compared=('kind','cmd_prompt','source_hashes','allowed_tools','options','known_model_setting')
            if any(old.get(k)!=body.get(k) for k in compared):
                raise ValueError('AI_EXECUTION_INPUT_CONFLICT')
            return {'run_attempt_id':attempt_id,'sha256':_digest(old),'recording_status':'recorded','replayed':True}
        return {'run_attempt_id':attempt_id,'sha256':envelope['sha256'],'recording_status':'recorded'}
    except Exception as exc:
        print(f'AI input snapshot unavailable: {type(exc).__name__}: {exc}',file=sys.stderr)
        return {'run_attempt_id':attempt_id,'recording_status':'recording_error','error':str(exc)}


def load_attempt(attempt_id):
    import execution_trace as E
    E._id(attempt_id)
    path=C.ontology_root()/'runtime_inputs'/f'{attempt_id}.json'
    if not path.exists(): return None
    value=record_store.load(path)
    if not isinstance(value,objects.ObjectDict) and _digest(value['record'])!=value.get('sha256'): raise ValueError('AI execution input hash mismatch')
    return value['record']


def record_result(attempt_id, terminal, result=None, error=None):
    """Keep the full output under the attempt ID, not the legacy minute filename."""
    try:
        import execution_trace as E
        E._id(attempt_id)
        body={'run_attempt_id':attempt_id,'terminal':terminal,'result':result,'error':error,
              'usage_observation': usage_observation(result),
              'created_at':C.now().isoformat()}
        path=C.ontology_root()/'runtime_inputs'/f'{attempt_id}.result.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        envelope={'record':body,'sha256':_digest(body)}
        record_store.publish(path, envelope, kind='runtime_result')
        return {'recording_status':'recorded','sha256':envelope['sha256']}
    except Exception as exc:
        print(f'AI result snapshot unavailable: {type(exc).__name__}: {exc}',file=sys.stderr)
        return {'recording_status':'recording_error','error':str(exc)}


def record_tool_call(name, arguments, result=None, error=None):
    """Freeze actual server-visible tool I/O; cannot change trading outcomes.

    Provider-internal reasoning/subagent traffic is not inferred from these
    records. Proposal/research payloads remain in their denied research stores.
    """
    # Historical inspection APIs retain their read-only contract. Capture the
    # tools that supply or consume an actual trading decision, not later queries.
    if name not in {'get_briefing','get_account','set_regime','record_ai_judgments',
                    'record_external_evidence','plan_order','place_order','record_decision','get_evidence_detail'}:
        return {'recording_status':'not_applicable'}
    attempt=os.environ.get('RUN_ATTEMPT_ID')
    if not attempt or os.environ.get('RUN_MODE') in ('case_review','proposal_review','validation_review','prompt_validation'):
        return {'recording_status':'not_applicable'}
    try:
        import execution_trace as E
        E._id(attempt)
        body={'run_attempt_id':attempt,'tool':name,'arguments':arguments,'result':result,'error':error,
              'recorded_at':C.now().isoformat(),'scope':'server_visible_interaction'}
        body['response_utf8_bytes'] = len(result.encode('utf-8')) if isinstance(result, str) else objects.fingerprint(result)[1]
        body['response_tokens'] = None
        path=C.ontology_root()/'runtime_inputs'/attempt/'tools'/f'tool-{uuid.uuid4().hex}.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        envelope={'record':body,'sha256':_digest(body)}
        record_store.publish(path, envelope, kind='tool_io')
        return {'recording_status':'recorded','sha256':envelope['sha256']}
    except Exception as exc:
        print(f'AI interaction record unavailable: {exc}',file=sys.stderr)
        return {'recording_status':'recording_error','error':str(exc)}


from contextlib import contextmanager


def usage_observation(result):
    result = result or {}
    usage = result.get('usage') or {}
    return {'provider_usage': dict(usage), 'total_cost_usd': result.get('total_cost_usd'),
            'missing_fields': [k for k in ('input_tokens', 'output_tokens',
                'cache_creation_input_tokens', 'cache_read_input_tokens') if k not in usage],
            'scope': 'provider_reported_total; subcalls_not_added_again',
            'provider_model_usage': result.get('modelUsage'), 'role_usage': 'not_reported',
            'subagent_transcripts': 'not_captured', 'account_limit_conversion': 'not_measured'}

@contextmanager
def operation(kind):
    """Record actual non-AI prep/close invocations without inventing usage rows."""
    identifier='operation-'+uuid.uuid4().hex
    started=C.now().isoformat();state={};failure=None
    def archive(phase,body):
        try:
            record_store.publish(C.ontology_root()/'runtime_operations'/(identifier+'.'+phase+'.json'),body,kind='runtime_operation')
        except Exception as exc:print('Operation recording failed: '+str(exc),file=sys.stderr)
    archive('start',{'operation_id':identifier,'kind':kind,'started_at':started,'status':'running'})
    try:yield state
    except BaseException as exc:
        failure=type(exc).__name__+': '+str(exc)
        raise
    finally:
        finished=C.now().isoformat()
        archive('finish',{'operation_id':identifier,'kind':kind,'started_at':started,'finished_at':finished,
            'status':'failed' if failure or state.get('source_process_ok') is False else 'completed',
            'source_process_ok':state.get('source_process_ok'),'error':failure})
        try:
            import logger
            logger.publish_runtime_summaries()
        except Exception as exc:print('Operation summary failed: '+str(exc),file=sys.stderr)
