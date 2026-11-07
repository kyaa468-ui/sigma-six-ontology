"""Read-only replay of explicitly supported, trusted repository engines.

Archived source is evidence, never executable input. Only function definitions
from this installation are compiled, and only after their source matches the
archived definition. All file reads, clocks and environment lookups are supplied
by the recorded context. The module never patches a live trading module.
"""
from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import inspect
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import common as C

VERSION = 'LOCAL_ENGINE_REPLAY@v1'
ROOT = Path(__file__).resolve().parent


class ReplayUnavailable(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def instant(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ReplayUnavailable('runtime_timezone_missing')
    return value.astimezone(C.KST)


def source_bundle(names=('common.py', 'sizing.py', 'risk.py')):
    return {name: (ROOT / name).read_text(encoding='utf-8') for name in names}


def check_definition(reference, definitions, names=('common.py', 'sizing.py', 'risk.py'), *, active_policy=False):
    identifier = (reference or {}).get('definition_id')
    entry = (definitions or {}).get(identifier) or {}
    body = entry.get('definition', entry)
    if not identifier or not body.get('sources'):
        raise ReplayUnavailable('original_engine_definition_missing')
    if body.get('sha256') != digest(body['sources']) or reference.get('sha256') != body['sha256']:
        raise ReplayUnavailable('original_engine_definition_hash_mismatch')
    if (body.get('source_identity') or {}).get('status') != 'verified_loaded_source':
        raise ReplayUnavailable('original_loaded_definition_unverified')
    if active_policy:
        identity=(body.get('source_identity') or {}).get('policy_interpreter') or {}
        if identity.get('status')!='verified_loaded_source' or body.get('policy_source_identity')!=identity:
            raise ReplayUnavailable('original_policy_definition_unverified')
        names=(*names,'policy_registry.py','condition_contract.py')
    current = source_bundle(names)
    if any(body['sources'].get(name) != source for name, source in current.items()):
        raise ReplayUnavailable('unsupported_engine_version')
    return {'engine_version': VERSION, 'definition_id': identifier,
            'source_hashes': {key: hashlib.sha256(value.encode()).hexdigest() for key, value in current.items()}}


def _logical(path):
    # Snapshot paths are metadata only; no archived path is opened.
    return str(path).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]


class FrozenReads:
    def __init__(self, rows, *, overrides=None, snapshots=None, strict=True):
        if not isinstance(rows, list):
            raise ReplayUnavailable('actual_read_sequence_missing')
        self.rows = copy.deepcopy(rows)
        self.position = 0
        self.overrides = copy.deepcopy(overrides or {})
        self.snapshots = copy.deepcopy(snapshots or {})
        self.strict = strict
        self.consumed = []
        if any(not isinstance(row, dict) or 'path' not in row or 'value' not in row
               or row.get('status') == 'recording_error' for row in rows):
            raise ReplayUnavailable('actual_read_sequence_incomplete')

    def read(self, path, default=None):
        key = _logical(path)
        if self.strict:
            if self.position >= len(self.rows):
                raise ReplayUnavailable('unrecorded_read:' + key)
            row = self.rows[self.position]
            if _logical(row['path']) != key:
                raise ReplayUnavailable('read_order_mismatch:' + key)
            self.position += 1
            value = row['value']
        else:
            if key not in self.snapshots and key not in self.overrides:
                raise ReplayUnavailable('unrecorded_branch_input:' + key)
            value = self.snapshots.get(key)
        self.consumed.append(key)
        return copy.deepcopy(self.overrides.get(key, value))

    def finish(self):
        if self.strict and self.position != len(self.rows):
            raise ReplayUnavailable('unused_recorded_reads')


def _compile_functions(name, namespace, selected=None):
    """Load trusted installed functions only, skipping all module-level I/O."""
    path = ROOT / name
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (selected is None or node.name in selected):
            node = copy.deepcopy(node)
            node.decorator_list = []
            nodes.append(node)
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace


def _blocked(*args, **kwargs):
    raise ReplayUnavailable('live_io_forbidden')


class Engine:
    """One isolated calculation; the captured RUN_MODE is a data input."""
    def __init__(self, runtime, reads, *, params=None, risk=None, overrides=None,
                 snapshots=None, strict=True, confidence_delta=0, policy_context=None):
        if not isinstance(runtime, dict) or not runtime.get('at') or not isinstance(runtime.get('environment'), dict):
            raise ReplayUnavailable('runtime_context_missing')
        module_state=runtime.get('module_state')
        if not isinstance(module_state,dict) or 'common._EXCH' not in module_state:
            raise ReplayUnavailable('runtime_module_state_missing')
        if module_state['common._EXCH'] is not None and not isinstance(module_state['common._EXCH'],dict):
            raise ReplayUnavailable('unsupported_runtime_module_state')
        self.at = instant(runtime['at'])
        env = runtime['environment']
        if 'RUN_MODE' not in env or 'RUN_ID' not in env:
            raise ReplayUnavailable('runtime_environment_incomplete')
        if env['RUN_MODE'] not in ('decide', 'view', 'exit', '', None):
            raise ReplayUnavailable('unsupported_original_execution_mode')
        self.runtime = copy.deepcopy(runtime)
        replacements = dict(overrides or {})
        if params is not None:
            replacements['params.json'] = params
        if risk is not None:
            replacements['risk_limits.json'] = risk
        self.reads = FrozenReads(reads, overrides=replacements, snapshots=snapshots, strict=strict)
        # No process environment, sockets, files or broker objects are exposed.
        local_os = SimpleNamespace(path=os.path, environ=dict(env))
        frozen_policy=copy.deepcopy(policy_context or {})
        import policy_registry as policy
        imports = {'copy': copy, 'math': math,
                   'policy_registry': SimpleNamespace(current_context=lambda: copy.deepcopy(frozen_policy),
                                                      evaluate_adjustment=policy.evaluate_adjustment,MARKER=policy.MARKER)}
        def import_module(name, *args, **kwargs):
            if name in imports:
                return imports[name]
            raise ReplayUnavailable('unregistered_engine_dependency:' + name)
        safe_builtins = dict(vars(builtins), open=_blocked, __import__=import_module)
        common_ns = {**vars(C), '__builtins__': safe_builtins, 'os': local_os,
                     'date': date, 'datetime': datetime, 'timedelta': timedelta, 'timezone': timezone}
        _compile_functions('common.py', common_ns)
        common_ns.update(now=lambda: self.at, today=lambda: self.at.date(),
                         _EXCH=copy.deepcopy(module_state['common._EXCH']),
                         read_json=self.reads.read, read_jsonl=lambda path: self.reads.read(path, []),
                         write_json=_blocked, append_jsonl=_blocked, journal=_blocked, load_env=_blocked)
        def observed_read(path,value,status='read'):
            # C.params may return the pinned value without calling read_json.
            captured=self.reads.read(path)
            if captured!=value: raise ReplayUnavailable('pinned_parameter_value_mismatch')
        common_ns['observed_read']=observed_read
        self.common_ns = common_ns
        self.common = SimpleNamespace(**common_ns)
        # Future active policy readers must use this explicit empty context,
        # never reach the current activation registry from historical replay.
        original_boost = common_ns.get('_conf_boost_details',common_ns['conf_boost_details'])
        def boost(*args, **kwargs):
            if 'policy_context' in inspect.signature(original_boost).parameters:
                kwargs['policy_context'] = copy.deepcopy(frozen_policy)
            result = original_boost(*args, **kwargs)
            if confidence_delta:
                result = {**result, 'before_cap': result['before_cap'] + confidence_delta,
                          'proposal_delta': confidence_delta}
                result['capped'] = min(result['cap'], result['before_cap'])
                result['adjusted'] = round(result['capped'], result['round_digits'])
            return result
        common_ns['conf_boost_details'] = boost
        self.common.conf_boost_details = boost
        sizing_ns = {'__builtins__': safe_builtins, 'C': self.common, 'math': math, 'copy': copy}
        _compile_functions('sizing.py', sizing_ns)
        self.sizing = SimpleNamespace(**sizing_ns)
        import risk as installed_risk
        risk_ns = {'__builtins__': safe_builtins, 'C': self.common, 'sizing': self.sizing,
                   'copy': copy, 'os': local_os, 'RULE_CHECKS': copy.deepcopy(installed_risk.RULE_CHECKS),
                   'RULE_EXPRESSIONS': copy.deepcopy(installed_risk.RULE_EXPRESSIONS)}
        _compile_functions('risk.py', risk_ns)
        self.risk = SimpleNamespace(**risk_ns)

    def finish(self):
        self.reads.finish()


def _runtime(audit, fallback=None):
    value = audit.get('runtime_context') or fallback
    if not value:
        raise ReplayUnavailable('runtime_context_missing')
    return value


def _supplied(audit, name):
    context = audit.get('runtime_context') or {}
    present = context.get('explicit_inputs')
    if isinstance(present, dict) and name in present:
        return bool(present[name])
    # Legacy recordings cannot establish whether a configured argument was
    # read before or inside the recorded function. Do not guess from filenames.
    raise ReplayUnavailable('argument_presence_missing:' + name)


def _confidence(detail, params, delta=0, policy_context=None):
    """Recalculate recorded operands; never substitute the latest RSI/regime."""
    if not all(k in detail for k in ('raw', 'code', 'side', 'inputs', 'params', 'before_cap', 'adjusted')):
        raise ReplayUnavailable('confidence_inputs_missing')
    raw = detail['raw']
    if type(raw) not in (int, float) or not math.isfinite(raw):
        raise ReplayUnavailable('confidence_raw_missing')
    inputs = detail['inputs']
    if not all(k in inputs for k in ('rsi', 'rsi_field', 'bad_news', 'regime')):
        raise ReplayUnavailable('confidence_operands_missing')
    add = 0.0
    rb, mb = params.get('rsi_boost'), params.get('market_boost')
    if rb and inputs['rsi'] is not None:
        if detail['side'] == '매수' and inputs['rsi'] < rb['buy_below'] and not inputs['bad_news']:
            add += rb['add']
        elif detail['side'] == '매도' and inputs['rsi'] >= rb['sell_at']:
            add += rb['add']
    if mb and detail['side'] == '매수' and inputs['regime'] in mb['regimes'] and not inputs['bad_news']:
        add += mb['add']
    if policy_context:
        import policy_registry as policy
        applied=policy.evaluate_adjustment(policy_context,detail['code'],detail['side'],raw,params=params)
        add+=applied['amount']
    before = raw + add + delta
    return {'raw': raw, 'before_cap': before, 'capped': min(1.0, before),
            'adjusted': round(min(1.0, before), 3), 'proposal_delta': delta}


def _effect(proposal, context):
    if not proposal or proposal.get('kind') == 'retain_current':
        return None, 'not_applicable'
    from condition_contract import evaluate_condition, validate_condition
    validate_condition(proposal['condition'])
    matched = evaluate_condition(proposal['condition'], context or {})
    if (proposal.get('effect') or {}).get('operation') == 'set_parameter':
        # The deployed JSON parameter is global. The proposal condition marks
        # its analysis cohort, never narrows the operational parameter change.
        return proposal['effect'], ('global_parameter:matched' if matched is True else
                                     'global_parameter:unknown' if matched is None else 'global_parameter:not_matched')
    return (proposal.get('effect') if matched is True else None,
            'matched' if matched is True else 'unknown' if matched is None else 'not_matched')


def changed_params(params, proposal, effect):
    result = copy.deepcopy(params)
    if effect and effect.get('operation') == 'set_parameter':
        target = proposal.get('target_snapshot') or {}
        key = target.get('key') or (proposal.get('target_ref') or {}).get('rule_id')
        if not isinstance(key, str) or '.' not in key:
            raise ReplayUnavailable('parameter_binding_missing')
        section, leaf = key.split('.', 1)
        if section not in result or leaf not in result[section]:
            raise ReplayUnavailable('parameter_input_missing')
        result[section][leaf] = effect['value']
    return result


PLAN_FIELDS = ('side', 'code', 'qty', 'price', 'amount', 'confidence', 'target_weight', 'regime',
               'equity_cap', 'limited_by', 'stop', 'take', 'max_days', 'high_vol', 'dip', 'index_dip',
               'inverse', 'index', 'buy_threshold', 'probe', 'held_qty', 'partial', 'rule_exit')


def projection(plan):
    return {key: copy.deepcopy(plan[key]) for key in PLAN_FIELDS if key in plan}


def _plan(event, proposal, context):
    inputs = event['inputs']; audit = inputs['sizing']; recorded = event['plan']
    values = audit['inputs']; params = values['params']; risk = values.get('risk')
    effect, condition = _effect(proposal, context)
    candidate_params = changed_params(params, proposal, effect)
    delta = effect.get('amount', 0) if effect and effect.get('operation') == 'add_confidence' else 0
    if effect and effect.get('operation') == 'prompt_guidance':
        raise ReplayUnavailable('prompt_reinvocation_required')
    original_policy=inputs.get('policy_context')
    if inputs['confidence'].get('policy') and not original_policy:
        raise ReplayUnavailable('active_policy_context_missing')
    retained_policy=original_policy if proposal is None else {}
    confidence = _confidence(inputs['confidence'], candidate_params, delta, retained_policy)
    engine = Engine(_runtime(audit), audit['actual_reads'], params=candidate_params, risk=risk,policy_context=retained_policy)
    side, code, price, account = recorded['side'], values['code'], values['price'], values['account']
    p_arg = candidate_params if _supplied(audit, 'p') else None
    if side == '매수':
        r_arg = risk if _supplied(audit, 'r') else None
        result = engine.sizing.plan_buy(code, confidence['adjusted'], price, copy.deepcopy(account),
                                        p=p_arg, r=r_arg, regime=values.get('regime') if _supplied(audit, 'regime') else None,
                                        today_change=values.get('today_change'))
    elif side == '매도':
        result = engine.sizing.plan_sell(code, confidence['adjusted'], price, copy.deepcopy(account),
                                         p=p_arg, rule_exit=values.get('rule_exit', False))
    else:
        raise ReplayUnavailable('unsupported_side')
    engine.finish()
    return {'plan': projection(result), 'confidence': confidence, 'condition_status': condition,
            'read_sequence': engine.reads.consumed}


def _risk(event, proposal, context):
    audit = event['inputs']; values = audit['inputs']; params = values['params']; limits = values['risk']
    effect, condition = _effect(proposal, context)
    candidate_params = changed_params(params, proposal, effect)
    delta = effect.get('amount', 0) if effect and effect.get('operation') == 'add_confidence' else 0
    if effect and effect.get('operation') == 'prompt_guidance':
        raise ReplayUnavailable('prompt_reinvocation_required')
    original_policy=audit.get('policy_context')
    if (audit.get('confidence') or {}).get('policy') and not original_policy:
        raise ReplayUnavailable('active_policy_context_missing')
    engine = Engine(_runtime(audit), audit['actual_reads'], params=candidate_params, risk=limits, confidence_delta=delta,
                    policy_context=original_policy if proposal is None else {})
    result = engine.risk.check(copy.deepcopy(values['order']), copy.deepcopy(values['account']), values['price'],
                              p=candidate_params if _supplied(audit, 'p') else None,
                              r=limits if _supplied(audit, 'r') else None)
    engine.finish()
    return {'violations': [list(row) for row in result], 'condition_status': condition,
            'read_sequence': engine.reads.consumed}


def replay_event(event, definitions, proposal=None, condition_context=None):
    """Return explicit baseline failure before attempting a counterfactual."""
    result = {'event_id': event.get('event_id'), 'kind': event.get('kind'),
              'baseline_status': 'incomplete', 'effect_status': 'not_run', 'engine_version': VERSION}
    try:
        policy_context=(event.get('inputs') or {}).get('policy_context') or {}
        result['engine'] = check_definition(event.get('definition'), definitions,
                                            active_policy=bool((policy_context.get('policy') or {}).get('policy_revision')))
        if event.get('kind') == 'order_plan':
            execute = _plan
            expected = {'plan': projection(event['plan'])}
        elif event.get('kind') == 'risk_evaluation':
            execute = _risk
            expected = {'violations': [list(row) for row in event.get('violations', event['inputs']['violations'])]}
        else:
            raise ReplayUnavailable('unsupported_event_kind')
        baseline = execute(event, None, condition_context)
        same = all(baseline.get(key) == value for key, value in expected.items())
        if event.get('kind') == 'order_plan':
            original = event['inputs']['confidence']
            same = same and all(baseline['confidence'][key] == original.get(key) for key in ('raw', 'before_cap', 'adjusted'))
        result.update(baseline=baseline, expected=expected, baseline_status='passed' if same else 'failed')
        if not same:
            return {**result, 'reason': 'baseline_result_mismatch'}
        candidate = execute(event, proposal, condition_context)
        result.update(candidate=candidate, effect_status='complete',
                      changed=any(candidate.get(key) != baseline.get(key) for key in expected),
                      direct_effect_only=True, financial_performance_verified=False)
        return result
    except (ReplayUnavailable, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        result['reason'] = str(exc)
        if result['baseline_status'] == 'passed':
            result['effect_status'] = 'incomplete'
        return result


class MemoryTrace:
    def __init__(self): self.records = {}
    def screen_input(self, *args): pass
    def screen_output(self, *args): pass
    def begin(self, row, px):
        ma20 = row.get('ma20')
        item = {'price_comparison': {'gap': px/ma20-1 if px is not None and ma20 else None,
                                     'above': px > ma20 if px is not None and ma20 is not None else None}}
        self.records[row['code']] = item
        return item
    def gate(self, *args): pass
    def trend(self, *args): pass
    def flow(self, *args): pass
    def ranked(self, *args): pass
    def attach(self, rows, route=None):
        for row in rows:
            row['_ontology']={'admission':{'route':route or ('high_vol' if row.get('high_vol') else 'general')}}


def selection_projection(rows):
    return [{'code':r['code'],'score':r.get('score'),
             **{key:bool(r.get(key)) for key in ('high_vol','dip','from_prep','inverse','index')},
             'admission_route':((r.get('_ontology') or {}).get('admission') or {}).get('route')}
            for r in rows]


def replay_selection(selection, proposal=None):
    """Reproduce the frozen screen, and recorded append/retain orchestration."""
    result = {'selection_run_id': selection.get('run_id'), 'scope': 'screen_only',
              'baseline_status': 'incomplete', 'effect_status': 'not_run'}
    try:
        manifest = selection.get('input_manifest') or {}
        if manifest.get('complete') is not True or not isinstance(manifest.get('ordered_codes'), list):
            raise ReplayUnavailable('ordered_screen_input_completeness_missing')
        sources = selection.get('engine_sources') or {}
        name = 'data_prep.py' if selection['kind'] == 'prep' else 'intraday.py'
        if any(sources.get(n) != body for n, body in source_bundle(('common.py', name)).items()):
            raise ReplayUnavailable('unsupported_screen_engine_version')
        if (selection.get('source_identity') or {}).get('status') != 'verified_loaded_source':
            raise ReplayUnavailable('screen_loaded_definition_unverified')
        rows = {x['engine_input']['code']: x['engine_input'] for x in selection['observations']}
        ordered = manifest['ordered_codes']
        if len(set(ordered)) != len(ordered) or not set(ordered) <= set(rows):
            raise ReplayUnavailable('screen_input_pool_incomplete')
        import pandas as pd
        # screen functions use only pure common helpers; no runtime file read is
        # allowed even if a future engine revision introduces one.
        runtime = selection.get('runtime_context')
        if not runtime: raise ReplayUnavailable('selection_runtime_context_missing')
        engine = Engine(runtime, [], strict=True)
        def mark_route(row, route, run_id=None):
            row['_ontology']={'admission':{'route':route}}
        ns = {'C':engine.common, 'pd':pd, 'O':SimpleNamespace(
            clean=lambda x: None if isinstance(x,float) and math.isnan(x) else x, mark_route=mark_route)}
        _compile_functions(name, ns, {'screen','rank_pct','fill_up','dips','etf'})
        params = selection['params']
        context = selection.get('context') or {}
        def run(p, pipeline=False):
            inputs = [copy.deepcopy(rows[code]) for code in ordered]
            trace = MemoryTrace()
            if name == 'data_prep.py':
                if 'login' not in manifest: raise ReplayUnavailable('screen_login_missing')
                output = ns['screen'](inputs,p,manifest['login'],selection.get('regime'),trace=trace)
                if pipeline:
                    if not isinstance(context.get('append_rows'),list):raise ReplayUnavailable('prep_append_rows_missing')
                    appended=copy.deepcopy(context['append_rows'])
                    for row in appended:trace.attach([row],'inverse' if row.get('inverse') else 'index')
                    output += appended
            else:
                frac = context.get('elapsed_fraction')
                if frac is None: raise ReplayUnavailable('screen_elapsed_fraction_missing')
                output = ns['screen'](inputs,p,selection['risk'],frac,selection.get('regime'),trace=trace)
                if pipeline:
                    if not all(key in context for key in ('prior_candidates','prep_candidates','held')):
                        raise ReplayUnavailable('intraday_append_context_missing')
                    if not output:
                        output = copy.deepcopy(context['prior_candidates'])
                    else:
                        output = ns['fill_up'](output,copy.deepcopy(context['prep_candidates']),set(context['held']),
                                               (p['screen'].get('intraday') or {}).get('min_candidates',0))
                        extra = ns['dips'](inputs,output,set(context['held']),p,engine.at)
                        trace.attach(extra,'dip');output += extra
            return selection_projection(output) if pipeline else [{k:r.get(k) for k in ('code','score','high_vol')} for r in output]
        expected = manifest.get('screen_result')
        if not isinstance(expected,list): raise ReplayUnavailable('screen_result_missing')
        baseline = run(copy.deepcopy(params))
        result.update(baseline=baseline, expected=expected, baseline_status='passed' if baseline==expected else 'failed')
        if baseline != expected: return {**result,'reason':'baseline_result_mismatch'}
        effect = (proposal or {}).get('effect')
        if effect and effect.get('operation') in ('add_confidence','prompt_guidance'):
            effect = None  # These operate after the candidate engine.
        if effect and effect.get('operation') != 'set_parameter': raise ReplayUnavailable('unsupported_screen_effect')
        # Scalar candidate parameters are global controls. Conditional per-row
        # threshold rules require their own registered engine, not silent reuse.
        candidate = run(changed_params(params,proposal,effect))
        result.update(candidate=candidate,changed=candidate!=baseline,effect_status='complete',
                new_candidate_codes=sorted({x['code'] for x in candidate}-{x['code'] for x in baseline}),
                downstream_ai_scores='not_inferred',financial_performance_verified=False)
        if context.get('pipeline_complete') is not True:
            return {**result,'pipeline_status':'incomplete','pipeline_reason':'append_or_retain_inputs_incomplete'}
        final = manifest.get('final_result')
        if not isinstance(final,list):
            return {**result,'pipeline_status':'incomplete','pipeline_reason':'final_candidate_result_missing'}
        original_pipeline = run(copy.deepcopy(params),True)
        if original_pipeline != final:
            return {**result,'pipeline_status':'failed','pipeline_baseline':original_pipeline,
                    'pipeline_expected':final,'pipeline_reason':'final_candidate_result_mismatch'}
        candidate_pipeline = run(changed_params(params,proposal,effect),True)
        return {**result,'scope':'full_candidate_pipeline','pipeline_status':'passed',
                'pipeline_baseline':original_pipeline,'pipeline_candidate':candidate_pipeline,
                'pipeline_changed':original_pipeline!=candidate_pipeline,
                'new_candidate_codes':sorted({x['code'] for x in candidate_pipeline}-{x['code'] for x in original_pipeline})}
    except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
        return {**result,'reason':str(exc)}
