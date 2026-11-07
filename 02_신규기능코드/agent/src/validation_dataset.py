"""Freeze original opportunities and ordered calls, including missing judgments.

No current quote/config repairs a past input. Archived source is data only; this
module never imports or executes it. Research discovery cases are annotations,
not the opportunity denominator used by a policy trial.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import common as C
import validation_store as S
from validation_spec import load_spec, instant

VERSION = 'VALIDATION_INPUTS@v1'

def _bounded(rows, cutoff, gaps, kind):
    out = []
    for row in rows:
        try:
            if instant(row['created_at']) <= cutoff: out.append(row)
        except (KeyError, ValueError, TypeError) as exc:
            gaps.append({'kind':kind,'reason':'availability_unknown','id':row.get('event_id'),'error':str(exc)})
    return out

def _original(kind, identifier):
    import ai_judgment as J
    import snapshot_store
    value = snapshot_store.load(J._path(kind, identifier))
    if not isinstance(value,dict): raise ValueError('ORIGINAL_OBJECT_REQUIRED')
    return value

def _scope(spec, code, side):
    return ('*' in spec['scope']['codes'] or code in spec['scope']['codes']) and side in spec['scope']['confidence_for']

def _judgment_context(judgment):
    # Runtime and replay use the same adopted-claim semantics across premises.
    from policy_registry import judgment_inputs
    context=judgment_inputs(judgment)
    return {key:copy.deepcopy(context.get(key)) for key in
            ('features','signed_flows','adoption','stratum','comparison_context','source_manifest',
             'feature_eligible','feature_reasons','selection_observation_snapshot','selection_definition_snapshot',
             'selection_settings_snapshot','decision_regime_snapshot')}


def _unknown_context(reason):
    return {'features':{},'signed_flows':{},'adoption':{},'stratum':{},'feature_eligible':False,'feature_reasons':[reason]}

def get_inputs(validation_id, input_id=None):
    if input_id:
        row=S.load('inputs',input_id)
        if not row or row.get('validation_id')!=validation_id: raise ValueError('INPUT_NOT_FOUND')
    else:
        reads=S.records('input_commits')
        if reads['errors']: raise ValueError('INPUT_COMMIT_HISTORY_UNREADABLE')
        own=[r for r in reads['records'] if r.get('validation_id')==validation_id]
        if not own: return None
        commit=max(own,key=lambda r:(r.get('input_revision',0),r['created_at'],r['input_id']))
        row=S.load('inputs',commit['input_id'])
    commit=S.load('input_commits',row['input_id'])
    if not commit or commit.get('input_hash')!=S.digest(row): raise ValueError('INPUT_NOT_COMMITTED')
    return row

def ensure_inputs(validation_id):
    """A new capture gets a new ID; read-only get_inputs never repairs history."""
    manifest=load_spec(validation_id); existing=get_inputs(validation_id)
    imports=S.records('market_imports')
    if imports['errors']:raise ValueError('MARKET_IMPORT_HISTORY_UNREADABLE')
    ids=sorted(r['market_import_id'] for r in imports['records'] if r.get('validation_id')==validation_id)
    old=sorted(r['market_import_id'] for r in (existing or {}).get('market_imports',[]))
    if existing and ids==old:
        if manifest['spec']['mode']!='prospective':return existing
        end=instant(manifest['spec']['period']['end'])
        capture=min(C.now(),end)
        if instant(existing['knowledge_as_of'])>=capture:return existing
        return build_inputs(validation_id,refresh_key='capture-'+S.digest([capture.isoformat(),ids])[:40])
    return build_inputs(validation_id,refresh_key='market-'+S.digest(ids)[:40]) if existing else build_inputs(validation_id)


def import_validation_market(validation_id, request_key, rows):
    """Import an explicit local tape; declared provider times are not verified.

    Actual import time remains separate. Imports enable local scenario replay,
    never establish contemporaneous availability or independent evidence alone.
    """
    import math
    manifest=load_spec(validation_id)
    if not isinstance(request_key,str) or not request_key.strip(): raise ValueError('REQUEST_KEY_REQUIRED')
    if not isinstance(rows,list) or not rows: raise ValueError('MARKET_ROWS_REQUIRED')
    normalized=[]; seen=set()
    allowed={'observation_id','code','observed_at','available_at','price','price_basis','source','executable_qty'}
    for row in rows:
        if not isinstance(row,dict) or set(row)-allowed: raise ValueError('INVALID_MARKET_ROW_FIELDS')
        oid=row.get('observation_id')
        if not isinstance(oid,str) or not oid or oid in seen: raise ValueError('DUPLICATE_OR_MISSING_OBSERVATION_ID')
        if not isinstance(row.get('code'),str) or not row['code']:raise ValueError('MARKET_CODE_REQUIRED')
        if row.get('price_basis')!='unadjusted' or not isinstance(row.get('source'),str) or not row['source'].strip(): raise ValueError('UNADJUSTED_PRICE_SOURCE_REQUIRED')
        if type(row.get('price')) not in (int,float) or not math.isfinite(row['price']) or row['price']<=0:raise ValueError('POSITIVE_FINITE_PRICE_REQUIRED')
        at,available=instant(row.get('observed_at')),instant(row.get('available_at'))
        if available<at:raise ValueError('AVAILABILITY_BEFORE_OBSERVATION')
        if at>C.now() or available>C.now():raise ValueError('FUTURE_MARKET_DATA_FORBIDDEN')
        qty=row.get('executable_qty')
        if qty is not None and (type(qty) is not int or qty<0):raise ValueError('INVALID_EXECUTABLE_QTY')
        seen.add(oid);normalized.append(copy.deepcopy(row))
    mid='market-import-'+S.digest([validation_id,request_key])[:40]
    request_hash=S.digest(normalized)
    with S.locked('market-'+validation_id):
        old=S.load('market_imports',mid)
        if old:
            if old.get('request_hash')!=request_hash:raise ValueError('IDEMPOTENCY_CONFLICT')
            return old
        return S.archive('market_imports',mid,{'market_import_id':mid,'validation_id':validation_id,
            'request_key':request_key,'request_hash':request_hash,'imported_at':C.now().isoformat(),
            'rows':normalized,'status':'imported','recording_status':'recorded',
            'provenance_status':'operator_supplied_unverified','historical_availability_verified':False,
            'adoption_eligible_as_independent_evidence':False})


def _simulation_inputs(events, opportunities, manifest, imports):
    """Assemble only captured fields; incomplete coverage is visible, not fixed."""
    import portfolio_capture as PC
    gaps=[]; quotes=[]; decisions=[]; initial=None; initial_positions=None; initial_journal=None
    scope_values={e.get('account_scope') for e in events if e.get('kind')=='order_plan' and e.get('account_scope')}
    scope=next(iter(scope_values)) if len(scope_values)==1 else None
    captures=[e for e in events if e.get('kind') in PC.KINDS and scope and e.get('account_scope')==scope]
    states={e['event_id']:e for e in captures if e.get('kind')=='portfolio_state'}
    market={e['event_id']:e for e in captures if e.get('kind')=='portfolio_market'}
    if len(scope_values)>1:gaps.append({'reason':'multiple_account_scopes_require_separate_portfolios'})
    byid={o['opportunity_id']:o for o in opportunities}
    plans={e.get('plan_id'):e for e in events if e.get('kind')=='order_plan'}
    for event in events:
        if event.get('kind')!='order_plan':continue
        inputs=event.get('inputs') or {};audit=inputs.get('sizing') or {};values=audit.get('inputs') or {}
        runtime=audit.get('runtime_context') or {}; env=runtime.get('environment') or {}
        snapshots={Path(x['path']).name:copy.deepcopy(x.get('value')) for x in audit.get('actual_reads',[]) if x.get('path') and x.get('status')!='recording_error'}
        state_ref=inputs.get('portfolio_state') or {}
        anchor=states.get(state_ref.get('event_id'))
        if state_ref.get('event_id') and not anchor:
            gaps.append({'event_id':event.get('event_id'),'reason':'initial_state_capture_reference_unavailable'})
        if anchor and (anchor.get('run_attempt_id')!=event.get('run_attempt_id')
                or anchor.get('purpose')!='order_plan' or anchor.get('account')!=inputs.get('account')
                or instant(anchor['captured_at'])>instant(event['created_at'])):
            gaps.append({'event_id':event.get('event_id'),'reason':'initial_state_capture_link_mismatch'})
            anchor=None
        if initial is None and isinstance(inputs.get('account'),dict):
            initial=copy.deepcopy(inputs['account']);initial['captured_at']=anchor['captured_at'] if anchor else runtime.get('at',event.get('created_at'))
            initial['complete']=(initial.get('complete') is True and all(k in initial for k in ('cash','total','holdings')))
            if not initial['complete']:gaps.append({'event_id':event.get('event_id'),'reason':'initial_account_completeness_unverified'})
            initial_positions=copy.deepcopy(anchor.get('positions') if anchor else snapshots.get('positions.json'))
            journal=anchor.get('journal') if anchor else snapshots.get('journal.jsonl')
            initial_journal=copy.deepcopy(journal) if isinstance(journal,list) else None
        quote=copy.deepcopy(inputs.get('quote') or {});code=values.get('code') or (event.get('plan') or {}).get('code')
        oid=quote.get('observation_id') or 'quote-'+S.digest([event.get('event_id'),quote])[:40]
        captured_quote=market.get(oid)
        if captured_quote and (captured_quote.get('observation') or {}).get('code')==code:
            raw=captured_quote['observation']
            if raw.get('price')!=quote.get('price'):
                gaps.append({'event_id':event.get('event_id'),'reason':'plan_quote_capture_value_mismatch'})
            else:quote={**copy.deepcopy(raw),'observation_id':oid}
        observed=quote.get('market_observed_at') or quote.get('observed_at') or quote.get('captured_at')
        available=quote.get('available_at') or quote.get('received_at')
        if observed and available and quote.get('price_basis')=='unadjusted' and quote.get('source') and quote.get('status')!='invalid':
            quotes.append({'observation_id':oid,'code':code,'observed_at':observed,'available_at':available,
                'price':quote.get('price'),'price_basis':'unadjusted','source':quote['source'],
                'executable_qty':quote.get('executable_qty'), **{k:copy.deepcopy(quote[k]) for k in
                    ('time_basis','provider_observed_at','received_at','provenance','capacity_source') if k in quote}})
        else:gaps.append({'event_id':event.get('event_id'),'reason':'quote_time_or_raw_basis_unrecorded'})
        original_orders=[e for e in events if e.get('stage')=='place_order' and e.get('kind')=='tool_result' and e.get('plan_id')==event.get('plan_id') and not (e.get('result') or {}).get('replayed')]
        if not original_orders:continue
        # Plans by themselves do not imply AI committed to ordering.
        for order in original_orders:
            original=next((c for o in opportunities for c in o['coverage_target'].get('calls',[]) if c.get('event_id')==order.get('coverage_event_id')),None)
            if not original:
                gaps.append({'event_id':order.get('event_id'),'reason':'original_order_call_unavailable'});continue
            context=next((byid[x]['condition_contexts_by_judgment'].get(original.get('judgment_id'),_unknown_context('original_judgment_missing')) for x in order.get('opportunity_ids',[]) if x in byid),_unknown_context('original_opportunity_missing'))
            decisions.append({'decision_id':original['event_id'],'at':original['created_at'],'code':original.get('code'),
                'side':original.get('confidence_for'),'raw_confidence':original.get('confidence_raw'),
                'recommended_action':original.get('confidence_for'),
                'params':copy.deepcopy(values.get('params')),'risk':copy.deepcopy(values.get('risk')),
                'quote_observation_id':oid,'run_id':env.get('RUN_ID'),'run_mode':env.get('RUN_MODE'),
                'module_state':copy.deepcopy(runtime.get('module_state')),
                'regime':values.get('regime'),'today_change':values.get('today_change'),
                'rule_exit':original.get('rule_exit',False),'requested_qty':(order.get('payload') or {}).get('qty'),
                'condition_context':context,'policy_context':copy.deepcopy(inputs.get('policy_context')),
                'briefing':snapshots.get('briefing.json'),'snapshots':snapshots,
                'source_event_id':event.get('event_id'),'definition':event.get('definition')})
    represented={d['decision_id'] for d in decisions}
    for opportunity in opportunities:
        calls=[c for c in opportunity['coverage_target'].get('calls',[]) if c.get('event_type')=='call_enter']
        actions=[c for c in calls if c.get('stage')=='record_decision' or c.get('action') in ('보유','관망')]
        for call in actions:
            if call['event_id'] in represented:continue
            action=call.get('action')
            watch_events=[e for e in events if e.get('coverage_event_id')==call['event_id'] and e.get('kind')=='tool_result']
            watch_policy=(watch_events[0].get('confidence_audit') or {}).get('policy_context') if len(watch_events)==1 else None
            watch_detail=((watch_events[0].get('confidence_audit') or {}).get('decision') or {}) if len(watch_events)==1 else {}
            if action not in ('보유','관망'):
                gaps.append({'opportunity_id':opportunity['opportunity_id'],'reason':'planned_action_is_not_executed_order_or_watch'})
            decisions.append({'decision_id':call['event_id'],'at':call['created_at'],'code':opportunity['code'],
                'side':opportunity['confidence_for'],'raw_confidence':call.get('confidence_raw'),
                'recommended_action':action if action in ('보유','관망') else None,
                'policy_context':copy.deepcopy(watch_policy),
                'module_state':copy.deepcopy((watch_detail.get('runtime_context') or {}).get('module_state')),
                'condition_context':opportunity['condition_contexts_by_judgment'].get(call.get('judgment_id'),_unknown_context('judgment_unrecorded')),
                'opportunity_id':opportunity['opportunity_id'],'source_kind':'original_record_decision'})
            represented.add(call['event_id'])
        if not any(c['event_id'] in represented for c in calls):
            gaps.append({'opportunity_id':opportunity['opportunity_id'],'reason':'original_final_decision_stream_missing'})
            decisions.append({'decision_id':opportunity['opportunity_id'],'at':opportunity.get('decision_at'),
                'code':opportunity['code'],'side':opportunity['confidence_for'],'raw_confidence':opportunity.get('raw_confidence'),
                'recommended_action':None,'condition_context':opportunity['condition_context'],
                'status':'unresolved_original_action'})
    # Preserve exact receipt-time observations for selected instruments and
    # initial holdings. Another account/instrument cannot supply missing data.
    codes={d.get('code') for d in decisions if d.get('code')}
    codes.update(h.get('code') for h in (initial or {}).get('holdings',[]) if h.get('code'))
    for identifier,event in market.items():
        q=event.get('observation') or {}
        if q.get('code') not in codes:continue
        if q.get('status')=='invalid':
            gaps.append({'event_id':identifier,'reason':'invalid_original_market_observation'});continue
        if not q.get('observed_at') or not q.get('available_at') or q.get('price_basis')!='unadjusted':
            gaps.append({'event_id':identifier,'reason':'original_market_time_or_basis_unknown'});continue
        quotes.append({'observation_id':identifier,**{k:copy.deepcopy(q.get(k)) for k in
            ('code','observed_at','available_at','price','price_basis','source','executable_qty')},
            **{k:copy.deepcopy(q[k]) for k in ('time_basis','provider_observed_at','received_at','provenance','capacity_source') if k in q}})
    for imported in imports:quotes.extend(copy.deepcopy(imported['rows']))
    unique_quotes={}
    for quote in quotes:
        oid=quote['observation_id']
        if oid in unique_quotes and quote!=unique_quotes[oid]:gaps.append({'observation_id':oid,'reason':'conflicting_market_observation_id'})
        else:unique_quotes[oid]=quote
    quotes=list(unique_quotes.values())
    if initial is None:gaps.append({'reason':'original_initial_account_unavailable'})
    elif instant(initial['captured_at'])!=instant(manifest['spec']['period']['start']):gaps.append({'reason':'initial_account_not_at_registered_start'})
    if initial_positions is None:gaps.append({'reason':'initial_position_metadata_unrecorded'})
    if initial_journal is None:gaps.append({'reason':'initial_journal_unrecorded'})
    if not quotes:gaps.append({'reason':'raw_market_tape_missing'})
    if any(q.get('executable_qty') is None for q in quotes):gaps.append({'reason':'executable_quantity_unrecorded'})
    coverage=PC.coverage(events,manifest['spec']['period']['start'],manifest['spec']['period']['end'],scope,codes)
    if not coverage['complete']:gaps.append({'reason':'continuous_monitor_coverage_unverified','details':coverage['gaps']})
    valuations=coverage['valuation_times']
    if not valuations:gaps.append({'reason':'valuation_schedule_unrecorded'})
    return {'status':'complete' if not gaps else 'incomplete','complete':not gaps,'gaps':gaps,
        'initial_account':initial,'initial_positions':initial_positions,'initial_journal':initial_journal,
        'market_observations':quotes,'decisions':sorted(decisions,key=lambda x:(x.get('at') or '',x['decision_id'])),
        'valuation_times':valuations,'monitor_coverage':'complete' if coverage['complete'] else 'unknown',
        'monitor_checks':coverage['checks'], 'capture_coverage':coverage,
        'rule_exit_contract':coverage.get('rule_exit_contract','unknown'),
        'capture_source_ids':[e['event_id'] for e in captures], 'account_scope':scope,
        'market_import_ids':[r['market_import_id'] for r in imports],
        'historical_market_availability_verified':not imports,
        'interpretation':'operator_imports_are_declared_scenario_inputs_not_verified_historical_provider_capacity' if imports else 'captured_original_inputs'}


def build_inputs(validation_id, refresh_key=None):
    import judgment_coverage as JC
    import ai_judgment as J
    import evaluation_context as EC
    import runtime_inputs as RI
    import validation_holdout as H
    manifest=load_spec(validation_id);spec=manifest['spec']
    if refresh_key is not None and (not isinstance(refresh_key,str) or not refresh_key.strip()): raise ValueError('REFRESH_KEY_REQUIRED')
    iid='validation-input-'+S.digest([validation_id,refresh_key])[:40]
    existing=S.load('inputs',iid)
    if existing:
        commit=S.load('input_commits',iid)
        if not commit:
            S.archive('input_commits',iid,{'validation_id':validation_id,'input_id':iid,'input_hash':S.digest(existing),'created_at':existing['created_at'],'input_revision':existing.get('input_revision',0)})
        return get_inputs(validation_id,iid)
    with S.locked('input-'+validation_id):
        existing=S.load('inputs',iid)
        if existing: return get_inputs(validation_id,iid)
        start,end=instant(spec['period']['start']),instant(spec['period']['end'])
        cutoff=instant(spec['period']['knowledge_as_of']) if spec['mode']=='retrospective' else min(C.now(),end)
        if cutoff<start: raise ValueError('VALIDATION_PERIOD_NOT_STARTED')
        gaps=[];sources=[];attempts,errors=JC._records('attempts');gaps.extend(errors)
        period_attempts=[a for a in _bounded(attempts,cutoff,gaps,'attempts') if start<=instant(a['created_at'])<end]
        system_attempts=[a for a in period_attempts if a.get('kind')=='exit']
        attempts=[a for a in period_attempts if a.get('kind') in ('decide','view')]
        coverage_events,errors=JC._records('coverage_events');gaps.extend(errors)
        coverage_events=_bounded(coverage_events,cutoff,gaps,'coverage_events')
        submissions,errors=JC._records('submissions');gaps.extend(errors)
        submissions=_bounded(submissions,cutoff,gaps,'submissions')
        opportunities=[];judgments={};runtime={};attempt_records=[]
        contexts={};all_calls=[]
        for attempt in sorted(attempts,key=lambda a:(a['created_at'],a['run_attempt_id'])):
            aid=attempt['run_attempt_id'];ev=[e for e in coverage_events if e.get('run_attempt_id')==aid]
            subs=[s for s in submissions if s.get('run_attempt_id')==aid]
            summary=JC._summarize(copy.deepcopy(attempt),copy.deepcopy(ev),copy.deepcopy(subs),[])
            attempt_records.append({'manifest':attempt,'coverage':summary,'coverage_events':ev,'submissions':subs})
            sources.append({'kind':'attempts','id':aid,'sha256':S.digest(attempt)})
            if summary.get('target_set_incomplete'): gaps.append({'run_attempt_id':aid,'reason':'target_set_incomplete'})
            try:
                snap=RI.load_attempt(aid)
                if snap and instant(snap['created_at'])>cutoff: snap=None
                runtime[aid]={'status':'recorded' if snap else 'unrecorded','snapshot':snap}
                if not snap:gaps.append({'run_attempt_id':aid,'reason':'original_ai_runtime_inputs_unrecorded'})
            except Exception as exc:
                runtime[aid]={'status':'recording_error','error':str(exc)}
                gaps.append({'run_attempt_id':aid,'reason':'original_ai_runtime_inputs_corrupt','error':str(exc)})
            for target in summary['targets']:
                code,side=target['code'],target['confidence_for']
                if not _scope(spec,code,side): continue
                oid='opportunity-'+S.digest([aid,code,side])[:40]
                ownjudgments=[];owncontexts={}
                for submit in target.get('submissions',[]):
                    jid=submit.get('judgment_id')
                    if submit.get('status')!='accepted' or not jid: continue
                    try:
                        row=J.load_judgment(jid)
                        if not row or instant(row['created_at'])>cutoff: raise ValueError('ORIGINAL_ACCEPTED_JUDGMENT_UNAVAILABLE')
                        if row.get('run_attempt_id')!=aid or row.get('code')!=code or row.get('confidence_for')!=side:
                            raise ValueError('ORIGINAL_JUDGMENT_TARGET_MISMATCH')
                        judgments[jid]=row; ownjudgments.append(row)
                        sources.append({'kind':'judgments','id':jid,'sha256':S.digest(row)})
                        if jid not in contexts:
                            try: contexts[jid]=_judgment_context(row)
                            except Exception as exc: contexts[jid]=_unknown_context('original_context_unavailable:'+str(exc))
                        owncontexts[jid]=contexts[jid]
                    except Exception as exc:gaps.append({'opportunity_id':oid,'judgment_id':jid,'reason':str(exc)})
                ownjudgments=list({j['judgment_id']:j for j in ownjudgments}.values())
                condition=next(iter(owncontexts.values())) if len(owncontexts)==1 else _unknown_context('judgment_missing' if not owncontexts else 'multiple_judgments_require_explicit_call_reference')
                opportunities.append({'opportunity_id':oid,'run_attempt_id':aid,'code':code,'confidence_for':side,
                    'origin_at':attempt['created_at'],'judgment_ids':[j['judgment_id'] for j in ownjudgments],
                    'judgments':ownjudgments,'condition_context':condition,'condition_contexts_by_judgment':owncontexts,
                    'coverage_status':target['status'],'coverage_target':target,'source_manifest':attempt,
                    'raw_confidence':ownjudgments[0].get('confidence_raw') if len(ownjudgments)==1 else None,
                    'decision_at':min([j['created_at'] for j in ownjudgments]+[c['created_at'] for c in target.get('calls',[])],default=None),
                    'at':min([j['created_at'] for j in ownjudgments]+[c['created_at'] for c in target.get('calls',[])],default=None)})
                all_calls.extend(target.get('calls',[]))
        for aid, snapshot in runtime.items():
            original=snapshot.get('snapshot')
            cutoffs=[o['decision_at'] for o in opportunities if o['run_attempt_id']==aid and o.get('decision_at')]
            if original and cutoffs and instant(original['created_at'])>min(instant(x) for x in cutoffs):
                runtime[aid]={'status':'unavailable_at_original_decision','snapshot':None,
                              'original_snapshot_hash':S.digest(original)}
                gaps.append({'run_attempt_id':aid,'reason':'runtime_input_not_available_at_original_decision'})
        # Keep every original event, including hooks/zero quantities/missing IDs.
        execution_errors=[]
        raw_events=EC._records(Path(C.WORK)/'ontology/executions',cutoff,execution_errors)
        gaps.extend(execution_errors)
        attempt_ids={a['run_attempt_id'] for a in attempts}
        system_ids={a['run_attempt_id'] for a in system_attempts}
        import portfolio_capture as PC
        own_scopes={e.get('account_scope') for e in raw_events if e.get('kind')=='order_plan'
                    and e.get('run_attempt_id') in attempt_ids | system_ids and e.get('account_scope')}
        events=[copy.deepcopy(e) for e in raw_events if e.get('run_attempt_id') in attempt_ids | system_ids and start<=instant(e['created_at'])<=end or
                e.get('kind') in ('monitor_check','monitor_tick','simulation_coverage') and start<=instant(e['created_at'])<=end or
                e.get('kind') in PC.KINDS and e.get('account_scope') in own_scopes and
                    (e.get('kind') in ('portfolio_session','portfolio_session_end') or start<=instant(e['created_at'])<=end)]
        definitions=EC._definitions(Path(C.WORK)/'ontology',events,gaps)
        by_call={call['event_id']:o['opportunity_id'] for o in opportunities for call in o['coverage_target'].get('calls',[])}
        for e in events:
            call_id=e.get('coverage_event_id',e.get('call_event_id'))
            jid=e.get('judgment_id') or (e.get('payload') or {}).get('judgment_id')
            refs=[o['opportunity_id'] for o in opportunities if jid and jid in o['judgment_ids']]
            if call_id in by_call: refs=[by_call[call_id]]
            e['opportunity_ids']=sorted(set(refs))
            sources.append({'kind':'executions','id':e.get('event_id'),'sha256':S.digest({k:v for k,v in e.items() if k!='opportunity_ids'})})
        events.sort(key=lambda e:(e.get('created_at',''),e.get('recorded_at_ns',0),e.get('event_id','')))
        context=EC.execution_context(list(judgments),cutoff.isoformat())
        gaps.extend(context.get('errors',[]))
        receipts,errors=JC._records('deliveries');gaps.extend(errors)
        deliveries=[r for r in _bounded(receipts,cutoff,gaps,'deliveries') if r.get('run_attempt_id') in attempt_ids]
        selection_ids=set()
        for delivery in deliveries:
            sources.append({'kind':'deliveries','id':delivery.get('delivery_id'),'sha256':S.digest(delivery)})
            b=delivery.get('briefing_snapshot') or {}
            if (b.get('ontology') or {}).get('selection_run_id'): selection_ids.add(b['ontology']['selection_run_id'])
            for key in ('candidates','holdings'):
                for row in b.get(key,[]):
                    obs=(row.get('_ontology') or {}).get('observation') or {}
                    if obs.get('run_id'):selection_ids.add(obs['run_id'])
        selection_runs=[]
        for sid in sorted(selection_ids):
            try:
                row=_original('selections',sid)
                if instant(row['created_at'])>cutoff:raise ValueError('SELECTION_NOT_AVAILABLE_AT_CUTOFF')
                selection_runs.append(row);sources.append({'kind':'selections','id':sid,'sha256':S.digest(row)})
            except Exception as exc:gaps.append({'selection_run_id':sid,'reason':str(exc)})
        if not attempts:gaps.append({'reason':'no_original_attempt_manifests'})
        if any(o['coverage_status']=='recording_error' for o in opportunities):gaps.append({'reason':'opportunity_recording_error'})
        enrollment=H.status(validation_id)
        if spec['mode']=='prospective':
            enrolled={r['run_attempt_id']:r for r in enrollment.get('enrollments',[]) if r.get('status')=='enrolled_before_outcome'}
            for opportunity in opportunities:
                proof=enrolled.get(opportunity['run_attempt_id'])
                opportunity['holdout_enrollment_id']=proof.get('enrollment_id') if proof else None
                positive=bool(proof and any(t.get('code')==opportunity['code'] and t.get('confidence_for')==opportunity['confidence_for'] for t in proof.get('eligible_target_scope',proof['initial_targets'])))
                opportunity['independent_holdout_eligible']=positive
                if not positive:gaps.append({'opportunity_id':opportunity['opportunity_id'],'reason':'positive_pre_outcome_enrollment_missing'})
            if C.now()<end:gaps.append({'reason':'prospective_period_in_progress'})
        imports_read=S.records('market_imports');gaps.extend(imports_read['errors'])
        imports=[r for r in imports_read['records'] if r.get('validation_id')==validation_id]
        simulation_inputs=_simulation_inputs(events,opportunities,manifest,imports)
        previous=get_inputs(validation_id)
        revision=(previous or {}).get('input_revision',0)+1
        bundle={'validation_id':validation_id,'input_id':iid,'input_revision':revision,
            'supersedes_input_id':(previous or {}).get('input_id'),'version':VERSION,'created_at':C.now().isoformat(),
            'market_as_of':min(C.now(),end).isoformat(),'knowledge_as_of':cutoff.isoformat(),
            'proposal':copy.deepcopy(manifest['proposal_snapshot']),'proposal_snapshot':copy.deepcopy(manifest['proposal_snapshot']),
            'source_bundle':copy.deepcopy(manifest['source_bundle']),'source_refs':sources,
            'opportunities':opportunities,'opportunity_count':len(opportunities),'attempts':attempt_records,
            'system_attempts':system_attempts,
            'events':events,'records':copy.deepcopy(events),'original_calls':all_calls,'engine_definitions':definitions,
            'execution_context':context,'deliveries':deliveries,'selection_runs':selection_runs,'runtime_inputs':runtime,
            'condition_groups':copy.deepcopy(manifest['source_bundle'].get('condition_groups',{})),
            'all_discovery_cases':copy.deepcopy(manifest['source_bundle'].get('all_cases',manifest['source_bundle'].get('cases',[]))),
            'holdout':enrollment,'complete':not gaps,'recording_status':'incomplete' if gaps else 'recorded','gaps':gaps,
            'simulation_inputs':simulation_inputs,
            'market_imports':imports,
            'source_execution_policy':'archived_source_is_data_only; registered_current_adapters_only',
            'denominator':'original_run_attempt_code_confidence_for; includes_unsubmitted_rejected_watch'}
        S.archive('inputs',iid,bundle)
        S.archive('input_commits',iid,{'validation_id':validation_id,'input_id':iid,'input_hash':S.digest(bundle),'created_at':bundle['created_at'],'input_revision':revision})
        return bundle
