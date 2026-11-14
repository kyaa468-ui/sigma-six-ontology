"""Versioned qualitative judgment stages; no new trading score or risk formula.

The runner assigns a role to a separate invocation. The server constructs and
persists each next input from accepted predecessors. This proves explicit data
flow, not the model's private reasoning or the truth of a causal explanation.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import uuid
from pathlib import Path

import common as C
import record_store

FORMAT = 'SIGMA_SEMANTIC@v1'
ROLES = ('interpretation', 'bull', 'bear', 'lead')
STATES = ('adopted', 'rejected', 'deferred', 'unreviewed')


class SemanticError(ValueError):
    def __init__(self, code, detail=''):
        self.code = code
        self.detail = detail
        super().__init__(code + (': ' + detail if detail else ''))


def digest(value):
    import storage_objects
    return storage_objects.fingerprint(value)[0]


def required():
    return os.environ.get('RUN_MODE') in ('decide', 'view') and os.environ.get('SION_SEMANTIC_MODE') != 'legacy'


def _identity():
    role = os.environ.get('SION_SEMANTIC_ROLE')
    attempt = os.environ.get('RUN_ATTEMPT_ID')
    if not required() or role not in ROLES or not attempt:
        raise SemanticError('SEMANTIC_ROLE_REQUIRED')
    return attempt, role


def _id(value):
    import re
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise SemanticError('SEMANTIC_INVALID_ID')
    return value


def _path(kind, identifier, attempt=None):
    attempt = _id(attempt or os.environ.get('RUN_ATTEMPT_ID'))
    return C.ontology_root() / 'semantic' / attempt / kind / (_id(identifier) + '.json')


def _read(kind, identifier, attempt=None):
    path = _path(kind, identifier, attempt)
    return record_store.load(path) if path.exists() else None


def _write(kind, identifier, value):
    record_store.publish(_path(kind, identifier), value, kind='semantic_' + kind)


def definitions():
    return C.read_json(Path(C.CONFIG) / 'ontology' / 'semantic_definitions.json') or _missing_definitions()


def _missing_definitions():
    raise SemanticError('SEMANTIC_DEFINITIONS_UNAVAILABLE')


def _current():
    import policy_registry as PR
    params = C.params()
    return {'definitions_hash': digest(definitions()), 'params': params,
            'policy': PR.snapshot(params), 'buy_threshold': C.buy_threshold(params),
            'probe_floor': C.probe_floor(params), 'risk_limits': C.risk_limits(),
            'regime': C.latest_regime(),
            'source_hashes': {str(p.relative_to(C.AGENT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                             [Path(C.AGENT)/'CLAUDE.md', Path(C.AGENT)/'src/semantic_layer.py', Path(C.AGENT)/'src/semantic_runner.py',
                              *[Path(C.AGENT)/'src'/name for name in ('common.py','sizing.py','risk.py','mcp_server.py','ai_judgment.py','judgment_coverage.py')],
                              *sorted((Path(C.AGENT)/'.claude/agents').glob('*.md'))] if p.is_file()}}


def _fresh(chain):
    targets()  # A newly discovered subject must also stop stale downstream use.
    if digest(chain['effective']) != digest(_current()):
        raise SemanticError('SEMANTIC_CONTEXT_CHANGED', 'Start a new judgment attempt; historical results are retained')
    if _path('invalidations', chain['chain_id']).exists():
        raise SemanticError('SEMANTIC_REASSESSMENT_REQUIRED')


def targets():
    """Use coverage's append-only target population; freeze before bull starts."""
    import judgment_coverage as JC
    attempt, role = _identity()
    coverage = JC.get_coverage(attempt, summary=True)
    if (coverage.get('recording_status') != 'recorded' or coverage.get('target_set_incomplete')
            or coverage.get('global_storage_errors')):
        raise SemanticError('SEMANTIC_TARGETS_UNAVAILABLE')
    rows = [{k: copy.deepcopy(t[k]) for k in ('code', 'confidence_for', 'reasons')}
            for t in coverage['targets']]
    keys = {(t['code'], t['confidence_for']) for t in rows}
    # Existing guidance requires a separate buy review if a holding is kept.
    # Register this potential direction before the four roles; sizing still
    # enforces add_on.enabled, and review never implies permission to buy.
    extra = [{'code': t['code'], 'confidence_for': '매수'} for t in rows
             if t['confidence_for'] == '매도' and (t['code'], '매수') not in keys]
    frozen = _read('scope', 'targets')
    if frozen:
        current_keys = keys | {(t['code'], t['confidence_for']) for t in extra}
        frozen_keys = {(t['code'], t['confidence_for']) for t in frozen['targets']}
        if current_keys != frozen_keys:
            raise SemanticError('SEMANTIC_TARGET_SET_CHANGED_RESTART_REQUIRED',
                                json.dumps({'new_targets': sorted(current_keys - frozen_keys),
                                            'instruction': 'Start a new attempt; preserve prior stages and executions.'}, ensure_ascii=False))
        return rows
    if extra:
        result = JC.register_review_targets(attempt, extra)
        if result.get('recording_status') != 'recorded':
            raise SemanticError('SEMANTIC_TARGET_REGISTRATION_FAILED')
        return targets()
    if role != 'interpretation':
        freeze_targets(rows)
    return rows


