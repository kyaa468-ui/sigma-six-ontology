"""Page and selected-detail projections of a pinned private view."""
from __future__ import annotations
import json
import record_store as R
import storage_objects as O

LISTS={'targets':'trace_id','cases':'application_id','proposals':'proposal_id',
       'validations':'result_id','policies':'activation_id','reviews':'review_id'}
FIELDS=('trace_id','application_id','proposal_id','validation_id','result_id','review_id',
        'activation_id','code','created_at','confidence_for','coverage_status','connection_status',
        'action','visibility','outside_expected_denominator','status','title','kind','version',
        'proposal_version','validation_ids','trace_ids','market_as_of','selected_market_as_of',
        'stale_evaluation','outcome_eligible','outcome','state','stratum','verdict',
        'evaluation_status','period_status','recording_status','policy_revision','package_id',
        'adoption_id','audit_status','activation_commit_status')


def summary(loaded):
    projection=loaded['projection']
    return {'schema_version':2,'status':loaded['status'],'view_id':loaded['view_id'],
        'manifest':{k:loaded['manifest'].get(k) for k in ('created_at','market_as_of','knowledge_as_of','status')},
        'projection':{k:projection.get(k) for k in ('schema_version','status','market_as_of','knowledge_as_of','protected_count')},
        'counts':{**{name:len(projection.get(name,[])) for name in LISTS},
                  'sources':len(projection.get('source_manifest',[])),
                  'errors':len(projection.get('errors',[]))},
        'errors':list(loaded.get('errors',[]))[:50]}


def list_page(loaded,name,*,cursor=None,limit=50,search=''):
    if name not in LISTS:raise ValueError('UNKNOWN_RELATION')
    projection=loaded['projection'];key=LISTS[name]
    def rows():
        for index,row in enumerate(projection.get(name,[])):
            compact={k:row[k] for k in FIELDS if k in row}
            compact['id']=str(row.get(key) or row.get('validation_id') or f'item-{index:012}')
            if name=='validations':
                result=row.get('result') or {}
                compact['result']={k:result.get(k) for k in ('mode','baseline_status','effect_status','adoption_eligibility')}
                compact['result']['performance']={'status':(result.get('performance') or {}).get('status')}
            if name=='proposals':compact['readiness']={'status':(row.get('readiness') or {}).get('status')}
            if name=='policies' and isinstance(row.get('current_applicability'),dict):
                compact['current_applicability']={k:row['current_applicability'].get(k) for k in ('status','policy_revision','package_id','activation_id','adoption_id')}
            if search and search.casefold() not in json.dumps(compact,ensure_ascii=False).casefold():continue
            yield compact
    return R.page(rows(),cursor=cursor,limit=limit,scope=loaded['view_id']+':'+name+':'+search,key=lambda x:x['id'])


def selected(loaded,kind,identifier):
    projection=loaded['projection']
    if kind=='trace':
        value=projection.get('traces',{}).get(identifier)
    else:
        relation={'case':'cases','proposal':'proposals','validation':'validations','policy':'policies'}.get(kind)
        if relation is None:raise ValueError('UNKNOWN_RELATION')
        key=LISTS[relation]
        value=next((row for row in projection.get(relation,[]) if str(row.get(key) or row.get('validation_id'))==identifier),None)
    if value is None:return {'status':'not_found','id':identifier}
    return value


def _small(row,fields):
    return {key:row[key] for key in fields if key in row and O.field_size(row,key)<=8192}


