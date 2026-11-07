"""Candidate evidence, separate from trading policy. Immutable snapshots use only local files."""
import copy
import hashlib
import json
import math
import os
import tempfile
import uuid
from pathlib import Path
from functools import wraps

import common as C
from ontology_adapter import load_definition_bundle, evaluate, digest


def stamp():
    return C.now().isoformat(timespec='microseconds')


def clean(value):
    """Strict JSON snapshots; pandas missing values must never become facts."""
    if isinstance(value, dict): return {k: clean(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)): return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value): return None
    if hasattr(value, 'item'): return clean(value.item())
    return value


def measurement(value, unit, source, *, observed_at=None, available_at=None, status=None, **meta):
    value=clean(value)
    return {'value':value,'unit':unit,'source':source,'observed_at':observed_at,
            'available_at':available_at,'status':status or ('observed' if value is not None else 'unknown'), **meta}


def raw_quantity(raw, parser, received_at):
    """Record missingness before legacy num()/or-zero conversion; retain that conversion separately."""
    valid=lambda v: v is not None and str(v).strip() not in ('','+','-')
    net=raw.get('netslmt')
    effective=parser(net or (parser(raw.get('buy_qty'))-parser(raw.get('sel_qty'))))
    known=valid(net) or valid(raw.get('buy_qty')) and valid(raw.get('sel_qty'))
    observed = parser(net) if valid(net) else parser(raw.get('buy_qty'))-parser(raw.get('sel_qty')) if known else None
    return measurement(observed, 'share', 'kiwoom:ka10065', available_at=received_at,
                       provider_time_status='not_provided', status='observed' if known else 'unknown',
                       reason=None if known else 'missing_quantity', effective_value=effective,
                       raw={k:raw.get(k) for k in ('netslmt','buy_qty','sel_qty')})


def price_comparison(px, ma20):
    if px is None or ma20 is None or ma20 <= 0 or not all(math.isfinite(v) for v in (px,ma20)):
        return {'above':None,'gap':None,'price':clean(px),'ma20':clean(ma20)}
    return {'above':bool(px > ma20),'gap':px/ma20-1,'price':px,'ma20':ma20}


def guarded(fn):
    @wraps(fn)
    def wrap(self,*args,**kwargs):
        try: return fn(self,*args,**kwargs)
        except Exception as exc:  # Evidence must not silently change the candidate engine's decisions.
            self.errors.append(f'{fn.__name__}: {type(exc).__name__}: {exc}')
            return None
    return wrap