def freeze_targets(rows=None):
    rows = targets() if rows is None else rows
    value = {'run_attempt_id': _identity()[0], 'targets': rows,
             'created_at': C.now().isoformat(), 'reason': 'interpretation_complete_before_bull'}
    try:
        _write('scope', 'targets', value)
    except FileExistsError:
        if {(t['code'], t['confidence_for']) for t in _read('scope', 'targets')['targets']} != {
                (t['code'], t['confidence_for']) for t in rows}:
            raise SemanticError('SEMANTIC_TARGET_SET_CHANGED_RESTART_REQUIRED')
    return rows


def _chain_id(code, side):
    return 'chain-' + digest([os.environ.get('RUN_ATTEMPT_ID'), code, side])[:32]


def _stage(chain_id, role):
    return _read('stages', chain_id + '-' + role)


def context(code=None, confidence_for=None):
    """Bounded per-target input, persisted BEFORE being returned to a role."""
    attempt, role = _identity()
    available = targets()
    if code is None:
        return {'format': FORMAT, 'role': role, 'targets': available,
                'instruction': 'Call get_semantic_context for each target, then submit_semantic_stage with its input_id.'}
    code = str(code)
    if not any(r['code'] == code and r['confidence_for'] == confidence_for for r in available):
        raise SemanticError('SEMANTIC_TARGET_MISMATCH')
    cid = _chain_id(code, confidence_for)
    chain = _read('chains', cid)
    if chain is None:
        if role != 'interpretation':
            raise SemanticError('SEMANTIC_PREDECESSOR_MISSING')
        import execution_trace as E
        chain = {'format': FORMAT, 'chain_id': cid, 'run_attempt_id': attempt,
                 'run_id': os.environ.get('RUN_ID'), 'code': code, 'confidence_for': confidence_for,
                 'created_at': C.now().isoformat(), 'effective': _current(), 'definitions': definitions(),
                 'engine_definition': E.capture_definition(), 'expected_roles': list(ROLES)}
        if chain['engine_definition'].get('recording_status') != 'recorded':
            raise SemanticError('SEMANTIC_DEFINITION_RECORDING_FAILED')
        try:
            _write('chains', cid, chain)
        except FileExistsError:
            chain = _read('chains', cid)
    _fresh(chain)
    predecessors = []
    for predecessor in ROLES[:ROLES.index(role)]:
        row = _stage(cid, predecessor)
        if not row or row['status'] != 'accepted':
            raise SemanticError('SEMANTIC_PREDECESSOR_MISSING', predecessor)
        predecessors.append(row)
    payload = {'format': FORMAT, 'chain': chain, 'role': role, 'predecessors': predecessors,
               'contract': contract(role), 'inference_status': 'qualitative_unverified'}
    iid = 'input-' + digest(payload)[:40]
    envelope = {'input_id': iid, 'role': role, 'chain_id': cid, 'run_attempt_id': attempt,
                'created_at': C.now().isoformat(), 'payload_hash': digest(payload), 'payload': payload}
    try:
        _write('inputs', iid, envelope)
    except FileExistsError:
        old = _read('inputs', iid)
        if old['payload_hash'] != digest(payload):
            raise SemanticError('SEMANTIC_INPUT_CONFLICT')
    return {'input_id': iid, **payload}


