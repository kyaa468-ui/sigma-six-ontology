"""Explicit paired AI experiments against frozen receipts. No trading imports.

A registered experiment does not authorize spending: real CLI execution also
requires with_ai=True, an explicit model, repetitions and a total dollar budget.
"""
from __future__ import annotations
import copy
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import sys
import uuid
from pathlib import Path
import common as C
import validation_store as S
import record_store
import storage_objects
import log_store
from process_spool import Spool, raw_text
import evidence_wire as W
from datetime import datetime

TOOLS = ('get_briefing', 'get_account', 'record_ai_judgments', 'get_evidence_detail')


def experimental_receipts(receipts, arm):
    """Replace the prior adopted package without rewriting its real receipt.

    Remaining facts keep their original values; the experimental delivery gets
    a new ID and an explicit source link. Unknown historical rendering fails.
    """
    rows=copy.deepcopy(receipts)
    if arm!='candidate':return rows
    import ontology as O
    import lesson_registry as L
    next_alias = max((int(r['bundle_alias'][1:]) for r in rows if r.get('bundle_alias')), default=0)
    for index, receipt in enumerate(rows):
        lessons=(receipt.get('context') or {}).get('adopted_lessons') or []
        if not lessons:continue
        if receipt.get('kind')!='briefing':raise ValueError('UNSUPPORTED_PRIOR_LESSON_DELIVERY')
        if receipt.get('wire_format') == W.FORMAT:
            old_id = receipt['delivery_id']
            new_id = 'experiment-' + S.digest([old_id, 'replace_prior_package'])[:40]
            catalog = [copy.deepcopy(e) for e in receipt['evidence_catalog'] if e.get('kind') != 'adopted_lesson']
            for evidence in catalog:
                for key in ('evidence_id', 'observation_id'):
                    if isinstance(evidence.get(key), str):
                        evidence[key] = evidence[key].replace(old_id, new_id)
            next_alias += 1
            rows[index] = W.experimental_receipt(receipt, f'B{next_alias}', new_id, catalog)
            continue
        text=receipt['text'];old_catalog=O.render_catalog(receipt['evidence_catalog'])
        block='\n'.join(L.render_for_briefing({'lessons':lessons}))
        if text.count(block)!=1 or text.count(old_catalog)!=1:
            raise ValueError('ORIGINAL_LESSON_RENDERING_UNREPRODUCIBLE')
        old_id=receipt['delivery_id'];new_id='experiment-'+S.digest([old_id,'replace_prior_package'])[:40]
        catalog=[e for e in receipt['evidence_catalog'] if e.get('kind')!='adopted_lesson']
        for evidence in catalog:
            for key in ('evidence_id','observation_id'):
                if isinstance(evidence.get(key),str):evidence[key]=evidence[key].replace(old_id,new_id)
        receipt['text']=text.replace(block,'',1).replace(old_catalog,O.render_catalog(catalog),1).replace('브리핑 전달 ID: '+old_id,'브리핑 전달 ID: '+new_id)
        receipt.update(delivery_id=new_id,evidence_catalog=catalog,original_delivery_id=old_id,
                       experimental_transformation='prior_adopted_guidance_removed; original market values preserved')
        receipt['context']['adopted_lessons']=[]
        receipt['sha256']=__import__('hashlib').sha256(receipt['text'].encode()).hexdigest()
    return rows