class SelectionTrace:
    def __init__(self, kind, params, regime=None, regime_source=None, risk=None, **context):
        self.id=f'{kind}-{uuid.uuid4().hex}'
        self.kind=kind
        self.errors=[]
        try: self.bundle=load_definition_bundle()
        except Exception as exc:
            self.bundle=None; self.errors.append(f'definition: {exc}')
        self.records={}
        self.snapshot={'run_id':self.id,'kind':kind,'created_at':stamp(),'params':copy.deepcopy(params),
                       'risk':copy.deepcopy(risk),'regime':regime,'regime_source':regime_source,
                       'context':context,'definitions':self.bundle,'observations':[], 'rules':[]}
        try:
            from audit_inputs import runtime_context
            self.snapshot['runtime_context'] = runtime_context()
        except Exception as exc:
            self.errors.append(f'runtime_context: {exc}')

    def rule_version(self, name):
        return next((r['rule_version_id'] for r in (self.bundle or {}).get('bindings', {}).get('rules', [])
                     if r['rule_version_id'].split('@')[0] == name), name+'@unavailable')

    @guarded
    def screen_input(self, rows, login=None):
        self.snapshot['input_manifest'] = {'complete':True, 'ordered_codes':[r['code'] for r in rows],
                                           'login':login, 'screen_result':[]}

    @guarded
    def screen_output(self, rows):
        self.snapshot['input_manifest']['screen_result'] = [{k:r.get(k) for k in ('code','score','high_vol')} for r in rows]

    @guarded
    def begin(self, row, px, daily_only=False):
        code=row['code']; oid=f'{self.id}:{code}'
        sources=copy.deepcopy(row.get('_sources') or {})
        daily=sources.get('daily') or {}
        daily_source=daily.get('source') or 'pykrx:ohlcv'
        daily_meta={k:daily[k] for k in ('price_basis','provider_version','provider_finality','request_parameters') if k in daily}
        is_live=self.kind == 'intraday' and not daily_only
        pm=sources.get('price') if is_live and row.get('price') else None
        price_kind='intraday_current' if is_live and row.get('price') else 'close_fallback' if is_live else 'daily_close'
        pm=pm or measurement(px,'KRW',daily_source,observed_at=daily.get('asof'),available_at=daily.get('available_at'),**daily_meta)
        pm={**pm,'value':px,'kind':price_kind}
        measures={'price':pm}
        for key in ('close','ma5','ma20','ma20_slope5','trading_value_20d'):
            measures[key]=measurement(row.get(key),'ratio' if key=='ma20_slope5' else 'KRW',daily_source,
                                      observed_at=daily.get('asof'),available_at=daily.get('available_at'),
                                      **daily_meta,
                                      calculation={'close':'last','ma5':'GS Quant MA5 rounded','ma20':'GS Quant MA20 rounded',
                                                   'ma20_slope5':'raw MA20[-1]/MA20[-6]-1 rounded(4)',
                                                   'trading_value_20d':'mean(last 20 trading values)'}[key])
        for actor, key in [('foreign','foreign_net'),('institution','inst_net')]:
            if is_live:
                sk='foreign_qty' if actor=='foreign' else 'inst_qty'
                qm=sources.get(sk) or measurement(row.get(sk),'share','legacy_input',status='observed' if row.get(sk) is not None else 'unknown',reason='source_metadata_unrecorded')
                known=qm.get('value') is not None
                amount=qm['value']*px if known else None
                effective=(row.get(sk) or 0)*px
                measures[actor]={**qm,'value':amount,'unit':'KRW','kind':'intraday_estimate',
                                 'quantity':copy.deepcopy(qm),'price_ref':f'{oid}.price',
                                 'effective_value':effective,'substituted':not known or effective != amount}
            else:
                fm=(sources.get('flow') or {}).get(actor) or measurement(row.get(key),'KRW','legacy_input',reason='source_metadata_unrecorded')
                measures[actor]={**fm,'kind':'daily_accumulated','effective_value':row.get(key),
                                 'substituted':fm.get('value') is None and row.get(key) is not None}
        rec={'observation_id':oid,'instrument_id':code,'instrument':{'code':code,'name':row.get('name'),'market':C.market_of(code)},
             'run_id':self.id,'created_at':stamp(),'measurements':measures,'source_snapshot':sources,
             'engine_input':{k:copy.deepcopy(v) for k,v in row.items() if k not in ('_ontology','_sources')},
             'criteria':[],'rules':[],'comparison_id':f'{oid}:price-ma20','price_comparison':price_comparison(px,row.get('ma20'))}
        self.records[code]=rec
        values={k:v['value'] for k,v in measures.items()}
        self.criterion(code,'PRICE_ABOVE_MA20',values,'briefing_reference')
        self.criterion(code,'MA20_GAP',values,'briefing_reference')
        for key in ('FOREIGN_NET_BUY','INSTITUTION_NET_BUY','INSTITUTION_NET_SELL'):
            self.criterion(code,key,values,'briefing_reference')
        f,i=measures['foreign'],measures['institution']
        same_period=(f.get('period') and f.get('period')==i.get('period')) if not is_live else (
            f.get('observed_at') and f.get('observed_at')==i.get('observed_at'))
        both= f['value'] is not None and i['value'] is not None
        rec['flow_summary']={'directions_opposed':bool(f['value']*i['value']<0) if both else None,
                             'sum':f['value']+i['value'] if both and same_period else None,
                             'available_snapshot_sum':f['value']+i['value'] if both else None,
                             'same_period_confirmed':bool(same_period),
                             'reason':None if same_period else 'provider_times_not_aligned_or_unprovided'}
        self.criterion(code,'FLOW_SUM',values,'briefing_reference', unknown=not (both and same_period))
        bars=daily.get('cross_bars') or []
        cv={**values,'price':row.get('close')}
        if len(bars)==2:
            cv.update(price=bars[-1]['close'],ma20=bars[-1]['ma20'],previous_close=bars[0]['close'],previous_ma20=bars[0]['ma20'])
        self.criterion(code,'CLOSE_CROSS_MA20',cv,'briefing_reference',unknown=len(bars)!=2)
        rec['cross_period']=[b['date'] for b in bars]
        rec['today_close_cross']='unconfirmed' if is_live else 'not_evaluated'
        return rec

    def criterion(self, code, cid, values, use, *, result=None, actual=False, unknown=False):
        rec=self.records[code]
        existing=next((e for e in rec['criteria'] if e['criterion_id']==cid and e['use']==use),None)
        if existing: return existing['criterion_evaluation_id']
        reason=None
        reads={}
        try:
            if unknown or not self.bundle: raise ValueError('Input or definition unconfirmed')
            computed=evaluate(self.bundle,cid,values,reads)
            if cid in ('PRICE_ABOVE_MA20','MA20_GAP') and values.get('price') == rec['price_comparison']['price'] and values.get('ma20') == rec['price_comparison']['ma20']:
                shared=rec['price_comparison']['above' if cid=='PRICE_ABOVE_MA20' else 'gap']
                if shared is None: raise ValueError('Invalid price/MA20 input')
                if shared != computed: raise ValueError('Definition does not match shared price comparison')
                computed=shared  # engine and briefing consume the shared comparison; IR check is validation only
            if actual and computed != result: raise ValueError('Definition does not match executed engine result')
            value=result if actual else computed
            status=('satisfied' if value else 'unsatisfied') if type(value) is bool else 'calculated'
        except (ValueError,TypeError,ZeroDivisionError,KeyError) as exc:
            value=None;status='unknown';reason=str(exc)
            if 'Definition does not match' in reason:
                self.errors.append(f'{cid}: {reason}')
        eid=f"{rec['observation_id']}:{cid}:{use}"
        refs={key:f"{rec['observation_id']}.measurements.{key}.value" for key in reads}
        if cid == 'CLOSE_CROSS_MA20':
            refs={key:f"{rec['observation_id']}.source_snapshot.daily.cross_bars.{index}.{field}"
                  for key,index,field in [('price',1,'close'),('ma20',1,'ma20'),('previous_close',0,'close'),('previous_ma20',0,'ma20')] if key in reads}
        elif use in ('candidate_filter','ranking'):
            for key in ('foreign','institution'):
                if key in reads: refs[key]=f"{rec['observation_id']}.measurements.{key}.effective_value"
        version=next((c['criterion_version_id'] for c in (self.bundle or {}).get('bindings',{}).get('criteria',[])
                      if c['criterion_id']==cid), cid+'@unavailable')
        rec['criteria'].append({'criterion_evaluation_id':eid,'criterion_id':cid,'criterion_version_id':version,
                                'observation_id':rec['observation_id'],'comparison_id':rec['comparison_id'] if cid in ('PRICE_ABOVE_MA20','MA20_GAP') else None,
                                'evaluated_at':stamp(),'use':use,'status':status,'value':value,'inputs':clean(reads),
                                'input_refs':refs,'reason':reason})
        return eid

    @guarded
    def gate(self, code, name, passed, inputs, criterion_ids=()):
        rec=self.records[code]
        rule='TREND' if name=='trend' else 'FLOW' if name=='flow' else 'ADMISSION'
        rec['rules'].append({'rule_evaluation_id':f"{rec['observation_id']}:{name}",'rule_version_id':self.rule_version(rule),
                             'name':name,'use':'candidate_filter','status':'passed' if passed else 'rejected',
                             'evaluated_at':stamp(),'inputs':clean(inputs),'criterion_evaluation_ids':list(criterion_ids),
                             'regime':self.snapshot['regime'],'regime_source':self.snapshot['regime_source']})

    @guarded
    def trend(self, code, evaluation):
        rec=self.records[code]
        values={k:v['value'] for k,v in rec['measurements'].items()}
        ids=[self.criterion(code,c['criterion_id'],values,'candidate_filter',result=c['result'],actual=True) for c in evaluation['comparisons']]
        self.gate(code,'trend',evaluation['passed'],evaluation,ids)

    @guarded
    def flow(self, code, enabled, f, i, ratio=None):
        rec=self.records[code]
        ids=[]
        if enabled:
            # The original condition reads institution only when foreign <= 0.
            values={'foreign':f,'institution':i}
            ids.append(self.criterion(code,'FOREIGN_NET_BUY',values,'candidate_filter',result=bool(f>0),actual=True))
            if f<=0: ids.append(self.criterion(code,'INSTITUTION_NET_BUY',values,'candidate_filter',result=bool(i>0),actual=True))
        inputs={'enabled':enabled,'effective_foreign':f,'effective_institution':i,
                'foreign_substituted':rec['measurements']['foreign']['substituted'],
                'institution_substituted':rec['measurements']['institution']['substituted']}
        self.gate(code,'flow',not enabled or not (f<=0 and i<=0),inputs,ids)
        if not enabled: rec['rules'][-1]['status']='not_applied'
        if ratio is not None: rec['flow_ratio']=ratio

    @guarded
    def ranked(self, row, gap, ratio, components):
        rec=self.records[row['code']]
        values={k:v['value'] for k,v in rec['measurements'].items()}
        ids=[self.criterion(row['code'],'MA20_GAP',values,'ranking',result=gap,actual=True)]
        if ratio is not None:
            values.update(foreign=rec['measurements']['foreign']['effective_value'],institution=rec['measurements']['institution']['effective_value'])
            ids.append(self.criterion(row['code'],'POSITIVE_FLOW_RATIO',values,'ranking'))
        rec['flow_ratio']=ratio
        rec['rules'].append({'rule_evaluation_id':f"{rec['observation_id']}:rank",'rule_version_id':self.rule_version('RANK'),'use':'ranking',
                             'inputs':clean(components),'criterion_evaluation_ids':ids,'score':row['score'],
                             'gap':gap,'rounded_flow_ratio':ratio})

    @guarded
    def attach(self, candidates, route=None):
        for position,row in enumerate(candidates,1):
            rec=self.records.get(row['code'])
            if rec is None: continue
            path=route or ('high_vol' if row.get('high_vol') else 'general')
            row['_ontology']={'observation':copy.deepcopy(rec),'admission':{'run_id':self.id,'route':path,'position':position},
                              'definitions_hash':self.bundle['hash'] if self.bundle else None}

    @guarded
    def finish(self, b, **context):
        # Retained candidates carry the original observations, never fabricated new filter passes.
        self.snapshot.update(context)
        self.snapshot['observations']=list(self.records.values())
        self.snapshot['candidates']=[{'code':r['code'],'score':r.get('score'),'final_position':i,'evidence':r.get('_ontology')} for i,r in enumerate(b['candidates'],1)]
        self.snapshot['errors']=self.errors
        if self.snapshot.get('input_manifest'):
            self.snapshot['input_manifest']['final_result'] = [{'code':r['code'],'score':r.get('score'),
                **{k:bool(r.get(k)) for k in ('high_vol','dip','from_prep','inverse','index')},
                'admission_route':((r.get('_ontology') or {}).get('admission') or {}).get('route')}
                for r in b['candidates']]
        import execution_trace as E
        names=('common.py','data_prep.py' if self.kind=='prep' else 'intraday.py')
        captured={name:E._runtime_source(name[:-3],Path(__file__).with_name(name)) for name in names}
        self.snapshot['engine_sources']={name:value[0] for name,value in captured.items()}
        self.snapshot['engine_sources'].update({name:Path(__file__).with_name(name).read_text(encoding='utf-8')
                                               for name in ('ontology.py','ontology_adapter.py')})
        identities={name:value[1] for name,value in captured.items()}
        self.snapshot['source_identity']={'version':'LOADED_ENGINE_SOURCE@v1','modules':identities,
            'status':'verified_loaded_source' if all(v['status']=='verified_loaded_source' for v in identities.values()) else 'unverified_loaded_source'}
        info={'selection_run_id':self.id,'status':'incomplete' if self.errors else 'recorded','errors':list(self.errors)}
        b['ontology']=info
        try:
            archive('selections',self.id,self.snapshot)
        except Exception as exc:
            info.update(status='unrecorded',errors=[*self.errors,f'archive: {exc}'])


