"""Prospective reservation and positive, pre-outcome enrollment evidence.

Reservation blocks research exposure even when enrollment logging fails. Lack of
an exposure log is never proof of independence. Release is a separate immutable
operation, available only after a committed final validation result.
"""
from __future__ import annotations
import copy
from datetime import datetime
import common as C
import validation_store as S

VERSION = 'VALIDATION_HOLDOUT@v1'

def _time(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if value.tzinfo is None: raise ValueError('TIMEZONE_REQUIRED')
    return value.astimezone(C.KST)

def _matches(row, code, origin_at, direction=None):
    spec = row['spec']; scope = spec['scope']
    if not origin_at:
        # Once the reservation starts, an undated scoped record cannot prove it
        # is outside the held-out cohort. Quarantine it until provenance exists.
        return (C.now() >= _time(spec['period']['start']) and
                ('*' in scope['codes'] or code in scope['codes']))
    return (_time(spec['period']['start']) <= _time(origin_at) < _time(spec['period']['end'])
            and ('*' in scope['codes'] or code in scope['codes'])
            and (direction is None or direction in scope['confidence_for']))

def reservations(include_released=False):
    read = S.records('holdout_reservations'); releases = S.records('holdout_releases')
    if read['errors'] or releases['errors']: raise ValueError('HOLDOUT_HISTORY_UNREADABLE')
    released = {r['validation_id'] for r in releases['records']}
    return [r for r in read['records'] if include_released or r['validation_id'] not in released]

def reserve(manifest):
    spec = manifest['spec']; identifier = manifest['validation_id']
    if spec['mode'] != 'prospective': raise ValueError('PROSPECTIVE_SPEC_REQUIRED')
    row = {'validation_id': identifier, 'version': VERSION, 'spec': copy.deepcopy(spec),
           'registered_at': manifest['registered_at'], 'spec_hash': S.digest(manifest),
           'status': 'reserved', 'independence': 'requires_pre_outcome_enrollment'}
    existing = S.load('holdout_reservations', identifier)
    if existing:
        if existing != row: raise ValueError('HOLDOUT_RESERVATION_CONFLICT')
        return existing
    if _time(manifest['registered_at']) >= _time(spec['period']['start']): raise ValueError('LATE_RESERVATION')
    # Registration recovery after the start cannot manufacture pre-start proof.
    if C.now() >= _time(spec['period']['start']): raise ValueError('RESERVATION_WRITE_AFTER_START')
    for other in reservations():
        other_spec = other['spec']
        overlaps = (max(_time(spec['period']['start']), _time(other_spec['period']['start'])) <
                    min(_time(spec['period']['end']), _time(other_spec['period']['end'])))
        codes_overlap = ('*' in spec['scope']['codes'] or '*' in other_spec['scope']['codes'] or
                         bool(set(spec['scope']['codes']) & set(other_spec['scope']['codes'])))
        if not overlaps or not codes_overlap: continue
        if spec['family_id'] != other_spec['family_id']: raise ValueError('HOLDOUT_RESERVED_BY_OTHER_FAMILY')
        if any(spec['period'][k] != other_spec['period'][k] for k in ('start','end')): raise ValueError('FAMILY_HOLDOUT_PERIOD_MISMATCH')
        if spec.get('metrics',{}).get('family_size') != other_spec.get('metrics',{}).get('family_size'):
            raise ValueError('FAMILY_SIZE_MUST_BE_PREDECLARED_CONSISTENTLY')
    family = [r for r in reservations() if r['spec']['family_id'] == spec['family_id']]
    capacity = spec.get('metrics', {}).get('family_size')
    if capacity and len(family) >= capacity: raise ValueError('PREDECLARED_FAMILY_SIZE_EXCEEDED')
    return S.archive('holdout_reservations', identifier, row)

def _app(row):
    row = row.get('application', row)
    return row.get('application_id', row.get('premise_application_id')), row.get('code'), row.get('created_at', row.get('origin_at')), row

def discovery_exclusions(applications):
    """Inspect origin metadata only, before reading features or outcome values."""
    active = reservations(); output = []
    for source in applications:
        identifier, code, at, row = _app(source)
        ids = [r['validation_id'] for r in active if _matches(r, code, at)]
        if ids: output.append({'application_id': identifier, 'validation_ids': sorted(ids),
                               'reason': 'prospective_validation_reserved'})
    return output

def filter_cases(cases):
    active = reservations(); kept, withheld = [], []
    for case in cases:
        ids = [r['validation_id'] for r in active if _matches(r, case.get('code'), case.get('origin_at',case.get('created_at')))]
        if ids: withheld.append({'application_id': case.get('application_id'), 'validation_ids': sorted(ids),
                                 'reason': 'prospective_validation_reserved'})
        else: kept.append(case)
    return kept, withheld

def is_reserved_judgment(judgment):
    return [r['validation_id'] for r in reservations()
            if _matches(r, judgment.get('code'), judgment.get('created_at'), judgment.get('confidence_for'))]

def enroll_attempt(manifest, briefing=None, account=None):
    """Called immediately after the pre-AI target manifest is committed.

    Return errors without altering the trading/coverage path. Snapshot hashes and
    the persisted pre-start reservation establish positive proof; late callbacks
    remain ineligible even when their clock happens to match an old attempt.
    """
    results = []
    try:
        import judgment_coverage as JC
        attempt = manifest['run_attempt_id']; origin = manifest['created_at']; enrolled_at = C.now().isoformat()
        submissions, read_errors = JC._records('submissions', attempt)
        if read_errors: raise ValueError('SUBMISSION_HISTORY_UNREADABLE')
        before = [s for s in submissions if s.get('run_attempt_id') == attempt]
        elapsed = (_time(enrolled_at) - _time(origin)).total_seconds()
        for reservation in reservations():
            spec = reservation['spec']
            if manifest.get('kind') == 'exit': continue
            if not _time(spec['period']['start']) <= _time(origin) < _time(spec['period']['end']): continue
            targets = [copy.deepcopy(t) for t in manifest.get('initial_targets', [])
                       if _matches(reservation, t.get('code'), origin, t.get('confidence_for'))]
            observed_codes=set((manifest.get('initial_snapshot') or {}).get('candidates',[])) | set((manifest.get('initial_snapshot') or {}).get('holdings',[]))
            target_scope=[{'code':code,'confidence_for':side,'basis':'pre_ai_observed_instrument_and_predeclared_direction'}
                          for code in sorted(observed_codes) for side in spec['scope']['confidence_for']
                          if _matches(reservation,code,origin,side)]
            eid = 'enrollment-' + S.digest([reservation['validation_id'], attempt])[:40]
            old = S.load('holdout_enrollments', eid)
            if old:
                results.append(old); continue
            row = {'enrollment_id': eid, 'validation_id': reservation['validation_id'],
                   'run_attempt_id': attempt, 'origin_at': origin, 'enrolled_at': enrolled_at,
                   'reservation_hash': S.digest(reservation), 'source_manifest': copy.deepcopy(manifest),
                   'initial_targets': targets, 'eligible_target_scope':target_scope, 'briefing_snapshot': copy.deepcopy(briefing),
                   'account_snapshot': copy.deepcopy(account),
                   'target_set_incomplete': manifest.get('target_set_incomplete', True),
                   'status': 'enrolled_before_outcome' if not before and 0 <= elapsed <= 60 else 'late_enrollment',
                   'version': VERSION}
            results.append(S.archive('holdout_enrollments', eid, row))
        return {'recording_status': 'recorded', 'enrollments': results}
    except Exception as exc:
        return {'recording_status': 'recording_error', 'enrollments': results,
                'errors': [S.record_error(None, 'enroll_attempt', exc)]}

def status(validation_id):
    from validation_spec import load_spec
    manifest = load_spec(validation_id)
    read = S.records('holdout_enrollments')
    rows = [r for r in read['records'] if r.get('validation_id') == validation_id]
    reservation = S.load('holdout_reservations', validation_id)
    release = S.load('holdout_releases', validation_id)
    errors = list(read['errors'])
    if manifest['spec']['mode'] != 'prospective':
        return {'validation_id': validation_id, 'status': 'retrospective_not_independent',
                'independent': False, 'recording_status': 'recorded', 'enrollments': []}
    valid = bool(reservation) and all(r.get('status') == 'enrolled_before_outcome'
             and r.get('reservation_hash') == S.digest(reservation) and not r.get('target_set_incomplete') for r in rows)
    import judgment_coverage as JC
    import research_evidence as RE
    apps, app_errors = JC._records('premises')
    errors.extend(app_errors)
    scoped = [a.get('application_id') for a in apps if a.get('application_id') and
              reservation and _matches(reservation, a.get('code'), a.get('created_at'))]
    usage = RE.usage_status(scoped) if scoped else {'items':[], 'errors':[]}
    errors.extend(usage.get('errors',[]))
    exposed = [x['application_id'] for x in usage['items'] if x.get('status')=='used']
    return {'validation_id': validation_id, 'status': 'released' if release else 'reserved',
            'independent': bool(rows) and valid and not errors and not exposed and not release,
            'exposed_application_ids': exposed,
            'independence_basis': 'positive_pre_outcome_enrollment' if rows and valid else 'positive_proof_missing_or_incomplete',
            'recording_status': 'recording_error' if errors else 'recorded', 'errors': errors,
            'enrollments': rows, 'release': release}

def release(validation_id, result_id, request_key):
    """Make consumed holdout discoverable; never restore independent status."""
    if not isinstance(request_key,str) or not request_key.strip(): raise ValueError('REQUEST_KEY_REQUIRED')
    result = S.load('results', result_id)
    if not result or result.get('validation_id') != validation_id or result.get('status') not in ('complete','completed','insufficient_data','failed'):
        raise ValueError('FINAL_VALIDATION_RESULT_REQUIRED')
    commit = S.load('result_commits', result_id)
    if not commit or commit.get('result_hash') != S.digest(result): raise ValueError('FINAL_RESULT_NOT_COMMITTED')
    old = S.load('holdout_releases', validation_id)
    if old:
        if old.get('result_id') != result_id or old.get('request_key') != request_key: raise ValueError('RELEASE_CONFLICT')
        return old
    return S.archive('holdout_releases', validation_id, {'validation_id':validation_id,'result_id':result_id,
        'request_key':request_key,'released_at':C.now().isoformat(),'result_hash':S.digest(result),
        'independent_for_new_trials':False,'status':'released'})


def assert_discovery_allowed(application_ids):
    """Last boundary guard; known reserved IDs cannot be exposed by direct calls."""
    if not application_ids or not reservations(): return
    import judgment_coverage as JC
    apps, errors = JC._records('premises')
    if errors: raise ValueError('PREMISE_HISTORY_UNREADABLE_DURING_HOLDOUT')
    wanted=set(application_ids)
    if discovery_exclusions([a for a in apps if a.get('application_id') in wanted]):
        raise ValueError('RESERVED_HOLDOUT_DISCOVERY_FORBIDDEN')