class FrozenTools:
    def __init__(self, request, sink=None):
        self.request = request
        self.delivered = {}
        self.calls = []
        self.submissions = []
        self.sink = sink
        self.read_counts = {}
        self.requests = {}
        self.wire_bundles = {}

    def call(self, name, arguments=None):
        arguments = arguments or {}
        if name not in TOOLS or name=='record_ai_judgments' and self.request.get('submission_allowed') is False:
            result = {'error_code': 'FROZEN_TOOL_FORBIDDEN'}
        elif name in ('get_briefing', 'get_account'):
            kind = 'briefing' if name == 'get_briefing' else 'account'
            receipts = [r for r in self.request['receipts'] if r.get('kind') == kind]
            if not receipts:
                result = {'status': 'unavailable', 'reason': 'not_archived_at_original_time'}
            else:
                # Multiple receipts preserve their original order and IDs. A new
                # current value never stands in for a missing old tool result.
                index = self.read_counts.get(kind, 0)
                if index >= len(receipts):
                    result = {'status': 'unavailable', 'reason': 'no_additional_archived_response'}
                else:
                    receipt = receipts[index]
                    if receipt.get('wire_format') == W.FORMAT and not receipt.get('_wire_bundle'):
                        result = {'status': 'unavailable', 'reason': 'original_wire_bundle_not_archived'}
                    else:
                        self.read_counts[kind] = index + 1
                        self.delivered[receipt['delivery_id']] = receipt
                        if receipt.get('_wire_bundle'):
                            bundle = receipt['_wire_bundle']
                            self.wire_bundles[bundle['bundle_alias']] = bundle
                        result = {'frozen': True, 'delivery': receipt.get('text') if kind == 'briefing' else receipt.get('payload')}
        elif name == 'get_evidence_detail':
            details = [d for r in self.delivered.values() for d in r.get('_wire_details', [])]
            try:
                with W.frozen(self.wire_bundles.values(), datetime.fromisoformat(self.request['as_of']), details):
                    result = W.detail(arguments.get('ref'), arguments.get('fields'))
                if isinstance(result, dict) and result.get('bundle_alias'):
                    match = next(d for d in details if d['bundle_alias'] == result['bundle_alias'])
                    self.wire_bundles[match['bundle_alias']] = match
            except W.WireError as exc:
                result = {'status': 'rejected', 'errors': [exc.error]}
        else:
            import ai_judgment as J
            items = arguments.get('items', [])
            if not isinstance(items, list):
                result = [{'status': 'rejected', 'errors': [{'code': 'SCHEMA_ERROR'}]}]
            else:
                result=[]
                for item in items:
                    key=item.get('request_key') if isinstance(item,dict) else None
                    fingerprint=S.digest(item)
                    old=self.requests.get(key) if key else None
                    if old:
                        row=copy.deepcopy(old[1]) if old[0]==fingerprint else {'status':'rejected','errors':[{'code':'IDEMPOTENCY_CONFLICT'}]}
                    else:
                        with W.frozen(self.wire_bundles.values(), datetime.fromisoformat(self.request['as_of'])):
                            row=J.validate_frozen_submission(item,self.delivered,self.request['target'],self.request['as_of'])
                        if key:self.requests[key]=(fingerprint,copy.deepcopy(row))
                        self.submissions.append(row)
                    result.append(row)
        event = storage_objects.freeze({'sequence': len(self.calls), 'tool': name, 'arguments': arguments, 'result': result},kind='frozen_tool_call')
        self.calls.append(event)
        if self.sink: self.sink(event)
        return result


def _cli_stage(request, frozen):
    folder = S.root()/'ai_runtime'/request['trial_id']
    folder.mkdir(parents=True, exist_ok=False)
    manifest = folder/'input.json'
    record_store.publish(manifest,request,kind='frozen_trial_input')
    config = folder/'mcp.json'
    config.write_text(S.canonical({'mcpServers': {'frozen': {'command': sys.executable,
        'args': [str(Path(__file__).with_name('validation_mcp.py')), str(manifest)],
        'env': {'RUN_MODE': 'prompt_validation'}}}}), encoding='utf-8')
    # An empty working directory and explicit empty settings prevent loading the
    # live CLAUDE.md, hooks or broad Read permissions into an historical trial.
    settings = folder/'settings.json'
    settings.write_text(S.canonical({'permissions':{'allow':[],'deny':['Read','Bash','Write','Edit','WebSearch','WebFetch','Agent','Task']},
        'claudeMdExcludes':['**/CLAUDE.md','**/CLAUDE.local.md','**/AGENTS.md','**/.claude/rules/**'],
        'autoMemoryEnabled':False}),encoding='utf-8')
    cmd = [shutil.which('claude') or 'claude', '-p', request['prompt'], '--model', request['model'],
           '--output-format', 'json', '--tools', '', '--settings', str(settings), '--setting-sources', '',
           '--disable-slash-commands', '--no-session-persistence', '--no-chrome',
           '--mcp-config', str(config), '--strict-mcp-config', '--allowedTools',
           *['mcp__frozen__'+t for t in TOOLS], '--disallowedTools', 'Read', 'Bash', 'Write', 'Edit', 'WebSearch', 'WebFetch', 'Agent', 'Task',
           '--max-budget-usd', str(request['budget_usd'])]
    env = {**os.environ, 'RUN_MODE': 'prompt_validation', 'RUN_ATTEMPT_ID': '', 'RUN_ID': request['trial_id']}
    temporary=tempfile.TemporaryDirectory(prefix='sigma-frozen-ai-')
    spool=Spool(request['trial_id']);timed_out=False;parsed=None;returncode=None
    try:
        process = subprocess.Popen(cmd, cwd=temporary.name, env=env, stdin=subprocess.DEVNULL, stdout=spool.stdout,
                                   stderr=spool.stderr, start_new_session=os.name!='nt')
        try:
            out, err = process.communicate(timeout=request.get('timeout_seconds', 1500))
        except subprocess.TimeoutExpired:
            if os.name == 'nt': subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
            else: os.killpg(process.pid, signal.SIGKILL)
            out, err = process.communicate();timed_out=True
        spool.accept(out,err);returncode=process.returncode
        try:parsed=spool.result()
        except (ValueError,TypeError):parsed=None
    finally:
        raw=spool.capture();temporary.cleanup()
        path = folder/'calls.jsonl'
        if path.exists():
            frozen.calls=log_store.capture(log_store.view(path))
            rows=(row for c in frozen.calls if c['tool']=='record_ai_judgments' and isinstance(c['result'],list) for row in c['result'])
            frozen.submissions=list({S.digest(row):row for row in rows}.values())
    result={'returncode':returncode,'raw_output':raw,
            'stdout':raw_text(raw['stdout']) if raw['stdout']['bytes']<=256*1024 else {'detail_ref':'raw_output.stdout'},
            'stderr':raw_text(raw['stderr']) if raw['stderr']['bytes']<=256*1024 else {'detail_ref':'raw_output.stderr'},
            'provider_result':parsed,'provider_result_status':'parsed' if isinstance(parsed,dict) else 'invalid_output',
            'capture_scope':'stdout_and_frozen_tool_io; no hidden reasoning or unavailable subagent output inferred'}
    record_store.publish(folder/('partial-output.json' if timed_out else 'provider-output.json'),result,kind='frozen_trial_output')
    if timed_out:raise TimeoutError('AI trial timed out; retry is a separate experiment attempt')
    return storage_objects.freeze(result,kind='frozen_trial_output')


