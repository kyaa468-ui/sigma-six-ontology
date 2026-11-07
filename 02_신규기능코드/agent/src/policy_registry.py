"""Immutable manual policy publication and pinned runtime interpretation.

Reading never creates directories or repairs history. Callers receive corruption
explicitly; an unreadable directory must not become a successful empty job.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextvars import ContextVar
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

import common as C

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}\Z")
_ERRORS = {}


class StoreError(ValueError):
    pass


class StoreBusy(RuntimeError):
    pass


def root():
    return Path(C.WORK) / 'ontology' / 'policy_registry'


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or '..' in value:
        raise StoreError('Invalid policy registry identifier')
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _path(kind, identifier):
    return root() / _id(kind) / (_id(identifier) + '.json')


def load(kind, identifier):
    path = _path(kind, identifier)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise StoreError(f'{kind}/{identifier}: expected object')
    if value.get('format') != 'immutable-policy-registry-record@v1' or not isinstance(value.get('record'), dict):
        raise StoreError(f'{kind}/{identifier}: missing policy registry integrity envelope')
    if digest(value['record']) != value.get('content_sha256'):
        raise StoreError(f'{kind}/{identifier}: policy registry content hash mismatch')
    return value['record']


def archive(kind, identifier, body):
    """Publish exactly one complete value. Same key/content is an idempotent read."""
    if not isinstance(body, dict):
        raise StoreError('Policy registry records must be objects')
    payload = canonical({'format': 'immutable-policy-registry-record@v1', 'content_sha256': digest(body), 'record': body})
    target = _path(kind, identifier)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.policy-registry-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            previous = load(kind, identifier)
            if previous is None or canonical(previous) != canonical(body):
                raise StoreError(f'Immutable policy registry key conflict: {kind}/{identifier}')
            return previous
        if os.name != 'nt':
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        os.unlink(temporary)
    return copy.deepcopy(body)


@contextmanager
def locked(identifier, timeout=30):
    """Serialize publication of one revision series across processes/threads."""
    path = root() / 'locks' / (_id(identifier) + '.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    # SQLite's reserved writer lock works on Windows and POSIX. These lock-only
    # databases live in research storage, completely separate from order guards.
    connection = sqlite3.connect(path, timeout=timeout)
    try:
        try:
            connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
                raise StoreBusy(identifier) from exc
            raise
        yield
        connection.commit()
    finally:
        connection.close()


def records(kind):
    folder = root() / _id(kind)
    result, errors = [], []
    try:
        paths = sorted(folder.iterdir())
    except FileNotFoundError:
        return {'records': [], 'errors': []}
    except OSError as exc:
        return {'records': [], 'errors': [{'kind': kind, 'error': str(exc)}]}
    for path in paths:
        if path.suffix != '.json':
            continue
        try:
            value = load(kind, path.stem)
            if value is None:
                raise StoreError('Record disappeared during read')
            result.append(value)
        except (OSError, ValueError) as exc:
            errors.append({'kind': kind, 'record_id': path.stem, 'error': str(exc)})
    return {'records': result, 'errors': errors}


def record_error(job_id, operation, error):
    """A policy registry error never changes the historical AI run's coverage status."""
    row = {'error_id': 'policy-registry-error-' + uuid.uuid4().hex, 'proposal_run_id': job_id,
           'operation': operation, 'error': str(error), 'recorded_at': C.now().isoformat()}
    _ERRORS.setdefault(str(root()), []).append(copy.deepcopy(row))
    try:
        archive('errors', row['error_id'], row)
    except Exception:
        pass
    return {'recording_status': 'recording_error', **row}


def errors(job_id=None):
    stored = records('errors')
    by = {x['error_id']: x for x in stored['records'] if isinstance(x.get('error_id'), str)}
    by.update({x['error_id']: x for x in _ERRORS.get(str(root()), [])})
    return {'records': [x for x in by.values() if job_id is None or x.get('proposal_run_id') == job_id],
            'errors': stored['errors']}


