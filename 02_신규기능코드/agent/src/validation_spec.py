"""Predeclared immutable policy trials. Registration never executes or adopts."""
from __future__ import annotations
import copy
import math
from datetime import datetime
import common as C
import validation_store as S

VERSION='POLICY_VALIDATION_SPEC@v1'

def instant(value):
    value=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if value.tzinfo is None: raise ValueError('TIMEZONE_REQUIRED')
    return value.astimezone(C.KST)

def _number(value, name, *, positive=False):
    if type(value) not in (int,float) or not math.isfinite(value) or (positive and value<=0):
        raise ValueError('INVALID_'+name.upper())
    return value

def validate_spec(spec):
    if not isinstance(spec,dict): raise ValueError('SPEC_OBJECT_REQUIRED')
    allowed={'mode','period','scope','metrics','costs','execution','minimums','risk_limits','family_id',
             'repetitions','seed','prompt','stop_rules','description','engine_version','missing_policy','comparison_kind'}
    if set(spec)-allowed: raise ValueError('UNKNOWN_SPEC_FIELD')
    if spec.get('mode') not in ('retrospective','prospective'): raise ValueError('MODE_REQUIRED')
    period=spec.get('period') or {}
    if not isinstance(period,dict) or set(period)-{'start','end','knowledge_as_of'}: raise ValueError('INVALID_PERIOD_FIELDS')
    start,end=instant(period.get('start')),instant(period.get('end'))
    if start>=end: raise ValueError('PERIOD_ORDER')
    if spec['mode']=='prospective' and start<=C.now(): raise ValueError('PROSPECTIVE_REGISTRATION_MUST_PRECEDE_START')
    knowledge=instant(period.get('knowledge_as_of',C.now().isoformat()))
    if spec['mode']=='retrospective' and (knowledge>C.now() or end>knowledge): raise ValueError('FUTURE_RETROSPECTIVE_INPUT')
    scope=spec.get('scope') or {}
    if set(scope)-{'codes','confidence_for'}: raise ValueError('UNKNOWN_SCOPE_FIELD')
    if not isinstance(scope.get('codes'),list) or not scope['codes'] or any(not isinstance(x,str) or not x for x in scope['codes']):
        raise ValueError('CODES_REQUIRED')
    if not isinstance(scope.get('confidence_for'),list) or not scope['confidence_for'] or not set(scope['confidence_for'])<={'매수','매도'}:
        raise ValueError('DIRECTIONS_REQUIRED')
    if not isinstance(spec.get('family_id'),str) or not spec['family_id'].strip(): raise ValueError('FAMILY_ID_REQUIRED')
    if spec.get('missing_policy','unknown_no_new_adjustment')!='unknown_no_new_adjustment': raise ValueError('UNSUPPORTED_MISSING_POLICY')
    for field in ('metrics','costs','execution','minimums','risk_limits','prompt','stop_rules'):
        if field in spec and not isinstance(spec[field],dict): raise ValueError(field.upper()+'_OBJECT_REQUIRED')
    if spec.get('stop_rules'):
        raise ValueError('UNSUPPORTED_STOP_RULES; missing inputs already make the result incomplete')
    if set(spec.get('prompt',{}))-{'model','budget_usd'}:
        raise ValueError('UNSUPPORTED_PROMPT_SETTING')
    if spec.get('comparison_kind')=='prompt_pair':
        prompt=spec.get('prompt',{})
        if not isinstance(prompt.get('model'),str) or not prompt['model'].strip():raise ValueError('EXPLICIT_MODEL_REQUIRED')
        _number(prompt.get('budget_usd'),'budget_usd',positive=True)
        if 'repetitions' not in spec:raise ValueError('EXPLICIT_REPETITIONS_REQUIRED')
    for key,value in spec.get('minimums',{}).items():
        if key not in ('sessions','affected_sessions','affected_decisions','symbols'): raise ValueError('UNKNOWN_MINIMUM')
        if type(value) is not int or value<1: raise ValueError('POSITIVE_INTEGER_MINIMUM_REQUIRED')
    for key,value in spec.get('costs',{}).items():
        if key not in ('fee_rate','sell_tax_rate','slippage_bps','latency_seconds'): raise ValueError('UNKNOWN_COST')
        if _number(value,key)<0: raise ValueError('NEGATIVE_COST')
        if key in ('fee_rate','sell_tax_rate') and value>=1: raise ValueError('COST_RATE_OUT_OF_RANGE')
    for key,value in spec.get('risk_limits',{}).items():
        if key not in ('max_drawdown','max_drawdown_degradation','min_net_gain','min_paired_completion_rate'):
            raise ValueError('UNKNOWN_RISK_CRITERION')
        _number(value,key)
        if key!='min_net_gain' and not 0<=value<=1: raise ValueError('RISK_CRITERION_RANGE')
    if 'repetitions' in spec and (type(spec['repetitions']) is not int or not 1<=spec['repetitions']<=100): raise ValueError('INVALID_REPETITIONS')
    if 'seed' in spec and type(spec['seed']) is not int: raise ValueError('INVALID_SEED')
    if spec.get('comparison_kind','fixed_judgment_stream') not in ('fixed_judgment_stream','prompt_pair','screen_only'):
        raise ValueError('UNSUPPORTED_COMPARISON_KIND')
    execution=spec.get('execution',{})
    if set(execution)-{'fill_model','quantity_policy','terminal_holdings','max_quote_age_seconds','initial_account','missing_price_policy'}:
        raise ValueError('UNKNOWN_EXECUTION_FIELD')
    if execution.get('fill_model','next_observation')!='next_observation': raise ValueError('UNSUPPORTED_FILL_MODEL')
    if execution.get('quantity_policy','follow_plan') not in ('follow_plan','recorded_request'): raise ValueError('UNSUPPORTED_QUANTITY_POLICY')
    if execution.get('terminal_holdings','mark_to_market') not in ('mark_to_market','liquidate_at_next_observation'): raise ValueError('UNSUPPORTED_TERMINAL_POLICY')
    if execution.get('missing_price_policy','incomplete')!='incomplete': raise ValueError('UNSUPPORTED_MISSING_PRICE_POLICY')
    if 'max_quote_age_seconds' in execution:_number(execution['max_quote_age_seconds'],'max_quote_age_seconds',positive=True)
    if 'initial_account' in execution: raise ValueError('INITIAL_ACCOUNT_MUST_COME_FROM_ARCHIVED_INPUT')
    metrics=spec.get('metrics',{})
    if set(metrics)-{'primary','secondary','family_size','bootstrap_repetitions','block_length'}: raise ValueError('UNKNOWN_METRIC_FIELD')
    if metrics.get('primary','net_equity_difference')!='net_equity_difference': raise ValueError('UNSUPPORTED_PRIMARY_METRIC')
    for name in ('family_size','bootstrap_repetitions','block_length'):
        if name in metrics and (type(metrics[name]) is not int or metrics[name]<1):raise ValueError('INVALID_'+name.upper())
    required={'metrics':('primary','family_size'), 'costs':('fee_rate','sell_tax_rate','slippage_bps','latency_seconds'),
              'execution':('fill_model','quantity_policy','terminal_holdings','max_quote_age_seconds'),
              'minimums':('sessions','affected_sessions','affected_decisions','symbols'),
              'risk_limits':('max_drawdown','max_drawdown_degradation','min_net_gain','min_paired_completion_rate')}
    missing=[f'{field}.{key}' for field,keys in required.items() for key in keys if key not in spec.get(field,{})]
    out=copy.deepcopy(spec)
    out['period']={**period,'start':start.isoformat(),'end':end.isoformat(),'knowledge_as_of':knowledge.isoformat()}
    return out,missing