def _cli_provider(request, frozen):
    agents=request.get('agents') or {}
    if not all(agents.get(k) for k in ('bull','bear')):
        return _cli_stage(request,frozen)
    outputs=[]
    for name,fraction in [('bull',.25),('bear',.25),('coordinator',.5)]:
        prompt_outputs=[]
        for output in outputs:
            result=dict(output['result'])
            for field in ('stdout','stderr'):
                if isinstance(result.get(field),dict) and result.get('raw_output'):
                    result[field]=raw_text(result['raw_output'][field])
            result.pop('raw_output',None)
            prompt_outputs.append({**output,'result':result})
        prior='\nFROZEN SUBAGENT OUTPUTS\n'+S.canonical(prompt_outputs)
        prompt=(agents[name] if name!='coordinator' else request['prompt'])+prior
        prompt+='\nUse frozen get_briefing/get_account only for target '+S.canonical(request['target'])+'. Missing original web results stay unavailable.'
        if name!='coordinator':prompt+=' Return your argument report to the coordinator; do not submit a final judgment.'
        subrequest={**request,'trial_id':request['trial_id']+'.'+name,'prompt':prompt,
                    'budget_usd':request['budget_usd']*fraction,'submission_allowed':name=='coordinator','agent_role':name}
        stage=FrozenTools(subrequest)
        try:
            result=_cli_stage(subrequest,stage)
            outputs.append({'role':name,'result':result})
            if result.get('returncode') or result.get('provider_result_status')!='parsed':
                return {'returncode':result.get('returncode') or 1,'subagent_outputs':outputs,'status':'stage_failed'}
        finally:
            frozen.calls.extend([{**x,'agent_role':name} for x in stage.calls])
            if name=='coordinator':frozen.submissions.extend(stage.submissions)
    return {'returncode':0,'subagent_outputs':outputs,'status':'completed','model':request['model']}


