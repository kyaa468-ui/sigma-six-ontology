"""Step-7 orchestration: reproduce first, then compare, never activate here."""
from __future__ import annotations
import copy
import uuid
import common as C
import validation_store as S
import validation_spec as Spec
import validation_dataset as Dataset
import replay_context as R
import simulation

VERSION = 'POLICY_VALIDATION_RESULT@v1'


def _independence(validation_id, manifest, bundle):
    import validation_holdout as H
    if manifest['spec']['mode'] != 'prospective':
        return {'status': 'unverified', 'reason': 'discovery_replay_is_not_independent'}
    try:
        proof = H.status(validation_id)
        enrolled = {r['run_attempt_id']: r for r in proof.get('enrollments', [])}
        missing = [o['opportunity_id'] for o in bundle.get('opportunities', [])
                   if o.get('run_attempt_id') not in enrolled]
        complete_period = C.now() >= R.instant(manifest['spec']['period']['end'])
        verified = proof.get('independent') and not missing and complete_period and bundle.get('complete') is True
        return {'status': 'verified' if verified else 'unverified', 'proof': proof,
                'missing_enrollment_opportunities': missing, 'registered_period_ended': complete_period}
    except Exception as exc:
        return {'status': 'unverified', 'reason': str(exc)}


def _cases(bundle, proposal):
    opportunities = bundle.get('opportunities', [])
    by_id = {o['opportunity_id']: o for o in opportunities}
    output, event_results = [], []
    for event in bundle.get('events', bundle.get('records', [])):
        if event.get('kind') not in ('order_plan', 'risk_evaluation'):
            continue
        ids = event.get('opportunity_ids', [])
        matching = [by_id[i] for i in ids if i in by_id]
        jid=event.get('judgment_id') or (event.get('payload') or {}).get('judgment_id')
        contexts = [(o.get('condition_contexts_by_judgment') or {}).get(jid,o.get('condition_context')) for o in matching]
        context = contexts[0] if contexts and all(c == contexts[0] for c in contexts) else {}
        row = R.replay_event(event, bundle.get('engine_definitions', {}), proposal, context)
        row.update(opportunity_ids=ids, original_call_kind=event['kind'])
        event_results.append(row)
    for opportunity in opportunities:
        oid = opportunity['opportunity_id']
        events = [e for e in event_results if oid in e['opportunity_ids']]
        reasons = []
        if not opportunity.get('judgments'):
            reasons.append('original_ai_judgment_missing')
        if not events:
            reasons.append('original_supported_call_missing')
        if any(e.get('baseline_status') != 'passed' for e in events):
            reasons.append('baseline_not_reproduced')
        if any(e.get('effect_status') != 'complete' for e in events):
            reasons.append('counterfactual_call_incomplete')
        output.append({'opportunity_id': oid, 'code': opportunity.get('code'),
            'confidence_for': opportunity.get('confidence_for'), 'origin_at': opportunity.get('origin_at'),
            'coverage_status': opportunity.get('coverage_status'), 'source_manifest': opportunity.get('source_manifest'),
            'judgment_ids': opportunity.get('judgment_ids', []), 'condition_context': opportunity.get('condition_context'),
            'calls': events, 'status': 'complete' if not reasons else 'incomplete', 'reasons': reasons,
            'changed': any(e.get('changed') for e in events)})
    orphan = [e for e in event_results if not any(i in by_id for i in e['opportunity_ids'])]
    return output, event_results, orphan


def _criteria(manifest, cases, performance):
    spec = manifest['spec']; checks = []
    if not manifest.get('criteria_complete'):
        return {'status': 'not_evaluated', 'reason': 'predeclared_criteria_incomplete',
                'missing': manifest.get('missing_criteria', [])}
    if performance.get('status') != 'complete':
        return {'status': 'not_evaluated', 'reason': 'paired_performance_incomplete'}
    affected = [c for c in cases if c.get('changed')]
    stats = performance.get('statistics', {})
    counts = {'sessions': stats.get('paired_sessions', 0),
              'affected_sessions': len({c.get('origin_at', '')[:10] for c in affected}),
              'affected_decisions': len(affected), 'symbols': len({c['code'] for c in cases})}
    for key, minimum in spec['minimums'].items():
        checks.append({'criterion': 'minimums.' + key, 'actual': counts[key], 'threshold': minimum,
                       'passed': counts[key] >= minimum})
    base, changed = (performance['arms'][arm]['metrics'] for arm in ('base', 'changed'))
    actual = {'max_drawdown': changed['max_drawdown'],
              'max_drawdown_degradation': changed['max_drawdown'] - base['max_drawdown'],
              'min_net_gain': performance['net_equity_difference'],
              'min_paired_completion_rate': performance['coverage']['paired_completion_rate']}
    for key, threshold in spec['risk_limits'].items():
        value = actual[key]
        passed = value is not None and (value <= threshold if key.startswith('max_') else value >= threshold)
        checks.append({'criterion': 'risk_limits.' + key, 'actual': value, 'threshold': threshold, 'passed': passed})
    checks.append({'criterion': 'paired_uncertainty_interval', 'passed': stats.get('status') == 'complete',
                   'actual': stats.get('status')})
    # A positive point estimate alone cannot pass the predeclared family test.
    checks.append({'criterion': 'family_adjusted_positive_effect',
                   'passed': stats.get('status') == 'complete' and stats['interval'][0] > 0,
                   'actual': stats.get('interval')})
    return {'status': 'passed' if all(c['passed'] for c in checks) else 'failed', 'checks': checks}


