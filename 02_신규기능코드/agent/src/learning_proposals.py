"""Step 6: immutable, evidence-bound proposals. This module cannot apply policy.

A structurally accepted proposal is a stage-7 test candidate, never an approved
lesson. The only writes are research records in independent stores.
"""
from __future__ import annotations

import copy
import math
import uuid
from datetime import datetime
from pathlib import Path

import common as C
import case_store as CS
import proposal_store as S

VERSION = 'LEARNING_PROPOSALS@v1'
TARGET_PATH = Path(__file__).resolve().parents[1] / 'config/ontology/proposal_targets.json'
KINDS = {'lesson_candidate', 'parameter_change', 'conditional_rule_change', 'retain_current'}


def now():
    return C.now().isoformat()


def instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return result.astimezone(C.KST)


def text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 8000:
        raise ValueError('SCHEMA_ERROR:' + field)
    return value.strip()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('FINITE_NUMBER_REQUIRED')
    return value


def target_snapshot():
    """Registry selects code, never user-supplied paths. Current values are a base,
    not a repair of missing historical values in source cases.
    """
    import json
    import ast
    import tuning
    registry = json.loads(TARGET_PATH.read_text(encoding='utf-8'))
    agent = Path(__file__).resolve().parents[1]
    sources = {name: (agent/'src'/name).read_text(encoding='utf-8') for name in registry['engine_sources']}
    prompts = {name: (agent/name).read_text(encoding='utf-8') for name in registry['prompt_sources']}
    params, risk, rules = copy.deepcopy(C.params()), C.risk_limits(), tuning.rules()
    # The active revision pointer is metadata, not a numeric engine setting.
    policy_base_ref = params.pop('_ontology_policy', None)
    definition_hash = S.digest({'sources': sources, 'prompts': prompts, 'registry': registry})
    settings_hash = S.digest({'params': params, 'risk': risk, 'tuning': rules})
    targets = copy.deepcopy(registry['targets'])
    bindings=registry.get('parameter_bindings',{})
    source_functions={name:{n.name for n in ast.walk(ast.parse(body)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
                      for name,body in sources.items()}
    for key, limits in sorted(rules['keys'].items()):
        binding=bindings.get(key)
        if not isinstance(binding,dict):
            raise ValueError('UNREGISTERED_PARAMETER_TARGET:'+key)
        if (binding.get('unit') not in ('confidence','ratio','trading_days','weekday_count','score_points')
                or not isinstance(binding.get('call_stage'),str) or not binding['call_stage']
                or not isinstance(binding.get('meaning'),str) or not binding['meaning']
                or not isinstance(binding.get('consumers'),list) or not binding['consumers']
                or not isinstance(binding.get('replay_requirements'),list)
                or any(x not in ('original_complete_screening_pool_required',
                                 'original_position_initialization_and_updates_required',
                                 'original_monitor_trigger_and_price_sequence_required')
                       for x in binding['replay_requirements'])):
            raise ValueError('INVALID_PARAMETER_BINDING:'+key)
        for consumer in binding['consumers']:
            if (not isinstance(consumer,dict) or set(consumer)!={'file','function','stage'}
                    or not consumer.get('stage')
                    or consumer.get('function') not in source_functions.get(consumer.get('file'),set())):
                raise ValueError('PARAMETER_CONSUMER_SOURCE_MISSING:'+key)
        current=number(tuning.get_value(params,key))
        targets.append({**copy.deepcopy(binding), 'target_id': 'parameter.' + key, 'rule_id': key, 'version': 'legacy_parameter',
                        'kinds': ['parameter_change'], 'operation': 'set_parameter',
                        'validation_kind': 'deterministic_rule', 'key': key, 'limits': limits,
                        'value': current})
    for target in targets:
        target['target_ref'] = {key: target[key] for key in ('target_id','rule_id','version','call_stage')}
        target['target_ref'].update(definition_hash=definition_hash, settings_hash=settings_hash)
    return {'definition_hash': definition_hash, 'settings_hash': settings_hash, 'registry': registry,
            'sources': sources, 'prompts': prompts, 'params': params, 'risk': risk, 'tuning': rules,
            'policy_base_ref':copy.deepcopy(policy_base_ref),
            'targets': targets}


def _load_context(run_id):
    context = S.load('contexts', run_id)
    commit = S.load('context_commits', run_id)
    if not context or not commit or commit.get('context_hash') != S.digest(context):
        raise ValueError('CONTEXT_NOT_COMMITTED')
    return context


def build_context(review_id, revision=None):
    """Freeze a completed comparison and a separate proposed-policy base."""
    import case_review as R
    import research_evidence as E
    if not isinstance(review_id, str) or not review_id:
        raise ValueError('REVIEW_ID_REQUIRED')
    if revision is not None:
        text(revision, 'revision')
    run_id = 'proposal-run-' + S.digest([VERSION, review_id, revision])[:40]
    try:
        if S.load('runs', run_id):
            return get_learning_proposals(run_id)
        with S.locked('context-build', timeout=0):
            if S.load('runs', run_id):
                return get_learning_proposals(run_id)
            S.archive('runs', run_id, {'proposal_run_id':run_id, 'review_id':review_id,
                       'revision':revision, 'created_at':now(), 'version':VERSION})
            status = R.get_case_review(review_id)
            result, dataset = CS.load('results',review_id), CS.load('datasets',review_id)
            if (status.get('analysis_status') != 'complete' or status.get('recording_status') != 'recorded'
                    or status.get('target_set_incomplete') or not result or not dataset
                    or not CS.load('publication_commits',review_id)):
                S.archive('context_failures',run_id, {'proposal_run_id':run_id, 'status':'source_not_ready',
                          'created_at':now(), 'source_status':status.get('run_status')})
                return get_learning_proposals(run_id)
            import validation_holdout as VH
            _, withheld = VH.filter_cases(dataset['cases'])
            if withheld:
                raise ValueError('SOURCE_REVIEW_CONTAINS_RESERVED_HOLDOUT_REBUILD_REQUIRED')
            case_ids = [c['application_id'] for c in dataset['cases']]
            refs = [{'review_id':review_id, 'result_hash':CS.digest(result), 'dataset_hash':CS.digest(dataset)}]
            # Reserve before exposing or processing case data. A failed worker
            # cannot turn viewed research into a clean stage-7 holdout.
            E.reserve_usage(run_id, case_ids, source_refs=refs, purpose='step6_input_reservation')
            base = target_snapshot()
            catalog = E.build_catalog(review_id,result,dataset)
            runtime_inputs = {}
            import runtime_inputs as RI
            for case in dataset['cases']:
                attempt = (case.get('judgment_snapshot') or {}).get('run_attempt_id')
                if not attempt or attempt in runtime_inputs:
                    continue
                try:
                    snapshot = RI.load_attempt(attempt)
                    if snapshot and (instant(snapshot['created_at']) > instant(case['origin_at'])
                                     or instant(snapshot['created_at']) > instant(result['knowledge_as_of'])):
                        raise ValueError('ORIGINAL_AI_INPUT_NOT_AVAILABLE_AT_ORIGIN')
                    runtime_inputs[attempt] = {'status':'recorded' if snapshot else 'unrecorded', 'snapshot':snapshot}
                except (OSError, ValueError, KeyError) as exc:
                    runtime_inputs[attempt] = {'status':'recording_error', 'reason':str(exc)}
            previous = S.records('runs')
            if previous['errors']:
                raise ValueError('PRIOR_PROPOSAL_RUN_HISTORY_CORRUPT')
            related = [r for r in previous['records'] if r['review_id'] == review_id and r['proposal_run_id'] != run_id]
            context = {'proposal_run_id':run_id, 'review_id':review_id, 'version':VERSION,
                       'created_at':now(), 'source_refs':refs, 'result_hash':CS.digest(result),
                       'market_as_of':result['market_as_of'], 'knowledge_as_of':result['knowledge_as_of'],
                       'cases':copy.deepcopy(dataset['cases']), 'source_definitions':copy.deepcopy(dataset.get('source_definitions', {})),
                       'runtime_inputs':runtime_inputs,
                       'comparison':copy.deepcopy(result.get('comparison', {})), 'catalog':catalog,
                       'source_ai_hypotheses':[copy.deepcopy(s) for s in status.get('ai_submissions',[]) if s.get('status')=='accepted'],
                       'base':base, 'supersedes_run_id':max(related,key=lambda x:x['created_at'])['proposal_run_id'] if revision and related else None,
                       'interpretation':'server_observations_and_unverified_AI_explanations_are_separate'}
            S.archive('contexts',run_id,context)
            E.record_usage(run_id,case_ids,'step6_context_frozen',source_refs=refs)
            S.archive('context_commits',run_id, {'proposal_run_id':run_id,'context_hash':S.digest(context),'created_at':now()})
        return get_learning_proposals(run_id)
    except S.StoreBusy:
        return {**get_learning_proposals(run_id), 'worker_status':'busy'}
    except Exception as exc:
        error=S.record_error(run_id,'build_context',exc)
        return {**get_learning_proposals(run_id),'run_status':'recording_error','errors':[error]}


SUBMISSION_SCHEMA = {
 'version':VERSION, 'kinds':sorted(KINDS),
 'common_fields':['item_id','kind','title','delivery_ids','fact_claims','stratum_ids','condition','reason',
                  'expected_change','validation_kind','validation_question','support_case_ids','counter_case_ids'],
 'optional_fields':['target_ref','effect','reason_kind','supersedes_proposal_id'],
 'fact_claim':{'id':'f1','reference':{'review_id':'from_catalog','result_hash':'from_catalog','metric_id':'from_catalog'},
               'op':'value','value':'actual value','unit':'from_catalog'},
 'condition_example':{'op':'gt','left':{'field':'features.gap'},'right':{'value':0,'unit':'ratio'}},
 'condition_policy':'only_registered_original_decision_fields; three_valued_missing; never_outcomes',
 'effect_types':['set_parameter','add_confidence','prompt_guidance'],
 'validation_kinds':['deterministic_rule','prompt_lesson','none'],
 'acceptance_meaning':'test_candidate_not_approved_or_active',
}


def _deliver(context, payload, *, catalog=None, case_ids=None):
    import research_evidence as E
    import evidence_wire as W
    W.pin_format()
    run_id=context['proposal_run_id']; did='proposal-delivery-'+uuid.uuid4().hex
    ids=list(case_ids or [])
    E.reserve_usage(run_id,ids,source_refs=context['source_refs'],purpose='step6_delivery_reservation')
    prepared = None
    if W.enabled():
        prepared, catalog = W.prepare_research(payload, catalog or [])
        payload = prepared['payload']
    body={**copy.deepcopy(payload),'proposal_run_id':run_id,'delivery_id':did,'created_at':now()}
    if prepared:
        body.pop('delivery_id')
    receipt={'delivery_id':did,'proposal_run_id':run_id,'created_at':body['created_at'],
             'body':body,'body_hash':S.digest(body),'catalog':copy.deepcopy(catalog or []),'case_ids':ids,
             'source_refs':context['source_refs']}
    if prepared:
        receipt.update(W.receipt_fields(prepared))
    S.archive('deliveries',did,receipt)
    E.record_usage(run_id,ids,'step6_delivered',source_refs=[*context['source_refs'],{'delivery_id':did,'body_hash':receipt['body_hash']}])
    S.archive('delivery_commits',did,{'delivery_id':did,'proposal_run_id':run_id,
                                    'receipt_hash':S.digest(receipt),'created_at':now()})
    if prepared:
        return W.commit(prepared, S.load('deliveries', did), catalog_key='catalog', output=body, case_ids=ids)
    return body


def get_proposal_context(proposal_run_id):
    try:
        context=_load_context(proposal_run_id)
        catalog=[x for x in context['catalog'] if not x.get('application_id')]
        payload={'review_id':context['review_id'],'source_refs':context['source_refs'],
                 'market_as_of':context['market_as_of'],'knowledge_as_of':context['knowledge_as_of'],
                 'comparison':context['comparison'],'source_ai_hypotheses':context['source_ai_hypotheses'],
                 'catalog':catalog,'targets':context['base']['targets'],
                 'strata':list({c.get('stratum_id'):c.get('stratum',{}) for c in context['cases'] if c.get('stratum_id')}.items()),
                 'condition_fields':context['base']['registry']['condition_fields'],
                 'schema':SUBMISSION_SCHEMA, 'case_count':len(context['cases']), 'recording_status':'recorded'}
        # Summary metrics can mention IDs, but only case pages count as delivered
        # original case evidence. This prevents citation of unseen pages.
        return _deliver(context,payload,catalog=catalog)
    except Exception as exc:
        return {'proposal_run_id':proposal_run_id,'recording_status':'recording_error',
                'errors':[S.record_error(proposal_run_id,'get_proposal_context',exc)]}


def get_proposal_cases(proposal_run_id,offset=0,limit=50,cursor=None):
    if isinstance(offset,bool) or not isinstance(offset,int) or offset<0 or isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=100:
        return {'status':'rejected','errors':[{'code':'INVALID_PAGINATION'}]}
    try:
        context=_load_context(proposal_run_id)
        import record_store, storage_objects
        page=record_store.sequence_page(context['cases'],scope='proposal:'+proposal_run_id,cursor=cursor,limit=limit,offset=offset,budget=512*1024)
        cases=page.pop('items')
        ids=[c['application_id'] for c in cases if c.get('application_id')]
        catalog=[x for x in context['catalog'] if x.get('application_id') in ids]
        if storage_objects.logical_size(catalog)>400*1024:
            catalog=[];ids=[];page['catalog_complete']=False
        return _deliver(context,{**page,'cases':cases,'catalog':catalog,'offset':offset,'total':len(context['cases']),'recording_status':'recorded'},
                        catalog=catalog,case_ids=ids)
    except Exception as exc:
        return {'proposal_run_id':proposal_run_id,'recording_status':'recording_error',
                'errors':[S.record_error(proposal_run_id,'get_proposal_cases',exc)]}


def read_proposal_context(proposal_run_id):
    """Read the frozen context without impersonating a new AI delivery.

    Presentation still requires the caller's research usage reservation. The
    immutable original delivery history is not extended by browser refreshes.
    """
    try:
        context = _load_context(proposal_run_id)
        import validation_holdout as VH
        _, withheld = VH.filter_cases(context.get('cases', []))
        if withheld:
            return {'proposal_run_id': proposal_run_id, 'visibility': 'protected',
                    'recording_status': 'withheld', 'reason': 'prospective_validation_reserved'}
        return {'proposal_run_id': proposal_run_id, 'recording_status': 'recorded',
                'visibility': 'available', 'context': copy.deepcopy(context), 'context_hash': S.digest(context)}
    except Exception as exc:
        return {'proposal_run_id': proposal_run_id, 'recording_status': 'recording_error',
                'errors': [{'operation': 'read_proposal_context', 'error': str(exc)}]}


def _field(case,path):
    value=case
    for part in path.split('.'):
        if not isinstance(value,dict) or part not in value: return None
        value=value[part]
    return value


def validate_condition(expr, fields, depth=0):
    """Compatibility entry point; analysis and runtime share one typed AST."""
    from condition_contract import validate_condition as validate
    return validate(expr, fields=fields, depth=depth)


def condition_value(expr, case):
    from condition_contract import evaluate_condition
    return evaluate_condition(expr, case)


def _cohort(context,item):
    ids=item.get('stratum_ids')
    if not isinstance(ids,list) or not ids or any(not isinstance(x,str) for x in ids): raise ValueError('STRATUM_REQUIRED')
    if len(set(ids)) != 1: raise ValueError('ONE_COMPARABLE_STRATUM_REQUIRED')
    available={c.get('stratum_id') for c in context['cases']}
    if not set(ids)<=available: raise ValueError('UNKNOWN_STRATUM')
    expr=item.get('condition')
    if validate_condition(expr,context['base']['registry']['condition_fields'])!='bool': raise ValueError('BOOLEAN_CONDITION_REQUIRED')
    matched,unknown,outside=[],[],[]
    for case in context['cases']:
        if case.get('stratum_id') not in ids: continue
        value=condition_value(expr,case)
        (matched if value is True else unknown if value is None else outside).append(case)
    complete=[c for c in matched if c.get('outcome_eligible') is True and type(c.get('label')) is int and c['label'] in (0,1)]
    refuted=[c['application_id'] for c in complete if c['label']==1]
    maintained=[c['application_id'] for c in complete if c['label']==0]
    return {'matched_case_ids':[c['application_id'] for c in matched], 'unknown_condition_case_ids':[c['application_id'] for c in unknown],
            'outside_condition_case_ids':[c['application_id'] for c in outside], 'refuted_case_ids':refuted,'maintained_case_ids':maintained,
            'incomplete_outcome_case_ids':[c['application_id'] for c in matched if c not in complete],
            'case_count':len(matched),'complete_count':len(complete),'refutation_rate':len(refuted)/len(complete) if complete else None,
            'counterevidence_policy':'both_refuted_and_maintained_are_always_retained',
            'measurement_caveats':sorted({c.get('signed_flows',{}).get('interpretation','unknown') for c in matched})}


def _delivery_scope(context,item):
    delivery_ids=item.get('delivery_ids')
    if not isinstance(delivery_ids,list) or not delivery_ids: raise ValueError('DELIVERY_REQUIRED')
    catalog,case_ids=[],set()
    for did in dict.fromkeys(delivery_ids):
        receipt=S.load('deliveries',did)
        commit=S.load('delivery_commits',did)
        if (not receipt or not commit or commit.get('receipt_hash')!=S.digest(receipt)
                or receipt.get('proposal_run_id')!=context['proposal_run_id']
                or receipt.get('body_hash')!=S.digest(receipt.get('body')) or instant(receipt['created_at'])>C.now()):
            raise ValueError('DELIVERY_MISMATCH')
        catalog.extend(receipt['catalog']); case_ids.update(receipt['case_ids'])
    # Multiple delivery calls of the same immutable metric remain one reference.
    catalog=list({S.digest(x['reference']):x for x in catalog}.values())
    return catalog,case_ids


def _effect(item,target,base):
    effect=item.get('effect'); kind=item['kind']
    if not isinstance(effect,dict) or effect.get('operation')!=target['operation']: raise ValueError('EFFECT_TARGET_MISMATCH')
    if item.get('validation_kind')!=target['validation_kind']: raise ValueError('VALIDATION_KIND_MISMATCH')
    if target['operation']=='prompt_guidance':
        if set(effect)!={'operation','text'}: raise ValueError('EFFECT_SCHEMA_ERROR')
        text(effect['text'],'effect.text')
    elif target['operation']=='add_confidence':
        if set(effect)!={'operation','amount','unit','placement'} or effect['unit']!='confidence' or effect['placement']!=target['call_stage']:
            raise ValueError('EFFECT_SCHEMA_ERROR')
        if not -1<=number(effect['amount'])<=1: raise ValueError('CONFIDENCE_DELTA_OUT_OF_RANGE')
    elif target['operation']=='set_parameter':
        if set(effect)!={'operation','value','unit'} or effect['unit']!=target['unit']: raise ValueError('EFFECT_SCHEMA_ERROR')
        value=number(effect['value']); limits=target['limits']; old=target['value']
        if not limits['min']<=value<=limits['max'] or abs(value-old)>limits['step']+1e-12:
            raise ValueError('PARAMETER_RANGE_OR_STEP')
        if limits.get('int') and type(value) is not int: raise ValueError('INTEGER_PARAMETER_REQUIRED')
        import tuning
        trial=copy.deepcopy(base['params']); section,leaf=target['key'].split('.',1); trial[section][leaf]=value
        if tuning.consistency(trial): raise ValueError('PARAMETER_CONSISTENCY')
    else: raise ValueError('UNSUPPORTED_EFFECT')
    return copy.deepcopy(effect)


def _readiness(item,cohort,context):
    if item['kind']=='retain_current': return {'status':'not_applicable','missing_inputs':[]}
    matched=set(cohort['matched_case_ids']); missing=[]
    if not matched: missing.append({'reason':'no_observed_matching_case'})
    for case in context['cases']:
        if case['application_id'] not in matched: continue
        if item['validation_kind']=='prompt_lesson':
            # Stage 7 must provide an actual model re-invocation experiment.
            missing.append({'application_id':case['application_id'],'reason':'prompt_reinvocation_trial_required'})
            attempt=(case.get('judgment_snapshot') or {}).get('run_attempt_id')
            if (context.get('runtime_inputs',{}).get(attempt) or {}).get('status')!='recorded':
                missing.append({'application_id':case['application_id'],'reason':'original_prompt_and_tool_contract_missing'})
        else:
            target=next((x for x in context['base']['targets'] if x['target_ref']==item.get('target_ref')),{})
            for reason in target.get('replay_requirements',[]):
                # A selected case and order plan do not reconstruct the full
                # screening pool, position updates or monitor price sequence.
                missing.append({'application_id':case['application_id'],'reason':reason})
            raw_score=(case.get('judgment_snapshot') or {}).get('confidence_raw')
            if type(raw_score) not in (int,float) or not math.isfinite(raw_score):
                missing.append({'application_id':case['application_id'],'reason':'original_ai_score_missing'})
            execution=case.get('execution_context') or {}
            events=[e for order in execution.get('orders',[]) for e in order.get('events',[])]
            events += [e for route in execution.get('non_order_paths',[]) for e in route.get('events',[])]
            ready_plans=[]
            definitions=execution.get('engine_definitions') or {}
            for event in events:
                inputs=event.get('inputs') or {}; reference=event.get('definition') or {}
                definition=definitions.get(reference.get('definition_id')) or {}
                if (event.get('kind')=='order_plan' and inputs.get('account') and inputs.get('quote')
                        and inputs.get('confidence') and definition.get('status')=='recorded'
                        and (definition.get('definition') or {}).get('sha256')==reference.get('sha256')
                        and (definition.get('definition') or {}).get('sources')
                        and ((definition.get('definition') or {}).get('source_identity') or {}).get('status')=='verified_loaded_source'):
                    ready_plans.append(event)
            if not ready_plans:
                missing.append({'application_id':case['application_id'],'reason':'frozen_plan_and_engine_inputs_missing'})
    return {'status':'replay_inputs_incomplete' if missing else 'ready_for_stage7_design', 'missing_inputs':missing,
            'validation_completed':False,'market_gain_verified':False}


def validate_proposal(item,context):
    import research_evidence as E
    allowed=set(SUBMISSION_SCHEMA['common_fields']+SUBMISSION_SCHEMA['optional_fields'])
    if not isinstance(item,dict) or set(item)-allowed: raise ValueError('SCHEMA_ERROR')
    text(item.get('item_id'),'item_id'); text(item.get('title'),'title'); text(item.get('reason'),'reason')
    if item.get('kind') not in KINDS: raise ValueError('UNKNOWN_PROPOSAL_KIND')
    catalog,visible_cases=_delivery_scope(context,item)
    claims=item.get('fact_claims')
    if not isinstance(claims,list) or not claims: raise ValueError('VERIFIED_FACT_REQUIRED')
    facts=[E.validate_claim(c,catalog) for c in claims]
    if len({x['id'] for x in claims})!=len(claims): raise ValueError('DUPLICATE_FACT_ID')
    cohort=_cohort(context,item)
    for field in ('support_case_ids','counter_case_ids'):
        refs=item.get(field,[])
        if not isinstance(refs,list) or any(not isinstance(x,str) for x in refs): raise ValueError('CASE_REFERENCE_SCHEMA')
        if not set(refs)<=visible_cases: raise ValueError('CASE_NOT_DELIVERED')
        if not set(refs)<=set(cohort['matched_case_ids']): raise ValueError('CASE_OUTSIDE_CONDITION')
    if item['kind']=='retain_current':
        if item.get('target_ref') or item.get('effect') or item.get('validation_kind')!='none': raise ValueError('RETAIN_CURRENT_HAS_EFFECT')
        if item.get('reason_kind') not in ('insufficient_evidence','no_supported_change'): raise ValueError('RETAIN_REASON_REQUIRED')
        target=None
    else:
        text(item.get('expected_change'),'expected_change'); text(item.get('validation_question'),'validation_question')
        target=next((t for t in context['base']['targets'] if t['target_ref']==item.get('target_ref')),None)
        if not target or item['kind'] not in target['kinds']: raise ValueError('TARGET_REFERENCE_MISMATCH')
        _effect(item,target,context['base'])
    previous=None
    if item.get('supersedes_proposal_id'):
        previous=_load_proposal(item['supersedes_proposal_id'])
        if not previous or previous['kind']!=item['kind'] or (previous.get('target_ref') or {}).get('target_id')!=(item.get('target_ref') or {}).get('target_id'):
            raise ValueError('INVALID_PREVIOUS_PROPOSAL')
    return {**copy.deepcopy(item),'facts':facts,'server_cohort':cohort,'source_refs':context['source_refs'],
            'readiness':_readiness(item,cohort,context),'target_snapshot':target,
            'version':previous['version']+1 if previous else 1,
            'series_id':previous.get('series_id',previous['proposal_id']) if previous else None,
            'content_validation':'passed','effect_validation':'submitted_unverified',
            'reason_status':'ai_explanation_unverified','expected_change_status':'submitted_unverified',
            'status':'test_candidate' if item['kind']!='retain_current' else 'retained', 'active':False}


def _load_proposal(identifier):
    row=S.load('proposals',identifier)
    if not row: return None
    commit=S.load('submissions',row['submission_id'])
    if not commit or identifier not in [x.get('proposal_id') for x in commit.get('items',[]) if x.get('status')=='accepted']:
        return None
    return row


def _schema_rejection(run_id,key,items,code):
    raw={'request_key':key,'items':items}
    try: S.canonical(raw)
    except (TypeError,ValueError): raw={'unserializable_repr':repr(raw)}
    aid='proposal-rejection-'+uuid.uuid4().hex
    row={'proposal_run_id':run_id,'submission_id':aid,'created_at':now(),'status':'rejected',
         'items':[],'errors':[{'path':'submission','code':code}],'raw_submission':raw}
    try:
        S.archive('submissions',aid,row);return row
    except Exception as exc:
        return {'status':'recording_error','errors':[S.record_error(run_id,'schema_rejection',exc)]}


def record_learning_proposals(proposal_run_id,request_key,items):
    import research_evidence as E
    import evidence_wire as W
    if not isinstance(request_key,str) or not request_key.strip() or len(request_key)>200 or not isinstance(items,list) or not items:
        return _schema_rejection(proposal_run_id,request_key,items,'SCHEMA_ERROR')
    try: request_hash=S.digest(items)
    except (TypeError,ValueError): return _schema_rejection(proposal_run_id,request_key,items,'SCHEMA_ERROR')
    sid='proposal-submission-'+S.digest([proposal_run_id,request_key])[:40]
    try:
        with S.locked(sid):
            old=S.load('submissions',sid)
            if old:
                if old.get('request_hash')==request_hash: return {**old,'idempotent':True}
                return _schema_rejection(proposal_run_id,request_key,items,'IDEMPOTENCY_CONFLICT')
            context=_load_context(proposal_run_id)
            reservation=S.load('submission_requests',sid)
            if reservation and reservation.get('request_hash')!=request_hash:
                return _schema_rejection(proposal_run_id,request_key,items,'IDEMPOTENCY_CONFLICT')
            if not reservation:
                reservation={'proposal_run_id':proposal_run_id,'submission_id':sid,'request_key':request_key,
                             'request_hash':request_hash,'created_at':now(),'raw_submission':copy.deepcopy(items)}
                S.archive('submission_requests',sid,reservation)
            # Reservations include rejected attempts, because they still reveal
            # conditions explored by the AI; do not certify such data as unused.
            E.record_usage(proposal_run_id,[c['application_id'] for c in context['cases']],
                           'step6_proposal_attempt',source_refs=context['source_refs'])
            results=[]; seen=set()
            for index,item in enumerate(items):
                try:
                    if isinstance(item,dict) and item.get('item_id') in seen: raise ValueError('DUPLICATE_ITEM_ID')
                    if isinstance(item,dict): seen.add(item.get('item_id'))
                    normalized = W.normalize_proposal(item)
                    validated=validate_proposal(normalized,context)
                    pid='learning-proposal-'+S.digest([sid,index,validated])[:40]
                    row={**validated,'proposal_id':pid,'series_id':validated['series_id'] or pid,
                         'proposal_run_id':proposal_run_id,'submission_id':sid,'created_at':reservation['created_at'],
                         'base_definition_hash':context['base']['definition_hash'],'base_settings_hash':context['base']['settings_hash'],
                         'base_policy_ref':copy.deepcopy(context['base'].get('policy_base_ref')),
                         'base_context_ref':{'proposal_run_id':proposal_run_id,'context_hash':S.digest(context)}}
                    if item.get('wire_format'):
                        row['wire_submission'] = {'wire_format': item['wire_format'], 'original': item,
                            'original_hash': S.digest(item), 'normalized_hash': S.digest(normalized)}
                    S.archive('proposals',pid,row)
                    results.append({'index':index,'item_id':item['item_id'],'status':'accepted','proposal_id':pid,
                                    'version':row['version'],'readiness':row['readiness']})
                except S.StoreError:
                    raise
                except (ValueError,TypeError,KeyError) as exc:
                    results.append({'index':index,'item_id':item.get('item_id') if isinstance(item,dict) else None,
                                    'status':'rejected','error':{'path':f'items[{index}]','code':str(exc).split(':')[0]}})
            accepted=sum(r['status']=='accepted' for r in results)
            record={'proposal_run_id':proposal_run_id,'submission_id':sid,'request_key':request_key,
                    'request_hash':request_hash,'created_at':reservation['created_at'],'status':'accepted' if accepted==len(results) else 'partial' if accepted else 'rejected',
                    'items':results,'raw_submission':copy.deepcopy(items)}
            S.archive('submissions',sid,record)
            return record
    except Exception as exc:
        return {'status':'recording_error','errors':[S.record_error(proposal_run_id,'record_learning_proposals',exc)]}


def get_learning_proposals(proposal_run_id,proposal_id=None):
    """Read-only lifecycle projection. Never finish, rebase or activate records."""
    try:
        run=S.load('runs',proposal_run_id)
        if not run: return {'proposal_run_id':proposal_run_id,'run_status':'not_found','recording_status':'unknown'}
        context=S.load('contexts',proposal_run_id);commit=S.load('context_commits',proposal_run_id)
        reads={k:S.records(k) for k in ('submissions','ai_attempts','ai_results')}
        errors=[e for read in reads.values() for e in read['errors']]
        stored=S.errors(proposal_run_id);errors+=stored['records']+stored['errors']
        own=[s for s in reads['submissions']['records'] if s.get('proposal_run_id')==proposal_run_id]
        starts=[s for s in reads['ai_attempts']['records'] if s.get('proposal_run_id')==proposal_run_id]
        ends=[s for s in reads['ai_results']['records'] if s.get('proposal_run_id')==proposal_run_id]
        result_ids=[i['proposal_id'] for s in own for i in s.get('items',[]) if i.get('status')=='accepted']
        rows=[]
        for pid in dict.fromkeys(result_ids):
            row=_load_proposal(pid)
            if row is None: raise ValueError('COMMITTED_PROPOSAL_MISSING')
            if proposal_id is None or proposal_id==pid: rows.append(row)
        if proposal_id and proposal_id not in result_ids: return {'proposal_run_id':proposal_run_id,'run_status':'not_found','recording_status':'unknown'}
        deployability_errors=[]
        try:
            current=target_snapshot() if rows else None
        except Exception as exc:
            current=None
            deployability_errors.append({'code':'CURRENT_BASE_UNAVAILABLE','error':str(exc)})
        for row in rows:
            row['base_status']=('unknown' if current is None else 'current' if row['base_definition_hash']==current['definition_hash'] and row['base_settings_hash']==current['settings_hash'] and row.get('base_policy_ref')==current.get('policy_base_ref') else 'stale_base')
        failure=S.load('context_failures',proposal_run_id)
        latest=max(ends,key=lambda x:x.get('created_at',''),default={})
        execution=latest.get('status','unfinished' if starts else 'not_started')
        ready=context is not None and commit is not None and commit.get('context_hash')==S.digest(context)
        submission='accepted' if result_ids else 'rejected' if own else 'not_submitted'
        status=('recording_error' if errors else 'source_not_ready' if failure else 'unfinished' if not ready else
                'no_targets' if not context['cases'] else 'partial' if execution in ('error','timeout','unfinished') else
                'complete' if submission=='accepted' else 'analysis_only' if execution=='not_requested' else 'awaiting_submission')
        return {**run,'run_status':status,'submission_status':submission,'ai_execution_status':execution,
                'expected_case_count':len(context['cases']) if context else None,
                'context_status':'ready' if ready else 'source_not_ready' if failure else 'unfinished',
                'proposals':rows,'submission_history':own,'ai_attempts':starts,'ai_results':ends,
                'recording_status':'recording_error' if errors else 'recorded' if ready else 'incomplete','errors':errors,
                'activation_status':'not_supported_in_step6', 'deployability_errors':deployability_errors}
    except Exception as exc:
        return {'proposal_run_id':proposal_run_id,'run_status':'recording_error','recording_status':'recording_error','errors':[{'error':str(exc)}]}


def export_validation_bundle(proposal_id):
    """Stage-7 input only. No simulation or approval is performed here."""
    row=_load_proposal(proposal_id)
    if not row: return {'status':'not_found'}
    context=_load_context(row['proposal_run_id'])
    import research_evidence as E
    summary=get_learning_proposals(row['proposal_run_id'],proposal_id)
    current=summary.get('proposals') or []
    if summary.get('recording_status')!='recorded' or not current:
        return {'status':'recording_error','errors':summary.get('errors',[])}
    base_status=current[0].get('base_status','unknown')
    matched=set(row['server_cohort']['matched_case_ids'])
    case_rows=[c for c in context['cases'] if c['application_id'] in matched]
    bundle={'proposal':row,'base_status':base_status,
            'cases':case_rows,'all_cases':copy.deepcopy(context['cases']),
            'condition_groups':{key:copy.deepcopy(row['server_cohort'].get(key,[])) for key in
                                ('matched_case_ids','outside_condition_case_ids','unknown_condition_case_ids')},
            'source_definitions':context['source_definitions'],
            'base_snapshot':context['base'], 'runtime_inputs':context.get('runtime_inputs',{}),
            'usage_status':E.usage_status([c['application_id'] for c in context['cases']]),
            'validation_kind':row['validation_kind'],'readiness':row['readiness'],
            'baseline_ai_score_policy':'freeze_original' if row['validation_kind']=='deterministic_rule' else 'requires_model_reinvocation',
            'evaluation_period_policy':'must_be_declared_by_stage7_before_evaluation',
            'stage7_completed':False,'active':False,
            'historical_readiness':copy.deepcopy(row['readiness']),
            'deployability':{'status':base_status,'errors':summary.get('deployability_errors',[])}}
    if base_status!='current':
        bundle['readiness']={**copy.deepcopy(row['readiness']),'status':'stale_base',
                             'original_status':row['readiness']['status']}
    bundle['status']='stale_base' if base_status!='current' else 'validation_bundle'
    return {**bundle,'bundle_hash':S.digest(bundle)}