def load_spec(validation_id):
    row=S.load('specs',validation_id); commit=S.load('spec_commits',validation_id)
    if not row or not commit or commit.get('spec_hash')!=S.digest(row): raise ValueError('VALIDATION_SPEC_NOT_COMMITTED')
    return row

def register_validation(proposal_id,request_key,spec):
    """Register first; absent performance criteria permit development only."""
    validation_id=None
    try:
        if not isinstance(request_key,str) or not request_key.strip() or len(request_key)>200: raise ValueError('REQUEST_KEY_REQUIRED')
        raw_hash=S.digest({'proposal_id':proposal_id,'spec':spec})
        validation_id='validation-'+S.digest([proposal_id,request_key])[:40]
        with S.locked('registration'):
            old=S.load('specs',validation_id)
            if old:
                if old.get('request_hash')!=raw_hash: raise ValueError('IDEMPOTENCY_CONFLICT')
                # Interrupted registration is recoverable from the original immutable body.
                if not S.load('spec_commits',validation_id):
                    if old['spec']['mode']=='prospective':
                        import validation_holdout as H
                        H.reserve(old)
                    S.archive('spec_commits',validation_id,{'validation_id':validation_id,'spec_hash':S.digest(old),'committed_at':old['registered_at']})
                return {**get_validation_spec(validation_id),'replayed':True}
            normalized,missing=validate_spec(spec)
            import learning_proposals as P
            source=P.export_validation_bundle(proposal_id)
            if not source.get('proposal') or source.get('status')=='recording_error': raise ValueError('PROPOSAL_SOURCE_UNAVAILABLE')
            if source['proposal'].get('kind')=='retain_current': raise ValueError('RETAIN_CURRENT_HAS_NO_POLICY_TRIAL')
            if source['proposal'].get('active') is not False: raise ValueError('INVALID_PROPOSAL_STATE')
            row={'validation_id':validation_id,'version':VERSION,'request_key':request_key,'request_hash':raw_hash,
                 'registered_at':C.now().isoformat(),'proposal_id':proposal_id,'proposal_snapshot':source['proposal'],
                 'source_bundle':source,'source_bundle_hash':S.digest(source),'spec':normalized,
                 'criteria_complete':not missing,'missing_criteria':missing,
                 'independence':'prospectively_reserved' if normalized['mode']=='prospective' else 'discovery_replay_not_independent',
                 'status':'registered','operational_activation':False,'real_ai_authorized_by_registration':False}
            S.archive('specs',validation_id,row)
            if normalized['mode']=='prospective':
                import validation_holdout as H
                H.reserve(row)
            S.archive('spec_commits',validation_id,{'validation_id':validation_id,'spec_hash':S.digest(row),'committed_at':row['registered_at']})
        return get_validation_spec(validation_id)
    except (ValueError,TypeError,KeyError) as exc:
        rejection={'validation_id':validation_id,'status':'rejected','recording_status':'recorded','errors':[{'code':str(exc)}]}
        try:
            fingerprint=S.digest({'proposal_id':proposal_id,'request_key':request_key,'spec':spec})
            identifier='spec-rejection-'+S.digest([fingerprint,str(exc)])[:40]
            old=S.load('spec_rejections',identifier)
            if not old:S.archive('spec_rejections',identifier,{**rejection,'request_hash':fingerprint,'created_at':C.now().isoformat()})
        except Exception as store_exc:
            rejection.update(recording_status='recording_error',recording_errors=[S.record_error(validation_id,'spec_rejection',store_exc)])
        return rejection
    except Exception as exc:
        return {'validation_id':validation_id,'status':'recording_error','recording_status':'recording_error',
                'errors':[S.record_error(validation_id,'register_validation',exc)]}

def get_validation_spec(validation_id):
    try:
        row=load_spec(validation_id)
        return {**row,'recording_status':'recorded','adoption_readiness':'criteria_registered' if row['criteria_complete'] else 'development_only'}
    except Exception as exc:
        return {'validation_id':validation_id,'status':'recording_error','recording_status':'recording_error','errors':[{'code':str(exc)}]}