def contract(role):
    common = {'no_numeric_risk_model': True, 'unknown_is_not_absent': True,
              'references': 'IDs of claims/paths/assessments actually provided in this input; preserve original IDs',
              'evidence': 'arguments use existing fact_claim/source_quote/hypothesis and compact wire declarations; adoption is assigned by server until lead'}
    if role == 'interpretation':
        return {**common, 'required': ['arguments', 'paths'],
                'path_shape': {'id': 'unique ID', 'claim_ids': ['ordered claim IDs'], 'assumptions': ['text'],
                               'grouping_reason': 'why one causal path', 'grouping_status': 'resolved|uncertain',
                               'direction': 'support|oppose|neutral', 'horizon': 'existing applicable period or explicit unknown'},
                'measurement_context': 'Optional fact_claim context assertions must exactly match visible source metadata (period/subject/field/unit/kind/observed_at/available_at).'}
    if role in ('bull', 'bear'):
        return {**common, 'required': ['evaluations', 'objections'], 'optional': ['arguments', 'paths'],
                'evaluation_shape': {'id': 'unique ID', 'path_id': 'path ID', 'stance': 'support|oppose|uncertain',
                                     'buy_interpretation': 'agree|disagree|unknown',
                                     'evidence_clarity': 'text', 'causal_clarity': 'text', 'likelihood': 'qualitative text',
                                     'loss_magnitude': 'qualitative text', 'reason': 'text', 'uncertainties': ['text'], 'refs': ['claim/path IDs']},
                'objection_shape': {'id': 'unique ID', 'text': 'counterargument', 'refs': ['claim/path/evaluation IDs']},
                'coverage': 'Evaluate every path in the provided input and every new path you introduce. Empty objections means none generated, not none exists.'}
    return {**common, 'required': ['confidence_band_id', 'confidence_raw', 'recommended_action', 'decision', 'dispositions', 'adopted_claim_ids', 'salient_risk_path_ids'],
            'bands': 'buy: strong|clear_risk|probe|balanced|opposed; sell: existing_sell_discretion (no invented sell interval)',
            'disposition_shape': {'evaluation_id': 'each bull/bear evaluation AND objection ID',
                                  'state': 'adopted|rejected|deferred|unreviewed', 'reason': 'text'},
            'salient_risk_path_ids': 'IDs of distinct oppose paths the lead judges to be clear risks; explain in decision. Uncertain grouping cannot supply an exact count.',
            'adoption_consistency': 'strong requires a jointly agreed support path whose bull/bear evaluations are both adopted and whose claims are covered by adopted claims or their explicit dependencies. Resolved salient risks require an adopted oppose evaluation and the same claim coverage. Uncertain risk paths may remain deferred references and never qualify an exact-count band.',
            'numeric_choice': 'Use existing confidence guidance; exact number within band remains model discretion.'}


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SemanticError('SEMANTIC_TEXT_REQUIRED', field)
    return value


def _refs(refs, known, field, allow_empty=False):
    if not isinstance(refs, list) or (not refs and not allow_empty) or any(not isinstance(r, str) or r not in known for r in refs):
        raise SemanticError('SEMANTIC_REFERENCE_MISMATCH', field)