VERSION = 'POLICY_REGISTRY@v1'
MARKER = '_ontology_policy'
_CONTEXT = ContextVar('ontology_runtime_policy', default=None)
_LOCK_HELD = ContextVar('ontology_policy_writer_lock', default=False)


def semantic_params(params):
    return {k:copy.deepcopy(v) for k,v in params.items() if k != MARKER}


def revision(params=None):
    return 'state-' + digest(C.params() if params is None else params)


def instant(value):
    value = value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if value.tzinfo is None: raise ValueError('TIMEZONE_REQUIRED')
    return value.astimezone(C.KST)


@contextmanager
def change_lock():
    """Same cross-process lock as legacy tuning; reentrant within one call."""
    if _LOCK_HELD.get():
        yield
        return
    path = Path(C.WORK) / 'tuning_submission_lock.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    connection = sqlite3.connect(path,timeout=30)
    token = None
    try:
        connection.execute('BEGIN IMMEDIATE')
        token = _LOCK_HELD.set(True)
        yield
        connection.commit()
    finally:
        if token is not None: _LOCK_HELD.reset(token)
        connection.close()


def _current_base():
    import learning_proposals as L
    return L.target_snapshot()


def snapshot(params=None, at=None):
    """Resolve a committed pointer once. Numeric params always stay authoritative.

    Expiry, source drift, or revoked proof suspends optional conditions/lessons;
    none of these states secretly restores old numeric parameters.
    """
    params = copy.deepcopy(C.params() if params is None else params)
    result = {'version':VERSION,'revision':revision(params),'policy_revision':None,
              'status':'inactive','params':params,'package':None}
    marker = params.get(MARKER)
    if not marker: return result
    try:
        if not isinstance(marker,dict): raise ValueError('INVALID_POLICY_POINTER')
        result['policy_revision'] = marker.get('package_id')
        if marker.get('status') != 'active':
            return {**result,'status':marker.get('status','suspended'),'reason':marker.get('reason')}
        package = load('packages',marker['package_id'])
        intent = load('activation_intents',marker['activation_id'])
        if (not package or not intent or digest(package) != marker.get('package_hash')
                or intent.get('package_hash') != digest(package)
                or intent.get('candidate_params_hash') != digest(semantic_params(params))
                or intent.get('package_id') != marker['package_id']
                or intent.get('after_params') != params):
            raise ValueError('UNCOMMITTED_OR_CHANGED_POLICY_POINTER')
        prepared = load('adoptions',package['adoption_id'])
        import validation_store as V
        report = V.load('results',package['validation_result_id'])
        proof = V.load('result_commits',package['validation_result_id'])
        if (not prepared or not report or not proof or proof.get('result_hash') != V.digest(report)
                or V.digest(report) != package['validation_result_hash']
                or prepared.get('validation_result_hash') != package['validation_result_hash']
                or prepared.get('candidate_policy_hash') != package['candidate_policy_hash']
                or prepared.get('candidate_params') != semantic_params(params)
                or prepared.get('expected_revision') != revision(intent['before_params'])
                or report.get('adoption_eligibility',{}).get('status') != 'eligible'):
            raise ValueError('ADOPTION_PROOF_CHAIN_INVALID')
        result['package'] = copy.deepcopy(package)
        result['activation_id'] = marker['activation_id']
        result['adoption_id'] = package['adoption_id']
        t = instant(at or C.now())
        if not instant(package['effective_from']) <= t < instant(package['effective_until']):
            return {**result,'status':'expired'}
        base = _current_base()
        if (base['definition_hash'] != package['baseline_ref']['definition_hash']
                or base['settings_hash'] != package['deployed_settings_hash']):
            return {**result,'status':'stale_base','reason':'VALIDATED_COMBINATION_CHANGED'}
        return {**result,'status':'active'}
    except Exception as exc:
        return {**result,'status':'recording_error','reason':str(exc)}