def simulate_trials(inputs,spec,trials,repetitions):
    """Preserve market sample identity across model repetitions and both arms."""
    import simulation
    outputs=[]
    source=inputs.get('simulation_inputs') or {}
    call_targets={c['event_id']:(o['run_attempt_id'],o['code'],o['confidence_for'])
                  for o in inputs.get('opportunities',[]) for c in o.get('coverage_target',{}).get('calls',[])}
    call_targets.update({o['opportunity_id']:(o['run_attempt_id'],o['code'],o['confidence_for'])
                         for o in inputs.get('opportunities',[]) if o.get('opportunity_id')})
    expected={(o['run_attempt_id'],o['code'],o['confidence_for']) for o in inputs.get('opportunities',[])}
    for repetition in range(repetitions):
        paired=copy.deepcopy(source);streams={};errors=[]
        used_trials=[]
        for source_arm,destination in [('baseline','base'),('candidate','changed')]:
            selected=[t for t in trials if t['repetition']==repetition and t['arm']==source_arm]
            bytarget={};used=set()
            for trial in selected:
                used_trials.append(trial['trial_id'])
                target=trial['target'];key=(target['run_attempt_id'],target['code'],target['confidence_for'])
                if trial['status']!='accepted' or len(trial.get('judgments',[]))!=1:
                    errors.append('unresolved_paired_judgment:'+str(key));continue
                bytarget[key]=trial['judgments'][0]
            stream=[]
            for original in source.get('decisions',[]):
                key=call_targets.get(original['decision_id']);judgment=bytarget.get(key)
                if not judgment:
                    errors.append('missing_paired_decision:'+original['decision_id']);continue
                used.add(key)
                stream.append({**copy.deepcopy(original),'raw_confidence':judgment['confidence_raw'],
                               'recommended_action':judgment['recommended_action'],
                               'policy_context':{} if destination=='changed' else original.get('policy_context')})
            if used!=expected: errors.append('whole_opportunity_simulation_inputs_incomplete')
            streams[destination]=stream
        paired['arm_decisions']=streams
        if errors: paired['complete']=False
        result=simulation.run_pair(paired,spec,None)
        outputs.append({'repetition':repetition,'status':result['status'],'result':result,'gaps':errors,'trial_ids':used_trials})
    return outputs