def archive(kind, identifier, value):
    folder=Path(C.WORK)/'ontology'/kind
    folder.mkdir(parents=True,exist_ok=True)
    # Exclusive create: an existing immutable record is never replaced.
    path=folder/(identifier+'.json')
    body=json.dumps(clean(value),ensure_ascii=False,indent=2,allow_nan=False)
    # Publish the fully written file once; concurrent readers never see partial JSON.
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.link(temporary, path)  # exclusive: FileExistsError preserves the prior receipt
    finally:
        os.unlink(temporary)
    return path


def mark_route(row, route, run_id=None):
    if not row.get('_ontology'): return
    row['_ontology']=copy.deepcopy(row['_ontology'])
    old=row['_ontology']['admission']
    row['_ontology']['admission']={**old,'origin':copy.deepcopy(old),'route':route,'run_id':run_id or old['run_id']}


def _render(b):
    lines=['','## 후보 선별 근거 (AI 채택 여부와 별개)']
    info=b.get('ontology') or {}
    if info.get('status') != 'recorded':
        lines.append('온톨로지 근거 미보관 또는 불완전: '+str(info.get('errors') or '기존 브리핑'))
    kinds={'intraday_current':'장중 현재가','close_fallback':'종가 대체','daily_close':'일봉 종가'}
    routes={'general':'일반 선별','high_vol':'고변동 별도 몫','prep_fill':'장전 후보 보충','dip':'급락 예외 편입',
            'inverse':'인버스 ETF 별도 편입','index':'지수 ETF 별도 편입'}
    for row in b.get('candidates',[]):
        evidence=row.get('_ontology')
        if not evidence:
            lines.append(f"- {row['name']}: 온톨로지 근거 미보관"); continue
        ob=evidence['observation'];m=ob['measurements'];pc=ob['price_comparison']
        lines.append(f"- {row['name']} [{ob['observation_id']}] · {routes.get(evidence['admission']['route'],evidence['admission']['route'])}")
        if pc['gap'] is not None:
            result='충족' if pc['above'] else '불충족'
            lines.append(f"  {kinds[m['price']['kind']]} {pc['price']:,}원 > 20일선 {pc['ma20']:,}원: {result}; 이격률 {pc['gap']:+.1%}. "
                         f"같은 비교의 충족 여부와 정도이며 독립된 두 근거가 아님 [{ob['comparison_id']}].")
        else: lines.append('  20일선 비교·이격률: 미확인')
        price=m['price'];ma=m['ma20']
        lines.append(f"  가격 기준 {price.get('observed_at') or '미제공'}, 수신 {price.get('available_at') or '미보관'} ({price['source']}); "
                     f"20일선 일봉 기준 {ma.get('observed_at') or '미보관'}, 수신 {ma.get('available_at') or '미보관'} ({ma['source']}).")
        slope=m['ma20_slope5']['value']
        if slope is not None: lines.append(f'  완료 일봉의 20일선 5거래일 변화율 {slope:+.2%} (가격 위치와 별개).')
        for actor,label in [('foreign','외국인'),('institution','기관')]:
            v=m[actor];amount=v['value']
            direction='미확인' if amount is None else '순매수' if amount>0 else '순매도' if amount<0 else '실제 0'
            shown='미확인' if amount is None else f'{amount/1e8:+g}억 원'
            timing=f"기간 {v.get('period')}" if v.get('period') else f"공급자 기준 시각 {v.get('observed_at') or '미제공'}"
            lines.append(f"  {label} {shown} ({direction}, {'수량×선별 가격 추정액' if v['kind']=='intraday_estimate' else '누적 금액'}); "
                         f"{timing}, 수신 {v.get('available_at') or '미보관'} ({v['source']}).")
            if v['substituted']:
                note='원천 수급 미확인' if amount is None else '원천값과 기존 변환값이 다름'
                lines.append(f"  {label} {note}; 엔진 계산용 대체값 {v['effective_value']:g}원.")
        fs=ob['flow_summary']
        if fs['directions_opposed']: lines.append('  두 주체의 매매 방향이 엇갈림.')
        if fs['sum'] is not None: lines.append(f"  두 주체 수급 합계 {fs['sum']/1e8:+g}억 원.")
        elif fs['available_snapshot_sum'] is not None:
            lines.append(f"  수신한 두 값의 산술합 {fs['available_snapshot_sum']/1e8:+g}억 원; 공급자 기준 시각 일치 미확인, 동시점 합계로 확정하지 않음.")
        else: lines.append('  두 주체 수급 합계: 미확인.')
        if ob.get('flow_ratio') is not None:
            lines.append(f"  양수 부분만 합산한 후보용 수급 비율 {ob['flow_ratio']:.1%}: (max(외국인,0)+max(기관,0))/20일 평균 거래대금. 순수 합계와 다름.")
        cr=next(e for e in ob['criteria'] if e['criterion_id']=='CLOSE_CROSS_MA20')
        lines.append(f"  완료 일봉 상향 돌파 {cr['status']} · 비교 일봉 {ob['cross_period'] or '미보관'}; "
                     + ('오늘 종가 기준 돌파는 장중 미확정.' if ob['today_close_cross']=='unconfirmed' else ''))
        rules=[f"{e['rule_version_id']}:{e.get('status','계산')}" for e in ob['rules']]
        lines.append('  실제 사용 규칙 '+(', '.join(rules) or '일반 필터 미적용')+f" · 정의 {evidence.get('definitions_hash')}")
    return '\n'.join(lines)+'\n'