def _report(validation_id, manifest, bundle, mode, result_id):
    proposal = manifest['proposal_snapshot']; spec = manifest['spec']
    cases, events, orphan = _cases(bundle, proposal)
    selections = [R.replay_selection(row, proposal) for row in bundle.get('selection_runs', [])]
    selection_ok = all(s.get('baseline_status') == 'passed' for s in selections)
    pipeline_ok = bool(selections) and all(s.get('pipeline_status') == 'passed' for s in selections)
    whole_complete = bundle.get('complete') is True and bool(cases) and all(c['status'] == 'complete' for c in cases) and not orphan and selection_ok
    baseline = 'passed' if whole_complete and events and all(e.get('baseline_status') == 'passed' for e in events) else 'failed' if any(e.get('baseline_status') == 'failed' for e in events) else 'incomplete'
    effect = 'complete' if baseline == 'passed' and all(e.get('effect_status') == 'complete' for e in events) else 'incomplete'
    if mode == 'screen_only':
        baseline='passed' if selections and all(s.get('baseline_status')=='passed' for s in selections) else 'failed' if any(s.get('baseline_status')=='failed' for s in selections) else 'incomplete'
        effect='complete' if baseline=='passed' and all(s.get('effect_status')=='complete' for s in selections) else 'incomplete'
    performance = {'status': 'not_run', 'reason': 'mode_is_single_call_replay', 'comparison_kind': 'single_call_only'}
    if mode == 'portfolio':
        if baseline != 'passed':
            performance = {'status': 'incomplete', 'reason': 'baseline_reproduction_required'}
        elif any(s.get('pipeline_changed') for s in selections):
            performance = {'status':'incomplete','reason':'changed_candidate_pipeline_requires_resolved_ai_decision_stream',
                'new_candidate_codes':sorted({code for s in selections for code in s.get('new_candidate_codes',[])}),
                'missing_ai_scores_are_not_invented':True}
        else:
            performance = simulation.run_pair(bundle.get('simulation_inputs'), spec, proposal)
    elif mode == 'screen_only':
        performance = {'status': 'not_run', 'reason': 'screen_only_has_no_ai_or_portfolio_effect', 'comparison_kind': 'screen_only'}
    independent = _independence(validation_id, manifest, bundle)
    criteria = _criteria(manifest, cases, performance)
    global_scope = ((proposal.get('effect') or {}).get('operation') != 'set_parameter' or
                    (spec.get('scope',{}).get('codes') == ['*'] and
                     set(spec.get('scope',{}).get('confidence_for',[])) == {'매수','매도'})) if proposal else True
    gates = {'baseline_reproduction': baseline == 'passed', 'whole_opportunity_coverage': whole_complete,
             'counterfactual_calls': effect == 'complete', 'independent_validation': independent['status'] == 'verified',
             'paired_performance': performance.get('status') == 'complete', 'predeclared_criteria': criteria['status'] == 'passed',
             'portfolio_mode': mode == 'portfolio', 'candidate_pipeline_reproduced': pipeline_ok,
             'global_parameter_scope': global_scope,
             'historical_market_availability_verified': (bundle.get('simulation_inputs') or {}).get('historical_market_availability_verified') is True,
             'registered_period_complete': C.now() >= R.instant(spec['period']['end'])}
    reasons = [key for key, passed in gates.items() if not passed]
    status = 'failed' if baseline == 'failed' else 'complete' if baseline == 'passed' and (mode != 'portfolio' or performance.get('status') == 'complete') else 'insufficient_data'
    return {'validation_id': validation_id, 'result_id': result_id, 'version': VERSION,
        'created_at': C.now().isoformat(), 'status': status, 'recording_status': 'recorded',
        'mode': mode, 'proposal': copy.deepcopy(proposal), 'spec': copy.deepcopy(spec),
        'input_id': bundle.get('input_id', bundle.get('bundle_id')), 'input_hash': S.digest(bundle),
        'source_bundle_hash': manifest.get('source_bundle_hash'), 'baseline_status': baseline,
        'effect_status': effect, 'performance': performance, 'criteria': criteria,
        'independent_validation': independent, 'adoption_eligibility': {'status': 'eligible' if not reasons else 'ineligible', 'reasons': reasons, 'gates': gates},
        'coverage': {'expected_opportunities': len(cases), 'complete_opportunities': sum(c['status'] == 'complete' for c in cases),
                     'supported_calls': len(events), 'unassigned_calls': len(orphan), 'bundle_complete': bundle.get('complete') is True},
        'cases': cases, 'events': events, 'selection_replays': selections, 'unassigned_calls': orphan,
        'gaps': copy.deepcopy(bundle.get('gaps', [])), 'operational_activation': False,
        'limitations': ['single_call_effect_and_sequential_portfolio_effect_are_separate',
                       'frozen_original_ai_judgments_do_not_represent_adaptive_ai',
                       'missing_historical_inputs_are_not_reconstructed_from_current_values']}


