"""Selected research details, called only after the existing MCP scope guard."""
import record_store as R
import storage_objects as O
import trace_pages


def detail(kind,identifier,index,field=None,offset=0):
    if type(index) is not int or index<0:raise ValueError('INVALID_DETAIL_INDEX')
    context=None
    if kind=='review':
        import case_store
        row=case_store.load('datasets',identifier)
        cases=(row or {}).get('cases',[])
    elif kind=='validation':
        import policy_validation
        row=policy_validation.get_policy_validation(identifier)
        cases=row.get('cases',[])
    elif kind=='proposal':
        import learning_proposals
        context=learning_proposals._load_context(identifier)
        cases=context['cases']
    else:raise ValueError('UNKNOWN_RESEARCH_SCOPE')
    if index>=len(cases):return {'schema_version':2,'status':'not_found'}
    value=cases[index];key=str(index)
    loaded={'view_id':kind+':'+identifier,'projection':{'traces':{key:value}}}
    result=trace_pages.chunk(loaded,'trace',key,field,offset) if field else trace_pages.detail(loaded,'trace',key)
    if not field:
        for info in result.get('item',{}).get('_detail_links',{}).values():
            info['detail_ref']={'scope':kind+':'+identifier,'index':index,'field':info['detail_ref']['field']}
    if context is not None:
        import learning_proposals
        complete=not field
        ids=[value['application_id']] if complete else []
        catalog=[r for r in context['catalog'] if r.get('application_id') in ids]
        if O.logical_size(catalog)>400*1024:catalog=[];ids=[];result['catalog_complete']=False
        result['catalog']=catalog
        return learning_proposals._deliver(context,result,catalog=catalog,case_ids=ids)
    return result