def render(b):
    try:
        return _render(b)
    except (KeyError, TypeError, ValueError) as exc:
        return f'\n온톨로지 근거 표시 실패 (미확인): {type(exc).__name__}\n'


class EvidenceCatalog:
    """The explicit display whitelist. Snapshots outside this list are not citable."""
    def __init__(self, delivery_id):
        self.delivery_id = delivery_id
        self.items = []

    def add(self, subject, field, value, unit, kind, *, observation_id=None,
            source='unrecorded', observed_at=None, available_at=None, status=None, **metadata):
        value = clean(value)
        item = {'evidence_id': f'{self.delivery_id}:e{len(self.items) + 1:04d}',
                'subject': str(subject), 'field': field, 'value': value, 'unit': unit,
                'kind': kind, 'status': 'unknown' if value is None else status or 'observed',
                'source': source, 'observed_at': observed_at, 'available_at': available_at,
                'observation_id': observation_id or f'{self.delivery_id}:{subject}', **clean(metadata)}
        self.items.append(item)
        return item['evidence_id']

    def measured(self, subject, field, m, *, observation_id, kind, **metadata):
        # Preserve source state; an engine's effective zero is never an observed zero.
        optional = {k: copy.deepcopy(m[k]) for k in
                    ('period', 'reason', 'provider_time_status', 'calculation', 'effective_value',
                     'substituted', 'price_ref', 'precision', 'price_basis', 'provider_version',
                     'provider_finality', 'request_parameters') if k in m}
        return self.add(subject, field, m.get('value'), m.get('unit', 'text'), m.get('kind', kind),
                        source=m.get('source', 'unrecorded'), observed_at=m.get('observed_at'),
                        available_at=m.get('available_at'), status=m.get('status'),
                        observation_id=observation_id, **{**optional, **metadata})


