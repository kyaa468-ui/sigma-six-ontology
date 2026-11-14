"""Versioned AI delivery, separate from canonical evidence and storage versions.

Aliases identify a delivery event, not a value. Immutable reservation files are
the allocator's authority; the existing SQLite lock serializes allocation on
Windows and POSIX. A committed bundle records a response attempt, not proof that
a model read it. No market calls or trading decisions belong in this module.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from datetime import datetime

import common as C
import record_store as R
import storage_objects as O

FORMAT = 'COMPACT_EVIDENCE@v1'
RENDERER = 'compact-groups@v1'
DICTIONARY = 'evidence-fields@v1'
ENV = 'SION_EVIDENCE_WIRE_FORMAT'
_FROZEN = ContextVar('wire_frozen_bundles', default=None)
ALIAS = re.compile(r'B[1-9][0-9]*\Z')
REFERENCE = re.compile(r'(B[1-9][0-9]*):([EOM][1-9][0-9]*)\Z')
# These are audit identities, not measurement semantics. The original objects
# stay intact and can be opened through the recorded detail tool.
PRIVATE = frozenset({'evidence_id', 'observation_id', 'criterion_evaluation_id',
    'comparison_id', 'input_refs', 'lesson_context', 'source_case_ids',
    'lesson_version_id', 'proposal_id', 'adoption_id', 'validation_id'})
COMMON = ('unit', 'kind', 'status', 'source', 'observed_at', 'available_at', 'scope',
          'application_id', 'metric_kind', 'feature_name', 'model_id', 'stratum',
          'denominator', 'outcome', 'temporal_scope', 'evidence_version', 'model_version', 'transform')


class WireError(ValueError):
    def __init__(self, code, path='$', message=None):
        self.error = {'code': code, 'path': path, 'message': message or code}
        super().__init__(code)


def digest(value):
    return O.fingerprint(value)[0]


def selected_format():
    value = os.environ.get(ENV, FORMAT)
    if value not in (FORMAT, 'legacy'):
        raise WireError('UNSUPPORTED_WIRE_FORMAT')
    return value


def enabled():
    return selected_format() == FORMAT


def instructions(wire_format=None):
    if (wire_format or selected_format()) == 'legacy':
        return '\n이번 실행은 legacy 형식: 원래 delivery_ids 및 canonical 참조를 사용합니다.'
    return ('\n이번 실행의 전달 형식은 COMPACT_EVIDENCE@v1로 고정됩니다. groups의 common은 각 rows에 적용됩니다. '
            '판단/제안 항목에 wire_format과 bundle_refs:["B1"]를 명시합니다. '
            'evidence 및 assertion.right에는 "B1:E1", comparison_context에는 "B1:O1"을 사용합니다. '
            '복기 제출에도 wire_format과 bundle_refs를 명시하고 metric_id/right_metric_id, '
            '제안의 reference/right_reference에는 "B1:M1"을 사용합니다. '
            '실제 받은 번호만 사용하고 전달받은 값·머리말·주의 문구를 함께 하위 에이전트에 넘깁니다. '
            '동일 자료 재사용 시 번호를 유지합니다. get_briefing/get_account 재호출은 새 관측·전달입니다. '
            '추가 상세는 get_evidence_detail(ref,fields)로 조회하고 반환된 새 번호로 인용합니다. '
            '판단·주문·전제 적용 ID는 원래 형태를 유지합니다.')


def scope():
    mode = os.environ.get('RUN_MODE') or 'decide'
    research = {'case_review': ('CASE_REVIEW_ID', 'CASE_REVIEW_ATTEMPT_ID'),
                'proposal_review': ('PROPOSAL_RUN_ID', 'PROPOSAL_ATTEMPT_ID'),
                'validation_review': ('VALIDATION_ID', 'VALIDATION_ATTEMPT_ID')}
    keys = research.get(mode)
    attempt = os.environ.get(keys[1]) if keys else os.environ.get('RUN_ATTEMPT_ID')
    owner = os.environ.get(keys[0]) if keys else None
    # Older research runners supplied RUN_ID as their unique attempt identifier.
    attempt = attempt or (os.environ.get('RUN_ID') if keys else None)
    if not attempt or (keys and not owner):
        raise WireError('MISSING_WIRE_SCOPE')
    return {'run_attempt_id': attempt, 'run_id': os.environ.get('RUN_ID'),
            'mode': mode, 'research_id': owner}


def root(context=None):
    context = context or scope()
    folder = {'case_review': 'case_review', 'proposal_review': 'learning_proposals',
              'validation_review': 'policy_validation'}.get(context['mode'])
    base = C.ontology_root() / folder if folder else C.ontology_root()
    return base / 'evidence_wire' / digest(context)


@contextmanager
def locked(folder):
    folder = Path(folder)
    boundary = C.ontology_root()
    if not folder.resolve().is_relative_to(boundary.resolve()):
        raise WireError('WIRE_PATH_OUTSIDE_ONTOLOGY')
    current = folder
    while current != boundary.parent:
        if current.is_symlink():
            raise WireError('WIRE_SYMLINK_FORBIDDEN')
        current = current.parent
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'allocation.sqlite3'
    if path.is_symlink():
        raise WireError('WIRE_SYMLINK_FORBIDDEN')
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute('BEGIN IMMEDIATE')
        yield
        con.commit()
    finally:
        con.close()


def reserve(context):
    folder = root(context)
    with locked(folder):
        reservations = folder / 'reservations'
        reservations.mkdir(exist_ok=True)
        # Reservations survive a crash even before a receipt is committed.
        highest = max((int(p.stem[1:]) for p in reservations.glob('B*.json')
                       if ALIAS.fullmatch(p.stem)), default=0)
        alias = f'B{highest + 1}'
        R.publish(reservations / (alias + '.json'),
                  {'bundle_alias': alias, 'scope': context, 'status': 'prepared'}, kind='wire_reservation')
        return alias


def pin_format():
    """Bind the attempt before its first delivery, including legacy attempts."""
    chosen = selected_format()
    try:
        context = scope()
    except WireError:
        if chosen == 'legacy':
            return chosen  # historical offline callers had no attempt context
        raise
    folder = root(context)
    with locked(folder):
        path = folder / 'format.json'
        if path.exists():
            original = R.load(path)
            if original.get('wire_format') != chosen or original.get('scope') != context:
                raise WireError('ATTEMPT_WIRE_FORMAT_CHANGED')
        else:
            R.publish(path, {'scope': context, 'wire_format': chosen}, kind='wire_attempt_format')
    return chosen


def _leaves(value, prefix=''):
    if isinstance(value, dict) and value:
        return [p for k, v in value.items() for p in _leaves(v, prefix + '.' + k if prefix else k)]
    if isinstance(value, list) and value:
        return [p for i, v in enumerate(value) for p in _leaves(v, f'{prefix}.{i}')]
    return [prefix]


def visible(row, fields=None):
    if fields is not None:
        if not isinstance(fields, list) or not fields or any(not isinstance(k, str) or k not in row for k in fields):
            raise WireError('UNKNOWN_DETAIL_FIELD', 'fields')
        # Essential semantics are always visible, including on detail deliveries.
        keys = set(fields) | {'subject', 'field', 'value', 'unit', 'kind', 'status',
                              'source', 'observed_at', 'available_at', 'period', 'precision',
                              'display_precision', 'formula', 'meaning', 'temporal_scope',
                              'price_basis', 'reason', 'substituted', 'effective_value'}
    else:
        keys = set(row) - PRIVATE - {'reference', 'metric_id', 'result_hash', 'review_id'}
    result = {k: copy.deepcopy(v) for k, v in row.items() if k in keys}
    if 'quote_text' in result and result['quote_text'] == result.get('value'):
        del result['quote_text']  # same citable text; no information is removed
    if 'display_value' in result and result['display_value'] == result.get('value'):
        del result['display_value']
    # Preserve calculation meaning while leaving storage pointers on the server.
    if isinstance(result.get('provenance'), dict):
        result['provenance'].pop('input_snapshot_ref', None)
    if str(result.get('field', '')).startswith('adopted_lesson.'):
        result['field'] = 'adopted_lesson'
    return result


def render(alias, catalog, *, metric=False, metadata=None, fields=None):
    entries, observations, groups = {}, {}, {}
    for i, source in enumerate(catalog, 1):
        short = ('M' if metric else 'E') + str(i)
        row = visible(source, fields)
        target = (source.get('reference') or {'metric_id': source['metric_id']}) if metric else source['evidence_id']
        entries[short] = {'type': 'metric' if metric else 'evidence', 'target': target,
                          'visible_fields': _leaves(row), 'display_field': row.get('field')}
        subject = row.pop('subject', row.get('application_id', 'research') if metric else 'unknown')
        row = {'ref': alias + ':' + short, **row}
        groups.setdefault(str(subject), []).append(row)
        oid = source.get('observation_id')
        if oid and not metric:
            observations.setdefault(oid, {'subject': str(subject), 'members': []})['members'].append(alias + ':' + short)
    obs = []
    for i, (oid, value) in enumerate(observations.items(), 1):
        short = 'O' + str(i)
        entries[short] = {'type': 'observation', 'target': oid, 'visible_fields': []}
        obs.append({'ref': alias + ':' + short, **value})
    packed = []
    for subject, rows in groups.items():
        common = {k: rows[0][k] for k in COMMON if k in rows[0] and all(k in r and r[k] == rows[0][k] for r in rows)}
        packed.append({'subject': subject, 'common': common,
                       'rows': [{k: v for k, v in row.items() if k not in common} for row in rows]})
    payload = {'wire_format': FORMAT, 'bundle_alias': alias, 'groups': packed,
               'observations': obs, 'metadata': metadata or {},
               'citation_rule': '묶음과 번호를 함께 인용. null은 미확인, 0은 실제 0. 공통 머리말은 각 행에 적용. 출처 주장·AI 분류·채택 지침은 시장 사실 미검증.'}
    return payload, entries


def public_rows(payload):
    """Decode only the public protocol; used by offline clients/rehearsals."""
    if payload.get('wire_format') != FORMAT:
        raise WireError('UNSUPPORTED_WIRE_FORMAT')
    return [{**group['common'], **row, 'subject': group['subject']}
            for group in payload['groups'] for row in group['rows']]


def public_document(payload):
    if payload.get('wire_format') != FORMAT:
        return payload
    rows = public_rows(payload)
    values = {row['ref']: row['value'] for row in rows}
    def expand(value):
        if isinstance(value, dict):
            if set(value) == {'ref'} and value['ref'] in values:
                return copy.deepcopy(values[value['ref']])
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list): return [expand(v) for v in value]
        return value
    document = expand(payload.get('document', {}))
    document['metric_catalog'] = [{**row, 'metric_id': row['ref'], 'reference': row['ref']} for row in rows]
    return document


def prepare(catalog, *, metric=False, metadata=None, fields=None, context=None):
    if pin_format() != FORMAT:
        raise WireError('COMPACT_FORMAT_NOT_SELECTED')
    context = context or scope()
    alias = reserve(context)
    payload, entries = render(alias, catalog, metric=metric, metadata=metadata, fields=fields)
    prepared = {'wire_format': FORMAT, 'renderer_version': RENDERER, 'field_dictionary_version': DICTIONARY,
                'scope': context, 'bundle_alias': alias, 'entries': entries, 'payload': payload}
    if metric and catalog and all(row.get('reference') for row in catalog):
        common = {k: catalog[0]['reference'][k] for k in ('review_id', 'result_hash')}
        if any(any(row['reference'][k] != v for k, v in common.items()) for row in catalog):
            raise WireError('MIXED_RESEARCH_RESULTS')
        prepared['research_reference'] = common
        for entry in entries.values():
            entry['target'] = {'metric_id': entry['target']['metric_id']}
    return prepared


def receipt_fields(prepared):
    return {'wire_format': FORMAT, 'bundle_alias': prepared['bundle_alias'], 'wire_scope': prepared['scope']}


def commit(prepared, receipt, *, catalog_key='evidence_catalog', output=None, case_ids=None):
    """Called only after the canonical receipt (and research exposure) commits."""
    output = prepared['payload'] if output is None else output
    record = {k: v for k, v in prepared.items() if k != 'payload'}
    record.update(canonical_delivery_id=receipt['delivery_id'], catalog_key=catalog_key,
                  canonical_receipt_ref=O.put_object(receipt, kind='wire_canonical_receipt'),
                  visible_payload_ref=O.put_object(output, kind='wire_visible_payload'),
                  visible_payload_hash=digest(output), alias_map_hash=digest(prepared['entries']),
                  case_ids=list(case_ids or []), created_at=C.now().isoformat(), status='response_attempted')
    R.publish(root(prepared['scope']) / 'bundles' / (prepared['bundle_alias'] + '.json'), record, kind='wire_bundle')
    return output


def load(alias, *, context=None):
    if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
        raise WireError('INVALID_BUNDLE_REF')
    frozen = _FROZEN.get()
    if frozen is not None:
        record = frozen['bundles'].get(alias)
        if record is None:
            raise WireError('NOT_DELIVERED')
    else:
        context = context or scope()
        path = root(context) / 'bundles' / (alias + '.json')
        if not path.exists():
            raise WireError('UNKNOWN_BUNDLE')
        try:
            record = R.load(path)
        except (OSError, ValueError) as exc:
            raise WireError('WIRE_INTEGRITY_ERROR') from exc
        if record.get('scope') != context:
            raise WireError('WIRE_SCOPE_MISMATCH')
    required = {'wire_format', 'renderer_version', 'field_dictionary_version', 'scope', 'bundle_alias',
                'entries', 'alias_map_hash', 'canonical_delivery_id', 'canonical_receipt_ref',
                'catalog_key', 'visible_payload_ref', 'visible_payload_hash', 'created_at', 'status'}
    if not isinstance(record, dict) or not required <= set(record) or not isinstance(record['entries'], dict):
        raise WireError('WIRE_INTEGRITY_ERROR')
    if (record.get('wire_format') != FORMAT or record.get('renderer_version') != RENDERER
            or record.get('field_dictionary_version') != DICTIONARY):
        raise WireError('UNSUPPORTED_WIRE_VERSION')
    if record.get('bundle_alias') != alias or digest(record['entries']) != record.get('alias_map_hash'):
        raise WireError('WIRE_INTEGRITY_ERROR')
    if record.get('status') != 'response_attempted':
        raise WireError('DELIVERY_NOT_COMMITTED')
    at = frozen.get('at') if frozen else C.now()
    if at and datetime.fromisoformat(record['created_at']) > at:
        raise WireError('FUTURE_EVIDENCE')
    # Reading the exact saved payload also verifies the immutable reference.
    try:
        payload = O.open_object(record['visible_payload_ref'])
        if digest(payload) != record['visible_payload_hash']:
            raise ValueError('payload hash')
    except (OSError, ValueError) as exc:
        raise WireError('WIRE_INTEGRITY_ERROR') from exc
    return record


def resolve(ref, kind, *, bundles=None):
    match = REFERENCE.fullmatch(ref) if isinstance(ref, str) else None
    if not match:
        raise WireError('INVALID_SHORT_REFERENCE')
    alias, key = match.groups()
    if bundles is not None and alias not in bundles:
        raise WireError('BUNDLE_NOT_DECLARED')
    record = load(alias)
    entry = record['entries'].get(key)
    if entry is None:
        raise WireError('NOT_DELIVERED')
    if entry['type'] != kind:
        raise WireError('REFERENCE_TYPE_MISMATCH')
    if kind == 'evidence':
        target = {'delivery_id': record['canonical_delivery_id'], 'evidence_id': entry['target']}
    elif kind == 'observation':
        target = {'delivery_id': record['canonical_delivery_id'], 'observation_id': entry['target']}
    else:
        target = {**copy.deepcopy(record.get('research_reference', {})), **copy.deepcopy(entry['target'])}
    return target, record, entry


def declaration(item):
    if item.get('wire_format') != FORMAT:
        raise WireError('UNSUPPORTED_WIRE_FORMAT', 'wire_format')
    aliases = item.get('bundle_refs')
    if not isinstance(aliases, list) or not aliases or any(not isinstance(a, str) for a in aliases):
        raise WireError('BUNDLE_REFS_REQUIRED', 'bundle_refs')
    records = [load(a) for a in dict.fromkeys(aliases)]
    return aliases, records


def normalize_judgment(item):
    if not isinstance(item, dict) or 'wire_format' not in item:
        if isinstance(item, dict) and 'wire_submission' in item:
            raise WireError('RESERVED_WIRE_FIELD', 'wire_submission')
        frozen_context = _FROZEN.get()
        required = bool(frozen_context['bundles']) if frozen_context is not None else enabled()
        if isinstance(item, dict) and required:
            raise WireError('WIRE_FORMAT_REQUIRED', 'wire_format')
        return item
    if 'wire_submission' in item:
        raise WireError('RESERVED_WIRE_FIELD', 'wire_submission')
    aliases, records = declaration(item)
    if 'delivery_ids' in item:
        raise WireError('MIXED_WIRE_FORMAT', 'delivery_ids')
    result = copy.deepcopy(item)
    result.pop('wire_format'); result.pop('bundle_refs')
    action_aliases = {'buy': '매수', 'sell': '매도', 'hold': '보유', 'watch': '관망'}
    for field in ('confidence_for', 'recommended_action'):
        if isinstance(result.get(field), str):
            result[field] = action_aliases.get(result[field], result[field])
    result['delivery_ids'] = list(dict.fromkeys(r['canonical_delivery_id'] for r in records))
    for i, arg in enumerate(result.get('arguments', [])):
        if not isinstance(arg, dict):
            raise WireError('SCHEMA_ERROR', f'arguments[{i}]')
        if arg.get('kind') in ('fact_claim', 'source_quote'):
            ref, _, entry = resolve(arg.get('evidence'), 'evidence', bundles=aliases)
            arg['evidence'] = ref
            if 'field' in arg and arg['field'] == entry.get('display_field'):
                source = source_row(records, ref['delivery_id'], ref['evidence_id'])
                arg['field'] = source['field']
            assertion = arg.get('assertion')
            if isinstance(assertion, dict) and 'right' in assertion:
                assertion['right'] = resolve(assertion['right'], 'evidence', bundles=aliases)[0]
    if result.get('comparison_context') is not None:
        result['comparison_context'] = resolve(result['comparison_context'], 'observation', bundles=aliases)[0]
    result['wire_submission'] = {'wire_format': FORMAT, 'original': copy.deepcopy(item),
        'original_hash': digest(item), 'normalized_hash': digest(result),
        'bundles': [{'bundle_alias': r['bundle_alias'], 'scope': r['scope'],
                     'alias_map_hash': r['alias_map_hash']} for r in records]}
    return result


def source_row(records, delivery_id, evidence_id):
    for record in records:
        if record['canonical_delivery_id'] == delivery_id:
            receipt = O.open_object(record['canonical_receipt_ref'])
            for row in receipt[record['catalog_key']]:
                if row.get('evidence_id') == evidence_id:
                    return row
    raise WireError('NOT_DELIVERED')


def check_visible(receipt, evidence, bundles=None):
    if receipt.get('wire_format') != FORMAT:
        return None
    records = [load(alias) for alias in bundles] if bundles else [load(receipt['bundle_alias'])]
    entries = [e for record in records if record['canonical_delivery_id'] == receipt['delivery_id']
               for e in record['entries'].values() if e['type'] == 'evidence' and e['target'] == evidence['evidence_id']]
    if not entries:
        raise WireError('NOT_DELIVERED')
    return {p for e in entries for p in e['visible_fields']}


def detail(ref, fields):
    match = REFERENCE.fullmatch(ref) if isinstance(ref, str) else None
    if not match or match[2].startswith('O'):
        raise WireError('DETAIL_REQUIRES_VALUE_REFERENCE')
    kind = 'metric' if match[2].startswith('M') else 'evidence'
    target, record, entry = resolve(ref, kind)
    receipt = O.open_object(record['canonical_receipt_ref'])
    rows = receipt[record['catalog_key']]
    source = next((r for r in rows if (r.get('reference') or {'metric_id': r.get('metric_id')}) == target), None) if kind == 'metric' else next((r for r in rows if r.get('evidence_id') == target['evidence_id']), None)
    if source is None:
        raise WireError('NOT_DELIVERED')
    replay = _FROZEN.get()
    if replay is not None:
        # Replay may return only an archived detail response, never open new data.
        for candidate in replay['details']:
            if candidate.get('detail_request') == {'ref': ref, 'fields': fields}:
                with frozen([*replay['bundles'].values(), candidate], replay['at']):
                    checked = load(candidate['bundle_alias'])
                    return O.open_object(checked['visible_payload_ref'])
        return {'status': 'unavailable', 'reason': 'detail_not_archived_at_original_time'}
    prepared = prepare([source], metric=kind == 'metric', fields=fields,
                       metadata={'detail_of': ref})
    # Detail events retain canonical identity but have their own visibility map.
    prepared['detail_request'] = {'ref': ref, 'fields': fields}
    return commit(prepared, receipt, catalog_key=record['catalog_key'], case_ids=record.get('case_ids'))


def archive_for_receipt(receipt):
    if receipt.get('wire_format') != FORMAT:
        return None
    return load(receipt['bundle_alias'], context=receipt['wire_scope'])


def freeze_receipt(receipt, cutoff=None):
    if receipt.get('wire_format') != FORMAT:
        return receipt
    bundle = archive_for_receipt(receipt)
    details = []
    for path in (root(receipt['wire_scope']) / 'bundles').glob('B*.json'):
        candidate = R.load(path)
        if (candidate.get('detail_request') and candidate['canonical_delivery_id'] == receipt['delivery_id']
                and (cutoff is None or datetime.fromisoformat(candidate['created_at']) <= cutoff)):
            details.append(candidate)
    return {**receipt, '_wire_bundle': bundle, '_wire_details': details}


def experimental_receipt(receipt, alias, delivery_id, catalog):
    """Render a new experimental delivery from structured, frozen source data."""
    import ontology
    result = copy.deepcopy(receipt)
    result.pop('_wire_bundle', None); result.pop('_wire_details', None)
    result.update(delivery_id=delivery_id, evidence_catalog=catalog, bundle_alias=alias,
                  original_delivery_id=receipt['delivery_id'], wire_format=FORMAT)
    result['context']['adopted_lessons'] = []
    payload, entries = render(alias, catalog, metadata=ontology.compact_briefing_metadata(
        result.get('briefing_snapshot') or {}, result.get('context')))
    output = '# 판단 자료\n' + json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    result.update(text=output, sha256=__import__('hashlib').sha256(output.encode()).hexdigest(),
                  experimental_transformation='prior_adopted_guidance_removed; frozen structured source rendered')
    bundle = {'wire_format': FORMAT, 'renderer_version': RENDERER, 'field_dictionary_version': DICTIONARY,
              'scope': {**result['wire_scope'], 'experiment_delivery_id': delivery_id}, 'bundle_alias': alias,
              'canonical_delivery_id': delivery_id, 'catalog_key': 'evidence_catalog', 'entries': entries,
              'alias_map_hash': digest(entries), 'canonical_receipt_ref': O.put_object(result, kind='experimental_receipt'),
              'visible_payload_ref': O.put_object(output, kind='experimental_display'), 'visible_payload_hash': digest(output),
              'created_at': receipt['created_at'], 'status': 'response_attempted', 'case_ids': []}
    result['_wire_bundle'] = bundle
    return result


@contextmanager
def frozen(bundles, at=None, details=()):
    by_alias = {}
    for bundle in bundles:
        alias = bundle['bundle_alias']
        if alias in by_alias and digest(by_alias[alias]) != digest(bundle):
            raise WireError('AMBIGUOUS_FROZEN_BUNDLE')
        by_alias[alias] = bundle
    token = _FROZEN.set({'bundles': by_alias, 'at': at, 'details': list(details)})
    try:
        yield
    finally:
        _FROZEN.reset(token)


@contextmanager
def frozen_receipts(receipts, at):
    if _FROZEN.get() is not None:
        yield
        return
    with frozen([r['_wire_bundle'] for r in receipts.values() if r.get('_wire_bundle')], at):
        yield


def concise(value):
    """AI summaries never echo internal snapshot trees. Operator reads stay raw."""
    blocked = {'evidence_catalog', 'catalog', 'metric_catalog', 'evidence_snapshots', 'evidence_snapshot',
               'metric_snapshot', 'raw_submission', 'original_submission', 'wire_submission',
               'briefing_snapshot', 'lesson_context', 'canonical_receipt_ref', 'entries'}
    if isinstance(value, dict):
        if 'delivery_id' in value and 'evidence_catalog' in value:
            blocked = blocked | {'text', 'payload', 'context', 'quotes', 'params', 'risk'}
        return {k: concise(v) for k, v in value.items() if k not in blocked}
    if isinstance(value, list):
        return [concise(v) for v in value]
    return value


def history_delivery(payload, source_id, source_time=None):
    """An AI historical read is a new delivery; operator views never call this."""
    import ontology
    did = 'history-' + uuid.uuid4().hex
    entry = {'evidence_id': did + ':summary', 'observation_id': did + ':history',
             'subject': 'history', 'field': 'historical_summary',
             'value': json.dumps(concise(payload), ensure_ascii=False, separators=(',', ':'), allow_nan=False),
             'kind': 'history', 'unit': 'text', 'status': 'reported_unverified',
             'source': 'archived:' + source_id, 'observed_at': source_time,
             'available_at': C.now().isoformat()}
    prepared = prepare([entry], metadata={'source_id': source_id, 'source_recorded_at': source_time,
        'note': '과거 기록의 이번 전달입니다. 과거 참조 번호를 현재 실행의 근거로 해석하지 않습니다.'})
    receipt = ontology._receipt(did, [entry], kind='history', source_id=source_id,
                                 payload=prepared['payload'], **receipt_fields(prepared))
    saved_path = ontology.archive('deliveries', did, receipt)
    return commit(prepared, R.load(saved_path))


def _metric_location(document, metric):
    """Resolve a registered path, never search for a similar numeric value."""
    parts = metric.get('path', '').strip('/').split('/')
    current = document
    if parts and parts[0] == 'cases' and len(parts) >= 3:
        cases = document.get('cases', [])
        if isinstance(document.get('item'), dict):
            cases = [*cases, document['item']]
        current = next(c for c in cases if c.get('application_id') == parts[1])
        parts = '/'.join(parts[2:]).split('.')
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    key = int(parts[-1]) if isinstance(current, list) else parts[-1]
    return current, key


def research_document(payload, catalog):
    document = copy.deepcopy(payload)
    for key in ('catalog', 'metric_catalog', 'source_refs', 'ai_submissions', 'ai_attempts', 'ai_results'):
        document.pop(key, None)
    document = concise(document)
    visible_catalog = []
    for metric in catalog:
        try:
            parent, key = _metric_location(document, metric)
            if type(parent[key]) is not type(metric['value']) or parent[key] != metric['value']:
                continue
        except (KeyError, IndexError, StopIteration, TypeError, ValueError):
            continue
        visible_catalog.append(metric)
    return document, visible_catalog


def prepare_research(payload, catalog):
    document, catalog = research_document(payload, catalog)
    prepared = prepare(catalog, metric=True)
    for i, metric in enumerate(catalog, 1):
        parent, key = _metric_location(document, metric)
        parent[key] = {'ref': prepared['bundle_alias'] + ':M' + str(i)}
    prepared['payload']['document'] = document
    prepared['payload'].pop('observations', None)  # research has no O references
    prepared['payload'].pop('metadata', None)
    prepared['payload']['citation_rule'] = 'M은 연구 지표. 분모·집단·기간·특징 의미를 유지하여 인용.'
    schema = document.get('schema')
    if isinstance(schema, dict):
        schema['wire_format'] = FORMAT
        schema['common_fields'] = [k for k in schema.get('common_fields', []) if k != 'delivery_ids'] + ['wire_format', 'bundle_refs']
        if 'fact_claim' in schema:
            schema['fact_claim']['reference'] = prepared['bundle_alias'] + ':M1'
    return prepared, catalog


def review_delivery(review_id, payload, *, cases=False):
    """MCP delivery boundary; case_review.get_case_review remains read-only."""
    import case_store as S
    import research_evidence as E
    result = S.load('results', review_id)
    if not result or payload.get('recording_status') not in ('recorded', None):
        return concise(payload)
    dataset = {'review_id': review_id, 'cases': payload.get('cases', []) if cases else []}
    # Review findings accept the review result's metrics. Case pages support
    # hypotheses by application_id; do not advertise proposal-only case metrics
    # which the review checker cannot accept.
    catalog = [] if cases else E.build_catalog(review_id, result, dataset)
    prepared, catalog = prepare_research(payload, catalog)
    ids = [c['application_id'] for c in payload.get('cases', []) if c.get('application_id')]
    if cases and payload.get('source_application_id'):
        ids.append(payload['source_application_id'])
    # Reserve before returning any original case material, even if no numeric
    # metric from that page can be cited.
    owner = prepared['scope']['run_attempt_id']
    E.reserve_usage(owner, ids, purpose='compact_review_delivery_reservation')
    receipt = {'delivery_id': 'review-delivery-' + uuid.uuid4().hex, 'review_id': review_id,
               'created_at': C.now().isoformat(), 'catalog': catalog, 'case_ids': ids,
               'body': prepared['payload'], **receipt_fields(prepared)}
    S.archive('deliveries', receipt['delivery_id'], receipt)
    E.record_usage(owner, ids, 'compact_review_delivered')
    return commit(prepared, S.load('deliveries', receipt['delivery_id']), catalog_key='catalog', case_ids=ids)


def normalize_findings(wire_format, bundle_refs, findings, hypotheses):
    aliases, records = declaration({'wire_format': wire_format, 'bundle_refs': bundle_refs})
    normalized = copy.deepcopy(findings)
    for item in normalized:
        for key in ('metric_id', 'right_metric_id'):
            if key in item:
                target = resolve(item[key], 'metric', bundles=aliases)[0]
                item[key] = target['metric_id']
    allowed_cases = {c for r in records for c in r.get('case_ids', [])}
    for item in hypotheses:
        if any(c not in allowed_cases for c in item.get('case_ids', [])):
            raise WireError('CASE_NOT_DELIVERED', 'hypotheses.case_ids')
    return normalized


def normalize_proposal(item):
    if not isinstance(item, dict) or 'wire_format' not in item:
        if enabled():
            raise WireError('WIRE_FORMAT_REQUIRED', 'wire_format')
        return item
    aliases, records = declaration(item)
    if 'delivery_ids' in item:
        raise WireError('MIXED_WIRE_FORMAT')
    result = copy.deepcopy(item)
    result.pop('wire_format'); result.pop('bundle_refs')
    result['delivery_ids'] = list(dict.fromkeys(r['canonical_delivery_id'] for r in records))
    for claim in result.get('fact_claims', []):
        for key in ('reference', 'right_reference'):
            if key in claim:
                claim[key] = resolve(claim[key], 'metric', bundles=aliases)[0]
    return result