def _trace_preview(value,field,budget):
    """Display values only; the original branch stays reachable by chunk link."""
    def observation(row):
        row=row.get('observation',row)
        return _small(row,('observation_id','instrument_id','instrument','measurements','price_comparison','flow_summary','criteria','comparison_id'))
    def judgment(row):
        result=_small(row,('judgment_id','recommended_action','confidence_for','confidence_raw','decision','reason','decision_regime_snapshot','confidence_at_submission'))
        result['confidence_at_submission']={'details':_small((row.get('confidence_at_submission') or {}).get('details') or {},('raw','adjusted','adjustments','note','cap','capped','round_digits'))}
        result['arguments']=[{**_small(arg,('kind','fact_validation','quote_validation','server_summary','text','quote','proposer','adoption','evidence','assertion','references','claim_ids')),
             'evidence_snapshots':[_small(item,('kind','value','unit','period','observed_at','available_at','source','observation_id','evidence_id')) for item in arg.get('evidence_snapshots',[])[:30]]} for arg in row.get('arguments',[])[:30]]
        result['unmentioned_evidence_count']=len(row.get('unmentioned_evidence',[]))
        if row.get('wire_submission'):
            original = row['wire_submission']['original']
            result['wire_submission'] = {'original': {
                'wire_format': original.get('wire_format'), 'bundle_refs': original.get('bundle_refs'),
                'arguments': [{k: a[k] for k in ('id','kind','evidence') if k in a}
                              for a in original.get('arguments', [])[:30]]}}
        return result
    def evaluation(row):
        row=row.get('evaluation',row)
        result=_small(row,('evaluation_id','application_id','premise_application_id','verdict','period_status','data_status','market_as_of','knowledge_as_of','evaluator_version'))
        first=row.get('first_known_refutation')
        result['first_known_refutation']=_small(first,('date','close','ma20','available_at')) if isinstance(first,dict) else first
        result['comparisons']=[_small(item,('date','close','ma20','status','confirmation','reason')) for item in row.get('comparisons',[])[:30]]
        return result
    makers={'observations':observation,'judgments':judgment,'evaluations':evaluation,
            'deliveries':lambda row:{**_small(row,('delivery_id','delivered_at','tool','wire_format','bundle_alias')),
                **({'wire_view': {**_small(row['wire_view'], ('wire_format','bundle_alias','renderer_version','display_status','visibility_note')),
                                  'payload_available_in_delivery_detail': True}} if row.get('wire_view') else {}),
                'additional_wire_views': [{**_small(w, ('wire_format','bundle_alias','renderer_version','display_status','visibility_note','detail_request')),
                                           'payload_available_in_delivery_detail': True} for w in row.get('additional_wire_views', [])],
                'quotes':[_small(item,('instrument_id','price','kind','observed_at','available_at','observation_id')) for item in row.get('quotes',[])[:30]]}}
    if field=='semantic_execution':
        result={'eligibility':value.get('eligibility'), 'visibility':value.get('visibility'), 'stages':{}}
        for role,stage in (value.get('stages') or {}).items():
            if not stage:
                result['stages'][role]=None
                continue
            summary=_small(stage,('stage_id','role','status','input_id','runtime_call_id','confidence_raw','confidence_band_id','decision'))
            for key in ('evaluations','objections'):
                summary[key]=list(stage.get(key,[]))[:20]
                summary['evaluation_count' if key=='evaluations' else 'objection_count']=len(stage.get(key,[]))
            result['stages'][role]=summary
        return result if O.logical_size(result)<=budget else {'status':'detail_required'}
    if field=='execution_context':
        rows=value.get('orders',[])
        result={'orders':[{**_small(row,('order_attempt_id','path_kind','link_status')),
                          'summary':_small(row.get('summary') or {},('requested_qty','confirmed_qty','unconfirmed_qty','confirmation_status','confirmed_average_price'))} for row in rows[:30]],
                'order_count':len(rows),'non_order_paths':[_small(row,('path_kind','kind','action','link_status')) for row in value.get('non_order_paths',[])[:30]]}
        return result if O.logical_size(result)<=budget else {}
    if field not in makers:return None
    output=[];used=2
    for row in value[:30]:
        compact=makers[field](row);size=O.logical_size(compact)
        if used+size>budget:break
        output.append(compact);used+=size+1
    return output


def _selected_preview(value,kind,field,budget):
    if kind=='trace':return _trace_preview(value,field,budget)
    result=None
    if kind=='validation' and field=='result':
        result=_small(value,('mode','baseline_status','effect_status','gaps','operational_activation'))
        for name,fields in {'performance':('status','gaps','missing_inputs','reasons'),
                            'adoption_eligibility':('status','reasons'),
                            'independent_validation':('status',)}.items():
            result[name]=_small(value.get(name) or {},fields)
    elif kind=='case' and field=='evaluation':
        result=_trace_preview([value],'evaluations',budget)
        result=result[0] if result else {}
    elif kind=='proposal' and field=='readiness':
        result=_small(value,('status','reasons'))
    elif kind=='policy' and field=='current_applicability':
        result=_small(value,('status','policy_revision','package_id','activation_id','adoption_id'))
    return result if result is None or O.logical_size(result)<=budget else {}


