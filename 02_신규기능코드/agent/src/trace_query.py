"""Stored-only step-8 projections. Never collect, repair, dispatch or run models.

The returned object is a *preparation input*, not permission to present research:
trace_snapshot reserves exposure before publishing it. Plain early records retain
that integrity grade; a later view hash does not turn them into signed originals.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import common as C
import snapshot_store
import record_store
import storage_objects as objects
from collections import OrderedDict

VERSION = 'TRACE_VIEW@v1'
TIME_FIELDS = ('created_at', 'available_at', 'committed_at', 'recorded_at', 'finished_at',
               'started_at', 'registered_at', 'knowledge_as_of', 'captured_at', 'published_at')
STORE_FORMATS = {'scoring': 'immutable-scoring-record@v1',
                 'case_review': 'immutable-case-review-record@v1',
                 'learning_proposals': 'immutable-learning-proposal-record@v1',
                 'policy_validation': 'immutable-policy-validation-record@v1',
                 'policy_registry': 'immutable-policy-registry-record@v1'}
IDENTIFIERS = {'attempts': 'run_attempt_id', 'coverage_events': 'event_id', 'coverage_errors': 'error_id',
    'judgments': 'judgment_id', 'submissions': 'event_id', 'deliveries': 'delivery_id', 'selections': 'run_id',
    'case_review/jobs': 'review_id', 'case_review/results': 'review_id', 'case_review/datasets': 'review_id',
    'case_review/publication_commits': 'review_id', 'case_review/model_publications': 'publication_id',
    'learning_proposals/contexts': 'proposal_run_id', 'learning_proposals/context_commits': 'proposal_run_id',
    'learning_proposals/proposals': 'proposal_id', 'learning_proposals/submissions': 'submission_id',
    'policy_validation/specs': 'validation_id', 'policy_validation/spec_commits': 'validation_id',
    'policy_validation/results': 'result_id', 'policy_validation/result_commits': 'result_id',
    'policy_validation/inputs': 'input_id', 'policy_validation/input_commits': 'input_id',
    'policy_validation/attempts': 'validation_id'}


def digest(value):
    return objects.fingerprint(value)[0]


def _has_policy_context(value):
    # Preserve the historical ASCII substring test without constructing a full
    # JSON string. Keep the seam because canonical chunks can split a word.
    tail = b''
    for chunk in objects.chunks(value):
        text = tail + chunk
        if b'policy' in text:
            return True
        tail = text[-5:]
    return False


def instant(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('TIMEZONE_REQUIRED')
    return value.astimezone(C.KST)


class Reader:
    """Freeze reads as bytes and retain errors, including deleted accepted IDs."""
    def __init__(self, cutoff):
        self.root = C.ontology_root()
        self.cutoff = instant(cutoff)
        self.manifest = {}
        self.errors = []
        self.cache = OrderedDict()
        self.resolver = objects.resolver_for(self.root)

    def visible(self, value):
        stamps = [instant(value[k]) for k in TIME_FIELDS if value.get(k)]
        return not stamps or max(stamps) <= self.cutoff

    def read(self, relative, *, required=False):
        relative = str(relative)
        if relative in self.cache:
            value = self.cache[relative]
            if required and value is None:
                self.errors.append({'path': relative, 'reason': 'required_source_unavailable'})
            return copy.deepcopy(value)
        try:
            path = self.root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                raise ValueError('SOURCE_PATH_OUTSIDE_ONTOLOGY')
            dependencies = {}
            value = record_store.load(path, base=self.root, dependencies=dependencies,resolver=self.resolver)
            stored = record_store.manifest(path, base=self.root)
            if stored.get('format') == record_store.FORMAT:
                commit=record_store.commit_path(path)
                dependencies[str(commit.absolute())]=objects.file_fingerprint(commit)[0]
                objects.verify_closure(stored['object_ref'], base=self.root, dependencies=dependencies,resolver=self.resolver)
            if not isinstance(value, dict):
                raise ValueError('SOURCE_OBJECT_REQUIRED')
            store = relative.split('/')[0]
            grade = 'plain_record'
            if store in STORE_FORMATS:
                if value.get('format') != STORE_FORMATS[store] or not isinstance(value.get('record'), dict):
                    raise ValueError('SOURCE_INTEGRITY_ENVELOPE_REQUIRED')
                body = value['record']
                if not isinstance(value,objects.ObjectDict) and digest(body) != value.get('content_sha256'):
                    raise ValueError('SOURCE_HASH_MISMATCH')
                value, grade = body, 'envelope_verified'
            folder = str(Path(relative).parent)
            expected_id = IDENTIFIERS.get(folder, IDENTIFIERS.get(store) if store in ('coverage_events', 'coverage_errors') else None)
            if expected_id and (not isinstance(value.get(expected_id), str) or not value[expected_id]):
                raise ValueError('SOURCE_IDENTIFIER_REQUIRED:' + expected_id)
            static_definition = folder == 'executions/definitions/engine_definition'
            if (expected_id or store in ('executions', 'premises', 'regimes')) and not static_definition and not any(value.get(k) for k in TIME_FIELDS):
                raise ValueError('SOURCE_AVAILABILITY_UNRECORDED')
            if not self.visible(value):
                self.cache[relative] = None
                if len(self.cache)>128:self.cache.popitem(last=False)
                return None
            source_fingerprint=objects.file_fingerprint(path)[0]
            previous=self.manifest.get(relative)
            if previous is not None and previous['sha256']!=source_fingerprint:
                raise ValueError('SOURCE_CHANGED_AFTER_CACHE_EVICTION')
            for dependency, fingerprint in dependencies.items():
                blob = Path(dependency)
                if not blob.resolve().is_relative_to(self.root.resolve()):
                    raise ValueError('SNAPSHOT_DEPENDENCY_OUTSIDE_ONTOLOGY')
                key = str(blob.resolve().relative_to(self.root.resolve()))
                self.manifest[key] = {'path': key, 'kind': 'snapshots/blobs', 'id': blob.stem,
                    'sha256': fingerprint, 'integrity': 'content_addressed_blob_verified',
                    'temporal_provenance': 'content_only_parent_record_controls_availability'}
            self.manifest[relative] = {'path': relative, 'kind': str(Path(relative).parent),
                'id': Path(relative).stem, 'sha256': source_fingerprint, 'integrity': grade,
                'temporal_provenance': 'recorded' if any(value.get(k) for k in TIME_FIELDS) else 'unrecorded'}
            self.cache[relative] = value
            if len(self.cache) > 128: self.cache.popitem(last=False)
            return copy.deepcopy(value)
        except FileNotFoundError:
            if required:
                self.errors.append({'path': relative, 'reason': 'source_missing'})
        except (OSError, ValueError, TypeError) as exc:
            self.errors.append({'path': relative, 'reason': 'source_corrupt_or_unreadable', 'error': str(exc)})
        self.cache[relative] = None
        if len(self.cache)>128:self.cache.popitem(last=False)
        return None

    def scan(self, relative):
        folder = self.root / relative
        try:
            if folder.is_symlink():
                raise ValueError('SOURCE_DIRECTORY_SYMLINK_FORBIDDEN')
            paths = sorted(folder.rglob('*.json')) if folder.exists() else []
            return [row for p in paths if (row := self.read(p.relative_to(self.root))) is not None]
        except (OSError, ValueError) as exc:
            self.errors.append({'path': relative, 'reason': 'source_directory_unreadable', 'error': str(exc)})
            return []


def _ids(value, field):
    if isinstance(value, list):
        return set().union(*(_ids(v, field) for v in value)) if value else set()
    if not isinstance(value, dict):
        return set()
    found = set()
    own = value.get(field)
    if isinstance(own, str): found.add(own)
    if isinstance(own, list): found.update(v for v in own if isinstance(v, str))
    for child in value.values():
        if isinstance(child, (list, dict)): found.update(_ids(child, field))
    return found


def _application_ids(value):
    fields=('application_id','application_ids','premise_application_id',
            'support_case_ids','counter_case_ids','matched_case_ids','case_ids')
    found=set()
    def visit(node):
        if isinstance(node,list):
            for child in node:
                if isinstance(child,(dict,list)):visit(child)
        elif isinstance(node,dict):
            for field in fields:
                own=node.get(field)
                if isinstance(own,str):found.add(own)
                elif isinstance(own,list):found.update(item for item in own if isinstance(item,str))
            for child in _container_children(node):visit(child)
    visit(value)
    return found


def _container_children(value):
    for key in value:
        # Inspect the small node header before decoding a large scalar source.
        if isinstance(value,objects.ObjectDict):
            if objects.field_kind(value,key) not in ('dict','list'):continue
        child=value[key]
        if isinstance(child,(dict,list)):yield child


def _blocked(value, reservations, at=None):
    """Shared receipts are withheld whole: their free text can mention any row."""
    if not reservations:
        return False
    import validation_holdout as VH
    if isinstance(value, list): return any(_blocked(v, reservations, at) for v in value)
    if not isinstance(value, dict): return False
    origin = value.get('origin_at') or value.get('created_at') or value.get('registered_at') or at
    code = value.get('code', value.get('subject', value.get('instrument_id')))
    if isinstance(code, str) and any(VH._matches(r, code.removeprefix('KRX:'), origin) for r in reservations):
        return True
    return any(_blocked(child, reservations, origin) for child in _container_children(value))


def _exposure_scopes(value, at=None):
    """Include subjects in shared receipts, not only accepted case targets."""
    if isinstance(value, list):
        return [scope for child in value for scope in _exposure_scopes(child, at)]
    if not isinstance(value, dict) or value.get('visibility') == 'protected':
        return []
    origin = value.get('origin_at') or value.get('created_at') or value.get('registered_at') or at
    code = value.get('code', value.get('subject', value.get('instrument_id')))
    rows = []
    if isinstance(code, str) and code.removeprefix('KRX:').isdigit():
        rows.append({'code': code.removeprefix('KRX:'), 'created_at': origin,
                     'confidence_for': value.get('confidence_for'), 'application_id': value.get('application_id')})
    for child in value.values():
        if isinstance(child, (dict, list)): rows.extend(_exposure_scopes(child, origin))
    return rows


def _placeholder(identifier, reason, **fields):
    return {**fields, 'id': identifier, 'visibility': 'protected', 'reason': reason}


def _accepted(reader, judgments, submissions):
    commits = {s.get('event_id'): s for s in submissions if s.get('status') == 'accepted'}
    raw = {j['judgment_id']: j for j in judgments if j.get('judgment_id')}
    expected = set(raw) | {s.get('judgment_id') for s in commits.values()} - {None}
    accepted, unavailable = {}, []
    for jid in sorted(expected):
        row = raw.get(jid)
        commit = commits.get((row or {}).get('acceptance_event_id'))
        if row and commit and commit.get('judgment_id') == jid:
            accepted[jid] = row
        else:
            if not row: reader.read('judgments/' + jid + '.json', required=True)
            origin = row or next((s for s in commits.values() if s.get('judgment_id') == jid), {})
            unavailable.append({k: origin.get(k) for k in ('run_attempt_id', 'code', 'confidence_for', 'created_at')} |
                {'judgment_id': jid, 'reason': 'uncommitted' if row else 'missing_or_corrupt'})
    return accepted, unavailable


def _research(reader, reservations):
    """Join committed research versions; latest is selected only within cutoff."""
    # These captured records are immutable throughout this join. Reuse a
    # checked logical hash; the snapshot publisher still rechecks every source
    # file before commit. Bound retained root handles independently of blobs.
    hashes=OrderedDict()
    def checked_digest(value):
        key=id(value)
        if key not in hashes:
            hashes[key]=(value,digest(value))
            if len(hashes)>128:hashes.popitem(last=False)
        hashes.move_to_end(key)
        return hashes[key][1]
    reviews, proposals, validations = [], [], []
    jobs = reader.scan('case_review/jobs')
    review_results = {r['review_id']: r for r in reader.scan('case_review/results')}
    commits = {r['review_id']: r for r in reader.scan('case_review/publication_commits')}
    datasets = {r['review_id']: r for r in reader.scan('case_review/datasets')}
    review_submissions = reader.scan('case_review/review_submissions')
    known_jobs = {r['review_id'] for r in jobs}
    for rid in sorted((set(review_results) | set(datasets) | set(commits)) - known_jobs):
        reader.read('case_review/jobs/' + rid + '.json', required=True)
        jobs.append({'review_id': rid, 'origin_missing': True})
    review_errors = reader.scan('case_review/errors') + reader.scan('case_review/job_failures')
    publications = {r['publication_id']: r for r in reader.scan('case_review/model_publications')}
    for job in jobs:
        rid = job['review_id']; result = review_results.get(rid)
        data = datasets.get(rid)
        if reservations and data is None:
            reviews.append(_placeholder(rid, 'research_scope_unavailable_during_holdout',
                           review_id=rid, status='recording_error')); continue
        if _blocked(data or job, reservations):
            reviews.append(_placeholder(rid, 'prospective_validation_reserved', review_id=rid)); continue
        own = [s for s in review_submissions if s.get('review_id') == rid]
        commit = commits.get(rid)
        proof = bool(result and data and commit and not job.get('origin_missing')
                     and result.get('input_hash') == data.get('input_hash')
                     and all(publications.get(p, {}).get('review_id') == rid for p in commit.get('publication_ids', []))
                     and (not commit.get('result_hash') or commit['result_hash'] == checked_digest(result)))
        own_errors = [e for e in review_errors if e.get('review_id') == rid]
        review_status = ('recording_error' if own_errors or job.get('origin_missing') else
                         'complete' if proof and result.get('analysis_status') == 'complete' else
                         'partial' if result and result.get('analysis_status') == 'partial' else 'unfinished')
        reviews.append({**copy.deepcopy(result or job), 'review_id': rid,
            'status': review_status, 'errors': own_errors + (result or {}).get('errors', []),
            'completion_proof': {'status': 'verified' if proof else 'unavailable',
                'result_hash_bound_by_commit': bool(commit and commit.get('result_hash')),
                'meaning': 'stored_envelopes_and_input_binding; legacy_publication_marker_has_no_result_hash'},
            'result_hash': checked_digest(result) if result else None,
            'application_ids': sorted(_application_ids(data or {})), 'ai_submissions': own,
            'visibility': 'available', 'recording_status': 'recorded' if proof else 'incomplete'})
    contexts = {r['proposal_run_id']: r for r in reader.scan('learning_proposals/contexts')}
    context_commits = {r['proposal_run_id']: r for r in reader.scan('learning_proposals/context_commits')}
    submissions = reader.scan('learning_proposals/submissions')
    raw = {p['proposal_id']: p for p in reader.scan('learning_proposals/proposals')}
    accepted = {item['proposal_id'] for s in submissions for item in s.get('items', []) if item.get('status') == 'accepted'}
    for pid in sorted(accepted | set(raw)):
        proposal = raw.get(pid)
        if proposal is None:
            reader.read('learning_proposals/proposals/' + pid + '.json', required=True)
            proposals.append({'proposal_id': pid, 'status': 'recording_error', 'reason': 'accepted_proposal_missing'}); continue
        context = contexts.get(proposal['proposal_run_id']); commit = context_commits.get(proposal['proposal_run_id'])
        if reservations and context is None:
            proposals.append(_placeholder(pid, 'research_scope_unavailable_during_holdout',
                             proposal_id=pid, status='recording_error')); continue
        if _blocked(context or proposal, reservations):
            proposals.append(_placeholder(pid, 'prospective_validation_reserved', proposal_id=pid)); continue
        valid = pid in accepted and context and commit and commit.get('context_hash') == checked_digest(context)
        proposals.append({**proposal, 'status': 'accepted' if valid else 'recording_error',
            'visibility': 'available', 'validation_ids': [], 'current_activation': 'not_joined',
            'source_review_id': (context or {}).get('review_id')})
    specs = reader.scan('policy_validation/specs')
    spec_commits = {r['validation_id']: r for r in reader.scan('policy_validation/spec_commits')}
    starts = reader.scan('policy_validation/attempts')
    results = reader.scan('policy_validation/results')
    result_commits = {r['result_id']: r for r in reader.scan('policy_validation/result_commits')}
    failures = reader.scan('policy_validation/errors')
    inputs = {r['input_id']: r for r in reader.scan('policy_validation/inputs')}
    input_commits = {r['input_id']: r for r in reader.scan('policy_validation/input_commits')}
    known = {r['validation_id'] for r in specs + starts + results + failures + list(spec_commits.values()) +
             list(result_commits.values()) if r.get('validation_id')}
    protected_validations = {r['validation_id'] for r in reservations}
    for vid in sorted(known):
        spec = next((s for s in specs if s['validation_id'] == vid), None)
        if vid in protected_validations or _blocked(spec or {}, reservations):
            validations.append(_placeholder(vid, 'prospective_validation_reserved', validation_id=vid)); continue
        attempts = [s for s in starts if s['validation_id'] == vid]
        issues = [f for f in failures if f.get('validation_id') == vid]
        own = [r for r in results if r['validation_id'] == vid]
        missing_committed = [c for c in result_commits.values() if c.get('validation_id') == vid
                             and not any(r['result_id'] == c['result_id'] for r in own)]
        for commit in missing_committed:
            reader.read('policy_validation/results/' + commit['result_id'] + '.json', required=True)
            validations.append({'validation_id': vid, 'result_id': commit['result_id'],
                'result_hash': commit.get('result_hash'), 'status': 'recording_error',
                'visibility': 'available', 'reason': 'committed_result_missing', 'errors': issues})
        if spec is None and vid in spec_commits:
            reader.read('policy_validation/specs/' + vid + '.json', required=True)
        if not own:
            if missing_committed: continue
            validations.append({'validation_id': vid, 'proposal_id': (spec or {}).get('proposal_id'),
                'status': 'recording_error' if issues or spec is None and vid in spec_commits else 'unfinished' if attempts else 'not_started',
                'attempts': attempts, 'errors': issues, 'result_id': None, 'input_id': None, 'visibility': 'available'}); continue
        for result in sorted(own, key=lambda r: (r.get('created_at', ''), r['result_id'])):
            rid = result['result_id']; commit = result_commits.get(rid)
            source_input = inputs.get(result.get('input_id')); icommit = input_commits.get(result.get('input_id'))
            if reservations and source_input is None:
                validations.append(_placeholder(rid, 'research_scope_unavailable_during_holdout',
                                   validation_id=vid, result_id=rid, status='recording_error')); continue
            scommit = spec_commits.get(vid)
            spec_valid = bool(spec and scommit and scommit.get('spec_hash') == checked_digest(spec))
            valid = bool(commit and commit.get('result_hash') == checked_digest(result))
            input_valid = bool(source_input and icommit and icommit.get('input_hash') == checked_digest(source_input)
                               and result.get('input_hash') == checked_digest(source_input))
            if _blocked(result, reservations) or _blocked(source_input or {}, reservations):
                validations.append(_placeholder(rid, 'prospective_validation_reserved', validation_id=vid, result_id=rid)); continue
            pid = (result.get('proposal') or {}).get('proposal_id') or (spec or {}).get('proposal_id')
            validations.append({'validation_id': vid, 'proposal_id': pid, 'result_id': rid,
                'result_hash': checked_digest(result), 'input_id': result.get('input_id'), 'input_hash': result.get('input_hash'),
                'status': 'recording_error' if valid and (not input_valid or not spec_valid) else result.get('status') if valid else 'unfinished',
                'historical_result_status': result.get('status'), 'result_committed': valid,
                'spec_integrity': 'verified' if spec_valid else 'unavailable_or_mismatch',
                'input_integrity': 'verified' if input_valid else 'unavailable_or_mismatch',
                'result': result, 'attempts': [a for a in attempts if a.get('result_id') == rid],
                'errors': issues, 'visibility': 'available'})
    for proposal in proposals:
        proposal['validation_ids'] = sorted({v['validation_id'] for v in validations
            if v.get('proposal_id') == proposal.get('proposal_id')})
    return reviews, proposals, validations


@objects.scoped
def build_projection(market_as_of=None, knowledge_as_of=None):
    """Build a bounded projection; caller must reserve research use before display."""
    now = C.now(); cutoff = instant(knowledge_as_of or now)
    market = date.fromisoformat(market_as_of or cutoff.date().isoformat()).isoformat()
    if cutoff > now or market > cutoff.date().isoformat():
        raise ValueError('FUTURE_VIEW_CUTOFF')
    reader = Reader(cutoff)
    base = {'schema_version': VERSION, 'generated_at': now.isoformat(), 'market_as_of': market,
            'knowledge_as_of': cutoff.isoformat(), 'status': 'complete', 'targets': [], 'traces': {},
            'cases': [], 'reviews': [], 'proposals': [], 'validations': [], 'policies': [],
            'source_manifest': [], 'errors': [], 'protected_count': 0, 'application_ids': [],
            'protection_scopes': [], 'coverage': [],
            'temporal_scope': {'market_as_of': 'premise_evaluation_market_date',
                'knowledge_as_of': 'record_availability_cutoff_for_targets_deliveries_calls_and_research',
                'current_applicability': 'separate_capture_time_authority_not_historical_execution'}}
    import validation_holdout as VH
    try:
        reservations = VH.reservations()
    except Exception as exc:
        return {**base, 'status': 'recording_error', 'visibility': 'protected',
                'errors': [{'reason': 'holdout_protection_unavailable', 'error': str(exc)}]}
    # Protection is current, even when the selected knowledge cutoff is old.
    for kind in ('holdout_reservations', 'holdout_releases'):
        reader.scan('policy_validation/' + kind)
    manifests = reader.scan('attempts')
    events = reader.scan('coverage_events')
    submissions = reader.scan('submissions')
    judgments, unavailable = _accepted(reader, reader.scan('judgments'), submissions)
    reader.scan('premises')
    reader.scan('regimes')
    executions = reader.scan('executions')
    errors = reader.scan('coverage_errors')
    deliveries = {r['delivery_id']: r for r in reader.scan('deliveries') if r.get('delivery_id')}
    selections = {r['run_id']: r for r in reader.scan('selections') if r.get('run_id')}
    for folder in ('scoring', 'case_review', 'learning_proposals', 'policy_validation', 'policy_registry'):
        # Capture bytes BEFORE legacy pure readers build derived cases. A changed
        # file can never be paired with the earlier reader's derived values.
        reader.scan(folder)
    import judgment_coverage as JC
    import case_dataset as CD
    import evaluation_context as EC
    try:
        dataset = CD.build_cases(market, cutoff.isoformat())
        for reference in dataset.get('source_manifest', []):
            candidates = [ref for ref in reader.manifest.values() if ref['kind'] == reference.get('kind')
                          and ref['id'] == reference.get('id')]
            # Some scorer references identify an item by application rather than
            # its file ID; those are still covered by the complete byte inventory.
            if candidates and not any(reference.get('sha256') == ref['sha256'] or
                    reference.get('sha256') == digest(reader.read(ref['path'],required=True)) for ref in candidates):
                raise ValueError('CASE_SOURCE_CHANGED_DURING_PROJECTION')
        base['cases'] = [c for c in dataset['cases'] if not _blocked(c, reservations)]
        base['errors'].extend(dataset.get('errors', []))
        base['protected_count'] += dataset.get('withheld_count', 0)
    except Exception as exc:
        base['errors'].append({'reason': 'case_projection_failed', 'error': str(exc)})
    reviews, proposals, validations = _research(reader, reservations)
    base.update(reviews=reviews, proposals=proposals, validations=validations)
    known_attempts = {m['run_attempt_id'] for m in manifests}
    for source in list(judgments.values()) + unavailable + events + submissions:
        aid = source.get('run_attempt_id')
        if aid and aid not in known_attempts:
            manifests.append({'run_attempt_id': aid, 'run_id': source.get('run_id'), 'kind': 'unknown',
                'created_at': source.get('created_at'), 'initial_snapshot': {'holdings': []},
                'initial_targets': [], 'target_set_incomplete': True, 'origin_missing': True})
            reader.read('attempts/' + aid + '.json', required=True); known_attempts.add(aid)
    for manifest in sorted(manifests, key=lambda m: (m.get('created_at', ''), m['run_attempt_id'])):
        aid = manifest['run_attempt_id']; own_events = [e for e in events if e.get('run_attempt_id') == aid]
        own_subs = [s for s in submissions if s.get('run_attempt_id') == aid]
        own_errors = [e for e in errors if e.get('run_attempt_id') == aid]
        lost = [r for r in unavailable if r.get('run_attempt_id') == aid]
        try:
            coverage = JC._summarize(copy.deepcopy(manifest), copy.deepcopy(own_events), copy.deepcopy(own_subs),
                copy.deepcopy(own_errors + lost), judgment_loader=judgments.get)
        except Exception as exc:
            coverage = {'run_attempt_id': aid, 'run_status': 'unknown', 'targets': [],
                'expected_count': None, 'target_set_incomplete': True, 'recording_status': 'recording_error',
                'errors': [{'reason': 'coverage_projection_failed', 'error': str(exc)}]}
        base['coverage'].append({k: copy.deepcopy(v) for k, v in coverage.items()
            if k not in ('targets', 'rejection_history', 'rule_exit')})
        target_rows = list(coverage.get('targets', []))
        seen = {(r['code'], r['confidence_for']) for r in target_rows}
        # Rule exits and origins missing from old records remain explorable, but
        # never inflate the normal coverage denominator.
        for source in own_events + own_subs + lost:
            key = (source.get('code'), source.get('confidence_for'))
            if key[0] and key not in seen:
                target_rows.append({'code': key[0], 'confidence_for': key[1],
                    'status': 'rule_exit' if manifest.get('kind') == 'exit' or source.get('rule_exit') else 'recording_error',
                    'calls': [], 'submissions': [], 'outside_expected_denominator': True})
                seen.add(key)
        for target in target_rows:
            code, side = target['code'], target['confidence_for']
            own_j = [j for j in judgments.values() if (j.get('run_attempt_id'), j.get('code'), j.get('confidence_for')) == (aid, code, side)]
            jid = {j['judgment_id'] for j in own_j}
            tid = 'trace-' + digest([aid, code, side])[:32]
            origin = manifest.get('created_at')
            protected = any(VH._matches(r, code, origin, side) for r in reservations) or _blocked(own_j, reservations)
            row = {'trace_id': tid, 'run_attempt_id': aid, 'code': code, 'confidence_for': side,
                'created_at': origin, 'coverage_status': target.get('status'),
                'action': own_j[-1].get('recommended_action') if own_j else next((c.get('action') for c in target.get('calls', []) if c.get('action')), None),
                'judgment_ids': sorted(jid), 'connection_status': 'linked' if target.get('status') == 'linked' else
                    'recording_error' if lost else 'unlinked',
                'outside_expected_denominator': target.get('outside_expected_denominator', False),
                'visibility': 'protected' if protected else 'available'}
            if protected:
                row.update(judgment_ids=[], action=None, coverage_status='protected', connection_status='protected')
                base['targets'].append(row)
                base['traces'][tid] = {'target': row, 'visibility': 'protected', 'reason': 'prospective_validation_reserved'}
                base['protected_count'] += 1
                continue
            base['targets'].append(row)
            base['protection_scopes'].append({'code': code, 'created_at': origin, 'confidence_for': side})
            own_cases = [c for c in base['cases'] if c.get('origin_judgment_id') in jid or set(c.get('judgment_ids', [])) & jid]
            app_ids = {c['application_id'] for c in own_cases}
            own_reviews = [r for r in reviews if set(r.get('application_ids', [])) & app_ids]
            own_proposals = [p for p in proposals if _application_ids(p) & app_ids]
            pids = {p['proposal_id'] for p in own_proposals}
            own_validations = [v for v in validations if v.get('proposal_id') in pids]
            receipt_ids = sorted({did for j in own_j for did in j.get('delivery_ids', [])})
            receipt_rows, observations, source_refs, detail_errors = [], [], [], []
            for did in receipt_ids:
                receipt = deliveries.get(did)
                if receipt is None:
                    reader.read('deliveries/' + did + '.json', required=True)
                    detail_errors.append({'delivery_id': did, 'reason': 'delivery_unavailable_at_cutoff'}); continue
                if _blocked(receipt, reservations):
                    receipt_rows.append(_placeholder(did, 'shared_receipt_contains_reserved_data', delivery_id=did)); continue
                receipt_rows.append(receipt)
                ref = reader.manifest.get('deliveries/' + did + '.json')
                if ref: source_refs.append(ref)
                b = receipt.get('briefing_snapshot') or {}
                for item in b.get('candidates', []) + b.get('holdings', []):
                    if item.get('code') == code and (item.get('_ontology') or {}).get('observation'):
                        observation = item['_ontology']['observation']
                        selection = selections.get(observation.get('run_id'))
                        observations.append({'observation': observation, 'selection': selection if selection and not _blocked(selection, reservations) else None,
                            'selection_status': 'recorded' if selection and not _blocked(selection, reservations) else 'unavailable_or_protected'})
            execution = EC.execution_context(sorted(jid), cutoff.isoformat()) if jid else {'orders': [], 'scope': 'no_accepted_judgment'}
            if _blocked(execution, reservations):
                execution = {'orders': [], 'visibility': 'protected', 'reason': 'shared_execution_contains_reserved_data'}
            # Only explicitly related events; no same-code/time/order expansion.
            calls = [e for e in own_events if e.get('event_type') == 'call_enter' and e.get('code') == code
                     and e.get('confidence_for', e.get('coverage_target_direction')) == side]
            call_ids = {e['event_id'] for e in calls}
            call_results = [e for e in own_events if e.get('call_event_id') in call_ids]
            calls = [e if not _blocked(e, reservations) else _placeholder(e['event_id'],
                     'shared_call_contains_reserved_data', event_id=e['event_id']) for e in calls]
            call_results = [e if not _blocked(e, reservations) else _placeholder(e['event_id'],
                     'shared_result_contains_reserved_data', event_id=e['event_id']) for e in call_results]
            own_exec = [e for e in executions if e.get('judgment_id') in jid or
                e.get('coverage_event_id', e.get('call_event_id')) in call_ids or
                (e.get('payload') or {}).get('judgment_id') in jid]
            exact_attempt_ids = {e.get('order_attempt_id') for e in own_exec} - {None}
            # These are the same objects selected from executions above. Deep
            # equality would repeatedly restore unrelated event payloads.
            selected_objects = {id(e) for e in own_exec}
            own_exec = [e for e in executions if id(e) in selected_objects or e.get('order_attempt_id') in exact_attempt_ids]
            own_exec = [e for e in own_exec if not _blocked(e, reservations)]
            base['traces'][tid] = {'trace_id': tid, 'target': row, 'visibility': 'available',
                'observations': observations, 'deliveries': receipt_rows, 'judgments': own_j,
                'premises': [c.get('application_snapshot', {'application_id': c['application_id']}) for c in own_cases],
                'execution_context': execution, 'execution_events': own_exec, 'calls': calls, 'call_results': call_results,
                'evaluations': own_cases, 'reviews': own_reviews, 'proposals': own_proposals, 'validations': own_validations,
                'policy_contexts': [e for e in own_exec if _has_policy_context(e)],
                'unavailable_judgments': [r for r in lost if r.get('code') == code and r.get('confidence_for') == side],
                'source_refs': source_refs, 'errors': detail_errors,
                'performance_contract': 'reference_price_returns_only; no_code_time_or_episode_pnl_attribution'}
    for case in base['cases']:
        case['trace_ids'] = [t['trace_id'] for t in base['targets'] if t.get('visibility') == 'available' and
            (case.get('origin_judgment_id') in t['judgment_ids'] or set(case.get('judgment_ids', [])) & set(t['judgment_ids']))]
        base['protection_scopes'].append({'code': case.get('code'), 'created_at': case.get('origin_at'),
            'confidence_for': (case.get('stratum') or {}).get('confidence_for'), 'application_id': case['application_id']})
    base['application_ids'] = sorted({c['application_id'] for c in base['cases']} |
        set().union(*(_application_ids(r) for r in reviews + proposals + validations if r.get('visibility') == 'available')))
    try:
        import policy_registry as PR
        params = copy.deepcopy(C.params())
        current = PR.snapshot(params=params)
        if digest(params) != digest(C.params()):
            raise ValueError('CURRENT_POLICY_CHANGED_DURING_CAPTURE')
        current['authority_snapshot'] = {'params': params, 'params_hash': digest(params),
            'captured_at': now.isoformat(), 'meaning': 'capture_time_authority; not historical_execution'}
        current_reader = Reader(now)
        commits = current_reader.scan('policy_registry/activation_commits')
        # Current authority is intentionally separate from the selected past.
        # A commit newer than knowledge_as_of is not a missing current commit.
        for kind, identifier in (('packages', current.get('policy_revision')),
                                 ('activation_intents', current.get('activation_id')),
                                 ('adoptions', current.get('adoption_id'))):
            if identifier: current_reader.read('policy_registry/' + kind + '/' + identifier + '.json', required=True)
        reader.manifest.update({path: {**ref, 'scope': 'current_authority_capture'}
                                for path, ref in current_reader.manifest.items()})
        reader.errors.extend(current_reader.errors)
        histories = {kind: reader.scan('policy_registry/' + kind) for kind in
            ('adoptions', 'packages', 'activation_intents', 'activation_commits', 'revocations', 'rollbacks')}
        current['captured_at'] = now.isoformat()
        current['scope'] = 'current_applicability_not_historical_execution'
        current['activation_recording_status'] = ('complete' if any(r.get('activation_id') == current.get('activation_id') for r in commits)
            else 'applied_commit_missing' if current.get('activation_id') else 'not_applicable')
        base['policies'] = [{'current_applicability': current, 'history': histories}] if not _blocked(histories, reservations) else [
            {'current_applicability': {'status': current['status']}, 'visibility': 'protected'}]
    except Exception as exc:
        base['policies'] = [{'status': 'recording_error', 'error': str(exc)}]
    base['source_manifest'] = sorted(reader.manifest.values(), key=lambda r: r['path'])
    scopes = base['protection_scopes'] + _exposure_scopes({k: base[k] for k in
        ('traces', 'cases', 'reviews', 'proposals', 'validations', 'policies')})
    base['protection_scopes'] = list({digest(s): s for s in scopes}.values())
    base['errors'].extend(reader.errors)
    base['status'] = 'partial' if base['errors'] or unavailable or any(v.get('status') in ('recording_error', 'unfinished', 'partial') for v in validations + reviews + proposals) else 'complete'
    changed = []
    for ref in reader.manifest.values():
        try:
            if objects.file_fingerprint(reader.root / ref['path'])[0] != ref['sha256']:
                changed.append(ref['path'])
        except OSError:
            changed.append(ref['path'])
    if changed:
        # Preserve the target denominator, but withhold mixed-version evidence.
        base.update(status='recording_error', application_ids=[], protection_scopes=[], cases=[],
                    reviews=[], proposals=[], validations=[], policies=[])
        base['errors'].append({'reason': 'sources_changed_during_projection', 'paths': sorted(changed)})
        base['traces'] = {t['trace_id']: {'target': t, 'status': 'recording_error',
            'reason': 'sources_changed_during_projection'} for t in base['targets']}
    return base


def _page(rows, offset=0, limit=50):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('INVALID_PAGINATION')
    return {'items': copy.deepcopy(rows[offset:offset + limit]), 'offset': offset, 'limit': limit, 'total': len(rows)}


def list_judgment_targets(projection, offset=0, limit=50, code=None, status=None):
    rows = [r for r in projection['targets'] if (code is None or r.get('code') == code)
            and (status is None or r.get('coverage_status') == status)]
    return _page(rows, offset, limit)


def get_trace(projection, trace_id):
    return copy.deepcopy(projection['traces'].get(trace_id, {'trace_id': trace_id, 'status': 'not_found'}))


def get_case_links(projection, application_id):
    return copy.deepcopy(next((c for c in projection['cases'] if c['application_id'] == application_id),
        {'application_id': application_id, 'status': 'not_found'}))


def get_proposal_links(projection, proposal_id):
    return copy.deepcopy(next((p for p in projection['proposals'] if p['proposal_id'] == proposal_id),
        {'proposal_id': proposal_id, 'status': 'not_found'}))