def after_legacy_tuning(before, after):
    """An ordinary tune ends proof of the exact validated combination."""
    result = copy.deepcopy(after)
    marker = before.get(MARKER)
    if marker and semantic_params(before) != semantic_params(after):
        result[MARKER] = {**copy.deepcopy(marker),'status':'suspended',
                          'reason':'LEGACY_TUNING_CHANGED_VALIDATED_BASE','suspended_at':C.now().isoformat()}
    return result


def build_context(code, side, judgment_id=None, params=None, briefing=None, expected_revision=None):
    policy = snapshot(params)
    context = {'version':VERSION,'code':code,'side':side,'judgment_id':judgment_id,
               'policy':policy,'inputs':{},'status':'unknown','reason':'JUDGMENT_REQUIRED'}
    if expected_revision and expected_revision != policy['revision']:
        return {**context,'reason':'POLICY_REVISION_CHANGED'}
    if policy['status'] != 'active': return {**context,'reason':policy['status']}
    if not judgment_id: return context
    try:
        import ai_judgment as J
        judgment = J.load_judgment(judgment_id)
        if (not judgment or judgment.get('code') != code or judgment.get('confidence_for') != side
                or instant(judgment['created_at']) > C.now()
                or judgment.get('run_attempt_id') != os.environ.get('RUN_ATTEMPT_ID')):
            raise ValueError('JUDGMENT_CONTEXT_MISMATCH')
        values = judgment_inputs(judgment)
        return {**context,'inputs':{k:values[k] for k in ('features','signed_flows','stratum','adoption')},
                'source_refs':values['source_manifest'],'observation_id':values.get('comparison_context',{}).get('observation_id'),
                'confidence_raw':judgment.get('confidence_raw'),'status':'recorded','reason':None}
    except Exception as exc:
        return {**context,'reason':str(exc)}


def judgment_inputs(judgment):
    """Shared research/runtime projection of one explicit accepted judgment.

    A judgment can contain several premises. Do not pick the first premise and
    accidentally change which adopted claims the runtime rule means. This
    projection covers adopted claims of the judgment and creates no premise.
    """
    import case_dataset as D
    application = {'application_id':None,'judgment_id':judgment['judgment_id'],'code':judgment['code'],
                   'judgment_date':judgment['created_at'][:10],
                   'claim_ids':[a['id'] for a in judgment.get('arguments',[]) if a.get('adoption')=='adopted']}
    return D.extract_case_context(judgment,application)


@contextmanager
def use_context(context):
    token = _CONTEXT.set(copy.deepcopy(context))
    try: yield context
    finally: _CONTEXT.reset(token)


def current_context():
    return _CONTEXT.get()


def evaluate_adjustment(context, code, side, confidence=None, params=None):
    """Only a supplied, frozen context can affect confidence; no live I/O here."""
    import condition_contract as CC
    policy = context.get('policy') or {}
    result = {'rule':'validated_conditional','amount':0.0,'applied':False,
              'policy_revision':policy.get('policy_revision'),'reason':context.get('reason','inactive')}
    if policy.get('status') != 'active' or context.get('status') != 'recorded': return result
    if context.get('code') != code or context.get('side') != side:
        return {**result,'reason':'JUDGMENT_CONTEXT_MISMATCH'}
    if confidence is not None and context.get('confidence_raw') != confidence:
        return {**result,'reason':'JUDGMENT_RAW_CONFIDENCE_MISMATCH'}
    if params is not None and semantic_params(params) != semantic_params(policy.get('params') or {}):
        return {**result,'reason':'PINNED_PARAMS_MISMATCH'}
    package = policy.get('package') or {}
    effect = package.get('effect') or {}
    if effect.get('operation') != 'add_confidence': return {**result,'reason':'not_confidence_policy'}
    try:
        scope = package.get('scope') or {}
        if '*' not in scope.get('codes',[]) and code not in scope.get('codes',[]):
            return {**result,'reason':'code_outside_scope'}
        if side not in scope.get('confidence_for',[]): return {**result,'reason':'direction_outside_scope'}
        fields = {'regimes':'features.regime','price_kinds':'stratum.price_kind','flow_methods':'stratum.flow_method',
                  'admission_routes':'stratum.admission_route','instrument_kinds':'stratum.instrument_kind'}
        for key,field in fields.items():
            if key in scope:
                value = CC._field(context['inputs'],field)
                if value is None: return {**result,'reason':'scope_unknown:'+field}
                if value not in scope[key]: return {**result,'reason':'scope_outside:'+field}
        outcome = CC.evaluate_condition(package['condition'],context['inputs'],stage='after_judgment')
        amount = effect['amount'] if outcome is True else 0.0
        if effect.get('unit') != 'confidence' or effect.get('placement') != 'after_legacy_boost_before_cap':
            raise ValueError('INVALID_EFFECT_PLACEMENT')
        return {**result,'amount':amount,'applied':outcome is True,'condition_result':outcome,
                'condition_version':CC.VERSION,'reason':'applied' if outcome is True else 'condition_unknown' if outcome is None else 'condition_false'}
    except Exception as exc:
        return {**result,'reason':'condition_error:'+str(exc)}