def detail(loaded,kind,identifier,*,field=None,cursor=None,limit=50):
    if kind=='validation' and field is None:
        records=loaded['projection'].get('validations',[])
        if not any(row.get('result_id')==identifier for row in records):
            choices=[{k:row[k] for k in FIELDS if k in row} for row in records if row.get('validation_id')==identifier and row.get('result_id')]
            if choices:
                page=R.page(choices,cursor=cursor,limit=limit,scope=loaded['view_id']+':validation-choices:'+identifier,key=lambda row:row['result_id'])
                return {**page,'status':'choices','kind':kind,'id':identifier}
    value=selected(loaded,kind,identifier)
    if value.get('status')=='not_found':return value
    if field is not None:
        if not isinstance(field,str) or field not in value:raise ValueError('UNKNOWN_DETAIL_FIELD')
        child=value[field]
        if not isinstance(child,list):raise ValueError('DETAIL_FIELD_NOT_SEQUENCE')
        return R.page(({'id':f'{i:012}','value':row} for i,row in enumerate(child)),cursor=cursor,
                      limit=limit,scope=loaded['view_id']+':'+kind+':'+identifier+':'+field)
    # Summary preserves every field's reachability. Large branches are explicit
    # links; a browser never downloads raw source as a side effect of opening it.
    output={};links={};remaining=R.PAGE_BYTES-4096
    for key in value:
        size=O.field_size(value,key)
        if size<=min(128*1024,remaining):output[key]=value[key];remaining-=size
        else:
            typ=O.field_kind(value,key)
            preview_fields={'trace':('observations','judgments','evaluations','deliveries','execution_context','semantic_execution'),
                            'validation':('result',),'case':('evaluation',),
                            'proposal':('readiness',),'policy':('current_applicability',)}
            preview=_selected_preview(value[key],kind,key,min(64*1024,remaining)) if key in preview_fields.get(kind,()) else None
            output[key]=preview if preview is not None else [] if typ=='list' else {} if typ=='dict' else None
            remaining-=O.logical_size(output[key])
            links[key]={'status':'detail_required','logical_bytes':size,
                        'preview_only':preview is not None,
                        'detail_ref':{'kind':kind,'id':identifier,'field':key}}
    if links:output['_detail_links']=links
    return {'schema_version':2,'status':'available','item':output,'kind':kind,'id':identifier,'complete':not links}


def relations(loaded,kind,identifier,relation,*,cursor=None,limit=50):
    allowed={('trace','cases'),('case','proposals'),('proposal','validations')}
    if (kind,relation) not in allowed:raise ValueError('UNKNOWN_RELATION')
    source=selected(loaded,kind,identifier)
    if source.get('status')=='not_found':return source
    def rows():
        if source.get('visibility') in ('protected','reserved'):return
        for row in loaded['projection'].get(relation,[]):
            matches=(identifier in row.get('trace_ids',[]) if kind=='trace' else
                     identifier in [*row.get('support_case_ids',[]),*row.get('counter_case_ids',[])] if kind=='case' else
                     row.get('proposal_id')==identifier)
            if matches:
                yield {k:row[k] for k in FIELDS if k in row}
    return R.page(rows(),cursor=cursor,limit=limit,scope=loaded['view_id']+':'+kind+':'+identifier+':'+relation,
                  key=lambda row:str(row.get(LISTS[relation]) or row.get('validation_id')))


def chunk(loaded,kind,identifier,field,offset=0):
    if type(offset) is not int or offset<0:raise ValueError('INVALID_OFFSET')
    value=selected(loaded,kind,identifier)
    if field not in value:raise ValueError('UNKNOWN_DETAIL_FIELD')
    # Chunk canonical bytes without accumulating a complete encoded document.
    import base64
    position=0;parts=[];used=0;more=False
    for raw in O.field_chunks(value,field):
        end=position+len(raw)
        if end>offset and used<R.CHUNK_BYTES:
            piece=raw[max(0,offset-position):max(0,offset-position)+R.CHUNK_BYTES-used]
            parts.append(piece);used+=len(piece)
        position=end
        if position>offset+used:more=True;break
    return {'schema_version':2,'encoding':'base64','data':base64.b64encode(b''.join(parts)).decode(),
            'offset':offset,'next_offset':offset+used if more else None,'complete':not more}
