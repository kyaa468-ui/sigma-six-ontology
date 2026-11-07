"""Allowlisted public summaries, prepared outside the publication directory.

This module never calls logger.write_all(), git, a broker, or an AI. Even a
successful export is a private staging artifact, not a deployment operation.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import common as C
import trace_snapshot as V

VERSION = 'TRACE_PUBLIC@v1'
STATES = frozenset('unknown complete partial recorded recording_error unavailable missing corrupt uncommitted '
                   'not_submitted rejected accepted_unlinked linked not_applicable direction_unresolved '
                   'reference_only unlinked ambiguous missing_origin verified valid invalid protected reserved '
                   'visible public private no_judgment no_reference no_link legacy_unlinked '
                   'not_started not_run running unfinished failed timeout insufficient_data eligible ineligible '
                   'passed active inactive suspended expired stale_base revoked prepared applied '
                   'complete_maintained complete_refuted early_refuted maintained refuted unscored conflict '
                   'in_progress data_incomplete ended normal error abandoned closed open '
                   'confirmed_full confirmed_partial balance_reconciled_estimate legacy_unverified '
                   'dispatched_order zero_quantity_order blocked_or_not_dispatched_order '
                   'blocked watch hold bought sold rule_exit'.split())
ACTIONS = frozenset(('매수', '매도', '관망', '보유', '매수 예정', '매도 예정', 'buy', 'sell', 'hold', 'watch',
                     'rule_exit', '규칙 매도', 'blocked', '차단', '검토'))
DIRECTIONS = frozenset(('매수', '매도', 'buy', 'sell', 'direction_unresolved'))
LABELS = {'linked': '판단 기록 정상 연결', 'not_submitted': '구조화 판단 미제출',
          'rejected': '판단 제출 거절', 'accepted_unlinked': '수락 후 실행 연결 미완료',
          'recording_error': '기록 확인 필요', 'protected': '검증 자료 보호 중',
          'reserved': '검증 자료 보호 중', 'unknown': '상태 미확인'}


def opaque_reference(kind, identifier):
    if kind not in ('judgment_id', 'order_attempt_id', 'plan_id') or not isinstance(identifier, str) or not identifier:
        raise ValueError('INVALID_PUBLIC_REFERENCE')
    return 'public-ref-' + hashlib.sha256((kind + ':' + identifier).encode('utf-8')).hexdigest()


def _status(value):
    return value if isinstance(value, str) and value in STATES else 'unknown'


def _time(value):
    try:
        time = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return time.isoformat() if time.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9]{6}', value) else None


def _trace_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'trace-[a-f0-9]{16,64}', value):
        raise ValueError('INVALID_OPAQUE_TRACE_ID')
    return value


def _target(row):
    visibility = _status(row.get('visibility', 'visible'))
    protected = visibility in ('protected', 'reserved')
    status = 'protected' if protected else _status(row.get('coverage_status'))
    # Explicit construction is intentional: adding a private field upstream
    # must not silently expand the public schema, even deeply nested fields.
    return {'trace_id': _trace_id(row.get('trace_id')), 'code': None if protected else _code(row.get('code')),
            'created_at': None if protected else _time(row.get('created_at')),
            'confidence_for': row.get('confidence_for') if not protected and row.get('confidence_for') in DIRECTIONS else None,
            'coverage_status': status,
            'connection_status': 'protected' if protected else _status(row.get('connection_status')),
            'action': row.get('action') if not protected and row.get('action') in ACTIONS else None,
            'visibility': 'protected' if protected else 'public',
            'outside_expected_denominator': row.get('outside_expected_denominator') is True,
            'server_summary': LABELS.get(status, '보관된 판단 연결 상태')}


def _refs(detail):
    output = set()
    def add(row):
        if not isinstance(row, dict): return
        for key in ('judgment_id', 'order_attempt_id', 'plan_id'):
            if isinstance(row.get(key), str) and row[key]:
                output.add((key, opaque_reference(key, row[key])))
    add(detail.get('target'))
    for judgment in detail.get('judgments', []): add(judgment)
    target = detail.get('target') or {}
    for identifier in target.get('judgment_ids', []):
        add({'judgment_id': identifier})
    context = detail.get('execution_context') or {}
    for order in context.get('orders', []):
        add(order)
        for call in order.get('original_calls', []): add(call)
        for event in order.get('events', []):
            add(event)
    for item in context.get('non_order_paths', []):
        add(item)
        add(item.get('call'))
    return [{'kind': kind, 'ref': ref} for kind, ref in sorted(output)]


def public_projection(loaded, export_id):
    projection = loaded['projection']
    targets, traces = [], {}
    cases = {row.get('application_id'): row for row in projection.get('cases', [])
             if isinstance(row, dict) and row.get('application_id')}
    for source in projection.get('targets', []):
        target = _target(source)
        targets.append(target)
        detail = projection.get('traces', {}).get(source['trace_id'], {})
        if target['visibility'] == 'protected':
            traces[target['trace_id']] = {**target, 'origin_refs': [], 'stage_counts': {},
                                          'premise_states': [], 'validation_states': [], 'errors_present': False}
            continue
        premise_ids = {p.get('application_id') for p in detail.get('premises', []) if isinstance(p, dict)}
        from case_dataset import _state
        states = Counter(_status(_state(cases[key])) for key in premise_ids if key in cases)
        validations = Counter(_status(row.get('status')) for row in detail.get('validations', []) if isinstance(row, dict))
        stage_counts = {name: len(detail[name]) if isinstance(detail.get(name), list) else None
                        for name in ('deliveries', 'judgments', 'premises', 'evaluations', 'reviews', 'proposals', 'validations')}
        orders = (detail.get('execution_context') or {}).get('orders')
        stage_counts['orders'] = len(orders) if isinstance(orders, list) else None
        traces[target['trace_id']] = {**target, 'origin_refs': _refs(detail),
            'stage_counts': stage_counts, 'premise_states': [{'status': k, 'count': v} for k,v in sorted(states.items())],
            'validation_states': [{'status': k, 'count': v} for k,v in sorted(validations.items())],
            'errors_present': bool(detail.get('errors'))}
    general = [row for row in targets if not row['outside_expected_denominator']]
    coverage = Counter(row['coverage_status'] for row in general)
    incomplete = (bool(projection.get('errors'))
                  or any(row.get('target_set_incomplete') for row in projection.get('coverage', []))
                  or any(row['visibility'] == 'protected' for row in general))
    return {'schema_version': VERSION, 'export_id': export_id, 'view_id': loaded['view_id'],
            'created_at': loaded['manifest']['created_at'],
            'market_as_of': loaded['manifest']['market_as_of'],
            'knowledge_as_of': loaded['manifest']['knowledge_as_of'], 'status': 'complete',
            'targets': targets, 'traces': traces,
            'counts': {'targets': len(targets), 'expected_general_judgments': len(general),
                       'coverage_states': dict(sorted(coverage.items())), 'target_set_incomplete': incomplete,
                       'linked_rate': coverage.get('linked', 0) / len(general) if general and not incomplete else None,
                       'rule_exit_count': sum(row['coverage_status'] == 'rule_exit' for row in targets),
                       'protected_targets': sum(x['visibility'] == 'protected' for x in targets)},
            'meaning': 'stored_status_summary_not_execution_or_premise_truth_validation'}


def _replace_manifest(path, manifest):
    descriptor, temporary = tempfile.mkstemp(prefix='.public-manifest-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(V.canonical(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def export_trace_summary(view_id):
    """Prepare a complete allowlisted generation; never write under docs/."""
    try:
        loaded = V.load_view_snapshot(view_id)
        if loaded.get('status') != 'complete':
            return {'status': 'unavailable', 'view_id': view_id, 'reason': 'VIEW_NOT_COMPLETE',
                    'errors': loaded.get('errors', [])}
        export_id = 'export-' + V.digest([VERSION, view_id, loaded['manifest']['projection_hash']])[:32]
        summary = public_projection(loaded, export_id)
        import research_evidence as RE
        reservation = RE.reserve_usage(export_id, loaded['projection'].get('application_ids', []),
            source_refs=[{'view_id': view_id, 'projection_hash': loaded['manifest']['projection_hash']}],
            purpose='step8_public_summary_preparation')
        if not isinstance(reservation, dict) or not reservation.get('usage_id'):
            raise ValueError('PUBLIC_EXPOSURE_RESERVATION_UNCONFIRMED')
        V.check_visibility(loaded['projection'])
        folder = V.root() / 'public' / export_id
        V._write_immutable(folder / 'summary.json', summary)
        body_hash = hashlib.sha256((folder / 'summary.json').read_bytes()).hexdigest()
        manifest = {'schema_version': VERSION, 'export_id': export_id, 'view_id': view_id,
                    'created_at': loaded['manifest']['created_at'],
                    'market_as_of': loaded['manifest']['market_as_of'],
                    'knowledge_as_of': loaded['manifest']['knowledge_as_of'],
                    'files': {'summary.json': body_hash}, 'status': 'complete'}
        V._write_immutable(folder / 'manifest.json', manifest)
        V.check_visibility(loaded['projection'])
        V._write_immutable(folder / 'commit.json', {'export_id': export_id, 'view_id': view_id,
                           'manifest_hash': V.digest(manifest), 'exposure_reservation_id': reservation['usage_id'],
                           'exposure_reservation_hash': V.digest(reservation)})
        # A consumer switches only after all generation files are complete.
        _replace_manifest(V.root() / 'public' / 'manifest.json', manifest)
        return {'status': 'complete', 'view_id': view_id, 'export_id': export_id, 'manifest': manifest,
                'staging_path': str(folder), 'published': False}
    except Exception as exc:
        return {'status': 'recording_error', 'view_id': view_id, 'errors': [{'reason': str(exc)}], 'published': False}


def load_public_export(export_id=None):
    """Pure staged export read, used by offline checks and local previews only."""
    try:
        if export_id is None:
            manifest = V._read(V.root() / 'public' / 'manifest.json')
            export_id = manifest.get('export_id')
        if not isinstance(export_id, str) or not re.fullmatch(r'export-[a-f0-9]{32}', export_id):
            raise ValueError('INVALID_EXPORT_ID')
        folder = V.root() / 'public' / export_id
        manifest = V._read(folder / 'manifest.json')
        commit = V._read(folder / 'commit.json')
        if (manifest.get('schema_version') != VERSION or manifest.get('export_id') != export_id
                or manifest.get('status') != 'complete' or commit.get('manifest_hash') != V.digest(manifest)
                or commit.get('export_id') != export_id or commit.get('view_id') != manifest.get('view_id')
                or set(manifest.get('files', {})) != {'summary.json'}):
            raise ValueError('PUBLIC_MANIFEST_INTEGRITY_MISMATCH')
        loaded = V.load_view_snapshot(manifest['view_id'])
        if loaded.get('status') != 'complete':
            return {'status': 'unavailable', 'errors': [{'reason': 'SOURCE_VIEW_NOT_AVAILABLE'}]}
        import case_store as CS
        reservation = CS.load('usage_events', commit.get('exposure_reservation_id'))
        if (not reservation or V.digest(reservation) != commit.get('exposure_reservation_hash')
                or reservation.get('owner_id') != export_id
                or sorted(reservation.get('application_ids', [])) != sorted(loaded['projection'].get('application_ids', []))):
            raise ValueError('PUBLIC_EXPOSURE_RESERVATION_MISSING_OR_CHANGED')
        summary = V._read(folder / 'summary.json')
        if (hashlib.sha256((folder / 'summary.json').read_bytes()).hexdigest() != manifest['files']['summary.json']
                or summary != public_projection(loaded, export_id)):
            raise ValueError('PUBLIC_SUMMARY_INTEGRITY_MISMATCH')
        return {'status': 'complete', 'manifest': manifest, 'summary': summary}
    except Exception as exc:
        return {'status': 'recording_error', 'errors': [{'reason': str(exc)}]}