# Canonical units and the precision used by the existing calculator, never by sign checks.
DAILY_FIELDS = {'close': ('KRW', 0), 'ma5': ('KRW', 0), 'ma20': ('KRW', 0), 'ma60': ('KRW', 0),
                'ma20_slope5': ('ratio', 4), 'ret5': ('ratio', 4), 'ret20': ('ratio', 4),
                'rsi14': ('point', 1), 'vol20': ('ratio', 3), 'mdd60': ('ratio', 3),
                'trading_value_20d': ('KRW', None)}


def _row_catalog(cat, row, scope):
    code = row['code']
    sources = row.get('_sources') or {}
    daily = sources.get('daily') or {}
    ob = (row.get('_ontology') or {}).get('observation') or {}
    oid = ob.get('observation_id') or f'{cat.delivery_id}:{scope}:{code}'
    common = {'observation_id': oid, 'scope': scope}
    if row.get('position_episode_id'):
        common['position_episode_id'] = row['position_episode_id']
    cat.add(code, 'name', row.get('name'), 'text', 'derived', source='briefing.instrument', **common)
    done = set()
    for field, m in ob.get('measurements', {}).items():
        kind = 'daily_close' if field == 'close' else 'derived'
        extra = {'comparison_id': ob.get('comparison_id')} if field in ('price', 'ma20') else {}
        if field in DAILY_FIELDS and DAILY_FIELDS[field][1] is not None:
            extra['precision'] = DAILY_FIELDS[field][1]
        cat.measured(code, field, m, kind=kind, **common, **extra)
        done.add(field)
        if m.get('quantity') is not None:
            quantity_field = {'foreign': 'foreign_qty', 'institution': 'inst_qty'}.get(field, field + '_quantity')
            cat.measured(code, quantity_field, m['quantity'], kind='intraday_current', **common)
    for field, (unit, precision) in DAILY_FIELDS.items():
        if field in done or field not in row:
            continue
        extra = {'precision': precision} if precision is not None else {}
        if field == 'rsi14':
            extra['provenance'] = {'algorithm': 'gs_quant.relative_strength_index', 'window': 14,
                                   'calculation_version': 'daily-rsi14@v1',
                                   'input_snapshot_ref': f'briefing_snapshot.{scope}[code={code}]._sources.daily.close_series',
                                   'provider_finality': daily.get('provider_finality', 'unrecorded')}
        cat.add(code, field, row[field], unit, 'daily_close' if field == 'close' else 'derived',
                source=(daily.get('source') + '/GS Quant') if daily.get('source') else 'pykrx:ohlcv/GS Quant', observed_at=daily.get('asof'),
                price_basis=daily.get('price_basis', 'unrecorded'),
                available_at=daily.get('available_at'), **common, **extra)
    # The daily table remains present even when the selection used intraday flows.
    for actor, field in [('foreign', 'foreign_net'), ('institution', 'inst_net')]:
        if field in row:
            m = (sources.get('flow') or {}).get(actor)
            if m is None:
                m = measurement(row[field], 'KRW', 'legacy_briefing', reason='source_metadata_unrecorded')
            cat.measured(code, field, m, kind='daily_accumulated', **common)
    if 'rsi14_live' in row:
        rs = sources.get('rsi14_live') or {}
        rsi_common = {**common, 'observation_id': rs.get('observation_id') or f'{cat.delivery_id}:rsi:{code}'}
        cat.add(code, 'rsi14_live', row['rsi14_live'], 'point', 'intraday_estimate', precision=1,
                source='gs_quant.relative_strength_index', observed_at=rs.get('price', {}).get('observed_at'),
                available_at=rs.get('calculated_at'),
                provenance={**{k: copy.deepcopy(rs[k]) for k in ('calculation_version', 'algorithm', 'window', 'price',
                                                               'interpretation', 'reason', 'index_policy') if k in rs},
                            'input_snapshot_ref': f'briefing_snapshot.{scope}[code={code}]._sources.rsi14_live'}, **rsi_common)
        if rs.get('price'):
            pm = {'unit': 'KRW', **rs['price']}
            cat.measured(code, 'rsi14_live.price', pm, kind='intraday_current', **rsi_common)
    for field, unit in [('score', 'ratio'), ('activity', 'ratio'), ('chg', 'ratio'), ('value', 'KRW')]:
        if field in row:
            ps = sources.get('price') or {}
            cat.add(code, field, row[field], unit, 'derived', source='candidate_engine',
                    observed_at=ps.get('observed_at'), available_at=ps.get('available_at'), **common)
    if ob:
        for field, unit, formula in [('above', 'bool', 'price > ma20'), ('gap', 'ratio', 'price / ma20 - 1')]:
            precision = {'precision': 3, 'display_precision': {'unit': 'percent', 'decimals': 1}} if field == 'gap' else {}
            cat.add(code, 'price_comparison.' + field, ob.get('price_comparison', {}).get(field), unit,
                    'derived', source='ontology:price_comparison', comparison_id=ob.get('comparison_id'),
                    formula=formula, input_refs={'price': f'{oid}.measurements.price.value',
                                                 'ma20': f'{oid}.measurements.ma20.value'}, **common, **precision)
        fs = ob.get('flow_summary') or {}
        for field in ('sum', 'available_snapshot_sum', 'directions_opposed'):
            cat.add(code, 'flow_summary.' + field, fs.get(field), 'bool' if field == 'directions_opposed' else 'KRW',
                    'derived', source='ontology:flow_summary', same_period_confirmed=fs.get('same_period_confirmed', False),
                    reason=fs.get('reason'), formula='foreign * institution < 0' if field == 'directions_opposed' else 'foreign + institution',
                    temporal_scope='aligned' if field == 'sum' else 'received_values_only',
                    input_refs={a: f'{oid}.measurements.{a}.value' for a in ('foreign', 'institution')}, **common)
        if 'flow_ratio' in ob:
            cat.add(code, 'flow_ratio', ob['flow_ratio'], 'ratio', 'derived', source='candidate_engine',
                    precision=4 if ob.get('run_id', '').startswith('intraday-') else 3,
                    formula='(max(foreign,0)+max(institution,0))/trading_value_20d',
                    meaning='positive_parts_only',
                    input_refs={a: f'{oid}.measurements.{a}.effective_value' for a in ('foreign', 'institution')}, **common)
        for ev in ob.get('criteria', []):
            precision = {'precision': 3, 'display_precision': {'unit': 'percent', 'decimals': 1}} if ev['criterion_id'] == 'MA20_GAP' else {}
            cat.add(code, 'criterion.' + ev['criterion_id'] + '.' + ev['use'], ev.get('value'),
                    'ratio' if ev['criterion_id'] == 'MA20_GAP' else 'KRW' if ev['criterion_id'] == 'FLOW_SUM' else 'bool',
                    'derived', source=ev['criterion_version_id'], available_at=ev.get('evaluated_at'),
                    criterion_evaluation_id=ev['criterion_evaluation_id'], criterion_version_id=ev['criterion_version_id'],
                    comparison_id=ev.get('comparison_id'), input_refs=copy.deepcopy(ev.get('input_refs')),
                    use=ev['use'], **common, **precision)
    admission = (row.get('_ontology') or {}).get('admission')
    if admission:
        cat.add(code, 'admission.route', admission.get('route'), 'text', 'derived', source='candidate_engine', **common)
    for flag in ('high_vol', 'dip', 'inverse', 'index', 'from_prep'):
        if flag in row:
            cat.add(code, flag, row[flag], 'bool', 'derived', source='candidate_engine', **common)
    for n, news in enumerate(row.get('news') or []):
        cat.add(code, f'news.{n}.title', news.get('title'), 'text', 'news', status='reported_unverified',
                source=news.get('source', 'naver:news_search'), observed_at=news.get('published_at'),
                available_at=news.get('available_at'), url=news.get('url'), display_date=news.get('date'),
                quote_scope='stored_title_only', **common)
        if 'risk' in news:
            cat.add(code, f'news.{n}.risk', news['risk'], 'bool', 'derived', source='news_title_keyword_match', **common)