def _arguments(data, chain, prior):
    import ai_judgment as J
    import evidence_wire as W
    raw = copy.deepcopy(data.get('arguments', []))
    if not isinstance(raw, list):
        raise SemanticError('SEMANTIC_ARGUMENTS_REQUIRED')
    if not raw:
        return [], [], []
    for a in raw:
        a['proposer'] = os.environ['SION_SEMANTIC_ROLE']
        a['adoption'] = 'reference'
    wire = {k: copy.deepcopy(data[k]) for k in ('wire_format', 'bundle_refs', 'delivery_ids') if k in data}
    normalized = W.normalize_judgment({**wire, 'arguments': raw})
    ctx = {**J._context(), 'code': chain['code'], 'confidence_for': chain['confidence_for'],
           'delivery_ids': normalized.get('delivery_ids', [])}
    if normalized.get('wire_submission'):
        ctx['wire_bundles'] = normalized['wire_submission']['original']['bundle_refs']
    known = {a['id'] for a in prior}
    ids = [a.get('id') for a in raw]
    if len(set(ids)) != len(ids) or known.intersection(ids):
        raise SemanticError('SEMANTIC_DUPLICATE_ID')
    known.update(ids)
    graph = {a['id']:a.get('claim_ids', []) for a in [*prior,*normalized['arguments']] if a.get('kind')=='hypothesis'}
    active, done = set(), set()
    def visit(identifier):
        if identifier in active: raise SemanticError('SEMANTIC_CYCLIC_CLAIM_DEPENDENCY')
        if identifier in done: return
        active.add(identifier)
        for child in graph.get(identifier, []):
            if child in graph: visit(child)
        active.remove(identifier); done.add(identifier)
    for identifier in graph: visit(identifier)
    result = []
    for a in normalized['arguments']:
        if a.get('kind') == 'hypothesis':
            _refs(a.get('claim_ids'), known - {a['id']}, 'hypothesis.claim_ids')
        checked = J._argument({k:v for k,v in a.items() if k != 'measurement_context'}, 'arguments', ctx)
        checked['semantic_definition_ids'] = [d['id'] for d in chain['definitions']['concepts']
                                              if any(e.get('field') in d['fields'] for e in checked.get('evidence_snapshots', []))]
        if a.get('measurement_context'):
            snapshot = checked.get('evidence_snapshots', [{}])[0]
            receipt = J._receipt(a['evidence']['delivery_id'], 'evidence', ctx)
            visible = W.check_visible(receipt, snapshot, ctx.get('wire_bundles'))
            for k, v in a['measurement_context'].items():
                # Compact visibility records leaf paths (period.0, period.1),
                # not the container key. Every asserted leaf must be delivered.
                delivered = visible is None or set(W._leaves({k: v})).issubset(visible)
                if k not in {'period','subject','field','unit','kind','observed_at','available_at'} or not delivered or snapshot.get(k) != v:
                    raise SemanticError('SEMANTIC_MEASUREMENT_MISMATCH', k)
        result.append(checked)
    return result, raw, [wire]


def _paths(items, arguments, prior_paths):
    if not isinstance(items, list):
        raise SemanticError('SEMANTIC_PATHS_REQUIRED')
    known = {a['id'] for a in arguments}
    sequences = {tuple(p['claim_ids']) for p in prior_paths}
    ids = set(known) | {p['id'] for p in prior_paths}
    for p in items:
        _id(p.get('id'))
        if p['id'] in ids:
            raise SemanticError('SEMANTIC_DUPLICATE_ID', p['id'])
        ids.add(p['id'])
        _refs(p.get('claim_ids'), known, 'path.claim_ids')
        if tuple(p['claim_ids']) in sequences:
            raise SemanticError('SEMANTIC_EXPLICIT_DUPLICATE_PATH')
        sequences.add(tuple(p['claim_ids']))
        if len(set(p['claim_ids'])) != len(p['claim_ids']):
            raise SemanticError('SEMANTIC_REPEATED_PATH_NODE')
        if p.get('direction') not in ('support','oppose','neutral') or p.get('grouping_status') not in ('resolved','uncertain'):
            raise SemanticError('SEMANTIC_PATH_STATUS')
        for k in ('grouping_reason', 'horizon'):
            _text(p.get(k), k)
        if not isinstance(p.get('assumptions'), list) or any(not isinstance(x,str) for x in p['assumptions']):
            raise SemanticError('SEMANTIC_ASSUMPTIONS_REQUIRED')
        p['causal_validity'] = 'unverified'
    return items


