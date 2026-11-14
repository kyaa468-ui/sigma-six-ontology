"""Independent end-of-day evaluation jobs. No model, order or tuning calls.

The manifest fixes the denominator before collecting prices. Each item and the
terminal report are immutable; an interrupted job remains unfinished. Existing
judgment coverage and dispatch/monitor state are never modified here.
"""
from __future__ import annotations

import copy
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import common as C
import scoring_store as S


def _time(value=None):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value or C.now()
    if dt.tzinfo is None:
        raise ValueError('knowledge_as_of must include timezone')
    if dt > C.now():
        raise ValueError('Future knowledge_as_of is not allowed')
    return dt.astimezone(C.KST)


def _date(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


def _error(job_id, operation, exc):
    return S.record_error(job_id, operation, exc)


def _definition():
    names = ('close_evaluation.py', 'premise_evaluation.py', 'market_calendar.py',
             'daily_observations.py', 'evaluation_context.py', 'scoring_store.py')
    source = {name: Path(__file__).with_name(name).read_text(encoding='utf-8') for name in names}
    identifier = 'scoring-definition-' + S.digest(source)
    body = {'definition_id': identifier, 'sources': source, 'sha256': S.digest(source)}
    S.archive('definitions', identifier, body)
    return {'definition_id': identifier, 'sha256': body['sha256']}


def _instrument_kind(application):
    definition = application.get('definition_snapshot') or {}
    if definition.get('instrument_kind') in ('equity', 'etf'):
        return definition['instrument_kind']
    # The known ETF code lists determine provider routing only. They never
    # replace the premise's frozen adjustment/MA definition.
    return 'etf' if C.market_etf(application['code'])[0] else 'equity'


def _counts(items, expected, incomplete):
    counts = Counter(item.get('verdict', 'unknown') for item in items)
    failed = sum(item.get('recording_status') == 'recording_error' for item in items)
    conclusive = sum(item.get('verdict') in ('maintained', 'refuted') and item.get('recording_status') == 'recorded'
                     for item in items)
    return {'expected_count': expected, 'counts': dict(counts),
            'evaluated_count': sum(item.get('evaluation_id') is not None for item in items),
            'recording_error_count': failed,
            'conclusive_count': conclusive,
            'conclusive_rate': None if incomplete or not expected or failed else conclusive / expected,
            'rate_status': 'unknown' if incomplete or failed else 'not_applicable' if not expected else 'available'}


@__import__("evaluation_context").scoped
def run_evaluation(market_as_of=None, knowledge_as_of=None, *, phase='close',
                   calendar=None, provider=None, max_symbols=50):
    """Collect/evaluate committed premises; dependency injection is for local tests.

max_symbols is a collection budget, never a horizon or a trading rule. Targets
beyond that budget remain explicitly unevaluated and can be retried next job.
"""
    import market_calendar as MC
    import daily_observations as DO
    import premise_evaluation as PE
    import evaluation_context as EC

    live = knowledge_as_of is None
    known = _time(knowledge_as_of)
    target = (_date(market_as_of) if market_as_of else
              known.date() - timedelta(days=1) if phase == 'prep' else known.date())
    if not isinstance(target, date) or target > known.date():
        raise ValueError('market_as_of must be a known date')
    if max_symbols < 0:
        raise ValueError('max_symbols must be nonnegative')
    job_id = 'scoring-job-' + uuid.uuid4().hex
    # Historical replay can use already archived observations; a fresh network
    # receipt must never be backdated to the requested knowledge cutoff.
    manifest = {'scoring_job_id': job_id, 'phase': phase, 'market_as_of': target.isoformat(),
                'knowledge_as_of': known.isoformat(), 'started_at': C.now().isoformat(),
                'max_symbols': max_symbols, 'expected_targets': [], 'target_set_incomplete': False}
    errors, items, storage_failed = [], [], False
    try:
        selected = PE.committed_premises(knowledge_as_of=known.isoformat(), market_as_of=target.isoformat())
        applications = selected['applications']
        manifest['expected_targets'] = [{'application_id': x['application']['application_id'],
                                         'code': x['application']['code'], 'judgment_ids': x['judgment_ids']} for x in applications]
        manifest['target_set_incomplete'] = bool(selected.get('target_set_incomplete'))
        errors.extend(selected.get('errors', []))
        manifest['definition'] = _definition()
        S.archive('jobs', job_id, manifest)
    except Exception as exc:
        error = _error(job_id, 'job_start', exc)
        return {**manifest, 'run_status': 'recording_error', 'recording_status': 'recording_error',
                'target_set_incomplete': True, 'errors': [error],
                **_counts([], len(manifest['expected_targets']), True)}

    legacy = None
    try:
        legacy = EC.capture_legacy_context(target.isoformat(), known.isoformat())
        if legacy.get('recording_status') == 'recording_error':
            errors.extend(legacy.get('errors', []))
            storage_failed = True
        S.archive('legacy_contexts', job_id, {'scoring_job_id': job_id, **legacy})
    except Exception as exc:
        storage_failed = True
        errors.append(_error(job_id, 'legacy_context', exc))

    windows, collected = {}, {}
    if applications:
        try:
            if calendar is None:
                first = min(_date(x['application']['judgment_date']) for x in applications)
                # An enormous submitted horizon is preserved, not guessed or
                # truncated to a different premise. Calendar resource limits
                # become explicit unknown windows.
                reasonable = [x for x in applications if x['application']['days'] <= 3650]
                last = max([known.date() + timedelta(days=14), target + timedelta(days=14)] + [
                    _date(x['application']['judgment_date']) + timedelta(days=x['application']['days'] * 2 + 30)
                    for x in reasonable])
                start = (first - timedelta(days=100)).isoformat()
                # Explicit local reviewed snapshots may extend beyond 2026.
                # Missing padding stays partial; no future sessions are guessed.
                calendar = MC.select_calendar_snapshot(start, last.isoformat(), None if live else known.isoformat(),
                                                       collect_default=live)
            if calendar.get('recording_status') == 'recording_error':
                storage_failed = True
                errors.append({'operation': 'calendar', 'reason': calendar.get('reason'), 'detail': calendar.get('detail')})
            grouped = {}
            for entry in applications:
                app = entry['application']
                window = ({'status': 'unknown', 'sessions': [], 'reason': 'calendar_resource_limit'}
                          if app['days'] > 3650 else MC.resolve_window(app, calendar))
                windows[app['application_id']] = window
                if window.get('recording_status') == 'recording_error':
                    storage_failed = True
                    errors.append({'operation': 'window', 'application_id': app['application_id'],
                                   'reason': window.get('reason'), 'detail': window.get('detail')})
                if window.get('status') == 'resolved' and PE.definition_compatibility(app)['status'] == 'compatible':
                    key = (app['code'], _instrument_kind(app))
                    grouped.setdefault(key, set()).update(d for d in window['sessions'] if d <= target.isoformat())
            projection_cutoff = C.now().isoformat() if live else known.isoformat()
            receipts = S.records('daily_receipts')
            if receipts['errors']:
                storage_failed = True
                errors.extend(receipts['errors'])
            last_receipt = {}
            for receipt in receipts['records']:
                key = (receipt['code'], receipt.get('instrument_kind', 'equity'))
                last_receipt[key] = max(last_receipt.get(key, ''), receipt['available_at'])
            fetched = 0
            for key, dates in sorted(grouped.items(), key=lambda pair: (last_receipt.get(pair[0], ''), pair[0])):
                projection = DO.confirm_daily_observations(key[0], dates, calendar, receipts['records'],
                                                          projection_cutoff, instrument_kind=key[1])
                pending = any(row.get('status') != 'policy_complete' for row in projection.values())
                should_fetch = live and bool(dates) and (phase == 'manual' or pending)
                within_budget = fetched < max_symbols
                try:
                    collected[key] = DO.collect_daily_observations(key[0], sorted(dates), calendar,
                        provider=provider, knowledge_as_of=projection_cutoff, instrument_kind=key[1],
                        collect_live=should_fetch and within_budget)
                    if should_fetch and within_budget:
                        fetched += 1
                    elif should_fetch:
                        collected[key]['errors'].append({'reason': 'collection_budget'})
                    if collected[key].get('recording_status') == 'recording_error':
                        storage_failed = True
                except Exception as exc:
                    storage_failed = True
                    collected[key] = {'rows': {}, 'errors': [{'reason': 'collection_failed', 'error': str(exc)}], 'recording_status': 'recording_error'}
                    errors.append(_error(job_id, 'daily_collection:' + key[0], exc))
        except Exception as exc:
            storage_failed = True
            errors.append(_error(job_id, 'calendar_or_collection', exc))

    # Receipts use their actual availability, never the job's earlier start.
    evaluation_cutoff = C.now().isoformat() if live else known.isoformat()
    for entry in applications:
        app = entry['application']
        item = {'scoring_job_id': job_id, 'application_id': app['application_id'],
                'judgment_ids': entry['judgment_ids'], 'market_as_of': target.isoformat(),
                'knowledge_as_of': evaluation_cutoff}
        try:
            window = windows.get(app['application_id']) or {'status': 'unknown', 'sessions': [], 'reason': 'calendar_unavailable'}
            daily = collected.get((app['code'], _instrument_kind(app)), {'rows': {}, 'errors': []})
            result = PE.evaluate_premise(app, window, daily.get('rows', {}), target.isoformat(), evaluation_cutoff)
            result['collection_errors'] = copy.deepcopy(daily.get('errors', []))
            stored = PE.record_evaluation(result, scoring_job_id=job_id)
            item.update(evaluation_id=stored.get('evaluation_id'), verdict=stored.get('verdict', 'unknown'),
                        recording_status=stored.get('recording_status', 'recorded'),
                        execution_context=EC.execution_context(entry['judgment_ids'], evaluation_cutoff))
            if item['recording_status'] != 'recorded':
                storage_failed = True
            if item['execution_context'].get('recording_status') == 'recording_error':
                errors.extend(item['execution_context'].get('errors', []))
                storage_failed = True
        except Exception as exc:
            storage_failed = True
            item.update(verdict='unknown', recording_status='recording_error',
                        error=_error(job_id, 'evaluate:' + app['application_id'], exc))
        try:
            S.archive('job_items', 'scoring-item-' + S.digest([job_id, app['application_id']]), item)
        except Exception as exc:
            storage_failed = True
            item.update(recording_status='recording_error')
            errors.append(_error(job_id, 'job_item', exc))
        items.append(item)

    incomplete = manifest['target_set_incomplete'] or storage_failed
    report = {**manifest, 'finished_at': C.now().isoformat(), 'items': items, 'errors': errors,
              'effective_knowledge_as_of': evaluation_cutoff,
              'legacy_context_id': job_id if legacy is not None else None,
              'run_status': 'partial' if incomplete or errors else 'completed',
              'recording_status': 'recording_error' if storage_failed else 'recorded',
              **_counts(items, len(applications), incomplete)}
    try:
        S.archive('job_results', job_id, report)
    except Exception as exc:
        report.update(run_status='recording_error', recording_status='recording_error', conclusive_rate=None)
        report['errors'].append(_error(job_id, 'job_finish', exc))
    return report


def get_close_evaluations(scoring_job_id=None, market_as_of=None, limit=20, cursor=None, paged=False):
    """Read-only job view, reconstructing unfinished status without repairing it."""
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('limit must be 1..100')
    try:
        jobs = {'records': [S.load('jobs', scoring_job_id)], 'errors': []} if scoring_job_id else S.records('jobs')
    except Exception as exc:
        return {'jobs': [], 'errors': [{'scoring_job_id': scoring_job_id, 'error': str(exc)}], 'recording_status': 'recording_error'}
    output, errors = [], list(jobs['errors'])
    for manifest in jobs['records']:
        if not manifest or market_as_of and manifest['market_as_of'] != market_as_of:
            continue
        job_id = manifest['scoring_job_id']
        try:
            result = S.load('job_results', job_id)
            if result is None:
                saved = S.records('job_items')
                own = [x for x in saved['records'] if x.get('scoring_job_id') == job_id]
                failure = S.errors(job_id)
                result = {**manifest, 'run_status': 'unfinished', 'recording_status': 'incomplete',
                          'items': own, 'errors': failure['records'] + saved['errors'] + failure['errors'],
                          **_counts(own, len(manifest['expected_targets']), True)}
            row=copy.deepcopy(result)
            row['legacy_context']=S.load('legacy_contexts',job_id)
            if isinstance(row,S.objects.ObjectDict):row._record_order=getattr(manifest,'_record_order',0)
            else:row['_record_order']=getattr(manifest,'_record_order',0)
            output.append(row)
        except Exception as exc:
            errors.append({'scoring_job_id': job_id, 'error': str(exc)})
    if paged:
        import record_store
        page=record_store.versioned_page(output,key=lambda x:x['started_at']+'|'+x['scoring_job_id'],reverse=True,
            scope=str(('scoring',scoring_job_id,market_as_of)),cursor=cursor,limit=min(limit,20))
        jobs=page.pop('items')
        return {**page,'jobs':jobs,'errors':errors,'recording_status':'recording_error' if errors else 'recorded'}
    return {'jobs': sorted(output, key=lambda x: (x['started_at'], x['scoring_job_id']), reverse=True)[:limit],
            'errors': errors, 'recording_status': 'recording_error' if errors else 'recorded'}


def get_premise_evaluations(application_id=None, judgment_id=None, scoring_job_id=None,
                           knowledge_as_of=None, include_history=False, cursor=None,limit=50,paged=False):
    """Only archived evaluations. No collection, writes, or execution side effects."""
    read = S.records('evaluations')
    rows, errors = read['records'], list(read['errors'])
    if paged:
        import record_store
        scope=str(('premises',application_id,judgment_id,scoring_job_id,knowledge_as_of,include_history))
        cutoff=record_store.cursor_cutoff(cursor,scope)
        rows=[r for r in rows if getattr(r,'_record_order',0)<=cutoff]
    if judgment_id:
        import ai_judgment as J
        judgment = J.load_judgment(judgment_id)
        if judgment is None:
            errors.append({'judgment_id': judgment_id, 'error': 'accepted_judgment_unavailable'})
        ids = {x['application_id'] for x in (judgment or {}).get('premise_applications', [])}
        rows = [r for r in rows if r.get('application_id') in ids]
    if scoring_job_id:
        entries = S.records('job_items'); errors.extend(entries['errors'])
        ids = {r.get('evaluation_id') for r in entries['records'] if r.get('scoring_job_id') == scoring_job_id}
        rows = [r for r in rows if r.get('evaluation_id') in ids]
    if application_id:
        rows = [r for r in rows if r.get('application_id') == application_id]
    if knowledge_as_of:
        cutoff = _time(knowledge_as_of)
        filtered = []
        for row in rows:
            try:
                if _time(row['knowledge_as_of']) <= cutoff:
                    filtered.append(row)
            except Exception as exc:
                errors.append({'evaluation_id': row.get('evaluation_id'), 'error': str(exc)})
        rows = filtered
    rows = sorted(rows, key=lambda r: (r.get('knowledge_as_of', ''), r.get('evaluation_id', '')))
    if not include_history:
        newest = {}
        for row in rows:
            key = (row['application_id'], row.get('market_as_of'))
            newest[key] = max(newest.get(key, ''), row['knowledge_as_of'])
        rows = [row for row in rows if row['knowledge_as_of'] == newest[(row['application_id'], row.get('market_as_of'))]]
        superseded = {identifier for row in rows for identifier in row.get('supersedes', [])}
        rows = [row for row in rows if row['evaluation_id'] not in superseded]
    if paged:
        import record_store
        page=record_store.versioned_page(rows,key=lambda x:x.get('knowledge_as_of','')+'|'+x['evaluation_id'],scope=scope,cursor=cursor,limit=limit)
        values=page.pop('items')
        return {**page,'evaluations':values,'errors':errors,'recording_status':'recording_error' if errors else 'recorded'}
    return {'evaluations': rows, 'errors': errors, 'recording_status': 'recording_error' if errors else 'recorded'}