def _market_catalog(cat, market):
    daily = (market.get('_sources') or {}).get('daily') or {}
    for field, (unit, precision) in DAILY_FIELDS.items():
        if field not in market:
            continue
        cat.add('market', field, market[field], 'point' if unit == 'KRW' else unit,
                'daily_close' if field == 'close' else 'derived', source=market.get('source', 'unrecorded'),
                observed_at=market.get('asof'), available_at=daily.get('available_at'),
                precision=precision, observation_id=f'{cat.delivery_id}:market:daily')
    if 'rule_hint' in market:
        cat.add('market', 'rule_hint', market['rule_hint'], 'text', 'derived', source='data_prep.rule_hint',
                observed_at=market.get('asof'), available_at=daily.get('available_at'))
    live = market.get('live') or {}
    for field in ('close', 'ma5', 'ma20', 'ma60', 'ma20_slope5', 'rule_hint'):
        if field not in live:
            continue
        cat.add('market', 'live.' + field, live[field], 'text' if field == 'rule_hint' else 'ratio' if field == 'ma20_slope5' else 'point',
                'intraday_current' if field == 'close' else 'intraday_estimate',
                source=live.get('source', 'live:last_snapshot'), observed_at=live.get('observed_at'),
                available_at=live.get('available_at'), observation_id=f'{cat.delivery_id}:market:live',
                provenance={'calculation_version': (live.get('provenance') or {}).get('calculation_version'),
                            'input_snapshot_ref': 'briefing_snapshot.market.live.provenance'},
                interpretation='current_price_as_todays_close')
    for us in market.get('us') or []:
        for field, unit in [('close', 'point'), ('chg', 'ratio')]:
            cat.add('market', f'us.{us.get("symbol", us.get("name"))}.{field}', us.get(field), unit,
                    'daily_close' if field == 'close' else 'derived', source=us.get('source', 'yahoo:chart'),
                    observed_at=us.get('date'), available_at=us.get('available_at'), precision=2 if field == 'close' else 4,
                    observation_id=f'{cat.delivery_id}:market:us:{us.get("symbol", us.get("name"))}')