class LessonBackend:
    def __init__(self, policy): self.policy = copy.deepcopy(policy)

    def list_lessons(self, as_of):
        # snapshot already checks exact current definitions, settings and pointer.
        p = self.policy
        if p['status'] == 'recording_error':
            raise StoreError(p.get('reason','POLICY_PROOF_UNAVAILABLE'))
        return {'lessons':copy.deepcopy(p['package'].get('lessons',[])) if p['status']=='active' else [],
                'display_limit':p['package'].get('display_limit',5) if p.get('package') else 5,
                'policy_status':p['status'],'policy_revision':p.get('policy_revision')}

    def verify_adoption(self, lesson, as_of):
        p = self.policy; package = p.get('package') or {}
        if p.get('status') != 'active' or lesson not in package.get('lessons',[]):
            raise ValueError('LESSON_NOT_IN_ACTIVE_PACKAGE')
        base = {'definition_hash':package['baseline_ref']['definition_hash'],
                'settings_hash':package['deployed_settings_hash']}
        return {'schema_version':'LESSON_ADOPTION_PROOF@v1','status':'verified','verifier_id':VERSION,
                'validation_id':package['validation_id'],'adoption_id':package['adoption_id'],
                'lesson_version_id':lesson['lesson_version_id'],'proposal_id':lesson['proposal_id'],
                'proposal_version':lesson['proposal_version'],'lesson_hash':digest(lesson),
                'base_ref':base,'current_base_ref':base,'base_status':'current',
                'adopted_at':package['adopted_at'],'verified_at':instant(as_of).isoformat(),
                'activation_id':p['activation_id'],'package_hash':digest(package)}


def lesson_backend():
    if not C.params().get(MARKER): return None
    return LessonBackend(snapshot())


def check_plan_revision(plan_id):
    import execution_trace as E
    import tuning
    # Compare to the on-disk authority, even inside a pinned order context.
    raw = C.read_json(tuning.PARAMS_PATH)
    current = snapshot(raw)
    if not plan_id and not current.get('policy_revision'): return None
    try:
        plan = E.load_plan(plan_id) or {}
        original = (plan.get('inputs') or {}).get('policy')
        if original is None and current.get('policy_revision') is None: return None
        if (isinstance(original,dict) and original.get('revision') == current['revision']
                and original.get('status') == current['status']
                and original.get('policy_revision') == current.get('policy_revision')): return None
        return {'ok':False,'error':'STALE_POLICY_PLAN','reason':'정책이 변경되어 새 주문 계획이 필요합니다.',
                'plan_id':plan_id,'expected_revision':original.get('revision') if isinstance(original,dict) else None,
                'current_revision':current['revision']}
    except Exception as exc:
        return {'ok':False,'error':'POLICY_REVISION_UNCONFIRMED','reason':str(exc),'plan_id':plan_id}