def _band(chain, band, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise SemanticError('SEMANTIC_CONFIDENCE_RANGE')
    if chain['confidence_for'] == '매도':
        if band != 'existing_sell_discretion':
            raise SemanticError('SEMANTIC_BAND_MISMATCH')
        return
    t, floor = chain['effective']['buy_threshold'], chain['effective']['probe_floor']
    # Preserve the literal .79 upper boundary in the existing prompt. Values in
    # an unspecified gap are rejected, not silently assigned a new interval.
    ranges = {'strong': value >= .80, 'clear_risk': t <= value <= .79,
              'probe': floor <= value < t, 'balanced': .45 <= value < floor, 'opposed': value < .45}
    if not ranges.get(band, False):
        raise SemanticError('SEMANTIC_BAND_MISMATCH')


def submit(input_id, data):
    """Atomic accepted stage, idempotent retries; failures remain distinct."""
    try:
        return _submit(input_id, data)
    except Exception as exc:
        event = {'input_id': input_id, 'role': os.environ.get('SION_SEMANTIC_ROLE'),
                 'status': 'rejected', 'error_code': getattr(exc, 'code', getattr(exc, 'error', {}).get('code', 'SEMANTIC_RECORDING_OR_VALIDATION_ERROR')),
                 'detail': str(exc), 'created_at': C.now().isoformat()}
        try:
            _write('failures', 'failure-' + uuid.uuid4().hex, event)
        except Exception:
            event['recording_status'] = 'recording_error'
        return event


def _submit(input_id, data):
    attempt, role = _identity()
    incoming = _read('inputs', input_id)
    if not incoming or incoming['role'] != role or incoming['run_attempt_id'] != attempt:
        raise SemanticError('SEMANTIC_INPUT_SCOPE_MISMATCH')
    payload = incoming['payload']
    if digest(payload) != incoming['payload_hash']:
        raise SemanticError('SEMANTIC_INPUT_INTEGRITY')
    chain = payload['chain']; cid = chain['chain_id']
    _fresh(chain)
    key = cid + '-' + role
    old = _read('stages', key)
    request_hash = digest([input_id, data])
    if old:
        if old['request_hash'] != request_hash:
            raise SemanticError('SEMANTIC_ACCEPTED_STAGE_IMMUTABLE')
        return {'status': 'accepted', 'stage_id': key, 'judgment_id': old.get('judgment_id'), 'idempotent': True}
    for p in payload['predecessors']:
        actual = _read('stages', p['stage_id'])
        if actual is None or digest(actual) != digest(p):
            raise SemanticError('SEMANTIC_PREDECESSOR_MISMATCH')
    all_arguments = [a for p in payload['predecessors'] for a in p.get('arguments', [])]
    all_paths = [a for p in payload['predecessors'] for a in p.get('paths', [])]
    row = {'format': FORMAT, 'stage_id': key, 'chain_id': cid, 'run_attempt_id': attempt,
           'role': role, 'runtime_call_id': attempt + '-' + role, 'input_id': input_id, 'input_hash': incoming['payload_hash'],
           'predecessor_ids': [p['stage_id'] for p in payload['predecessors']],
           'created_at': C.now().isoformat(), 'request_hash': request_hash, 'status': 'accepted',
           'qualitative_validity': 'unverified'}
    if role != 'lead':
        arguments, raw, wire = _arguments(data, chain, all_arguments)
        prior_nonclaims = {p['id'] for p in all_paths} | {e['id'] for s in payload['predecessors']
                            for e in s.get('evaluations', []) + s.get('objections', [])}
        if any(a['id'] in prior_nonclaims for a in arguments):
            raise SemanticError('SEMANTIC_DUPLICATE_ID')
        all_arguments += arguments
        paths = _paths(copy.deepcopy(data.get('paths', [])), all_arguments, all_paths)
        if any(p['id'] in prior_nonclaims for p in paths):
            raise SemanticError('SEMANTIC_DUPLICATE_ID')
        all_paths += paths
        if role == 'interpretation' and (not arguments or not all_paths):
            raise SemanticError('SEMANTIC_INTERPRETATION_REQUIRED')
        row.update(arguments=arguments, raw_arguments=raw, wire_declarations=wire, paths=paths)
        if role in ('bull', 'bear'):
            known = {a['id'] for a in all_arguments + all_paths}
            evaluations = copy.deepcopy(data.get('evaluations'))
            objections = copy.deepcopy(data.get('objections'))
            if not isinstance(evaluations, list) or not isinstance(objections, list):
                raise SemanticError('SEMANTIC_EVALUATIONS_REQUIRED')
            if sorted(e.get('path_id', '') for e in evaluations) != sorted(p['id'] for p in all_paths):
                raise SemanticError('SEMANTIC_PATH_EVALUATION_MISSING')
            previous_ids = {e['id'] for p in payload['predecessors'] for e in p.get('evaluations', []) + p.get('objections', [])}
            for e in evaluations + objections:
                _id(e.get('id'))
                if e['id'] in known | previous_ids:
                    raise SemanticError('SEMANTIC_DUPLICATE_ID', e['id'])
                _refs(e.get('refs'), known | previous_ids, 'evaluation.refs')
                previous_ids.add(e['id'])
                if 'path_id' in e:
                    if e.get('stance') not in ('support','oppose','uncertain') or e.get('buy_interpretation') not in ('agree','disagree','unknown'):
                        raise SemanticError('SEMANTIC_EVALUATION_STATUS')
                    for field in ('evidence_clarity','causal_clarity','likelihood','loss_magnitude','reason'):
                        _text(e.get(field), field)
                    if not isinstance(e.get('uncertainties'), list) or any(not isinstance(x,str) for x in e['uncertainties']):
                        raise SemanticError('SEMANTIC_UNCERTAINTY_REQUIRED')
                else:
                    _text(e.get('text'), 'objection.text')
            row.update(evaluations=evaluations, objections=objections, objection_scope='generated_only_not_exhaustive')
    else:
        import ai_judgment as J
        _band(chain, data.get('confidence_band_id'), data.get('confidence_raw'))
        evaluations = [e for p in payload['predecessors'] for e in p.get('evaluations', []) + p.get('objections', [])]
        dispositions = data.get('dispositions')
        if not isinstance(dispositions, list) or sorted(d.get('evaluation_id','') for d in dispositions) != sorted(e['id'] for e in evaluations):
            raise SemanticError('SEMANTIC_COUNTERARGUMENT_DISPOSITION_MISSING')
        for d in dispositions:
            if d.get('state') not in STATES:
                raise SemanticError('SEMANTIC_DISPOSITION_STATUS')
            _text(d.get('reason'), 'disposition.reason')
        _refs(data.get('adopted_claim_ids'), {a['id'] for a in all_arguments}, 'adopted_claim_ids', allow_empty=True)
        raw = [a for p in payload['predecessors'] for a in p.get('raw_arguments', [])]
        raw = copy.deepcopy(raw)
        for a in raw:
            a.pop('measurement_context', None)
            a['adoption'] = 'adopted' if a['id'] in data['adopted_claim_ids'] else 'reference'
        declarations = [w for p in payload['predecessors'] for w in p.get('wire_declarations', [])]
        wire = {}
        if any('wire_format' in w for w in declarations):
            import evidence_wire as W
            wire = {'wire_format': W.FORMAT, 'bundle_refs': list(dict.fromkeys(b for w in declarations for b in w.get('bundle_refs', [])))}
        else:
            wire = {'delivery_ids': list(dict.fromkeys(d for w in declarations for d in w.get('delivery_ids', [])))}
        disposition_by_id = {d['evaluation_id']: d['state'] for d in dispositions}
        claims_by_id = {a['id']: a for a in all_arguments}
        adopted_closure = set()
        def adopt_dependencies(identifier):
            if identifier in adopted_closure:
                return
            adopted_closure.add(identifier)
            for dependency in claims_by_id[identifier].get('claim_ids', []):
                adopt_dependencies(dependency)
        for identifier in data['adopted_claim_ids']:
            adopt_dependencies(identifier)
        joint, adopted_joint, path_adoptions = [], [], {}
        for p in all_paths:
            assessments = [(s['role'], e) for s in payload['predecessors'] for e in s.get('evaluations', [])
                           if e['path_id'] == p['id']]
            agreed = {role for role, e in assessments if e['buy_interpretation'] == 'agree'}
            adopted_agreed = {role for role, e in assessments if e['buy_interpretation'] == 'agree'
                              and e['stance'] == 'support' and disposition_by_id[e['id']] == 'adopted'}
            covered = set(p['claim_ids']) <= adopted_closure
            path_adoptions[p['id']] = {'claims_covered': covered,
                'adopted_evaluation_ids': [e['id'] for _, e in assessments if disposition_by_id[e['id']] == 'adopted']}
            if p['direction'] == 'support' and agreed == {'bull','bear'}:
                joint.append(p['id'])
                if adopted_agreed == {'bull', 'bear'} and covered:
                    adopted_joint.append(p['id'])
        salient = data.get('salient_risk_path_ids')
        _refs(salient, {p['id'] for p in all_paths if p['direction']=='oppose'}, 'salient_risk_path_ids', allow_empty=True)
        if len(set(salient)) != len(salient):
            raise SemanticError('SEMANTIC_DUPLICATE_RISK_COUNT')
        for p in all_paths:
            if p['id'] not in salient:
                continue
            assessments = [e for s in payload['predecessors'] for e in s.get('evaluations', []) if e['path_id'] == p['id']]
            if p['grouping_status'] == 'resolved':
                consistent = path_adoptions[p['id']]['claims_covered'] and any(
                    e['stance'] == 'oppose' and disposition_by_id[e['id']] == 'adopted' for e in assessments)
            else:
                consistent = any(disposition_by_id[e['id']] in ('adopted', 'deferred') for e in assessments)
            if not consistent:
                raise SemanticError('SEMANTIC_RISK_ADOPTION_MISMATCH', p['id'])
        risk_count = len(salient) if all(p['grouping_status']=='resolved' for p in all_paths if p['id'] in salient) else None
        if chain['confidence_for']=='매수' and data['confidence_band_id']=='clear_risk' and risk_count != 1:
            raise SemanticError('SEMANTIC_CLEAR_RISK_COUNT_REQUIRED')
        if chain['confidence_for']=='매수' and data['confidence_band_id']=='strong':
            if not joint:
                raise SemanticError('SEMANTIC_JOINT_INTERPRETATION_REQUIRED')
            if not adopted_joint:
                raise SemanticError('SEMANTIC_ADOPTED_JOINT_INTERPRETATION_REQUIRED')
        item = {**wire, 'request_key': key + '-' + request_hash[:16], 'code': chain['code'], 'confidence_for': chain['confidence_for'],
                'confidence_raw': data['confidence_raw'], 'recommended_action': data.get('recommended_action'),
                'decision': _text(data.get('decision'), 'decision'), 'arguments': raw, 'premises': data.get('premises', []),
                'semantic': {'format': FORMAT, 'chain_id': cid, 'stage_id': key, 'input_id': input_id,
                             'confidence_band_id': data['confidence_band_id'], 'dispositions': dispositions,
                             'joint_buy_interpretation_paths': joint, 'salient_risk_path_ids': salient, 'salient_risk_count': risk_count,
                             'adopted_joint_buy_interpretation_paths': adopted_joint, 'path_adoptions': path_adoptions,
                             'unreviewed_ids': [d['evaluation_id'] for d in dispositions if d['state'] == 'unreviewed']}}
        result = J.record_ai_judgments([item])[0]
        if result.get('status') != 'accepted':
            raise SemanticError('SEMANTIC_FINAL_REJECTED', json.dumps(result, ensure_ascii=False))
        row.update(judgment_id=result['judgment_id'], decision=item['semantic'],
                   confidence_raw=data['confidence_raw'], confidence_band_id=data['confidence_band_id'])
    _fresh(chain)
    try:
        _write('stages', key, row)
    except FileExistsError:
        if _read('stages', key)['request_hash'] != request_hash:
            raise SemanticError('SEMANTIC_ACCEPTED_STAGE_IMMUTABLE')
    return {'status': 'accepted', 'stage_id': key, 'judgment_id': row.get('judgment_id')}


def consume(code, side, confidence, judgment_id, *, rule_exit=False, stage='plan'):
    """Return the accepted number, never trust a separate caller-supplied score."""
    if not required():
        return confidence
    if side == '매도' and rule_exit:
        import monitor
        holdings = (C.read_json(C.ACCOUNT) or {}).get('holdings', [])
        checked = monitor.evaluate_triggers(holdings)
        if code in checked['hits']:
            return confidence
        raise SemanticError('SEMANTIC_PROTECTIVE_TRIGGER_REQUIRED')
    _, role = _identity()
    if role != 'lead':
        raise SemanticError('SEMANTIC_LEAD_REQUIRED')
    import ai_judgment as J
    judgment = J.load_judgment(judgment_id) if judgment_id else None
    if not judgment or judgment.get('run_attempt_id') != os.environ.get('RUN_ATTEMPT_ID') or judgment.get('code') != code or judgment.get('confidence_for') != side:
        raise SemanticError('SEMANTIC_ACCEPTED_JUDGMENT_REQUIRED')
    semantic = judgment.get('semantic') or {}
    final = _read('stages', semantic.get('stage_id', 'missing'))
    chain = _read('chains', semantic.get('chain_id', 'missing'))
    if not final or not chain or final.get('judgment_id') != judgment_id:
        raise SemanticError('SEMANTIC_FINAL_STAGE_INCOMPLETE')
    _fresh(chain)
    if confidence != judgment['confidence_raw']:
        raise SemanticError('SEMANTIC_CONFIDENCE_MISMATCH')
    _write('consumptions', 'use-' + uuid.uuid4().hex,
           {'stage': stage, 'judgment_id': judgment_id, 'chain_id': chain['chain_id'], 'input_id': final['input_id'],
            'confidence_raw': judgment['confidence_raw'], 'created_at': C.now().isoformat()})
    return judgment['confidence_raw']


def invalidate(dependency_id, reason, *, chain_id=None):
    import policy_registry as PR
    with PR.change_lock():
        return _invalidate(dependency_id, reason, chain_id=chain_id)


def _invalidate(dependency_id, reason, *, chain_id=None):
    """Trusted correction hook, not an AI tool. Never cancels sent orders."""
    _text(reason, 'reason')
    affected = []
    root = C.ontology_root() / 'semantic'
    for path in root.glob('*/chains/*.json'):
        chain = record_store.load(path)
        attempt, cid = chain['run_attempt_id'], chain['chain_id']
        if chain_id is not None and chain_id != cid:
            continue
        stages = [record_store.load(p) for p in (root / attempt / 'stages').glob(cid + '-*.json')]
        # Match scalar identifiers, including relationship and query-set refs;
        # values alone are never used to infer unchanged dependencies.
        def includes(value):
            if isinstance(value, dict): return any(includes(v) for v in value.values())
            if isinstance(value, list): return any(includes(v) for v in value)
            return isinstance(value, str) and value == dependency_id
        if includes(chain) or includes(stages):
            target = _path('invalidations', cid, attempt)
            if not target.exists():
                record_store.publish(target, {'chain_id':cid, 'dependency_id':dependency_id, 'reason':reason,
                                             'created_at':C.now().isoformat(), 'status':'reassessment_required'}, kind='semantic_invalidation')
            affected.append({'chain_id':cid,'run_attempt_id':attempt,'judgment_ids':[s['judgment_id'] for s in stages if s.get('judgment_id')]})
    return {'affected': affected, 'external_effects_cancelled': False}


def stored_trace(judgment_id):
    """Read a single historical chain without refreshing or changing it."""
    import ai_judgment as J
    judgment = J.load_judgment(judgment_id)
    if not judgment or not judgment.get('semantic'):
        return {'status':'legacy_or_unavailable','judgment_id':judgment_id}
    attempt = judgment['run_attempt_id']; cid = judgment['semantic']['chain_id']
    return {'judgment_id':judgment_id,'chain':_read('chains',cid,attempt),
            'stages':{r:_read('stages',cid+'-'+r,attempt) for r in ROLES},
            'eligibility':_read('invalidations',cid,attempt) or {'status':'not_invalidated'},
            'scope':'stored_only; not a certification of current eligibility'}


def trace(code, side):
    """Current attempt only; absent stages remain visible."""
    _identity()
    cid = _chain_id(code, side)
    chain = _read('chains', cid)
    if not chain: raise SemanticError('SEMANTIC_CHAIN_MISSING')
    return {'chain':chain, 'stages':{r:_stage(cid,r) for r in ROLES},
            'eligibility':_read('invalidations',cid) or {'status':'not_invalidated'},
            'limits':'Recorded qualitative judgments are not proven causal explanations.'}