def _config_unit(prefix, value):
    unit = 'bool' if isinstance(value, bool) else 'text' if isinstance(value, str) else 'json' if isinstance(value, list) else 'number'
    leaf = prefix.rsplit('.', 1)[-1]
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        if (prefix.startswith('params.regime_cap.') or any(s in leaf for s in ('pct', 'ratio', 'weight', 'confidence', 'rate'))
                or leaf in ('drop', 'fraction', 'band', 'add', 'relax', 'full_at', 'daily_loss_limit', 'total_loss_limit',
                            'total_loss_cap_factor', 'max_buy_day_change', 'no_chase_change', 'spike_ret5', 'max_vol20',
                            'buy_threshold', 'probe_floor')):
            unit = 'ratio'
        elif leaf in ('buy_below', 'sell_at'):
            unit = 'point'
        elif 'days' in leaf:
            unit = 'trading_day'
        elif leaf == 'initial_capital' or any(s in leaf for s in ('value', 'cash', 'amount')):
            unit = 'KRW'
    return unit


def _config_catalog(cat, value, prefix):
    """Parameters are explicitly displayed here; internal underscore comments are not evidence."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not key.startswith('_'):
                _config_catalog(cat, item, prefix + '.' + key)
        return
    cat.add('config', prefix, value, _config_unit(prefix, value), 'config', source='local:' + prefix.split('.')[0])


def briefing_catalog(delivery_id, b, quotes, params, risk, context=None):
    cat = EvidenceCatalog(delivery_id)
    for scope in ('candidates', 'holdings'):
        for row in b.get(scope, []):
            _row_catalog(cat, row, scope)
    _market_catalog(cat, b.get('market') or {})
    for q in quotes:
        for field, unit in [('price', 'KRW'), ('change', 'ratio')]:
            cat.add(q['instrument_id'], field, q.get(field), unit,
                    q.get('kind', 'intraday_current') if field == 'price' else 'derived',
                    source=q.get('source', 'unrecorded'), observed_at=q.get('observed_at'),
                    available_at=q.get('available_at'), observation_id=q.get('observation_id'),
                    scope='briefing_quote', queried_at=q.get('queried_at'), reason=q.get('reason'))
    _config_catalog(cat, params, 'params')
    _config_catalog(cat, risk, 'risk')
    context = context or {}
    for index, lesson in enumerate(context.get('lessons') or []):
        cat.add('history', f'lessons.{index}', lesson, 'text', 'history', status='reported_unverified',
                source='local:lessons', available_at=context.get('lessons_available_at'))
    # Only the server's step-7 verifier can produce these rows. Candidates and
    # free-form legacy lessons never acquire an adopted-lesson identity here.
    for lesson in context.get('adopted_lessons') or []:
        import lesson_registry
        lesson_registry.validate_frozen_lesson(lesson, lesson['code'], stamp())
        cat.add(lesson['code'], 'adopted_lesson.' + lesson['lesson_version_id'], lesson['text'],
                'text', 'adopted_lesson', status='reported_unverified', source='verified_stage7_adoption',
                available_at=lesson['selected_at'],
                observation_id=f"{delivery_id}:lesson:{lesson['lesson_version_id']}:{lesson['code']}:{lesson['confidence_for']}",
                lesson_version_id=lesson['lesson_version_id'], proposal_id=lesson['proposal_id'],
                adoption_id=lesson['adoption_id'], validation_id=lesson['validation_id'],
                confidence_for=lesson['confidence_for'], source_case_ids=lesson['source_case_ids'],
                lesson_context=copy.deepcopy(lesson), verified_scope='adopted_guidance_not_market_truth')
    if context.get('prior_judgments'):
        cat.add('history', 'prior_judgments', context['prior_judgments'], 'text', 'history',
                source='common.prior_judgments', status='reported_unverified')
    if context.get('regime'):
        reg = context['regime']
        cat.add('market', 'ai_regime', reg.get('regime') if isinstance(reg, dict) else reg, 'text',
                'ai_classification', source='journal:set_regime', status='reported_unverified',
                observed_at=None, recorded_time_raw=(reg.get('time') or reg.get('ts')) if isinstance(reg, dict) else None)
    for field, value in (context.get('applied_config') or {}).items():
        cat.add('config', 'applied.' + field, value, _config_unit('applied.' + field, value),
                'config', source='get_briefing:applied_config')
    return cat.items


def render_catalog(catalog):
    # Each citable item (including its measurement semantics) is actually returned to the AI.
    # The complete internal briefing snapshot is deliberately NOT a citation whitelist.
    lines = ['\n## 인용 가능한 전달 근거',
             '아래 ID와 delivery_id로 구조화 사실을 제출하세요. null은 미확인이고 0과 다릅니다. '
             'news·history·ai_classification·adopted_lesson은 출처의 주장/분류/판단 지침이며 검증된 시장 사실이 아닙니다. '
             '가설과 자유문장은 사실 검증 대상 문장으로 인정하지 않습니다.', '```jsonl']
    lines.extend(json.dumps(clean(item), ensure_ascii=False, separators=(',', ':'), allow_nan=False) for item in catalog)
    return '\n'.join([*lines, '```', ''])


def _receipt(delivery_id, catalog, **extra):
    return {'schema_version': 2, 'delivery_id': delivery_id, 'ai_run_id': os.environ.get('RUN_ID'),
            'run_attempt_id': os.environ.get('RUN_ATTEMPT_ID'), 'created_at': stamp(),
            'recording_status': 'recorded', 'evidence_catalog': catalog,
            'observation_ids': list(dict.fromkeys(e['observation_id'] for e in catalog)),
            'criterion_evaluation_ids': [e['criterion_evaluation_id'] for e in catalog if e.get('criterion_evaluation_id')],
            **extra}


def delivery_error(exc):
    try:
        import judgment_coverage as JC
        JC.recording_error("evidence_delivery", exc)
    except Exception:
        pass


def deliver(text, b, quotes, params, risk, context=None):
    did = f'delivery-{uuid.uuid4().hex}'
    try:
        catalog = briefing_catalog(did, b, quotes, params, risk, context)
        output = text + render_catalog(catalog) + f'\n브리핑 전달 ID: {did}\n'
        value = _receipt(did, catalog, kind='briefing',
                         selection_run_id=(b.get('ontology') or {}).get('selection_run_id'),
                         briefing_snapshot=b, quotes=quotes, params=params, risk=risk, context=context or {},
                         text=output, sha256=hashlib.sha256(output.encode()).hexdigest())
        archive('deliveries', did, value)
    except Exception as exc:
        delivery_error(exc)
        return text + f'\n브리핑 전달 ID: {did}\n브리핑 전달 기록 저장 실패 (미보관): {type(exc).__name__}\n'
    return output


def account_delivery(payload):
    """Add a display catalog without changing any existing account field or broker call."""
    did = f'delivery-{uuid.uuid4().hex}'
    cat = EvidenceCatalog(did)
    received = stamp()
    for field, unit in [('mode', 'text'), ('total', 'KRW'), ('cash', 'KRW'), ('stock_value', 'KRW'),
                        ('daily_pnl', 'ratio'), ('regime', 'text'), ('equity_cap', 'ratio')]:
        if field in payload:
            cat.add('account', field, payload[field], unit, 'ai_classification' if field == 'regime' else 'account',
                    source='get_account:broker_and_local_policy', available_at=received,
                    status='reported_unverified' if field == 'regime' else None)
    for h in payload.get('holdings') or []:
        for field, unit in [('name', 'text'), ('qty', 'share'), ('avg_price', 'KRW'), ('price', 'KRW'),
                            ('value', 'KRW'), ('pnl_pct', 'ratio'), ('weight', 'ratio'), ('stop', 'KRW'),
                            ('take', 'KRW'), ('held_days', 'weekday_count'), ('max_days', 'weekday_count'), ('position_episode_id', 'text')]:
            if field in h:
                cat.add(h['code'], field, h[field], unit, 'account', source='get_account:positions_view',
                        available_at=received, observation_id=f'{did}:account:{h["code"]}')
    result = {**copy.deepcopy(payload), 'delivery_id': did, 'evidence_catalog': cat.items, 'recording_status': 'recorded'}
    try:
        body = json.dumps(clean(result), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        archive('deliveries', did, _receipt(did, cat.items, kind='account', payload=result,
                                            sha256=hashlib.sha256(body.encode()).hexdigest()))
    except Exception as exc:
        delivery_error(exc)
        result.update(recording_status='error', recording_error={'code': 'RECORDING_ERROR', 'reason': type(exc).__name__})
    return result
