"""Freeze comparison context at acceptance; never reconstruct it from today's state."""
from __future__ import annotations

import copy
from datetime import datetime

import common as C
import case_store as S


def instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timezone required')
    return result


def record_regime(row):
    identifier = row.get('regime_event_id')
    if not identifier or row.get('kind') != 'regime':
        raise ValueError('A regime journal row with its original event ID is required')
    body = {'regime_event_id': identifier, 'regime': row['regime'],
            'recorded_at': C.now().isoformat(), 'source': 'set_regime',
            'classification_status': 'ai_classification_unverified', 'journal_snapshot': copy.deepcopy(row)}
    S.archive('regimes', identifier, body)
    return {**body, 'recording_status': 'recorded'}


def snapshot_regime():
    captured = C.now().isoformat()
    missing = {'status': 'unknown', 'value': None, 'captured_at': captured,
               'source': 'server_registered_regime', 'classification_status': 'ai_classification_unverified'}
    try:
        row = C.latest_regime()
        if not row:
            return {**missing, 'reason': 'no_registered_regime'}
        identifier = row.get('regime_event_id')
        stored = S.load('regimes', identifier) if identifier else None
        if not stored or stored.get('journal_snapshot') != row:
            return {**missing, 'reason': 'regime_event_unrecorded'}
        if instant(stored['recorded_at']) > instant(captured):
            return {**missing, 'reason': 'future_regime_event'}
        return {**missing, 'status': 'recorded', 'value': stored['regime'],
                'regime_event_id': identifier, 'registered_at': stored['recorded_at'],
                'event_snapshot': copy.deepcopy(stored)}
    except Exception as exc:
        # Metadata failure does not reject an otherwise valid judgment.
        return {**missing, 'reason': 'regime_recording_error', 'error': str(exc)}


def _catalog(receipt, code, observation_id):
    return [copy.deepcopy(x) for x in receipt.get('evidence_catalog', [])
            if x.get('subject') == code and x.get('observation_id') == observation_id]


def _semantic(catalog):
    # The same persisted selection can be displayed in two deliveries. It is
    # still one observation only when all displayed measurement semantics agree.
    return [{k: v for k, v in x.items() if k not in ('evidence_id',)}
            for x in sorted(catalog, key=lambda x: (x.get('field', ''), x.get('evidence_id', '')))]


def comparison_snapshot(code, receipts, declared=None):
    by_id = {r['delivery_id']: r for r in receipts}
    if declared is not None:
        if not isinstance(declared, dict) or set(declared) != {'delivery_id', 'observation_id'}:
            raise ValueError('comparison_context requires exactly delivery_id and observation_id')
        receipt = by_id.get(declared['delivery_id'])
        catalog = _catalog(receipt or {}, code, declared['observation_id'])
        if not receipt or not catalog:
            raise ValueError('Comparison observation was not delivered for this instrument')
        return {'status': 'recorded', 'selection': 'explicit', **copy.deepcopy(declared),
                'catalog': catalog, 'delivery_created_at': receipt.get('created_at')}
    choices = {}
    for receipt in receipts:
        identifiers = {e.get('observation_id') for e in receipt.get('evidence_catalog', [])
                       if e.get('subject') == code and e.get('field') in ('ma20', 'price_comparison.gap')}
        for identifier in identifiers - {None}:
            catalog = _catalog(receipt, code, identifier)
            value = {'delivery_id': receipt['delivery_id'], 'observation_id': identifier,
                     'catalog': catalog, 'delivery_created_at': receipt.get('created_at')}
            prior = choices.get(identifier)
            if prior and S.digest(_semantic(prior['catalog'])) != S.digest(_semantic(catalog)):
                return {'status': 'unknown', 'reason': 'conflicting_deliveries_for_observation',
                        'observation_ids': sorted(choices | {identifier: value})}
            choices.setdefault(identifier, value)
    if len(choices) != 1:
        return {'status': 'unknown', 'reason': 'ambiguous_observation' if choices else 'comparison_observation_unrecorded',
                'observation_ids': sorted(choices)}
    return {'status': 'recorded', 'selection': 'unique_delivered_observation', **next(iter(choices.values()))}
