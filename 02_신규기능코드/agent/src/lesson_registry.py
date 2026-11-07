"""Read-only consumer of manually activated, validation-bound lessons.

Neither candidate JSON nor an ``approved`` flag activates a lesson. The backend
resolves the atomic params pointer and its committed validation chain. Archived
briefings validate their frozen receipts, never today's revocations or text.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime

import common as C

VERSION = 'ADOPTED_LESSON@v1'
PROOF_VERSION = 'LESSON_ADOPTION_PROOF@v1'
FIELDS = {
    'features.gap':'ratio', 'features.slope':'ratio', 'features.foreign_ratio':'ratio',
    'features.institution_ratio':'ratio', 'features.regime':'text',
    'signed_flows.foreign':'KRW', 'signed_flows.institution':'KRW',
    'signed_flows.foreign_direction':'text', 'signed_flows.institution_direction':'text',
    'signed_flows.same_period_confirmed':'bool', 'stratum.price_kind':'text',
    'stratum.flow_method':'text', 'stratum.flow_comparability':'text',
    'stratum.admission_route':'text', 'stratum.instrument_kind':'text',
    'stratum.confidence_for':'text', 'adoption.explicitly_uses_above_ma20_and_foreign_buy':'bool',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                           separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def instant(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if value.tzinfo is None:
        raise ValueError('timezone_required')
    return value.astimezone(C.KST)


def _adoption_backend():
    """No active pointer retains the legacy unavailable result; never a write."""
    import policy_registry
    return policy_registry.lesson_backend()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _check_lesson(lesson, proof, at):
    if not isinstance(lesson, dict) or lesson.get('schema_version') != VERSION:
        raise ValueError('unsupported_lesson_schema')
    for key in ('lesson_version_id','proposal_id','text'):
        if not _text(lesson.get(key)):
            raise ValueError('missing_' + key)
    # Match learning_proposals.version exactly; strings and bools cannot select
    # a different representation of an immutable proposal version.
    if type(lesson.get('proposal_version')) is not int or lesson['proposal_version'] < 1:
        raise ValueError('invalid_proposal_version')
    for key in ('source_case_ids','source_refs'):
        if not isinstance(lesson.get(key), list) or not lesson[key]:
            raise ValueError('missing_' + key)
    if any(not _text(x) for x in lesson['source_case_ids']):
        raise ValueError('invalid_source_cases')
    if any(not isinstance(r, dict) or not all(_text(r.get(k)) for k in ('review_id','result_hash')) for r in lesson['source_refs']):
        raise ValueError('invalid_source_refs')
    start, end = instant(lesson['effective_from']), instant(lesson['effective_until'])
    if not start < end or not start <= at < end:
        raise ValueError('outside_effective_period')
    base = lesson.get('base_ref')
    if not isinstance(base, dict) or not all(_text(base.get(k)) for k in ('definition_hash','settings_hash')):
        raise ValueError('base_version_missing')
    if not isinstance(proof, dict) or proof.get('schema_version') != PROOF_VERSION or proof.get('status') != 'verified':
        raise ValueError('adoption_not_verified')
    for key in ('verifier_id','validation_id','adoption_id'):
        if not _text(proof.get(key)):
            raise ValueError('adoption_proof_incomplete')
    for key in ('lesson_version_id','proposal_id','proposal_version'):
        if proof.get(key) != lesson[key]:
            raise ValueError('adoption_identity_mismatch')
    if proof.get('lesson_hash') != digest(lesson):
        raise ValueError('lesson_version_hash_mismatch')
    if proof.get('base_ref') != base or proof.get('current_base_ref') != base or proof.get('base_status') != 'current':
        raise ValueError('stale_base')
    adopted_at, verified_at = instant(proof['adopted_at']), instant(proof['verified_at'])
    if adopted_at > at or verified_at > at:
        raise ValueError('future_adoption')
    if verified_at < adopted_at:
        raise ValueError('verification_before_adoption')
    if proof.get('revoked_at') and instant(proof['revoked_at']) <= at:
        raise ValueError('revoked')
    scope = lesson.get('scope')
    if not isinstance(scope, dict) or set(scope) - {'codes','regimes','price_kinds','flow_methods','admission_routes','instrument_kinds','confidence_for'}:
        raise ValueError('unsupported_scope')
    for key in ('codes','confidence_for'):
        if not isinstance(scope.get(key), list) or not scope[key]:
            raise ValueError('scope_required:' + key)
    for key, values in scope.items():
        if not isinstance(values, list) or not values or any(not _text(x) for x in values):
            raise ValueError('invalid_scope:' + key)
    if any(x not in ('매수','매도') for x in scope['confidence_for']):
        raise ValueError('invalid_confidence_direction')
    if isinstance(lesson.get('priority'), bool) or not isinstance(lesson.get('priority'), int):
        raise ValueError('explicit_priority_required')


def _measurement(ob, field, at):
    measurement = (ob.get('measurements') or {}).get(field) or {}
    value = measurement.get('value')
    unit='ratio' if field=='ma20_slope5' else 'KRW'
    if measurement.get('status')!='observed' or measurement.get('unit')!=unit or isinstance(value, bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
        return None
    try:
        if measurement.get('available_at') and instant(measurement['available_at']) > at:
            return None
    except (ValueError,TypeError):
        return None
    return value


def _inputs(row, regime, side, at):
    ob = (row.get('_ontology') or {}).get('observation') or {}
    measures = ob.get('measurements') or {}
    price, ma20 = _measurement(ob,'price',at), _measurement(ob,'ma20',at)
    foreign, institution, denominator = (_measurement(ob,k,at) for k in ('foreign','institution','trading_value_20d'))
    def direction(value): return None if value is None else 'buy' if value > 0 else 'sell' if value < 0 else 'zero'
    flow_method = measures.get('foreign',{}).get('kind')
    if flow_method != measures.get('institution',{}).get('kind'): flow_method = None
    aligned = (ob.get('flow_summary') or {}).get('same_period_confirmed')
    route = (row.get('_ontology') or {}).get('admission',{}).get('route')
    values = {'features.gap':price/ma20-1 if price is not None and ma20 is not None and ma20 > 0 else None,
              'features.slope':_measurement(ob,'ma20_slope5',at),
              'features.foreign_ratio':foreign/denominator if foreign is not None and denominator and denominator>0 else None,
              'features.institution_ratio':institution/denominator if institution is not None and denominator and denominator>0 else None,
              'features.regime':regime.get('value') if regime and regime.get('status')=='recorded' else None,
              'signed_flows.foreign':foreign, 'signed_flows.institution':institution,
              'signed_flows.foreign_direction':direction(foreign), 'signed_flows.institution_direction':direction(institution),
              'signed_flows.same_period_confirmed':aligned,
              'stratum.price_kind':measures.get('price',{}).get('kind'), 'stratum.flow_method':flow_method,
              'stratum.flow_comparability':'aligned' if aligned is True else 'received_values_only' if aligned is False else None,
              'stratum.admission_route':route, 'stratum.instrument_kind':'etf' if row.get('index') or row.get('inverse') else 'equity',
              'stratum.confidence_for':side,
              # AI adoption has not happened at briefing time. Never infer it
              # merely from a condition being present or true.
              'adoption.explicitly_uses_above_ma20_and_foreign_buy':None}
    return {'values':values,'observation_id':ob.get('observation_id'), 'regime_snapshot':copy.deepcopy(regime),
            'selection_observation_hash':digest(ob) if ob else None}


def _condition(expression, values, depth=0):
    import condition_contract as CC
    # Preserve historical receipt interpretation. New publications reject
    # post-judgment fields; legacy receipts keep these values explicitly unknown.
    try:
        unit = CC.validate_condition(expression,FIELDS,depth=depth)
        return CC.evaluate_condition(expression,values,FIELDS),unit
    except ValueError as exc:
        if str(exc) == 'FORBIDDEN_CONDITION_FIELD': raise ValueError('unsupported_field') from exc
        raise


def _applicability(lesson, row, regime, side, at):
    inputs = _inputs(row,regime,side,at)
    scope, reasons = lesson['scope'], []
    mapping={'regimes':'features.regime','price_kinds':'stratum.price_kind','flow_methods':'stratum.flow_method',
             'admission_routes':'stratum.admission_route','instrument_kinds':'stratum.instrument_kind'}
    unknown=False
    if '*' not in scope['codes'] and row['code'] not in scope['codes']: reasons.append('code_outside_scope')
    if side not in scope['confidence_for']: reasons.append('direction_outside_scope')
    for key, field in mapping.items():
        if key not in scope: continue
        actual=inputs['values'][field]
        if actual is None: unknown=True; reasons.append('unknown:'+field)
        elif actual not in scope[key]: reasons.append('outside:'+field)
    if lesson.get('condition') is not None:
        matched,unit=_condition(lesson['condition'],inputs['values'])
        if unit!='bool': raise ValueError('condition_must_return_bool')
        if matched is None: unknown=True; reasons.append('condition_unknown')
        elif not matched: reasons.append('condition_false')
    status='unknown' if unknown else 'not_applicable' if reasons else 'applicable'
    return {'status':status,'matches':True if status=='applicable' else False if status=='not_applicable' else None,
            'code':row['code'],'confidence_for':side,'evaluated_at':at.isoformat(),
            'inputs':inputs,'reasons':reasons,'scope':copy.deepcopy(scope),'condition':copy.deepcopy(lesson.get('condition'))}


def select_for_briefing(briefing, at=None):
    at=instant(at or C.now())
    backend=_adoption_backend()
    if backend is None:
        return {'lessons':[],'excluded':[],'recording_status':'not_applicable','status':'step7_verifier_unavailable'}
    included,excluded=[],[]
    verification_error=False
    try:
        result=backend.list_lessons(at.isoformat())
        limit=result.get('display_limit')
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=50:
            raise ValueError('explicit_validated_display_limit_required')
        import judgment_context
        regime=judgment_context.snapshot_regime()
        if regime.get('status')=='recorded' and instant(regime['registered_at'])>at:
            regime={**regime,'status':'unknown','value':None,'reason':'future_regime_event'}
        rows={}
        ambiguous=set()
        for row in briefing.get('candidates',[])+briefing.get('holdings',[]):
            code=row['code']
            if code in rows and digest(rows[code])!=digest(row): ambiguous.add(code)
            rows.setdefault(code,row)
        lessons=result['lessons']
        if not isinstance(lessons,list): raise ValueError('invalid_verified_lesson_list')
        # A conflicting repeated identity cannot be resolved by backend list
        # order. Omit every copy instead of keeping whichever arrived first.
        counts={}
        for lesson in lessons:
            identifier=lesson.get('lesson_version_id') if isinstance(lesson,dict) else None
            if isinstance(identifier,str): counts[identifier]=counts.get(identifier,0)+1
        for lesson in lessons:
            identifier=lesson.get('lesson_version_id') if isinstance(lesson,dict) else None
            try:
                if isinstance(identifier,str) and counts[identifier]>1:
                    raise ValueError('duplicate_lesson_version')
                proof=backend.verify_adoption(copy.deepcopy(lesson),at.isoformat())
                _check_lesson(lesson,proof,at)
                for code,row in sorted(rows.items()):
                    if code in ambiguous:
                        excluded.append({'lesson_version_id':identifier,'code':code,'reason':'ambiguous_briefing_observation'}); continue
                    for side in lesson['scope']['confidence_for']:
                        application=_applicability(lesson,row,regime,side,at)
                        if application['status']!='applicable':
                            excluded.append({'lesson_version_id':identifier,**application}); continue
                        included.append({'kind':'adopted_lesson','code':code,'confidence_for':side,'text':lesson['text'],
                            'lesson_version_id':identifier,'proposal_id':lesson['proposal_id'],'proposal_version':lesson['proposal_version'],
                            'adoption_id':proof['adoption_id'],'validation_id':proof['validation_id'],'verifier_id':proof['verifier_id'],
                            'source_case_ids':copy.deepcopy(lesson['source_case_ids']),'source_refs':copy.deepcopy(lesson['source_refs']),
                            'effective_from':lesson['effective_from'],'effective_until':lesson['effective_until'],
                            'base_ref':copy.deepcopy(lesson['base_ref']),'applicability':application,
                            'lesson_snapshot':copy.deepcopy(lesson),'adoption_proof':copy.deepcopy(proof),
                            'selected_at':at.isoformat(),'market_truth':'not_established'})
            except Exception as exc:
                verification_error |= not isinstance(exc,(ValueError,KeyError,TypeError))
                excluded.append({'lesson_version_id':identifier,'reason':str(exc)})
        included.sort(key=lambda x:(-x['lesson_snapshot']['priority'],x['lesson_version_id'],x['code'],x['confidence_for']))
        excluded.extend({'lesson_version_id':x['lesson_version_id'],'code':x['code'],'reason':'display_limit'} for x in included[limit:])
        return {'lessons':included[:limit],'excluded':excluded,
                'recording_status':'recording_error' if verification_error else 'recorded',
                'status':'partial' if verification_error else 'selected',
                **({k:result.get(k) for k in ('policy_status','policy_revision')} if 'policy_status' in result else {})}
    except Exception as exc:
        return {'lessons':[],'excluded':excluded,'recording_status':'recording_error','status':'unavailable','error':str(exc)}


def validate_frozen_lesson(row, code, delivered_at, confidence_for=None):
    """Check only the originally delivered scope; a later revocation is not time travel."""
    if row.get('kind')!='adopted_lesson' or row.get('code')!=code:
        raise ValueError('lesson_subject_mismatch')
    at=instant(row['selected_at']); delivery=instant(delivered_at)
    if at>delivery: raise ValueError('lesson_selection_after_delivery')
    _check_lesson(row['lesson_snapshot'],row['adoption_proof'],at)
    for key in ('lesson_version_id','proposal_id','proposal_version','text','base_ref','source_case_ids','source_refs','effective_from','effective_until'):
        if row.get(key)!=row['lesson_snapshot'].get(key): raise ValueError('lesson_snapshot_mismatch')
    for key in ('adoption_id','validation_id','verifier_id'):
        if row.get(key)!=row['adoption_proof'].get(key): raise ValueError('lesson_proof_mismatch')
    application=row.get('applicability') or {}
    if (application.get('status')!='applicable' or application.get('matches') is not True or application.get('code')!=code or
        application.get('confidence_for')!=row.get('confidence_for') or
        application.get('evaluated_at')!=row['selected_at']): raise ValueError('lesson_not_applicable')
    if confidence_for is not None and confidence_for!=row['confidence_for']: raise ValueError('lesson_direction_mismatch')
    if not instant(row['effective_from'])<=delivery<instant(row['effective_until']): raise ValueError('lesson_expired_at_delivery')
    if application.get('scope')!=row['lesson_snapshot']['scope'] or application.get('condition')!=row['lesson_snapshot'].get('condition'):
        raise ValueError('lesson_scope_mismatch')
    scope=row['lesson_snapshot']['scope']
    if ('*' not in scope['codes'] and code not in scope['codes']) or row['confidence_for'] not in scope['confidence_for']:
        raise ValueError('lesson_scope_not_matched')
    values=application.get('inputs',{}).get('values',{})
    if values.get('stratum.confidence_for')!=row['confidence_for']:
        raise ValueError('lesson_direction_input_mismatch')
    for key,field in {'regimes':'features.regime','price_kinds':'stratum.price_kind','flow_methods':'stratum.flow_method',
                      'admission_routes':'stratum.admission_route','instrument_kinds':'stratum.instrument_kind'}.items():
        if key in scope and values.get(field) not in scope[key]:
            raise ValueError('lesson_scope_input_mismatch')
    if row['lesson_snapshot'].get('condition') is not None:
        value,unit=_condition(row['lesson_snapshot']['condition'],application['inputs']['values'])
        if value is not True or unit!='bool': raise ValueError('lesson_condition_not_confirmed')
    return True


def render_for_briefing(selection):
    lines=[]
    for row in selection.get('lessons',[]):
        validate_frozen_lesson(row,row['code'],row['selected_at'])
        lines.append(f"- {row['code']} {row['confidence_for']} · {row['text']} "
                     f"[교훈 {row['lesson_version_id']}; 채택 {row['adoption_id']}; 검증 범위 내 판단 지침, 시장 사실·인과의 확정 아님]")
    return (['','## 검증 후 채택된 판단 지침',*lines] if lines else [])