def run_validation(validation_id, mode='replay'):
    """Deterministic local execution only. AI comparisons have a separate entrypoint."""
    if mode not in ('replay', 'portfolio', 'screen_only'):
        return {'validation_id': validation_id, 'status': 'rejected', 'reason': 'unsupported_validation_mode'}
    result_id = None
    try:
        manifest = Spec.load_spec(validation_id)
        bundle = Dataset.ensure_inputs(validation_id)
        if not bundle or bundle.get('recording_status') == 'recording_error':
            raise ValueError('VALIDATION_INPUT_RECORDING_ERROR')
        result_id = 'validation-result-' + S.digest([validation_id, mode, S.digest(bundle), VERSION, R.source_bundle()])[:40]
        with S.locked('result-' + validation_id):
            previous = S.load('results', result_id)
            if previous:
                commit = S.load('result_commits', result_id)
                if not commit:
                    S.archive('result_commits', result_id, {'result_id': result_id, 'validation_id': validation_id,
                        'result_hash': S.digest(previous), 'committed_at': previous['created_at']})
                return get_policy_validation(result_id)
            attempt_id = 'validation-attempt-' + uuid.uuid4().hex
            S.archive('attempts', attempt_id, {'attempt_id': attempt_id, 'validation_id': validation_id,
                'result_id': result_id, 'mode': mode, 'started_at': C.now().isoformat(), 'status': 'started'})
            result = _report(validation_id, manifest, bundle, mode, result_id)
            S.archive('results', result_id, result)
            S.archive('result_commits', result_id, {'result_id': result_id, 'validation_id': validation_id,
                'result_hash': S.digest(result), 'committed_at': result['created_at'], 'attempt_id': attempt_id})
            if mode == 'portfolio' and result['status'] == 'complete' and manifest['spec']['mode'] == 'prospective':
                import validation_holdout as H
                H.release(validation_id,result_id,'consumed-'+result_id)
            return {**result, 'validation_result_hash': S.digest(result)}
    except Exception as exc:
        return {'validation_id': validation_id, 'result_id': result_id, 'status': 'recording_error',
                'recording_status': 'recording_error', 'adoption_eligibility': {'status': 'ineligible', 'reasons': ['validation_execution_error']},
                'errors': [S.record_error(validation_id, 'run_validation', exc)]}


