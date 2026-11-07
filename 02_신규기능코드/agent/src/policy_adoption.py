"""Operator-only adoption of completed step-7 trials.

Preparing a package writes research records only. Adoption commits one atomic
params document containing both numeric values and the package pointer. Packages
and activation intents are published first; a lost response is recovered from
that pointer, never by applying the effect a second time.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

import common as C
import condition_contract as CC
import policy_registry as S

VERSION = 'MANUAL_POLICY_ADOPTION@v1'


def _operator(operator):
    if os.environ.get('RUN_MODE','') not in ('','operator'):
        raise ValueError('MANUAL_OPERATOR_ONLY')
    if not isinstance(operator,str) or not operator.strip() or len(operator)>200:
        raise ValueError('OPERATOR_REQUIRED')
    return operator.strip()


def _key(key):
    if not isinstance(key,str) or not key.strip() or len(key)>200: raise ValueError('REQUEST_KEY_REQUIRED')
    return key


def _read_params():
    import tuning
    return json.loads(Path(tuning.PARAMS_PATH).read_text(encoding='utf-8'))


def _replace_params(params):
    import tuning
    target = Path(tuning.PARAMS_PATH)
    fd, temporary = tempfile.mkstemp(prefix='.policy-',suffix='.tmp',dir=target.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(params,stream,ensure_ascii=False,indent=2,allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,target)
        if os.name != 'nt':
            descriptor = os.open(target.parent,os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _validated_result(validation_id):
    import validation_store as V
    import validation_spec as Q
    import policy_validation as P
    manifest = Q.load_spec(validation_id)
    view = P.get_policy_validation(validation_id)
    result_id = view.get('result_id',validation_id)
    result, commit = V.load('results',result_id), V.load('result_commits',result_id)
    if (not result or not commit or commit.get('result_hash') != V.digest(result)
            or result.get('validation_id') != validation_id
            or view.get('validation_result_hash') != V.digest(result)):
        raise ValueError('VALIDATION_RESULT_NOT_COMMITTED')
    checks = [('baseline_status',result.get('baseline_status')=='passed'),
              ('effect_status',result.get('effect_status')=='complete'),
              ('performance',(result.get('performance') or {}).get('status')=='complete'),
              ('criteria',(result.get('criteria') or {}).get('status')=='passed'),
              ('independence',(result.get('independent_validation') or {}).get('status')=='verified'),
              ('eligibility',(result.get('adoption_eligibility') or {}).get('status')=='eligible'),
              ('registered_criteria',manifest.get('criteria_complete') is True)]
    failed = [name for name,ok in checks if not ok]
    if failed: raise ValueError('VALIDATION_NOT_ELIGIBLE:'+','.join(failed))
    if result.get('proposal') != manifest['proposal_snapshot'] or result.get('spec') != manifest['spec']:
        raise ValueError('VALIDATION_SOURCE_SNAPSHOT_MISMATCH')
    return manifest,result


def _candidate(manifest, base):
    proposal = manifest['proposal_snapshot']
    effect = proposal.get('effect') or {}
    candidate = S.semantic_params(base['params'])
    target = proposal.get('target_snapshot') or {}
    import tuning
    if effect.get('operation') == 'set_parameter':
        key = target.get('key')
        limits = base['tuning']['keys'].get(key)
        if not limits or target.get('operation') != 'set_parameter': raise ValueError('UNREGISTERED_PARAMETER')
        value, old = effect.get('value'),tuning.get_value(candidate,key)
        if (type(value) not in (int,float) or not limits['min'] <= value <= limits['max']
                or abs(value-old) > limits['step']+1e-12 or limits.get('int') and type(value) is not int
                or effect.get('unit') != target.get('unit')):
            raise ValueError('PARAMETER_RANGE_OR_UNIT')
        section,leaf = key.split('.',1); candidate[section][leaf] = value
        if tuning.consistency(candidate): raise ValueError('PARAMETER_CONSISTENCY')
    elif effect.get('operation') == 'add_confidence':
        amount = effect.get('amount')
        if (type(amount) not in (int,float) or not -1 <= amount <= 1
                or effect.get('unit') != 'confidence' or effect.get('placement') != 'after_legacy_boost_before_cap'):
            raise ValueError('INVALID_CONFIDENCE_EFFECT')
    elif effect.get('operation') == 'prompt_guidance':
        if not isinstance(effect.get('text'),str) or not effect['text'].strip(): raise ValueError('LESSON_TEXT_REQUIRED')
    else: raise ValueError('UNSUPPORTED_POLICY_EFFECT')
    if CC.validate_condition(proposal['condition'],stage='briefing' if effect['operation']=='prompt_guidance' else 'after_judgment') != 'bool':
        raise ValueError('BOOLEAN_CONDITION_REQUIRED')
    return candidate


def _scope(value, fallback):
    result = copy.deepcopy(fallback if value is None else value)
    allowed = {'codes','confidence_for','regimes','price_kinds','flow_methods','admission_routes','instrument_kinds'}
    if (not isinstance(result,dict) or set(result)-allowed
            or any(not isinstance(v,list) or not v or any(not isinstance(x,str) or not x for x in v) for v in result.values())
            or not result.get('codes') or not result.get('confidence_for')
            or not set(result['confidence_for']) <= {'매수','매도'}): raise ValueError('INVALID_ACTIVATION_SCOPE')
    # An approval may narrow the predeclared validation scope, never widen it.
    for field in ('codes','confidence_for'):
        if '*' not in fallback[field] and not set(result[field]) <= set(fallback[field]):
            raise ValueError('ACTIVATION_SCOPE_EXCEEDS_VALIDATION')
    return result


def prepare_adoption(validation_id, request_key, *, operator, effective_from, effective_until,
                     scope=None, priority=0, display_limit=5):
    """Create the concrete review artifact without changing runtime policy."""
    identifier = None
    try:
        operator = _operator(operator); _key(request_key)
        request = {'validation_id':validation_id,'effective_from':effective_from,'effective_until':effective_until,
                   'scope':scope,'priority':priority,'display_limit':display_limit,'operator':operator}
        request_hash = S.digest(request)
        identifier = 'adoption-'+S.digest([validation_id,request_key])[:40]
        with S.change_lock():
            old = S.load('adoptions',identifier)
            if old:
                if old['request_hash'] != request_hash: raise ValueError('IDEMPOTENCY_CONFLICT')
                return {**old,'replayed':True}
            start,end = S.instant(effective_from),S.instant(effective_until)
            if not start < end or end <= C.now(): raise ValueError('INVALID_EFFECTIVE_PERIOD')
            if type(priority) is not int or type(display_limit) is not int or not 1 <= display_limit <= 50:
                raise ValueError('INVALID_DISPLAY_POLICY')
            manifest,result = _validated_result(validation_id)
            base = S._current_base()
            frozen = manifest['source_bundle']['base_snapshot']
            if any(base[k] != frozen[k] for k in ('definition_hash','settings_hash')):
                raise ValueError('STALE_BASE')
            before = _read_params()
            if S.semantic_params(before) != base['params']: raise ValueError('CURRENT_PARAMS_SOURCE_MISMATCH')
            if frozen.get('policy_base_ref') != before.get(S.MARKER):
                raise ValueError('STALE_POLICY_BASE')
            if base.get('policy_base_ref') != before.get(S.MARKER):
                raise ValueError('CURRENT_POLICY_SOURCE_MISMATCH')
            candidate = _candidate(manifest,base)
            proposal = manifest['proposal_snapshot']
            adopted_scope = _scope(scope,manifest['spec']['scope'])
            if proposal['effect']['operation'] == 'set_parameter' and (
                    adopted_scope['codes'] != ['*'] or set(adopted_scope['confidence_for']) != {'매수','매도'}
                    or set(adopted_scope) != {'codes','confidence_for'}):
                # params.json is a global authority, not a scoped overlay. A
                # narrow study cannot authorize changes outside its population.
                raise ValueError('GLOBAL_PARAMETER_REQUIRES_GLOBAL_VALIDATION_SCOPE')
            baseline_ref = {k:base[k] for k in ('definition_hash','settings_hash')}
            candidate_settings_hash = S.digest({'params':candidate,'risk':base['risk'],'tuning':base['tuning']})
            row = {'adoption_id':identifier,'version':VERSION,'validation_id':validation_id,
                   'validation_result_id':result.get('result_id',validation_id),'validation_result_hash':S.digest(result),
                   'request_key':request_key,'request_hash':request_hash,'operator':operator,'prepared_at':C.now().isoformat(),
                   'expected_revision':S.revision(before),'before_params':before,'candidate_params':candidate,
                   'baseline_policy_ref':copy.deepcopy(before.get(S.MARKER)),
                   'baseline_ref':baseline_ref,'candidate_settings_hash':candidate_settings_hash,
                   'baseline_snapshot_hash':S.digest({'base_ref':baseline_ref,'params':S.semantic_params(before)}),
                   'candidate_policy_hash':S.digest({'params':candidate,'condition':proposal['condition'],
                                                   'effect':proposal['effect'],'scope':adopted_scope}),
                   'proposal':copy.deepcopy(proposal),'scope':adopted_scope,'priority':priority,'display_limit':display_limit,
                   'effective_from':start.isoformat(),'effective_until':end.isoformat(),
                   'status':'prepared','operational_activation':False}
            S.archive('adoptions',identifier,row)
        return row
    except (ValueError,KeyError,TypeError) as exc:
        return {'adoption_id':identifier,'status':'rejected','error':str(exc)}
    except Exception as exc:
        return {'adoption_id':identifier,'status':'recording_error','error':str(exc)}


def _package(prepared, activation_id, operator):
    proposal = prepared['proposal']
    package = {'package_id':'policy-'+activation_id.removeprefix('activation-'),'version':S.VERSION,
               'adoption_id':prepared['adoption_id'],'validation_id':prepared['validation_id'],
               'validation_result_id':prepared['validation_result_id'],
               'validation_result_hash':prepared['validation_result_hash'],'operator':operator,
               'adopted_at':C.now().isoformat(),'baseline_ref':prepared['baseline_ref'],
               'deployed_settings_hash':prepared['candidate_settings_hash'],
               'baseline_snapshot_hash':prepared['baseline_snapshot_hash'],'candidate_policy_hash':prepared['candidate_policy_hash'],
               'condition_version':CC.VERSION,'condition':proposal['condition'],'effect':proposal['effect'],
               'baseline_policy_ref':prepared.get('baseline_policy_ref'),
               'replacement_semantics':'replace_optional_policy_retain_numeric_base',
               'scope':prepared['scope'],'effective_from':prepared['effective_from'],'effective_until':prepared['effective_until'],
               'display_limit':prepared['display_limit'],'lessons':[]}
    if proposal['effect']['operation']=='prompt_guidance':
        package['lessons'] = [{'schema_version':'ADOPTED_LESSON@v1','lesson_version_id':'lesson-'+activation_id,
            'proposal_id':proposal['proposal_id'],'proposal_version':proposal['version'],'text':proposal['effect']['text'],
            'source_case_ids':list(dict.fromkeys(proposal.get('support_case_ids',[])+proposal.get('counter_case_ids',[]))),
            'source_refs':proposal['source_refs'],'scope':prepared['scope'],'condition':proposal['condition'],
            'priority':prepared['priority'],'effective_from':prepared['effective_from'],'effective_until':prepared['effective_until'],
            'base_ref':{'definition_hash':prepared['baseline_ref']['definition_hash'],'settings_hash':prepared['candidate_settings_hash']}}]
    return package


def _commit_result(intent):
    result = {'activation_id':intent['activation_id'],'adoption_id':intent.get('adoption_id'),
              'package_id':intent.get('package_id'),'status':intent['result_status'],
              'revision':S.revision(intent['after_params']),'previous_revision':S.revision(intent['before_params']),
              'deployed_snapshot_hash':S.digest(intent['after_params']),'request_hash':intent['request_hash'],
              'operator':intent['operator'],'committed_at':intent['created_at']}
    S.archive('activation_commits',intent['activation_id'],result)
    return result


def _recover(identifier, request_hash):
    old = S.load('activation_intents',identifier)
    if not old: return None
    if old['request_hash'] != request_hash: raise ValueError('IDEMPOTENCY_CONFLICT')
    committed = S.load('activation_commits',identifier)
    if committed:
        current = _read_params()
        return {**committed,'replayed':True,'pointer_still_current':current==old['after_params'],
                'current_revision':S.revision(current)}
    current = _read_params()
    if current == old['after_params']:
        return {**_commit_result(old),'replayed':True,'recovered_response':True}
    if current != old['before_params']: raise ValueError('STALE_REVISION')
    return old


def adopt_policy(adoption_id, expected_revision, request_key, *, operator):
    identifier = None
    try:
        operator = _operator(operator); _key(request_key)
        identifier = 'activation-'+S.digest([adoption_id,request_key])[:40]
        request_hash = S.digest([adoption_id,expected_revision,request_key,operator])
        with S.change_lock():
            old = _recover(identifier,request_hash)
            if old and 'after_params' not in old: return old
            prepared = S.load('adoptions',adoption_id)
            if not prepared: raise ValueError('PREPARATION_REQUIRED')
            current = _read_params()
            if S.revision(current) != expected_revision or prepared['expected_revision'] != expected_revision:
                raise ValueError('STALE_REVISION')
            if not S.instant(prepared['effective_from']) <= C.now() < S.instant(prepared['effective_until']):
                raise ValueError('OUTSIDE_EFFECTIVE_PERIOD')
            manifest,result = _validated_result(prepared['validation_id'])
            if S.digest(result) != prepared['validation_result_hash']: raise ValueError('VALIDATION_RESULT_CHANGED')
            base = S._current_base()
            if {k:base[k] for k in ('definition_hash','settings_hash')} != prepared['baseline_ref']:
                raise ValueError('STALE_BASE')
            package = S.load('packages',old['package_id']) if old else _package(prepared,identifier,operator)
            S.archive('packages',package['package_id'],package)
            after = copy.deepcopy(prepared['candidate_params'])
            after[S.MARKER] = {'status':'active','package_id':package['package_id'],
                               'package_hash':S.digest(package),'activation_id':identifier}
            intent = old or {'activation_id':identifier,'adoption_id':adoption_id,'package_id':package['package_id'],
                   'package_hash':S.digest(package),'candidate_params_hash':S.digest(S.semantic_params(after)),
                   'before_params':current,'after_params':after,'request_hash':request_hash,
                   'result_status':'active','operator':operator,'created_at':C.now().isoformat()}
            S.archive('activation_intents',identifier,intent)
            _replace_params(intent['after_params'])
            return _commit_result(intent)
    except (ValueError,KeyError,TypeError) as exc:
        return {'activation_id':identifier,'status':'rejected','error':str(exc)}
    except Exception as exc:
        return {'activation_id':identifier,'status':'recording_error','error':str(exc)}


def _end_policy(operation, expected_revision, request_key, operator, reason, activation_id=None):
    identifier = None
    try:
        operator = _operator(operator); _key(request_key)
        if not isinstance(reason,str) or not reason.strip(): raise ValueError('REASON_REQUIRED')
        identifier = operation+'-'+S.digest([request_key,expected_revision,activation_id])[:40]
        request_hash = S.digest([operation,expected_revision,request_key,operator,reason,activation_id])
        with S.change_lock():
            old = _recover(identifier,request_hash)
            if old and 'after_params' not in old: return old
            current = _read_params()
            if S.revision(current) != expected_revision: raise ValueError('STALE_REVISION')
            marker = current.get(S.MARKER)
            if not isinstance(marker,dict): raise ValueError('NO_POLICY_TO_END')
            if operation == 'rollback':
                previous = S.load('activation_intents',activation_id)
                if (not previous or previous['after_params'] != current
                        or marker.get('activation_id') != activation_id):
                    raise ValueError('ROLLBACK_WOULD_OVERWRITE_LATER_CHANGES')
                after = copy.deepcopy(previous['before_params'])
                # Restore numeric base without resurrecting an older policy.
                after[S.MARKER] = {'status':'rolled_back','activation_id':identifier,
                                   'previous_package_id':marker.get('package_id'),'reason':reason}
            else:
                after = copy.deepcopy(current)
                after[S.MARKER] = {**marker,'status':'revoked','reason':reason,'revoked_at':C.now().isoformat()}
            intent = old or {'activation_id':identifier,'adoption_id':None,'package_id':marker.get('package_id'),
                   'before_params':current,'after_params':after,'request_hash':request_hash,
                   'result_status':'rolled_back' if operation=='rollback' else 'revoked',
                   'operator':operator,'reason':reason,'created_at':C.now().isoformat()}
            S.archive('activation_intents',identifier,intent)
            _replace_params(intent['after_params'])
            return _commit_result(intent)
    except (ValueError,KeyError,TypeError) as exc:
        return {'activation_id':identifier,'status':'rejected','error':str(exc)}
    except Exception as exc:
        return {'activation_id':identifier,'status':'recording_error','error':str(exc)}


def revoke_policy(expected_revision, request_key, *, operator, reason):
    return _end_policy('revoke',expected_revision,request_key,operator,reason)


def rollback_policy(activation_id, expected_revision, request_key, *, operator, reason):
    return _end_policy('rollback',expected_revision,request_key,operator,reason,activation_id)
