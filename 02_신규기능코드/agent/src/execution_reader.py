"""Replayable metadata-filtered execution relations; no archive-sized lists."""
from itertools import chain
from pathlib import Path
import record_store as R


class EventRows:
    def __init__(self,root,errors):self.root=Path(root);self.errors=errors
    def _rows(self,filters=None):
        errors=[]
        try:
            for row in R.iter_records(self.root,filters=filters,errors=errors):
                if row.get('kind')!='engine_definition':yield row
        finally:
            for error in errors:
                relative=Path(error.get('record',''))
                run=relative.parts[1] if len(relative.parts)>1 else None
                item={**error,'run_attempt_id':run}
                if item not in self.errors:self.errors.append(item)
    def __iter__(self):return self._rows()
    def __add__(self,other):return chain(self,other)
    def matching(self,ids):
        seen=set()
        for key in sorted(ids):
            for value in sorted(ids[key]):
                queries=[{key:value}]+([{'event_id':value}] if key=='rule_trigger_id' else [])
                for row in chain.from_iterable(self._rows(query) for query in queries):
                    identifier=row.get('event_id')
                    if identifier not in seen:
                        seen.add(identifier);yield row


class DispatchRows:
    def __init__(self,connect,decode,errors):self.connect=connect;self.decode=decode;self.errors=errors
    def _rows(self,ids=None,run=None):
        db=None
        try:
            db=self.connect(readonly=True);where=[];params=[]
            for key,values in (ids or {}).items():
                if key not in ('plan_id','order_attempt_id','judgment_id','position_episode_id','rule_trigger_id'):raise ValueError('INVALID_DISPATCH_QUERY')
                for value in sorted(values):
                    if key in ('plan_id','order_attempt_id'):
                        where.append(key+'=?');params.append(value)
                    where.append('json_extract(payload_json,?)=?');params.extend(['$.'+key,value])
            query='SELECT * FROM order_attempts'
            if where:query+=' WHERE ('+' OR '.join(where)+')'
            if run is not None:
                query+=(' AND ' if where else ' WHERE ')+'run_attempt_id=?';params.append(run)
            for row in db.execute(query+' ORDER BY created_at',params):yield self.decode(row)
        except Exception as exc:
            error={'record':'order_dispatch.sqlite3','error':str(exc)}
            if error not in self.errors:self.errors.append(error)
        finally:
            if db is not None:db.close()
    def __iter__(self):return self._rows()
    def matching(self,ids):return self._rows(ids)
    def for_run(self,run):return self._rows(run=run)