def get_policy_validation(identifier):
    try:
        result = S.load('results', identifier)
        if result is None:
            commits = S.records('result_commits')
            if commits['errors']:
                raise ValueError('RESULT_COMMIT_HISTORY_UNREADABLE')
            matches = [r for r in commits['records'] if r.get('validation_id') == identifier]
            if not matches:
                attempts = S.records('attempts')
                errors = S.errors(identifier)
                own = [r for r in attempts['records'] if r.get('validation_id') == identifier]
                issues = attempts['errors'] + errors['records'] + errors['errors']
                return {'validation_id': identifier, 'status': 'recording_error' if issues else 'unfinished' if own else 'not_run',
                        'lifecycle_status': 'recording_error' if issues else 'unfinished' if own else 'not_started',
                        'attempts': own, 'errors': issues, 'expected_count': None,
                        'recording_status': 'recording_error' if issues else 'incomplete',
                        'adoption_eligibility': {'status': 'ineligible', 'reasons': ['no_committed_result']}}
            latest = max(matches, key=lambda r: (r['committed_at'], r['result_id']))
            result = S.load('results', latest['result_id'])
        if not result:
            raise ValueError('COMMITTED_RESULT_MISSING')
        commit = S.load('result_commits', result['result_id'])
        if not commit or commit.get('result_hash') != S.digest(result):
            raise ValueError('RESULT_NOT_COMMITTED')
        return {**result, 'validation_result_hash': commit['result_hash']}
    except Exception as exc:
        return {'validation_id': identifier, 'status': 'recording_error', 'recording_status': 'recording_error',
                'errors': [{'reason': str(exc)}], 'adoption_eligibility': {'status': 'ineligible', 'reasons': ['result_integrity_error']}}


def get_validation_cases(validation_id, offset=0, limit=50):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        return {'status': 'rejected', 'reason': 'invalid_pagination'}
    report = get_policy_validation(validation_id)
    cases = report.get('cases', [])
    known = 'cases' in report
    return {'validation_id': report.get('validation_id'), 'result_id': report.get('result_id'),
            'status': report['status'], 'offset': offset, 'limit': limit, 'total': len(cases) if known else None,
            'count_known': known, 'errors': report.get('errors', []),
            'adoption_eligibility': report.get('adoption_eligibility'),
            'cases': cases[offset:offset+limit], 'validation_result_hash': report.get('validation_result_hash')}