def run_prompt_validation(validation_id, *, with_ai=False, model=None, repetitions=None, budget_usd=None, provider=None):
    import validation_spec as V
    import validation_dataset as D
    import condition_contract as CC
    manifest = V.load_spec(validation_id)
    spec, proposal = manifest['spec'], manifest['proposal_snapshot']
    if spec.get('comparison_kind') != 'prompt_pair': raise ValueError('PROMPT_PAIR_SPEC_REQUIRED')
    if not model or type(repetitions) is not int or repetitions < 1: raise ValueError('EXPLICIT_MODEL_AND_REPETITIONS_REQUIRED')
    if type(budget_usd) not in (int,float) or not math.isfinite(budget_usd) or budget_usd <= 0: raise ValueError('EXPLICIT_BUDGET_REQUIRED')
    declared = spec.get('prompt', {})
    if declared.get('model') != model or spec.get('repetitions') != repetitions or declared.get('budget_usd') != budget_usd:
        raise ValueError('EXPERIMENT_SETTINGS_DIFFER_FROM_REGISTRATION')
    if provider is None and not with_ai: raise ValueError('REAL_AI_REQUIRES_EXPLICIT_WITH_AI')
    if (proposal.get('effect') or {}).get('operation') != 'prompt_guidance': raise ValueError('PROMPT_GUIDANCE_PROPOSAL_REQUIRED')
    condition = proposal.get('scope_condition') or proposal.get('condition')
    if condition: CC.validate(condition, stage='before_judgment')
    inputs = D.ensure_inputs(validation_id)
    opportunities = inputs.get('opportunities', [])
    experiment_id = 'prompt-'+uuid.uuid4().hex
    receipts = inputs.get('deliveries', [])
    if isinstance(receipts, dict): receipts=list(receipts.values())
    denom = max(1, len(opportunities)*repetitions*2)
    start = {'experiment_id':experiment_id,'validation_id':validation_id,'model':model,'repetitions':repetitions,
             'budget_usd':budget_usd,'input_hash':S.digest(inputs),'started_at':C.now().isoformat(),
             'mode':'local_fake' if provider else 'explicit_real_ai','comparison_label':'new_paired_model_comparison',
             'subagent_policy':'frozen bull then bear then coordinator; explicit same model, per-arm budget split 25/25/50; live Agent tool disabled'}
    start.update(input_id=inputs.get('input_id'),spec_hash=S.digest(manifest),proposal_hash=S.digest(proposal))
    with S.locked('prompt-budget-'+validation_id):
        previous=S.records('prompt_experiments')
        if previous['errors']: raise ValueError('PRIOR_EXPERIMENT_BUDGET_UNCONFIRMED')
        if any(x.get('validation_id')==validation_id for x in previous['records']):
            raise ValueError('EXPERIMENT_ALREADY_STARTED_REGISTER_NEW_VARIANT_FOR_RETRY')
        S.archive('prompt_experiments',experiment_id,start)
    trials, pairs, gaps = [], [], []
    provider = provider or _cli_provider
    for opportunity in opportunities:
        aid = opportunity['run_attempt_id']
        ri = inputs.get('runtime_inputs',{}).get(aid)
        if isinstance(ri,dict) and 'snapshot' in ri: ri=ri['snapshot']
        subset = [r for r in receipts if r.get('run_attempt_id')==aid]
        if not ri or not ri.get('cmd_prompt') or not subset:
            gaps.append({'target':opportunity,'reason':'original_prompt_or_receipts_unavailable'});continue
        as_of = opportunity.get('at') or opportunity.get('decision_at')
        if not as_of:
            gaps.append({'target':opportunity,'reason':'historical_decision_cutoff_unavailable'});continue
        if ri.get('created_at') and V.instant(ri['created_at'])>V.instant(as_of):
            gaps.append({'target':opportunity,'reason':'runtime_input_after_original_decision'});continue
        subset = [r for r in subset if V.instant(r['created_at'])<=V.instant(as_of)]
        if not subset:
            gaps.append({'target':opportunity,'reason':'no_receipt_before_original_decision'});continue
        original = ri['cmd_prompt']
        sources = ri.get('source_files') or ri.get('sources') or {}
        agents={name:(sources.get('.claude/agents/'+name+'.md') or '').split('---',2)[-1] for name in ('bull','bear')}
        if provider is _cli_provider and not all(agents.values()):
            gaps.append({'target':opportunity,'reason':'original_bull_bear_prompts_unavailable'});continue
        original += '\nFROZEN ORIGINAL INSTRUCTIONS\n'+S.canonical({k:v for k,v in sources.items() if k=='CLAUDE.md' or k.startswith('.claude/agents/')})
        context = opportunity.get('condition_context') or {}
        matches = CC.evaluate(condition,context,stage='before_judgment') if condition else True
        try:
            by_arm={arm:experimental_receipts(subset,arm) for arm in ('baseline','candidate')}
        except ValueError as exc:
            gaps.append({'target':opportunity,'reason':str(exc)});continue
        for repetition in range(repetitions):
            pair = []
            for arm in ('baseline','candidate'):
                trial_id='trial-'+uuid.uuid4().hex
                target={k:opportunity[k] for k in ('run_attempt_id','code','confidence_for')}
                target['run_id']=(subset[0].get('ai_run_id') or subset[0].get('run_id')) if subset else None
                prompt=original+'\nThis is an isolated historical experiment. Use only frozen tools. Submit one structured judgment for '+S.canonical(target)+'. Missing old information remains unavailable; no order is possible.'
                if arm=='candidate' and matches is True: prompt+='\nEXPERIMENTAL GUIDANCE (not a verified market fact):\n'+proposal['effect']['text']
                request={'trial_id':trial_id,'experiment_id':experiment_id,'validation_id':validation_id,'arm':arm,
                         'repetition':repetition,'target':target,'as_of':as_of,'receipts':by_arm[arm],'prompt':prompt,
                         'model':model,'budget_usd':budget_usd/denom,'condition_result':matches,
                         'agents':agents,
                         'new_guidance_applied':arm=='candidate' and matches is True}
                S.archive('prompt_requests',trial_id,request)
                frozen=FrozenTools(request)
                row={'trial_id':trial_id,'arm':arm,'target':target,'repetition':repetition}
                try:
                    result=provider(copy.deepcopy(request),frozen)
                    accepted=[x for x in frozen.submissions if x.get('status')=='accepted']
                    row.update(status='error' if result.get('returncode',0) else 'accepted' if accepted else 'rejected' if frozen.submissions else 'not_submitted',
                               output=result, judgments=accepted)
                except Exception as exc:
                    row.update(status='timeout' if isinstance(exc,TimeoutError) else 'error',error=str(exc))
                row.update(calls=frozen.calls,submissions=frozen.submissions,condition_result=matches)
                S.archive('prompt_trials',trial_id,row)
                trials.append(row);pair.append(row)
            pairs.append({'trial_ids':[x['trial_id'] for x in pair], 'status':'paired' if all(x['status']=='accepted' for x in pair) else 'incomplete',
                          'target':opportunity,'repetition':repetition})
    output={**start,'status':'complete' if not gaps and all(x['status']=='paired' for x in pairs) and pairs else 'incomplete',
            'pairs':pairs,'trial_ids':[x['trial_id'] for x in trials],'gaps':gaps,
            'market_sample_count':len(opportunities),'repetitions_are_independent_market_samples':False,
            'adoption_eligibility':False,'performance_status':'requires_sequential_simulation_of_paired_outputs'}
    output['simulations']=simulate_trials(inputs,spec,trials,repetitions)
    output['performance_status']='complete' if output['simulations'] and all(x['status']=='complete' for x in output['simulations']) else 'incomplete'
    S.archive('prompt_results',experiment_id,output)
    S.archive('prompt_result_commits',experiment_id,{'experiment_id':experiment_id,'validation_id':validation_id,'result_hash':S.digest(output)})
    return output