def finalize_prompt_validation(validation_id, experiment_id):
    """Seal an explicit two-arm AI experiment; no provider is called here."""
    result_id = None
    try:
        manifest=Spec.load_spec(validation_id);spec=manifest['spec']
        trial=S.load('prompt_results',experiment_id);commit=S.load('prompt_result_commits',experiment_id)
        if not trial or not commit or commit.get('result_hash') != S.digest(trial):
            raise ValueError('PROMPT_EXPERIMENT_NOT_COMMITTED')
        if trial.get('validation_id') != validation_id or trial.get('spec_hash') != S.digest(manifest):
            raise ValueError('PROMPT_EXPERIMENT_SPEC_MISMATCH')
        if trial.get('proposal_hash') != S.digest(manifest['proposal_snapshot']):
            raise ValueError('PROMPT_EXPERIMENT_PROPOSAL_MISMATCH')
        if spec.get('comparison_kind')!='prompt_pair' or trial.get('budget_usd')!=(spec.get('prompt') or {}).get('budget_usd'):
            raise ValueError('PROMPT_EXPERIMENT_CONTRACT_MISMATCH')
        bundle=Dataset.get_inputs(validation_id,trial.get('input_id'))
        if not bundle or S.digest(bundle)!=trial.get('input_hash'):
            raise ValueError('PROMPT_EXPERIMENT_INPUT_MISMATCH')
        result_id='validation-prompt-result-'+S.digest([validation_id,experiment_id,S.digest(trial),VERSION])[:40]
        with S.locked('result-'+validation_id):
            previous=S.load('results',result_id)
            if previous:
                if not S.load('result_commits',result_id):
                    S.archive('result_commits',result_id,{'result_id':result_id,'validation_id':validation_id,
                        'result_hash':S.digest(previous),'committed_at':previous['created_at']})
                return get_policy_validation(result_id)
            baseline_manifest={**manifest,'proposal_snapshot':None}
            result=_report(validation_id,baseline_manifest,bundle,'replay',result_id)
            result.update(mode='prompt_pair',proposal=copy.deepcopy(manifest['proposal_snapshot']),
                          prompt_experiment_id=experiment_id,prompt_experiment_hash=S.digest(trial))
            pairs=trial.get('pairs',[]);repetitions=trial.get('repetitions')
            expected={(o['opportunity_id'],rep) for o in bundle.get('opportunities',[]) for rep in range(repetitions or 0)}
            seen=set();valid_pairs=True
            for pair in pairs:
                target=pair.get('target') or {};oid=target.get('opportunity_id') if isinstance(target,dict) else target
                key=(oid,pair.get('repetition'))
                if key in seen or key not in expected or pair.get('status')!='paired':valid_pairs=False
                seen.add(key)
                identifiers=pair.get('trial_ids',[])
                if len(identifiers)!=2:valid_pairs=False
                for index,identifier in enumerate(identifiers):
                    saved=S.load('prompt_trials',identifier);request=S.load('prompt_requests',identifier)
                    if not saved or not request or request.get('experiment_id')!=experiment_id or request.get('model')!=trial.get('model') or saved.get('status')!='accepted':
                        valid_pairs=False
                    expected_target={k:target.get(k) for k in ('run_attempt_id','code','confidence_for')} if isinstance(target,dict) else {}
                    if not request or request.get('arm')!=('baseline' if index==0 else 'candidate') or request.get('repetition')!=pair.get('repetition') or {k:(request.get('target') or {}).get(k) for k in expected_target}!=expected_target:
                        valid_pairs=False
                saved_pair=[S.load('prompt_trials',i) for i in identifiers]
                if len(saved_pair)==2 and all(t and len(t.get('judgments',[]))==1 for t in saved_pair):
                    values=[{k:t['judgments'][0].get(k) for k in ('confidence_raw','recommended_action')} for t in saved_pair]
                    pair['changed']=values[0]!=values[1]
            valid_pairs=valid_pairs and bool(expected) and seen==expected
            simulations=trial.get('simulations',[])
            # Rebuild the experiment's execution result from immutable requests
            # and submissions. A saved success flag is not an execution proof.
            import prompt_validation as Prompt
            saved_trials=[S.load('prompt_trials',identifier) for identifier in trial.get('trial_ids',[])]
            reproduced=Prompt.simulate_trials(bundle,spec,saved_trials,repetitions)
            if S.digest(simulations)!=S.digest(reproduced):
                raise ValueError('PROMPT_SIMULATION_REPRODUCTION_MISMATCH')
            simulation_ids=[s.get('repetition') for s in simulations]
            simulations_complete=(set(simulation_ids)==set(range(repetitions or 0)) and len(simulation_ids)==len(set(simulation_ids))
                and all(s.get('result',{}).get('status')=='complete' for s in simulations))
            cases=copy.deepcopy(result['cases'])
            # Prompt comparisons define affected opportunities by submitted
            # arm decisions, not by a numeric policy replay of the old model.
            for case in cases:
                case['changed']=any(pair.get('changed') is True for pair in pairs
                    if ((pair.get('target') or {}).get('opportunity_id') if isinstance(pair.get('target'),dict) else pair.get('target'))==case['opportunity_id'])
            criteria=[_criteria(manifest,cases,s['result']) for s in simulations]
            result['cases']=cases
            result['effect_status']='complete' if valid_pairs else 'incomplete'
            result['performance']={'status':'complete' if simulations_complete else 'incomplete',
                'comparison_kind':'new_paired_model_comparison','repetitions':repetitions,'simulations':simulations,
                'historical_ai_reproduction_claimed':False,'unique_market_opportunities':len(bundle.get('opportunities',[]))}
            result['criteria']={'status':'passed' if criteria and all(c['status']=='passed' for c in criteria) and simulations_complete else 'failed',
                                'repetition_results':criteria,'repetitions_do_not_multiply_market_sample_count':True}
            gates=dict(result['adoption_eligibility']['gates'])
            gates.update(counterfactual_calls=valid_pairs,portfolio_mode=True,paired_performance=simulations_complete,
                predeclared_criteria=result['criteria']['status']=='passed',
                explicit_real_ai=trial.get('mode')=='explicit_real_ai',
                experiment_complete=trial.get('status')=='complete',
                registered_model=trial.get('model')==(spec.get('prompt') or {}).get('model'),
                registered_repetitions=repetitions==spec.get('repetitions'))
            reasons=[key for key,value in gates.items() if not value]
            result['adoption_eligibility']={'status':'eligible' if not reasons else 'ineligible','gates':gates,'reasons':reasons}
            result['status']='complete' if result['baseline_status']=='passed' and valid_pairs and simulations_complete else 'insufficient_data'
            S.archive('results',result_id,result)
            S.archive('result_commits',result_id,{'result_id':result_id,'validation_id':validation_id,
                'result_hash':S.digest(result),'committed_at':result['created_at']})
            if result['status']=='complete' and spec['mode']=='prospective':
                import validation_holdout as H
                H.release(validation_id,result_id,'consumed-'+result_id)
            return {**result,'validation_result_hash':S.digest(result)}
    except Exception as exc:
        return {'validation_id':validation_id,'result_id':result_id,'status':'recording_error','recording_status':'recording_error',
                'adoption_eligibility':{'status':'ineligible','reasons':['prompt_result_invalid']},
                'errors':[S.record_error(validation_id,'finalize_prompt_validation',exc)]}
